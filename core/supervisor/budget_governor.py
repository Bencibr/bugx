#!/usr/bin/env python3
"""core/supervisor/budget_governor.py - Supervisor 预算管控与并发协调服务 (DOC-070)。

负责轮次截止期、软硬预算监控、并发状态对齐、僵尸检测及预算耗尽状态转换。
"""

from __future__ import annotations

import dataclasses
import re
import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


ROUND_HEARTBEAT_TIMEOUT_SECONDS = 450.0
LONG_CALL_MAX_SILENCE_SECONDS = 600.0

# 系统级熔断与预算耗尽写入已下沉到 systemic_failure / claim_events（2026-10-26
# 架构收敛）：budget_governor 与 lease_manager 的双向依赖由此消除。这里 re-export，
# 既有 `from .budget_governor import ...` 调用点保持不变。
try:
    from .claim_events import append_event
    from .systemic_failure import (
        SYSTEMIC_FAILURE_MARKERS,
        check_and_record_systemic_failure,
        set_budget_exhausted,
    )
except ImportError:  # pragma: no cover
    from core.supervisor.claim_events import append_event
    from core.supervisor.systemic_failure import (
        SYSTEMIC_FAILURE_MARKERS,
        check_and_record_systemic_failure,
        set_budget_exhausted,
    )

from .supervisor_models import (
    BudgetExhausted,
    InvalidCheckpoint,
    InvalidTransition,
    MAX_LEASE_SECONDS,
    MAX_WORKER_ATTEMPTS,
    TERMINAL_COVERED,
    RunBudget,
    RunState,
    Status,
    WorkerClaim,
    workspace_fingerprint,
)

from core.supervisor.lease_manager import (
    eligible_units,
    mark_claim_expired,
    mark_worker_attempts_exhausted,
    record_expiry_accounting,
)


def reconcile_parallel(state: RunState, now: float) -> None:
    """从持久化执行窗口重建本轮真实重叠，跨进程 resume 也不丢失。"""
    intervals: list[tuple[float, float]] = []
    for claim in state.claims.values():
        if claim.round_no != state.round_no or claim.execution_started_at is None:
            continue
        start = float(claim.execution_started_at)
        if claim.execution_state == "running":
            # B5：开区间右端必须夹到真实租约边界，否则僵尸 running claim 的
            # 重叠时长会随等待时间线性增长（并发证据可被"等得够久"伪造）。
            boundaries = [now]
            if claim.lease_expires_at is not None:
                boundaries.append(float(claim.lease_expires_at))
            if claim.total_lease_deadline is not None:
                boundaries.append(float(claim.total_lease_deadline))
            end = min(boundaries)
        else:
            end = float(
                claim.execution_finished_at
                or claim.submitted_at
                or claim.lease_expires_at
            )
        if end > start:
            intervals.append((start, end))
    events: dict[float, int] = {}
    for start, end in intervals:
        events[start] = events.get(start, 0) + 1
        events[end] = events.get(end, 0) - 1
    active_count = 0
    peak = sum(
        claim.active and claim.execution_state == "running"
        for claim in state.claims.values()
        if claim.round_no == state.round_no
    )
    overlap = 0.0
    points = sorted(events)
    for index, point in enumerate(points):
        active_count += events[point]
        peak = max(peak, active_count)
        if index + 1 < len(points) and active_count >= 2:
            overlap += max(0.0, points[index + 1] - point)
    state.round_max_active_workers = peak
    state.round_overlap_seconds = round(overlap, 6)
    current_running = sum(
        claim.active and claim.execution_state == "running"
        and claim.round_no == state.round_no
        for claim in state.claims.values()
    )
    # LOG-10：``current_running`` 必须与 ``peak`` / ``round_max_active_workers``
    # 同口径（只统计当前轮）。历史实现跨轮统计，导致上一轮遗留的 running claim
    # 就能把本轮 ``round_parallel_active_since`` 置位，误报「本轮并发达标」。
    state.round_parallel_active_since = now if current_running >= 2 else None


def _record_adaptive_outcome(state: RunState, outcome: str) -> None:
    try:
        from . import task_scheduler
        task_scheduler.AdaptiveConcurrencyController(
            min_concurrency=state.budget.min_concurrency,
            max_concurrency=state.budget.max_concurrency_ceiling,
        ).record_outcome(state, outcome)
    except Exception:
        # Scheduling feedback is advisory; never hide the lease/claim state.
        pass


