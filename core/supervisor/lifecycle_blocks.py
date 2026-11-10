#!/usr/bin/env python3
"""core/supervisor/lifecycle_blocks.py - 生命周期阻塞记录与所有者取消残渣清理。"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .supervisor_models import (
    MAX_LIFECYCLE_BLOCKED_PER_GATE,
    NoRunError,
    PersistenceError,
    RunState,
    SupervisorError,
)

from core.supervisor import finding_key

if TYPE_CHECKING:
    from .audit_supervisor import Supervisor

logger = logging.getLogger(__name__)

def _count_lifecycle_blocks(
    state: RunState, event_type: str, operation: str, gate: str,
) -> int:
    """统计同 (operation, gate) 的指定类型事件数（节流判定用）。"""
    return sum(
        1 for e in state.history
        if e.get("type") == event_type
        and e.get("operation") == operation and e.get("gate") == gate
    )

@staticmethod
def _lifecycle_block_fields(
    operation: str, gate: str, safe_reason: str, finding_key: str | None,
) -> dict[str, object]:
    """构造 lifecycle_blocked 事件字段（节流与清淤共用同一份）。"""
    fields: dict[str, object] = {
        "operation": operation, "gate": gate, "reason": safe_reason,
    }
    if finding_key:
        fields["finding_key"] = str(finding_key)[:512]
    return fields

@staticmethod
def _release_owner_cancelled_debris(
    state: RunState, fields: dict[str, object],
) -> None:
    """D27 即时清淤：宿主取消回传时释放「已领取但从未 worker-start」的
    队列残渣 claim。

    占着并发槽等到 1800s 租约到期纯属浪费。已 running 的 claim 不动——
    取消后由后继 worker 在原 claim 上合法续跑（实测路径），或由租约
    drain 按心跳超时回收。
    """
    released: list[str] = []
    for worker_id, claim in state.claims.items():
        if (
            getattr(claim, "state", "") == "active"
            and getattr(claim, "execution_state", "") == "claimed"
        ):
            claim.state = "preempted"
            claim.preempted_reason = "owner_cancelled_queue_debris"
            released.append(worker_id)
    if released:
        fields["released_claims"] = released

def record_lifecycle_block(
    self,
    *,
    operation: str,
    gate: str,
    reason: str,
    finding_key: str | None = None,
    expected_revision: int | None = None,
) -> bool:
    """DOC-204 L1：把 post/sync/checkpoint 的阻断以 lifecycle_blocked 事件
    落入权威哈希链（独立 _mutate，非嵌套在失败事务内——checkpoint 的
    atomic_pair 路径异常不落盘，审计员 #2 BLOCK 修订）。

    fail-closed：链损坏（validate_history 失败）或 NoRunError 时拒绝登记
    并 stderr 告警（带外修复的审计盲区如实暴露，不做链外伪登记）。
    节流：同 (operation, gate) 超过 MAX_LIFECYCLE_BLOCKED_PER_GATE 条后
    合并为单条 lifecycle_blocked_throttled{count}。
    返回 True 表示已登记。**不豁免任何门禁，仅留痕。**
    """
    safe_reason = str(reason)[:512]
    try:
        def _append_block(state: RunState, now: float) -> RunState:
            blocked = self._count_lifecycle_blocks(
                state, "lifecycle_blocked", operation, gate)
            throttled = self._count_lifecycle_blocks(
                state, "lifecycle_blocked_throttled", operation, gate)
            fields = self._lifecycle_block_fields(
                operation, gate, safe_reason, finding_key)
            if gate == "owner_cancelled":
                self._release_owner_cancelled_debris(state, fields)
            if blocked >= MAX_LIFECYCLE_BLOCKED_PER_GATE:
                # 累计被抑制总数：50 上限 + 此前 throttled 条数 + 本条
                fields["throttled_count"] = (
                    MAX_LIFECYCLE_BLOCKED_PER_GATE + throttled + 1)
                self._append_event(
                    state, "lifecycle_blocked_throttled", now, **fields,
                )
            else:
                self._append_event(
                    state, "lifecycle_blocked", now, **fields,
                )
            return state

        self._mutate(_append_block)
        return True
    except (NoRunError, PersistenceError) as exc:
        print(
            f"[bugx] ⚠ lifecycle_blocked 登记失败（{type(exc).__name__}: {exc}）"
            "——权威链不可用，阻断原因仅本次输出可见",
            file=sys.stderr,
        )
        return False

