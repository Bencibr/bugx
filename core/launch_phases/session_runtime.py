#!/usr/bin/env python3
"""launch_phases/session_runtime.py - 状态机维护、runtime.json 加锁管理、时间分段与事件记录。"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.launch_phases.common import (
    AGENT_DIR,
    RUNTIME_FILE,
    RUNTIME_LOCK_FILE,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_STATES,
    HEARTBEAT_TIMEOUT_SECONDS,
    POLLING_POLICIES,
    REVIEW_MODES,
    COMMIT_POLICIES,
    REVIEW_DECISION_LATER,
    POLLING_DECISION_REQUIRED,
    AUDIT_TODO_ITEMS,
    RUNTIME_STARTED_THIS_PROCESS,
    set_runtime_started_this_process,
    diagnostic_logger,
    resolve_project_root,
    uses_project_layout,
    is_number,
    format_duration,
    SUPERVISOR_STATE,
    COVERAGE_EVIDENCE,
    LIFE_FILE,
    LESSON_LEDGER_FILE,
    GLOBAL_LESSON_LEDGER_FILE,
    LIFECYCLE_MAX_NO_PROGRESS_CYCLES,
    pollings_root,
    bugx_paths,
)
import tempfile
import threading
import uuid
import core.secure_atomic as secure_atomic


#: ``runtime_lock`` 的同进程**同线程**重入深度（LOG-03）。flock 对不同 fd
#: 仍互斥，因此重入必须由本线程自己识别；必须是 thread-local 而非普通全局，
#: 否则线程 A 持锁会让线程 B 误以为「已持锁」而跳过 flock，互斥直接失效。
_RUNTIME_LOCK_STATE = threading.local()


#: 显式钉住的 runtime 路径。设置后 ``refresh_runtime_paths`` 用它取代按
#: ``resolve_project_root()`` 的重算——否则直接给 ``RUNTIME_FILE`` 赋值会被
#: ``runtime_lock()``/``load_runtime()`` 里的路径刷新静默覆盖，「指定 root」
#: 的调用方（MCP host 心跳）与测试夹具都会失效。contextvar 按线程隔离，
#: 心跳 loop 线程与调用线程各自持钉，不会互相污染。
_RUNTIME_PATH_PIN: contextvars.ContextVar[dict[str, Path] | None] = contextvars.ContextVar(
    "bugx_runtime_path_pin", default=None,
)


@contextmanager
def _pinned_runtime_paths(runtime: Path, runtime_lock: Path | None = None):
    """在动态作用域内钉住 runtime 与锁路径，绕过 ``resolve_project_root()`` 重算。

    进入时立即写入 globals（直接读 ``RUNTIME_FILE`` 的调用方也立刻看到钉住值），
    退出时还原，避免钉住值泄漏到后续不相关的项目访问。
    """
    lock = (
        Path(runtime_lock) if runtime_lock is not None
        else Path(runtime).with_name("runtime.lock")
    )
    paths = {"RUNTIME_FILE": Path(runtime), "RUNTIME_LOCK_FILE": lock}
    previous = {k: v for k, v in ((k, globals().get(k)) for k in paths) if v is not None}
    token = _RUNTIME_PATH_PIN.set(paths)
    _apply_runtime_paths(paths)
    try:
        yield
    finally:
        _RUNTIME_PATH_PIN.reset(token)
        if previous:
            _apply_runtime_paths(previous)


def _runtime_lock_depth() -> int:
    """当前线程持有运行状态锁的重入深度。"""
    return int(getattr(_RUNTIME_LOCK_STATE, "depth", 0))


def _set_runtime_lock_depth(value: int) -> None:
    _RUNTIME_LOCK_STATE.depth = value



def refresh_runtime_paths() -> None:
    """Bind runtime paths to the active .bugx lifecycle.

    The project .bugx directory contains runtime state only. Executable assets
    are loaded from the global engine; a missing project layout is deliberately
    surfaced to callers.
    """
    pinned = _RUNTIME_PATH_PIN.get()
    if pinned is not None:
        # 显式钉住优先于重算：调用方已指定 runtime 路径时，不得再按
        # resolve_project_root() 覆盖，否则 root 参数静默失效。
        _apply_runtime_paths(pinned)
        return
    if not uses_project_layout():
        return
    # Use the same root resolver as every other global-engine launcher path.
    # Calling bugx_paths.project_root(AGENT_DIR) here is safe because the CLI
    # binds BUGX_PROJECT_ROOT before starting the engine.
    root = resolve_project_root()
    active = bugx_paths.require(root)
    _apply_runtime_paths({
        "COVERAGE_EVIDENCE": active.coverage,
        "SUPERVISOR_STATE": active.supervisor,
        "LESSON_LEDGER_FILE": active.knowledge_seed,
        "GLOBAL_LESSON_LEDGER_FILE": active.global_knowledge_seed,
        "RUNTIME_FILE": active.runtime,
        "RUNTIME_LOCK_FILE": active.runtime_lock,
    })


def _apply_runtime_paths(path_updates: dict[str, Path]) -> None:
    """把路径写进共享层与显式阶段模块，杜绝运行态路径漂移。

    阶段模块仍有少量历史 ``from common import`` 路径别名，需要同步以保持
    现有调用契约；使用显式包引用，不再通过 ``sys.modules`` 扫描并广播。
    """
    global COVERAGE_EVIDENCE, SUPERVISOR_STATE, LESSON_LEDGER_FILE
    global GLOBAL_LESSON_LEDGER_FILE, RUNTIME_FILE, RUNTIME_LOCK_FILE
    for k, v in path_updates.items():
        globals()[k] = v

    # These are explicit compatibility sinks for legacy ``from common import``
    # aliases.  Keep the list small and auditable until each consumer reads
    # ``common`` directly.
    from . import (
        common,
        finding_settlement,
        lesson_context,
        lifecycle_reporter,
        post_phase,
        pre_phase,
        report_sharing,
        round_orchestrator,
        startup_gates,
        supervisor_bridge,
        worker_orchestrator,
    )
    for module in (
        common,
        lesson_context,
        pre_phase,
        round_orchestrator,
        post_phase,
        worker_orchestrator,
        startup_gates,
        lifecycle_reporter,
        finding_settlement,
        report_sharing,
        supervisor_bridge,
    ):
        for k, v in path_updates.items():
            if hasattr(module, k):
                setattr(module, k, v)


def _ensure_project_runtime_paths() -> None:
    """Refresh paths before direct runtime helpers use module-level defaults."""
    if uses_project_layout():
        refresh_runtime_paths()


def audit_todo_path() -> Path | None:
    """Return the current Round's derived TODO view path.

    The current pointer is the sole scope selector.  This keeps the view in
    the same lifecycle ``state`` directory as runtime/coverage evidence and
    avoids accepting a path supplied by an agent or worker.
    """
    if not uses_project_layout() or bugx_paths is None:
        return None
    return bugx_paths.require(resolve_project_root()).state_root / "audit-todo.md"

def _audit_todo_content(runtime: dict, phase: str, reason: str | None = None) -> str:
    """Build the human-readable current-Round progress view.

    This is intentionally a projection of authoritative runtime boundaries;
    it contains no coverage-unit, worker, finding, priority, or completion
    decisions of its own.
    """
    runtime = runtime if isinstance(runtime, dict) else {}
    phase = str(phase or "running")
    completed_through = {
        "running": 4,
        "resumed": 4,
        "life-settled": 9,
        "round-completed": 11,
        "session-completed": len(AUDIT_TODO_ITEMS) - 1,
        "share-pending": len(AUDIT_TODO_ITEMS) - 1,
        "share-completed": len(AUDIT_TODO_ITEMS),
        "blocked": 11,
        "aborted": 0,
    }.get(phase, 0)
    active_index = {
        "running": 4,
        "resumed": 4,
        "life-settled": 9,
        "round-completed": 11,
        "session-completed": len(AUDIT_TODO_ITEMS) - 1,
        "share-pending": len(AUDIT_TODO_ITEMS) - 1,
        "blocked": 11,
    }.get(phase)
    lines = [
        "# BugX 当前 Round TODO",
        "",
        f"- Session：`{runtime.get('session_id') or 'unknown'}`",
        f"- Round：`{runtime.get('round_id') or runtime.get('lifecycle_id') or 'unknown'}`",
        f"- Life：`{runtime.get('life_no') or 'unknown'}`",
        f"- Runtime：`{runtime.get('state') or 'unknown'}`；事件数 `{len(runtime.get('events') or [])}`",
        f"- 同步：`{runtime.get('sync_status') or 'unknown'}`；共享队列状态由 `share_pending.json` 体现",
        f"- 阶段：`{phase}`",
        f"- 最后校准：`{iso_time(time.time())}`",
        "- 来源：`runtime.json`、事件账本、覆盖证据与 Round 报告（派生视图）",
        "",
    ]
    for index, item in enumerate(AUDIT_TODO_ITEMS):
        if index < completed_through:
            marker = "x"
        elif phase == "blocked" and index == active_index:
            marker = "!"
        elif active_index is not None and index == active_index:
            marker = "~"
        else:
            marker = " "
        lines.append(f"- [{marker}] {item}")
    if reason:
        lines.extend(["", f"- 当前原因：{reason}"])
    lines.extend([
        "",
        "> 本文件仅是当前 Round 的操作视图，不是账本、覆盖清单、Finding 来源或完成判定器。",
    ])
    return "\n".join(lines) + "\n"

def refresh_audit_todo(runtime: dict, phase: str, reason: str | None = None) -> Path | None:
    """Atomically refresh the derived TODO view without blocking authority."""
    path = audit_todo_path()
    if path is None:
        return None
    try:
        secure_atomic.atomic_write_text(
            path,
            _audit_todo_content(runtime, phase, reason),
            mode=0o600,
            parent_mode=0o700,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        # Runtime/events/report remain authoritative.  A stale or missing view
        # is recoverable and must never turn a completed boundary into failure.
        print(f"  （TODO 清单视图刷新失败，不影响权威状态：{exc}）")
        return None
    return path

def _default_runtime() -> dict:
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "run_id": None,
        "state": "idle",
        "active": False,
        "pid": None,
        "host_pid": None,
        "started_at": None,
        "started_epoch": None,
        "session_started_at": None,
        "session_started_epoch": None,
        "startup_action": None,
        "segment_started_epoch": None,
        "paused_at": None,
        "heartbeat_at": None,
        "heartbeat_epoch": None,
        "round_at_start": None,
        "session_elapsed_seconds": 0.0,
        "last_session_elapsed_seconds": 0.0,
        "total_elapsed_seconds": 0.0,
        "sessions_completed": 0,
        "sessions_aborted": 0,
        "sessions": [],
        "life_runs": [],
        "events": [],
        # 轮询策略独立于 verify_life.mode：后者控制修复方式，这里只控制
        # 当前轮死亡后的续轮行为。None 代表尚未完成启动前的必问交互。
        "polling_policy": None,
        # 提交策略属于启动上下文；log-only 会自动归一为 not_applicable。
        "commit_policy": None,
        "commit_status": None,
        "commit_boundary": None,
        "commit_ref": None,
        "commit_error": None,
        "sync_status": "local",
        "startup_id": None,
        "startup_observation": None,
        "startup_intent": None,
        "startup_result": None,
        # DOC-138: explicit host-agent autonomous mode.  The policy/hash are
        # captured at Session start and must not be inferred from model output.
        "host": None,
        "autonomous_mode": False,
        "execute": False,
        "workers": 0,
        "worker_capacity": None,
        "subagent_type": "bugx-worker",
        "stagger_seconds": 0.0,
        "provider": None,
        "autonomous_policy": None,
        "autonomous_policy_hash": None,
        "session_id": None,
        "round_id": None,
        "life_no": None,
        "life_kind": "mine",
        "session_state": "initializing",
        "session_closed_at": None,
        "final_round_id": None,
        # 审查执行面：manual 由宿主/Agent 按步骤推进；mcp-auto 由唯一的
        # bugx-supervisor MCP 负责状态、租约、心跳和门禁协调。None 代表
        # 尚未完成启动前的必问交互。
        "review_mode": None,
        # 仅在明确选择手动回退时记录 deferred；“下次再说”会停止本轮。
        "review_mode_deferred": False,
        "polling_round_auto_started": False,
        "polling_last_round": None,
        "polling_stop_reason": None,
        # Round 状态：verify_life.round/rounds_completed 表示 Round 内 Life。
        "lifecycle_no": 0,
        "lifecycle_id": None,
        "lifecycle_auto_started": False,
        "lifecycle_started_at": None,
        "lifecycle_started_epoch": None,
        "lifecycle_cycles_completed": 0,
        "lifecycle_findings_total": 0,
        "lifecycle_credited_total": 0,
        "lifecycle_fraud_total": 0,
        "lifecycle_last_life": None,
        "lifecycle_terminal": False,
        "lifecycle_last_report": None,
        "lifecycle_no_progress_cycles": 0,
        "lifecycle_stop_reason": None,
        "lifecycle_history": [],
        # Pattern IDs injected into this big lifecycle.  A later small
        # lifecycle receives only fresh cards, preventing stale repetition.
        "lifecycle_lesson_ids": [],
        "lifecycle_incident_ids": [],
        "lesson_recent_signatures": [],
        "realized_fix_incidents": [],
        "lesson_context": None,
        "project_profile_hash": None,
        "project_profile_revision": None,
        "project_profile_status": "unknown",
        "project_classification": None,
        "project_routing_tags": {},
        "audit_strategy": None,
    }

_RUNTIME_RESET_GUIDANCE = (
    "旧状态不迁移。确认要丢弃旧运行态后，请执行 "
    "`bugx reset --scope project --root <项目路径> --yes`；若重置被活动状态阻断，"
    "先确认没有 Worker 仍会写入状态，再显式加 `--force`。项目 reset 会先创建项目外归档。"
)


def _require_current_runtime_schema(state: dict, source: Path) -> None:
    """Reject old or incomplete runtime documents before they can be restored or used."""
    schema_version = state.get("schema_version")
    if schema_version != RUNTIME_SCHEMA_VERSION:
        raise ValueError(
            f"运行计时文件 schema_version 不受支持: {source} "
            f"(expected={RUNTIME_SCHEMA_VERSION}, actual={schema_version!r})；"
            f"{_RUNTIME_RESET_GUIDANCE}"
        )
    missing = sorted(set(_default_runtime()) - state.keys())
    if missing:
        raise ValueError(
            f"运行计时文件不符合当前 schema: {source}；缺少必需字段 "
            f"{', '.join(missing)}；{_RUNTIME_RESET_GUIDANCE}"
        )

def _is_pid_alive(pid: int | None) -> bool:
    """检测进程是否依然存活（跨平台且不把 Windows WinError 87 当死亡）。"""
    if pid is None or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: process exists
            try:
                exit_code = ctypes.c_uint32()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) \
                    and exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as err:
        import errno
        if getattr(err, "errno", None) == errno.ESRCH:
            return False
        if getattr(err, "errno", None) == errno.EPERM:
            return True
        return False


def _auto_started_idle_shell(state: dict) -> bool:
    """自动续轮建立的空壳回合：从未完成任何 Life 且零有效发现。

    post 轮询对这类回合的既定收敛语义是 ``auto_lifecycle_zero_findings``
    （post_phase 的非常驻收敛分支）；孤儿裁决复用同一判据，避免把
    「宿主尚未派发 worker 就失去属主」的空壳回合记成挖掘事故。
    """
    if not state.get("polling_round_auto_started"):
        return False
    if int(state.get("lifecycle_cycles_completed", 0) or 0) != 0:
        return False
    return int(state.get("lifecycle_credited_total", 0) or 0) == 0


def _supervisor_terminal_awaiting_settle(state: dict) -> bool:
    """Supervisor 权威终态已落盘、post 尚未消费时的免孤儿豁免。

    Supervisor ``finish()``（coverage 未完成绝不产生 COMPLETE）与 COMPLETE
    后的 reset_budget 禁令共同保证：status=COMPLETE（或 INCOMPLETE/FAILED
    且 completed_at 已写）是**不可复活的终态**。此时挖掘工作已合法结束，
    仅因宿主 MCP 通道退出导致 runtime 无人续约；把它裁决成 aborted 会
    让 post 的 runtime_active 门永久拒绝结算。这里只承认终态，不刷新
    心跳、不放宽 post 门禁——身份不匹配或文件异常一律回退 abort 路径。
    """
    path = SUPERVISOR_STATE
    if not isinstance(path, Path) or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    status = payload.get("status")
    terminal = status == "COMPLETE" or (
        status in {"INCOMPLETE", "FAILED"} and payload.get("completed_at") is not None
    )
    if not terminal:
        return False
    session_id = state.get("session_id")
    round_id = state.get("round_id") or state.get("lifecycle_id")
    if not session_id or payload.get("session_id") != session_id:
        return False
    if not round_id or payload.get("round_id") != round_id:
        return False
    return True


def _reconcile_orphaned_state_locked(state: dict, now: float | None = None) -> bool:

    """若当前为 active 状态但对应 PID 进程已死亡（或心跳严重超时），自动裁决为 aborted。"""
    if not state.get("active"):
        return False
    pid = state.get("pid")
    heartbeat_epoch = (
        state.get("heartbeat_epoch")
        or state.get("segment_started_epoch")
        or state.get("started_epoch")
    )
    current = time.time() if now is None else now
    heartbeat_stalled = (
        heartbeat_epoch is not None
        and is_number(heartbeat_epoch)
        and (current - float(heartbeat_epoch)) > HEARTBEAT_TIMEOUT_SECONDS
    )
    is_dead = False
    ownership_lost = False
    if pid is not None and isinstance(pid, int) and not isinstance(pid, bool):
        if not _is_pid_alive(pid):
            if heartbeat_stalled or heartbeat_epoch is None:
                is_dead = True
            else:
                # 认领属主（宿主 MCP server 等短生命周期通道）死亡但会话
                # 心跳仍新鲜：只代表通道退出，不代表挖掘进程崩溃。清除
                # 所有权即可；把通道死亡连坐成 Run 中止，会让宿主会话的
                # 开关误杀后台无人值守 Run（真实事故：auto 续轮的 Round
                # 被已退出 MCP 通道的 pid 拖成 aborted）。
                ownership_lost = True
    elif heartbeat_stalled:
        is_dead = True

    if ownership_lost:
        state.update({"pid": None, "host_pid": None})
        append_runtime_event(
            state, "ownership_released", current,
            "claimed owner process died; heartbeat still fresh",
        )
        # DOC-204 L7：取消回传结构化——"进程死但心跳新鲜"是宿主取消 agent
        # 的引擎侧信号；落 supervisor 权威链 owner_cancelled（D27 修复钩子）。
        try:
            from core.launch_phases.supervisor_bridge import get_supervisor as _bridge_supervisor

            _bridge_supervisor().record_lifecycle_block(
                operation="lifecycle", gate="owner_cancelled",
                reason="宿主取消 agent：owner 进程死亡而心跳仍新鲜（D27 取消回传）",
            )
        except Exception:  # noqa: BLE001 - 回传失败不阻断 ownership 释放
            pass
        return True

    if not is_dead:
        return False

    if _supervisor_terminal_awaiting_settle(state):
        # Supervisor 已经合法收尾，但 post 尚未消费其结算副作用。这里不续期
        # heartbeat，也不伪造存活；只把权威终态交给 post，避免先被孤儿裁决
        # 写成 aborted 后永久堵死结算。
        return False

    if _auto_started_idle_shell(state):
        # 空壳回合（自动续轮、零 Life、零有效发现）失去属主按收敛处理，
        # 复用 post 轮询的 auto_lifecycle_zero_findings 语义：一次落
        # polling_stop_reason 与 lifecycle_stop_reason 两个键，
        # 且不计入 sessions_aborted。
        _accrue_running_segment(state, current)
        elapsed = float(state.get("session_elapsed_seconds", 0.0))
        state.update({
            "state": "completed",
            "active": False,
            "pid": None,
            "host_pid": None,
            "paused_at": None,
            "segment_started_epoch": None,
            "last_session_elapsed_seconds": round(elapsed, 3),
            "sessions_completed": int(state.get("sessions_completed", 0)) + 1,
            "polling_stop_reason": "auto_lifecycle_zero_findings",
            "lifecycle_stop_reason": "auto_lifecycle_zero_findings",
        })
        append_runtime_event(
            state, "orphaned_session_reconciled", current,
            "auto-started idle shell converged (zero findings)",
        )
        return True

    _accrue_running_segment(state, current)
    elapsed = float(state.get("session_elapsed_seconds", 0.0))
    life_run = {
        "run_id": state.get("run_id"),
        "session_id": state.get("session_id"),
        "round_id": state.get("round_id") or state.get("lifecycle_id"),
        "life_no": state.get("life_no"),
        "outcome": "aborted",
        "started_at": state.get("started_at"),
        "finished_at": iso_time(current),
        "elapsed_seconds": round(elapsed, 3),
        "round_at_start": state.get("round_at_start"),
        "round_at_finish": _current_round(),
        "reason": "process terminated unexpectedly (orphaned)",
    }
    if not isinstance(state.get("life_runs"), list):
        state["life_runs"] = []
    state["life_runs"].append(life_run)
    state.update({
        "state": "aborted",
        "active": False,
        "pid": None,
        "host_pid": None,
        "paused_at": None,
        "segment_started_epoch": None,
        "last_session_elapsed_seconds": round(elapsed, 3),
        "sessions_aborted": int(state.get("sessions_aborted", 0)) + 1,
    })
    append_runtime_event(state, "orphaned_session_reconciled", current, "process died")
    return True


def load_runtime(*, reconcile: bool = True) -> dict:
    """读取并校验计时状态；损坏时优先从 .bak 恢复，拒绝静默归零。

    LOG-01：本函数**自身不写盘**。孤儿状态的裁决写入收敛到
    :func:`_reconcile_runtime_locked`，它在运行状态锁内**重新读取**后再写。
    旧实现直接用调用方手上（可能已陈旧）的快照调 ``save_runtime``，会把另一个
    进程刚写入的 running 心跳整体覆盖成 aborted —— 状态回退。
    """
    _ensure_project_runtime_paths()
    state = _read_runtime_state()
    if not reconcile or not state.get("active"):
        return state
    return _reconcile_runtime_locked()


def _reconcile_runtime_locked() -> dict:
    """在运行状态锁内重新读取并裁决孤儿状态。

    拿不到锁时退化为纯只读返回：宁可本轮不裁决，也不做无锁写入。
    """
    try:
        with runtime_lock():
            state = _read_runtime_state()
            if state.get("active") and _reconcile_orphaned_state_locked(state):
                if RUNTIME_FILE.parent.is_dir():
                    try:
                        save_runtime(state)
                    except OSError:
                        pass
            return state
    except (TimeoutError, OSError):
        return _read_runtime_state()


def _read_runtime_state() -> dict:
    """纯只读解析 + 校验运行计时文件（不裁决、不写盘）。"""
    if RUNTIME_FILE.is_symlink():
        raise ValueError(f"运行计时文件不能是符号链接: {RUNTIME_FILE}")
    bak_path = RUNTIME_FILE.with_name(RUNTIME_FILE.name + ".bak")
    if not RUNTIME_FILE.is_file():
        if bak_path.is_file() and not bak_path.is_symlink():
            recovered = None
            try:
                bak_content = bak_path.read_text(encoding="utf-8").strip()
                if bak_content:
                    recovered = json.loads(bak_content)
            except (OSError, json.JSONDecodeError):
                recovered = None
            if isinstance(recovered, dict):
                _require_current_runtime_schema(recovered, bak_path)
                try:
                    import shutil
                    shutil.copy2(bak_path, RUNTIME_FILE)
                except OSError:
                    pass
        if not RUNTIME_FILE.is_file():
            supervisor_backup = SUPERVISOR_STATE.with_name(SUPERVISOR_STATE.name + ".bak")
            if SUPERVISOR_STATE.exists() or supervisor_backup.exists():
                raise ValueError(
                    f"活动生命周期运行计时文件缺失，拒绝回退为空闲状态: {RUNTIME_FILE}; "
                    "请先执行 bugx doctor --repair 或从备份恢复"
                )
            # 缺文件回退的默认 runtime 同样注入 polling_id：新建 Session 尚未
            # 写 runtime 时，结算/报告路径也必须解析到真实 polling 目录。
            return _inject_runtime_polling_id(_default_runtime())
    try:
        raw_text = RUNTIME_FILE.read_text(encoding="utf-8").strip()
        if not raw_text:
            raise ValueError("运行计时文件为空（0 字节）")
        state = json.loads(raw_text)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        recovered = None
        if bak_path.is_file() and not bak_path.is_symlink():
            try:
                bak_content = bak_path.read_text(encoding="utf-8").strip()
                if bak_content:
                    candidate = json.loads(bak_content)
                    if isinstance(candidate, dict):
                        _require_current_runtime_schema(candidate, bak_path)
                        recovered = candidate
                        try:
                            import shutil
                            shutil.copy2(bak_path, RUNTIME_FILE)
                        except OSError:
                            pass
            except (OSError, json.JSONDecodeError):
                recovered = None
        if recovered is not None:
            state = recovered
        else:
            raise ValueError(f"运行计时文件无法解析: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError("运行计时文件根节点必须是对象")
    _require_current_runtime_schema(state, RUNTIME_FILE)
    if state.get("state") not in RUNTIME_STATES:
        raise ValueError("运行计时文件 state 无效")
    if not isinstance(state.get("active"), bool):
        raise ValueError("运行计时文件 active 必须是布尔值")
    if state["active"] != (state["state"] in {"running", "paused"}):
        raise ValueError("运行计时文件 active 与 state 不一致")
    for key in ("session_elapsed_seconds", "last_session_elapsed_seconds",
                "total_elapsed_seconds"):
        if not is_number(state.get(key)) or state[key] < 0:
            raise ValueError(f"运行计时文件 {key} 无效")
    if not isinstance(state.get("sessions_completed"), int) \
            or isinstance(state["sessions_completed"], bool) \
            or state["sessions_completed"] < 0:
        raise ValueError("运行计时文件 sessions_completed 无效")
    if not isinstance(state.get("sessions_aborted"), int) \
            or isinstance(state["sessions_aborted"], bool) \
            or state["sessions_aborted"] < 0:
        raise ValueError("运行计时文件 sessions_aborted 无效")
    if not isinstance(state.get("sessions"), list):
        raise ValueError("运行计时文件 sessions 必须是列表")
    if not isinstance(state.get("life_runs"), list):
        raise ValueError("运行计时文件 life_runs 必须是列表")
    if not isinstance(state.get("events"), list):
        raise ValueError("运行计时文件 events 必须是列表")
    policy = state.get("polling_policy")
    if policy is not None and policy not in POLLING_POLICIES:
        raise ValueError("运行计时文件 polling_policy 无效")
    commit_policy = state.get("commit_policy")
    if commit_policy is not None and commit_policy not in COMMIT_POLICIES:
        raise ValueError("运行计时文件 commit_policy 无效")
    review_mode = state.get("review_mode")
    if review_mode is not None and review_mode not in REVIEW_MODES:
        raise ValueError("运行计时文件 review_mode 无效")
    if not isinstance(state.get("review_mode_deferred"), bool):
        raise ValueError("运行计时文件 review_mode_deferred 无效")
    if not isinstance(state.get("autonomous_mode"), bool):
        raise ValueError("运行计时文件 autonomous_mode 无效")
    host = state.get("host")
    if host is not None and (
        not isinstance(host, str) or not host.strip() or len(host.strip()) > 64
    ):
        raise ValueError("运行计时文件 host 无效")
    if not isinstance(state.get("execute"), bool):
        raise ValueError("运行计时文件 execute 无效")
    for key in ("workers", "worker_capacity"):
        value = state.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"运行计时文件 {key} 无效")
    subagent_type = state.get("subagent_type")
    if not isinstance(subagent_type, str) or not subagent_type.strip():
        raise ValueError("运行计时文件 subagent_type 无效")
    stagger_seconds = state.get("stagger_seconds")
    if (
        not isinstance(stagger_seconds, (int, float))
        or isinstance(stagger_seconds, bool)
        or not math.isfinite(float(stagger_seconds))
        or stagger_seconds < 0
    ):
        raise ValueError("运行计时文件 stagger_seconds 无效")
    provider = state.get("provider")
    if provider is not None and (
        not isinstance(provider, str) or not provider.strip()
    ):
        raise ValueError("运行计时文件 provider 无效")
    policy_hash = state.get("autonomous_policy_hash")
    if policy_hash is not None and (
        not isinstance(policy_hash, str) or len(policy_hash) != 64
    ):
        raise ValueError("运行计时文件 autonomous_policy_hash 无效")
    if not isinstance(state.get("polling_round_auto_started"), bool):
        raise ValueError("运行计时文件 polling_round_auto_started 无效")
    if state.get("polling_last_round") is not None \
            and not isinstance(state.get("polling_last_round"), dict):
        raise ValueError("运行计时文件 polling_last_round 无效")
    if state.get("polling_stop_reason") is not None \
            and not isinstance(state.get("polling_stop_reason"), str):
        raise ValueError("运行计时文件 polling_stop_reason 无效")
    if not isinstance(state.get("lifecycle_no"), int) \
            or isinstance(state.get("lifecycle_no"), bool) \
            or state["lifecycle_no"] < 0:
        raise ValueError("运行计时文件 lifecycle_no 无效")
    if state.get("lifecycle_id") is not None \
            and not isinstance(state.get("lifecycle_id"), str):
        raise ValueError("运行计时文件 lifecycle_id 无效")
    if state.get("lifecycle_started_epoch") is not None \
            and not is_number(state.get("lifecycle_started_epoch")):
        raise ValueError("运行计时文件 lifecycle_started_epoch 无效")
    if not isinstance(state.get("lifecycle_auto_started"), bool):
        raise ValueError("运行计时文件 lifecycle_auto_started 无效")
    if not isinstance(state.get("lifecycle_terminal"), bool):
        raise ValueError("运行计时文件 lifecycle_terminal 无效")
    for key in ("lifecycle_cycles_completed", "lifecycle_findings_total",
                "lifecycle_credited_total", "lifecycle_fraud_total"):
        if not isinstance(state.get(key), int) \
                or isinstance(state.get(key), bool) or state[key] < 0:
            raise ValueError(f"运行计时文件 {key} 无效")
    if state.get("lifecycle_last_life") is not None \
            and (not isinstance(state.get("lifecycle_last_life"), int)
                 or isinstance(state.get("lifecycle_last_life"), bool)):
        raise ValueError("运行计时文件 lifecycle_last_life 无效")
    if state.get("lifecycle_last_report") is not None \
            and not isinstance(state.get("lifecycle_last_report"), str):
        raise ValueError("运行计时文件 lifecycle_last_report 无效")
    if not isinstance(state.get("lifecycle_no_progress_cycles"), int) \
            or isinstance(state.get("lifecycle_no_progress_cycles"), bool) \
            or state["lifecycle_no_progress_cycles"] < 0:
        raise ValueError("运行计时文件 lifecycle_no_progress_cycles 无效")
    if state.get("lifecycle_stop_reason") is not None \
            and not isinstance(state.get("lifecycle_stop_reason"), str):
        raise ValueError("运行计时文件 lifecycle_stop_reason 无效")
    if not isinstance(state.get("lifecycle_history"), list):
        raise ValueError("运行计时文件 lifecycle_history 必须是列表")
    lesson_ids = state.get("lifecycle_lesson_ids")
    if not isinstance(lesson_ids, list) or any(
            not isinstance(item, str) or not item for item in lesson_ids):
        raise ValueError("运行计时文件 lifecycle_lesson_ids 无效")
    if len(lesson_ids) != len(set(lesson_ids)):
        raise ValueError("运行计时文件 lifecycle_lesson_ids 不得重复")
    for key in ("lifecycle_incident_ids", "lesson_recent_signatures", "realized_fix_incidents"):
        values = state.get(key)
        if not isinstance(values, list) or any(
                not isinstance(item, str) or not item for item in values):
            raise ValueError(f"运行计时文件 {key} 无效")
        if len(values) != len(set(values)):
            raise ValueError(f"运行计时文件 {key} 不得重复")
    lesson_context = state.get("lesson_context")
    if lesson_context is not None and not isinstance(lesson_context, dict):
        raise ValueError("运行计时文件 lesson_context 无效")
    if state["active"] and not is_number(state.get("started_epoch")):
        raise ValueError("活动计时缺少 started_epoch")
    if state["active"] and not isinstance(state.get("run_id"), str):
        raise ValueError("活动计时缺少 run_id")
    if state["state"] == "running" and not is_number(state.get("segment_started_epoch")):
        raise ValueError("运行中计时缺少 segment_started_epoch")
    if state["active"] and not is_number(state.get("heartbeat_epoch")):
        raise ValueError("活动计时缺少 heartbeat_epoch")
    if state.get("polling_id") is not None \
            and not isinstance(state.get("polling_id"), str):
        raise ValueError("运行计时文件 polling_id 无效")
    return _inject_runtime_polling_id(state)

def _inject_runtime_polling_id(state: dict) -> dict:
    """从布局权威 current.json 注入 polling_id（Session 标识）。

    运行计时文件历史上从不写 polling_id，结算/报告路径只能盲猜
    polling-000001，第二个 Session 起会解析到错误目录。current.json
    是 Session/Round 活动指针的唯一权威；已有值（同生命周期持久化
    产物）优先，避免旧 runtime 被新 Session 指针劫持。
    """
    if "polling_id" in state:
        return state
    if bugx_paths is None:
        return state
    if not uses_project_layout():
        # 自包含引擎副本布局没有 ``current.json`` 指针，Session 维度只能由
        # 运行态自身承载。缺省时**就地铸造**一个稳定 id（同一 lifecycle 恒定），
        # 让写入端与读取端指向同一目录。旧行为留空 ⇒ 双方各自落到
        # ``polling-unknown`` 之外的私有推导，findings.json 成为孤儿，
        # 表现为 "Finding 快照校验失败" 与 "finding_count=1，结算结果=0"。
        return state
    try:
        polling_id = bugx_paths.read_current(resolve_project_root()).get("polling_id")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return state
    if isinstance(polling_id, str) and polling_id:
        state["polling_id"] = polling_id
    return state

@contextmanager
def runtime_lock(timeout_seconds: float = 3.0):
    """用 flock 串行化短写操作；持有进程死亡时由内核自动释放（DOC-031 锁统一）。

    锁文件本身跨进程常驻（flock 语义下 unlink 会导致锁竞争窗口），不再
    写 pid/token，也不再做 age 抢占——陈旧锁由内核在进程退出时回收。

    LOG-03：打开锁文件时加 ``O_NOFOLLOW`` 并预检符号链接。若 ``runtime.lock``
    被替换成符号链接，``flock`` 会作用到链接指向的任意文件，互斥被绕过。

    同进程重入：flock 对同一文件的**不同 fd** 仍互斥，因此 ``runtime_lock``
    内再调用 ``load_runtime(reconcile=True)`` 会自锁死；这里用**线程内**深度
    计数放行同线程重入（外层已持有锁，语义等价）。跨线程不共享该计数，因此
    线程 B 仍必须真正竞争 flock。
    """
    _ensure_project_runtime_paths()
    depth = _runtime_lock_depth()
    if depth > 0:
        _set_runtime_lock_depth(depth + 1)
        try:
            yield
        finally:
            _set_runtime_lock_depth(depth)
        return
    lock_parent = RUNTIME_LOCK_FILE.parent
    bugx_paths.assert_no_symlink_components(lock_parent)
    bugx_paths.ensure_dir(lock_parent, mode=0o700)
    bugx_paths.assert_no_symlink_components(lock_parent)
    if RUNTIME_LOCK_FILE.is_symlink():
        raise ValueError(f"运行状态锁不能是符号链接: {RUNTIME_LOCK_FILE}")
    deadline = time.monotonic() + timeout_seconds
    fd = os.open(
        RUNTIME_LOCK_FILE,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows fallback
            fcntl = None  # type: ignore[assignment]
        try:
            import msvcrt
        except ImportError:  # pragma: no cover - POSIX fallback
            msvcrt = None  # type: ignore[assignment]
        if fcntl is not None:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "运行状态正被另一进程更新，请稍后重试"
                            f"（{RUNTIME_LOCK_FILE.name} flock 等待超过 {timeout_seconds:.0f}s）"
                        )
                    time.sleep(0.05)
        elif msvcrt is not None:
            # ``msvcrt.locking`` locks a byte range and requires the file to
            # contain that byte.  Keep the lock file resident and use the
            # non-blocking primitive so Windows follows the same bounded wait
            # contract as the POSIX flock path.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            lock_mode = getattr(msvcrt, "LK_NBLCK", msvcrt.LK_LOCK)
            while True:
                try:
                    msvcrt.locking(fd, lock_mode, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "运行状态正被另一进程更新，请稍后重试"
                            f"（{RUNTIME_LOCK_FILE.name} msvcrt 等待超过 {timeout_seconds:.0f}s）"
                        )
                    time.sleep(0.05)
        else:
            raise OSError("当前平台缺少 fcntl 和 msvcrt，无法安全锁定运行状态")
        _set_runtime_lock_depth(depth + 1)
        try:
            yield
        finally:
            _set_runtime_lock_depth(depth)
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            elif msvcrt is not None:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
    finally:
        os.close(fd)

def save_runtime(state: dict) -> None:
    """原子写入计时状态，避免中断产生半个 JSON，并保持 .bak 容灾副本。"""
    fd, temp_path = tempfile.mkstemp(
        dir=str(RUNTIME_FILE.parent), prefix=".runtime-", suffix=".tmp",
    )
    backup_temp_path: str | None = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        bak_path = RUNTIME_FILE.with_name(RUNTIME_FILE.name + ".bak")
        if RUNTIME_FILE.is_file() and not RUNTIME_FILE.is_symlink():
            backup_fd, backup_temp_path = tempfile.mkstemp(
                dir=str(RUNTIME_FILE.parent), prefix=".runtime-bak-", suffix=".tmp",
            )
            os.close(backup_fd)
            import shutil
            shutil.copy2(RUNTIME_FILE, backup_temp_path)
            os.replace(backup_temp_path, bak_path)
            backup_temp_path = None
        os.replace(temp_path, RUNTIME_FILE)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        if backup_temp_path is not None:
            try:
                os.unlink(backup_temp_path)
            except OSError:
                pass
        raise

def iso_time(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )

_iso_time = iso_time
_load_runtime = load_runtime

def _current_round() -> int | None:
    life = baseline_json()
    value = life.get("round") if life else None
    return value if isinstance(value, int) else None

def _round_or_default(new_lifecycle: bool) -> int | None:
    """新生命周期首个 Round 的兜底轮号。

    auto 续轮先由 ``start_runtime`` 切换 current.json、life.json 随后才
    初始化；此刻 ``_current_round()`` 读不到基线返回 None。运行时缺
    ``round`` 会让 findings 运行绑定按 fail-closed 拒绝结算，因此全新
    生命周期按协议初始态取 1；非新生命周期（恢复/续开）保持原语义。
    """
    round_value = _current_round()
    if round_value is None and new_lifecycle:
        return 1
    return round_value

def diagnostic_context() -> dict[str, object]:
    """Attach runtime identity to diagnostic events without making logs authoritative."""
    try:
        state = load_runtime()
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    fields: dict[str, object] = {}
    for key in ("run_id", "lifecycle_id", "worker_id", "finding_id", "incident_id"):
        value = state.get(key)
        if isinstance(value, str) and value:
            fields[key] = value
    round_value = _current_round()
    if round_value is not None:
        fields["round"] = round_value
    return fields

def append_runtime_event(state: dict, event: str, now: float,
                          message: str | None = None) -> None:
    item = {"at": iso_time(now), "epoch": now, "event": event}
    if message:
        item["message"] = message
    state["events"] = [*state.get("events", []), item][-200:]

def _accrue_running_segment(state: dict, now: float) -> None:
    if state.get("state") != "running":
        return
    started = float(state["segment_started_epoch"])
    state["session_elapsed_seconds"] = round(
        float(state["session_elapsed_seconds"]) + max(0.0, now - started), 3
    )
    state["segment_started_epoch"] = now

def reset_segment_timer(now: float | None = None) -> None:
    """更新会话起算点，避免将人类交互和环境探针的开销计入 Worker 任务执行时间。"""
    with runtime_lock():
        state = load_runtime()
        if not state.get("active") or state.get("state") != "running":
            return
        current = time.time() if now is None else now
        state["started_at"] = iso_time(current)
        state["started_epoch"] = current
        state["segment_started_epoch"] = current
        state["heartbeat_at"] = iso_time(current)
        state["heartbeat_epoch"] = current
        save_runtime(state)

def start_runtime(now: float | None = None,
                   auto_started: bool | None = None,
                   new_lifecycle: bool | None = None,
                   new_session: bool | None = None) -> tuple[dict, bool]:
    global RUNTIME_STARTED_THIS_PROCESS
    with runtime_lock():
        state = load_runtime()
        if state["active"]:
            current = time.time() if now is None else now
            if _reconcile_orphaned_state_locked(state, current):
                save_runtime(state)
            else:
                return state, False
        current = time.time() if now is None else now
        if auto_started is None:
            auto_started = False
        if new_lifecycle is None:
            new_lifecycle = state.get("lifecycle_id") is None
        if new_session is None:
            # 自动续 Round 仍属于同一次 IDE Session；只有首次启动或新建
            # polling 容器时才建立新的 Session 身份。
            new_session = bool(new_lifecycle and not state.get("session_id"))
        session_state = {}
        if uses_project_layout():
            try:
                session_state = json.loads(
                    bugx_paths.require(resolve_project_root()).session_state.read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, ValueError, json.JSONDecodeError):
                session_state = {}
        if new_lifecycle:
            if uses_project_layout():
                active = bugx_paths.require(resolve_project_root())
                current_layout = bugx_paths.read_current(resolve_project_root())
                if new_session or (state.get("lifecycle_id") is None and not state.get("lifecycle_no")):
                    # The first runtime session adopts the lifecycle created by
                    # `bugx init`; a new Session likewise adopts the fresh
                    # polling/lifecycle tree created by create_session().
                    lifecycle_no = int(current_layout["lifecycle_no"])
                    lifecycle_id = str(current_layout["lifecycle_id"])
                else:
                    lifecycle_no = int(state.get("lifecycle_no", 0)) + 1
                    lifecycle_id = f"lifecycle-{lifecycle_no:06d}"
                    bugx_paths.activate_lifecycle(
                        resolve_project_root(), lifecycle_no, lifecycle_id,
                        polling_id=str(current_layout["polling_id"]),
                        polling_no=int(current_layout.get("polling_no", 1)),
                    )
                    # activate_lifecycle 已把 current.json 切到新生命周期；
                    # 路径全局（RUNTIME_FILE/SUPERVISOR_STATE 等）必须立即
                    # 重绑，否则新 Round 的 runtime 会写进旧生命周期的
                    # state 目录——指针交叉与 runtime 缺失的根源。
                    refresh_runtime_paths()
            else:
                lifecycle_no = int(state.get("lifecycle_no", 0)) + 1
                lifecycle_id = uuid.uuid4().hex
            lifecycle_fields = {
                "lifecycle_no": lifecycle_no,
                "lifecycle_id": lifecycle_id,
                "lifecycle_auto_started": bool(auto_started),
                "lifecycle_started_at": iso_time(current),
                "lifecycle_started_epoch": current,
                "lifecycle_cycles_completed": 0,
                "lifecycle_findings_total": 0,
                "lifecycle_credited_total": 0,
                "lifecycle_fraud_total": 0,
                "lifecycle_last_life": None,
                "lifecycle_terminal": False,
                "lifecycle_last_report": None,
                "lifecycle_no_progress_cycles": 0,
                "lifecycle_stop_reason": None,
                "lifecycle_lesson_ids": [],
                # Life 幂等键只属于当前 Round；新 Round
                # 即使复用相同的 verify_life.round，也必须重新计数。
                "polling_last_round": None,
            }
        else:
            lifecycle_fields = {
                "lifecycle_terminal": False,
                "lifecycle_last_report": None,
            }
        # A new Session gets a fresh intermediate-record space.  The project
        # knowledge/coverage layers remain shared, but session-local run,
        # round, life and consumption history must never leak from the
        # completed/aborted predecessor.
        session_fields = {
            "startup_id": (
                uuid.uuid4().hex if new_session
                else state.get("startup_id") or uuid.uuid4().hex
            ),
            "startup_action": (
                None if new_session else state.get("startup_action")
            ),
            "life_runs": [] if new_session else list(state.get("life_runs", [])),
            "lifecycle_history": (
                [] if new_session else list(state.get("lifecycle_history", []))
            ),
            "lifecycle_incident_ids": (
                [] if new_session else list(state.get("lifecycle_incident_ids", []))
            ),
            "lesson_recent_signatures": (
                [] if new_session else list(state.get("lesson_recent_signatures", []))
            ),
            "realized_fix_incidents": (
                [] if new_session else list(state.get("realized_fix_incidents", []))
            ),
            "lesson_context": None if new_session else state.get("lesson_context"),
        }
        # DOC-RECOVERY-BINDING：恢复会话（非 new_session）时，若同 lifecycle 的
        # Supervisor 账本仍在（挖掘证据在链上），必须**收养**其 run_id——否则
        # runtime 与 supervisor 身份分裂，post 的 findings 绑定校验永久
        # findings_binding_mismatch（实测：恢复 pre 换新 run_id 后 9 条 confirmed
        # 无法结算）。仅 new_session 才铸造新身份。
        adopted_run_id = None
        if not new_session:
            try:
                sup_path = (
                    pollings_root()
                    / str(state.get("polling_id"))
                    / "lifecycles" / str(state.get("lifecycle_id"))
                    / "state" / "supervisor.json"
                )
                if sup_path.is_file():
                    sup_run = json.loads(sup_path.read_text(encoding="utf-8")).get("run_id")
                    if isinstance(sup_run, str) and sup_run.strip():
                        adopted_run_id = sup_run
            except Exception:
                adopted_run_id = None
        state.update({
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "run_id": adopted_run_id or uuid.uuid4().hex,
            "state": "running",
            "active": True,
            "pid": (
                int(os.environ["BUGX_PROCESS_PID"])
                if os.environ.get("BUGX_PROCESS_PID", "").isdigit()
                else None
            ),
            "host_pid": (
                int(os.environ["BUGX_HOST_PID"])
                if os.environ.get("BUGX_HOST_PID", "").isdigit()
                else None
            ),
            "started_at": iso_time(current),
            "started_epoch": current,
            "segment_started_epoch": current,
            "paused_at": None,
            "heartbeat_at": iso_time(current),
            "heartbeat_epoch": current,
            "round_at_start": _round_or_default(bool(new_lifecycle)),
            # 当前 Round 必须持久化：findings 运行绑定
            # （finding_settlement._expected_findings_binding / _auto_build_findings_json）
            # 与 lifecycle_reporter / post_dry_run 都读 runtime["round"]，且缺该字段时
            # 结算按 fail-closed 拒绝（tests.test_finding_settlement
            # ::test_auto_build_rejects_missing_runtime_round_before_writing）。
            # 这里与 round_at_start 同源（本 Session 所属 Round），保证
            # 「本轮已结算后再 post」仍能通过绑定校验；新生命周期在 life
            # 初始化前取初始态 1（见 _round_or_default）。
            "round": _round_or_default(bool(new_lifecycle)),
            "session_elapsed_seconds": 0.0,
            "last_session_elapsed_seconds": 0.0,
            "events": [],
            "polling_round_auto_started": (
                bool(auto_started) if new_lifecycle
                else bool(state.get("polling_round_auto_started"))
            ),
            "polling_stop_reason": None,
            "startup_id": state.get("startup_id") or uuid.uuid4().hex,
            "session_id": (
                str(bugx_paths.read_current(resolve_project_root())["polling_id"])
                if new_session and uses_project_layout()
                else state.get("session_id") or str(
                    state.get("polling_id") or "session-unknown"
                )
            ),
            "session_started_at": (
                iso_time(current) if new_session
                else state.get("session_started_at") or iso_time(current)
            ),
            "session_started_epoch": (
                current if new_session
                else state.get("session_started_epoch") or current
            ),
            "total_elapsed_seconds": float(
                session_state.get("total_elapsed_seconds", state.get("total_elapsed_seconds", 0.0))
            ),
            "sessions_completed": int(
                session_state.get("sessions_completed", state.get("sessions_completed", 0))
            ),
            "sessions_aborted": int(
                session_state.get("sessions_aborted", state.get("sessions_aborted", 0))
            ),
            "sessions": list(session_state.get("sessions", state.get("sessions", []))),
            "round_id": lifecycle_fields.get("lifecycle_id") or state.get("lifecycle_id"),
            "life_no": _current_round(),
            "startup_result": "resume" if not new_lifecycle else "new_session",
            "session_state": "running",
            "session_closed_at": None,
            "final_round_id": None,
            **session_fields,
            **lifecycle_fields,
        })
        append_runtime_event(state, "started", current)
        save_runtime(state)
    RUNTIME_STARTED_THIS_PROCESS = True
    set_runtime_started_this_process(True)
    return state, True

def runtime_summary(now: float | None = None) -> dict:
    state = load_runtime()
    current = time.time() if now is None else now
    live = float(state["session_elapsed_seconds"])
    if state["state"] == "running":
        live += max(0.0, current - float(state["segment_started_epoch"]))
    heartbeat_age = None
    if is_number(state.get("heartbeat_epoch")):
        heartbeat_age = max(0.0, current - float(state["heartbeat_epoch"]))
    stalled = state["state"] == "running" and (
        heartbeat_age is None or heartbeat_age > HEARTBEAT_TIMEOUT_SECONDS
    )
    health = "stalled" if stalled else state["state"]
    return {
        **state,
        "health": health,
        "stalled": stalled,
        "heartbeat_age_seconds": round(heartbeat_age, 3)
        if heartbeat_age is not None else None,
        "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT_SECONDS,
        "current_elapsed_seconds": round(live, 3),
        "effective_total_seconds": round(float(state["total_elapsed_seconds"]) + live, 3),
        "current_round": _current_round(),
    }

def close_runtime(outcome: str, now: float | None = None,
                   reason: str | None = None) -> tuple[dict, bool]:
    with runtime_lock():
        state = load_runtime()
        if not state["active"]:
            return runtime_summary(now), False
        current = time.time() if now is None else now
        _accrue_running_segment(state, current)
        elapsed = float(state["session_elapsed_seconds"])
        life_run = {
            "run_id": state.get("run_id"),
            "session_id": state.get("session_id"),
            "round_id": state.get("round_id") or state.get("lifecycle_id"),
            "life_no": state.get("life_no"),
            "outcome": outcome,
            "started_at": state["started_at"],
            "finished_at": iso_time(current),
            "elapsed_seconds": round(elapsed, 3),
            "round_at_start": state.get("round_at_start"),
            "round_at_finish": _current_round(),
        }
        if reason:
            life_run["reason"] = reason
        state.update({
            "state": outcome,
            "active": False,
            "pid": None,
            "host_pid": None,
            "segment_started_epoch": None,
            "paused_at": None,
            "heartbeat_at": iso_time(current),
            "heartbeat_epoch": current,
            "total_elapsed_seconds": round(
                float(state["total_elapsed_seconds"]) + elapsed, 3
            ),
            "last_session_elapsed_seconds": round(elapsed, 3),
            "session_elapsed_seconds": 0.0,
            # A runtime file is rotated for every Life/ Round.  Do not count
            # that segment as a completed Session; Session counters are
            # committed exactly once by _close_session below.
            "life_runs": [*state.get("life_runs", []), life_run][-100:],
        })
        append_runtime_event(state, outcome, current, reason)
        save_runtime(state)
        if uses_project_layout():
            # Keep the cumulative Session timer in the Session container so a
            # new Round can start with the same total instead of resetting it.
            try:
                bugx_paths.update_session_state(
                    resolve_project_root(),
                    {"total_elapsed_seconds": state["total_elapsed_seconds"]},
                )
            except (OSError, ValueError, RuntimeError):
                # The authoritative Life checkpoint is already durable.  A
                # missing Session index is surfaced on the next startup rather
                # than silently changing the completed Life result.
                pass
    # TODO is a derived operator view, not the authority.  Refresh it only
    # after the runtime checkpoint is durable so an aborted/completed segment
    # cannot leave the visible checklist claiming that work is still running.
    if outcome == "aborted":
        refresh_audit_todo(state, "aborted", reason)
    elif outcome == "completed":
        refresh_audit_todo(state, "life-settled")
    return runtime_summary(current), True

def finish_runtime(now: float | None = None) -> dict:
    runtime, _ = close_runtime("completed", now)
    return runtime

def pause_runtime(now: float | None = None) -> tuple[dict, bool]:
    with runtime_lock():
        state = load_runtime()
        if state["state"] != "running":
            return state, False
        current = time.time() if now is None else now
        _accrue_running_segment(state, current)
        state.update({
            "state": "paused",
            "active": True,
            "segment_started_epoch": None,
            "paused_at": iso_time(current),
            "heartbeat_at": iso_time(current),
            "heartbeat_epoch": current,
        })
        append_runtime_event(state, "paused", current)
        save_runtime(state)
    return state, True

def resume_runtime(now: float | None = None) -> tuple[dict, bool]:
    with runtime_lock():
        state = load_runtime()
        if state["state"] != "paused":
            return state, False
        current = time.time() if now is None else now
        state.update({
            "state": "running",
            "active": True,
            "segment_started_epoch": current,
            "paused_at": None,
            "heartbeat_at": iso_time(current),
            "heartbeat_epoch": current,
        })
        append_runtime_event(state, "resumed", current)
        save_runtime(state)
    return state, True

def heartbeat_runtime(now: float | None = None,
                       owner_pid: int | None = None) -> tuple[dict, bool]:
    if owner_pid is not None and (
            not isinstance(owner_pid, int)
            or isinstance(owner_pid, bool)
            or owner_pid <= 0
    ):
        raise ValueError("heartbeat owner PID 无效")
    with runtime_lock():
        # A host MCP channel can outlive the short-lived pre process that
        # originally wrote ``pid``.  Let that channel claim ownership before
        # the normal orphan reconciliation runs; ordinary reads keep the
        # existing fail-closed dead-process behavior.
        state = load_runtime(reconcile=owner_pid is None)
        if owner_pid is not None and state.get("state") == "running":
            state["pid"] = owner_pid
            state["host_pid"] = owner_pid
            if _reconcile_orphaned_state_locked(state):
                save_runtime(state)
                return state, False
        if state["state"] != "running":
            return state, False
        current = time.time() if now is None else now
        state.update({
            "heartbeat_at": iso_time(current),
            "heartbeat_epoch": current,
        })
        append_runtime_event(state, "heartbeat", current)
        save_runtime(state)
    return state, True

def print_runtime(summary: dict) -> None:
    labels = {
        "idle": "未运行", "running": "运行中", "paused": "已暂停",
        "completed": "已完成", "aborted": "已中止", "stalled": "疑似卡住",
    }
    status_text = labels.get(summary["health"], summary["health"])
    print(f"运行计时: {status_text}")
    if summary.get("run_id"):
        print(f"  Run ID: {summary['run_id']}")
    if summary["active"]:
        print(f"  开始时间: {summary['started_at']}")
        print(f"  当前耗时: {format_duration(summary['current_elapsed_seconds'])}")
        if summary.get("heartbeat_at"):
            print(f"  最近心跳: {summary['heartbeat_at']}")
        if summary["stalled"]:
            print(f"  ⚠ 超过 {summary['heartbeat_timeout_seconds']} 秒没有心跳")
    print(f"  累计耗时: {format_duration(summary['effective_total_seconds'])}")
    print(f"  完成会话: {summary['sessions_completed']}")
    print(f"  中止会话: {summary['sessions_aborted']}")
    policy = summary.get("polling_policy")
    print(f"  Round 续轮策略: {policy or '待询问（启动前必须确认）'}")
    print(f"  提交策略: {summary.get('commit_policy') or '待询问（log-only 自动不适用）'}")
    if summary.get("commit_status"):
        print(f"  最近提交: {summary['commit_status']}"
              + (f"（{summary.get('commit_ref')}）" if summary.get("commit_ref") else ""))
    review_mode = summary.get("review_mode")
    print(
        "  审查方式: "
        f"{review_mode or '待询问（手动 / MCP 自动 / 下次再说）'}"
        + ("（本轮手动，下次重问）"
           if summary.get("review_mode_deferred") else "")
    )
    if summary.get("lifecycle_no"):
        lifecycle_state = "已完成" if summary.get("lifecycle_terminal") else "进行中"
        print(
            f"  Round: #{summary['lifecycle_no']} {lifecycle_state}；"
            f"Life {summary.get('lifecycle_cycles_completed', 0)} 个；"
            f"累计有效 Bug {summary.get('lifecycle_credited_total', 0)} 条；"
            f"连续无有效进展 {summary.get('lifecycle_no_progress_cycles', 0)}"
            f"/{LIFECYCLE_MAX_NO_PROGRESS_CYCLES} 次"
        )
        if summary.get("lifecycle_stop_reason"):
            print(f"  Round 停止原因: {summary['lifecycle_stop_reason']}")
        if summary.get("lifecycle_last_report"):
            print(f"  Round 报告: {summary['lifecycle_last_report']}")
    if summary.get("polling_stop_reason"):
        print(f"  轮询终止原因: {summary['polling_stop_reason']}")
    if summary.get("startup_result") or summary.get("startup_observation") or summary.get("startup_intent"):
        res = summary.get("startup_result") or "未记录"
        obs = summary.get("startup_observation") or "未记录"
        intent = summary.get("startup_intent") or "未记录"
        print(f"  启动判定: 结果={res} 观察={obs} 意图={intent}")
    if summary.get("current_round") is not None:
        print(f"  当前轮次: {summary['current_round']}")

_BASELINE_JSON_CACHE: dict[tuple[str, int, int], object] = {}
_BASELINE_JSON_CACHE_LIMIT = 32


def _clear_baseline_json_cache() -> None:
    """清空 ``baseline_json()`` 的 stat 记忆化缓存（测试或显式失效时使用）。"""
    _BASELINE_JSON_CACHE.clear()


def baseline_json() -> dict | None:
    """读取当前 life 状态作为外部基线（供 pre 输出，防漏洞 2）。

    PERF-05：``pre()`` 内本函数被调用多次，每次都会 ``bugx_paths.require``
    再读取并解析 ``life.json``。按 ``(路径, st_mtime_ns, st_size)`` 记忆化：
    任何写盘（``run_verify_life("snapshot")`` / ``repair`` / 新 Session）都会改变
    mtime 或 size，使缓存**自然失效**，因此写操作之后绝不会返回陈旧基线。
    返回浅拷贝，避免调用方原地修改污染缓存。
    """
    if uses_project_layout():
        try:
            life = bugx_paths.require(resolve_project_root()).life
        except (OSError, ValueError, RuntimeError):
            return None
    else:
        # 外部基线 → life 的真实位置与 ``_path_values_for_root`` **同源**。
        # 旧实现写 ``AGENT_DIR / "bugx-life.json"``：``AGENT_DIR`` 是**可执行
        # 引擎目录**，而 pre 写入的寿命状态在项目根，于是独立运行
        # （``python core/launch_bugx.py pre``）读到空基线 ⇒
        # ``runtime life_no`` 为 None ⇒ 串行授权身份校验 fail-closed，
        # 而进程内车道（绑定了完整路径表）却通过——两条车道再次分叉。
        life = Path(LIFE_FILE)
    try:
        stat = life.stat()
    except OSError:
        return None
    key = (str(life), stat.st_mtime_ns, stat.st_size)
    if key in _BASELINE_JSON_CACHE:
        cached = _BASELINE_JSON_CACHE[key]
        return dict(cached) if isinstance(cached, dict) else cached  # type: ignore[return-value]
    if not life.is_file():
        return None
    try:
        payload = json.loads(life.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if payload is None:
        return None
    if len(_BASELINE_JSON_CACHE) >= _BASELINE_JSON_CACHE_LIMIT:
        _BASELINE_JSON_CACHE.clear()
    _BASELINE_JSON_CACHE[key] = payload
    return dict(payload) if isinstance(payload, dict) else payload  # type: ignore[return-value]

def set_polling_policy(policy: str) -> tuple[dict, bool]:
    if policy not in POLLING_POLICIES:
        raise ValueError(f"轮询策略必须是 auto 或 manual，收到: {policy}")
    with runtime_lock():
        state = load_runtime()
        changed = state.get("polling_policy") != policy
        state["polling_policy"] = policy
        if changed:
            append_runtime_event(state, "polling-policy", time.time(), policy)
        save_runtime(state)
    return state, changed

def set_review_mode(mode: str, deferred: bool = False) -> tuple[dict, bool]:
    """持久化审查执行面；deferred 表示本轮手动、下次启动重新询问。"""
    if mode not in REVIEW_MODES:
        raise ValueError(f"审查方式必须是 manual 或 mcp-auto，收到: {mode}")
    with runtime_lock():
        state = load_runtime()
        changed = (
            state.get("review_mode") != mode
            or state.get("review_mode_deferred", False) != deferred
        )
        state["review_mode"] = mode
        state["review_mode_deferred"] = deferred
        if changed:
            detail = f"{mode};next_start={'ask' if deferred else 'reuse'}"
            append_runtime_event(state, "review-mode", time.time(), detail)
        save_runtime(state)
    return state, changed

def _record_startup_action(action: str) -> tuple[dict, bool]:
    """Persist the explicit stale-MCP startup decision and its audit event."""
    if action != "start":
        raise ValueError(f"启动动作只能记录 start，收到: {action}")
    with runtime_lock():
        state = load_runtime()
        changed = state.get("startup_action") != action
        state["startup_action"] = action
        # ``start`` is a one-session manual fallback, not a lasting review
        # preference.  Keep both facts in one atomic runtime update.
        state["review_mode"] = "manual"
        state["review_mode_deferred"] = True
        if changed:
            append_runtime_event(state, "startup-action", time.time(), action)
        save_runtime(state)
    return state, changed

def set_commit_policy(policy: str) -> tuple[dict, bool]:
    """持久化当前 Session 的提交边界；不把手动提交当作异常。"""
    if policy not in COMMIT_POLICIES:
        raise ValueError("提交策略必须是 life、round、manual 或 not_applicable")
    with runtime_lock():
        state = load_runtime()
        changed = state.get("commit_policy") != policy
        state["commit_policy"] = policy
        if changed:
            append_runtime_event(state, "commit-policy", time.time(), policy)
        save_runtime(state)
    return state, changed

def set_polling_stop(reason: str) -> dict:
    """持久化停止原因，同时落 ``polling_stop_reason`` 与 ``lifecycle_stop_reason``。

    两个键描述的是同一个事实（轮询/生命周期为何停止）。历史上只有
    post_phase 的 ``--final`` 分支写后者，而自治链路被宿主明令禁用
    ``--final``，于是 ``lifecycle_stop_reason`` 永远是 None，
    ``supervisor_host._autonomous_stop_reason`` 永远读不到
    ``coverage_target_completed``，成功收敛被误报成 blocked / exit 1。
    在唯一写入口一次落两个键，同类断链不再复现。
    """
    with runtime_lock():
        state = load_runtime()
        state["polling_stop_reason"] = reason
        state["lifecycle_stop_reason"] = reason
        append_runtime_event(state, "polling-stopped", time.time(), reason)
        save_runtime(state)
    return state
