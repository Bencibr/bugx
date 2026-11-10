#!/usr/bin/env python3
"""core/supervisor/lease_manager.py - Supervisor 租约与 Worker 领域服务 (DOC-070)。

负责 Worker 身份校验、租约生命周期（认领、确认、心跳、释放、过期判定）
以及并发调度单元/资源组隔离。
"""

from __future__ import annotations

import errno
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

from core.planning.unit_kind import is_internal

from .supervisor_models import (
    COVERAGE_PENDING,
    GLOBAL_BLOCKED,
    TERMINAL_COVERED,
    UNIT_LEVEL_DEBT,
    COVERAGE_BLOCKED,
    MAX_HISTORY_EVENTS,
    MAX_WORKER_ATTEMPTS,
    MAX_LONG_CALL_SECONDS,
    MAX_LONG_CALLS_PER_CLAIM,
    ClaimRejected,
    InvalidCheckpoint,
    LeaseExpired,
    RunState,
    WorkerClaim,
    event_hash,
    process_start_token,
    worker_attempts_exhausted,
)


# 事件链原语已下沉到 claim_events（2026-10-26 架构收敛）：与
# budget_governor 互为依赖的环由此打破。本地名字保留，既有导入点不变。
from core.supervisor.claim_events import append_event


def mark_worker_attempts_exhausted(
    state: RunState,
    now: float,
    unit_ids: Iterable[str],
    *,
    worker_id: str | None = None,
    attempt: int | None = None,
) -> list[str]:
    """Mark retry-exhausted units blocked and record one auditable event."""
    blocked: list[str] = []
    for unit_id in dict.fromkeys(str(item) for item in unit_ids):
        if state.coverage.get(unit_id) in TERMINAL_COVERED:
            continue
        if state.coverage.get(unit_id) == COVERAGE_BLOCKED:
            continue
        state.coverage[unit_id] = COVERAGE_BLOCKED
        blocked.append(unit_id)
    if blocked:
        append_event(
            state,
            "worker_attempts_exhausted",
            now,
            worker_id=worker_id,
            unit_ids=sorted(blocked),
            attempt=attempt,
            max_attempts=MAX_WORKER_ATTEMPTS,
            scheduler_epoch=state.scheduler_epoch,
            reason="worker retry circuit breaker",
        )
    return blocked


def record_expiry_accounting(
    state: RunState,
    claim: WorkerClaim,
    now: float,
    *,
    include_execution_id: bool = False,
) -> None:
    """只做「过期」的计数 + 事件记账，不改 claim 状态。

    与 :func:`mark_claim_expired` 分开的理由：僵尸（预占）路径会先把 claim 标记成
    ``expired``，此时 ``claim.active`` 已为假，再调 ``mark_claim_expired`` 会被
    它的 ``if not claim.active: return`` 直接吞掉 —— 这正是「running 形态租约过期
    记账被僵尸路径吞」的物理成因（缺陷 #8：``total_expired`` 系统性少计）。
    """
    state.scheduling_metrics.total_expired += 1
    fields: dict[str, object] = {
        "worker_id": claim.worker_id,
        "lease_id": claim.lease_id,
    }
    if include_execution_id:
        fields["execution_id"] = claim.execution_id
    append_event(state, "lease_expired", now, **fields)


def mark_claim_expired(
    state: RunState,
    claim: WorkerClaim,
    now: float,
    *,
    include_execution_id: bool = False,
) -> None:
    """把租约标记为过期，并统一记账 + 发事件（LOG-11）。

    「租约过期」是同一个事实，但历史上有两条发现路径各自实现：
    ``budget_governor`` 的调和循环标记时会 ``scheduling_metrics.total_expired += 1``
    并发事件，``lease_manager.active_claim`` 标记时既不计数也不记录事件。于是同一
    事实按发现路径不同得到不同遥测，``total_expired`` 系统性偏低。这里把三件事
    （状态标记 / 计数 / 事件）收敛到单点。

    ``include_execution_id`` 保留既有事件字段差异，避免无谓改动已 hash-chain
    签名的事件；字段更完整的那条路径传 True。
    """
    if not claim.active:
        return
    claim.state = "expired"
    claim.execution_state = "expired"
    claim.execution_finished_at = now
    record_expiry_accounting(
        state, claim, now, include_execution_id=include_execution_id,
    )
    if claim.attempt >= MAX_WORKER_ATTEMPTS:
        mark_worker_attempts_exhausted(
            state, now, claim.unit_ids,
            worker_id=claim.worker_id, attempt=claim.attempt,
        )


