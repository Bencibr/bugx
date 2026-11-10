#!/usr/bin/env python3
"""launch_phases/supervisor_bridge.py - Supervisor 探活、MCP 状态门禁、探针健康检查与降级处理。"""

from __future__ import annotations

from core.launch_phases.startup_interaction import ask_interaction, answered_values

import json
import os
import shlex
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from core.launch_phases import common as _common
from core.launch_phases.common import (
    AGENT_DIR,
    MANIFEST_TABLE_HEADER,
    RUNTIME_STARTED_THIS_PROCESS,
    is_runtime_started_this_process,
    supervisor_state_path,
    TOOLS_KB,
    PREP_RECORD,
    PREP_VALIDATE,
    RESTART_REQUIRED,
    PROBE_JSON_CACHE,
    SUPERVISOR_MCP_HARD_CHANNELS,
    resolve_project_root,
    uses_project_layout,
    coverage_manifest_path,
    child_environment,
    run_child,
    run_bounded,
    bugx_paths,
)
import hashlib
import shutil
import uuid
from core.host_runtime import host_registry
from core.repair import self_heal
from core.runtime.tool_install_targets import PROBE_REPAIR_TARGETS
from core.launch_phases.startup_gates import (
    detect_startup_environment,
    emit_startup_gate,
    ask_interaction,
    answered_value,
    answered_values,
)

try:
    from core.supervisor import audit_supervisor
    supervisor_type = audit_supervisor.Supervisor
    budget_type = audit_supervisor.RunBudget
except ImportError:
    supervisor_type = None
    budget_type = None





# ---------------------------------------------------------------------------
# DOC-256: 架构解耦导出：Coverage 清单与降级探针治理
# ---------------------------------------------------------------------------
from core.launch_phases.supervisor_coverage import (
    carry_over_previous_manifest,
    append_manifest_rows,
    coverage_bootstrap_runtime_lock,
    runtime_coverage_recovery_mode,
    state_path_exists_or_symlink,
    bootstrap_coverage_manifest,
    bootstrap_coverage_manifest_locked,
)
_carry_over_previous_manifest = carry_over_previous_manifest
_append_manifest_rows = append_manifest_rows
_coverage_bootstrap_runtime_lock = coverage_bootstrap_runtime_lock
_runtime_coverage_recovery_mode = runtime_coverage_recovery_mode
_state_path_exists_or_symlink = state_path_exists_or_symlink
_bootstrap_coverage_manifest = bootstrap_coverage_manifest
_bootstrap_coverage_manifest_locked = bootstrap_coverage_manifest_locked

from core.launch_phases.supervisor_degraded import (
    degraded_actions,
    DegradedDecision,
    degraded_decision,
    degraded_item_not_applicable,
    degraded_item_advisory,
    degraded_item_transient,
    degraded_item_critical,
    degraded_exemption_hit,
    degraded_decision_for_policy,
    degraded_receipt_for_report,
    report_self_heal,
    emit_self_heal_evidence,
    preflight_probe_id,
    auto_provision_accelerators,
    autonomous_degraded_choices,
    watchlist_error_report,
    safe_watchlist_incident_id,
    check_fix_regressions,
    report_degraded_probes,
    record_degraded_choices,
    degraded_install,
    prompt_degraded_choices,
    tooling_exemptions,
    degraded_gate,
    DEGRADED_HEALED,
    DEGRADED_NOT_APPLICABLE,
    DEGRADED_EXEMPTED,
    DEGRADED_BLOCKED,
    DEGRADED_DEFERRED,
    DEGRADED_TRANSIENT,
    DEGRADED_PASSING_STATUSES,
)
_DEGRADED_HEALED = DEGRADED_HEALED
_DEGRADED_NOT_APPLICABLE = DEGRADED_NOT_APPLICABLE
_DEGRADED_EXEMPTED = DEGRADED_EXEMPTED
_DEGRADED_BLOCKED = DEGRADED_BLOCKED
_DEGRADED_DEFERRED = DEGRADED_DEFERRED
_DEGRADED_TRANSIENT = DEGRADED_TRANSIENT
_DEGRADED_PASSING_STATUSES = DEGRADED_PASSING_STATUSES
_degraded_actions = degraded_actions
_DegradedDecision = DegradedDecision
_degraded_decision = degraded_decision
_degraded_item_not_applicable = degraded_item_not_applicable
_degraded_item_advisory = degraded_item_advisory
_degraded_item_transient = degraded_item_transient
_degraded_item_critical = degraded_item_critical
_degraded_exemption_hit = degraded_exemption_hit
_degraded_decision_for_policy = degraded_decision_for_policy
_degraded_receipt_for_report = degraded_receipt_for_report
_report_self_heal = report_self_heal
_emit_self_heal_evidence = emit_self_heal_evidence
_preflight_probe_id = preflight_probe_id
_watchlist_error_report = watchlist_error_report
_safe_watchlist_incident_id = safe_watchlist_incident_id
_record_degraded_choices = record_degraded_choices
_degraded_install = degraded_install
_prompt_degraded_choices = prompt_degraded_choices
_tooling_exemptions = tooling_exemptions

def get_supervisor() -> object:
    """Load the external scheduler lazily so legacy imports stay lightweight."""
    try:
        from core.supervisor import audit_supervisor
    except (ImportError, OSError) as exc:
        raise ValueError(f"并发 Supervisor 无法加载: {exc}") from exc
    budget_type = audit_supervisor.RunBudget
    supervisor_type = audit_supervisor.Supervisor
    return supervisor_type(
        resolve_project_root(),
        budget=budget_type(),
        state_path=supervisor_state_path(),
    )

def supervisor_units() -> list[str]:
    """Read stable internal/business unit IDs for the current project."""
    from core.coverage import module_coverage
    manifest = coverage_manifest_path()
    _bootstrap_coverage_manifest(manifest)
    if not manifest.is_file():
        raise ValueError("覆盖清单没有可调度的最小单元（清单缺失且项目无可发现源码）")
    try:
        rows, errors = module_coverage.parse_manifest(manifest)
    except (OSError, ValueError) as exc:
        raise ValueError(f"覆盖单元无法加载: {exc}") from exc
    if errors:
        raise ValueError("覆盖清单无效，不能建立并发任务队列: " + "; ".join(errors))
    units = sorted({module_coverage.coverage_unit_id(row) for row in rows})
    if not units:
        raise ValueError("覆盖清单没有可调度的最小单元")
    return units


