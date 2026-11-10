from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .. import __version__
from ..common import *
from .agent import (
    _check_host_mcp_config,
    _check_opencode_config,
    _detected_host_name,
    _ensure_opencode_config,
)
from .knowledge import _cloud_login_and_register, _cloud_sync_command

MAX_SYNC_BUNDLE_BYTES = 16 * 1024 * 1024


_TLS_ERROR_MARKERS = (
    "CERTIFICATE_VERIFY_FAILED",
    "certificate verify failed",
    "self-signed certificate",
    "certificate has expired",
    "SSLCertVerificationError",
    "hostname mismatch",
)


def _is_tls_failure(error: object) -> bool:
    """区分 TLS 信任失败与普通网络不可达。

    两者在 ``verify_auth()`` 中都表现为“联网核验失败”，但处置方式完全不同：
    前者需要信任证书或配置 ``SSL_CERT_FILE``，后者需要排查网络与地址。
    混为一谈会把证书问题误导成“API key 失效”。
    """
    text = str(error or "")
    return any(marker in text for marker in _TLS_ERROR_MARKERS)


def _is_local_network_privacy_failure(error: object, base_url: str = "") -> bool:
    """识别 macOS 私网访问被本地网络隐私权限拦截的失败。"""
    data = error if isinstance(error, dict) else getattr(error, "data", None)
    if isinstance(data, dict):
        code = data.get("code")
        nested = data.get("data")
        if isinstance(nested, dict):
            code = nested.get("code", code)
        if str(code or "").upper() == "LOCAL_NETWORK_BLOCKED":
            return True

    if not base_url:
        return False
    try:
        from core.cloud_sync.cloud_client import diagnose_network_error
    except (ImportError, AttributeError):
        return False
    raw_error = error.get("error") if isinstance(error, dict) else getattr(error, "error", error)
    if not isinstance(raw_error, BaseException):
        raw_error = Exception(str(raw_error or error))
    return bool(diagnose_network_error(raw_error, str(base_url)).get(
        "is_local_network_blocked", False
    ))


def _telemetry_config_path() -> Path:
    default = Path(os.environ.get("BUGX_HOME", str(Path.home() / ".bugx"))) / "cloud-config.json"
    return Path(os.environ.get("BUGX_CLOUD_CONFIG", str(default))).expanduser()


def _save_data_sharing(value: bool) -> None:
    path = _telemetry_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    telemetry = dict(current.get("telemetry") or {})
    telemetry["data_sharing"] = bool(value)
    current["telemetry"] = telemetry
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def confirm_data_sharing(args: argparse.Namespace) -> bool:
    """数据共享同意；**已选择云同步即视为同意**，不再重复询问。

    为什么这样改（build 297）：``--cloud-sync sync`` 与「是否允许共享」在用户
    视角是**同一个决策**——不许上传的话选 sync 毫无意义。此前 ``init`` 末尾会
    再弹一次「允许 BugX 上传脱敏后的攻防数据用于改进平台？」，等于同一个决定
    问两遍，且两套状态各存一处（``cloud-onboard.json`` vs ``cloud-config.json``），
    很容易出现「选了 sync 却因共享未表态而同步不了」。

    语义收敛：
      * ``--cloud-sync sync``  ⇒ 共享同意落盘为 true（选择即同意）；
      * ``--cloud-sync local`` ⇒ **不写**共享状态（只表示本项目不同步，
        不应替用户做「全局撤销」这种跨项目决定）；
      * 未指定 cloud-sync   ⇒ 保持原有询问/默认逻辑。
    """
    if str(getattr(args, "cloud_sync", "") or "").strip().lower() == "sync":
        _save_data_sharing(True)
        return True
    if getattr(args, "privacy", "standard") == "strict":
        _save_data_sharing(bool(getattr(args, "accept_data_sharing", False)))
        return bool(getattr(args, "accept_data_sharing", False))
    if getattr(args, "accept_data_sharing", False) or os.environ.get("BUGX_ACCEPT_DATA_SHARING") == "1":
        _save_data_sharing(True)
        return True
    path = _telemetry_config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        stored = (config.get("telemetry") or {}).get("data_sharing")
    except (OSError, json.JSONDecodeError):
        stored = None
    if isinstance(stored, bool):
        return stored
    if (
        os.environ.get("CI", "").lower() == "true"
        or os.environ.get("BUGX_NON_INTERACTIVE") == "1"
        or os.environ.get("BUGX_HEADLESS") == "1"
        or "unittest" in sys.modules
        or "pytest" in sys.modules
        or not sys.stdin.isatty()
    ):
        # **不得替用户撤销同意**（build 293 实测缺陷）：此前这里
        # ``_save_data_sharing(False)`` 把「本次无法交互确认」永久写成
        # ``data_sharing=false`` ⇒ 一次非交互运行就让该用户此后所有云同步被
        # ``data_sharing_revoked`` 拒绝，而用户从未做过任何撤销动作。
        # 契约是「默认允许、仅显式 false 才拒绝」（``require_sharing`` 只看
        # ``sharing_revoked()``），所以非交互时**不落盘**、按默认允许处理。
        return True
    try:
        answer = input("允许 BugX 上传脱敏后的攻防数据用于改进平台？[y/N] ").strip().lower()
    except (EOFError, OSError):
        answer = "n"
    allowed = answer in {"y", "yes"}
    _save_data_sharing(allowed)
    return allowed


def command_telemetry(args: argparse.Namespace) -> int:
    """Inspect and export the independent flywheel outbox/forensic ledger."""
    try:
        _ensure_core_importable()
        from core.cloud_sync.flywheel_client import FlywheelUploader
        uploader = FlywheelUploader(data_sharing=True)
        action = getattr(args, "telemetry_command", "inspect")
        if action == "inspect":
            result = uploader.inspect(latest=bool(getattr(args, "latest", False)))
            if getattr(args, "json", False):
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                for entry in result:
                    print(json.dumps(entry, ensure_ascii=False))
            return 0
        if action in {"flush", "upload"}:
            batch_size = int(getattr(args, "batch_size", 20) or 20)
            flushed = uploader.flush(batch_size=batch_size)
            sent = flushed.get("sent", flushed) if isinstance(flushed, dict) else flushed
            print(f"[telemetry] 已推送并清空本地 Outbox：{sent} 条事件")
            return 0
        output = uploader.export_bundle(
            getattr(args, "out"),
            passphrase=getattr(args, "passphrase", None) or os.environ.get("BUGX_TELEMETRY_PASSPHRASE"),
        )
        print(f"[telemetry] 已导出加密穿梭包: {output}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[telemetry] 操作失败: {exc}")
        return 1