def normalize_worker_id(worker_id: str) -> str:
    if not isinstance(worker_id, str):
        raise ClaimRejected("worker_id 不能为空")
    normalized = worker_id.strip()
    if not normalized:
        raise ClaimRejected("worker_id 不能为空")
    if len(normalized) > 128:
        raise ClaimRejected("worker_id 过长（>128）")
    if any(ord(char) < 32 for char in normalized):
        raise ClaimRejected("worker_id 不得包含控制字符")
    return normalized


def normalize_checkpoint_id(checkpoint_id: str | None) -> str | None:
    if checkpoint_id is None:
        return None
    if not isinstance(checkpoint_id, str):
        raise InvalidCheckpoint("checkpoint_id 必须是字符串")
    normalized = checkpoint_id.strip()
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
        raise InvalidCheckpoint("checkpoint_id 无效")
    return normalized


def normalize_ids(
    values: Sequence[str] | None, name: str, max_items: int | None = None,
) -> tuple[str, ...] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise ClaimRejected(f"{name} 必须是字符串数组")
    if max_items is not None:
        maximum = max_items
    else:
        maximum = 5 if name == "unit_ids" else 10
    raw_list: list[object] = []
    try:
        iterator = iter(values)
        for item in iterator:
            if len(raw_list) >= maximum:
                raise ClaimRejected(f"{name} 过多（最多 {maximum} 项）")
            raw_list.append(item)
    except TypeError as exc:
        raise ClaimRejected(f"{name} 必须是字符串数组") from exc
    raw_values = tuple(raw_list)
    if any(not isinstance(item, str) for item in raw_values):
        raise ClaimRejected(f"{name} 必须是字符串数组")
    normalized = tuple(item.strip() for item in raw_values)
    if any(not item for item in normalized):
        raise ClaimRejected(f"{name} 不能包含空值")
    if len(set(normalized)) != len(normalized):
        raise ClaimRejected(f"{name} 不能重复")
    return normalized


def claimed_units_this_round(state: RunState) -> set[str]:
    """Round-scoped duplicate guard for ``claim``.

    ``fenced_from_attempts`` 标记的是"历史代"claim（recovery 重置尝试史或
    内容失效作废，见 ``AuditSupervisor.invalidate_coverage``）：它们的产物已作废，
    不再占用单元。已 covered 的单元本就不可申领（``TERMINAL_COVERED``），
    因此跳过 fenced claim 不会制造同一单元的并发重复劳动。
    """
    return {
        unit
        for claim in state.claims.values()
        if claim.round_no == state.round_no
        and claim.state in {"active", "submitted"}
        and not getattr(claim, "fenced_from_attempts", False)
        for unit in claim.unit_ids
    }


def eligible_units(state: RunState) -> set[str]:
    """Keep internal debt visible until all of it is retry-exhausted.

    注意（契约，见 ``tests/test_lease_manager.py::EligibleUnitsTests``）：本函数返回
    的是**范围/可见性集合**，不是「此刻可申领集合」。因此：

    * 内部单元**整体保留**（含已终态者），调用方不得据此假设可申领；
    * 业务单元仅在显式准入（``business_admissions``）后才可见；
    * 「此刻是否可申领」由消费方叠加债务态与尝试次数自行判定
      （``supervisor/observability.py`` 与 ``launch_phases/worker_orchestrator.py``
      都显式过滤 ``UNIT_LEVEL_DEBT`` 与 ``worker_attempts_exhausted``）。
    """
    internal = {
        unit for unit in state.coverage if is_internal(unit)
    }
    internal_debt = {
        unit for unit in internal
        if state.coverage[unit] not in TERMINAL_COVERED
    }
    if internal_debt:
        if all(worker_attempts_exhausted(state, unit) for unit in internal_debt):
            # 043ce363 的有意放行：内部债务全部熔断后不再连坐业务单元。
            return set(state.coverage)
        admitted = set(state.business_admissions)
        return internal | (admitted & set(state.coverage))
    return set(state.coverage)


