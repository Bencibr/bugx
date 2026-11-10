#!/usr/bin/env python3
"""Supervisor 下一 Round 的状态推进与事件编排边界。

入口校验、锁和持久化仍由 ``audit_supervisor.Supervisor`` 负责；本模块只
组合已校验的 round 参数和现有 lease/budget 原语。
"""

from __future__ import annotations

try:
    from .supervisor_models import (
        BudgetExhausted,
        ConcurrencyNotSatisfied,
        InvalidTransition,
        RunState,
        Status,
    )
    from core.supervisor.budget_governor import (
        ROUND_HEARTBEAT_TIMEOUT_SECONDS,
        close_parallel_round,
        deadline_reasons,
        ensure_live_for_work,
        observe_clock,
        set_budget_exhausted,
        soft_limit_reasons,
    )
    from core.supervisor.lease_manager import (
        drain_dangling_claims,
        reclaim_orphan_leases,
    )
except ImportError:  # pragma: no cover
    from .supervisor_models import (
        BudgetExhausted,
        ConcurrencyNotSatisfied,
        InvalidTransition,
        RunState,
        Status,
    )
    from core.supervisor.budget_governor import (
        ROUND_HEARTBEAT_TIMEOUT_SECONDS,
        close_parallel_round,
        deadline_reasons,
        ensure_live_for_work,
        observe_clock,
        set_budget_exhausted,
        soft_limit_reasons,
    )
    from core.supervisor.lease_manager import (
        drain_dangling_claims,
        reclaim_orphan_leases,
    )


def apply_next_round_operation(
    supervisor,
    state: RunState,
    now: float,
    *,
    expected_revision: int | None = None,
    drain_dangling: bool = False,
) -> RunState:
    """``next_round`` 的状态变更体（DES-03）。"""
    supervisor._check_revision(state, expected_revision)
    supervisor._refresh(state, now)
    # D3：refresh 已把水位推进到 max(now, 高水位)；新轮的 round_started_at
    # 等墙钟锚点必须用有效 now，否则时钟回拨会让新轮凭空多出一段时间。
    now = observe_clock(state, now)
    supervisor._assert_target_fingerprint(state, now)
    if state.active_claims:
        # DOC-196: 孤儿租约扫描与死锁自愈
        reclaim_orphan_leases(state, now)
        if drain_dangling:
            drain_dangling_claims(
                state, now, ROUND_HEARTBEAT_TIMEOUT_SECONDS,
            )
            if state.active_claims:
                still_active = list(state.active_claims.keys())
                raise InvalidTransition(
                    f"仍有 active worker 无法排干 ({still_active})，必须先 serial submit"
                )
        else:
            if state.active_claims:
                raise InvalidTransition("仍有 active worker，必须先 serial submit")
    ensure_live_for_work(state)
    parallel = close_parallel_round(state, now)
    if state.life_kind != "fix" and not parallel["satisfied"]:
        failure = ConcurrencyNotSatisfied(
            "无法进入下一 Life：并发门禁未满足（"
            + ", ".join(str(item) for item in parallel["reasons"])
            + ")"
        )
        # DOC-203 D4：把完整 gate dict 随异常携带，供
        # handle_supervisor_next_round 的 gate_failure_mode="report"
        # 非交互出口构造结构化结果。
        failure.gate = parallel  # type: ignore[attr-defined]
        raise failure
    reasons = [
        *deadline_reasons(state, now),
        *soft_limit_reasons(state),
    ]
    if state.round_no >= state.budget.max_rounds:
        reasons.append("max_rounds")
    if reasons:
        set_budget_exhausted(state, now, reasons)
        raise BudgetExhausted("无法进入下一轮: " + ", ".join(reasons))
    # DOC-231 A12: 彻底移除可变 finding 诱导的折扣套利漏洞，统一固定扣分 -1.0
    life_cost = -1.0
    state.life_score = max(0.0, state.life_score + life_cost)
    state.round_no += 1
    state.round_version = getattr(state, "round_version", 0) + 1
    # DOC-205 S2: 新 Round 即新调度代际，推进 scheduler_epoch 解冻上一代际的临时重试计数
    state.scheduler_epoch = getattr(state, "scheduler_epoch", 0) + 1
    state.round_started_at = now
    state.round_max_active_workers = 0
    state.round_overlap_seconds = 0.0
    state.round_parallel_active_since = None
    state.status = Status.NEXT_ROUND
    supervisor._append_event(
        state, "next_round", now, round_no=state.round_no,
        life_score=state.life_score, life_round_cost=life_cost,
        round_version=state.round_version,
    )
    return state


__all__ = ["apply_next_round_operation"]
