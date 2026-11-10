#!/usr/bin/env python3
"""系统级环境崩溃的级联熔断（跨单元 >= 3 命中致命基础设施错误，DOC-242）。

为什么独立成模块（架构收敛，2026-10-26）：该判定被 ``lease_manager``（租约过期路径）
与 ``budget_governor``（并发调和路径）**双向**共享，此前只能靠
``lease_manager → budget_governor`` 的延迟 import 实现，于是两者互为依赖。
下沉后依赖方向唯一：

    lease_manager ──┐
                    ├──> systemic_failure ──> claim_events ──> supervisor_models
    budget_governor ┘
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .supervisor_models import RunState, Status

try:
    from .claim_events import append_event
except ImportError:  # pragma: no cover
    from core.supervisor.claim_events import append_event


SYSTEMIC_FAILURE_MARKERS = (
    "command not found",
    "no such file or directory",
    "permission denied",
    "cannot allocate memory",
    "cannot execute binary file",
    "exit code 127",
    "exit code 126",
    "exit 127",
    "exit 126",
)


def set_budget_exhausted(
    state: RunState,
    now: float,
    reasons: Iterable[str],
    *,
    event_type: str = "budget_exhausted",
) -> None:
    """把 run 标记为预算耗尽并追加事件（唯一写入实现）。"""

    merged = list(dict.fromkeys(
        [*state.budget_reasons, *(str(item) for item in reasons)]
    ))
    state.budget_reasons = merged
    state.budget_exhausted = True
    # D2：INCOMPLETE 是收尾终态，预算记账不得把它复活为 BUDGET_EXHAUSTED。
    if state.status not in {
        Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
    }:
        state.status = Status.BUDGET_EXHAUSTED
    append_event(
        state, event_type, now, reasons=list(merged),
        round_no=state.round_no,
    )


def check_and_record_systemic_failure(
    state: RunState,
    unit_ids: Sequence[str],
    error: str,
    now: float,
) -> bool:
    """检查跨单元系统级环境崩溃；跨 >=3 个不同单元连续命中致命基础设施错误 ⇒ 全局级联熔断。"""

    if not error or not isinstance(error, str):
        return False
    lowered = error.casefold()
    matched_marker = None
    for marker in SYSTEMIC_FAILURE_MARKERS:
        if marker in lowered:
            matched_marker = marker
            break
    if not matched_marker:
        return False

    if not hasattr(state, "systemic_failure_units") or state.systemic_failure_units is None:
        state.systemic_failure_units = {}

    for uid in unit_ids:
        state.systemic_failure_units[str(uid)] = matched_marker

    if len(state.systemic_failure_units) >= 3:
        reason = (
            f"systemic_environment_collapse: {matched_marker} across "
            f"{len(state.systemic_failure_units)} units"
        )
        set_budget_exhausted(state, now, [reason])
        append_event(
            state,
            "systemic_failure_circuit_breaker_triggered",
            now,
            marker=matched_marker,
            affected_units=list(state.systemic_failure_units.keys()),
            reason=reason,
        )
        return True
    return False
