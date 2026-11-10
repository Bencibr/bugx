#!/usr/bin/env python3
"""Supervisor 只读观测聚合。

该模块只处理已经读取到内存的状态，不获取锁、不读文件，也不修改运行状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_decision_snapshot(state: Any, now: float) -> dict[str, object]:
    """构造自治决策所需的不可变字段快照。"""
    coverage = getattr(state, "coverage", {}) or {}
    total_units = len(coverage)
    covered_units = sum(
        1 for status in coverage.values() if status == "covered"
    )
    # A7（Wave 3 口径）：活跃判定不得只看墙钟。优先用 ``WorkerClaim.is_expired``
    # （内部只信单调时钟）；对象缺该方法时按 ``lease_expires_mono`` 判定；
    # 两者都没有才退回墙钟（兼容测试/第三方 claim 形状，不是契约兼容层）。
    def _is_live(claim: object) -> bool:
        expired_check = getattr(claim, "is_expired", None)
        if callable(expired_check):
            return not expired_check(now)
        lease_mono = getattr(claim, "lease_expires_mono", None)
        if lease_mono is not None:
            return float(lease_mono) > now
        return getattr(claim, "lease_expires_at", 0) > now

    active_claims = [
        claim for claim in getattr(state, "claims", {}).values() if _is_live(claim)
    ]
    status = getattr(state, "status", "")
    return {
        "status": status.value if hasattr(status, "value") else str(status),
        "coverage_complete": bool(getattr(state, "coverage_complete", False)),
        "total_units": total_units,
        "covered_units": covered_units,
        "coverage_percentage": (
            round(covered_units / total_units * 100, 2)
            if total_units > 0 else 0.0
        ),
        "budget_exhausted": bool(getattr(state, "budget_exhausted", False)),
        "budget_reasons": list(getattr(state, "budget_reasons", None) or []),
        "active_claims_count": len(active_claims),
        "findings_count": len(getattr(state, "findings", []) or []),
        "revision": getattr(state, "revision", 0),
        "scheduler_epoch": getattr(state, "scheduler_epoch", 0),
        "now": now,
    }


def build_parallel_gate_snapshot(
    state: Any,
    gate: Mapping[str, object],
    target_gate: Mapping[str, object],
) -> dict[str, object]:
    """Add target and coverage observations to an already computed gate.

    Gate calculation and target fingerprint calculation stay in their domain
    helpers.  This function only combines their read-only results for status
    consumers; it does not refresh state or access Supervisor-owned resources.
    """
    result = dict(gate)
    result.update(target_gate)
    if result.get("target_drift"):
        result["satisfied"] = False
        result["reasons"] = [*result.get("reasons", []), "target_drift"]
    # A1（TODO-009）：把「拒绝原因 → 恢复动作」内联进快照，使宿主/Agent 无需
    # 试错即可知道下一步。未登记原因 fail-closed 标注（`registered: false`），
    # 绝不被静默当作"无需恢复"——BX-ENGINE-24 正是"读到原因却不知下一步"的代价。
    from core.supervisor.rejection_remedies import remedies_for
    result["reason_remedies"] = remedies_for(result.get("reasons"))
    try:
        from .supervisor_models import UNIT_LEVEL_DEBT
    except ImportError:
        try:
            from .supervisor_models import UNIT_LEVEL_DEBT
        except ImportError:
            UNIT_LEVEL_DEBT = {"pending", "partial", "unverified", "stale", "blocked"}

    coverage = getattr(state, "coverage", {}) or {}
    result["coverage_units"] = len(coverage)
    pending_units = sum(
        status in UNIT_LEVEL_DEBT for status in coverage.values()
    )
    result["pending_units"] = pending_units
    active_claims = getattr(state, "active_claims", {}) or {}
    claimed_units = {
        unit
        for claim in (active_claims.values() if isinstance(active_claims, Mapping) else [])
        for unit in getattr(claim, "unit_ids", [])
    }
    # BX-ENGINE-23：调度器派发资格还包含「本轮已被 active|submitted claim 占用」——
    # `lease_manager.claimed_units_this_round(state)` 正是该口径。注意它**不含**
    # released/expired：那些是"重试债"（见 `budget_governor.parallel_gate_result` 的
    # 注释），其单元仍可被重新领取。
    # 本门禁此前只排除 **active** claim，把 **submitted**（已交付但本轮尚未终结）占用的
    # 单元也计入可申领数，导致调度器按虚高数字派发并立刻收到 ClaimRejected
    # （真实项目实测：gate 263 vs 实际可领取 235，虚高 28 个单元）。
    claimed_this_round: set[str] = set()
    try:
        from core.supervisor.lease_manager import claimed_units_this_round
        claimed_this_round = claimed_units_this_round(state)
    except Exception:
        claimed_this_round = set()
    # DOC-205 S3: 门禁观察口径与调度器派发资格（eligible_units & worker_attempts_exhausted）严格一致
    try:
        from core.supervisor.lease_manager import eligible_units
        from .supervisor_models import worker_attempts_exhausted
        eligible = eligible_units(state)
        unclaimed = sum(
            status in UNIT_LEVEL_DEBT
            and unit not in claimed_units
            and unit not in claimed_this_round
            and unit in eligible
            and not worker_attempts_exhausted(state, unit)
            for unit, status in coverage.items()
        )
    except Exception:
        unclaimed = sum(
            status in UNIT_LEVEL_DEBT
            and unit not in claimed_units
            and unit not in claimed_this_round
            for unit, status in coverage.items()
        )
    result["unclaimed_pending_units"] = unclaimed
    result["claimed_this_round_units"] = len(claimed_this_round)

    budget = getattr(state, "budget", None)
    max_c = int(getattr(budget, "max_concurrency", 1) or 1)
    current_active = len(active_claims)
    result["available_slots"] = max(0, max_c - current_active)
    result["max_concurrency"] = max_c
    return result


__all__ = ["build_decision_snapshot", "build_parallel_gate_snapshot"]