def command_telemetry_config(args: argparse.Namespace) -> int:
    config_key = getattr(args, "config_key", "")
    config_value = getattr(args, "config_value", "")
    if config_key == "telemetry.data_sharing":
        if config_value not in ("true", "false"):
            print("[config] telemetry.data_sharing 只接受 true/false")
            return 2
        enabled = config_value == "true"
        try:
            _save_data_sharing(enabled)
            if not enabled:
                _ensure_core_importable()
                from core.cloud_sync.flywheel_client import FlywheelUploader
                cleared = FlywheelUploader(data_sharing=True).clear_outbox()
                print(f"[config] telemetry.data_sharing=false；已清空 {cleared} 条飞轮记录")
                print("[config] 云端同步已停用：撤销态下 sync_layers/auto_sync 拒绝所有 push/pull")
            else:
                print("[config] telemetry.data_sharing=true")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[config] 写入失败: {exc}")
            return 1
    if config_key.startswith("cloud."):
        # DOC-211 同步内容策略键；清飞轮等副作用严格限定在 data_sharing。
        field = config_key[len("cloud."):]
        try:
            from core.cloud_sync.cloud_client import update_cloud_config
            written = update_cloud_config({field: config_value})
        except ValueError as exc:
            print(f"[config] 写入失败: {exc}")
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"[config] 写入失败: {exc}")
            return 1
        print(f"[config] {config_key}={written.get(field)} 已写入 ~/.bugx/cloud-config.json")
        return 0
    return 2


def _build_local_sync_bundle(client: Any, root: Path, scope: str,
                             detail_mode: str) -> dict[str, Any]:
    from core.cloud_sync.cloud_onboard import get_account_id, get_imports, layer_ledger, resolve_store_path
    from core.cloud_sync.cloud_schema import build_push_envelope
    from core.cloud_sync.sync_push import _private_v2_records

    ns = get_imports()
    project_id = ""
    if scope == "project":
        from core.cloud_sync.cloud_onboard import load_project_binding
        project_id = load_project_binding(root)
        if not project_id:
            raise ValueError("项目 scope 缺少 .bugx/cloud-project.json 绑定")
    account_id = get_account_id(client)
    store = ns["SyncStore"](resolve_store_path(ns, root, scope))
    try:
        state = store.get_scope_state(account_id, scope, project_id) or {}
        prepared = _private_v2_records(
            ns, layer_ledger(ns, root, scope), detail_mode=detail_mode,
            sync_states=store.get_record_states(account_id, scope, project_id),
            layer=scope,
        )
    finally:
        store.close()
    pid = int(project_id) if project_id else None
    return build_push_envelope(
        scope, pid, prepared["records"],
        snapshot_id=f"export-{uuid.uuid4().hex}",
        operation_id=f"export-op-{uuid.uuid4().hex}",
        base_version=int(state.get("remote_version") or 0),
        device_id=ns["get_device_id"](),
    )