_AUTO_REBIND_REASON = "auto-rebind current workspace target on drift"


def _supervisor_recovery_snapshot(supervisor: object) -> dict[str, object]:
    """Return one authoritative Supervisor snapshot plus its machine action."""
    from core.mcp_handlers.base import next_action, recovery_chain

    snapshot = supervisor.status_with_gate()
    if not isinstance(snapshot, Mapping):
        raise ValueError("Supervisor status_with_gate 返回了无效快照")
    state = snapshot.get("state")
    gate = snapshot.get("parallel_gate")
    if not isinstance(state, Mapping) or not isinstance(gate, Mapping):
        raise ValueError("Supervisor 快照缺少 state/parallel_gate")
    state_payload = dict(state)
    gate_payload = dict(gate)
    return {
        "state": state_payload,
        "parallel_gate": gate_payload,
        "status": state_payload.get("status"),
        "coverage_complete": state_payload.get("coverage_complete"),
        "confirmation_gate": state_payload.get("confirmation_gate", {}),
        "active_workers": gate_payload.get("active_workers", []),
        "next_action": next_action(state_payload, gate_payload),
        "recovery_chain": recovery_chain(state_payload, gate_payload),
    }


def _coverage_manifest_exists(root: Path | None) -> bool:
    """Check only the active project's trusted coverage manifest."""
    try:
        from core.coverage.coverage_manifest import default_manifest_for_root

        return default_manifest_for_root(root or resolve_project_root()).is_file()
    except (ImportError, OSError, RuntimeError, ValueError):
        return False


def _host_reset_budget_arguments(
    state: Mapping[str, object],
    *,
    budget_override: Mapping[str, object] | None = None,
    allow_serial: bool | None = None,
    serial_reason: str | None = None,
) -> dict[str, object]:
    """Build the explicit host MCP arguments for a budget reset."""
    current = state.get("budget")
    values = dict(current) if isinstance(current, Mapping) else {}
    values.update(dict(budget_override or {}))
    if allow_serial is None:
        allow_serial = not bool(values.get("require_parallel", True))
        serial_reason = values.get("serial_reason")
    arguments: dict[str, object] = {
        "caller_role": "host",
        "force": True,
        "renew_wall_clock": True,
        "allow_serial": allow_serial,
    }
    if allow_serial and isinstance(serial_reason, str) and serial_reason.strip():
        arguments["serial_reason"] = serial_reason
    for key in (
        "max_rounds", "max_wall_seconds", "max_round_wall_seconds",
        "max_tool_calls", "max_cost_units", "max_concurrency",
    ):
        if key in values:
            arguments[key] = values[key]
    return arguments


def _host_mcp_call(root: Path, tool: str, arguments: Mapping[str, object]) -> dict[str, object]:
    """Call one host-only Supervisor MCP tool in-process."""
    from core.skills.bugx_mcp_server import BugxMcpService

    return BugxMcpService(root, role="host").call(tool, arguments)


def auto_recover_supervisor(
    root: Path | None = None,
    *,
    supervisor: object | None = None,
    snapshot: Mapping[str, object] | None = None,
    coverage_manifest_exists: bool | None = None,
    budget_override: Mapping[str, object] | None = None,
    allow_serial: bool | None = None,
    serial_reason: str | None = None,
) -> dict[str, object]:
    """Execute the host recovery chain and return a fresh status snapshot.

    This is the in-process equivalent of the host-only MCP calls.  The
    Supervisor remains the authority, so each mutation writes its normal
    ``target_rebound``/``budget_reset`` event before the next action is read.
    """
    supervisor = supervisor or get_supervisor()
    current = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    if not current.get("parallel_gate") or "next_action" not in current:
        current = _supervisor_recovery_snapshot(supervisor)

    gate = current.get("parallel_gate")
    gate = gate if isinstance(gate, Mapping) else {}
    status = str(current.get("status") or "")
    next_action = str(current.get("next_action") or "")
    drift = bool(gate.get("target_drift")) or next_action == "REBUILD_PLAN"
    manifest_exists = (
        _coverage_manifest_exists(root)
        if coverage_manifest_exists is None else bool(coverage_manifest_exists)
    )
    events: list[dict[str, object]] = []

    if drift:
        if not manifest_exists:
            detail = "target drift requires an existing coverage manifest"
            print(f"[supervisor] ✗ 自动重绑未执行: {detail}")
            return {
                "status": "blocked",
                "reason": "coverage_manifest_missing",
                "detail": detail,
                "snapshot": current,
                "events": events,
            }
        try:
            _host_mcp_call(
                root or resolve_project_root(),
                "bugx_supervisor_rebind_target",
                {
                    "caller_role": "host",
                    "reason": _AUTO_REBIND_REASON,
                },
            )
        except Exception as exc:  # noqa: BLE001 - recovery must fail closed
            detail = f"自动重绑目标失败: {exc}"
            print(f"[supervisor] ✗ {detail}")
            return {
                "status": "blocked",
                "reason": "supervisor_rebind_failed",
                "detail": detail,
                "snapshot": current,
                "events": events,
            }
        event = {
            "type": "host_auto_rebind",
            "action": "REBUILD_PLAN",
            "tool": "bugx_supervisor_rebind_target",
            "caller_role": "host",
            "reason": _AUTO_REBIND_REASON,
        }
        events.append(event)
        print("[supervisor] ✓ 已自动重绑当前工作区目标，继续读取 recovery_chain")
        current = _supervisor_recovery_snapshot(supervisor)
        status = str(current.get("status") or "")
        next_action = str(current.get("next_action") or "")

    budget_due = status == "BUDGET_EXHAUSTED" or next_action == "RESET_BUDGET"
    if budget_due:
        try:
            arguments = _host_reset_budget_arguments(
                current.get("state") if isinstance(current.get("state"), Mapping) else current,
                budget_override=budget_override,
                allow_serial=allow_serial,
                serial_reason=serial_reason,
            )
            _host_mcp_call(
                root or resolve_project_root(),
                "bugx_supervisor_reset_budget",
                arguments,
            )
        except Exception as exc:  # noqa: BLE001 - recovery must fail closed
            detail = f"自动预算续期失败: {exc}"
            print(f"[supervisor] ✗ {detail}")
            return {
                "status": "blocked",
                "reason": "supervisor_budget_reset_failed",
                "detail": detail,
                "snapshot": current,
                "events": events,
            }
        event = {
            "type": "host_auto_budget_reset",
            "action": "RESET_BUDGET",
            "tool": "bugx_supervisor_reset_budget",
            "caller_role": "host",
            "force": True,
            "renew_wall_clock": True,
        }
        events.append(event)
        print("[supervisor] ✓ 已自动开启新 budget epoch 并续期墙钟，继续读取 recovery_chain")
        current = _supervisor_recovery_snapshot(supervisor)

    final_gate = current.get("parallel_gate")
    final_gate = final_gate if isinstance(final_gate, Mapping) else {}
    final_status = str(current.get("status") or "")
    final_action = str(current.get("next_action") or "")
    if (
        final_gate.get("target_drift")
        or final_action in {"REBUILD_PLAN", "RESET_BUDGET"}
        or final_status == "BUDGET_EXHAUSTED"
    ):
        detail = "自动 recovery 后 Supervisor 仍未解除 drift/budget 门禁"
        print(f"[supervisor] ✗ {detail}")
        return {
            "status": "blocked",
            "reason": "supervisor_recovery_incomplete",
            "detail": detail,
            "snapshot": current,
            "events": events,
        }
    if events and final_status != "RUNNING":
        detail = f"自动 recovery 已执行，但 Supervisor 未回到 RUNNING（当前为 {final_status or 'unknown'}）"
        print(f"[supervisor] ✗ {detail}")
        return {
            "status": "blocked",
            "reason": "supervisor_recovery_incomplete",
            "detail": detail,
            "snapshot": current,
            "events": events,
        }
    return {
        "status": "recovered" if events else "noop",
        "snapshot": current,
        "events": events,
    }