def _renew_expired_live_claim(
    state: RunState, claim: WorkerClaim, now: float, mono_now: float,
) -> bool:
    """到期时保留仍有进展证据的 Worker，并延长原租约时长。

    A7/B1：活性与上限判定只读单调时钟；``renewal_count`` 不再参与终止判定，
    单 claim 寿命只由 ``total_lease_deadline`` 决定。
    """
    if claim.execution_state != "running" or claim.last_heartbeat_mono is None:
        return False
    if mono_now - claim.last_heartbeat_mono > ROUND_HEARTBEAT_TIMEOUT_SECONDS:
        return False
    # 只保留总租约截止期这一个寿命上限，杜绝假活无限空转续期
    deadline_mono = claim.total_lease_deadline_mono
    if deadline_mono is not None and mono_now >= deadline_mono:
        claim.preempted_reason = "total_lease_deadline_exceeded"
        return False

    if claim.process_pid is not None:
        try:
            from . import task_scheduler
            if not task_scheduler.ZombieDetector.is_pid_alive(claim.process_pid):
                return False
        except Exception:
            return False
    previous_expires_at = claim.lease_expires_at
    renewal_seconds = max(1.0, claim.lease_seconds)
    next_expires_at = now + renewal_seconds
    next_expires_mono = mono_now + renewal_seconds
    if claim.total_lease_deadline is not None:
        next_expires_at = min(next_expires_at, claim.total_lease_deadline)
    if deadline_mono is not None:
        next_expires_mono = min(next_expires_mono, deadline_mono)
    if next_expires_mono <= mono_now:
        claim.preempted_reason = "total_lease_deadline_exceeded"
        return False
    claim.lease_expires_at = max(claim.lease_expires_at, next_expires_at)
    claim.lease_expires_mono = max(
        claim.lease_expires_mono or 0.0, next_expires_mono,
    )
    claim.renewal_count += 1
    claim.preempted_reason = None
    append_event(
        state,
        event_type="worker_lease_renewed_after_timeout",
        now=now,
        worker_id=claim.worker_id,
        lease_id=claim.lease_id,
        execution_id=claim.execution_id,
        previous_lease_expires_at=previous_expires_at,
        lease_expires_at=claim.lease_expires_at,
        reason=f"fresh_heartbeat_renewal_{claim.renewal_count}",
    )
    return True


def _serial_budget_is_legal(state: RunState) -> bool:
    """Recognize the bounded serial budget used by a single-task host."""
    budget = getattr(state, "budget", None)
    reason = getattr(budget, "serial_reason", None)
    return (
        getattr(budget, "require_parallel", True) is False
        and isinstance(reason, str)
        and bool(reason.strip())
    )


_UNIT_PREFIX_RE = re.compile(r"^(?:内部|业务):\d+:")


#: 租约自适应下限：与 ``ZombieDetector`` 的心跳断流窗口（450s）对齐。
#: 低于它没有意义——回收判定本来就以该窗口为最快触发点。
ADAPTIVE_LEASE_FLOOR_SECONDS = 450.0
#: 无历史观测时的默认租约（保持历史行为，避免首轮突变）。
DEFAULT_LEASE_SECONDS = 1800.0


def adaptive_lease_seconds(state: RunState) -> float:
    """按**观测到的执行耗时**推导租约长度，替代写死的 1800s。

    观测源：``SchedulingMetrics``（``total_execution_seconds`` / ``max_execution_seconds``，
    由 checkpoint 侧持续记账）。规则：``3 × max(平均耗时, 最长耗时)``，再夹到
    ``[ADAPTIVE_LEASE_FLOOR_SECONDS, MAX_LEASE_SECONDS]``。

    为什么是「3×」与「下界 450」：租约是 worker 对自身执行时长的申报；不超过 3 倍
    实测峰值就没有容错余量，而低于心跳断流窗口会让「正在干活但没打心跳」的 worker
    被误杀（实测宿主的 Task 子代理常规就不打心跳）。

    观测不足（已完成 < 2）时返回默认值——自适应只在有事实时生效，不猜。
    """

    if not state or state.scheduling_metrics is None:
        return DEFAULT_LEASE_SECONDS
    metrics = state.scheduling_metrics
    completed = int(getattr(metrics, "total_completed", 0) or 0)
    executed = float(getattr(metrics, "total_execution_seconds", 0.0) or 0.0)
    if completed < 2 or executed <= 0:
        return DEFAULT_LEASE_SECONDS
    average = executed / completed
    peak = max(average, float(getattr(metrics, "max_execution_seconds", 0.0) or 0.0))
    return max(ADAPTIVE_LEASE_FLOOR_SECONDS, min(MAX_LEASE_SECONDS, 3.0 * peak))