CLAIM_MODE_AUTO = "auto"
CLAIM_MODE_EXPLICIT = "explicit"
CLAIM_MODE_TAKEOVER = "takeover"


def claimable_units(
    state: RunState,
    *,
    mode: str,
    requested_units: Sequence[str] | None = None,
    takeover_claim: WorkerClaim | None = None,
) -> tuple[str, ...]:
    """Apply shared unit qualification for automatic, explicit and takeover claims.

    ``eligible_units`` is a visibility/admission set. This helper adds the
    mode semantics while keeping them distinct: automatic claims select debt,
    explicit claims may review a terminal unit, and takeover claims inherit the
    exact old package. Callers must invoke this inside the Supervisor mutation
    lock; a read-side candidate list never authorizes a later write.
    """
    if mode not in {CLAIM_MODE_AUTO, CLAIM_MODE_EXPLICIT, CLAIM_MODE_TAKEOVER}:
        raise ClaimRejected(f"未知 claim mode: {mode}")
    coverage = getattr(state, "coverage", {}) or {}
    visible = eligible_units(state)
    occupied = claimed_units_this_round(state)

    if mode == CLAIM_MODE_AUTO:
        pending = sorted(
            unit for unit, status in coverage.items()
            if unit in visible and unit not in occupied and status == COVERAGE_PENDING
        )
        debt = sorted(
            unit for unit, status in coverage.items()
            if unit in visible and unit not in occupied
            and status in UNIT_LEVEL_DEBT and status != COVERAGE_PENDING
        )
        return tuple([*pending, *debt])

    if mode == CLAIM_MODE_TAKEOVER:
        if takeover_claim is None:
            raise ClaimRejected("接手 claim 缺少原 claim")
        if takeover_claim.state not in {"expired", "released"}:
            raise ClaimRejected("只有 expired/released worker 才能被接手")
        if takeover_claim.round_no != state.round_no:
            raise ClaimRejected("只允许在同一 Round 接手过期 worker")
        selected = tuple(requested_units or takeover_claim.unit_ids)
        if selected != takeover_claim.unit_ids:
            raise ClaimRejected("接手 worker 的 unit_ids 必须与原 claim 完全一致")
        # A historical package is already an authorized context. Preserve
        # its coverage status while still rechecking identity, global blocks,
        # and current-round occupancy under the mutation lock.
        visible_for_selection: set[str] = set(selected)
    else:
        selected = tuple(requested_units or ())
        visible_for_selection = visible

    unknown = [unit for unit in selected if unit not in coverage]
    if unknown:
        raise ClaimRejected("未知 coverage unit: " + ", ".join(sorted(unknown)))
    ineligible = sorted(set(selected) - visible_for_selection)
    if ineligible:
        raise ClaimRejected(
            "内部单元未完成前不能领取业务 unit: " + ", ".join(ineligible)
        )
    globally_blocked = sorted(
        unit for unit in selected if coverage.get(unit) in GLOBAL_BLOCKED
    )
    if globally_blocked:
        raise ClaimRejected(
            "coverage unit 处于全局阻塞，不能直接派发: "
            + ", ".join(globally_blocked)
        )
    duplicate = sorted(set(selected) & occupied)
    if duplicate:
        raise ClaimRejected(
            "coverage unit 已在当前 Round (round_no="
            + str(state.round_no)
            + ") 领取: "
            + ", ".join(duplicate)
        )
    return selected


def active_resource_keys(state: RunState) -> set[str]:
    return {
        resource
        for claim in state.active_claims.values()
        for resource in claim.resource_keys
    }


def lease_expired(claim: WorkerClaim, mono_now: float) -> bool:
    """租约是否已失效的**唯一**判定入口（A7：单调时钟是唯一权威）。

    覆盖两个条件：租约到期（``lease_expires_mono``）与总截止期到达
    （``total_lease_deadline_mono``）。墙钟字段（``lease_expires_at`` /
    ``total_lease_deadline``）只用于审计展示，不参与判定。
    """
    return claim.is_expired(now_mono=float(mono_now))