def _archive_supervisor_state() -> None:
    """Archive the previous run before a new big lifecycle starts."""
    supervisor_state = supervisor_state_path()
    archive_dir = AGENT_DIR / "audit-supervisor-archive"
    if uses_project_layout():
        archive_dir = bugx_paths.require(resolve_project_root()).lifecycle_root / "recovery" / "supervisor"
    archive_dir.mkdir(parents=True, exist_ok=True)
    # DOC-228：归档上一轮快照时必须**一并**归档 `.revision` 栅栏。
    # `StateStore.ensure_new_run_allowed()` 对「栅栏存在但 state/backup 均不存在」
    # 是 fail-closed（RECOVERY_REQUIRED: revision fence exists without a state
    # snapshot）。只搬走快照而留下栅栏，会让自动续轮的 `ensure_supervisor(new_run=True)`
    # 永久失败（实测 3 个 test_auto_* 用例命中「自动轮询无法启动下一轮」）。
    for source in (
        supervisor_state,
        supervisor_state.with_name(supervisor_state.name + ".bak"),
        supervisor_state.with_name(supervisor_state.name + ".revision"),
    ):
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_symlink():
            raise ValueError(f"拒绝归档符号链接 Supervisor 状态: {source}")
        target = archive_dir / f"{source.name}.{int(time.time())}.{uuid.uuid4().hex[:8]}"
        os.replace(source, target)
    lock = supervisor_state.with_name(supervisor_state.name + ".lock")
    if lock.exists() or lock.is_symlink():
        if lock.is_symlink():
            raise ValueError(f"拒绝清理符号链接 Supervisor 锁: {lock}")
        lock.unlink()
    evidence = _common.COVERAGE_EVIDENCE
    if evidence.exists() or evidence.is_symlink():
        if evidence.is_symlink():
            raise ValueError(f"拒绝归档符号链接覆盖证据: {evidence}")
        os.replace(evidence, archive_dir / f"{evidence.name}.{int(time.time())}.{uuid.uuid4().hex[:8]}")

def _expected_supervisor_run_id(lifecycle_id: str | None,
                                session_id: str | None) -> str | None:
    """PE-20261005-003: Supervisor run_id 用 polling 限定形式，消除跨 polling 撞键。

    session_id 即 polling_id（lifecycle_id 每个 polling 都从 000001 重编号，
    裸形式 `lifecycle-{lifecycle_id}` 跨轮必然碰撞）；session_id 缺失时
    （无 polling 上下文的遗留单轮布局）回退裸形式。
    """
    if not lifecycle_id:
        return None
    if session_id:
        return f"lifecycle-{session_id}-{lifecycle_id}"
    return f"lifecycle-{lifecycle_id}"