def _resource_domains(state: RunState) -> int:
    """可领取单元能形成的**并发资源域**数量（同文件单元共享一个域）。

    claim 层给每个单元派生 ``file:<path>`` 互斥键，同一源文件的两个单元因此
    无法同时激活。若全部待领取单元都落在同一个文件里，并发在物理上不可行，
    「要求 peak>=2」就成了不可满足的门禁——实测 2 个 symbol 单元同属
    ``probe/app.py`` 时，parallel_gate 必然残留 ``parallel_peak_below_two``，
    整轮被判 FAILED（正确行为应是按串行完成）。
    """

    domains: set[str] = set()
    for unit in sorted(eligible_units(state)):
        status = (state.coverage or {}).get(unit)
        if status in TERMINAL_COVERED:
            continue
        # 单元 ID 形如 ``内部:1:probe/app.py::read_app``：先剥掉 ``阶段:N:`` 前缀，
        # 再去 ``::symbol``，剩下的才是 claim 层派生 ``file:`` 互斥键的那个文件路径。
        scope = _UNIT_PREFIX_RE.sub("", unit)
        derived = scope.split("::", 1)[0] if "::" in scope else scope
        domains.add(derived)
    return len(domains)


def parallel_required(state: RunState) -> bool:
    if getattr(state, "life_kind", "mine") == "fix":
        return False
    if _serial_budget_is_legal(state):
        return False
    if _resource_domains(state) < 2:
        # 并发在物理上不可行（单元共享同一资源域）：不把 peak>=2 当门禁。
        return False
    return (
        state.budget.require_parallel
        and len(eligible_units(state)) >= 2
    )