def _read_sync_bundle(path: Path) -> dict[str, Any]:
    from core.secure_atomic import reject_symlink_components
    from core.cloud_sync.cloud_schema import validate_push_envelope

    reject_symlink_components(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("bundle 必须是普通文件，不能是符号链接")
    if path.stat().st_size > MAX_SYNC_BUNDLE_BYTES:
        raise ValueError(f"bundle 超过 {MAX_SYNC_BUNDLE_BYTES // (1024 * 1024)} MiB 限制")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("bundle 不是合法 JSON") from exc
    return validate_push_envelope(bundle)


def command_cloud(args: argparse.Namespace) -> int:
    """云端同步：登录 / 同步 / 项目 / 设备 / onboarding。"""
    try:
        _ensure_core_importable()
        from core.cloud_sync.cloud_client import CloudClient, get_device_id, lesson_to_wire, incident_to_wire
        from core.ledger.lesson_ledger import read as read_ledger, list_incidents
        from core.bugx_paths import global_knowledge_root, project_knowledge_root
    except Exception as exc:  # noqa: BLE001
        print(f"[cloud] 依赖加载失败: {exc}")
        return 2

    root = args.root.resolve()
    act = args.cloud_command

    if act == "config":
        try:
            from core.cloud_sync.cloud_client import (
                clear_cloud_base_url, cloud_base_url_status,
                load_cloud_config, set_cloud_base_url,
            )
            disable_vc = getattr(args, "disable_version_check", None)
            if getattr(args, "clear_base_url", False):
                clear_cloud_base_url()
                result = cloud_base_url_status()
                message = "[cloud] 已清除持久化 Base URL"
            elif getattr(args, "refresh_route", False):
                from core.cloud_sync.cloud_client import clear_route_cache
                clear_route_cache()
                result = cloud_base_url_status()
                message = f"[cloud] 已刷新路由测速缓存，当前端点: {result['base_url']}（来源: {result['source']}）"
            elif getattr(args, "base_url", None):
                normalized = set_cloud_base_url(args.base_url)
                result = cloud_base_url_status()
                message = f"[cloud] 已设置云端 Base URL: {normalized}"
            elif disable_vc is not None:
                # Read current config, update disable_version_check, persist.
                from core.cloud_sync.cloud_client import _cloud_config_path
                from core.report_engine.report_client import _atomic_write_nofollow
                cfg_path = _cloud_config_path()
                existing = load_cloud_config(cfg_path)
                existing["disable_version_check"] = (disable_vc == "true")
                payload = json.dumps(existing, ensure_ascii=False, indent=2).encode("utf-8")
                _atomic_write_nofollow(cfg_path, payload, parent_mode=0o700)
                result = cloud_base_url_status()
                result["disable_version_check"] = existing.get("disable_version_check", False)
                if disable_vc == "true":
                    os.environ["BUGX_DISABLE_VERSION_CHECK"] = "1"
                    message = "[cloud] 已禁用启动时版本检查"
                else:
                    os.environ.pop("BUGX_DISABLE_VERSION_CHECK", None)
                    message = "[cloud] 已启用启动时版本检查"
            else:
                result = cloud_base_url_status()
                # Also show disable_version_check status.
                cfg = load_cloud_config()
                result["disable_version_check"] = cfg.get("disable_version_check", False)
                vc_status = "禁用" if result["disable_version_check"] else "启用"
                message = (
                    f"[cloud] 当前云端 Base URL: {result['base_url']}"
                    f"（来源: {result['source']}）；版本检查: {vc_status}"
                )
        except (OSError, ValueError, ImportError) as exc:
            # P3d：_cloud_config_path/_atomic_write_nofollow 的二次 import
            # 也会抛 ImportError，不能让它逃逸成未受控堆栈。
            print(f"[cloud] 配置失败: {exc}")
            return 2
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(message)
        return 0

    try:
        client = CloudClient()
    except ValueError as exc:
        # DOC-211：坏配置不得裸奔 traceback；cloud config 子命令不经此路径，
        # 修复入口始终可用。
        print(f"[cloud] cloud-config.json 配置非法: {exc}")
        print("[cloud] 修复：bugx config set cloud.<键> <合法值>，"
              "或删除 ~/.bugx/cloud-config.json 后重试")
        return 1

    if act == "export":
        try:
            bundle = _build_local_sync_bundle(
                client, root, args.bundle_scope,
                getattr(args, "detail_mode", None) or "full",
            )
            output = Path(args.output).expanduser().absolute()
            from core.report_engine.report_client import _atomic_write_nofollow
            _atomic_write_nofollow(
                output,
                json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8"),
                parent_mode=0o700,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[cloud] JSON 导出失败: {exc}")
            return 1
        print(f"[cloud] 已导出 {bundle['scope']} bundle: {output}")
        return 0

    if act == "upload":
        try:
            bundle = _read_sync_bundle(Path(args.input).expanduser().absolute())
            # DOC-211：upload 与 sync 同吃一档敏感策略（配置缺省 allow 透传）。
            try:
                from core.cloud_sync.cloud_client import load_cloud_config
                upload_policy = load_cloud_config().get("sensitive_policy") or "allow"
            except Exception:  # noqa: BLE001
                upload_policy = "allow"
            result = client.import_knowledge_bundle(
                bundle, mode=args.mode, sensitive_policy=upload_policy)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[cloud] JSON 上传失败: {exc}")
            return 1
        if not result.ok:
            err = client.sync_error(result)
            print(f"[cloud] JSON 上传被拒绝: code={err.code} {err.message or result.error}")
            return 1
        data = client._data(result)
        if args.mode in ("local_authoritative", "merge"):
            try:
                from core.cloud_sync.cloud_onboard import get_account_id, get_imports, resolve_store_path
                from core.cloud_sync.sync_pull import pull_layer
                ns = get_imports()
                account_id = get_account_id(client)
                if account_id:
                    project_id = str(bundle.get("project_id") or "") if bundle["scope"] == "project" else ""
                    version = data.get("snapshot_version")
                    snapshot_id = data.get("snapshot_id")
                    if not isinstance(version, int) or isinstance(version, bool) or version < 0 \
                            or not isinstance(snapshot_id, str) or not snapshot_id:
                        raise ValueError("服务端导入响应缺少可信 snapshot_id/snapshot_version")
                    store = ns["SyncStore"](resolve_store_path(ns, root, bundle["scope"]))
                    try:
                        store.adopt_import_snapshot(
                            account_id, bundle["scope"], project_id,
                            snapshot_id=snapshot_id, remote_version=version,
                        )
                    finally:
                        store.close()
                    pull_result = pull_layer(
                        client, ns, root, bundle["scope"], project_id, account_id,
                        force_baseline=True,
                    )
                    if not isinstance(pull_result, dict) or not pull_result.get("ok"):
                        raise RuntimeError(
                            (pull_result or {}).get("error", "baseline pull failed")
                            if isinstance(pull_result, dict) else "baseline pull failed"
                        )
            except Exception as exc:  # noqa: BLE001
                print(f"[cloud] 本地导入状态未能收敛: {exc}")
                return 1
        if getattr(args, "json", False):
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"[cloud] JSON 上传完成: scope={bundle['scope']} mode={args.mode}")
        return 0

    if act == "log-report":
        """内测期诊断日志上报：打包 bugx.jsonl 尾部 -> POST /agent/log-reports。

        默认不上报：仅在 AI/用户显式调用本命令时执行；服务端还有登录认证
        与每账号每日限额（后台可配置）。
        """
        tail_kb = max(1, min(1024, int(getattr(args, "tail_kb", 256) or 256)))
        # 读取方必须跟随写入方的落点：未初始化项目会回退到用户级日志，
        # 直接拼 <root>/.bugx/logs 会永远报"未找到诊断日志"。
        log_file = diagnostic_log_path(root)
        content = ""
        lines = 0
        if log_file.is_file():
            size = log_file.stat().st_size
            with log_file.open("rb") as fh:
                if size > tail_kb * 1024:
                    fh.seek(size - tail_kb * 1024)
                    fh.readline()  # 丢弃不完整的半行
                content = fh.read().decode("utf-8", errors="replace")
            lines = content.count("\n")
        else:
            print("[cloud] 未找到诊断日志（本次会话尚未产生日志），无需上报")
            return 0
        if not content.strip():
            print("[cloud] 诊断日志为空，无需上报")
            return 0

        from core.cloud_sync.cloud_client import current_version_str
        kind = str(getattr(args, "kind", None) or "cli-diagnostics").strip() or "cli-diagnostics"
        body = {
            "device_id": get_device_id(),
            "client_version": current_version_str(),
            "kind": kind,
            "payload": content,
        }
        if getattr(args, "json", False):
            print(json.dumps({"bytes": len(content.encode("utf-8")), "lines": lines},
                             ensure_ascii=False))
        try:
            result = client.post_log_report(body)
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] 日志上报失败: {exc}")
            return 1
        envelope = getattr(result, "data", None)
        payload_data = envelope.get("data") if isinstance(envelope, dict) else None
        remaining = payload_data.get("remaining") if isinstance(payload_data, dict) else None
        if not getattr(result, "ok", False):
            err_text = getattr(result, "error", None) or str(envelope)
            print(f"[cloud] 日志上报被拒绝: {err_text}")
            if isinstance(payload_data, dict) and payload_data.get("limit") is not None:
                print(f"[cloud] 今日配额: 已用 {payload_data.get('used')}/"
                      f"{payload_data.get('limit')}，请明日再试")
            return 1
        tail_note = f"，剩余 {remaining} 次" if remaining is not None else ""
        print(f"[cloud] 日志上报成功（{lines} 行{tail_note}）")
        return 0

    if act == "feedback":
        """错题卡质量闭环：本地 feedback 汇总 -> POST /agent/lessons/hit-feedback。"""
        try:
            from core.ledger.lesson_ledger import feedback_outcomes
            from core.bugx_paths import project_knowledge_root
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] 依赖加载失败: {exc}")
            return 2
        try:
            items = feedback_outcomes(
                project_knowledge_root(root) / "lesson-ledger.sqlite3",
                lifecycle_id=getattr(args, "lifecycle", None) or None,
                limit=max(1, min(100, int(getattr(args, "limit", 100) or 100))),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] 本地反馈读取失败: {exc}")
            return 2
        if getattr(args, "json", False) or getattr(args, "dry_run", False):
            preview = json.dumps({"items": items, "count": len(items)},
                                 ensure_ascii=False, indent=2)
            print(preview)
        if getattr(args, "dry_run", False):
            return 0
        if not items:
            print("[cloud] 没有可上报的错题卡反馈")
            return 0
        try:
            result = client.post_lesson_hit_feedback(
                items, run_id=getattr(args, "run_id", "") or "",
                project_name=getattr(args, "project_name", "") or "",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] hit-feedback 上报失败: {exc}")
            return 1
        envelope = getattr(result, "data", None)
        payload = envelope.get("data") if isinstance(envelope, dict) else None
        updated = payload.get("updated") if isinstance(payload, dict) else "?"
        missing = payload.get("missing") if isinstance(payload, dict) else "?"
        if not getattr(result, "ok", False):
            print(f"[cloud] hit-feedback 上报被拒绝: {getattr(result, 'error', None) or envelope}")
            return 1
        print(f"[cloud] hit-feedback 已上报: updated={updated} missing={missing}")
        return 0

    if act == "status":
        """只读展示本地同步状态；不登录、不联网、不创建 sync store。"""
        try:
            from core.runtime.sync_store import SyncStore, SyncStoreError, global_store_path, project_store_path
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] 同步状态依赖加载失败: {exc}")
            return 2

        scope = getattr(args, "status_scope", "all")
        scopes = ("global", "project") if scope == "all" else (scope,)
        credential = getattr(client, "cred", None)
        user = getattr(credential, "user", None)
        account_id = ""
        if isinstance(user, dict):
            for key in ("id", "user_id", "account_id", "sub"):
                if user.get(key) not in (None, ""):
                    account_id = str(user[key])
                    break
        auth_state = "missing"
        try:
            auth_state = credential.auth_state() if credential is not None else "missing"
        except Exception:  # noqa: BLE001
            auth_state = "unknown"

        project_id = ""
        binding_path = root / ".bugx" / "cloud-project.json"
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            if isinstance(binding, dict):
                project_id = str(binding.get("project_id") or "")
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        onboarding = "pending"
        onboarding_path = root / ".bugx" / "cloud-onboard.json"
        try:
            onboarding_data = json.loads(onboarding_path.read_text(encoding="utf-8"))
            if isinstance(onboarding_data, dict):
                onboarding = str(onboarding_data.get("state") or "pending")
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

        stores: dict[str, dict] = {}
        errors: list[str] = []
        for layer in scopes:
            store_path = global_store_path() if layer == "global" else project_store_path(root)
            layer_project_id = "" if layer == "global" else project_id
            try:
                stores[layer] = SyncStore.inspect_scope(
                    store_path, account_id, layer, layer_project_id,
                )
            except SyncStoreError as exc:
                stores[layer] = {
                    "path": str(store_path), "scope": layer,
                    "project_id": layer_project_id, "state": "error", "error": str(exc),
                }
                errors.append(f"{layer}: {exc}")

        payload = {
            "authenticated": bool(credential and credential.is_authenticated()),
            "auth_state": auth_state,
            "auth_source": (
                "api_key" if getattr(credential, "api_key", None) else
                "local_cache" if getattr(credential, "token", None) else "none"
            ),
            "auth_verified": None,
            "auth_verification_status": "not_checked",
            "has_account_id": bool(account_id),
            "onboarding": onboarding,
            "project_id": project_id or None,
            "scopes": stores,
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            auth_note = ""
            if auth_state == "expired" and getattr(credential, "auth_state_override", None) == "expired":
                auth_note = "（已记录服务端拒绝；需重新登录或使用 `bugx auth status --verify`）"
            elif payload["auth_verification_status"] == "not_checked" \
                    and payload["auth_source"] in {"local_cache", "api_key"}:
                auth_note = "（本地凭据，未联网核验；需 `bugx auth status --verify`）"
            print(f"[cloud] 登录态: {auth_state}{auth_note}；onboarding: {onboarding}")
            print(f"[cloud] 项目绑定: {project_id or '未绑定'}")
            for layer, item in stores.items():
                state = item.get("scope_state") or {}
                outbox = item.get("outbox") or {}
                conflicts = item.get("conflicts") or {}
                print(
                    f"  {layer}: {item.get('state')}，"
                    f"cursor={state.get('committed_cursor') or '未提交'}，"
                    f"error={state.get('last_error_code') or '无'}，"
                    f"pending={outbox.get('pending', 0)}，"
                    f"inflight={outbox.get('inflight', 0)}，"
                    f"dead_letter={outbox.get('dead_letter', 0)}，"
                    f"conflicts_open={conflicts.get('open', 0)}"
                )
            if errors:
                print("[cloud] 状态读取失败: " + "; ".join(errors))
        return 1 if errors else 0

    if act == "conflicts":
        """冲突队列治理：list/prune 与 resolve --side local 离线执行；
        resolve --side remote 需要联网拉取远端权威内容后解决。"""
        try:
            from core.runtime.sync_store import (
                SyncStore, SyncStoreError, global_store_path, project_store_path,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] 冲突队列依赖加载失败: {exc}")
            return 2
        conflict_action = getattr(args, "conflict_action", "list")
        scope = getattr(args, "conflict_scope", "all")
        layers = (("global", global_store_path()), ("project", project_store_path(root)))
        if scope != "all":
            layers = tuple(item for item in layers if item[0] == scope)

        if conflict_action == "list":
            conflicts: list[dict] = []
            errors = []
            for layer, store_path in layers:
                if not store_path.exists():
                    continue
                try:
                    store = SyncStore(store_path)
                    try:
                        conflicts.extend(store.list_conflicts())
                    finally:
                        store.close()
                except SyncStoreError as exc:
                    errors.append(f"{layer}: {exc}")
            if getattr(args, "json", False):
                print(json.dumps({"conflicts": conflicts, "count": len(conflicts)},
                                 ensure_ascii=False, indent=2))
            elif not conflicts:
                print("[cloud] 没有待治理的同步冲突")
            else:
                print(f"[cloud] 同步冲突队列: {len(conflicts)} 条待治理")
                for item in conflicts:
                    print(
                        f"  [{item.get('scope')}] {item.get('conflict_id')} "
                        f"{item.get('record_type')}/{item.get('record_id')} "
                        f"local_v={item.get('local_version')} "
                        f"remote_v={item.get('remote_version')} "
                        f"reason={item.get('reason') or 'unknown'} "
                        f"created={item.get('created_at')}"
                    )
            if errors:
                print("[cloud] 冲突队列读取失败: " + "; ".join(errors))
            return 1 if errors else 0

        if conflict_action == "prune":
            account_id = ""
            credential = getattr(client, "cred", None)
            user = getattr(credential, "user", None)
            if isinstance(user, dict):
                for key in ("id", "user_id", "account_id", "sub"):
                    if user.get(key) not in (None, ""):
                        account_id = str(user[key])
                        break
            project_id = ""
            binding_path = root / ".bugx" / "cloud-project.json"
            try:
                binding = json.loads(binding_path.read_text(encoding="utf-8"))
                if isinstance(binding, dict):
                    project_id = str(binding.get("project_id") or "")
            except Exception:
                pass
            total_pruned = 0
            errors = []
            for layer, store_path in layers:
                if not store_path.exists():
                    continue
                try:
                    store = SyncStore(store_path)
                    try:
                        layer_pid = project_id if layer == "project" else ""
                        total_pruned += store.prune_converged_conflicts(
                            account_id, layer, layer_pid, resolver_identity="cli-conflicts-prune"
                        )
                    finally:
                        store.close()
                except SyncStoreError as exc:
                    errors.append(f"{layer}: {exc}")
            if errors:
                print("[cloud] 冲突修剪失败: " + "; ".join(errors))
                return 1
            print(f"[cloud] 冲突队列修剪完成：已自动关闭 {total_pruned} 条已收敛/无分歧冲突")
            return 0

        # resolve（G3' 实效化）：
        #   local = 保留本地——清除该记录的远端版本元数据（本地权威但**不发明
        #           版本**），下一轮正常 push 以变更内容走服务器分配新版本；
        #   remote = 接受远端版本——先联网拉取权威内容经既有导入事务写账本并
        #           upsert_record_states 对齐 remote_version/remote_hash，再落
        #           解决记账；拉取失败则不解决（fail-closed，冲突保持 open）。
        # resolve 是显式人工命令，没有任何自动触发路径。
        side = getattr(args, "conflict_side", None) or ""
        if side not in {"local", "remote"}:
            print("[cloud] 用法: bugx cloud conflicts resolve <conflict_id>|--all "
                  "--side local|remote [--reason-filter <reason>]")
            return 2
        resolution = "resolved_local" if side == "local" else "resolved_remote"

        def _pull_authoritative(layer: str, project_id: str, account_id: str) -> dict:
            # --side remote 专用：一次翻页 pull（页数受 client.max_pull_pages
            # 约束）拉取权威内容，经 _pull_layer 既有导入事务写账本，并由
            # inbox 提交回写 record_state。
            from core.cloud_sync.cloud_onboard import get_account_id, get_imports
            from core.cloud_sync.sync_pull import pull_layer
            ns = get_imports()
            return pull_layer(client, ns, root, layer, project_id,
                               account_id or get_account_id(client),
                               resolve_converged_conflicts=True)

        def _pull_detail(pull: Any) -> str:
            if isinstance(pull, dict):
                return str(pull.get("error") or "unknown")
            return "unknown"

        if getattr(args, "conflict_all", False):
            # 批量形态：遍历冲突队列逐条按选边解决；单条失败不中断，
            # 收集后汇总 resolved/failed 计数（R6 曾需手工循环 56 次）。
            if getattr(args, "conflict_id", None):
                print("[cloud] --all 与 conflict_id 互斥，不能同时使用")
                return 2
            reason_filter = str(getattr(args, "conflict_reason_filter", "") or "")
            # G3' 限速保护：--side remote 批量解决总数上限 200；网络侧每层
            # 至多一次翻页 pull 会话（优于逐条 pull 的请求放大）。
            remote_budget = 200
            resolved_count = 0
            failed_count = 0
            over_limit = 0
            failures: list[str] = []
            pulled_layers: list[str] = []
            errors = []
            for layer, store_path in layers:
                if not store_path.exists():
                    continue
                try:
                    store = SyncStore(store_path)
                except SyncStoreError as exc:
                    errors.append(f"{layer}: {exc}")
                    continue
                try:
                    if side == "remote":
                        pending_rows = [
                            item for item in store.list_conflicts()
                            if not reason_filter
                            or (item.get("reason") or "") == reason_filter
                        ]
                        if not pending_rows:
                            continue
                        if remote_budget <= 0:
                            over_limit += len(pending_rows)
                            continue
                        first = pending_rows[0]
                        try:
                            pull = _pull_authoritative(
                                layer, str(first.get("project_id") or ""),
                                str(first.get("account_id") or ""))
                        except Exception as exc:  # noqa: BLE001
                            failed_count += len(pending_rows)
                            failures.append(
                                f"{layer}: 权威内容拉取失败 "
                                f"{type(exc).__name__}: {exc}")
                            continue
                        if not isinstance(pull, dict) or not pull.get("ok"):
                            failed_count += len(pending_rows)
                            failures.append(
                                f"{layer}: 权威内容拉取失败 {_pull_detail(pull)}")
                            continue
                        pulled_layers.append(layer)
                    for item in store.list_conflicts():
                        if reason_filter and (item.get("reason") or "") != reason_filter:
                            continue
                        if side == "remote":
                            if remote_budget <= 0:
                                over_limit += 1
                                continue
                            remote_budget -= 1
                        try:
                            store.resolve_conflict(
                                str(item["conflict_id"]), resolution,
                                resolver_identity="bugx-cli")
                            resolved_count += 1
                        except SyncStoreError as exc:
                            current = store.get_conflict(str(item["conflict_id"]))
                            if current and current.get("state") != "open":
                                # pull 的冲突感知导入已把该记录幂等收敛
                                resolved_count += 1
                                continue
                            failed_count += 1
                            failures.append(f"{layer}/{item.get('conflict_id')}: {exc}")
                finally:
                    store.close()
            note = ("保留本地，丢弃远端记录（该记录远端版本元数据已清除）"
                    if side == "local" else "接受远端版本（已拉取权威内容入账本）")
            if errors:
                print("[cloud] 冲突队列读取失败: " + "; ".join(errors))
            if getattr(args, "json", False):
                payload = {
                    "all": True, "side": side, "resolution": resolution,
                    "reason_filter": reason_filter or None,
                    "resolved": resolved_count, "failed": failed_count,
                    "failures": failures,
                    "pulled": pulled_layers,
                }
                if over_limit:
                    payload["over_limit"] = over_limit
                print(json.dumps(payload, ensure_ascii=False))
            else:
                filter_note = f"，reason={reason_filter}" if reason_filter else ""
                limit_note = f"，超出上限未处理 {over_limit} 条" if over_limit else ""
                print(f"[cloud] 批量解决完成（side={side}，{note}{filter_note}）: "
                      f"resolved={resolved_count} failed={failed_count}{limit_note}")
                for failure in failures:
                    print(f"  [failed] {failure}")
            return 1 if (errors or failed_count) else 0
        conflict_id = getattr(args, "conflict_id", None) or ""
        if not conflict_id:
            print("[cloud] 用法: bugx cloud conflicts resolve <conflict_id>|--all "
                  "--side local|remote [--reason-filter <reason>]")
            return 2
        resolved_scopes: list[str] = []
        resolve_failures: list[str] = []
        errors = []
        for layer, store_path in layers:
            if not store_path.exists():
                continue
            try:
                store = SyncStore(store_path)
            except SyncStoreError as exc:
                errors.append(f"{layer}: {exc}")
                continue
            try:
                row = store.get_conflict(conflict_id)
                if not row or row.get("state") != "open":
                    # 该层没有这条 open 冲突；conflict_id 是跨层唯一的确定性哈希。
                    continue
                if side == "remote":
                    try:
                        pull = _pull_authoritative(
                            layer, str(row.get("project_id") or ""),
                            str(row.get("account_id") or ""))
                    except Exception as exc:  # noqa: BLE001
                        resolve_failures.append(
                            f"{layer}: 权威内容拉取失败 {type(exc).__name__}: {exc}")
                        continue
                    if not isinstance(pull, dict) or not pull.get("ok"):
                        resolve_failures.append(
                            f"{layer}: 权威内容拉取失败 {_pull_detail(pull)}")
                        continue
                try:
                    store.resolve_conflict(conflict_id, resolution,
                                           resolver_identity="bugx-cli")
                except SyncStoreError:
                    current = store.get_conflict(conflict_id)
                    if not (side == "remote" and current
                            and current.get("state") == "resolved_remote"):
                        raise  # 既未显式解决、pull 也未收敛：按失败处理
                resolved_scopes.append(layer)
            except SyncStoreError as exc:
                resolve_failures.append(f"{layer}: {exc}")
            finally:
                store.close()
        if errors:
            print("[cloud] 冲突队列读取失败: " + "; ".join(errors))
            return 1
        if resolve_failures:
            for failure in resolve_failures:
                print(f"[cloud] {failure}")
            print(f"[cloud] 冲突 {conflict_id} 未能解决：远端权威内容未落地，"
                  "冲突保持 open（fail-closed）")
            return 1
        if not resolved_scopes:
            print(f"[cloud] 冲突 {conflict_id} 不存在或已解决")
            return 1
        note = ("保留本地，丢弃远端记录（该记录远端版本元数据已清除，"
                "下一轮 push 由服务器分配新版本）"
                if side == "local" else
                "接受远端版本（已拉取权威内容入账本并回写记录版本元数据）")
        if getattr(args, "json", False):
            print(json.dumps({
                "conflict_id": conflict_id, "resolution": resolution,
                "side": side, "scopes": resolved_scopes,
            }, ensure_ascii=False))
        else:
            print(f"[cloud] 冲突 {conflict_id} 已解决（side={side}，{note}）"
                  f": scopes={','.join(resolved_scopes)}")
        return 0

    if act == "dead-letter":
        """本地死信队列治理（不联网）：list / purge。

        purge 必须显式 ``--confirm`` 才删除；缺省只 dry-run 打印将删条数。
        删除前先把将删行整体导出到 ``.bugx/cloud-sync/dead-letter-archive-<ts>.json``
        留档（可追溯），归档失败的层不删除。
        """
        try:
            from core.runtime.sync_store import (
                SyncStore, SyncStoreError, global_store_path, project_store_path,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] 死信队列依赖加载失败: {exc}")
            return 2
        dead_action = getattr(args, "dead_letter_action", "list")
        dead_scope = getattr(args, "dead_letter_scope", "all")
        layers = (("global", global_store_path()), ("project", project_store_path(root)))
        if dead_scope != "all":
            layers = tuple(item for item in layers if item[0] == dead_scope)
        try:
            older_than_days = int(getattr(args, "dead_letter_older_than_days", 0) or 0)
        except (TypeError, ValueError):
            print("[cloud] --older-than-days 必须是非负整数")
            return 2
        if older_than_days < 0:
            print("[cloud] --older-than-days 必须是非负整数")
            return 2

        scanned: list[tuple[str, Path, list[dict]]] = []
        errors: list[str] = []
        for layer, store_path in layers:
            if not store_path.exists():
                continue
            try:
                store = SyncStore(store_path)
                try:
                    rows = store.list_dead_letters(older_than_days=older_than_days)
                finally:
                    store.close()
                scanned.append((layer, store_path, rows))
            except SyncStoreError as exc:
                errors.append(f"{layer}: {exc}")
        total = sum(len(rows) for _layer, _path, rows in scanned)

        def dead_letter_view(layer: str, row: dict) -> dict:
            return {
                "layer": layer,
                "scope": row.get("scope"),
                "operation_id": row.get("operation_id"),
                "record_type": row.get("record_type"),
                "record_id": row.get("record_id"),
                "last_error_code": row.get("last_error_code"),
                "created_at": row.get("created_at"),
                "attempts": row.get("attempts"),
            }

        if dead_action == "list":
            if getattr(args, "json", False):
                payload = {
                    "dead_letters": [
                        dead_letter_view(layer, row)
                        for layer, _path, rows in scanned for row in rows
                    ],
                    "count": total,
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif total == 0:
                print("[cloud] 没有滞留的同步死信")
            else:
                print(f"[cloud] 同步死信队列: {total} 条滞留")
                for layer, _path, rows in scanned:
                    for row in rows:
                        print(
                            f"  [{layer}/{row.get('scope')}] {row.get('operation_id')} "
                            f"{row.get('record_type')}/{row.get('record_id')} "
                            f"error={row.get('last_error_code') or 'unknown'} "
                            f"created={row.get('created_at')} "
                            f"attempts={row.get('attempts')}"
                        )
            if errors:
                print("[cloud] 死信队列读取失败: " + "; ".join(errors))
            return 1 if errors else 0

        # purge
        if not getattr(args, "dead_letter_confirm", False):
            # dry-run：只有显式 --confirm 才会真正删除。
            if getattr(args, "json", False):
                print(json.dumps({"dry_run": True, "would_purge": total},
                                 ensure_ascii=False))
            elif total:
                print(f"[cloud] dry-run：将清理 {total} 条死信（先归档后删除）；"
                      "追加 --confirm 执行")
            else:
                print("[cloud] 没有可清理的死信")
            if errors:
                print("[cloud] 死信队列读取失败: " + "; ".join(errors))
            return 1 if errors else 0

        archive_stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        purged_total = 0
        archives: list[str] = []
        for layer, store_path, rows in scanned:
            if not rows:
                continue
            archive_path = store_path.parent / f"dead-letter-archive-{archive_stamp}.json"
            if archive_path.exists():
                # 同秒二次清理：不覆盖前一次留档。
                archive_path = store_path.parent / (
                    f"dead-letter-archive-{archive_stamp}-{uuid.uuid4().hex[:8]}.json")
            payload = {
                "schema_version": 1,
                "archived_at": archive_stamp,
                "layer": layer,
                "source_store": str(store_path),
                "dead_letters": rows,
            }
            try:
                # 原子留档（同 cloud.py 内 _save_data_sharing 的写法）：
                # 归档失败的层绝不进入删除步骤。
                temporary = archive_path.with_name(archive_path.name + ".tmp")
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                os.replace(temporary, archive_path)
                os.chmod(archive_path, 0o600)
            except OSError as exc:
                errors.append(f"{layer}: 死信归档失败，已跳过删除: {exc}")
                continue
            try:
                store = SyncStore(store_path)
                try:
                    purged_total += store.purge_dead_letters(
                        [str(row["operation_id"]) for row in rows])
                finally:
                    store.close()
            except SyncStoreError as exc:
                errors.append(f"{layer}: {exc}")
                continue
            archives.append(str(archive_path))
        if getattr(args, "json", False):
            print(json.dumps({
                "purged": purged_total, "archives": archives, "errors": errors,
            }, ensure_ascii=False))
        else:
            summary = f"[cloud] 死信清理完成：已清理 {purged_total} 条死信"
            if archives:
                summary += "；归档: " + ", ".join(archives)
            print(summary)
        if errors:
            print("[cloud] 死信清理部分失败: " + "; ".join(errors))
            return 1
        return 0

    def pattern_list(ledger_path: Path) -> list:
        try:
            data = read_ledger(ledger_path)
            return list((data.get("lessons") or {}).values())
        except Exception:  # noqa: BLE001
            return []

    def incident_list(ledger_path: Path) -> list:
        try:
            return list_incidents(ledger_path)
        except Exception:  # noqa: BLE001
            return []

    def layer_ledger(layer: str) -> Path:
        if layer == "global":
            return global_knowledge_root() / "lesson-ledger.json"
        return project_knowledge_root(root) / "lesson-ledger.json"

    if act == "auth":
        auth_action = getattr(args, "auth_action", "")
        if auth_action == "login":
            sharing = confirm_data_sharing(args)
            if not sharing:
                print("[cloud] 未同意数据反哺；本地保持离线隐私模式")
            try:
                _cloud_login_and_register(client, get_device_id)
            except RuntimeError as exc:
                print(f"[cloud] 登录失败: {exc}")
                return 1
            print("[cloud] 登录成功，凭据已保存")
            try:
                from core.cloud_sync.cloud_onboard import get_state, set_state
                if get_state(root) in {"dismissed", "local"}:
                    set_state(root, "pending")
                    print("[cloud] 已重新开启本项目 onboarding；下次 Agent 启动会询问同步")
            except Exception as exc:  # noqa: BLE001
                print(f"[cloud] onboarding 状态未更新: {exc}")
            return 0
        if auth_action == "set-key":
            if not args.api_key:
                raise CliError(_i18n_exc("cloud.auth_set_key_api_key_required", "auth set-key 必须提供 --api-key"))
            client.set_api_key(args.api_key)
            print("[cloud] api-key 已保存")
            return 0
        if auth_action == "logout":
            client.logout()
            print("[cloud] 已退出云端登录；设备身份和本地同步状态未删除")
            return 0
        if auth_action == "status":
            credential = client.cred
            user = credential.user if isinstance(credential.user, dict) else {}
            user_id = next(
                (str(user[key]) for key in ("id", "user_id", "account_id", "sub")
                 if user.get(key) not in (None, "")),
                "",
            )
            payload = {
                "authenticated": credential.is_authenticated(),
                "auth_state": credential.auth_state(),
                "auth_source": (
                    "api_key" if credential.api_key else
                    "local_cache" if credential.token else "none"
                ),
                "has_api_key": bool(credential.api_key),
                "has_refresh_token": bool(credential.refresh_token),
                "user_id": user_id or None,
                "auth_verified": None,
                "auth_verification_status": "not_checked",
            }
            if getattr(args, "verify_auth", False):
                verified = client.verify_auth()
                verification_error = getattr(verified, "error", None)
                verification_data = getattr(verified, "data", None)
                if not verification_error and isinstance(verification_data, dict):
                    verification_error = verification_data.get("message") or verification_data.get("error")
                verification_http_status = getattr(verified, "status", None)
                payload["auth_verified"] = bool(verified.ok)
                local_network_blocked = (
                    not verified.ok
                    and _is_local_network_privacy_failure(
                        verified,
                        getattr(client, "base", "") or getattr(client, "base_url", ""),
                    )
                )
                payload["auth_verification_status"] = (
                    "valid" if verified.ok else
                    "expired" if verification_http_status == 401 else
                    "local_network_blocked" if local_network_blocked else
                    "tls_untrusted" if _is_tls_failure(verification_error) else
                    "unavailable"
                )
                payload["auth_verification_error"] = verification_error
                payload["auth_verification_http_status"] = verification_http_status
                payload["auth_state"] = credential.auth_state()
            if getattr(args, "json", False):
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                state_note = ""
                if payload["auth_verification_status"] == "not_checked" \
                        and payload["auth_source"] == "local_cache":
                    state_note = "（本地缓存，未联网验证）"
                elif payload["auth_verification_status"] == "local_network_blocked":
                    reason = payload.get("auth_verification_error") or "局域网目标不可达"
                    state_note = f"（服务端验证: local_network_blocked，原因: {reason}）"
                elif payload["auth_verification_status"] != "not_checked":
                    state_note = f"（服务端验证: {payload['auth_verification_status']}）"
                if payload.get("auth_verification_error") \
                        and payload["auth_verification_status"] != "local_network_blocked":
                    state_note += f"，原因: {payload['auth_verification_error']}"
                print(f"[cloud] 登录态: {payload['auth_state']}{state_note}")
                print(f"[cloud] API Key: {'已配置' if payload['has_api_key'] else '未配置'}")
                print(f"[cloud] Refresh Token: {'已配置' if payload['has_refresh_token'] else '未配置'}")
                print(f"[cloud] 账号: {payload['user_id'] or '未识别'}")
            return 0
        return 2

    if act == "sync":
        # DOC-211：--scope 缺省时读用户级 sync_scope_default（默认 all）；
        # onboard-on（:1216）刻意传显式 "all"，不受配置收窄。
        try:
            from core.cloud_sync.cloud_client import load_cloud_config
            sync_default_scope = load_cloud_config().get("sync_scope_default") or "all"
        except Exception:  # noqa: BLE001
            sync_default_scope = "all"
        if getattr(args, "sync_scope", None) is None and sync_default_scope != "all":
            print(f"[cloud] sync_scope_default={sync_default_scope}（配置生效；显式 --scope 可覆盖）")
        return _cloud_sync_command(args, client, root, get_device_id,
                                   default_scope=sync_default_scope)

    if act == "projects":
        sub = getattr(args, "cloud_project_action", "list")
        if sub == "list":
            r = client.list_projects()
            data = client._data(r)
            print("[cloud] 已登记项目:", json.dumps(data, ensure_ascii=False) if data else "（无）")
            return 0 if r.ok else 1
        if sub == "add":
            try:
                # 显式登记命令保持“缺省创建新身份”的能力（用户意图明确），
                # 但同样要暴露“身份文件未提交”的跨设备风险（DOC-031 P0）。
                import importlib
                cloud_onboard = importlib.import_module("core.cloud_sync.cloud_onboard")
                _save_project_binding = getattr(cloud_onboard, "_save_project_binding", None) or getattr(cloud_onboard, "save_project_binding")
                onboard_wizard = importlib.import_module("core.cloud_sync.onboard_wizard")
                ensure_project_identity = getattr(onboard_wizard, "ensure_project_identity", None) or getattr(onboard_wizard, "_ensure_project_identity")
                warn_uncommitted_identity = getattr(onboard_wizard, "warn_uncommitted_identity", None) or getattr(onboard_wizard, "_warn_uncommitted_identity")
                identity = ensure_project_identity(root, args.project_slug)
                warn_uncommitted_identity(root)
            except Exception as exc:  # noqa: BLE001
                print(f"[cloud] 项目身份准备失败: {exc}")
                return 1
            r = client.register_project(
                slug=identity["slug"], name=identity["slug"],
                project_key=identity["project_key"],
            )
            print(f"[cloud] 登记项目: status={r.status} {r.error or ''}")
            if r.ok:
                pid = client.project_id(r)
                remote_key = client.project_key(r)
                if not pid:
                    print("[cloud] 项目登记响应缺少 project_id，拒绝写入项目绑定")
                    return 1
                if remote_key != identity["project_key"]:
                    print("[cloud] 项目登记响应 project_key 不一致，拒绝写入项目绑定")
                    return 1
                try:
                    _save_project_binding(
                        root, pid, identity["slug"], identity["project_key"],
                    )
                    print("[cloud] 已写入 .bugx-project.json（请提交到仓库，换机克隆即可对上同一云端项目）")
                except Exception as exc:  # noqa: BLE001
                    print(f"[cloud] 项目绑定/身份文件写入失败: {exc}")
                    return 1
            return 0 if r.ok else 1
        return 2

    if act == "devices":
        r = client.sync_status()
        data = client._data(r)
        print("[cloud] 设备同步状态:", json.dumps(data, ensure_ascii=False) if data else "（空）")
        return 0 if r.ok else 1

    if act == "onboard":
        # 项目级 onboarding 状态；显式 ``on`` 是“开启并立即同步”，而不是
        # 只写一个 pending 标记再要求 Agent 解释下一步。
        try:
            from core.cloud_sync.cloud_onboard import get_state, set_state
            cur = get_state(root)
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] onboarding 状态读取失败: {exc}")
            return 1
        onboard_action = getattr(args, "onboard_action", "status")
        if onboard_action == "status":
            print(f"[cloud] 本项目 onboarding: {cur}")
            return 0
        if onboard_action == "off":
            try:
                set_state(root, "local")
            except Exception as exc:  # noqa: BLE001
                print(f"[cloud] onboarding 写失败: {exc}")
                return 1
            print("[cloud] onboarding 已设为 local(本地模式)")
            return 0
        # ``on`` 是显式用户动作：立即走完整登录/设备登记/全量同步链路。
        try:
            set_state(root, "pending")
        except Exception as exc:  # noqa: BLE001
            print(f"[cloud] onboarding 写失败: {exc}")
            return 1
        print("[cloud] 已开启云端同步，立即同步 global + project…")
        sync_args = argparse.Namespace(
            project_slug=args.project_slug,
            sync_scope="all",
            detail_mode=None,
        )
        return _cloud_sync_command(sync_args, client, root, get_device_id)

    return 2


def _supervisor_deploy(root: Path) -> int:
    """接入 canonical stdio MCP。"""
    try:
        status, backup = _ensure_opencode_config(root)
    except CliError as exc:
        print(f"[supervisor] OpenCode 配置未修改: {exc}")
        return 1
    suffix = f"；备份={backup.relative_to(root)}" if backup else ""
    print(f"[supervisor] 已接入 canonical bugx-supervisor stdio MCP（{status}{suffix}）")
    return 0


def _supervisor_status(root: Path) -> int:
    """显示运行态和宿主配置状态。

    先检查是否有活跃的 legacy 运行态（audit-supervisor.json），
    再检查 engine runtime 状态，最后才检查宿主 MCP 配置。
    避免在运行态正常但宿主配置缺失时误导操作者。
    """
    print("  Supervisor 状态")
    print("-" * 40)

    # 1. 检查 legacy 运行态
    legacy_state_path = root / ".bugx" / "audit-supervisor.json"
    if legacy_state_path.is_file():
        try:
            import json
            raw = json.loads(legacy_state_path.read_text(encoding="utf-8"))
            status_val = raw.get("status", "unknown")
            covered = len(raw.get("coverage", {}))
            covered_count = sum(
                1 for v in raw.get("coverage", {}).values()
                if isinstance(v, dict) and v.get("status") == "covered"
            ) if isinstance(raw.get("coverage"), dict) else 0
            total_units = raw.get("total_units") or covered
            findings = len(raw.get("findings", []))
            print(f"  运行态: {status_val}")
            print(f"  覆盖: {covered_count}/{total_units} 单元")
            print(f"  Findings: {findings}")
            if status_val in ("BUDGET_EXHAUSTED", "FAILED"):
                print(f"  ⚠ 运行态处于 {status_val}，需要处理")
        except (OSError, ValueError) as exc:
            print(f"  运行态文件存在但解析失败: {exc}")
    else:
        print("  运行态: 未启动（无 audit-supervisor.json）")

    # 2. 检查 engine runtime
    runtime_path = root / ".bugx" / "bugx-runtime.json"
    if runtime_path.is_file():
        try:
            import json
            rt = json.loads(runtime_path.read_text(encoding="utf-8"))
            lid = rt.get("lifecycle_id", "-")
            print(f"  Lifecycle: {lid}")
        except (OSError, ValueError):
            pass

    print("-" * 40)

    # 3. 检查**当前宿主**的 MCP 配置
    # 缺陷 #12：这里原本无条件调用 OpenCode 的检查器并打印 ``OpenCode canonical
    # MCP 配置``，于是在 dsh / claude / codex 宿主上 status 永远报「✗ 缺少
    # opencode.json」，让用户去装一个根本没在用的宿主。宿主名必须来自环境探测，
    # 配置校验复用 ``_check_host_mcp_config`` 的逐宿主分派。
    host_name = _detected_host_name(root)
    config_ok, config_detail = _check_host_mcp_config(root, host_name)
    display = "未识别宿主" if host_name == "unknown" else host_name
    print(f"  {'✓' if config_ok else '✗'} {display} 宿主 MCP 配置")
    if not config_ok:
        print(f"  {config_detail}")
    return 0 if config_ok else 1
