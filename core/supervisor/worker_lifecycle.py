#!/usr/bin/env python3
"""core/supervisor/worker_lifecycle.py - Worker 租约凭证、生命周期与心跳流转。"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .supervisor_models import (
    MAX_LEASE_SECONDS,
    MAX_LONG_CALL_SECONDS,
    MAX_WORKER_ATTEMPTS,
    BudgetExhausted,
    ClaimRejected,
    InvalidCheckpoint,
    LeaseExpired,
    RunState,
    WorkerClaim,
    finite_number,
    process_start_token,
)

from core.supervisor import (
    adaptive_lease_seconds,
    claim_sync,
    graph_sync,
    mark_worker_attempts_exhausted,
    release_claim,
    worker_sync,
)

if TYPE_CHECKING:
    from .audit_supervisor import Supervisor

logger = logging.getLogger(__name__)

def _apply_claim_operation(
    self,
    state,
    now,
    **kwargs,
) -> WorkerClaim:
    """Compatibility seam for the claim mutation boundary."""
    return claim_sync.apply_claim_operation(
        self, state, now, **kwargs,
    )
def claim_worker(
    self,
    worker_id: str,
    *,
    lease_seconds: float | None = None,
    unit_ids: Sequence[str] | None = None,
    resource_keys: Sequence[str] | None = None,
    expected_revision: int | None = None,
    process_pid: int | None = None,
    allow_bundle: bool = False,
    supersede_worker_id: str | None = None,
    continue_worker: bool = False,
    recover_expired: bool = False,
    write_access: bool | None = None,
    assignment_kind: str | None = None,
    graph_task_id: str | None = None,
    worker_role: str | None = None,
    preferred_affinity: str | None = None,
) -> WorkerClaim:
    """领取一个 coverage unit 或一个独立 graph-stage lease。

    ``recover_expired=True``：同一 worker_id 的**终态** claim（expired/released）
    允许「自接手」——继承 unit_ids/resource_keys/artifact_root，``attempt`` 严格 +1，
    租约重新计时（MCP 层同名参数的同源实现）。这是 **同代重试** 的协议入口：
    没有它，单元 expired 后只能跨代接手，attempt 熔断在协议面无法被驱动。
    """

    worker_key = self._normalize_worker_id(worker_id)
    if assignment_kind not in {None, "unit", "graph_stage"}:
        raise ClaimRejected("assignment_kind 必须是 unit、graph_stage 或 null")
    # None 表示「按观测自适应」：租约长度由已记账执行耗时推导，显式传值仍生效。
    lease = None if lease_seconds is None else finite_number(lease_seconds, "lease_seconds")
    if lease is not None and lease <= 0:
        raise ClaimRejected("lease_seconds 必须 > 0")
    if lease is not None and lease > MAX_LEASE_SECONDS:
        raise ClaimRejected(f"lease_seconds 超过上限（>{MAX_LEASE_SECONDS} 秒）")
    requested_units = self._normalize_ids(unit_ids, "unit_ids")
    requested_resources = self._normalize_ids(resource_keys, "resource_keys") or ()
    if assignment_kind == "graph_stage":
        task_key = graph_task_id.strip() if isinstance(graph_task_id, str) else ""
        if not task_key:
            raise ClaimRejected("graph_stage claim 必须提供 graph_task_id")
        if requested_units not in (None, ()):
            raise ClaimRejected(
        "graph_stage claim 的 unit_ids 必须为空（传 []），"
        "图谱阶段的 claim 不针对具体覆盖单元；请改用普通 claim 领取单元"
    )
        if supersede_worker_id is not None or continue_worker:
            raise ClaimRejected("graph_stage claim 不支持接手或复用 worker")
        if not isinstance(continue_worker, bool):
            raise ClaimRejected(
        "continue_worker 必须是布尔值，形如 true / false；"
        "传 true 表示提交后由同一 worker 继续领取未完成单元，否则省略该字段"
    )
        if requested_resources and len(requested_resources) > 10:
            raise ClaimRejected("resource_keys 过多（>10）")

        def graph_operation(state: RunState, now: float) -> WorkerClaim:
            return graph_sync.graph_claim_sync(
                self, state, now,
                worker_id=worker_key,
                graph_task_id=task_key,
                lease_seconds=(adaptive_lease_seconds(state) if lease is None else lease),
                expected_revision=expected_revision,
                process_pid=process_pid,
                requested_resources=requested_resources,
            )

        return self._mutate(graph_operation)  # type: ignore[return-value]
    if graph_task_id is not None:
        raise ClaimRejected("非 graph_stage claim 不能提供 graph_task_id")
    supersede_key = self._normalize_worker_id(supersede_worker_id) if supersede_worker_id is not None else None
    # Enforce MCP-consistent single-unit per Worker unless allow_bundle is True.
    if requested_units is not None:
        if not allow_bundle and supersede_key is None and len(requested_units) != 1:
            raise ClaimRejected(
                "每个 Worker 只能领取一个 coverage unit；请由调度器拆分任务后再次 claim"
            )
        if len(requested_units) < 1 or len(requested_units) > 5:
            raise ClaimRejected("单次 claim 单元数必须在 1 到 5 之间")
    if requested_resources and len(requested_resources) > 10:
        raise ClaimRejected("resource_keys 过多（>10）")
    if not isinstance(continue_worker, bool):
        raise ClaimRejected(
            "continue_worker 必须是布尔值，形如 true / false；"
            "传 true 表示提交后由同一 worker 继续领取未完成单元"
        )
    if not isinstance(recover_expired, bool):
        raise ClaimRejected(
        "recover_expired 必须是布尔值，形如 true / false；"
        "传 true 表示接手同名单 worker 已终态（expired/released）的租约"
    )
    if recover_expired and supersede_key is not None:
        raise ClaimRejected("recover_expired 与 supersede_worker_id 互斥")
    if recover_expired:
        # 自接手：同一个 worker_id 换约（claim_sync 的 self_supersede_recovery 分支）。
        supersede_key = worker_key

    def operation(state: RunState, now: float) -> WorkerClaim:
        return self._apply_claim_operation(
            state=state,
            now=now,
            allow_bundle=allow_bundle,
            continue_worker=continue_worker,
            expected_revision=expected_revision,
            lease=(adaptive_lease_seconds(state) if lease is None else lease),
            process_pid=process_pid,
            requested_resources=requested_resources,
            requested_units=requested_units,
            supersede_key=supersede_key,
            worker_key=worker_key,
            write_access=write_access,
            worker_role=worker_role,
            preferred_affinity=preferred_affinity,
        )

    return self._mutate(operation)  # type: ignore[return-value]

def start_worker(
    self,
    worker_id: str,
    *,
    provider: str = "unknown",
    execution_id: str | None = None,
    expected_revision: int | None = None,
    process_pid: int | None = None,
) -> WorkerClaim:
    """确认 Worker 已由宿主真实启动；此刻才进入并发重叠统计。"""
    worker_key = self._normalize_worker_id(worker_id)
    if not isinstance(provider, str) or not provider.strip():
        raise ClaimRejected("provider 不能为空")
    if execution_id is not None and (
        not isinstance(execution_id, str) or not execution_id.strip()
    ):
        raise ClaimRejected(
        "execution_id 必须是非空字符串，形如 \"exec-0001\"；"
        "不能传 None / 空串 / 纯空白，每次 claim 应使用新值"
    )

    def operation(state: RunState, now: float) -> WorkerClaim:
        claim = worker_sync.apply_start_worker_operation(
            self,
            state,
            now,
            worker_key=worker_key,
            provider=provider,
            execution_id=execution_id,
            expected_revision=expected_revision,
            process_pid=process_pid,
        )
        graph_sync.graph_worker_started(state, claim, now, self._append_event)
        return claim

    return self._mutate(operation)  # type: ignore[return-value]

def record_worker_failure(
    self,
    worker_id: str,
    *,
    lease_id: str,
    execution_id: str,
    reason: str,
) -> WorkerClaim | None:
    """持久记录本地 Worker 执行失败并释放仍匹配的 claim。

    Claim 身份双重围栏可防止迟到的失败回调释放同名 Worker 的新 lease。
    失败沿用既有 ``released`` 状态与 hash-chain ``worker_released`` 事件，
    不把单 Worker 失败升级为 run 失败，也不生成 checkpoint/coverage。
    """
    worker_key = self._normalize_worker_id(worker_id)
    if not isinstance(lease_id, str) or not lease_id.strip():
        raise ClaimRejected(
        "lease_id 必须是非空字符串，取自 claim 返回的 lease_id；"
        "不要自行编造或复用其他 worker 的 lease_id"
    )
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise ClaimRejected(
        "execution_id 必须是非空字符串，形如 \"exec-0002\"；"
        "每个 execution 应是独立新值，不能复用上一次的 execution_id"
    )
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidCheckpoint("Worker failure 必须提供 reason")
    failure_reason = reason.strip()[:2000]

    def operation(state: RunState, now: float) -> WorkerClaim | None:
        self._refresh(state, now)
        claim = state.claims.get(worker_key)
        if (
            claim is None
            or claim.lease_id != lease_id.strip()
            or claim.execution_id != execution_id.strip()
        ):
            return None
        # Refresh can have already expired/released the claim. Preserve that
        # terminal result and never append a failure to another attempt.
        if not claim.active:
            return claim
        release_claim(
            state, claim, now, reason="execution_failed", error=failure_reason,
        )
        graph_sync.graph_worker_failed(state, claim, now, failure_reason)
        self._reconcile_parallel(state, now)
        return claim

    return self._mutate(operation)  # type: ignore[return-value]

def heartbeat(
    self,
    worker_id: str,
    *,
    lease_seconds: float | None = None,
    heartbeat_nonce: str | None = None,
    expected_revision: int | None = None,
) -> WorkerClaim:
    """Renew an active worker lease without recording a tool call."""

    worker_key = self._normalize_worker_id(worker_id)
    # 心跳沿用 claim 的租约长度（None = 沿用 claim 现租约，不做自适应重算，
    # 避免一次心跳把长租约缩回默认值）。
    lease = (
        None if lease_seconds is None
        else finite_number(lease_seconds, "lease_seconds")
    )
    if lease is not None and lease <= 0:
        raise ClaimRejected("lease_seconds 必须 > 0")
    if lease is not None and lease > MAX_LEASE_SECONDS:
        raise ClaimRejected(
            f"lease_seconds 超过上限（>{MAX_LEASE_SECONDS} 秒）"
        )

    def operation(state: RunState, now: float) -> WorkerClaim:
        self._check_revision(state, expected_revision)
        self._refresh(state, now)
        self._ensure_live_for_work(state)
        self._assert_target_fingerprint(state, now)
        claim = self._active_claim(state, worker_key, now, require_running=True)
        if claim.heartbeat_nonce is not None and heartbeat_nonce != claim.heartbeat_nonce:
            raise LeaseExpired(f"worker heartbeat nonce 不匹配: {worker_key}")
        if claim.process_pid is not None and claim.process_start_token is not None:
            current_token = process_start_token(claim.process_pid)
            if current_token != claim.process_start_token:
                claim.state = "expired"
                claim.execution_state = "expired"
                claim.execution_finished_at = now
                self._append_event(
                    state, "worker_owner_fenced", now,
                    worker_id=worker_key, lease_id=claim.lease_id,
                    expected_process_start_token=claim.process_start_token,
                    actual_process_start_token=current_token,
                )
                if claim.attempt >= MAX_WORKER_ATTEMPTS:
                    mark_worker_attempts_exhausted(
                        state, now, claim.unit_ids,
                        worker_id=claim.worker_id, attempt=claim.attempt,
                    )
                raise LeaseExpired(f"worker owner identity changed: {worker_key}")
        renew_lease = lease if lease is not None else adaptive_lease_seconds(state)
        self._apply_heartbeat_renewal(state, claim, now, renew_lease, worker_key)
        self._append_event(
            state, "worker_heartbeat", now, worker_id=worker_key,
            lease_id=claim.lease_id,
            lease_expires_at=claim.lease_expires_at,
            renewal_count=claim.renewal_count,
            total_lease_deadline=claim.total_lease_deadline,
        )
        return claim

    return self._mutate(operation)  # type: ignore[return-value]

def begin_long_call(
    self,
    worker_id: str,
    *,
    expected_seconds: float,
    expected_revision: int | None = None,
) -> WorkerClaim:
    """DOC-203 D3：worker 为一次长工具调用声明保活窗口（锁内串行）。"""

    worker_key = self._normalize_worker_id(worker_id)
    expected = finite_number(expected_seconds, "expected_seconds")
    if expected <= 0 or expected > MAX_LONG_CALL_SECONDS:
        raise ClaimRejected(
            f"expected_seconds 必须在 (0, {MAX_LONG_CALL_SECONDS}] 内"
        )

    def operation(state: RunState, now: float) -> WorkerClaim:
        self._check_revision(state, expected_revision)
        self._refresh(state, now)
        self._ensure_live_for_work(state)
        self._assert_target_fingerprint(state, now)
        claim = self._active_claim(state, worker_key, now, require_running=True)
        from core.supervisor.lease_manager import begin_long_call as _begin_long_call
        _begin_long_call(state, claim, now, expected, worker_key)
        return claim

    return self._mutate(operation)  # type: ignore[return-value]

def record_tool_call(
    self,
    worker_id: str,
    *,
    cost_units: float = 0.0,
    tool_name: str | None = None,
    expected_revision: int | None = None,
) -> RunState:
    """记录一次工具调用；本模块不会替 worker 执行工具。"""

    worker_key = self._normalize_worker_id(worker_id)
    cost = finite_number(cost_units, "cost_units")
    if cost < 0:
        raise InvalidCheckpoint("cost_units 不能为负")

    def operation(state: RunState, now: float) -> RunState:
        self._check_revision(state, expected_revision)
        self._refresh(state, now)
        self._ensure_live_for_work(state)
        self._assert_target_fingerprint(state, now)
        claim = self._active_claim(state, worker_key, now, require_running=True)
        if state.budget_tool_calls + 1 > state.budget.max_tool_calls:
            self._set_budget_exhausted(
                state, now, ["max_tool_calls"], event_type="tool_call_rejected",
            )
            raise BudgetExhausted("max_tool_calls 将被超过")
        if state.budget_cost_units + cost > state.budget.max_cost_units:
            self._set_budget_exhausted(
                state, now, ["max_cost_units"], event_type="tool_call_rejected",
            )
            raise BudgetExhausted("max_cost_units 将被超过")
        # A validated worker tool call is liveness evidence for no-PID
        # hosts.  Keep the shared lease path aligned with the host
        # contract: the next refresh can renew an expired lease when the
        # previous call is still within the heartbeat timeout window.
        claim.last_heartbeat_at = now
        state.tool_calls += 1
        state.budget_tool_calls += 1
        state.cost_units += float(cost)
        state.budget_cost_units += float(cost)
        self._append_event(
            state, "tool_call_recorded", now, worker_id=worker_key,
            lease_id=claim.lease_id, tool_name=tool_name or "unknown",
            cost_units=float(cost), budget_tool_calls=state.budget_tool_calls,
        )
        return state

    return self._mutate(operation)  # type: ignore[return-value]

# DES-03：以下方法是 submit_checkpoint 的状态变更体，由原先 328 行的
# 嵌套闭包提升而来；submit_checkpoint 只保留归一化 + 适配调用。