def parallel_gate_result(
    state: RunState,
    *,
    now: float | None = None,
) -> dict[str, object]:
    # An expired claim is a retry debt, not a permanent round failure.
    # Once a later attempt for the same unit is active or submitted, the
    # old expired claim remains in history but must no longer block the
    # round forever. Looking at the latest attempt per unit also handles
    # a second expiry correctly: only that newest expiry remains a gate.
    # DOC-231 A2/D04: 先隔离代际，再在当前代内选最新 attempt，防止旧代的高 attempt 遮蔽当前代的过期债务。
    # 跨代 expired claim 已无法被接手，语义裁定：scheduler_epoch 是调度代际，上一代的过期债由新一代重新派发，
    # 不再作为本代的门禁理由。
    state_epoch = getattr(state, "scheduler_epoch", None)
    latest_by_unit: dict[str, WorkerClaim] = {}
    for claim in state.claims.values():
        if claim.round_no != state.round_no:
            continue
        claim_epoch = getattr(claim, "scheduler_epoch", None)
        if state_epoch is not None and claim_epoch not in (None, state_epoch):
            # 排除非本代的历史 claim，防止旧代的高 attempt 遮蔽当前代债务
            continue
        for unit_id in claim.unit_ids:
            previous = latest_by_unit.get(unit_id)
            if previous is None or (
                claim.attempt, claim.claimed_at
            ) > (previous.attempt, previous.claimed_at):
                latest_by_unit[unit_id] = claim

    expired = sorted({
        claim.worker_id for claim in latest_by_unit.values()
        if claim.state == "expired"
    })
    active = sorted(state.active_claims)
    running = sorted(
        claim.worker_id for claim in state.claims.values()
        if claim.active and claim.execution_state == "running"
    )
    submitted = sorted(
        claim.worker_id for claim in state.claims.values()
        if claim.round_no == state.round_no
        and claim.state == "submitted"
        and claim.submitted_at is not None
    )
    # Do not trust the mutable summary counters alone.  A real parallel proof
    # needs two distinct workers that both completed a non-empty checkpoint,
    # plus overlapping persisted execution windows.
    intervals: list[tuple[float, float]] = []
    observed_peak = sum(
        1
        for claim in state.claims.values()
        if claim.round_no == state.round_no
        and claim.active
        and claim.execution_state == "running"
    )
    progress_workers: list[str] = []
    for claim in state.claims.values():
        if claim.round_no != state.round_no or claim.execution_started_at is None:
            continue
        if claim.state == "submitted" and claim.submitted_at is not None:
            end = claim.execution_finished_at or claim.submitted_at
            if end > claim.execution_started_at:
                intervals.append((claim.execution_started_at, end))
            if claim.last_checkpoint_digest:
                progress_workers.append(claim.worker_id)
        elif claim.active and claim.execution_state == "running":
            # A lease expiry is a promise about the future, not evidence that
            # the worker actually ran until that point.  Use the observation
            # boundary after refresh when available; direct callers without a
            # clock only get the last persisted heartbeat boundary.
            end = (
                min(now, claim.lease_expires_at)
                if now is not None
                else (claim.last_heartbeat_at or claim.execution_started_at)
            )
            if end > claim.execution_started_at:
                intervals.append((claim.execution_started_at, end))
        elif claim.execution_finished_at is not None:
            intervals.append((claim.execution_started_at, claim.execution_finished_at))
    observed_overlap = 0.0
    points: dict[float, int] = {}
    for start, end in intervals:
        if end <= start:
            continue
        points[start] = points.get(start, 0) + 1
        points[end] = points.get(end, 0) - 1
    current = 0
    ordered_points = sorted(points)
    for index, point in enumerate(ordered_points):
        current += points[point]
        observed_peak = max(observed_peak, current)
        if index + 1 < len(ordered_points) and current >= 2:
            observed_overlap += ordered_points[index + 1] - point
    progress_workers = sorted(set(progress_workers))
    required = parallel_required(state)
    reasons: list[str] = []
    if (
        getattr(state.budget, "require_parallel", True) is False
        and not _serial_budget_is_legal(state)
    ):
        reasons.append("serial_budget_invalid")
    if active:
        reasons.append("active_workers_remaining")
    if expired:
        reasons.append("expired_workers_require_retry")
    if required and observed_peak < 2:
        reasons.append("parallel_peak_below_two")
    if required and observed_peak >= 2 and observed_overlap <= 0:
        reasons.append("parallel_overlap_missing")
    if required and observed_peak >= 2 and len(progress_workers) < 2:
        reasons.append("parallel_workers_missing_checkpoint")
    return {
        "round": state.round_no,
        "required": required,
        "satisfied": not reasons,
        "max_active_workers": observed_peak,
        "overlap_seconds": round(observed_overlap, 3),
        "active_workers": active,
        "running_workers": running,
        "submitted_workers": submitted,
        "progress_workers": progress_workers,
        "expired_workers": sorted(expired),
        "serial_reason": state.budget.serial_reason,
        "reasons": reasons,
    }


def target_gate_result(workspace_root: Path, state: RunState) -> dict[str, object]:
    """Return a read-only target-drift marker for status/gate consumers."""
    expected = state.target_fingerprint
    if not expected:
        return {"target_drift": False}
    actual = workspace_fingerprint(workspace_root)
    if actual == expected:
        return {"target_drift": False}
    return {
        "target_drift": True,
        "expected_target_fingerprint": expected,
        "actual_target_fingerprint": actual,
    }


def close_parallel_round(state: RunState, now: float) -> dict[str, object]:
    reconcile_parallel(state, now)
    result = parallel_gate_result(state, now=now)
    state.parallel_history = [
        *state.parallel_history,
        dict(result),
    ][-100:]
    append_event(
        state, "parallel_round_closed", now,
        **{key: value for key, value in result.items() if key != "satisfied"},
        satisfied=result["satisfied"],
    )
    return result


