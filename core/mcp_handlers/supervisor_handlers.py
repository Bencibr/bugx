#!/usr/bin/env python3
"""core/mcp_handlers/supervisor_handlers.py - Supervisor 控制平面工具处理器 (DOC-070)。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from core.supervisor import audit_supervisor
import core.bugx_paths as bugx_paths
from core.coverage import coverage_bootstrap
from core.coverage import module_coverage
import core.secure_atomic as secure_atomic

try:
    from core.supervisor.claim_sync import pid_is_alive
    from core.supervisor.lease_manager import normalize_worker_id
    from core.supervisor.supervisor_models import VALID_COVERAGE_STATES, worker_attempts_exhausted
except ImportError:  # pragma: no cover - core-package fallback
    from core.supervisor.claim_sync import pid_is_alive
    from core.supervisor.lease_manager import normalize_worker_id
    from core.supervisor.supervisor_models import VALID_COVERAGE_STATES, worker_attempts_exhausted

from .base import (
    McpProtocolError,
    bool_argument,
    check_json_depth_and_keys,
    number_argument,
    optional_string,
    optional_string_array_argument,
    project_state,
    serialize_value,
    status_payload_safe,
    string_array_argument,
    validate_checkpoint_sizes,
)


_CHECKPOINT_ZERO_TEXT = re.compile(r"[+-]?0+(?:\.0+)?\Z")
logger = logging.getLogger(__name__)


def _refresh_acceptance_receipt_after_rebind(service: Any) -> None:
    """Keep an already-written receipt aligned with the rebound supervisor state."""
    from core.launch_phases.post_receipt import refresh_receipt_supervisor_hash

    active = service._scope()
    try:
        refresh_receipt_supervisor_hash(
            active.supervisor,
            (
                active.state_root / "acceptance-evidence-receipt.json",
                active.lifecycle_root / "acceptance-evidence-receipt.json",
            ),
        )
    except Exception as exc:
        raise McpProtocolError(f"无法刷新验收收据: {exc}") from exc


def checkpoint_life_delta(arguments: Mapping[str, object]) -> int | float:
    """Accept the MCP wire form of exact zero without weakening the invariant.

    Some ACP models serialize the schema's numeric constant as the string
    "0". Textual input is accepted only when it is an unambiguous decimal
    representation of zero; non-zero text and all other types continue
    through the strict numeric validator and are rejected.
    """
    raw = arguments.get("life_delta", 0.0)
    if isinstance(raw, str):
        if not _CHECKPOINT_ZERO_TEXT.fullmatch(raw.strip()):
            raise McpProtocolError("life_delta 必须是数字")
        raw = 0.0
    value = number_argument({"life_delta": raw}, "life_delta", 0.0)
    # CORE-405 契约：``life_delta`` 必须是 **0**。此前只由 MCP JSON Schema 的
    # ``const: 0`` 保证——绕过 schema 的调用方（引擎 CLI / 库直调 handler /
    # 内部编排）可以把 1.5 直接写进 checkpoint，破坏生命周期账本。
    # 这里补第二道闸（与 L4 审计 A4「区间只在 schema 层」同类修复）。
    if float(value) != 0.0:
        raise McpProtocolError(
            f"life_delta 必须为 0（收到 {value!r}）：非 0 会改写生命账本"
        )
    return value


def _qualified_run_id(current: Mapping[str, object] | None,
                      explicit: object) -> str | None:
    """PE-20261005-003: Supervisor run_id 归一化为 polling 限定形式。

    - 显式缺省 → 派生 `lifecycle-{polling_id}-{lifecycle_id}`；
    - 显式裸形式 `lifecycle-{lifecycle_id}` → 归一化为 polling 限定（堵跨轮撞键旁路）；
    - 其余显式值（会话 UUID 等）原样保留；
    - polling 上下文缺失时无法归一化，保留显式值或返回 None。
    """
    lifecycle_id = str((current or {}).get("lifecycle_id") or "").strip()
    polling_id = str((current or {}).get("polling_id") or "").strip()
    qualified = (
        f"lifecycle-{polling_id}-{lifecycle_id}"
        if lifecycle_id and polling_id else None
    )
    value = explicit.strip() if isinstance(explicit, str) else None
    if not value:
        return qualified
    if qualified and value == f"lifecycle-{lifecycle_id}":
        return qualified
    return value


def handle_supervisor_start(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_start"
    service._require_role(arguments, name, "host")
    clean_aborted = arguments.get("clean_aborted", False)
    if not isinstance(clean_aborted, bool):
        raise McpProtocolError("clean_aborted 必须是布尔值")
    active = service._scope()
    # D5 前置校验：Session 与门禁状态检查
    if active.runtime.is_file():
        try:
            runtime_data = json.loads(active.runtime.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            runtime_data = None
        if isinstance(runtime_data, dict):
            session_st = runtime_data.get("session_state")
            run_st = runtime_data.get("state")
            startup_res = runtime_data.get("startup_result")
            if run_st == "aborted" or session_st == "aborted":
                if clean_aborted:
                    runtime_data.update({
                        "state": "idle",
                        "active": False,
                        "session_state": "ready",
                        "startup_result": "clean_aborted",
                        "polling_stop_reason": None,
                        "lifecycle_stop_reason": None,
                    })
                    try:
                        secure_atomic.atomic_write_json(active.runtime, runtime_data)
                    except OSError as exc:
                        raise McpProtocolError(
                            f"无法清理已中断运行态: {exc}"
                        ) from exc
                    startup_res = "clean_aborted"
                    logger.warning(
                        "clean_aborted=true: cleared the aborted start gate "
                        "(run_state=%s, session_state=%s)",
                        run_st,
                        session_st,
                    )
                else:
                    return {
                        "status": "ABORTED_SESSION_ACTION_REQUIRED",
                        "interactive_required": True,
                        "gate": "aborted_session",
                        "next_action": "RECOVER_OR_NEW_SESSION",
                        "detail": "检测到已中断的会话，请先恢复或新建会话后再启动 Supervisor",
                    }
            if startup_res in {"blocked", "gated"}:
                return {
                    "status": "STARTUP_GATE_BLOCKED",
                    "interactive_required": True,
                    "gate": "startup_gate",
                    "next_action": "RESOLVE_STARTUP_GATE",
                    "detail": "启动门禁未通过，请先解决启动门禁阻断项",
                }
    coverage_state = coverage_bootstrap.inspect(service.root)
    if coverage_state.get("needs_bootstrap"):
        return service._coverage_bootstrap_gate(coverage_state)
    if coverage_state.get("blocked"):
        raise McpProtocolError(str(coverage_state.get("detail") or "覆盖清单门禁失败"))
    manifest = Path(str(coverage_state.get("path") or ""))
    strict_errors = module_coverage.startup_gate_errors(
        service.root, manifest, allow_business_partial=True,
    )
    if strict_errors:
        return {
            "status": "COVERAGE_MANIFEST_UNTRUSTED",
            "interactive_required": True,
            "next_action": "REBUILD_COVERAGE",
            "gate": "coverage_integrity",
            "multiple": False,
            "path": str(manifest),
            "errors": strict_errors[:20],
            "detail": "自动审查拒绝使用未通过符号/关系图完整性校验的覆盖清单",
        }
    try:
        current = bugx_paths.read_current(service.root)
    except (OSError, ValueError):
        # 显式 run_id 的调用方可能来自无完整布局的上下文；归一化尽力而为
        current = None
    run_id = _qualified_run_id(current, arguments.get("run_id"))
    if not isinstance(run_id, str) or not run_id.strip():
        raise McpProtocolError("run_id 必须是非空字符串")
    # Project/Session/Round/Life/plan 绑定：优先使用调用方显式传入，否则从当前 lifecycle 推导
    project_id = optional_string(arguments, "project_id")
    session_id = optional_string(arguments, "session_id")
    round_id = optional_string(arguments, "round_id")
    life_id = optional_string(arguments, "life_id")
    life_kind = arguments.get("life_kind", "mine")
    if life_kind not in {"mine", "fix"}:
        raise McpProtocolError("life_kind 必须是 mine 或 fix")
    plan_hash = optional_string(arguments, "plan_hash")
    if project_id is None:
        try:
            current = bugx_paths.read_current(service.root)
            project_id = current.get("project_id") or active.root.name
        except Exception:
            project_id = None
    if session_id is None:
        try:
            current = bugx_paths.read_current(service.root)
            session_id = current.get("polling_id")
        except Exception:
            session_id = None
    if round_id is None:
        round_id = run_id
    supervisor = service.get_supervisor(budget=service._budget(arguments))
    try:
        state = supervisor.start(
            life_score=number_argument(arguments, "life_score", 1.0),
            coverage_units=service._coverage_units(),
            run_id=run_id,
            project_id=project_id,
            session_id=session_id,
            round_id=round_id,
            life_id=life_id,
            life_kind=str(life_kind),
            plan_hash=plan_hash,
        )
        return project_state(state)
    except audit_supervisor.RunAlreadyStarted:
        existing = supervisor.status()
        if existing.run_id != run_id:
            raise
        # Reuse must expose the same gate decision as status.  A rebuilt
        # workspace can drift after the first start; returning CLAIM_WORKERS
        # here would bypass the required rebind/rebuild step.
        return {**service._status_payload(supervisor), "reused": True}


def handle_supervisor_reset_budget(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """Open a new budget epoch without discarding findings or coverage."""
    name = "bugx_supervisor_reset_budget"
    service._require_role(arguments, name, "host")
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise McpProtocolError("expected_revision 必须是非负整数")
    force = bool_argument(arguments, "force", False)
    renew_wall_clock = bool_argument(arguments, "renew_wall_clock", False)
    try:
        service.get_supervisor(budget=service._budget(arguments)).reset_budget(
            expected_revision=expected_revision,
            force=force,
            renew_wall_clock=renew_wall_clock,
        )
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc)) from exc
    return status_payload_safe(service, service.get_supervisor(), mutation_committed=True)


def handle_supervisor_findings(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """DOC-230 §3.8 第 4 项：severity 修复通道（host-only）。

    规格要求历史脏 severity「可修复而非永久毒化」。边界与 CLI 一致：只改
    ``severity``，不改 ``confirmation``；值必须命中写侧词表；真实变更写
    ``finding_severity_repaired`` 审计事件；幂等调用不推进 revision。
    返回修复结果本身（含 changed/revision），便于调用方断言"是否真的改了"。
    """
    name = "bugx_supervisor_findings"
    service._require_role(arguments, name, "host")
    fix_severity = arguments.get("fix_severity", False)
    if not isinstance(fix_severity, bool):
        raise McpProtocolError("fix_severity 必须是布尔值")
    if not fix_severity:
        raise McpProtocolError(
            "findings 目前仅支持 fix_severity=true（只读汇总请用 status / coverage）"
        )
    finding = arguments.get("finding")
    if not isinstance(finding, str) or not finding.strip():
        raise McpProtocolError("finding 必须是非空字符串")
    severity = arguments.get("severity")
    if not isinstance(severity, str) or not severity.strip():
        raise McpProtocolError("severity 必须是非空字符串")
    reason = arguments.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise McpProtocolError("reason 若提供必须是非空字符串")
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise McpProtocolError("expected_revision 必须是非负整数")
    supervisor = service.get_supervisor()
    try:
        result = supervisor.fix_finding_severity(
            finding=finding,
            severity=severity,
            reason=reason,
            expected_revision=expected_revision,
        )
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc)) from exc
    result = dict(result)
    result["status"] = "REPAIRED" if result.get("changed") else "UNCHANGED"
    return result


def handle_supervisor_rebind_target(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """Bind the run to a deliberately rebuilt workspace target fingerprint."""
    name = "bugx_supervisor_rebind_target"
    service._require_role(arguments, name, "host")
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise McpProtocolError("reason 必须是非空字符串")
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise McpProtocolError("expected_revision 必须是非负整数")
    supervisor = service.get_supervisor()
    gate = service._status_payload(supervisor).get("parallel_gate", {})
    if not gate.get("target_drift"):
        result = service._status_payload(service.get_supervisor())
        result.update({
            "rebound": False,
            "rebind_skipped_reason": "target_not_drifted",
        })
        return result
    try:
        supervisor.rebind_target(
            reason=reason,
            expected_revision=expected_revision,
        )
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc)) from exc
    result = service._status_payload(service.get_supervisor())
    _refresh_acceptance_receipt_after_rebind(service)
    result["rebound"] = True
    return result


def handle_supervisor_sync_finding_decisions(
    service: Any, arguments: Mapping[str, object],
) -> dict[str, object]:
    """Import only validated decisions from the active lifecycle report."""
    name = "bugx_supervisor_sync_finding_decisions"
    service._require_role(arguments, name, "host")
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise McpProtocolError("expected_revision 必须是非负整数", code=-32602)
    canonicalize_report_identity = arguments.get("canonicalize_report_identity", False)
    if not isinstance(canonicalize_report_identity, bool):
        raise McpProtocolError("canonicalize_report_identity 必须是布尔值", code=-32602)
    supervisor = service.get_supervisor()
    try:
        supervisor.sync_finding_decisions_audited(
            expected_revision=expected_revision,
            canonicalize_report_identity=canonicalize_report_identity,
        )
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc), code=-32602) from exc
    return status_payload_safe(service, supervisor, mutation_committed=True)


def handle_supervisor_verify_finding(
    service: Any, arguments: Mapping[str, object],
) -> dict[str, object]:
    """Run engine-owned dual positive plus fixed negative verification."""
    name = "bugx_supervisor_verify_finding"
    service._require_role(arguments, name, "host")
    finding = arguments.get("finding")
    if not isinstance(finding, str) or not finding.strip():
        raise McpProtocolError("finding 必须是非空字符串", code=-32602)
    recipe = arguments.get("recipe")
    if not isinstance(recipe, Mapping):
        raise McpProtocolError("recipe 必须是对象", code=-32602)
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise McpProtocolError("expected_revision 必须是非负整数", code=-32602)
    request_id = arguments.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id.strip()
    ):
        raise McpProtocolError("request_id 必须是非空字符串", code=-32602)
    try:
        return service.get_supervisor().verify_finding(
            finding=finding,
            recipe=recipe,
            expected_revision=expected_revision,
            request_id=request_id,
        )
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc), code=-32602) from exc
    except (ValueError, OSError) as exc:
        raise McpProtocolError(str(exc), code=-32602) from exc


def handle_supervisor_confirm_finding(
    service: Any, arguments: Mapping[str, object],
) -> dict[str, object]:
    """Apply one host-owned Finding decision in the Supervisor transaction."""
    name = "bugx_supervisor_confirm_finding"
    service._require_role(arguments, name, "host")
    finding_id = arguments.get("finding_id")
    if not isinstance(finding_id, str) or not finding_id.strip():
        raise McpProtocolError("finding_id 必须是非空字符串", code=-32602)
    decision = arguments.get("decision")
    if not isinstance(decision, str) or decision not in {"confirmed", "rejected"}:
        raise McpProtocolError(
            "decision 只能是 confirmed 或 rejected", code=-32602,
        )
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise McpProtocolError(
            "reason 必须是非空字符串（最多2000字符）", code=-32602,
        )
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise McpProtocolError("expected_revision 必须是非负整数", code=-32602)
    rejection_class = arguments.get("rejection_class")
    if rejection_class is not None and (
        not isinstance(rejection_class, str) or not rejection_class.strip()
    ):
        raise McpProtocolError("rejection_class 必须是非空字符串", code=-32602)
    root_cause_verified = arguments.get("root_cause_verified")
    if root_cause_verified is not None and not isinstance(root_cause_verified, bool):
        raise McpProtocolError("root_cause_verified 必须是布尔值", code=-32602)
    oracle = arguments.get("oracle")
    if oracle is not None and not isinstance(oracle, Mapping):
        raise McpProtocolError("oracle 必须是对象", code=-32602)
    remediation = arguments.get("remediation")
    if remediation is not None and not isinstance(remediation, Mapping):
        raise McpProtocolError("remediation 必须是对象", code=-32602)
    target_platform = arguments.get("target_platform")
    if target_platform is not None and (
        not isinstance(target_platform, str) or not target_platform.strip()
    ):
        raise McpProtocolError("target_platform 必须是非空字符串", code=-32602)

    supervisor = service.get_supervisor()
    try:
        # L1-ADV-D1：裁决证据由引擎核验 receipt 派生并自检，缺料即 fail-closed；
        # handler 不再自己写 confirmation（旧实现只写 {state,reason,at} 必然堵死 post）。
        supervisor.confirm_finding(
            finding=finding_id.strip(),
            decision=decision,
            reason=reason,
            expected_revision=expected_revision,
            rejection_class=(
                rejection_class.strip() if isinstance(rejection_class, str) else None
            ),
            root_cause_verified=root_cause_verified,
            remediation=remediation,
            oracle=oracle,
            target_platform=(
                target_platform.strip() if isinstance(target_platform, str) else None
            ),
        )
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc), code=-32602) from exc
    except ValueError as exc:
        # 确认门禁拒绝（缺引擎核验/证据不足/oracle 来源未核验）：明确拒绝而非静默成功。
        raise McpProtocolError(str(exc), code=-32602) from exc
    return status_payload_safe(service, supervisor, mutation_committed=True)


def handle_supervisor_bootstrap_coverage(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_bootstrap_coverage"
    service._require_role(arguments, name, "host")
    confirmed = arguments.get("confirmed", False)
    if not isinstance(confirmed, bool):
        raise McpProtocolError("confirmed 必须是布尔值")
    if not confirmed:
        state = coverage_bootstrap.inspect(service.root)
        return service._coverage_bootstrap_gate(state)
    try:
        return coverage_bootstrap.bootstrap(service.root, confirmed=True)
    except coverage_bootstrap.CoverageBootstrapError as exc:
        raise McpProtocolError(f"{exc.code}: {exc}") from exc


def _uninitialized_status_payload(service: Any, reason: str = "") -> dict[str, object]:
    role = getattr(service, "role", "host") or "host"
    root_str = str(getattr(service, "root", "."))
    return {
        "status": "UNINITIALIZED",
        "initialized": False,
        "next_action": "INITIALIZE_PROJECT",
        "root": root_str,
        "gate": "initialization_gate",
        "polling_allowed": False,
        "install_command": f"bugx install --root . --agent {role} --non-interactive",
        "detail": (
            f"项目尚未完成 BugX 工作区初始化: {reason}"
            if reason
            else "项目尚未完成 BugX 工作区初始化，缺少 .bugx/ 布局；请先执行初始化或通过宿主安装配置"
        ),
    }


def handle_supervisor_status(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    try:
        supervisor = service.get_supervisor()
    except Exception as exc:
        return _uninitialized_status_payload(service, str(exc))
    since_revision = arguments.get("since_revision")
    if since_revision is not None and isinstance(since_revision, int):
        try:
            cur_state = supervisor.status()
            if cur_state.revision == since_revision:
                return {
                    "revision": cur_state.revision,
                    "changed": False,
                    "status": cur_state.status.value,
                    "next_action": "POLL_LATER",
                    "root": str(service.root),
                }
        except Exception:
            pass
    return service._status_payload(supervisor)


def handle_supervisor_gate(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    try:
        supervisor = service.get_supervisor()
    except Exception as exc:
        role = getattr(service, "role", "host") or "host"
        root_str = str(getattr(service, "root", "."))
        return {
            "status": "UNINITIALIZED",
            "initialized": False,
            "next_action": "INITIALIZE_PROJECT",
            "parallel_gate": None,
            "gate": "initialization_gate",
            "polling_allowed": False,
            "root": root_str,
            "install_command": f"bugx install --root . --agent {role} --non-interactive",
            "detail": f"项目尚未完成 BugX 工作区初始化: {exc}；请先通过受控入口初始化",
        }
    since_revision = arguments.get("since_revision")
    if since_revision is not None and isinstance(since_revision, int):
        try:
            cur_state = supervisor.status()
            if cur_state.revision == since_revision:
                return {
                    "revision": cur_state.revision,
                    "changed": False,
                    "next_action": "POLL_LATER",
                }
        except Exception:
            pass
    status = service._status_payload(supervisor)
    gate = status.get("parallel_gate")
    if not isinstance(gate, Mapping):
        # ``_status_payload`` deliberately returns a compact NO_RUN payload
        # before the host has called ``start``.  Keep the read-only gate
        # facade total instead of turning that valid lifecycle state into a
        # server-side KeyError during an ACP tools/call.
        return {
            "status": status.get("status", "NO_RUN"),
            "next_action": status.get("next_action", "START_RUN"),
            "root": status.get("root", str(service.root)),
        }
    return {**gate, "next_action": status["next_action"]}


def handle_supervisor_coverage(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """Read one coverage unit or changes after an event sequence cursor."""
    try:
        state = service.get_supervisor().status()
    except Exception as exc:
        return {
            "status": "UNINITIALIZED",
            "initialized": False,
            "next_action": "INITIALIZE_PROJECT",
            "coverage": {},
            "revision": 0,
            "detail": f"项目尚未完成 BugX 工作区初始化: {exc}",
        }
    unit_id = arguments.get("unit_id")
    since = arguments.get("since_revision", 0)
    status_filter = arguments.get("status_filter")
    if unit_id is not None and (not isinstance(unit_id, str) or not unit_id.strip()):
        raise McpProtocolError("unit_id 必须是非空字符串")
    if isinstance(since, bool) or not isinstance(since, int) or since < 0:
        raise McpProtocolError("since_revision 必须是非负整数")
    if status_filter is not None and (
        not isinstance(status_filter, str)
        or status_filter not in VALID_COVERAGE_STATES
    ):
        raise McpProtocolError("status_filter 必须是有效 coverage 状态")
    blocked_reasons = {
        unit: "worker_attempts_exhausted"
        for unit, status in state.coverage.items()
        if status == "blocked" and worker_attempts_exhausted(state, unit)
    }
    if unit_id is not None:
        if unit_id not in state.coverage:
            raise McpProtocolError(f"未知 coverage unit: {unit_id}")
        if status_filter is None:
            return {"coverage": {unit_id: state.coverage[unit_id]}, "revision": state.revision}
        coverage = (
            {unit_id: state.coverage[unit_id]}
            if state.coverage[unit_id] == status_filter
            else {}
        )
        return {
            "coverage": coverage,
            "revision": state.revision,
            "status_filter": status_filter,
            "blocked_reasons": {
                unit: reason
                for unit, reason in blocked_reasons.items()
                if unit in coverage
            },
        }
    if status_filter is not None:
        coverage = {
            unit: status
            for unit, status in state.coverage.items()
            if status == status_filter
        }
        return {
            "coverage": dict(sorted(coverage.items())),
            "since_revision": since,
            "revision": state.revision,
            "status_filter": status_filter,
            "blocked_reasons": {
                unit: reason
                for unit, reason in blocked_reasons.items()
                if unit in coverage
            },
        }
    changed: dict[str, str] = {}
    for event in state.history:
        if not isinstance(event, dict):
            continue
        event_revision = int(event.get("revision", event.get("seq", 0)) or 0)
        if event_revision <= since:
            continue
        updates = event.get("coverage_updates")
        if isinstance(updates, dict):
            changed.update({str(key): str(value) for key, value in updates.items()})
    return {
        "coverage": dict(sorted(changed.items())),
        "since_revision": since,
        "revision": state.revision,
        "blocked_reasons": {
            unit: reason
            for unit, reason in blocked_reasons.items()
            if unit in changed
        },
    }


def handle_supervisor_claim(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_claim"
    service._require_role(arguments, name, "worker")
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    worker_key = normalize_worker_id(worker_id)
    unit_ids = string_array_argument(arguments, "unit_ids")
    allow_bundle = arguments.get("allow_bundle", False)
    if not isinstance(allow_bundle, bool):
        raise McpProtocolError("allow_bundle 必须是布尔值")
    continue_worker = arguments.get("continue_worker", False)
    if not isinstance(continue_worker, bool):
        raise McpProtocolError("continue_worker 必须是布尔值")
    recover_expired = arguments.get("recover_expired", False)
    if not isinstance(recover_expired, bool):
        raise McpProtocolError("recover_expired 必须是布尔值")
    supersede_worker_id = arguments.get("supersede_worker_id")
    if supersede_worker_id is not None and (
        not isinstance(supersede_worker_id, str) or not supersede_worker_id.strip()
    ):
        raise McpProtocolError("supersede_worker_id 必须是非空字符串")

    if allow_bundle:
        # DOC-231 A4: Worker MCP 新 claim 禁止 allow_bundle=true。
        # 本地调度器保留 core 批量调用通道，防止 Worker 自报 bundle 扩大领取。
        raise McpProtocolError(
            "Worker MCP claim 禁止 allow_bundle=true；批量 claim 只能由本地 orchestrator core 调用"
        )
    supervisor = service.get_supervisor()
    if unit_ids is not None:
        # 缺陷 #7：schema 曾声明 ``maxItems: 5``（亲和任务包），而这里只接受单元
        # claim——声明比实现宽，按 schema 合法的 2~5 单元调用一律被拒。多单元
        # 批量领取是 core 本地调度器的特权（DOC-231 A4：Worker MCP 禁
        # ``allow_bundle=true`` 自扩领取），因此**收窄 schema** 而不是放宽治理：
        # MCP 侧契约就是「一次一个单元」，越界时报同一个上界。
        if not allow_bundle and len(unit_ids) != 1 and supersede_worker_id is None:
            raise McpProtocolError(
                "每个 Worker 每次只能领取一个 coverage unit（schema maxItems=1）；"
                "请拆分任务后再次 claim"
            )
        if len(unit_ids) < 1 or len(unit_ids) > 5:
            raise McpProtocolError("单次 claim 单元数必须在 1 到 5 之间")
    raw_pid = arguments.get("process_pid")
    process_pid = None
    if raw_pid is not None:
        process_pid = int(number_argument(arguments, "process_pid", 0, integer=True, minimum=1))
    resource_keys = string_array_argument(arguments, "resource_keys")
    # 缺陷 B（recover_expired=true）：同名单 worker 已终态时，一次调用完成
    # supersede 接手 —— 同一 worker_id 换新租约，继承原 unit_ids/artifact_root，
    # 宿主随后正常走 worker_start（无 PID）+ checkpoint。worker 不存在时退化为
    # 普通 claim；非终态（active/submitted）显式拒绝。
    recover_key: str | None = None
    if recover_expired and supersede_worker_id is None and continue_worker is False:
        existing = None
        try:
            snapshot = supervisor.status()
            existing = (getattr(snapshot, "claims", {}) or {}).get(worker_key)
        except Exception:
            existing = None  # 无 run 等场景交给 claim_worker 抛出规范错误
        if existing is not None:
            if existing.state in {"expired", "released"}:
                recover_key = worker_key
            elif existing.state == "superseded":
                raise McpProtocolError(
                    f"worker {worker_key} 已被 {existing.superseded_by or '其他 worker'} 接手；"
                    "recover_expired 仅适用于 expired/released claim",
                    code=-32602,
                )
            else:
                raise McpProtocolError(
                    "recover_expired 仅适用于终态（expired/superseded）claim："
                    f"worker {worker_key} 当前状态为 {existing.state}",
                    code=-32602,
                )
    result = dict(serialize_value(supervisor.claim_worker(
        worker_id,
        lease_seconds=number_argument(arguments, "lease_seconds", 300.0),
        unit_ids=unit_ids,
        resource_keys=resource_keys,
        expected_revision=arguments.get("expected_revision"),
        process_pid=process_pid,
        # claim_sync 仅为 <=3 单元的历史接手包提供隐式 bundle 例外；
        # 新 claim 仍必须显式 allow_bundle=True 才能批量领取。
        allow_bundle=allow_bundle,
        supersede_worker_id=(
            recover_key if recover_key is not None else supersede_worker_id
        ),
        continue_worker=continue_worker,
        worker_role=arguments.get("worker_role"),
        preferred_affinity=arguments.get("preferred_affinity"),
    )))
    if recover_key is not None:
        # 新租约信息（lease_id/lease_expires_at/total_lease_deadline）已在
        # claim 序列化内；recovered_from 标明本次换约接手的旧 worker 身份。
        result["recovered_from"] = recover_key
    # Claim 与 worker-start 分离：原子 claim 成功不等于真实 Worker 已启动。
    # 旧实现吞掉 start_worker 异常并返回 claimed，随后 start timeout 回收 claim，
    # 子代理只能在数百秒后发现 checkpoint 不可提交。调用方必须紧接 worker_start；
    # 本返回明确 execution_state=claimed，禁止伪装 running。
    result["epoch"] = 1
    result["start_required"] = True
    return result


def handle_supervisor_reopen_finding(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    service._require_role(arguments, "bugx_supervisor_reopen_finding", "host")
    return project_state(service.get_supervisor().reopen_finding(
        arguments.get("finding_id"), reason=arguments.get("reason"),
        expected_revision=arguments.get("expected_revision"),
    ))


def handle_supervisor_admit_business(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_admit_business"
    service._require_role(arguments, name, "host")
    unit_id = arguments.get("unit_id")
    reason = arguments.get("reason")
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise McpProtocolError("unit_id 必须是非空字符串")
    if not isinstance(reason, str) or not reason.strip():
        raise McpProtocolError("reason 必须是非空字符串")
    return project_state(service.get_supervisor().admit_business_unit(
        unit_id.strip(),
        risk_score=number_argument(arguments, "risk_score", 70.0),
        reason=reason,
        expected_revision=arguments.get("expected_revision"),
    ))


def handle_supervisor_worker_start(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_worker_start"
    service._require_role(arguments, name, "worker")
    supervisor = service.get_supervisor()
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    provider = arguments.get("provider", "unknown")
    if not isinstance(provider, str) or not provider.strip():
        raise McpProtocolError("provider 必须是非空字符串")
    execution_id = arguments.get("execution_id")
    if execution_id is not None and (
        not isinstance(execution_id, str) or not execution_id.strip()
    ):
        raise McpProtocolError("execution_id 必须是非空字符串")
    raw_pid = arguments.get("process_pid")
    process_pid = None
    if raw_pid is not None:
        process_pid = int(number_argument(arguments, "process_pid", 0, integer=True, minimum=1))
        if not pid_is_alive(process_pid):
            # 缺陷 A：worker_start 登记 PID 前同样前移存活校验，避免死 PID
            # 触发后续抢占把 claim 直接推进终态 expired。
            raise McpProtocolError(
                f"process_pid {process_pid} 不存活：Task 子代理请省略 process_pid，"
                "以心跳作为活性证据",
                code=-32602,
            )
    return dict(serialize_value(supervisor.start_worker(
        worker_id,
        provider=provider,
        execution_id=execution_id,
        expected_revision=arguments.get("expected_revision"),
        process_pid=process_pid,
    )))


def handle_supervisor_tool_call(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_tool_call"
    service._require_role(arguments, name, "worker")
    supervisor = service.get_supervisor()
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    return dict(serialize_value(supervisor.record_tool_call(
        worker_id,
        cost_units=number_argument(arguments, "cost_units", 0.0),
        tool_name=arguments.get("tool_name") if isinstance(arguments.get("tool_name"), str) else None,
        expected_revision=arguments.get("expected_revision"),
    )))


def handle_supervisor_heartbeat(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_heartbeat"
    service._require_role(arguments, name, "worker")
    supervisor = service.get_supervisor()
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    return dict(serialize_value(supervisor.heartbeat(
        worker_id,
        lease_seconds=number_argument(arguments, "lease_seconds", 300.0),
        heartbeat_nonce=optional_string(arguments, "heartbeat_nonce"),
        expected_revision=arguments.get("expected_revision"),
    )))


def handle_supervisor_checkpoint(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_checkpoint"
    service._require_role(arguments, name, "worker")
    supervisor = service.get_supervisor()
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    checkpoint_id = arguments.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
        raise McpProtocolError(
            "checkpoint_id 必须是非空字符串；重试必须复用同一个 checkpoint_id"
        )
    findings = arguments.get("findings")
    coverage = arguments.get("coverage")
    evidence = arguments.get("evidence")
    signatures = optional_string_array_argument(arguments, "signatures")
    notes = arguments.get("notes")
    # Typed checks first, then bounded size/depth checks (structured error)
    if findings is not None and not isinstance(findings, list):
        raise McpProtocolError("findings 必须是数组", code=-32602)
    if coverage is not None and not isinstance(coverage, dict):
        raise McpProtocolError("coverage 必须是对象", code=-32602)
    if evidence is not None and not isinstance(evidence, dict):
        raise McpProtocolError("evidence 必须是对象", code=-32602)
    if notes is not None and not isinstance(notes, dict):
        raise McpProtocolError("notes 必须是 unit_id -> 文本对象", code=-32602)
    check_json_depth_and_keys(arguments)
    validate_checkpoint_sizes(findings, coverage, evidence, signatures, notes)
    try:
        # Worker checkpoint 热路径禁止先调用 supervisor.status()：status 会刷新租约、
        # 回收僵尸并持久化 revision，随后让全局 CAS 失效（并发 Worker 的主要
        # 失败源）。submit_checkpoint 自己在同一 Supervisor mutation 锁内校验
        # claim/lease/execution/payload；Worker 不发送全局 expected_revision。
        state = supervisor.submit_checkpoint(
            worker_id,
            findings=findings,
            coverage=coverage,
            evidence=evidence,
            life_delta=checkpoint_life_delta(arguments),
            expected_revision=None,
            signatures=signatures,
            notes=notes,
            checkpoint_id=checkpoint_id,
        )
        delta = {
            unit: state.coverage[unit]
            for unit in (coverage or {})
            if unit in state.coverage
        }
        return project_state(state, coverage_delta=delta)
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc), code=-32602) from exc


def handle_supervisor_salvage_checkpoint(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """DOC-203 D2：宿主代交过期 claim 的已固化 checkpoint（host-only）。

    与 worker 通道同强度校验（五元组绑定 + 工件哈希重算 + mtime 纪元守卫 +
    一次性 salvaged_at），仅额外接受 state=="expired" 的 claim 并以受信执行
    对待 receipt（有记录的例外，见 checkpoint_sync.apply_checkpoint_operation）。
    """
    name = "bugx_supervisor_salvage_checkpoint"
    service._require_role(arguments, name, "host")
    supervisor = service.get_supervisor()
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    checkpoint_id = arguments.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
        raise McpProtocolError(
            "checkpoint_id 必须是非空字符串；重试必须复用同一个 checkpoint_id"
        )
    findings = arguments.get("findings")
    coverage = arguments.get("coverage")
    evidence = arguments.get("evidence")
    signatures = optional_string_array_argument(arguments, "signatures")
    notes = arguments.get("notes")
    if findings is not None and not isinstance(findings, list):
        raise McpProtocolError("findings 必须是数组", code=-32602)
    if coverage is not None and not isinstance(coverage, dict):
        raise McpProtocolError("coverage 必须是对象", code=-32602)
    if evidence is not None and not isinstance(evidence, dict):
        raise McpProtocolError("evidence 必须是对象", code=-32602)
    if notes is not None and not isinstance(notes, dict):
        raise McpProtocolError("notes 必须是 unit_id -> 文本对象", code=-32602)
    check_json_depth_and_keys(arguments)
    validate_checkpoint_sizes(findings, coverage, evidence, signatures, notes)
    try:
        before = supervisor.status()
        state = supervisor.submit_checkpoint(
            worker_id,
            findings=findings,
            coverage=coverage,
            evidence=evidence,
            life_delta=checkpoint_life_delta(arguments),
            expected_revision=arguments.get("expected_revision"),
            signatures=signatures,
            notes=notes,
            checkpoint_id=checkpoint_id,
            salvage=True,
        )
        before_coverage = before.coverage
        delta = {
            unit: status for unit, status in state.coverage.items()
            if before_coverage.get(unit) != status
        }
        return project_state(state, coverage_delta=delta)
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc), code=-32602) from exc


def handle_supervisor_begin_long_call(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """DOC-203 D3：worker 为一次长工具调用声明保活窗口（worker-only）。"""
    name = "bugx_supervisor_begin_long_call"
    service._require_role(arguments, name, "worker")
    supervisor = service.get_supervisor()
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    try:
        claim = supervisor.begin_long_call(
            worker_id,
            expected_seconds=number_argument(arguments, "expected_seconds", 0.0),
            expected_revision=arguments.get("expected_revision"),
        )
    except audit_supervisor.SupervisorError as exc:
        raise McpProtocolError(str(exc), code=-32602) from exc
    return serialize_value(claim)


def handle_supervisor_next_round(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_next_round"
    service._require_role(arguments, name, "host")
    supervisor = service.get_supervisor()
    mode = arguments.get("gate_failure_mode") or "report"
    if mode not in ("prompt_user", "report"):
        raise McpProtocolError(
            "gate_failure_mode 只能是 prompt_user 或 report", code=-32602
        )
    if mode == "report":
        # DOC-203 D4：只读预检——门禁未满足时直接返回结构化结果，
        # 不触发 next_round 的状态变更（避免自治重试风暴持续 bump
        # revision 伤害并发 worker 的乐观锁）。
        try:
            snapshot = supervisor.status_with_gate()
        except Exception:
            snapshot = {}
        gate = snapshot.get("parallel_gate") if isinstance(snapshot, dict) else None
        if isinstance(gate, Mapping) and not gate.get("satisfied"):
            state_dict = snapshot.get("state") if isinstance(snapshot, dict) else {}
            round_no = state_dict.get("round_no") if isinstance(state_dict, Mapping) else None
            return {
                "status": "GATE_UNSATISFIED",
                "round_no": round_no,
                "gate": dict(gate),
                "next_action": "RECOVER_OR_FINISH",
                "non_interactive": True,
                "hint": (
                    "并发门禁未满足（原因见 gate.reasons）。宿主可："
                    "1) 让过期/被回收 worker 按 PE-20261005-003 走 "
                    "recover_expired 接手或 supersede 换新 id 后重试；"
                    "2) 调用 finish(coverage_complete=false) 诚实收口。"
                    "本结果为只读快照，未产生任何状态变更。"
                ),
            }
    compact = bool(arguments.get("compact", False))
    try:
        state = supervisor.next_round(
            expected_revision=arguments.get("expected_revision"),
        )
    except audit_supervisor.ConcurrencyNotSatisfied as exc:
        # 预检通过后的竞态：gate dict 由 next_round_sync 挂在异常上。
        if mode == "report":
            gate = getattr(exc, "gate", None)
            if isinstance(gate, Mapping):
                return {
                    "status": "GATE_UNSATISFIED",
                    "gate": dict(gate),
                    "next_action": "RECOVER_OR_FINISH",
                    "non_interactive": True,
                    "hint": "并发门禁在预检后发生变化；本结果为结构化报告。",
                }
        raise
    return project_state(state, compact=compact)


def handle_supervisor_bind_context(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_bind_context"
    service._require_role(arguments, name, "host")
    supervisor = service.get_supervisor()
    return project_state(supervisor.bind_context(
        project_id=optional_string(arguments, "project_id"),
        session_id=optional_string(arguments, "session_id"),
        round_id=optional_string(arguments, "round_id"),
        life_id=optional_string(arguments, "life_id"),
        plan_hash=optional_string(arguments, "plan_hash"),
    ))


def handle_supervisor_finish(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_finish"
    service._require_role(arguments, name, "host")
    supervisor = service.get_supervisor()
    coverage_complete = arguments.get("coverage_complete")
    compact = bool(arguments.get("compact", False))
    if coverage_complete is not None and not isinstance(coverage_complete, bool):
        raise McpProtocolError("coverage_complete 必须是布尔值或 null")
    return project_state(supervisor.finish(
        coverage_complete=coverage_complete,
        expected_revision=arguments.get("expected_revision"),
    ), compact=compact)


def handle_supervisor_fail(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_fail"
    service._require_role(arguments, name, "host")
    supervisor = service.get_supervisor()
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise McpProtocolError("reason 必须是非空字符串")
    return project_state(supervisor.fail(
        reason, expected_revision=arguments.get("expected_revision")
    ))


def handle_supervisor_recover(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "bugx_supervisor_recover"
    service._require_role(arguments, name, "host")
    supervisor = service.get_supervisor()
    reset_worker_attempts = arguments.get("reset_worker_attempts", False)
    if not isinstance(reset_worker_attempts, bool):
        raise McpProtocolError("reset_worker_attempts 必须是布尔值")
    # Explicit recovery: must call Supervisor.recover() — normal status/claim stays fail-closed.
    try:
        if reset_worker_attempts:
            supervisor.recover(reset_worker_attempts=True)
        else:
            supervisor.recover()
    except audit_supervisor.NoRunError:
        return {
            "status": "NO_RUN",
            "next_action": "START_RUN",
            "root": str(service.root),
        }
    return status_payload_safe(service, supervisor, mutation_committed=True)


_WORKER_PROBE_SHELL_NAMES = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "fish",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "env",
        "env.exe",
        "busybox",
    }
)
_WORKER_PROBE_SHELL_OPERATORS = frozenset(
    {";", "&&", "||", "|", ">", ">>", "<", "<<", "<<<", "&", "2>&1", "1>&2"}
)


def handle_supervisor_run_sandboxed_probe(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """Run one bounded argv command from the project root; this is not an OS filesystem sandbox."""
    name = "bugx_supervisor_run_sandboxed_probe"
    service._require_role(arguments, name, "worker")
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    cmd_raw = arguments.get("command")
    if not isinstance(cmd_raw, list) or not cmd_raw or len(cmd_raw) > 64:
        raise McpProtocolError("command 必须是 1~64 项 argv 数组；不再接受 shell 命令字符串")
    if any(
        not isinstance(item, str) or not item or "\x00" in item or len(item.encode("utf-8")) > 4096
        for item in cmd_raw
    ):
        raise McpProtocolError("command 的每个 argv 必须是非空字符串、无 NUL 且不超过 4096 字节")
    if sum(len(item.encode("utf-8")) for item in cmd_raw) > 16384:
        raise McpProtocolError("command 的 argv 总长度不能超过 16384 字节")
    if any(Path(item).name.casefold() in _WORKER_PROBE_SHELL_NAMES for item in cmd_raw):
        raise McpProtocolError("Worker probe 禁止 shell 启动器；请用单条 argv 命令执行")
    if any(item in _WORKER_PROBE_SHELL_OPERATORS for item in cmd_raw):
        raise McpProtocolError("Worker probe 禁止 shell 控制符；请拆成多个 argv 命令并由 MCP 捕获输出")

    command = shlex.join(cmd_raw)
    from core.supervisor.supervisor_models import MAX_COMMAND_LENGTH
    if len(command) > MAX_COMMAND_LENGTH:
        raise McpProtocolError("command 超过 checkpoint 命令长度上限")
    supervisor = service.get_supervisor()
    state = supervisor.status()
    claim = state.claims.get(worker_id)
    if claim is None or not claim.active or claim.is_expired() or claim.execution_state != "running":
        raise McpProtocolError("probe 要求已 worker_start 且未过期的 active claim")
    for field in ("lease_id", "execution_id"):
        if arguments.get(field) != getattr(claim, field):
            raise McpProtocolError(f"probe {field} 与当前 claim 不一致；请使用本次 Task 的身份")

    timeout = number_argument(arguments, "timeout_seconds", 10.0, minimum=0.5, maximum=60.0)
    root = Path(service.root if hasattr(service, "root") else Path.cwd()).resolve(strict=False)

    start_time = time.monotonic()
    env = dict(os.environ)
    extra_env = arguments.get("env")
    if extra_env is not None and not isinstance(extra_env, dict):
        raise McpProtocolError("env 必须是字符串键值对象")
    if isinstance(extra_env, dict):
        if "PWD" in extra_env:
            requested_pwd = Path(str(extra_env["PWD"]))
            if not requested_pwd.is_absolute():
                requested_pwd = root / requested_pwd
            try:
                pwd_root = requested_pwd.resolve(strict=False)
            except (OSError, RuntimeError, ValueError) as exc:
                raise McpProtocolError("env.PWD 无法解析") from exc
            if pwd_root != root:
                raise McpProtocolError("env.PWD 必须绑定到项目根目录")
        env.update({str(k): str(v) for k, v in extra_env.items()})
    env["PWD"] = str(root)

    supervisor.record_tool_call(worker_id, tool_name=name)
    proc = None
    stdout, stderr = "", ""
    exit_code = 0
    try:
        proc = subprocess.Popen(
            cmd_raw,
            shell=False,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            stdout, stderr = proc.communicate()
            exit_code = -signal.SIGKILL
            stderr = (stderr or "") + f"\n[timeout]: Command timed out after {timeout}s and process group was terminated."
    except Exception as exc:
        exit_code = 1
        stderr = f"[error]: {type(exc).__name__}: {exc}"
    finally:
        duration_ms = round((time.monotonic() - start_time) * 1000, 2)

    result = {
        "status": "success" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "stdout": (stdout or "")[:100000],
        "stderr": (stderr or "")[:100000],
        "duration_ms": duration_ms,
    }
    # Capture execution output once, inside the Supervisor-owned artifact root.
    # The caller still decides coverage after inspecting this result and source.
    artifact_path = root / claim.artifact_root / f"probe-{uuid.uuid4().hex}.json"
    if not artifact_path.resolve(strict=False).is_relative_to(root):
        raise McpProtocolError("claim artifact_root 越出项目根目录")
    secure_atomic.atomic_write_json(artifact_path, {
        **result, "schema_version": 1, "argv": cmd_raw,
        "command": command, "worker_id": worker_id,
        "run_id": state.run_id, "round_no": claim.round_no,
        "lease_id": claim.lease_id, "execution_id": claim.execution_id,
        "target_fingerprint": state.target_fingerprint,
        "timestamp": time.time(),
    }, mode=0o600, parent_mode=0o700)
    current = supervisor.status()
    current_claim = current.claims.get(worker_id)
    if (
        current_claim is None or not current_claim.active or current_claim.is_expired()
        or current_claim.execution_state != "running"
        or current_claim.lease_id != claim.lease_id
        or current_claim.execution_id != claim.execution_id
        or current.target_fingerprint != state.target_fingerprint
    ):
        raise McpProtocolError("probe 执行期间 claim/目标发生变化；输出已保存，不可用作通过证据")
    artifact_rel = artifact_path.relative_to(root).as_posix()
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    evidence_status = "passed" if exit_code == 0 else "failed"
    receipt = {
        "schema_version": 1, "execution_id": claim.execution_id,
        "worker_id": worker_id, "lease_id": claim.lease_id,
        "command": command, "exit_code": exit_code,
        "artifact_path": artifact_rel, "artifact_sha256": digest,
        "status": evidence_status,
    }
    evidence = {
        "command": command, "exit_code": exit_code,
        "artifact_path": artifact_rel, "artifact_sha256": digest,
        "target_fingerprint": state.target_fingerprint,
        "timestamp": time.time(), "worker_id": worker_id,
        "run_id": state.run_id, "round_no": claim.round_no,
        "lease_id": claim.lease_id, "status": evidence_status,
        "execution_receipt": receipt,
    }
    return {**result, "unit_ids": list(claim.unit_ids), "evidence": evidence}


def handle_supervisor_submit_finding(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """补丁 17 & 18：强类型提交 Finding 并执行 Epoch 世代与反事实双向差分证据门禁校验。"""
    name = "bugx_supervisor_submit_finding"
    service._require_role(arguments, name, "worker")
    worker_id = arguments.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise McpProtocolError("worker_id 必须是非空字符串")
    worker_key = normalize_worker_id(worker_id)

    supervisor = service.get_supervisor()
    snapshot = supervisor.status()
    claims = getattr(snapshot, "claims", {}) or {}
    claim = claims.get(worker_key)
    if claim is None or claim.state != "active":
        raise McpProtocolError(
            f"worker {worker_key} 当前无有效活跃租约 (state={getattr(claim, 'state', 'none')})，拒绝提交",
            code=-32001,
        )
    epoch = arguments.get("epoch")
    if epoch is not None and int(epoch) < claim.scheduler_epoch:
        raise McpProtocolError(
            f"STALE_LEASE: 租约代数已过期 (claim.epoch={claim.scheduler_epoch}, provided={epoch})",
            code=-32002,
        )

    # 补丁 17：反事实双向证据门禁校验
    diff_evidence = arguments.get("differential_evidence")
    if not isinstance(diff_evidence, dict):
        raise McpProtocolError("submit_finding 必须提供 differential_evidence 双向证据对象")
    base_p = str(diff_evidence.get("baseline_payload", "")).strip()
    exploit_p = str(diff_evidence.get("exploit_payload", "")).strip()
    base_resp = str(diff_evidence.get("baseline_response", "")).strip()
    exploit_resp = str(diff_evidence.get("exploit_response", "")).strip()

    if not base_p or not exploit_p:
        raise McpProtocolError("differential_evidence 必须同时包含 baseline_payload 与 exploit_payload")
    if base_p == exploit_p:
        raise McpProtocolError("baseline_payload 与 exploit_payload 不得完全相同；必须提供反事实对照")
    if not exploit_resp:
        raise McpProtocolError("exploit_response 不能为空")
    if base_resp == exploit_resp:
        raise McpProtocolError("baseline_response 与 exploit_response 响应完全一致，未观察到利用特异性差异")

    finding_raw = arguments.get("finding")
    if not isinstance(finding_raw, dict):
        raise McpProtocolError("finding 必须是有效的字典对象")
    fp = finding_raw.get("fingerprint")
    if not isinstance(fp, str) or not fp.strip():
        raise McpProtocolError("finding.fingerprint 必须是非空字符串")

    finding_dict = dict(finding_raw)
    finding_dict.setdefault("id", f"finding-{fp[:16]}")
    finding_dict.setdefault("reproducible", True)
    finding_dict.setdefault("poc_input", exploit_p)
    finding_dict.setdefault("poc_negative_input", base_p)

    # DOC-245: 业务意图对齐裁决门禁 (IntentAdjudicator)
    try:
        from core.planning import spec_harvester
        from core.planning import intent_adjudicator
        sup_root = Path(getattr(supervisor, "root", getattr(service, "root", Path.cwd())))
        loaded_spec = spec_harvester.load_business_spec(sup_root)
        if loaded_spec:
            admitted, reason = intent_adjudicator.adjudicate_finding_against_spec(
                finding_dict, loaded_spec.to_dict()
            )
            if not admitted:
                raise McpProtocolError(
                    f"INTENDED_BEHAVIOR_REJECTED: 行为符合规约设计预期 ({reason})，严禁作为漏洞立案",
                    code=-32003,
                )
    except McpProtocolError:
        raise
    except Exception:
        pass

    evidence_dict = {
        fp: {
            "command": "differential_verification",
            "exit_code": 0,
            "artifact_path": str(claim.artifact_root or "evidence.txt"),
            "artifact_sha256": hashlib.sha256(exploit_resp.encode("utf-8")).hexdigest(),
            "target_fingerprint": fp,
            "timestamp": time.time(),
            "worker_id": worker_key,
            "run_id": str(getattr(snapshot, "run_id", "run")),
            "round_no": claim.round_no,
            "lease_id": claim.lease_id,
            "status": "passed",
            "assertion_differential": diff_evidence.get("assertion_differential", ""),
        }
    }

    checkpoint_res = supervisor.record_checkpoint(
        worker_id=worker_key,
        findings=[finding_dict],
        coverage={},
        evidence=evidence_dict,
        signatures=[],
        notes={},
        life_delta=0.0,
        expected_revision=arguments.get("expected_revision"),
    )
    return {
        "status": "accepted",
        "finding_id": finding_dict["id"],
        "fingerprint": fp,
        "revision": getattr(checkpoint_res, "revision", None),
    }


SUPERVISOR_HANDLERS: dict[str, Any] = {
    "bugx_supervisor_start": handle_supervisor_start,
    "bugx_supervisor_reset_budget": handle_supervisor_reset_budget,
    "bugx_supervisor_findings": handle_supervisor_findings,
    "bugx_supervisor_rebind_target": handle_supervisor_rebind_target,
    "bugx_supervisor_sync_finding_decisions": handle_supervisor_sync_finding_decisions,
    "bugx_supervisor_verify_finding": handle_supervisor_verify_finding,
    "bugx_supervisor_confirm_finding": handle_supervisor_confirm_finding,
    "bugx_supervisor_bootstrap_coverage": handle_supervisor_bootstrap_coverage,
    "bugx_supervisor_status": handle_supervisor_status,
    "bugx_supervisor_gate": handle_supervisor_gate,
    "bugx_supervisor_coverage": handle_supervisor_coverage,
    "bugx_supervisor_claim": handle_supervisor_claim,
    "bugx_supervisor_admit_business": handle_supervisor_admit_business,
    "bugx_supervisor_reopen_finding": handle_supervisor_reopen_finding,
    "bugx_supervisor_worker_start": handle_supervisor_worker_start,
    "bugx_supervisor_tool_call": handle_supervisor_tool_call,
    "bugx_supervisor_heartbeat": handle_supervisor_heartbeat,
    "bugx_supervisor_checkpoint": handle_supervisor_checkpoint,
    "bugx_supervisor_salvage_checkpoint": handle_supervisor_salvage_checkpoint,
    "bugx_supervisor_begin_long_call": handle_supervisor_begin_long_call,
    "bugx_supervisor_run_sandboxed_probe": handle_supervisor_run_sandboxed_probe,
    "bugx_supervisor_submit_finding": handle_supervisor_submit_finding,
    "bugx_supervisor_next_round": handle_supervisor_next_round,
    "bugx_supervisor_bind_context": handle_supervisor_bind_context,
    "bugx_supervisor_finish": handle_supervisor_finish,
    "bugx_supervisor_fail": handle_supervisor_fail,
    "bugx_supervisor_recover": handle_supervisor_recover,
}