def expiry_reason(claim: WorkerClaim, mono_now: float) -> str | None:
    """过期原因：``total_lease_deadline_exceeded`` 或普通租约到期（None）。"""
    deadline_mono = claim.total_lease_deadline_mono
    if deadline_mono is not None and float(mono_now) >= deadline_mono:
        return "total_lease_deadline_exceeded"
    return None


def active_claim(
    state: RunState, worker_id: str, now: float, *, require_running: bool = False,
    now_mono: float | None = None,
) -> WorkerClaim:
    claim = state.claims.get(worker_id)
    if claim is None:
        raise ClaimRejected(f"worker 未 claim: {worker_id}")
    if claim.scheduler_epoch != state.scheduler_epoch:
        # 旧 epoch 的租约一律视为陈旧，拒绝旧 Worker 的心跳/结果覆盖新纪元
        append_event(
            state, "stale_epoch_rejected", now, worker_id=worker_id,
            lease_id=claim.lease_id, claim_epoch=claim.scheduler_epoch,
            current_epoch=state.scheduler_epoch,
        )
        raise InvalidCheckpoint(
            f"worker epoch 已过期: {worker_id} (claim epoch {claim.scheduler_epoch} != current {state.scheduler_epoch})"
        )
    mono_now = time.monotonic() if now_mono is None else float(now_mono)
    if lease_expired(claim, mono_now):
        reason = expiry_reason(claim, mono_now)
        if reason is not None:
            claim.preempted_reason = reason
            mark_claim_expired(state, claim, now, include_execution_id=True)
            raise LeaseExpired(f"worker total lease deadline 已过期: {worker_id}")
        # LOG-11：与 budget_governor 的调和路径共用同一记账 helper，避免
        # 「同一事实、两条路径、两套遥测」。
        mark_claim_expired(state, claim, now)
        raise LeaseExpired(f"worker lease 已过期: {worker_id}")
    if not claim.active:
        raise ClaimRejected(
            f"worker claim 不可提交: {worker_id} ({claim.state})"
        )
    if require_running and claim.execution_state != "running":
        raise ClaimRejected(
            f"worker 尚未 worker-start，不能执行该操作: {worker_id}"
        )
    return claim


def renew_claim(
    state: RunState,
    claim: WorkerClaim,
    now: float,
    lease_seconds: float,
    worker_id: str,
    *,
    now_mono: float | None = None,
) -> None:
    """Apply the bounded renewal policy to one active worker claim.

    The caller owns the surrounding lock, identity checks, and heartbeat
    event.  This function only mutates the claim's lease fields and records
    terminal expiry through the shared lease helper.

    B1：寿命只由 ``total_lease_deadline`` 决定，``renewal_count`` 仅是审计计数；
    B3：续租只允许延长或保持已声明窗口（``max``，与 ``begin_long_call`` 对称）。
    """
    if claim.total_lease_deadline is None or claim.total_lease_deadline_mono is None:
        # 无上界的续租会让 ``max`` 语义退化为永久租约，必须明确拒绝。
        raise ClaimRejected(
            f"worker claim 缺少总租约截止期，无法续租: {worker_id}"
        )
    mono_now = time.monotonic() if now_mono is None else float(now_mono)
    if lease_expired(claim, mono_now):
        reason = expiry_reason(claim, mono_now)
        if reason is not None:
            claim.preempted_reason = reason
        mark_claim_expired(state, claim, now, include_execution_id=True)
        raise LeaseExpired(f"worker total lease deadline 已过期: {worker_id}")
    claim.renewal_count += 1
    next_expiry = min(now + float(lease_seconds), claim.total_lease_deadline)
    claim.lease_expires_at = max(claim.lease_expires_at, next_expiry)
    claim.last_heartbeat_at = now
    claim.last_heartbeat_mono = mono_now
    next_expiry_mono = min(
        mono_now + float(lease_seconds), claim.total_lease_deadline_mono,
    )
    claim.lease_expires_mono = max(
        claim.lease_expires_mono or 0.0, next_expiry_mono,
    )