def observe_clock(state: RunState, now: float) -> float:
    """墙钟回拨防护：返回不早于已观测高水位的有效 now (D3)。

    预算的墙钟锚（``started_at`` / ``round_started_at``）是持久化字段，
    系统时钟回拨会让 ``now - 锚点`` 变小，等于凭空把已耗掉的墙钟预算
    还回来。这里把有效 now 钉在「本 run 曾观测到的最晚时刻」
    （``clock_high_water``，随快照持久化），使已计时长的记账只增不减。

    下界还包含全部已持久化的墙钟锚：旧快照没有水位字段（升级首读），
    此时至少保证 ``elapsed >= 0``，回拨不可能把计时变成负数再重新开始。

    这是下界而非权威时钟：``now`` 前跳时照常推进水位（前跳导致预算
    提前耗尽属于 fail-closed 方向，由租约的 mono 权威兜住 Worker 误杀）。
    """
    effective = float(now)
    high_water = float(getattr(state, "clock_high_water", 0.0) or 0.0)
    for name in ("started_at", "budget_started_at", "round_started_at"):
        anchor = float(getattr(state, name, 0.0) or 0.0)
        if anchor > high_water:
            high_water = anchor
    if effective < high_water:
        effective = high_water
    # 水位只增不减；被锚点抬高的下界同样落盘，供跨进程复核。
    state.clock_high_water = effective
    return effective


def deadline_reasons(state: RunState, now: float) -> list[str]:
    now = observe_clock(state, now)
    reasons: list[str] = []
    # max_wall_seconds is the run-wide hard cap.  budget_started_at advances
    # for each recoverable epoch, so using it here would let reset_budget
    # restart the global wall clock.
    if now - state.started_at >= state.budget.max_wall_seconds:
        reasons.append("max_wall_seconds")
    if now - state.round_started_at >= state.budget.max_round_wall_seconds:
        # DOC-196: 在标记超时前，尝试双重进展活性续期
        if not _renew_round_budget_if_live(state, now):
            reasons.append("max_round_wall_seconds")
    return reasons


def _round_has_live_activity(state: RunState, now: float, now_mono: float | None = None) -> bool:
    """Return whether the current Round has evidence it is progressing (DOC-196 / DOC-242).

    A fresh running-worker heartbeat is sufficient liveness evidence.  With no
    active claims, a submitted claim means the Round is ready for the host to
    advance, not hung.  Expired/claimed-only workers and empty rounds do not
    renew the deadline.
    """
    if now_mono is None:
        now_mono = time.monotonic()
    active = list(state.active_claims.values())
    if not active:
        return any(
            claim.round_no == state.round_no and claim.state == "submitted"
            for claim in state.claims.values()
        )
    for claim in active:
        if claim.round_no != state.round_no or claim.execution_state != "running":
            continue
        # DOC-203 D3 / L2-Timeout / DOC-242:
        # 若 worker 声明了长调用且租约未到期，长调用窗口在 LONG_CALL_MAX_SILENCE_SECONDS 内视为活跃；
        # 若静默超限，必须具有非零 CPU 活跃度，否则判定死锁不再续期。
        in_long_call = (
            getattr(claim, "long_call_count", 0) > 0
            and claim.lease_expires_mono is not None
            and now_mono < claim.lease_expires_mono
        )
        if in_long_call:
            last_comm_mono = getattr(claim, "last_heartbeat_mono", None) or getattr(claim, "claimed_mono", None)
            if last_comm_mono is not None:
                long_silence = now_mono - last_comm_mono
            else:
                last_comm = claim.last_heartbeat_at or claim.claimed_at
                long_silence = now - last_comm if last_comm else 0.0
            if long_silence > LONG_CALL_MAX_SILENCE_SECONDS:
                process_pid = claim.process_pid
                if process_pid is not None:
                    try:
                        from . import task_scheduler
                        if not task_scheduler.ZombieDetector.is_pid_cpu_active(process_pid):
                            continue
                    except Exception:
                        pass
        else:
            # A7：心跳新鲜度同样只读单调时钟，墙钟跳变不改变活性判定。
            last_heartbeat_mono = claim.last_heartbeat_mono
            is_fresh_heartbeat = (
                last_heartbeat_mono is not None
                and (now_mono - last_heartbeat_mono <= ROUND_HEARTBEAT_TIMEOUT_SECONDS)
            )
            if not is_fresh_heartbeat:
                continue
        process_pid = claim.process_pid
        if process_pid is not None:
            try:
                from . import task_scheduler
                if not task_scheduler.ZombieDetector.is_pid_alive(process_pid):
                    continue
            except Exception:
                continue
        # B1：只要总截止期未到即视为有效进展（续租次数不参与终止判定）。
        deadline_mono = claim.total_lease_deadline_mono
        if deadline_mono is None or now_mono < deadline_mono:
            return True
    return False


