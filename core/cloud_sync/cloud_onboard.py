#!/usr/bin/env python3
"""bugx 云端同步 onboarding + V2-only 同步编排（DOC-008）。

- onboarding 状态机（项目级）：pending / done / local；旧 dismissed 只作为待迁移状态。
- 首启交互钩子：项目启动时若 pending → 未认证询问「立即登录 / 本地模式 / 下次再说」，
  已认证询问「云端同步 / 本地模式 / 下次再说」。
- Push（§6.2）：从 Ledger state 生成确定性 operation_id → outbox 幂等入队 →
  V2 push（含 Tool Experience 聚合与 tombstones 占位）→ ack / 重试退避 / dead-letter。
  Pull（§6.3）：读 committed cursor → 分页拉取并逐页暂存 inbox → 全量校验 →
  tombstone/冲突过滤 → Supervisor 原子导入 → 「inbox imported + cursor 推进」
  原子提交 → quarantine 落地仅作诊断。

不变量：显式 quarantine 导入只能为 candidate；私有多设备同步按远端治理状态恢复
active/candidate；committed cursor 只随成功导入推进；
任一页失败时 Ledger、旧 snapshot 与 committed cursor 均不变化（AD-008-02/03）。
"""

from __future__ import annotations

import hashlib
import json
# Kept as a module-level seam for tests that inject atomic replace failures.
import os  # noqa: F401
import re
import secrets
import subprocess
import sys
from functools import wraps
from pathlib import Path
from typing import Any

from core.secure_atomic import AtomicWriteError, atomic_write_json

try:
    from core.runtime import interaction
except ImportError:  # pragma: no cover - legacy snapshots without the helper.
    interaction = None  # type: ignore[assignment]

CRC = None  # CloudClient 延迟导入（依赖 core/ 在 sys.path）

RECORD_TYPES = ("patterns", "incidents", "tool_experience", "consumption_events", "tombstones")
PROJECT_IDENTITY_SCHEMA_VERSION = 2
PROJECT_KEY_RE = re.compile(r"^bxpk_[A-Za-z0-9_-]{22,96}$")




class CloudOnboardError(RuntimeError):
    """同步前本地事实源或落地区不可安全使用。"""


class CloudOnboardDecisionRequired(CloudOnboardError):
    """启动时必须询问用户，但当前进程没有可用交互终端。"""

    def __init__(self, message: str, *, request_emitted: bool = False) -> None:
        super().__init__(message)
        self.request_emitted = request_emitted


def get_imports(**extra_ns):
    global CRC
    from core.cloud_sync.cloud_client import (
        CloudClient, get_device_id, get_device_label,
        incident_to_wire, lesson_to_wire,
    )
    from core.cloud_sync.cloud_schema import manifest_hash, validate_records
    from core.ledger.lesson_ledger import (
        add_incident, add_lesson, import_synced_records, initialize as init_ledger,
        database_path, import_tool_experience, list_incidents,
        read as read_ledger, reset_cloud_scope, tool_metrics,
        synced_pattern, synced_incident,
    )
    from core.runtime.project_profile import (
        ensure_project_profile, load_project_profile, save_project_profile,
        wire_project_profile,
    )
    from core.bugx_paths import global_knowledge_root, project_knowledge_root
    from core.retrieval import knowledge_sync
    from core.runtime import sync_store as ss
    ns = {"CloudClient": CloudClient, "get_device_id": get_device_id,
           "get_device_label": get_device_label,
          "incident_to_wire": incident_to_wire, "lesson_to_wire": lesson_to_wire,
          "read_ledger": read_ledger, "list_incidents": list_incidents,
          "add_lesson": add_lesson, "add_incident": add_incident,
          "import_synced_records": import_synced_records,
          "synced_pattern": synced_pattern,
          "synced_incident": synced_incident,
          "reset_cloud_scope": reset_cloud_scope,
          "import_tool_experience": import_tool_experience,
          "init_ledger": init_ledger, "database_path": database_path,
          "tool_metrics": tool_metrics,
          "load_project_profile": load_project_profile,
          "save_project_profile": save_project_profile,
          "ensure_project_profile": ensure_project_profile,
          "wire_project_profile": wire_project_profile,
          "manifest_hash": manifest_hash, "validate_records": validate_records,
          "build_private_sync_records": knowledge_sync.build_private_sync_records,
          "resolve_detail_mode": knowledge_sync.resolve_detail_mode,
          "strip_to_summary": knowledge_sync.strip_to_summary,
          "enforce_credential_block": knowledge_sync.enforce_credential_block,
          "SyncStore": ss.SyncStore,
          "global_knowledge_root": global_knowledge_root,
          "project_knowledge_root": project_knowledge_root}
    ns.update(extra_ns)
    return ns