def ensure_supervisor(allow_serial: bool = False,
                       serial_reason: str | None = None,
                       lifecycle_id: str | None = None,
                       new_run: bool = False,
                       session_id: str | None = None,
                       life_id: str | None = None,
                       life_kind: str = "mine",
                       plan_hash: str | None = None,
                       budget_override: Mapping[str, object] | None = None) -> dict:
    """Create/resume the Worker controller bound to one Project/Session/Round/Life."""
    if allow_serial and not isinstance(serial_reason, str) or \
            allow_serial and not serial_reason.strip():
        raise ValueError("允许串行时必须提供非空 --serial-reason")
    # [BEGIN DEV_ONLY: FAST_VERIFICATION_BUDGET]
    effective_budget_override = dict(budget_override or {})
    try:
        from core.runtime.interaction import try_get_preset_runtime_config
    except ImportError:
        try:
            from core.runtime.interaction import try_get_preset_runtime_config
        except ImportError:
            try_get_preset_runtime_config = None
    if try_get_preset_runtime_config is not None:
        preset_cfg = try_get_preset_runtime_config(resolve_project_root() if uses_project_layout() else None)
        if preset_cfg.get("round_duration_seconds") is not None:
            effective_budget_override["max_round_wall_seconds"] = int(preset_cfg["round_duration_seconds"])
        if preset_cfg.get("max_rounds") is not None:
            effective_budget_override["max_rounds"] = int(preset_cfg["max_rounds"])
        if preset_cfg.get("max_total_wall_seconds") is not None:
            effective_budget_override["max_wall_seconds"] = int(preset_cfg["max_total_wall_seconds"])
        if preset_cfg.get("max_worker_concurrency") is not None:
            effective_budget_override["max_concurrency"] = int(preset_cfg["max_worker_concurrency"])
    budget_override = effective_budget_override
    # [END DEV_ONLY: FAST_VERIFICATION_BUDGET]
    expected_run_id = _expected_supervisor_run_id(lifecycle_id, session_id)
    project_id = None
    if uses_project_layout():
        project_id = hashlib.sha256(
            str(resolve_project_root().resolve()).encode("utf-8")
        ).hexdigest()[:16]
    if new_run:
        _archive_supervisor_state()
    coverage_units = supervisor_units()
    supervisor_state = supervisor_state_path()
    if not supervisor_state.is_file():
        if allow_serial:
            budget = None
            try:
                from core.supervisor import audit_supervisor
                budget_values = audit_supervisor.RunBudget().to_dict()
                budget_values.update(dict(budget_override or {}))
                budget = audit_supervisor.RunBudget.from_dict({
                    **budget_values,
                    "require_parallel": False, "serial_reason": serial_reason,
                    "require_confirmation": True,
                })
                supervisor = audit_supervisor.Supervisor(
                    resolve_project_root(), budget=budget, state_path=supervisor_state,
                )
                state = supervisor.start(
                    coverage_units=coverage_units, run_id=expected_run_id,
                    project_id=project_id, session_id=session_id,
                    round_id=lifecycle_id, life_id=life_id, plan_hash=plan_hash,
                    life_kind=life_kind,
                )
            except (ImportError, OSError, ValueError) as exc:
                raise ValueError(f"无法建立串行例外 Supervisor: {exc}") from exc
        else:
            try:
                from core.supervisor import audit_supervisor
                budget_values = audit_supervisor.RunBudget().to_dict()
                budget_values.update(dict(budget_override or {}))
                budget = audit_supervisor.RunBudget.from_dict({
                    **budget_values,
                    "require_confirmation": True,
                })
                supervisor = audit_supervisor.Supervisor(
                    resolve_project_root(), budget=budget, state_path=supervisor_state,
                )
                state = supervisor.start(
                    coverage_units=coverage_units, run_id=expected_run_id,
                    project_id=project_id, session_id=session_id,
                    round_id=lifecycle_id, life_id=life_id, plan_hash=plan_hash,
                    life_kind=life_kind,
                )
            except (ImportError, OSError, ValueError) as exc:
                raise ValueError(f"无法建立并发 Supervisor: {exc}") from exc
    else:
        supervisor = get_supervisor()
        state = supervisor.status()
        if expected_run_id and state.run_id != expected_run_id:
            # PE-20261005-003: 仅接受「同轮升级兼容」——引擎升级前以旧裸形式
            # 创建、且 session_id 一致的在途 run；跨 polling 植入的旧 state
            # （session 不一致）一律拒绝，不得静默收养。
            legacy_run_id = f"lifecycle-{lifecycle_id}" if lifecycle_id else None
            same_session_legacy = bool(
                legacy_run_id
                and state.run_id == legacy_run_id
                and session_id
                and getattr(state, "session_id", None) == session_id
            )
            if not same_session_legacy:
                raise ValueError(
                    "Supervisor run 未绑定当前 Round: "
                    f"expected={expected_run_id} actual={state.run_id}"
                )
        bindings = {
            "project_id": project_id,
            "session_id": session_id,
            "round_id": lifecycle_id,
            "life_id": life_id,
        }
        for name, expected in bindings.items():
            if expected is not None and getattr(state, name, None) != expected:
                if name == "life_id" and state.active_claims:
                    raise ValueError(
                        f"Supervisor run 未绑定当前 {name}: "
                        f"expected={expected} actual={getattr(state, name, None)}"
                    )
                try:
                    state = supervisor.bind_context(**bindings)
                except (AttributeError, OSError, ValueError, RuntimeError) as exc:
                    raise ValueError(f"Supervisor 上下文绑定失败: {exc}") from exc
                break
        recovery = auto_recover_supervisor(
            resolve_project_root(),
            supervisor=supervisor,
            coverage_manifest_exists=_coverage_manifest_exists(resolve_project_root()),
            budget_override=budget_override,
            allow_serial=allow_serial,
            serial_reason=serial_reason,
        )
        if recovery.get("status") == "blocked":
            raise ValueError(str(recovery.get("detail") or "Supervisor 自动恢复失败"))
        state = supervisor.status()
        if state.status.value in {"COMPLETE", "FAILED"}:
            # 终态 run（例如上一轮 finish 早于 post、或 post 中途崩溃后由
            # 宿主手动收尾）无法承载新 runtime 会话；此前直接 raise 会让
            # pre 无法重建 runtime、post 又要求活动 runtime，形成状态死锁，
            # 只能弃数据开新 polling。这里按提示语义"开启新审计 run"执行：
            # 归档终态 state（证据保留在 lifecycle recovery 目录）后走
            # new_run 重建路径。INCOMPLETE 不在此列——post 仍可对其结算。
            _archive_supervisor_state()
            return ensure_supervisor(
                allow_serial, serial_reason, lifecycle_id=lifecycle_id,
                new_run=True, session_id=session_id, life_id=life_id,
                life_kind=life_kind, plan_hash=plan_hash,
                budget_override=budget_override,
            )
    return state.to_dict() if hasattr(state, "to_dict") else dict(state)

def check_supervisor_gate() -> tuple[bool, dict]:
    supervisor = get_supervisor()
    result = supervisor.parallel_gate()
    return bool(result.get("satisfied")), result

def advance_supervisor_round() -> dict:
    supervisor = get_supervisor()
    state = supervisor.next_round()
    return state.to_dict()

def run_tools_kb() -> int:
    """启动前拒绝使用过期/无日期工具知识。优先进程内直接调用，失败或异常回退子进程。"""
    if not TOOLS_KB.is_file():
        print("[launch_bugx] ✗ 缺少 tools_kb.py，无法验证工具知识有效期")
        return 1
    try:
        parent_dir = str(TOOLS_KB.parent.resolve())
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from core.skills import tools_kb
        # 目录路径只有 ``tools_kb.KB_FILE`` 一个真源。此前这里重推了两级
        # ``parent``/``parent.parent`` 候选，目录迁进 ``core/planning/`` 后全部落空，
        # 门禁于是拿到 ``path=None`` 再回落到同一条失效路径，表现为"知识库缺失"。
        kb_file = getattr(tools_kb, "KB_FILE", None)
        func = getattr(tools_kb, "_run_check", None)
        if callable(func):
            return int(func(path=kb_file if kb_file and Path(kb_file).is_file() else None))
        main_func = getattr(tools_kb, "main", None)
        if callable(main_func):
            return int(main_func(["check"]))
    except Exception:
        pass
    return run_child(
        [sys.executable, str(TOOLS_KB), "check"], cwd=resolve_project_root(),
    )