def _renew_round_budget_if_live(
    state: RunState, now: float, *, now_mono: float | None = None,
) -> bool:
    """Roll only the single-Round wall clock when progress is still live (DOC-196)."""
    now = observe_clock(state, now)
    if now - state.started_at >= state.budget.max_wall_seconds:
        return False
    if now - state.round_started_at < state.budget.max_round_wall_seconds:
        return True
    if not _round_has_live_activity(state, now, now_mono):
        return False
    reasons = set(state.budget_reasons)
    if reasons - {"max_round_wall_seconds"}:
        return False
    previous = state.round_started_at
    state.round_started_at = now
    state.budget_reasons = [
        reason for reason in state.budget_reasons
        if reason != "max_round_wall_seconds"
    ]
    state.budget_exhausted = bool(state.budget_reasons)
    if state.status == Status.BUDGET_EXHAUSTED and not state.budget_exhausted:
        state.status = Status.RUNNING
    append_event(
        state, "budget_round_renewed", now,
        round_no=state.round_no,
        previous_round_started_at=previous,
        next_round_started_at=now,
        reason="live_worker_or_submitted_progress",
    )
    return True


def soft_limit_reasons(state: RunState) -> list[str]:
    reasons: list[str] = []
    if state.budget_tool_calls >= state.budget.max_tool_calls:
        reasons.append("max_tool_calls")
    if state.budget_cost_units >= state.budget.max_cost_units:
        reasons.append("max_cost_units")
    return reasons


def validate_parallel_capacity(budget: RunBudget, unit_count: int) -> None:
    """Reject parallel runs that cannot schedule two eligible units."""
    if budget.require_parallel and unit_count >= 2 and budget.max_concurrency < 2:
        raise InvalidCheckpoint(
            "至少有两个覆盖单元时，max_concurrency 必须 >= 2；"
            "如确需串行，请提供 serial_reason"
        )





def refresh(
    state: RunState,
    now: float,
    workspace_root: Path | None = None,
    now_mono: float | None = None,
) -> None:
    if now_mono is None:
        now_mono = time.monotonic()
    # D3：一次 refresh 内所有墙钟写入（事件时间戳、租约展示期、预算锚）
    # 都使用回拨防护后的有效 now，避免记账时间倒退。
    now = observe_clock(state, now)
    for worker_id, claim in list(state.claims.items()):
        if not claim.active:
            continue
        if claim.is_expired(now_mono=now_mono) and _renew_expired_live_claim(
            state, claim, now, now_mono,
        ):
            continue
        # 租约是否已实际失效，必须在任何状态突变**之前**取样：僵尸分支会把 claim
        # 标记成 ``expired``（此后 ``claim.active`` 为假，``mark_claim_expired`` 会被
        # 自身守卫直接吞掉），于是 running 形态的过期记账整条丢失（缺陷 #8）。
        # A7：过期判定只读单调时钟（mono 是唯一权威），墙钟跳变不改变结论。
        lease_lapsed = claim.is_expired(now_mono=now_mono)
        is_zombie = False
        z_reason = ""
        try:
            from . import task_scheduler
            is_zombie, z_reason = task_scheduler.ZombieDetector.check_zombie_claim(claim, now, now_mono=now_mono)
        except Exception as exc:
            # A failed liveness probe is not evidence of health.  Fail closed
            # and reclaim the claim so a broken probe cannot preserve capacity
            # forever.
            is_zombie = True
            z_reason = "zombie_probe_failed"
            append_event(
                state, "worker_zombie_probe_failed", now,
                worker_id=worker_id, lease_id=claim.lease_id,
                execution_id=claim.execution_id,
                error=type(exc).__name__,
            )


        if is_zombie:
            terminated = None
            if z_reason == "heartbeat_timeout" and claim.process_pid is not None:
                refusal = ""
                try:
                    from . import task_scheduler
                    # 归属判定收敛在 terminate_claim：命令行证据 + 启动令牌双重
                    # 校验，任一缺失/不匹配即拒绝发信号（进程组投递另有把关）。
                    terminated, refusal = task_scheduler.ZombieDetector.terminate_claim(
                        claim, workspace_root=workspace_root or getattr(state, "workspace_root", None),
                    )
                except Exception:
                    terminated, refusal = False, "terminate_error"
                z_reason = (
                    f"{z_reason}_killed" if terminated
                    else f"{z_reason}_kill_failed"
                )
                if not terminated:
                    # 拒绝终止必须可观测：Worker 自报的 PID 可能指向无关进程，
                    # 静默拒绝会掩盖「有人试图诱导宿主杀进程」这一事实。
                    append_event(
                        state, "worker_termination_refused", now,
                        worker_id=worker_id, lease_id=claim.lease_id,
                        execution_id=claim.execution_id,
                        process_pid=claim.process_pid,
                        reason=refusal or "unknown",
                    )
            claim.state = "expired"
            claim.execution_state = "expired"
            claim.execution_finished_at = now
            claim.preempted_reason = z_reason
            state.scheduling_metrics.total_preempted += 1
            _record_adaptive_outcome(state, "preempted")
            append_event(
                state, "worker_preempted", now, worker_id=worker_id,
                lease_id=claim.lease_id,
                execution_id=claim.execution_id,
                reason=z_reason,
                process_pid=claim.process_pid,
                process_terminated=terminated,
            )
            if lease_lapsed:
                # 「租约失效」与「被预占」是两个独立事实，必须同时记账（缺陷 #8：
                # running 形态此前只记预占，``total_expired`` 系统性少计）。
                record_expiry_accounting(
                    state, claim, now, include_execution_id=True,
                )
            if claim.attempt >= MAX_WORKER_ATTEMPTS:
                mark_worker_attempts_exhausted(
                    state, now, claim.unit_ids,
                    worker_id=claim.worker_id, attempt=claim.attempt,
                )
        elif claim.is_expired(now_mono=now_mono):
            # LOG-11：过期记账（状态/计数/事件）收敛到 lease_manager 单点。
            mark_claim_expired(state, claim, now, include_execution_id=True)
            _record_adaptive_outcome(state, "expired")
    reconcile_parallel(state, now)
    if _renew_round_budget_if_live(state, now, now_mono=now_mono):
        return
    deadline = deadline_reasons(state, now)
    # D2：INCOMPLETE 是 finish 已收尾终态，不得被预算刷新改写成 BUDGET_EXHAUSTED。
    if deadline and state.status not in {
        Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
    }:
        if not state.budget_exhausted:
            set_budget_exhausted(state, now, deadline)
        else:
            state.budget_reasons = list(dict.fromkeys(
                [*state.budget_reasons, *deadline]
            ))