def begin_long_call(
    state: RunState,
    claim: WorkerClaim,
    now: float,
    expected_seconds: float,
    worker_id: str,
    *,
    now_mono: float | None = None,
) -> float:
    """DOC-203 D3：worker 为一次不可分的长工具调用声明保活窗口。

    subagent 结构下一次只能执行一个工具调用，10-20 分钟的探针期间结构性
    无法发心跳；本调用把租约推进到 ``max(当前值, min(now+expected+60,
    total_lease_deadline))``——``max`` 保证声明绝不会**缩短**现有租约
    （红队修订），``total_lease_deadline`` 硬顶不可越过。活性语义保留：
    窗口内不回收，窗口结束 ``is_expired`` 照常成立（drain/僵尸检测回收），
    调用本身刷新 ``last_heartbeat_at``（worker 刚发起 MCP 调用即是活性）。
    达到每 claim 的声明上限即抛 ``ClaimRejected``，且**零租约副作用**（不改
    任何租约字段、不刷新心跳、不加计数）；Worker 需立即 checkpoint 或等待
    宿主恢复。调用方拥有外层锁与身份校验。
    """
    mono_now = time.monotonic() if now_mono is None else float(now_mono)
    if not claim.active:
        raise ClaimRejected(f"worker claim 不活跃，无法声明长调用: {worker_id}")
    deadline_mono = claim.total_lease_deadline_mono
    if deadline_mono is None:
        # A7 收尾（与 ``_renew_expired_live_claim`` 对齐）：总租约截止期是硬顶，
        # 缺 mono 锚点属**证据缺失** ⇒ fail-closed，绝不能当作「无上限」放行——
        # 否则长调用声明可以把租约无限延长，绕过硬顶。
        raise ClaimRejected(
            f"worker claim 缺少总租约截止期锚点，无法声明长调用: {worker_id}"
        )
    if mono_now >= deadline_mono:
        claim.preempted_reason = "total_lease_deadline_exceeded"
        mark_claim_expired(state, claim, now, include_execution_id=True)
        raise LeaseExpired(f"worker total lease deadline 已过期: {worker_id}")
    if claim.long_call_count >= MAX_LONG_CALLS_PER_CLAIM:
        # B8：拒绝即无副作用——事件是可观察的审计痕迹，不是资源状态变更。
        append_event(
            state, "long_call_cap_reached", now,
            worker_id=worker_id,
            lease_id=claim.lease_id,
            long_call_count=claim.long_call_count,
            max_long_calls_per_claim=MAX_LONG_CALLS_PER_CLAIM,
            lease_expires_at=claim.lease_expires_at,
            total_lease_deadline=claim.total_lease_deadline,
        )
        raise ClaimRejected(
            f"worker 长调用声明次数已达上限 {MAX_LONG_CALLS_PER_CLAIM}: {worker_id}；"
            "请停止声明更多长调用，完成当前工作并 finish/checkpoint，或等待宿主恢复"
        )
    expected = float(expected_seconds)
    if not expected > 0 or expected > MAX_LONG_CALL_SECONDS:
        raise ClaimRejected(
            f"expected_seconds 必须在 (0, {MAX_LONG_CALL_SECONDS}] 内: {worker_id}"
        )
    claim.long_call_count += 1
    requested_expiry = now + expected + 60.0
    requested_mono = mono_now + expected + 60.0
    if claim.total_lease_deadline is not None:
        requested_expiry = min(requested_expiry, claim.total_lease_deadline)
    if deadline_mono is not None:
        requested_mono = min(requested_mono, deadline_mono)
    claim.lease_expires_at = max(claim.lease_expires_at, requested_expiry)
    claim.last_heartbeat_at = now
    claim.last_heartbeat_mono = mono_now
    claim.lease_expires_mono = max(
        claim.lease_expires_mono or 0.0, requested_mono,
    )
    append_event(
        state, "long_call_declared", now,
        worker_id=worker_id,
        lease_id=claim.lease_id,
        expected_seconds=expected,
        long_call_count=claim.long_call_count,
        lease_expires_at=claim.lease_expires_at,
    )
    return claim.lease_expires_at


