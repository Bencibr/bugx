"""覆盖失效事务体（DES-03：把大闭包提升为具名操作）。

``L1-ADV-D4``：内容绑定覆盖一旦判定"字节变了旧证据失效"，必须有一条引擎侧
唯一退回路径把 ``covered`` 退回 ``pending``，否则 ``TERMINAL_COVERED`` 会让
claim 永久拒绝该单元，单轮内无法重新覆盖。

本模块只放 ``AuditSupervisor.invalidate_coverage`` 的**事务体**；方法本身保持薄壳
（文件锁、revision 栅栏、事件与持久化都由 ``Supervisor._mutate`` 提供）。
"""

from __future__ import annotations

from typing import Iterable

from .supervisor_models import (
    COVERAGE_COVERED,
    COVERAGE_PENDING,
    ClaimRejected,
    InvalidCheckpoint,
    RunState,
)


FENCEABLE_CLAIM_STATES = frozenset({"submitted", "released", "expired", "superseded"})


def normalize_units(units: Iterable[str]) -> list[str]:
    """校验并去重宿主提交的覆盖单元（不接受臆造 id / 空值 / 字符串）。"""
    if isinstance(units, (str, bytes)) or not hasattr(units, "__iter__"):
        raise InvalidCheckpoint("units 必须是字符串序列")
    wanted: list[str] = []
    for unit in units:
        if not isinstance(unit, str) or not unit.strip():
            raise InvalidCheckpoint("coverage unit 必须是非空字符串")
        name = unit.strip()
        if name not in wanted:
            wanted.append(name)
    if not wanted:
        raise InvalidCheckpoint("invalidate_coverage 必须提供至少一个 coverage unit")
    return wanted


def apply_invalidation(
    supervisor: object,
    state: RunState,
    now: float,
    *,
    units: list[str],
    reason: str,
    expected_revision: int | None = None,
) -> dict[str, object]:
    """``covered -> pending`` 的唯一路径，含历史代钉扎（fenced claims）。"""
    supervisor._check_revision(state, expected_revision)  # type: ignore[attr-defined]
    unknown = [unit for unit in units if unit not in state.coverage]
    if unknown:
        raise InvalidCheckpoint("未知 coverage unit: " + ", ".join(sorted(unknown)))
    active = {
        str(unit)
        for claim in state.claims.values() if getattr(claim, "active", False)
        for unit in getattr(claim, "unit_ids", ())
    }
    busy = sorted(active & set(units))
    if busy:
        raise ClaimRejected(
            "覆盖单元仍有活跃租约，拒绝在飞行中失效: " + ", ".join(busy)
        )
    invalidated = [
        unit for unit in units if state.coverage.get(unit) == COVERAGE_COVERED
    ]
    skipped = [unit for unit in units if unit not in invalidated]
    for unit in invalidated:
        state.coverage[unit] = COVERAGE_PENDING
    fenced = fence_claims_for(state, invalidated)
    detail = str(reason).strip()
    if invalidated:
        supervisor._append_event(  # type: ignore[attr-defined]
            state, "coverage_invalidated", now,
            units=list(invalidated), reason=detail,
            channel="coverage_invalidate",
            fenced_claims=sorted(fenced),
            previous_revision=state.revision,
            next_action="REDISPATCH_UNITS",
        )
    return {
        "invalidated": list(invalidated),
        "skipped": list(skipped),
        "fenced_claims": sorted(fenced),
        "reason": detail,
        "coverage_complete": state.coverage_complete,
        "revision": state.revision,
    }


def fence_claims_for(state: RunState, units: list[str]) -> list[str]:
    """把失效单元的历史 claim 钉成历史代。

    同一 Round 的去重规则（``lease_manager.claimed_units_this_round``）按"当前覆盖
    代际"生效：内容失效使旧 claim 的产物作废，不钉扎就会继续拒绝重新派发。
    ``covered`` 单元本身不可申领（``TERMINAL_COVERED``），因此钉扎不会制造同一
    单元的并发重复劳动；活跃租约在调用方已被 fail-closed 拒绝。
    """
    targets = set(units)
    if not targets:
        return []
    fenced: list[str] = []
    for worker_key, claim in state.claims.items():
        if not (set(getattr(claim, "unit_ids", ())) & targets):
            continue
        if getattr(claim, "state", "") not in FENCEABLE_CLAIM_STATES:
            continue
        if getattr(claim, "fenced_from_attempts", False):
            continue
        claim.fenced_from_attempts = True
        fenced.append(worker_key)
    return sorted(fenced)
