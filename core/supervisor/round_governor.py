#!/usr/bin/env python3
"""core/supervisor/round_governor.py - Round 推进、预算重置与目标重绑。"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .supervisor_models import (
    InvalidCheckpoint,
    InvalidTransition,
    RunBudget,
    RunState,
    Status,
    SupervisorError,
    WorkerClaim,
)

from core.supervisor.observability import build_parallel_gate_snapshot

from core.supervisor import (
    context_sync,
    finish_sync,
    next_round_sync,
    observe_clock,
    resolve_session_coverage_ledger_path,
    session_coverage_complete,
)

if TYPE_CHECKING:
    from .audit_supervisor import Supervisor

logger = logging.getLogger(__name__)

def recalibrate_round_budget(
    self,
    recommended_seconds: float,
    *,
    expected_revision: int | None = None,
    reason: str = "adaptive_round_complexity",
) -> RunState:
    """动态校准当前 Round 预算上限 (DOC-150)。"""

    def operation(state: RunState, now: float) -> RunState:
        self._check_revision(state, expected_revision)
        self._ensure_live_for_work(state)
        self._recalibrate_round_budget(
            state, recommended_seconds, now, reason=reason,
        )
        return state

    return self._mutate(operation)  # type: ignore[return-value]

def _apply_next_round_operation(
    self,
    state: RunState,
    now: float,
    *,
    expected_revision: int | None = None,
    drain_dangling: bool = False,
) -> RunState:
    return next_round_sync.apply_next_round_operation(
        self,
        state,
        now,
        expected_revision=expected_revision,
        drain_dangling=drain_dangling,
    )

def next_round(
    self,
    *,
    expected_revision: int | None = None,
    drain_dangling: bool = False,
) -> RunState:
    """在所有当前 worker 已提交后进入下一轮；life 不参与判断。

    当 drain_dangling=True 时，若存在超时或失联的活跃 Worker，
    自动将其释放并排干（drained_at_round_boundary），避免孤儿 Worker 阻断全局战役。
    """
    return self._mutate(
        lambda s, now: self._apply_next_round_operation(
            s, now, expected_revision=expected_revision, drain_dangling=drain_dangling,
        )
    )  # type: ignore[return-value]

def bind_context(
    self,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
    round_id: str | None = None,
    life_id: str | None = None,
    plan_hash: str | None = None,
) -> RunState:
    """把同一 Worker 控制面绑定到下一 Life，不创建新的 Round。"""
    return self._mutate(
        lambda state, now: context_sync.apply_bind_context_operation(
            self,
            state,
            now,
            project_id=project_id,
            session_id=session_id,
            round_id=round_id,
            life_id=life_id,
            plan_hash=plan_hash,
        )
    )  # type: ignore[return-value]

def _check_session_coverage_ledger(self, state: RunState) -> bool:
    """检查 Session 级覆盖总账本是否涵盖了当前所有目标单元 (DOC-137 / B-5)。"""
    ledger_path = resolve_session_coverage_ledger_path(
        self.workspace_root,
        getattr(self, "coverage_ledger_path", None),
    )
    return session_coverage_complete(state, ledger_path)

def finish(
    self,
    *,
    coverage_complete: bool | None = None,
    expected_revision: int | None = None,
) -> RunState:
    """结束 run；coverage 未完成时绝不产生 COMPLETE。"""

    def operation(state: RunState, now: float) -> RunState:
        return finish_sync.apply_finish_operation(
            self, state, now,
            coverage_complete=coverage_complete,
            expected_revision=expected_revision,
        )

    return self._mutate(operation)  # type: ignore[return-value]

def parallel_gate(self) -> dict[str, object]:
    """返回当前 Life 的并发门禁结果，不推进 Round。"""

    def operation(state: RunState, now: float) -> dict[str, object]:
        self._refresh(state, now)
        return build_parallel_gate_snapshot(
            state,
            self._parallel_gate_result(state, now=now),
            self._target_gate_result(state),
        )

    return self._query_readonly(operation)  # type: ignore[return-value]

# DES-03：以下方法是 reset_budget 的状态变更体，由原先的嵌套闭包
# 提升而来；reset_budget 只保留归一化 + 适配调用。
def _apply_reset_budget_operation(
    self,
    state,
    now,
    *,
    expected_revision,
    force,
    next_budget,
    renew_wall_clock=False,
) -> RunState:
    """``reset_budget`` 的状态变更体（DES-03）。

    原先这是一段 81 行的嵌套闭包 ``operation``，捕获了
    ``reset_budget`` 的 3 个外层名字（含仅关键字参数）。
    闭包形式让这段逻辑无法单独命名、阅读、测试，也使外层方法
    膨胀到 94 行。现提升为具名方法：捕获的名字
    改为**仅关键字参数**传入，行为逐字不变。

    ``renew_wall_clock=True`` 时，全局墙钟锚点 ``started_at`` 被刷新
    为当前时刻，允许交互式多轮挖掘在墙钟耗尽后继续。
    """
    self._check_revision(state, expected_revision)
    if state.status == Status.COMPLETE:
        raise InvalidTransition("COMPLETE run 不允许 reset_budget")
    if state.status == Status.FAILED:
        raise InvalidTransition("FAILED run 不允许 reset_budget")
    # LOG-07：finish() 在三条 INCOMPLETE 分支都会写 completed_at 并发出
    # run_finished（并发门禁未满足 / Finding 待确认 / 覆盖未完成），因此
    # 「INCOMPLETE 且 completed_at 已写」是一个已经收尾的终态。若在此重开
    # 预算，会把 completed_at 擦除、状态回退为 RUNNING，而 history 里仍留着
    # run_finished —— 终态记录与状态机自相矛盾。真正的可续状态是
    # BUDGET_EXHAUSTED（由 _set_budget_exhausted 产生，不写 completed_at）。
    if state.completed_at is not None:
        raise InvalidTransition(
            "已结束的 INCOMPLETE run（completed_at 已写）不允许 reset_budget"
        )
    # D3：回拨防护——预算判定与墙钟锚点重置统一使用有效 now
    # （不早于本 run 已观测到的高水位），时钟回拨不得把已耗尽的
    # max_wall_seconds 还回来，也不得把重置后的锚点写到过去。
    now = observe_clock(state, now)
    if (not renew_wall_clock
            and now - state.started_at >= next_budget.max_wall_seconds):
        raise InvalidTransition(
            "全局 max_wall_seconds 已耗尽，不允许 reset_budget"
            "（使用 --renew-wall-clock 可续期全局墙钟）"
        )
    if state.active_claims:
        if force:
            self._release_active_claims(
                state, now, reason="force_reset_budget",
            )
        else:
            raise InvalidTransition("仍有 active worker，不能 reset_budget")
    self._assert_target_fingerprint(state, now)
    if next_budget.max_rounds < state.round_no:
        raise InvalidCheckpoint(
            "新 budget.max_rounds 不能小于当前 round_no，避免擦除轮次历史"
        )
    self._validate_parallel_capacity(next_budget, len(state.coverage))
    old = state.budget.to_dict()
    previous_parallel_proof = {
        "budget_epoch": state.budget_epoch,
        "round_no": state.round_no,
        "round_max_active_workers": state.round_max_active_workers,
        "round_overlap_seconds": state.round_overlap_seconds,
        "parallel_history": copy.deepcopy(state.parallel_history[-1:]),
    }
    previous_usage = {
        "budget_epoch": state.budget_epoch,
        # Keep the legacy per-epoch keys stable for existing consumers.
        "tool_calls": state.budget_tool_calls,
        "cost_units": state.budget_cost_units,
        # These are observed totals, never synthesized from the new
        # budget.  reset_budget deliberately leaves the cumulative fields
        # untouched while only the epoch counters return to zero.
        "cumulative_tool_calls": state.tool_calls,
        "cumulative_cost_units": state.cost_units,
        "findings": len(state.findings),
        "coverage": dict(state.coverage),
        "history_length": len(state.history),
    }
    state.budget = next_budget
    state.budget_epoch += 1
    state.budget_started_at = now
    state.round_started_at = now
    if renew_wall_clock:
        state.started_at = now
    state.budget_tool_calls = 0
    state.budget_cost_units = 0.0
    state.budget_exhausted = False
    state.budget_reasons = []
    state.parallel_proof_history.append({
        **previous_parallel_proof,
    })
    state.round_max_active_workers = 0
    state.round_overlap_seconds = 0.0
    state.round_parallel_active_since = None
    state.adaptive_concurrency_window = None
    state.adaptive_success_streak = 0
    state.adaptive_failure_streak = 0
    state.completed_at = None
    state.last_error = None
    state.status = Status.RUNNING
    self._append_event(
        state, "budget_reset", now, old_budget=old,
        new_budget=next_budget.to_dict(), previous_usage=previous_usage,
        preserved_findings=len(state.findings),
        preserved_coverage=dict(state.coverage),
        preserved_parallel_proof=previous_parallel_proof,
        renew_wall_clock=renew_wall_clock,
    )
    return state

def reset_budget(
    self,
    budget: RunBudget | None = None,
    *,
    expected_revision: int | None = None,
    force: bool = False,
    renew_wall_clock: bool = False,
) -> RunState:
    """开启新预算 epoch；绝不清理 findings、coverage 或 history。

    ``renew_wall_clock=True`` 时刷新全局墙钟锚点 ``started_at``，
    允许交互式多轮挖掘场景在墙钟耗尽后继续。默认 ``False`` 保持
    原有行为不变（新上限 bound 到 ``min(new, old)``）。
    """

    next_budget = budget or self._configured_budget

    def operation(state: RunState, now: float) -> RunState:
        if renew_wall_clock:
            bounded_budget = next_budget
        else:
            bounded_budget = replace(
                next_budget,
                max_wall_seconds=min(
                    next_budget.max_wall_seconds,
                    state.budget.max_wall_seconds,
                ),
            )
        return self._apply_reset_budget_operation(
            state=state,
            now=now,
            expected_revision=expected_revision,
            force=force,
            next_budget=bounded_budget,
            renew_wall_clock=renew_wall_clock,
        )

    return self._mutate(operation)  # type: ignore[return-value]