def release_claim(
    state: RunState,
    claim: WorkerClaim,
    now: float,
    *,
    reason: str,
    error: str | None = None,
) -> bool:
    """Release one active claim and append its worker_released event."""
    if not claim.active:
        return False
    claim.state = "released"
    claim.execution_state = "released"
    claim.execution_finished_at = now
    # DOC-196: 沉淀租约消耗的 Token 到全局账本
    if getattr(claim, "tokens_consumed", 0) > 0:
        state.tokens_consumed = getattr(state, "tokens_consumed", 0) + claim.tokens_consumed
        if not hasattr(state, "token_accounting") or state.token_accounting is None:
            state.token_accounting = {}
        for uid in claim.unit_ids:
            state.token_accounting[uid] = state.token_accounting.get(uid, 0) + claim.tokens_consumed
    fields: dict[str, object] = {
        "worker_id": claim.worker_id,
        "lease_id": claim.lease_id,
        "execution_id": claim.execution_id,
        "reason": reason,
    }
    if error is not None:
        fields["error"] = error
    append_event(state, "worker_released", now, **fields)
    if reason != "target_rebound" and claim.attempt >= MAX_WORKER_ATTEMPTS:
        mark_worker_attempts_exhausted(
            state, now, claim.unit_ids,
            worker_id=claim.worker_id, attempt=claim.attempt,
        )
    if error and reason == "execution_failed":
        from core.supervisor.systemic_failure import check_and_record_systemic_failure
        try:
            check_and_record_systemic_failure(state, claim.unit_ids, error, now)
        except Exception:
            pass
    return True


def _reconcile_parallel_after_bulk_release(state: RunState, now: float) -> None:
    """批量回收后统一重算并发度量一次（B5）。

    惰性导入避免 ``lease_manager`` ↔ ``budget_governor`` 的模块级循环依赖。
    重算失败不得打断回收流程，但必须留完整堆栈，禁止静默吞掉。
    """
    try:
        from core.supervisor import budget_governor
        budget_governor.reconcile_parallel(state, now)
    except Exception:
        logger.exception(
            "批量回收后并发度量重算失败（回收结果已保留）: round=%s now=%s",
            getattr(state, "round_no", None), now,
        )


def release_active_claims(
    state: RunState, now: float, *, reason: str,
) -> None:
    """Release claims from the fenced coordinator epoch.

    Epoch fencing alone rejects old callbacks but leaves their active
    claims occupying capacity and coverage forever. Recovery therefore
    closes each old claim explicitly so the next worker can retry it.
    """
    for claim in state.claims.values():
        release_claim(state, claim, now, reason=reason)
    state.round_parallel_active_since = None
    _reconcile_parallel_after_bulk_release(state, now)


def drain_dangling_claims(
    state: RunState,
    now: float,
    heartbeat_timeout_seconds: float,
    reason: str = "drained_at_round_boundary",
    *,
    now_mono: float | None = None,
) -> list[str]:
    """Release active claims that cannot prove live execution progress."""
    mono_now = time.monotonic() if now_mono is None else float(now_mono)
    released: list[str] = []
    for worker_id, claim in list(state.active_claims.items()):
        is_expired = lease_expired(claim, mono_now)
        # D3-A / A7：心跳静默快通道同样只看单调时钟。墙钟前跳会把活 Worker
        # 的「墙钟静默」瞬间放大到超时（误杀），回拨则会虚增其寿命；
        # 口径与 reclaim_orphan_leases 一致（mono 是唯一权威）。
        heartbeat_ref_mono = claim.last_heartbeat_mono or claim.claimed_mono
        heartbeat_silent = (
            heartbeat_ref_mono is not None
            and mono_now - heartbeat_ref_mono > heartbeat_timeout_seconds
        )
        # DOC-203 D3：长调用窗口内（lease_expires_at 已被 begin_long_call
        # 推进且尚未过期）不启用 heartbeat 静默快速通道；窗口结束
        # is_expired 必然成立，照常回收——活性语义不被绕过。曾声明过长
        # 调用的 claim 之后若不再声明，回收退化为租约到期锚（有界延迟）。
        in_long_call_window = claim.long_call_count > 0 and not is_expired
        if not (
            is_expired
            or (heartbeat_silent and not in_long_call_window)
            or claim.execution_state != "running"
        ):
            continue
        if release_claim(state, claim, now, reason=reason):
            released.append(worker_id)
    _reconcile_parallel_after_bulk_release(state, now)
    return released