def _onb_path(root: Path) -> Path:
    return root / ".bugx" / "cloud-onboard.json"


def _project_binding_path(root: Path) -> Path:
    return root / ".bugx" / "cloud-project.json"


def load_project_binding_info(root: Path) -> dict[str, str]:
    path = _project_binding_path(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise CloudOnboardError("项目绑定文件路径不安全")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        project_id = value.get("project_id")
        if not project_id:
            raise CloudOnboardError("项目绑定文件缺少 project_id")
        result = {"project_id": str(project_id), "slug": str(value.get("slug") or "")}
        if value.get("account_id") not in (None, ""):
            result["account_id"] = str(value["account_id"])
        if value.get("project_key") not in (None, ""):
            result["project_key"] = str(value["project_key"])
        return result
    except CloudOnboardError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise CloudOnboardError("项目绑定文件格式无效") from exc


def load_project_binding(root: Path) -> str:
    return load_project_binding_info(root).get("project_id", "")


def _save_project_binding(root: Path, project_id: str, slug: str,
                          project_key: str, account_id: str = "") -> None:
    validate_project_key(project_key)
    path = _project_binding_path(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise CloudOnboardError("项目绑定文件路径不安全")
    try:
        payload = {
            "schema_version": PROJECT_IDENTITY_SCHEMA_VERSION,
            "project_id": project_id,
            "project_key": project_key,
            "slug": slug,
        }
        if account_id:
            payload["account_id"] = account_id
        atomic_write_json(path, payload, mode=0o600, indent=None)
    except (AtomicWriteError, OSError) as exc:
        raise CloudOnboardError(f"项目绑定文件写入失败: {path}") from exc


def committed_identity_path(root: Path) -> Path:
    return root / ".bugx-project.json"


def validate_project_key(project_key: str) -> str:
    if not isinstance(project_key, str) or not PROJECT_KEY_RE.fullmatch(project_key.strip()):
        raise CloudOnboardError("项目身份 project_key 格式无效")
    return project_key.strip()


def _new_project_key() -> str:
    return "bxpk_" + secrets.token_urlsafe(32)


def _git_repo_identity(root: Path) -> str:
    """返回跨设备稳定的仓库身份（origin URL + 首个 commit）。

    同一仓库在多台设备 clone 后必须得到同一身份，否则每台设备都会生成
    随机 key，服务端 upsert 认不出是同一项目而创建重复项目。无 remote
    或 git 不可用时返回空串，由调用方回退随机 key（本地一次性仓库）。
    """
    def _git(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    remote = _git("config", "--get", "remote.origin.url")
    if not remote:
        return ""
    roots = [line.strip() for line in _git("rev-list", "--max-parents=0", "HEAD").splitlines()]
    return f"{remote}\n{roots[0] if roots else ''}"


def derive_project_key(root: Path) -> str:
    """优先由仓库身份确定性推导 project_key，无法推导时才随机生成。

    确定性 key 让 ``register_project`` 的 upsert 真正生效：克隆仓库在
    任何设备首次运行时得到同一 ``project_key``，服务端解析到既有项目而
    不是新建一个重复项。已存在的 binding/committed 身份始终优先，绝不
    因本函数而改写既有项目。
    """
    identity = _git_repo_identity(root)
    if identity:
        return "bxpk_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _new_project_key()


def load_committed_identity(root: Path) -> dict[str, str]:
    """读取随仓库提交的稳定项目身份文件。

    v1 文件只含 slug，作为一次性旧项目线索；v2 才是跨设备绑定的正式
    ``project_key``。任何损坏文件都必须显式失败，禁止路径哈希兜底。
    """
    path = committed_identity_path(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise CloudOnboardError("项目身份文件路径不安全")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise CloudOnboardError("项目身份文件格式无效") from exc
    if not isinstance(value, dict):
        raise CloudOnboardError("项目身份文件必须是对象")
    schema = value.get("schema_version", 1)
    if schema not in (1, PROJECT_IDENTITY_SCHEMA_VERSION):
        raise CloudOnboardError("项目身份文件 schema_version 不受支持")
    slug = str(value.get("slug") or "").strip()
    if not slug:
        raise CloudOnboardError("项目身份文件缺少 slug")
    result = {"schema_version": str(schema), "slug": slug}
    if schema == PROJECT_IDENTITY_SCHEMA_VERSION:
        result["project_key"] = validate_project_key(str(value.get("project_key") or ""))
    return result


def _load_committed_slug(root: Path) -> str:
    """保留给只读显示路径的辅助函数；正式注册必须使用 project_key。"""
    return load_committed_identity(root).get("slug", "")


def save_committed_identity(root: Path, slug: str, project_key: str | None = None) -> str:
    key = validate_project_key(project_key or _new_project_key())
    path = committed_identity_path(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise CloudOnboardError("项目身份文件路径不安全")
    try:
        # 0o644：key 不含秘密，需要随仓库提交，不能按秘密文件权限存放。
        atomic_write_json(path, {
            "schema_version": PROJECT_IDENTITY_SCHEMA_VERSION,
            "project_key": key,
            "slug": slug,
        }, mode=0o644, indent=None)
    except (AtomicWriteError, OSError) as exc:
        raise CloudOnboardError(f"项目身份文件写入失败: {path}") from exc
    return key


def get_state(root: Path) -> str:
    path = _onb_path(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise CloudOnboardError(f"onboarding 状态路径不安全: {path}")
    if not path.exists():
        return "pending"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        state = value.get("state") if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise CloudOnboardError(f"onboarding 状态无法读取: {path}") from exc
    if state not in {"pending", "done", "local", "dismissed", "sync"}:
        raise CloudOnboardError(f"onboarding 状态无效: {state!r}")
    return state


def set_state(root: Path, state: str) -> None:
    if state not in {"pending", "done", "local", "dismissed", "sync"}:
        raise ValueError(f"无效 onboarding 状态: {state}")
    p = _onb_path(root)
    if p.is_symlink() or p.parent.is_symlink():
        raise CloudOnboardError(f"onboarding 状态路径不安全: {p}")
    try:
        atomic_write_json(p, {"state": state}, mode=0o600, indent=None)
    except (AtomicWriteError, OSError) as exc:
        raise CloudOnboardError(f"onboarding 状态写入失败: {p}") from exc


# ---------------------------------------------------------------------------
# 隔离落地（quarantine，仅诊断；绝不进入运行时检索）
# ---------------------------------------------------------------------------

def landing_path(root: Path, layer: str) -> Path:
    ns = get_imports()
    base = ns["global_knowledge_root"]() if layer == "global" else ns["project_knowledge_root"](root)
    return base / "cloud-synced.json"


def landing_store(root: Path, layer: str, records: dict[str, Any] | None,
                  cursor: str, metadata: dict[str, Any] | None = None) -> bool:
    """落地到独立「云同步知识区」（quarantine），不写 local ledger。

    V2 起 payload 为完整 records 结构（五类记录恒在）；cursor 仅是诊断快照，
    权威 committed cursor 存于 sync store。
    """
    try:
        p = landing_path(root, layer)
        if p.is_symlink():
            return False
        # Only the controlled landing directory is in scope.  Rejecting
        # symlinked system ancestors (for example macOS /var) would make safe
        # temporary/project roots unusable while adding no protection.
        if p.parent.is_symlink():
            return False
        payload_records = records if isinstance(records, dict) else {
            kind: [] for kind in RECORD_TYPES}
        # The landing file is an explicit-import boundary.  Always bind it to
        # the exact four-category payload that was written; callers must never
        # be able to supply an unrelated/empty manifest hash.
        ns = get_imports()
        ns["validate_records"](payload_records)
        payload = {"schema_version": 2, "scope": layer, "cursor": cursor,
                   "manifest_hash": ns["manifest_hash"](payload_records)}
        if metadata:
            payload.update(metadata)
        payload["manifest_hash"] = ns["manifest_hash"](payload_records)
        payload["records"] = payload_records
        atomic_write_json(p, payload, mode=0o600, indent=2)
        return True
    except (AtomicWriteError, OSError, ValueError):
        return False


def get_pattern_list(ns, ledger: Path) -> list:
    try:
        payload = ns["read_ledger"](ledger)
        lessons = payload.get("lessons") if isinstance(payload, dict) else None
        if not isinstance(lessons, dict):
            raise CloudOnboardError(f"本地错题集格式无效: {ledger}")
        return list(lessons.values())
    except CloudOnboardError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CloudOnboardError(f"无法读取本地错题集: {ledger}: {exc}") from exc


def get_incident_list(ns, ledger: Path) -> list:
    try:
        return ns["list_incidents"](ledger)
    except Exception as exc:  # noqa: BLE001
        raise CloudOnboardError(f"无法读取本地经验集: {ledger}: {exc}") from exc


def ensure_ledger(ns, ledger: Path) -> Path:
    """读取前保证本地 ledger 存在（pull 目标可能尚未初始化）。"""
    try:
        if not ns["database_path"](ledger).is_file():
            ns["init_ledger"](ledger)
        ns["read_ledger"](ledger)
        return ledger
    except Exception:  # noqa: BLE001
        ns["init_ledger"](ledger)
        return ledger


def layer_ledger(ns, root: Path, layer: str) -> Path:
    if layer == "global":
        return ns["global_knowledge_root"]() / "global.json"
    return ns["project_knowledge_root"](root) / "lesson-ledger.json"


# ---------------------------------------------------------------------------
# Sync store 绑定
# ---------------------------------------------------------------------------

def resolve_store_path(ns, root: Path, layer: str) -> Path:
    if layer == "global":
        return ns["global_knowledge_root"]().parent / "cloud-sync" / "state.sqlite3"
    return ns["project_knowledge_root"](root).parent / "cloud-sync" / "state.sqlite3"


def get_account_id(client: Any) -> str:
    cred = getattr(client, "cred", None)
    user = getattr(cred, "user", None)
    if isinstance(user, dict):
        for key in ("id", "user_id", "account_id", "sub"):
            value = user.get(key)
            if value not in (None, ""):
                return str(value)
    authenticated = False
    try:
        authenticated = bool(cred and cred.is_authenticated())
    except Exception:  # noqa: BLE001
        authenticated = True
    if authenticated:
        raise CloudOnboardError(
            "云端凭据已认证但认证响应缺少稳定 account_id，拒绝同步以避免账号状态串用"
        )
    return ""


# ---------------------------------------------------------------------------
# Push（DOC-008 §6.2）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pull（DOC-008 §6.3 / §6.4）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------

def response_version_field(data: Any, keys: tuple[str, ...] = ("snapshot_version", "version")) -> int | None:
    """响应版本字段的类型守卫（与 cloud_schema._int_field(minimum=0) 同语义）。

    兼容省略版本字段的旧网关：缺字段/空值返回 None；字段一旦出现就必须是
    非负真整数——bool/浮点/数字字符串一律拒绝，防止被胁迫进 remote_version
    推进路径后悄悄改变本地同步基线。
    """
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data.get(key) is not None:
            value = data[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CloudOnboardError(f"响应字段 {key} 必须是非负整数: {value!r}")
            return value
    return None


def _sync_project_profile(client, ns, root: Path, project_id: str) -> dict[str, Any]:
    """Synchronize one project profile through its metadata endpoint.

    A profile is a single project aggregate, so a 409 is resolved by one
    remote read and one retry.  It never enters the knowledge outbox/cursor.
    """
    if not hasattr(client, "get_project_profile") or not hasattr(client, "put_project_profile"):
        return {"ok": True, "detail": "当前云端客户端不支持项目画像接口，已跳过"}
    ledger_path = ns["project_knowledge_root"](root) / "lesson-ledger.json"
    local = ns["load_project_profile"](ledger_path)
    if not local:
        # Direct ``bugx cloud sync`` is also a supported entry point.  A
        # project that has not yet reached startup preparation may not have a
        # ledger at all; knowledge sync remains usable and the next startup
        # will create the profile.  When the ledger exists, create the profile
        # here so the metadata path is still self-healing.
        database = ledger_path.with_suffix(".sqlite3")
        if database.is_file() and not database.is_symlink():
            local, _ = ns["ensure_project_profile"](
                ledger_path, root, producer_version="cloud-sync",
            )
        else:
            return {"ok": True, "detail": "本地项目画像尚未生成，已跳过画像同步"}
    vulnerability_cache = _warm_vulnerability_cache(client, local)
    remote = client.get_project_profile(project_id)
    if remote.ok:
        data = client._data(remote)
        remote_profile = data.get("profile") if isinstance(data.get("profile"), dict) else data
        remote_hash = str(data.get("profile_hash") or remote_profile.get("profile_hash") or "")
        remote_revision = int(data.get("profile_revision") or data.get("revision") or 0)
        if remote_hash and remote_hash == local.get("profile_hash"):
            return {"ok": True, "detail": "项目画像已同步", "revision": remote_revision, "vulnerability_cache": vulnerability_cache}
        if remote_revision > int(local.get("revision") or 0) and isinstance(remote_profile, dict) and remote_profile.get("schema_version") == 2:
            ns["save_project_profile"](
                ledger_path, remote_profile,
                producer_version=str(remote_profile.get("producer_version") or "cloud"),
                revision=remote_revision,
            )
            return {"ok": True, "detail": "已接收较新项目画像", "revision": remote_revision, "vulnerability_cache": vulnerability_cache}
        base_revision = remote_revision
    elif remote.status not in {404, 405}:
        return {"ok": False, "error": f"画像拉取失败 code={remote.status} {remote.error}"}
    else:
        base_revision = 0
    # DOC-057 设计项 4：上传紧凑投影而非完整 profile，确保满足
    # PROJECT_PROFILE_MAX_BYTES 上限；profile_hash 保留完整语义。
    payload = ns["wire_project_profile"](local)
    result = client.put_project_profile(
        project_id, payload, base_revision=base_revision,
        operation_id=f"profile-{local.get('profile_hash', 'unknown')}",
    )
    if result.ok:
        data = client._data(result)
        return {"ok": True, "detail": "项目画像已推送", "revision": data.get("profile_revision", base_revision + 1), "vulnerability_cache": vulnerability_cache}
    if result.status != 409:
        return {"ok": False, "error": f"画像推送失败 code={result.status} {result.error}"}
    latest = client.get_project_profile(project_id)
    if not latest.ok:
        return {"ok": False, "error": f"画像冲突后重读失败 code={latest.status} {latest.error}"}
    latest_data = client._data(latest)
    latest_revision = int(latest_data.get("profile_revision") or latest_data.get("revision") or 0)
    retry = client.put_project_profile(
        project_id, payload, base_revision=latest_revision,
        operation_id=f"profile-{local.get('profile_hash', 'unknown')}",
    )
    if not retry.ok:
        return {"ok": False, "error": f"画像冲突重试失败 code={retry.status} {retry.error}"}
    return {"ok": True, "detail": "项目画像冲突后已重试同步", "revision": latest_revision + 1, "vulnerability_cache": vulnerability_cache}


def _warm_vulnerability_cache(client, profile: dict[str, Any]) -> dict[str, Any]:
    """Best-effort advisory prefetch; never changes the profile or sync result."""
    query = getattr(client, "query_vulnerabilities", None)
    if not callable(query):
        return {"status": "unsupported"}
    components = profile.get("components") if isinstance(profile, dict) else None
    if not isinstance(components, list):
        return {"status": "empty", "advisories": 0}
    try:
        result = query(components[:100])
    except (OSError, ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "error": str(exc)[:200]}
    if result.status in {404, 405}:
        return {"status": "unsupported"}
    if not result.ok:
        return {"status": "unavailable", "code": result.status}
    data = client._data(result) if hasattr(client, "_data") else {}
    rows = data.get("results") if isinstance(data, dict) else []
    advisories = sum(
        len(row.get("advisories") or [])
        for row in rows if isinstance(row, dict)
    )
    return {"status": "ready", "advisories": advisories, "results": len(rows)}

def _sync_active(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        previous = os.environ.get("BUGX_CLOUD_SYNC_ACTIVE")
        os.environ["BUGX_CLOUD_SYNC_ACTIVE"] = "1"
        try:
            return func(*args, **kwargs)
        finally:
            if previous is None:
                os.environ.pop("BUGX_CLOUD_SYNC_ACTIVE", None)
            else:
                os.environ["BUGX_CLOUD_SYNC_ACTIVE"] = previous

    return wrapper


@_sync_active
def sync_layers(client, root: Path, project_slug: str | None = None,
                scopes: set[str] | None = None,
                detail_mode: str | None = None,
                allow_identity_create: bool = False,
                sensitive_policy: str | None = None) -> dict:
    """push 全局+项目；pull 两层走 inbox 校验/导入/cursor 提交。返回汇总。

    DOC-211 配置注入漏斗：detail_mode 与 sensitive_policy 未显式给出时读
    用户级 cloud-config.json（CLI 显式 > 配置 > 默认 full/allow）。程序化
    调用方（auto_sync/startup_gates/onboard_wizard/启动重放）不传显式值，
    统一经此处或 _push_layer 缺省链吃配置。
    """
    # F1 硬闸门：用户显式撤销共享时零 egress、零状态变更，直接返回结构化拒绝。
    from core.cloud_sync.consent import revoked_summary, sharing_revoked
    if sharing_revoked():
        return revoked_summary(scopes)
    from core.cloud_sync.onboard_wizard import (
        ask_project_identity_decision,
        ensure_project_identity,
        warn_uncommitted_identity,
    )
    from core.cloud_sync.sync_pull import pull_layer
    from core.cloud_sync.sync_push import push_layer

    ns = get_imports()
    scopes = set(scopes or {"global", "project"})
    if not scopes or not scopes.issubset({"global", "project"}):
        raise ValueError("scopes 只能包含 global/project")
    try:
        from core.cloud_sync.cloud_client import load_cloud_config as _load_cloud_config
        sync_cfg = _load_cloud_config()
    except Exception:  # noqa: BLE001 - 配置缺失/非法不改变同步入口行为（非法值由闸报错）
        sync_cfg = {}
    selected_policy = sensitive_policy or sync_cfg.get("sensitive_policy") or "allow"
    selected_detail_mode = ns["resolve_detail_mode"](
        detail_mode or sync_cfg.get("sync_detail_mode"))
    out: dict = {}
    deleted_layers: set[str] = set()
    account_id = get_account_id(client)
    pid = ""
    if "project" in scopes:
        # Validate the binding before any project network mutation.
        # 开箱即用与确定性自愈：身份文件缺失时直接基于仓库特征确定性生成并落盘，
        # 严禁中断同步流程去弹窗询问用户（DOC-031 假性门禁已废弃，由确定性 key 幂等兜底）。
        identity = ensure_project_identity(root, project_slug, create_if_missing=True)
        binding = identity.get("project_id", "")
        bound_account = identity.get("account_id", "")
        if bound_account and bound_account != account_id:
            raise CloudOnboardError("本地项目绑定账号与当前云端账号不一致")
        proj = client.register_project(
            # name 必须跟随稳定的 slug（来自 committed identity），不能用本地
            # 目录名：同一项目在不同设备 clone 后目录名可能不同，用 Path(root).name
            # 会让最后一次 init 的设备覆盖云端项目名（实测 dev2 覆盖了 autoinit）。
            slug=identity["slug"], name=identity["slug"],
            project_key=identity["project_key"],
        )
        if not proj.ok:
            out["project"] = f"登记失败 code={proj.status} {proj.error}"
            out["ok"] = False
            return out
        pid = client.project_id(proj)
        if not pid:
            out["ok"] = False
            out["error"] = "项目登记响应缺少 project_id"
            return out
        remote_key = client.project_key(proj)
        if remote_key != identity["project_key"]:
            out["ok"] = False
            out["error"] = "项目登记响应 project_key 不一致，拒绝跨项目同步"
            return out
        if binding and binding != pid:
            out["ok"] = False
            out["error"] = "本地项目绑定与服务端 project_id 不一致，拒绝跨项目同步"
            return out
        _save_project_binding(root, pid, identity["slug"], identity["project_key"], account_id)
        project_data = client._data(proj) if hasattr(client, "_data") else {}
        if isinstance(project_data, dict) and project_data.get("lifecycle_status") == "deleted":
            # Registration is deliberately allowed to return a terminal
            # project identity so the client can still pull/ACK its retained
            # tombstones.  Never call profile or push for that namespace.
            deleted_layers.add("project")
        # 同步（pull/push）开始前提醒一次：身份文件在 git 仓库里但未提交，
        # 多设备会因身份缺失注册出重复项目。检测失败/非 git 仓库时静默跳过。
        warn_uncommitted_identity(root)
        if "project" not in deleted_layers:
            profile_result = _sync_project_profile(client, ns, root, pid)
            out["profile"] = profile_result.get("detail") or profile_result.get("error", "")
            if not profile_result.get("ok"):
                out["ok"] = False
                out["error"] = profile_result.get("error", "project profile sync failed")
                return out
    # 每个 scope 固定 pull → push → pull：先取得远端基线，避免多设备并发
    # 时以过期 base_version 直接写入；push 后再 pull 收敛服务端产生的版本。
    for layer, layer_project_id in (("global", ""), ("project", pid)):
        if layer not in scopes:
            continue
        baseline = pull_layer(client, ns, root, layer, layer_project_id, account_id)
        out[f"{layer}_baseline_pull"] = baseline.get("detail") or baseline.get("error", "")
        if not baseline.get("ok"):
            out["ok"] = False
            out["error"] = baseline.get("error", f"{layer} baseline pull failed")
            return out
        if baseline.get("scope_status") == "deleted":
            deleted_layers.add(layer)
    for layer, layer_project_id in (("global", ""), ("project", pid)):
        if layer not in scopes:
            continue
        if layer in deleted_layers:
            out[f"{layer}_push"] = f"{layer} scope 已删除，跳过 push"
            continue
        pushed = push_layer(client, ns, root, layer, layer_project_id, account_id,
                             detail_mode=selected_detail_mode,
                             sensitive_policy=selected_policy)
        out[f"{layer}_push"] = pushed.get("detail") or pushed.get("error", "")
        if not pushed.get("ok"):
            out["ok"] = False
            out["error"] = pushed.get("error", f"{layer} push failed")
            return out
        if pushed.get("scope_status") == "deleted":
            deleted_layers.add(layer)
    for layer, layer_project_id in (("global", ""), ("project", pid)):
        if layer not in scopes:
            continue
        pulled = pull_layer(
            client, ns, root, layer, layer_project_id, account_id,
            resolve_converged_conflicts=True,
        )
        out[f"{layer}_pull"] = pulled.get("detail") or pulled.get("error", "")
        if not pulled.get("ok"):
            out["ok"] = False
            out["error"] = pulled.get("error", f"{layer} pull failed")
            return out
    for layer, layer_project_id in (("global", ""), ("project", pid)):
        if layer not in scopes:
            continue
        try:
            store = ns["SyncStore"](resolve_store_path(ns, root, layer))
            try:
                store.prune_converged_conflicts(account_id, layer, layer_project_id, resolver_identity="sync-layers-prune")
            finally:
                store.close()
        except Exception:
            pass
    # DOC-211 G3：非默认策略/粒度必须在同步结果里可见（回滚混版时
    # summary→full 的静默反转由此暴露）；默认值不添键，既有断言零扰动。
    if selected_policy != "allow":
        out["sensitive_policy"] = (
            f"{selected_policy}（审计: ~/.bugx/logs/sync-policy-audit.jsonl）")
    if selected_detail_mode != "full":
        out["detail_mode"] = selected_detail_mode
    out["ok"] = True
    return out

save_project_binding = _save_project_binding
