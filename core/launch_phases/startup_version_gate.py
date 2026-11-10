#!/usr/bin/env python3
"""launch_phases/startup_version_gate.py - 启动版本一致性门禁与远端更新检查。"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.launch_phases.common import (
    AGENT_DIR,
    HOST_DISPLAY_NAMES,
    ISOLATED_REMOTE_VERSION_RESULT,
    resolve_project_root,
    uses_project_layout,
    child_environment,
    run_child,
    POLLING_DECISION_REQUIRED,
    bugx_paths,
    bugx_logging,
)
import shutil
import subprocess
import core.secure_atomic as secure_atomic
from core.launch_phases.session_runtime import (
    iso_time,
    load_runtime,
    save_runtime,
    append_runtime_event,
    runtime_lock,
)




from core.launch_phases.common import resolve_project_root as _orig_resolve_project_root
from core.launch_phases.session_runtime import load_runtime as _orig_load_runtime, save_runtime as _orig_save_runtime

def _sg():
    """已加载的 ``startup_gates`` 模块（点分名优先，扁平名仅为归档布局兼容）。"""
    return (sys.modules.get("core.launch_phases.startup_gates")
            or sys.modules.get("launch_phases.startup_gates"))

def _sg_dispatch(name, fallback_fn=None):
    def wrapper(*args, **kwargs):
        sg = _sg()
        if sg is not None and hasattr(sg, name):
            target = getattr(sg, name)
            if target is not wrapper:
                return target(*args, **kwargs)
        if fallback_fn is not None:
            return fallback_fn(*args, **kwargs)
        raise RuntimeError(f"{name} not available and no fallback provided")
    wrapper.__name__ = name
    return wrapper

def _orig_maybe_onboard(*args, **kwargs):
    try:
        from core.cloud_sync.onboard_wizard import maybe_onboard as fn
        return fn(*args, **kwargs)
    except Exception:
        return None

def _orig_get_state(*args, **kwargs):
    try:
        from core.cloud_sync.cloud_onboard import get_state as fn
        return fn(*args, **kwargs)
    except Exception:
        return None

def _orig_set_state(*args, **kwargs):
    try:
        from core.cloud_sync.cloud_onboard import set_state as fn
        return fn(*args, **kwargs)
    except Exception:
        return None

def _orig_sync_layers(*args, **kwargs):
    try:
        from core.cloud_sync.cloud_onboard import sync_layers as fn
        return fn(*args, **kwargs)
    except Exception:
        return None

def _orig_uses_project_layout(*args, **kwargs):
    from core.launch_phases.common import uses_project_layout as fn
    return fn(*args, **kwargs)

resolve_project_root = _sg_dispatch("resolve_project_root", _orig_resolve_project_root)
uses_project_layout = _sg_dispatch("uses_project_layout", _orig_uses_project_layout)
maybe_onboard = _sg_dispatch("maybe_onboard", _orig_maybe_onboard)
get_state = _sg_dispatch("get_state", _orig_get_state)
set_state = _sg_dispatch("set_state", _orig_set_state)
sync_layers = _sg_dispatch("sync_layers", _orig_sync_layers)
load_runtime = _sg_dispatch("load_runtime", _orig_load_runtime)
save_runtime = _sg_dispatch("save_runtime", _orig_save_runtime)

class _CloudClientDispatcher:
    def __call__(self, *args, **kwargs):
        sg = _sg()
        cls = getattr(sg, "CloudClient", None) if sg else None
        if cls is not None:
            return cls(*args, **kwargs)
        from core.cloud_sync.cloud_client import CloudClient as real_cls
        return real_cls(*args, **kwargs)
    def __getattr__(self, name):
        sg = _sg()
        cls = getattr(sg, "CloudClient", None) if sg else None
        if cls is not None:
            return getattr(cls, name)
        from core.cloud_sync.cloud_client import CloudClient as real_cls
        return getattr(real_cls, name)

CloudClient = _CloudClientDispatcher()

FATAL_BLOCKED_BANNER = (
    "\n[FATAL_BLOCKED] 阻断已确凿，严禁模型自行连续重试或发散自旋推理。\n"
    "【强制停机指示】请立即停止调用工具，直接向用户呈现上述阻断原因与修复建议。\n"
)

def _si():
    """已加载的 ``startup_interaction`` 模块（同上）。"""
    return (sys.modules.get("core.launch_phases.startup_interaction")
            or sys.modules.get("launch_phases.startup_interaction"))



def emit_startup_gate(*args, **kwargs):
    mod = _sg()
    if mod and hasattr(mod, "emit_startup_gate"):
        return mod.emit_startup_gate(*args, **kwargs)
    raise RuntimeError("startup_gates.emit_startup_gate not available")

def _stdin_tty():
    mod = _sg() or _si()
    if mod and hasattr(mod, "stdin_tty"):
        return mod.stdin_tty()
    if mod and hasattr(mod, "_stdin_tty"):
        return mod._stdin_tty()
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False

def _tty_menu_available():
    mod = _sg() or _si()
    if mod and hasattr(mod, "tty_menu_available"):
        return mod.tty_menu_available()
    if mod and hasattr(mod, "_tty_menu_available"):
        return mod._tty_menu_available()
    return False

def detect_startup_environment():
    mod = _sg()
    if mod and hasattr(mod, "detect_startup_environment"):
        return mod.detect_startup_environment()
    return "terminal"

def normalize_cli_version(value: str) -> str | None:
    """Normalize a BugX/Python version to the compatibility contract version.

    委托 :func:`bugx_paths.contract_version`（唯一实现）。历史缺陷：此处曾把
    ``+build`` 保留在 base 组里，于是 ``0.1.0+2`` 与 ``0.1.0`` 被判为不一致并
    **拒绝启动**，而 ``bugx doctor`` 用另一套规则判为"一致"——同一份清单、两个
    相反结论。归一化规则只能有一处，否则两边必然再次漂移。
    """
    return bugx_paths.contract_version(value)


def installed_cli_version(root: Path) -> str | None:
    """Read the CLI version without letting an old PATH wrapper win.

    Scheme 3 has one executable runtime: the global engine.  When a launcher
    is executed directly from that engine (for example via ``bugx engine path``)
    there may be no CLI wrapper process to report its version, and probing an
    unrelated ``bugx`` found on PATH can reintroduce the very drift this layout
    removes.  An explicit ``BUGX_CLI_VERSION`` still wins because the wrapper
    uses it to prove that its own version matches the engine.  Direct global
    launcher execution falls back to the engine's own marker; source-tree
    launchers keep the historical PATH probe for compatibility and tests.
    """
    override = os.environ.get("BUGX_CLI_VERSION")
    if override:
        normalized = _normalize_cli_version(override)
        if normalized:
            return normalized
    try:
        runtime_root = Path(__file__).resolve().parent.parent
        engine_root = _global_engine_root().resolve()
        if runtime_root == engine_root:
            engine_version = version_marker(engine_root / "metadata.txt")
            if engine_version is None:
                engine_version = version_marker(engine_root / "bugx.md")
            if engine_version:
                return engine_version
    except (OSError, ValueError, RuntimeError):
        pass
    executable = shutil.which("bugx")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"], cwd=root, capture_output=True,
            text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(
        r"\bbugx(?:\s+cli)?(?:\s+version)?\s+v?"
        r"(?P<base>\d+\.\d+\.\d+(?:\+[0-9A-Za-z.-]+)?)"
        r"(?:[-.]?(?:alpha|a|beta|b|rc)[-.]?\d+)?\b",
        result.stdout or "",
        re.IGNORECASE,
    )
    return match.group("base") if result.returncode == 0 and match else None

def global_engine_root() -> Path:
    return bugx_paths.global_engine_root()

_global_engine_root = _sg_dispatch("_global_engine_root", global_engine_root)

def version_marker(path: Path) -> str | None:
    """Read a human-readable BugX version marker without failing startup."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = re.search(
        r"(?:bugx_version[\"']?\s*:\s*[\"']?|^version:\s*)"
        r"([0-9]+\.[0-9]+\.[0-9]+(?:\+[0-9A-Za-z.-]+)?)", text, re.MULTILINE,
    )
    return match.group(1) if match else None