def run_prep_validate() -> int:
    """结束前确认开工准备有可追溯记录。优先进程内直接调用，失败或异常回退子进程。"""
    if not PREP_VALIDATE.is_file():
        print("[launch_bugx] ✗ 缺少 prep_validate.py，拒绝通过准备门禁")
        return 1
    record = Path(PREP_RECORD)
    if uses_project_layout():
        record = bugx_paths.require(resolve_project_root()).prep_record
    try:
        parent_dir = str(PREP_VALIDATE.parent.resolve())
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from core.runtime import prep_validate
        func = getattr(prep_validate, "_validate_direct", None)
        if callable(func):
            return int(func(record=record, root=resolve_project_root()))
        main_func = getattr(prep_validate, "main", None)
        if callable(main_func):
            return int(main_func(["--root", str(resolve_project_root()), "--record", str(record)]))
    except Exception:
        pass
    return run_child(
        [sys.executable, str(PREP_VALIDATE),
         "--root", str(resolve_project_root()),
         "--record", str(record)], cwd=resolve_project_root(),
    )

def run_preflight_probes(
    *, refresh: bool = False, include_profile: bool = False,
    project_profile_data: dict | None = None,
) -> tuple[int, list[dict]] | tuple[int, list[dict], dict]:
    """按 probe_registry 的 ``lifecycle="preflight"`` 统一驱动启动前探针。

    DES-13：启动执行的**唯一真源**是探针注册中心——``preflight_probes()``
    返回什么就执行什么。新增/下线 preflight 探针只需改注册表，不再需要
    在启动流水线里手写包装函数与路径常量。所有探针在同一个并发扇出中
    各执行一次（``run_preflight_probes`` 内部并行 + 环境画像缓存）。

    ``required`` 探针的脚本缺失时硬阻断启动；``required=False`` 的建议性
    探针（如 openwiki）缺失时静默跳过。返回 ``(退出码, 降级动作清单)``。
    """
    from core.probes import probe_registry

    root = resolve_project_root()
    descriptors = probe_registry.preflight_probes()
    # 探针脚本与 ``probe_registry`` 同目录（``run_preflight_probes`` 也按此
    # 定位）。必须用同一个目录判定缺失，否则隔离/最小安装布局下会把
    # 实际存在的探针误判为缺失而错误硬阻断。
    try:
        registry_dir = Path(probe_registry.__file__).resolve().parent
    except (AttributeError, OSError, TypeError):
        registry_dir = AGENT_DIR
    # 必须与 ``run_preflight_probes`` 用**同一个**解析器判定缺失：那条路径走
    # ``_resolve_probe_script``（会按文件名在引擎树内唯一解析），这里若直接拼
    # ``registry_dir / descriptor.script``，一旦探针脚本被归档进子包
    # （``core/report_engine/web_conformance.py``），就会把存在的脚本判成缺失，
    # 于是 ``pre`` 硬阻断，且报错指向一个"看起来该在 probes/ 下"的假路径。
    missing = [
        descriptor for descriptor in descriptors
        if descriptor.required
        and not probe_registry._resolve_probe_script(
            registry_dir, descriptor.script
        ).is_file()
    ]
    if missing:
        for descriptor in missing:
            print(
                f"[launch_bugx] ✗ 缺少 {descriptor.script}，无法完成"
                f"{descriptor.description or descriptor.name}准备"
            )
        return (1, [], {}) if include_profile else (1, [])

    profile = probe_registry.run_preflight_probes(
        root,
        parallel=True,
        refresh=refresh,
        project_profile_data=project_profile_data,
    )
    reports = profile.get("probes") or {}
    for descriptor in descriptors:
        report = reports.get(descriptor.name)
        if not isinstance(report, dict):
            continue
        print(f"  探针 {descriptor.name}: {_preflight_probe_summary(report)}")
    _sync_codebase_memory_env(profile)
    check_fix_regressions(root)
    degraded = list(profile.get("degraded_actions", []))
    return (0, degraded, profile) if include_profile else (0, degraded)


def _preflight_probe_summary(report: dict) -> str:
    """把归一化探针报告压成一行人类可读摘要（不改变探针自身输出）。"""
    status = str(report.get("status", "READY"))
    duration = report.get("duration_seconds")
    suffix = f"（{duration:.2f}s）" if isinstance(duration, (int, float)) else ""
    if status.strip().upper() == "NOT_APPLICABLE":
        # 形态不适用是一条**结论**，不是"未就绪"：后者会让人误以为环境缺能力。
        reason = str(report.get("detail") or "").strip()
        return f"{status}{suffix}" + (f"；{reason}" if reason else "")
    failed = [
        str(check.get("name") or "未命名检查")
        for check in (report.get("checks") or [])
        if isinstance(check, dict) and not check.get("ok")
    ]
    detail = f"；未就绪: {', '.join(failed[:4])}" if failed else ""
    return f"{status}{suffix}{detail}"


def _sync_codebase_memory_env(profile: dict) -> None:
    """把 codebase-memory 探针结论同步到进程环境变量。"""
    cb_probe = (profile.get("probes") or {}).get("codebase-memory")
    if isinstance(cb_probe, dict):
        os.environ["BUGX_USE_CODEBASE_MEMORY"] = (
            "1" if cb_probe.get("status") == "READY" else "0"
        )


# DES-13：启动前探针的执行真源已收敛到 probe_registry.preflight_probes()，
# 下列名称不再包含任何手写探针逻辑。它们仅为保持历史导入方
# （round_orchestrator 仍在 import 这些名字）可用而保留；一旦被真正调用，
# 立即显式报错而不是静默执行另一条路径。
def _removed_probe_wrapper(name: str):
    def _entry(*_args, **_kwargs):
        raise RuntimeError(
            f"{name} 已由 probe_registry 统一驱动（DES-13）；"
            "请改用 run_preflight_probes()"
        )
    _entry.__name__ = name
    _entry.__doc__ = f"已删除的手写探针包装（DES-13）；调用即报错。原名: {name}"
    return _entry