def apply_status_budget(state: RunState, now: float) -> None:
    """Apply status-query budget gates after the caller refreshes leases."""
    now = observe_clock(state, now)
    if (
        state.status not in {
            Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
            Status.BUDGET_EXHAUSTED,
        }
        and not state.coverage_complete
        and (state.budget_exhausted or soft_limit_reasons(state))
    ):
        set_budget_exhausted(
            state, now, soft_limit_reasons(state),
            event_type="status_budget_exhausted",
        )


def refresh_status_budget(state: RunState, now: float) -> None:
    """Refresh worker leases and apply status-query budget gates once."""
    refresh(state, now)
    apply_status_budget(state, now)


def ensure_live_for_work(state: RunState) -> None:
    # D2：INCOMPLETE 与 COMPLETE/FAILED 同级冻结，claim/next_round 等一切
    # 工作入口必须拒绝（BUDGET_EXHAUSTED 不在内：它是可续耗尽态）。
    if state.status in {Status.COMPLETE, Status.FAILED, Status.INCOMPLETE}:
        raise InvalidTransition(f"run 已结束: {state.status.value}")
    if state.budget_exhausted:
        raise BudgetExhausted(
            "预算已耗尽: " + ", ".join(state.budget_reasons or ["unknown"])
        )


def recalibrate_round_budget(
    state: RunState,
    recommended_seconds: float,
    now: float,
    *,
    reason: str = "adaptive_round_complexity",
) -> float:
    """动态校准当前 Round 的单轮墙钟预算上限 (DOC-150)。

    受全局总预算 max_wall_seconds 约束，校准后立即检查是否满足活性并记录审计事件。
    """
    if recommended_seconds <= 0:
        return state.budget.max_round_wall_seconds
    previous = state.budget.max_round_wall_seconds
    bounded = min(recommended_seconds, state.budget.max_wall_seconds)
    state.budget = dataclasses.replace(state.budget, max_round_wall_seconds=bounded)
    append_event(
        state,
        "round_budget_recalibrated",
        now,
        previous_budget=previous,
        new_budget=bounded,
        reason=reason,
    )
    _renew_round_budget_if_live(state, now)
    return bounded
