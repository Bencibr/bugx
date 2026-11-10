#!/usr/bin/env python3
"""Supervisor finish 的终态编排边界。

该模块只编排已收集的生命周期门禁和终态事件载荷；锁、持久化以及状态
对象所有权仍由 ``audit_supervisor.Supervisor`` 负责。
"""

from __future__ import annotations

from typing import Any, Mapping

from .supervisor_models import InvalidCheckpoint, InvalidTransition, Status

from core.ledger import finding_confirmation

from core.supervisor.finish_policy import (
    classify_finish,
    classify_graph_finish,
    graph_completion,
)


def _record_finish_result(
    supervisor: Any,
    state: Any,
    now: float,
    *,
    result: Status,
    event_fields: Mapping[str, object],
    last_error: str | None = None,
) -> Any:
    """写入一次 finish 终态及其审计事件。"""
    state.status = result
    if last_error is not None:
        state.last_error = last_error
    state.completed_at = now
    supervisor._append_event(
        state, "run_finished", now, result=result.value, **dict(event_fields),
    )
    return state


def apply_finish_operation(
    supervisor: Any,
    state: Any,
    now: float,
    *,
    coverage_complete: bool | None,
    expected_revision: int | None,
) -> Any:
    """在 Supervisor 的 mutation 事务中执行 finish 终态编排。"""
    supervisor._check_revision(state, expected_revision)
    supervisor._refresh(state, now)
    supervisor._assert_target_fingerprint(state, now)
    if state.active_claims:
        raise InvalidTransition("仍有 active worker，不能 finish")
    # D2：COMPLETE 与 INCOMPLETE 都是 finish 已落盘的收尾终态；对 ACP 超时
    # 重试必须幂等返回同一 state（不重裁决、不追加事件、不推进 revision）。
    if state.status in {Status.COMPLETE, Status.INCOMPLETE}:
        return state
    if state.status == Status.FAILED:
        raise InvalidTransition("FAILED run 不能 finish")

    # Graph goals have their own completion contract and authoritative evidence
    # in RunState.agent_graph. Ordinary coverage, Finding confirmation, and the
    # historical mine/fix gates below remain byte-for-byte on the non-graph path.
    if state.agent_graph is not None:
        parallel = supervisor._close_parallel_round(state, now)
        deadline = supervisor._deadline_reasons(state, now)
        ready, graph_reason = graph_completion(state.agent_graph)
        decision = classify_graph_finish(
            ready=ready,
            reason=graph_reason,
            budget_exhausted=state.budget_exhausted,
            budget_reasons=state.budget_reasons,
            deadline_reasons=deadline,
        )
        graph_event = {
            "graph_id": state.agent_graph.graph_id,
            "graph_goal": state.agent_graph.goal,
            "graph_ready": ready,
            "graph_reason": graph_reason,
            "report_revision": state.agent_graph.report_revision,
            "parallel_gate": parallel,
        }
        if decision.status == Status.COMPLETE.value:
            return _record_finish_result(
                supervisor, state, now, result=Status.COMPLETE,
                event_fields={**graph_event, "coverage_complete": None},
            )
        if decision.status == Status.BUDGET_EXHAUSTED.value:
            reasons = decision.budget_reasons or tuple(state.budget_reasons) or ("deadline",)
            supervisor._set_budget_exhausted(
                state, now, reasons, event_type="run_finished",
            )
            return state
        return _record_finish_result(
            supervisor, state, now, result=Status.INCOMPLETE,
            last_error=decision.last_error,
            event_fields={
                **graph_event,
                "reason": decision.reason,
                "coverage_complete": None,
            },
        )

    parallel = supervisor._close_parallel_round(state, now)
    confirmation_gate = supervisor._refresh_confirmation_gate(state)
    open_findings = finding_confirmation.open_findings(
        state.findings, supervisor.workspace_root,
    )
    actual_complete = state.coverage_complete
    if not actual_complete:
        actual_complete = supervisor._check_session_coverage_ledger(state)
    if coverage_complete is True and not actual_complete:
        raise InvalidCheckpoint("调用方声称 coverage complete，但仍有未覆盖 unit")
    deadline = supervisor._deadline_reasons(state, now)
    soft_limits = supervisor._soft_limit_reasons(state)
    decision = classify_finish(
        actual_complete=actual_complete,
        requested_coverage_complete=coverage_complete,
        life_kind=state.life_kind,
        parallel_satisfied=bool(parallel["satisfied"]),
        parallel_reasons=parallel["reasons"],
        open_findings=open_findings,
        require_confirmation=state.budget.require_confirmation,
        confirmation_ready=bool(confirmation_gate.get("ready")),
        confirmation_findings=confirmation_gate.get("findings", []),
        confirmation_error=confirmation_gate.get("error"),
        budget_exhausted=state.budget_exhausted,
        budget_reasons=state.budget_reasons,
        deadline_reasons=deadline,
        soft_limit_reasons=soft_limits,
    )
    if decision.status == Status.COMPLETE.value:
        return _record_finish_result(
            supervisor, state, now, result=Status.COMPLETE,
            event_fields={
                "life_score": state.life_score,
                "coverage_complete": True,
            },
        )
    if decision.reason == "open_findings":
        return _record_finish_result(
            supervisor, state, now, result=Status.INCOMPLETE,
            last_error=decision.last_error,
            event_fields={
                "reason": "open_findings", "open_findings": open_findings,
                "life_score": state.life_score, "coverage_complete": True,
            },
        )
    if decision.reason == "concurrency_not_satisfied":
        return _record_finish_result(
            supervisor, state, now, result=Status.INCOMPLETE,
            last_error=decision.last_error,
            event_fields={
                "reason": "concurrency_not_satisfied",
                "parallel_gate": parallel,
                "life_score": state.life_score, "coverage_complete": True,
            },
        )
    if decision.reason == "finding_confirmation_pending":
        return _record_finish_result(
            supervisor, state, now, result=Status.INCOMPLETE,
            last_error=decision.last_error,
            event_fields={
                "reason": "finding_confirmation_pending",
                "confirmation_gate": confirmation_gate,
                "life_score": state.life_score, "coverage_complete": True,
            },
        )
    if decision.status == Status.BUDGET_EXHAUSTED.value:
        supervisor._set_budget_exhausted(
            state, now, decision.budget_reasons, event_type="run_finished",
        )
        return state
    return _record_finish_result(
        supervisor, state, now, result=Status.INCOMPLETE,
        event_fields={
            "life_score": state.life_score, "coverage_complete": False,
        },
    )


__all__ = ["apply_finish_operation"]