def _host_may_skip_pid(provider: str) -> bool:
    """宿主是否**明确声明**不需要真实 PID（插件 worker_runtime.pid_required is False）。

    只有 ready 插件且显式 pid_required=False 才为真；未知 provider、空
    provider、未迁移宿主一律返回 False（保守：不主动回收，仍走原租约到期路径）。
    若把未知也当 False，会把「按轮次边界 drain」的既有回收抢在前面触发，
    改变既有语义（实测 test_next_round_gate_mode 因此失败）。
    """
    try:
        import core.hosts as host_plugins
    except ImportError:
        from core import hosts as host_plugins
    try:
        plugin = host_plugins.get(provider)
    except Exception:
        return False
    if plugin is None or not plugin.ready or plugin.worker_runtime is None:
        return False
    return plugin.worker_runtime.pid_required is False


def reclaim_orphan_leases(
    state: RunState,
    now: float,
    timeout_seconds: float | None = None,
    *,
    now_mono: float | None = None,
    start_timeout_seconds: float = 300.0,
) -> list[str]:
    """回收已确认死亡或已超时的孤儿租约。

    ``kill(pid, 0)`` 成功只证明 PID 当前存在，并不证明它仍是原 Worker；
    因此存活 PID 必须继续保留到租约超时，且其复合启动指纹不匹配时绝不
    走孤儿快通道。

    ``timeout_seconds`` 缺省与 ``budget_governor.ROUND_HEARTBEAT_TIMEOUT_SECONDS``
    （450s）对齐：governor 在该窗口内仍把心跳视为新鲜并续租，drain 若用更
    短的阈值（旧默认 90s）就会抢在 governor 之前杀死本应续租的 Worker，
    且与协议文档「每 5 分钟心跳一次」的口径互相矛盾。显式传参的调用方
    （测试、特殊回收策略）不受影响。
    """
    if timeout_seconds is None:
        try:
            from core.supervisor.budget_governor import (
                ROUND_HEARTBEAT_TIMEOUT_SECONDS as _round_timeout,
            )
        except ImportError:
            try:
                from core.supervisor.budget_governor import (
                    ROUND_HEARTBEAT_TIMEOUT_SECONDS as _round_timeout,
                )
            except ImportError:
                _round_timeout = 450.0
        timeout_seconds = float(_round_timeout)
    else:
        timeout_seconds = float(timeout_seconds)
    mono_now = time.monotonic() if now_mono is None else float(now_mono)
    reclaimed: list[str] = []
    for worker_id, claim in list(state.active_claims.items()):
        pid_state = "missing"
        if claim.process_pid is not None:
            try:
                os.kill(claim.process_pid, 0)
            except PermissionError:
                # EPERM means the PID exists but is not signalable by us.
                pid_state = "alive"
            except ProcessLookupError:
                pid_state = "dead"
            except OSError as exc:
                # Only ESRCH proves that the process disappeared.  EINVAL,
                # EPERM and platform-specific errors are deliberately unknown.
                pid_state = "dead" if exc.errno == errno.ESRCH else "unknown"
            except (TypeError, ValueError):
                pid_state = "unknown"
            else:
                pid_state = "alive"
        # A7（Wave 3 口径）：心跳停滞也只看单调时钟。墙钟前跳会让
        # ``now - last_heartbeat_at`` 突然变大，把活 Worker 判成停滞。
        heartbeat_ref_mono = claim.last_heartbeat_mono or claim.claimed_mono
        is_stale = (
            heartbeat_ref_mono is not None
            and mono_now - heartbeat_ref_mono > timeout_seconds
        )
        is_expired = lease_expired(claim, mono_now)
        if pid_state == "dead" or is_expired:
            reason = "orphan_process_reclaimed" if pid_state == "dead" else "stale_lease_reclaimed"
            claim.preempted_reason = reason
            if release_claim(state, claim, now, reason=reason):
                reclaimed.append(worker_id)
        elif (
            claim.process_pid is None
            and claim.execution_state == "claimed"
            and mono_now - (claim.claimed_mono or mono_now) > start_timeout_seconds
        ):
            # 预占后**从未** worker-start（provider 仍是 unknown，worker_start 才写
            # provider）：这不是「活任务在思考」，而是「抢占未开始」。它既没有
            # PID 可探活、也不满足插件 ``pid_required=False`` 分支，此前只能白占
            # 租约到 2400s 到期——期间同一资源组的后续 claim 会撞
            # ``资源组正在被其他 worker 使用``（实测 dsh v1/v2 连续失败）。
            # 用 claim 状态的启动超时兜底：超过 start_timeout 就释放，让下一轮重领。
            # 该判定**独立于心跳 staleness**：drain 阈值已与 governor 活性窗口
            # （450s）对齐，若仍嵌套在 is_stale 之下，start_timeout（300s）会被
            # 永远遮蔽——预占未启动的 claim 要多白占 150s 才可能被回收。
            claim.preempted_reason = "claim_start_timeout_reclaimed"
            if release_claim(
                state, claim, now,
                reason="claim_start_timeout_reclaimed",
            ):
                reclaimed.append(worker_id)
                logger.info(
                    "Worker %s 预占后 %.1fs 未 worker-start，按启动超时释放"
                    "（provider=%s）",
                    worker_id,
                    mono_now - (claim.claimed_mono or mono_now),
                    claim.provider,
                )
        elif is_stale and claim.process_pid is None:
            # 无独立进程的 worker（插件 ``worker_runtime.pid_required=False``，例如
            # dsh 这类「模型自己扮演 worker」的宿主）**心跳是唯一存活证据**：没有
            # 进程可供探活，那一份停滞的心跳就是「已经死了」的全部证据。此前这里
            # 一律只告警、等满整个租约，等于让这类宿主的死任务白占租约
            # （实测 dsh：age=113s 停滞、租约还剩 186s，回收通道完全走不通）。
            # 要求真实 PID 的宿主（cline 等）保持原行为：它们本应在 claim 时就带
            # pid，缺失属异常，交给租约到期兜底比误杀更安全。
            if _host_may_skip_pid(claim.provider):
                claim.preempted_reason = "silent_unpid_worker_reclaimed"
                if release_claim(
                    state, claim, now, reason="silent_unpid_worker_reclaimed",
                ):
                    reclaimed.append(worker_id)
                    logger.info(
                        "Worker %s 心跳停滞 (age=%.1fs) 且宿主不要求 PID，"
                        "按静默死亡回收租约",
                        worker_id,
                        mono_now - (heartbeat_ref_mono or mono_now),
                    )
            else:
                # 预占后从未 worker-start 的 claim 已提升为独立分支（不依赖
                # staleness）；这里只剩「非 may_skip 宿主的 running claim 心跳
                # 停滞」：无 PID 可探活、租约未到期，只能告警等租约到期兜底。
                logger.warning(
                    "Worker %s 心跳停滞 (age=%.1fs)，但 process_pid=None 且租约未到期 (剩余 %.1fs)，无法走孤儿快通道回收",
                    worker_id,
                    mono_now - (heartbeat_ref_mono or mono_now),
                    max(0.0, (claim.lease_expires_mono or mono_now) - mono_now),
                )
        elif is_stale and pid_state == "alive":
            observed = process_start_token(claim.process_pid)
            if claim.process_start_token is None or observed is None:
                logger.warning(
                    "Worker %s PID=%s 存活但缺少可验证复合指纹，保持租约至超时",
                    worker_id,
                    claim.process_pid,
                )
            elif observed != claim.process_start_token:
                logger.warning(
                    "Worker %s PID=%s 复合指纹不匹配，疑似 PID 复用，保持租约至超时",
                    worker_id,
                    claim.process_pid,
                )
        elif is_stale and pid_state == "unknown":
            logger.warning(
                "Worker %s PID=%s 存活性未知，保持租约至超时",
                worker_id,
                claim.process_pid,
            )
    _reconcile_parallel_after_bulk_release(state, now)
    return reclaimed