_run_codebase_memory_probe = _removed_probe_wrapper("_run_codebase_memory_probe")
_run_efficiency_probe = _removed_probe_wrapper("_run_efficiency_probe")
_run_sast_probe = _removed_probe_wrapper("_run_sast_probe")
_run_mobile_probe = _removed_probe_wrapper("_run_mobile_probe")
_run_openwiki_probe = _removed_probe_wrapper("_run_openwiki_probe")

def _probe_json(script: Path) -> dict | None:
    """在有界 Worker 进程组中运行探针 --json 并解析；失败返回 None。"""
    try:
        key = script.resolve()
    except (OSError, RuntimeError):
        key = script
    if key in PROBE_JSON_CACHE:
        return PROBE_JSON_CACHE[key]
    try:
        from core.supervisor.worker_execution import run_bounded
        result = run_bounded(
            [sys.executable, str(script), "check", "--root",
             str(resolve_project_root()), "--json"],
            cwd=resolve_project_root(), env=child_environment(), timeout=30,
            capture=True,
        )
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
        PROBE_JSON_CACHE[key] = None
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        PROBE_JSON_CACHE[key] = None
        return None
    from core.probes import probe_registry
    p_forms = None
    p_langs = None
    try:
        from core.runtime import project_profile
        prof = project_profile.detect_project_profile(resolve_project_root())
        p_forms = set(prof.get("forms", []))
        p_langs = {str(l).casefold() for l in prof.get("languages", [])}
    except Exception:
        pass
    value = probe_registry._normalize_probe_report(
        script.stem, payload, returncode=result.returncode, forms=p_forms, languages=p_langs,
    )
    PROBE_JSON_CACHE[key] = value
    return value


def supervisor_mcp_snapshot() -> dict[str, object] | None:
    """Read and report the selected host's MCP session capabilities."""
    if not uses_project_layout():
        return None
    try:
        from core.supervisor import supervisor_host
        root = resolve_project_root()
        adapter = supervisor_host.detect_host(root)
        status = supervisor_host.check_mcp_env(root, adapter)
        # DOC-065: 扩充宿主底层环境能力与无头运行支持检查
        try:
            status["host_env"] = supervisor_host.check_host_env(root, adapter)
            status["supports_headless"] = bool(adapter.supports_headless())
        except Exception:
            pass
    except (ImportError, OSError, ValueError, RuntimeError):
        return {
            "host": "unknown",
            "configured": [],
            "missing": ["bugx-supervisor"],
            "degraded": [],
            "critical_missing": ["bugx-supervisor"],
            "detail": "无法读取宿主 MCP 配置",
            "supports_headless": False,
        }
    return status

def _mcp_status_names(status: dict[str, object], key: str) -> set[str]:
    """Return normalized MCP names from a status field."""
    values = status.get(key, ())
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {str(value) for value in values}

def _supervisor_mcp_status() -> dict[str, object] | None:
    """Return a blocking MCP status for automatic review, if any."""
    status = supervisor_mcp_snapshot()
    if status is None:
        return None
    missing = _mcp_status_names(status, "missing") & SUPERVISOR_MCP_HARD_CHANNELS
    degraded = _mcp_status_names(status, "degraded") & SUPERVISOR_MCP_HARD_CHANNELS
    if not missing and not degraded:
        supervisor = (status.get("capabilities") or {}).get("bugx-supervisor", {})
        worker = (status.get("capabilities") or {}).get("bugx-supervisor-worker", {})
        if not (isinstance(supervisor, dict) and isinstance(worker, dict)
                and supervisor.get("loaded") and supervisor.get("callable")
                and supervisor.get("healthy") and worker.get("loaded")
                and worker.get("callable") and worker.get("healthy")):
            status["detail"] = "bugx-supervisor 当前会话尚未证明 host/worker 均已加载、可调用且健康"
            return status
        return None
    status["detail"] = (
        "bugx-supervisor host/worker MCP 未安装或未配置"
        if missing else "bugx-supervisor host/worker MCP 已配置但探活失败"
    )
    return status

def _mcp_session_is_stale(status: dict[str, object] | None) -> bool:
    """Return whether configured Supervisor MCP is not loaded in this session.

    Missing/degraded configuration has its own install/recovery gate.  This
    predicate is intentionally limited to the important middle case: the
    configuration exists, but the current host process has not loaded or
    proved the host/worker channels.  That case must never silently become a
    manual run, because the user may still expect the configured MCP path.
    """
    if not isinstance(status, dict):
        return False
    if (_mcp_status_names(status, "missing") & SUPERVISOR_MCP_HARD_CHANNELS) or \
            (_mcp_status_names(status, "degraded") & SUPERVISOR_MCP_HARD_CHANNELS):
        return False
    capabilities = status.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    supervisor = capabilities.get("bugx-supervisor")
    worker = capabilities.get("bugx-supervisor-worker")
    if not isinstance(supervisor, dict) or not isinstance(worker, dict):
        return False
    configured_names = _mcp_status_names(status, "configured")
    configured = (
        SUPERVISOR_MCP_HARD_CHANNELS <= configured_names
        or all(bool(channel.get("configured")) for channel in (supervisor, worker))
    )
    healthy = all(
        bool(channel.get(field))
        for channel in (supervisor, worker)
        for field in ("loaded", "callable", "healthy")
    )
    return configured and not healthy

def print_supervisor_mcp_snapshot(status: dict[str, object] | None) -> None:
    """Print the MCP environment result before startup decisions."""
    if not status:
        return
    capabilities = status.get("capabilities") or {}
    names = ("bugx-supervisor", "bugx-supervisor-worker", "playwright", "postmcp")
    summary = []
    for name in names:
        item = capabilities.get(name) if isinstance(capabilities, dict) else None
        summary.append(f"{name}={item.get('status', 'unknown') if isinstance(item, dict) else 'missing'}")
    if "supports_headless" in status:
        summary.append(f"headless={'yes' if status.get('supports_headless') else 'no'}")
    print("[BugX] MCP environment: " + ", ".join(summary))

def supervisor_mcp_restart_instructions(host: str, *, install_required: bool) -> str:
    """Configuration changes require a new host session and fresh MCP proof."""
    install = "先初始化项目并安装当前宿主的 Supervisor MCP；" if install_required else ""
    return (
        f"{install}重启 {host} 宿主会话后重新启动 BugX，"
        "并验证 bugx-supervisor 与 bugx-supervisor-worker 已加载且可调用。"
    )