def startup_release_version(value: object) -> tuple[int, int, int] | None:
    """Parse the strict x.y.z release format used by the update endpoint."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        return None
    major, minor, patch = (int(part) for part in value.split("."))
    return major, minor, patch

def startup_version_banner(*, stream=None) -> None:
    """Always show the Agent version before dispatching a launch command."""
    stream = stream or sys.stdout
    root = resolve_project_root()
    engine = _global_engine_root()
    engine_version = version_marker(engine / "metadata.txt")
    if engine_version is None:
        engine_version = version_marker(engine / "bugx.md")
    # The project manifest records layout/installation state; executable code
    # has one version source: the global engine.
    project_version = version_marker(root / ".bugx" / "manifest.json")
    cli_version = _installed_cli_version(root)
    display = engine_version or project_version or cli_version or "unknown"
    print(f"[BugX] Agent version: {display}", file=stream)
    print(
        "[BugX] Version contract: "
        f"cli={cli_version or 'unknown'} "
        f"project_manifest={project_version or 'unknown'} "
        f"engine={engine_version or 'unknown'}",
        file=stream,
    )

def startup_cloud_onboarding(
    cloud_sync_policy: str | None = None, *, autonomous: bool = False,
) -> int:
    """取得云端同步决定，并在经验上下文构建前完成一次正式同步。"""
    if not uses_project_layout():
        return 0
    try:
        from core.cloud_sync.cloud_onboard import get_state, set_state
        state = get_state(resolve_project_root())
        policy = cloud_sync_policy
        if autonomous and policy is None and state == "pending":
            # An undecided project is not consent to upload. Headless startup
            # defaults only this pending state to local; persisted or explicit
            # sync choices remain authoritative below.
            policy = "local"
        if policy in {"skip", "local"} and state == "pending":
            # Profile 只补齐未决状态；已由用户选择的 done/local 不被覆盖。
            set_state(resolve_project_root(), "local")
            state = "local"
            print("[launch_bugx] ✓ Profile 已选择本地云同步模式")
        elif policy == "sync" or state in {"sync", "done"}:
            # 云端同步模式：严禁擅自消极降级为 local！
            # 未登录时必须直接跳转浏览器登录授权页面，授权完成后自动对齐并同步。
            pass
        elif state == "pending":
            # 仅在调用方未显式指定云端策略、且当前处于完全无交互环境时，才临时以本地模式运行，避免无头 CI 挂起
            if not _stdin_tty() and not _tty_menu_available():
                try:
                    from core.cloud_sync.cloud_client import CloudClient
                    client = CloudClient()
                    is_auth = bool(client.cred.is_authenticated())
                except Exception:
                    is_auth = False
                if not is_auth:
                    set_state(resolve_project_root(), "local")
                    state = "local"
                    print("[launch_bugx] ✓ 无交互环境下未配置云端策略，已采用本地模式运行（不阻断启动）")

        # 门禁重入去重（build 389）：``pre`` 是无状态一次性进程，每道门禁应答后
        # 都要重跑 pre，此前每次都全量推拉云同步（实测每轮 6 个网络往返、秒级开销×N）。
        # 已 done/sync 且距本次启动上一次同步成功不足 900s 时直接复用结果；
        # 显式 ``--cloud-sync sync`` 视为用户点名同步，不跳过。
        marker_path = resolve_project_root() / ".bugx" / "state" / "cloud-sync-startup.json"
        marker_age: float | None = None
        if state in {"sync", "done"} and policy != "sync":
            try:
                marker_age = time.time() - float(
                    json.loads(marker_path.read_text(encoding="utf-8")).get("at") or 0.0
                )
            except (OSError, ValueError):
                marker_age = None
        if marker_age is not None and 0.0 <= marker_age <= 900.0:
            print(
                f"[launch_bugx] ✓ 门禁重入：{int(marker_age)} 秒前已完成本次启动云同步，"
                "跳过重复推拉"
            )
            synced = True
        else:
            synced = maybe_onboard(
                resolve_project_root(), require_interaction=not autonomous, perform_sync=True,
                decision=("sync" if (policy == "sync" and state == "pending") else None),
                allow_identity_create=autonomous,
            )
            if synced:
                try:
                    marker_path.parent.mkdir(parents=True, exist_ok=True)
                    marker_path.write_text(
                        json.dumps({"at": time.time()}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except (OSError, ValueError):
                    pass
        state = get_state(resolve_project_root())
        if state not in {"done", "local"}:
            # ``sync`` = 用户**已显式选择**云端同步（`init --cloud-sync sync`），
            # 但本轮同步没走到 ``done``（服务端/网络/身份失败）。此前它与
            # 「从未选择」共用同一条「决策尚未完成」文案，导致用户被告知去做他
            # 已经做过的事（build 288 实测）。两种情形必须分开报告。
            if state == "sync":
                reason = ""
                try:
                    payload = json.loads(
                        (resolve_project_root() / ".bugx" / "cloud-last-error.json")
                        .read_text(encoding="utf-8")
                    )
                    reason = str(payload.get("error") or "")
                except (OSError, ValueError):
                    reason = ""
                print(
                    "[launch_bugx] ✗ 云端同步已获授权但本轮未完成；"
                    + ("自治模式已 BLOCKED，不会等待交互。" if autonomous else "")
                )
                if reason == "data_sharing_revoked":
                    print(
                        "  原因：数据共享授权已被撤销（隐私门禁，不会绕过）。"
                        "如需云端同步请显式重新授权："
                        "`bugx cloud config --root . --accept-data-sharing`；"
                        "本地开发可改用 `--cloud-sync local`。"
                    )
                elif reason:
                    print(f"  原因：{reason}；请检查 `bugx cloud status --root .`。")
                else:
                    print("  原因未记录；请检查 `bugx cloud status --root .` 后重试。")
                return POLLING_DECISION_REQUIRED
            print(
                "[launch_bugx] ✗ 云端多设备同步决策尚未完成；"
                + ("自治模式已 BLOCKED，不会等待交互。" if autonomous else
                   "本轮不会进入审查方式、轮询策略或修复模式询问。")
            )
            print("  请在当前云端菜单完成选择并成功登录/同步，或明确选择本地模式后重试。")
            return POLLING_DECISION_REQUIRED
    except Exception as exc:
        # Import lazily so standalone protocol tests do not need cloud dependencies.
        try:
            from core.cloud_sync.cloud_onboard import CloudOnboardDecisionRequired
        except Exception:  # pragma: no cover - only malformed installations
            CloudOnboardDecisionRequired = ()
        if CloudOnboardDecisionRequired and isinstance(exc, CloudOnboardDecisionRequired):
            if getattr(exc, "request_emitted", False):
                if autonomous:
                    print("[launch_bugx] ✗ 自治模式需要云端登录/身份决策，已 BLOCKED，不等待交互。")
                    return POLLING_DECISION_REQUIRED
                print("[launch_bugx] ✗ 云端同步/项目身份决策尚未完成；当前交互请求已交给宿主处理。")
                return POLLING_DECISION_REQUIRED
            if autonomous:
                print("[launch_bugx] ✗ 自治模式需要云端登录/身份决策，已 BLOCKED，不发起交互。")
                return POLLING_DECISION_REQUIRED
            environment = detect_startup_environment()
            print("[launch_bugx] ✗ 启动前需要云同步/登录决策。")
            emit_startup_gate(
                "cloud_sync",
                "选择本项目的云端多设备同步模式",
                [
                    {
                        "label": "立即登录并同步",
                        "description": "完成登录、设备登记并同步本项目",
                        "value": "sync",
                "action": "cloud_sync_all",
                    },
                    {
                        "label": "本地模式",
                        "description": "持久化本地模式，不自动同步",
                        "value": "local",
                "action": "set_cloud_local",
                    },
                    {
                        "label": "下次再说",
                        "description": "保持待决定并停止本轮启动",
                        "value": "later",
                        "action": "stop_startup",
                    },
                ],
                environment=environment,
                detail="选择后执行 bugx cloud sync 或 bugx cloud onboard off，再重新运行 pre",
            )
        try:
            from core.cloud_sync.cloud_onboard import get_state
            if get_state(resolve_project_root()) == "done":
                print(f"[launch_bugx] ⚠ 云同步 onboarding 遇网络异常（{exc}），自动降级为本地运行（不阻断启动）")
                return 0
        except Exception:
            pass
        print(f"[launch_bugx] ✗ 云同步 onboarding 检查失败：{exc}")
        return 1
    if state == "done" and not synced:
        print(
            "[launch_bugx] ⚠ 云端网络不可达或同步未完成，自动平滑降级为本地模式（已标记 sync_pending，不阻断启动）"
        )
        try:
            from core.cloud_sync.auto_sync import schedule_recovery
            for scope in ("global", "project"):
                schedule_recovery(resolve_project_root(), scope)
        except (ImportError, OSError, ValueError, RuntimeError):
            pass
        try:
            with runtime_lock():
                st = load_runtime()
                st["sync_status"] = "sync_pending"
                save_runtime(st)
        except Exception:
            pass
        return 0
    return 0

def run_cloud_boundary_sync(runtime: dict, scope: str) -> str:
    """在 Life/终局 Round 边界同步对应层，并把结果写回运行态。"""
    if scope not in {"project", "global"}:
        raise ValueError("同步边界只能是 project/global")
    status = "local"
    detail = "local mode"
    consecutive_failures = int(runtime.get("cloud_sync_consecutive_failures") or 0)
    # L5-OPT-4: 离线熔断断路器（连续失败达到 5 次以上转入休眠保护，消除每轮无意义子进程唤醒）
    if consecutive_failures >= 5 and scope == "project":
        status = "offline_dormant"
        detail = f"连续 {consecutive_failures} 次网络同步失败，进入离线熔断保护（可运行 bugx cloud sync 重试）"
    elif uses_project_layout():
        try:
            from core.cloud_sync.cloud_client import CloudClient
            from core.cloud_sync.cloud_onboard import get_state, sync_layers
            if get_state(resolve_project_root()) == "done":
                summary = sync_layers(
                    CloudClient(), resolve_project_root(), scopes={scope},
                )
                status = "ready" if summary.get("ok") else "sync_pending"
                detail = summary.get("error") or summary.get(f"{scope}_pull") or status
            else:
                detail = "cloud sync is not enabled"
        except Exception as exc:  # noqa: BLE001
            status = "sync_pending"
            detail = f"{type(exc).__name__}: {exc}"
    if status == "ready":
        consecutive_failures = 0
    elif status == "sync_pending":
        consecutive_failures += 1
    try:
        with runtime_lock():
            state = load_runtime()
            state["cloud_sync_consecutive_failures"] = consecutive_failures
            boundaries = state.get("sync_boundaries")
            if not isinstance(boundaries, dict):
                boundaries = {}
            boundaries[scope] = {
                "status": status, "detail": str(detail),
                "updated_at": iso_time(time.time()),
            }
            state["sync_boundaries"] = boundaries
            statuses = [
                item.get("status") for item in boundaries.values()
                if isinstance(item, dict)
            ]
            state["sync_status"] = (
                "sync_pending" if "sync_pending" in statuses else
                "ready" if "ready" in statuses else
                "local"
            )
            append_runtime_event(
                state, "cloud-boundary-sync", time.time(),
                f"scope={scope} status={status}",
            )
            save_runtime(state)
    except (OSError, ValueError):
        print(f"  ⚠ 无法写入 {scope} 云同步边界状态；后续启动将重新检查")
    if status == "sync_pending":
        print(f"  ⚠ {scope} 边界同步未完成：已保留待重放状态（{detail}）")
        try:
            from core.cloud_sync.auto_sync import schedule_recovery
            schedule_recovery(resolve_project_root(), scope)
        except (ImportError, OSError, ValueError, RuntimeError):
            pass
    elif status == "offline_dormant":
        print(f"  ℹ {scope} 云端同步连续失败达到阈值，已转入离线静默状态（可通过 bugx cloud sync 手动重试）")
    return status

def startup_version_gate() -> tuple[bool, str, str]:
    """Require CLI, project layout/manifest and global engine versions to agree."""
    root = resolve_project_root()
    cli_version = _installed_cli_version(root)
    try:
        status = bugx_paths.version_status(
            root, _global_engine_root(), cli_version=cli_version,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return False, f"版本清单无法读取：{exc}{FATAL_BLOCKED_BANNER}", "version_contract"
    if status.get("consistent"):
        return True, str(status.get("detail", "版本一致")), ""
    versions = status.get("versions", {})
    project_versions = status.get("legacy_versions")
    project_integrity = status.get("project_core_integrity") or {}
    layout_invalid = (
        isinstance(project_versions, dict)
        and "project_layout" in project_versions
        and project_versions["project_layout"] != bugx_paths.LAYOUT_VERSION
    ) or (project_integrity.get("available") and not project_integrity.get("ok"))
    if layout_invalid:
        return False, (
            f"项目 BugX 状态不完整或格式不受支持：{status.get('detail', '')}；"
            f"程序版本标记：{versions}。布局格式标记不是 BugX 程序版本；"
            "已保留既有项目状态，请检查并恢复，不要直接重建或刷新全局引擎。"
            f"{FATAL_BLOCKED_BANNER}"
        ), "project_layout"
    return False, (
        f"{status.get('detail', '版本标记不一致')}；当前版本：{versions}。"
        "本次启动不会自动重试；请检查实际不一致的程序版本或缺失的版本标记。"
        "若仅是 CLI 版本识别失败，可临时设置 `BUGX_CLI_VERSION=X.Y.Z` 覆盖识别；"
        "该覆盖不替代实际 CLI 升级，也不能掩盖项目/引擎版本漂移。"
        f"{FATAL_BLOCKED_BANNER}"
    ), "version_contract"

# PERF-07：远端版本检查的硬超时与后台线程状态。
# 单次请求 timeout 压到 1 秒且不重试；此设置不是 DNS/配置读取的总墙钟保证。
# CLI 仍由后台线程检查；进程内 harness 必须在恢复全局环境前完成检查。
REMOTE_VERSION_CHECK_TIMEOUT_SECONDS = 1.0

REMOTE_VERSION_LOCK = threading.Lock()
REMOTE_VERSION_THREAD: threading.Thread | None = None
def pending_remote_version_result() -> dict[str, Any]:
    return {
        "status": "pending",
        "detail": "",
        "current_version": "",
        "latest_version": "",
        "checked_at": None,
    }


_pending_remote_version_result = pending_remote_version_result
REMOTE_VERSION_RESULT: dict[str, Any] = _pending_remote_version_result()


def _active_remote_version_result() -> dict[str, Any]:
    sg = _sg()
    if sg and hasattr(sg, "_REMOTE_VERSION_RESULT"):
        target = getattr(sg, "_REMOTE_VERSION_RESULT")
        if target is not _REMOTE_VERSION_RESULT and isinstance(target, dict):
            return target
    return _REMOTE_VERSION_RESULT


def remote_version_check_result() -> dict[str, Any]:
    """返回最近一次后台远端版本检查的快照（PERF-07）。

    状态取值：``pending``（尚未运行/进行中）、``disabled``（显式关闭）、
    ``unavailable``（cloud_client 不可导入）、``network_error``（不可达）、
    ``invalid_metadata``（服务端元数据无效）、``up_to_date``、
    ``update_available``、``update_required``。
    """
    scoped_result = ISOLATED_REMOTE_VERSION_RESULT.get()
    if scoped_result is not None:
        if not scoped_result:
            scoped_result.update(_pending_remote_version_result())
        return dict(scoped_result)
    with REMOTE_VERSION_LOCK:
        return dict(_active_remote_version_result())


def clear_remote_version_check_cache() -> None:
    """清空后台版本检查的结果缓存（测试用；不影响已启动的线程）。"""
    global _REMOTE_VERSION_THREAD
    scoped_result = ISOLATED_REMOTE_VERSION_RESULT.get()
    if scoped_result is not None:
        scoped_result.clear()
        scoped_result.update(_pending_remote_version_result())
        return
    with REMOTE_VERSION_LOCK:
        REMOTE_VERSION_THREAD = None
        target = _active_remote_version_result()
        target.clear()
        target.update(_pending_remote_version_result())


def record_remote_version_result(
    *, status: str, detail: str = "",
    current_version: str = "", latest_version: str = "",
) -> None:
    """记录后台版本检查结论，供 :func:`remote_version_check_result` 读取。"""
    payload = {
        "status": status,
        "detail": detail,
        "current_version": current_version,
        "latest_version": latest_version,
        "checked_at": time.time(),
    }
    scoped_result = ISOLATED_REMOTE_VERSION_RESULT.get()
    if scoped_result is not None:
        scoped_result.update(payload)
        return
    with REMOTE_VERSION_LOCK:
        target = _active_remote_version_result()
        target.update(payload)


def schedule_startup_remote_version_check(
    *, stream=None,
) -> threading.Thread | None:
    """在守护线程中执行远端版本检查，立即返回、绝不阻塞启动（PERF-07）。

    旧实现自称「非阻断」，实际是在 ``main()`` 中同步调用：黑洞网络下
    ``2 × 5s + sleep ≈ 10.5s`` 全部压在任何后续启动步骤之前。现在只负责
    启动一个 daemon 线程；进程退出时线程自动终止，慢检查不可能拖慢
    ``bugx pre``。结果写入进程内缓存（:func:`remote_version_check_result`）。

    进程内 harness 没有 OS 进程退出边界：检查在当前作用域同步完成，结果写入
    ContextVar 容器，既不遗留后台线程也不覆盖宿主的线程/结果状态。
    """
    global _REMOTE_VERSION_THREAD
    if ISOLATED_REMOTE_VERSION_RESULT.get() is not None:
        _startup_remote_version_check(stream=stream)
        return None
    with REMOTE_VERSION_LOCK:
        sg = _sg()
        sg_thread = getattr(sg, "_REMOTE_VERSION_THREAD", None) if sg else None
        if sg_thread is not None and getattr(sg_thread, "is_alive", lambda: False)():
            return sg_thread
        if _REMOTE_VERSION_THREAD is not None and _REMOTE_VERSION_THREAD.is_alive():
            return _REMOTE_VERSION_THREAD
        thread_cls = getattr(getattr(sg, "threading", None), "Thread", threading.Thread)
        thread = thread_cls(
            target=_startup_remote_version_check,
            kwargs={"stream": stream},
            name="bugx-remote-version-check",
            daemon=True,
        )
        REMOTE_VERSION_THREAD = thread
    thread.start()
    return thread


def startup_remote_version_check(*, stream=None) -> None:
    """远端版本检查体；经调度器选择 CLI 后台或 harness 同步边界。

    Queries the BugX server for the latest CLI version. If a newer version
    is available, prints a non-blocking hint. If the check fails (air-gap,
    DNS, etc.), it is silently skipped.

    直接调用本函数会阻塞当前线程；启动链路经
    :func:`schedule_startup_remote_version_check` 决定执行边界。

    Can be disabled with BUGX_DISABLE_VERSION_CHECK=1 env var or
    disable_version_check=true in cloud-config.json.
    """
    stream = stream or sys.stdout
    if os.environ.get("BUGX_DISABLE_VERSION_CHECK", "").lower() in {"1", "true", "yes"}:
        _record_remote_version_result(status="disabled", detail="BUGX_DISABLE_VERSION_CHECK")
        return
    cloud_client = getattr(_sg(), "_cloud_client_module", None)
    client_metadata = getattr(_sg(), "_client_metadata_module", None)
    if cloud_client is None or client_metadata is None:
        _record_remote_version_result(status="unavailable", detail="cloud_client 不可导入")
        return

    # Check persisted config for disable_version_check.
    try:
        cfg = cloud_client.load_cloud_config()
        if cfg.get("disable_version_check", False):
            _record_remote_version_result(status="disabled", detail="cloud-config")
            return
    except Exception:  # noqa: BLE001
        pass  # config read failure; continue with env check

    try:
        client = cloud_client.CloudClient()
        # PERF-07: 单次短超时且不重试——后台线程也不应长期挂起。
        try:
            client.timeout = min(float(client.timeout), REMOTE_VERSION_CHECK_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            client.timeout = REMOTE_VERSION_CHECK_TIMEOUT_SECONDS
        if hasattr(client, "retries"):
            client.retries = 0
        # Get current CLI version.
        cli_ver = ""
        if hasattr(cloud_client, "current_version_str"):
            cli_ver = cloud_client.current_version_str()
        elif hasattr(cloud_client, "__version__"):
            cli_ver = cloud_client.__version__
        if cli_ver == "unknown":
            cli_ver = ""
        result = client.check_version(
            current_version=cli_ver,
            channel=client_metadata.release_channel(),
        )
        if not result.ok:
            _record_remote_version_result(
                status="network_error", detail=str(result.error or ""),
                current_version=str(cli_ver),
            )
            return  # network unreachable; silently skip
        data = result.data if isinstance(result.data, dict) else {}
        info = data.get("data") if isinstance(data.get("data"), dict) else data
        latest = str(info.get("latest_version") or "").strip()
        min_client = str(info.get("min_client_version") or "").strip()
        current_tuple = _startup_release_version(cli_ver)
        latest_tuple = _startup_release_version(latest)
        min_tuple = _startup_release_version(min_client) if min_client else None
        if latest_tuple is None or (min_client and min_tuple is None):
            print(
                f"[version] 服务端版本元数据无效，已跳过启动提示: "
                f"latest={latest!r} min_client={min_client!r}",
                file=stream,
            )
            _record_remote_version_result(
                status="invalid_metadata",
                detail=f"latest={latest!r} min_client={min_client!r}",
                current_version=str(cli_ver),
            )
            return
        if current_tuple is None:
            _record_remote_version_result(
                status="invalid_metadata", detail=f"cli={cli_ver!r}",
                current_version=str(cli_ver), latest_version=latest,
            )
            return
        if latest_tuple < current_tuple:
            print(
                f"[version] 服务端发布版本 {latest} 低于当前 CLI {cli_ver}，"
                "已跳过启动更新提示",
                file=stream,
            )
            _record_remote_version_result(
                status="up_to_date", detail="服务端发布版本低于当前 CLI",
                current_version=str(cli_ver), latest_version=latest,
            )
            return
        if min_tuple is not None and min_tuple > latest_tuple:
            print(
                f"[version] 服务端版本元数据矛盾，已跳过启动提示："
                f"最低兼容版本 {min_client} 高于发布版本 {latest}",
                file=stream,
            )
            _record_remote_version_result(
                status="invalid_metadata", detail="min_client_version 高于 latest_version",
                current_version=str(cli_ver), latest_version=latest,
            )
            return
        update_required = (info.get("update_required") is True
                           or (min_tuple is not None and current_tuple < min_tuple))
        update_available = (info.get("update_available") is True
                            or latest_tuple > current_tuple)
        if update_required:
            print(
                f"\n⚠ BugX CLI 版本 {cli_ver} 低于最低兼容版本 {min_client}，"
                "可能导致 Cloud Sync 协议不兼容。",
                file=stream,
            )
            print(f"  最新版本: {latest}", file=stream)
            print("  请立即执行：bugx update\n", file=stream)
            _record_remote_version_result(
                status="update_required", detail=f"min_client={min_client}",
                current_version=str(cli_ver), latest_version=latest,
            )
        elif update_available and latest:
            print(f"\n✦ BugX CLI 有新版本可用: {latest}（当前: {cli_ver}）", file=stream)
            print("  建议执行：bugx update\n", file=stream)
            _record_remote_version_result(
                status="update_available", detail="",
                current_version=str(cli_ver), latest_version=latest,
            )
        else:
            _record_remote_version_result(
                status="up_to_date", detail="",
                current_version=str(cli_ver), latest_version=latest,
            )
    except Exception as exc:  # noqa: BLE001
        _record_remote_version_result(
            status="network_error", detail=f"{type(exc).__name__}: {exc}",
        )
        return

def emit_version_contract_gate(detail: str, environment=None) -> None:
    """Stop at one explicit version gate instead of feeding a retry loop.

    Version repair can replace the global engine and host entrypoints. The
    current process must therefore never repair and re-enter itself. The host
    performs one explicit repair action, restarts, and only then retries.
    """
    emit_startup_gate(
        "version_contract",
        "BugX CLI、项目核心和全局引擎版本契约不一致",
        [
            {
                "label": "执行一次受控更新并重启宿主",
                "description": "只执行一次更新；完成后重启宿主，再重试启动",
                "value": "repair_once",
                "action": "repair_version_contract",
                "retry": "only_after_host_restart_once",
            },
            {
                "label": "停止本轮",
                "description": "保留当前状态，等待人工处理版本契约",
                "value": "stop",
                "action": "stop_startup",
            },
        ],
        environment=environment,
        detail=detail + "；不得在同一会话中自动循环重试。",
    )

def emit_project_layout_gate(detail: str, environment=None) -> None:
    """Keep existing project data intact when its layout cannot be trusted."""
    emit_startup_gate(
        "project_layout",
        "项目 BugX 状态不完整或格式不受支持",
        [{
            "label": "停止本轮并检查项目状态",
            "description": "保留已有状态，检查或恢复项目数据后继续",
            "value": "stop",
            "action": "stop_startup",
        }],
        environment=environment,
        detail=detail,
    )


_normalize_cli_version = normalize_cli_version
_installed_cli_version = installed_cli_version
_startup_release_version = startup_release_version
_pending_remote_version_result = pending_remote_version_result
_record_remote_version_result = record_remote_version_result
_startup_remote_version_check = startup_remote_version_check
_REMOTE_VERSION_LOCK = REMOTE_VERSION_LOCK
_REMOTE_VERSION_RESULT = REMOTE_VERSION_RESULT
_REMOTE_VERSION_THREAD = REMOTE_VERSION_THREAD