def _install_supervisor_mcp(status: dict[str, object]) -> bool:
    """Install the host bundle after an explicit user choice.

    Installation changes host configuration but cannot hot-load the current AI
    session.  A successful install therefore returns False and keeps startup at
    the gate until the host is restarted.
    """
    cli = shutil.which("bugx")
    host = str(
        getattr(detect_startup_environment(), "host", None)
        or status.get("host")
        or "unknown"
    ).strip().casefold()
    if cli is None:
        print("[launch_bugx] ✗ 找不到 bugx CLI，无法自动安装 Supervisor MCP。")
        print("  请先安装/更新 BugX CLI，再重新进入启动引导。")
        return False
    try:
        install_cmd = [
            cli, "install", "--root", str(resolve_project_root()), "--agent", host,
            "--allow-missing", "--non-interactive",
        ]
        if os.environ.get("BUGX_FORCE_INSTALL") or os.environ.get("BUGX_GLOBAL_ENGINE_ROOT"):
            install_cmd.append("--force")
        result = subprocess.run(
            install_cmd,
            cwd=str(resolve_project_root()), capture_output=True, text=True, timeout=90,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[launch_bugx] ✗ Supervisor MCP 安装失败: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        print(f"[launch_bugx] ✗ Supervisor MCP 安装失败: {detail}")
        return False
    print("[launch_bugx] ✓ Supervisor MCP 已写入宿主配置。")
    print(supervisor_mcp_restart_instructions(host, install_required=False))
    return False

def host_entry_gate() -> int:
    """DOC-236 C7：宿主侧入口（MCP + skill）就绪性门禁。

    返回 ``0``（就绪）或 ``RESTART_REQUIRED``（需重新装配并重启宿主）。
    与 MCP 门禁同为"配置→重启→resume"周期的一部分：宿主不支持热加载，
    因此入口未就绪时必须停在**独立终态**，而不是伪装成"需要人决策"。
    """
    if not uses_project_layout():
        return 0
    try:
        from core.supervisor import supervisor_host
        status = supervisor_host.host_entry_status(resolve_project_root())
    except Exception as exc:  # noqa: BLE001 - 检查失败一律 fail-closed
        print(f"[launch_bugx] ✗ 宿主入口就绪性检查失败（{type(exc).__name__}）：{exc}")
        return RESTART_REQUIRED
    if status.get("ready"):
        return 0
    print("[launch_bugx] ✗ 需要重启宿主：宿主侧入口（MCP + skill）未就绪——"
          + str(status.get("reason") or "未知原因"))
    stale = status.get("stale_files") or []
    if stale:
        print("  受影响入口文件：" + ", ".join(str(item) for item in stale))
    print("  处置：运行 `bugx install` 重新装配宿主入口，然后**重启宿主会话**再启动 BugX。")
    return RESTART_REQUIRED


def _prompt_supervisor_mcp(
    status: dict[str, object], *, allow_manual: bool = True,
) -> str | None:
    """通过统一交互组件返回 install/later（自动模式）或 manual。"""
    options = [
        ("立即初始化/安装配置", "先初始化项目并写入当前宿主的 bugx-supervisor MCP；完成后必须重启宿主会话"),
    ]
    if allow_manual:
        options.append(("本轮手动回退", "本轮由宿主/Agent 手动推进，不使用 MCP 自动审查"))
    options.append(("下次再说", "停止本轮启动，保留当前配置不变"))
    result = ask_interaction(
        "supervisor_mcp", "未检测到 bugx-supervisor MCP；请先初始化项目并完成宿主配置", options,
        default=0,
        detail=(
            str(status.get("detail") or "bugx-supervisor MCP 缺失")
            + "。完成后必须重启宿主，再在新会话重新启动 BugX。"
        ),
    )
    value = answered_value(result)
    if value == "1":
        return "install"
    if allow_manual and value == "2":
        return "manual"
    if value == ("3" if allow_manual else "2"):
        return "later"
    return None

def supervisor_mcp_gate(*, review_mode: str | None,
                         autonomous: bool = False) -> str:
    """Require an install/manual/later decision for MCP automatic review.

    DOC-236 C7：返回值由布尔改为**三态**，以便区分"需要重启宿主"与"需要人决策"：
      * ``"ok"`` —— 可继续；
      * ``"restart_required"`` —— 配置已写入/已存在但当前宿主未加载 ⇒ 必须重启宿主；
      * ``"blocked"`` —— 需要用户/宿主决策或安装未完成。
    """
    if review_mode != "mcp-auto":
        return "ok"
    # [BEGIN DEV_ONLY: FAST_VERIFICATION_MCP_GATE]
    try:
        from core.runtime.interaction import try_get_preset_runtime_config
    except ImportError:
        try:
            from core.runtime.interaction import try_get_preset_runtime_config
        except ImportError:
            try_get_preset_runtime_config = None
    if try_get_preset_runtime_config is not None:
        preset_cfg = try_get_preset_runtime_config(resolve_project_root() if uses_project_layout() else None)
        if preset_cfg.get("supervisor_mcp") in {"manual", "skip"}:
            return "ok"
    # [END DEV_ONLY: FAST_VERIFICATION_MCP_GATE]
    status = _supervisor_mcp_status()
    if status is None:
        return "ok"
    # A configured-but-not-loaded session is handled by the final
    # ``startup_action`` gate below.  Do not present the older install/manual
    # gate here, otherwise the host can silently choose manual review and move
    # on to the polling question (the bug this gate is meant to prevent).
    if _mcp_session_is_stale(status):
        # 尝试快速 stdio 活体探活（A1 自愈机制）
        try:
            from core.supervisor import supervisor_host
            probe_result = supervisor_host.probe_builtin_mcp_session(resolve_project_root(), deadline=None)
            if not probe_result.get("error"):
                os.environ[supervisor_host.MCP_SESSION_PROOF_ENV] = json.dumps(probe_result, ensure_ascii=False)
                return "ok"
        except Exception:
            pass
        if autonomous:
            print("[launch_bugx] ✗ 需要重启宿主：Supervisor MCP 已配置但当前宿主未加载且探活未通过；"
                  "支持使用 stdio JSON-RPC 等效通道驱动，或重启宿主会话后重试。")
            return "restart_required"
        return "ok"
    if autonomous:
        # The installer is already constrained to the BugX-managed host
        # adapter and offline MCP runtime.  It returns False after writing
        # config because the current host cannot hot-load the new server.
        if not _install_supervisor_mcp(status):
            print("[launch_bugx] 自治模式已停止：Supervisor MCP 安装失败（需宿主重启后重试）。")
            return "restart_required"
        print("[launch_bugx] Supervisor MCP 配置已写入；需要宿主重新启动后才能加载。")
        return "restart_required"
    choice = _prompt_supervisor_mcp(status, allow_manual=False)
    if choice is None:
        environment = detect_startup_environment()
        host = str(status.get("host") or "当前宿主")
        emit_startup_gate(
            "supervisor_mcp",
            f"{host} 未检测到 bugx-supervisor MCP；请先初始化项目、完成宿主配置并重启后继续。",
            [
                {
                    "label": "立即初始化/安装配置",
                    "description": "先初始化项目并写入宿主配置；完成后必须重启当前宿主会话",
                    "value": "install",
                    "action": "install_supervisor_mcp",
                },
                {
                    "label": "下次再说",
                    "description": "停止本轮启动，完成初始化和重启后再重新启动 BugX",
                    "value": "later",
                    "action": "stop_startup",
                },
            ],
            environment=environment,
            detail=(
                str(status.get("detail") or "bugx-supervisor MCP 缺失")
                + "。"
                + supervisor_mcp_restart_instructions(host, install_required=True)
            ),
        )
        return "blocked"
    if choice == "install":
        _install_supervisor_mcp(status)
        print("[launch_bugx] Supervisor MCP 配置已写入；需要宿主重新启动后才能加载。")
        return "restart_required"
    if choice == "later":
        print("[launch_bugx] Supervisor MCP 安装已延后，本轮启动停止。")
        return "blocked"
    print("[launch_bugx] MCP 自动审查未取得有效初始化/重启决定，本轮启动停止。")
    return "blocked"

def recover_supervisor_budget() -> bool:
    """Apply the host recovery chain before post continues its current round."""
    try:
        result = auto_recover_supervisor(
            resolve_project_root(),
            coverage_manifest_exists=_coverage_manifest_exists(resolve_project_root()),
        )
    except (OSError, ValueError, AttributeError, RuntimeError) as exc:
        print(f"[supervisor] ✗ 自动恢复未完成: {exc}")
        return False
    return result.get("status") != "blocked"

def mutation_not_applicable(reason: str, report_only: bool) -> dict[str, Any]:
    """无适用变异目标时的返回。

    ``report_only=True``（``fix_mutation_report_only`` 的默认值）表示该门禁
    只汇报、不阻断结算；此时如实返回 ``reported`` 并保留 ``reason``，绝不能
    反过来把"没有可变异目标"升级成 post 失败。``report_only=False`` 时才
    保留 ``not_applicable`` 终态，由调用方决定是否阻断。
    """
    if report_only:
        return {
            "status": "reported",
            "reason": reason,
            "report_only": True,
            "mutation_applicable": False,
        }
    return {
        "status": "not_applicable",
        "reason": reason,
        "report_only": False,
        "mutation_applicable": False,
    }


def run_fix_mutation_gate(
    root: Path,
    descriptor: Mapping[str, Any] | None,
    *,
    report_only: bool = True,
    threshold: float = 60.0,
) -> dict[str, Any]:
    """Run bounded mutation verification for a fix Life descriptor.

    The descriptor is host-controlled. Worker-provided shell is rejected; the existing
    verifier remains the only execution engine and restores mutated files on every path.
    """
    if not isinstance(descriptor, Mapping):
        return _mutation_not_applicable("missing_regression_descriptor", report_only)
    test_target = str(descriptor.get("test_target") or "").strip()
    target_files = descriptor.get("target_files")
    if not test_target or not isinstance(target_files, list) or not target_files:
        return {"status": "invalid", "reason": "descriptor_requires_test_target_and_target_files"}
    try:
        command = shlex.split(test_target)
    except ValueError as exc:
        return {"status": "invalid", "reason": f"invalid_test_target: {exc}"}
    allowed_roots = {str(Path(sys.executable).resolve()), "python", "python3"}
    if not command or command[0] not in allowed_roots:
        return {"status": "invalid", "reason": "test_target_must_use_python_runner"}
    if "-m" not in command or "unittest" not in command:
        return {"status": "invalid", "reason": "test_target_must_use_unittest"}
    try:
        timeout_sec = float(descriptor.get("timeout_sec", 2.0))
    except (TypeError, ValueError):
        return {"status": "invalid", "reason": "timeout_sec_invalid"}
    if not 0.5 <= timeout_sec <= 10.0:
        return {"status": "invalid", "reason": "timeout_sec_out_of_range"}

    project_root = root.resolve()
    targets: list[Path] = []
    for raw in target_files[:8]:
        path = (project_root / str(raw)).resolve()
        try:
            path.relative_to(project_root)
        except ValueError:
            return {"status": "invalid", "reason": "target_file_outside_project"}
        if path.suffix != ".py" or not path.is_file():
            return _mutation_not_applicable(f"unsupported_target: {raw}", report_only)
        targets.append(path)

    from core.planning import test_capability_verifier
    reports: list[dict[str, Any]] = []
    for target in targets:
        try:
            summary = test_capability_verifier.verify_test_capability(
                target_path=target,
                test_target=test_target,
                max_mutants=min(int(descriptor.get("max_mutants", 20)), 50),
                timeout_sec=timeout_sec,
                cwd=project_root,
            )
            reports.append(summary.to_dict())
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            return {"status": "error", "reason": str(exc), "reports": reports}
    score = min(float(item.get("mutation_score", 0.0)) for item in reports)
    status = "reported" if report_only else ("passed" if score >= threshold else "rejected")
    return {
        "status": status,
        "mutation_score": round(score, 2),
        "threshold": threshold,
        "reports": reports,
        "report_only": report_only,
    }


_mutation_not_applicable = mutation_not_applicable
_RUNTIME_STARTED_THIS_PROCESS = RUNTIME_STARTED_THIS_PROCESS
_coverage_manifest_path = coverage_manifest_path

