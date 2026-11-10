#!/usr/bin/env python3
"""Supervisor 新 run 的状态创建与 ``run_started`` 事件编排边界。

输入校验、锁和持久化仍由 ``audit_supervisor.Supervisor`` 负责；本模块只
组合已经校验的启动参数，避免生命周期创建逻辑与 Supervisor 门面耦合。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Sequence

from .supervisor_models import (
    COVERAGE_PENDING,
    InvalidCheckpoint,
    RunState,
    Status,
    target_identity,
)


def create_run(
    supervisor: Any,
    now: float,
    *,
    score: float,
    units: Sequence[str],
    run_id: str | None,
    project_id: str | None,
    session_id: str | None,
    round_id: str | None,
    life_id: str | None,
    plan_hash: str | None,
    life_kind: str,
    workspace_fingerprint: Callable[[Any], str],
) -> RunState:
    """Create one validated run state and append its start event."""
    identifier = run_id or f"run-{uuid.uuid4().hex}"
    if not identifier.strip():
        raise InvalidCheckpoint("run_id 不能为空")
    supervisor.target_fingerprint = workspace_fingerprint(supervisor.workspace_root)
    target_id = target_identity(supervisor.workspace_root)
    state = RunState(
        run_id=identifier,
        status=Status.RUNNING,
        round_no=1,
        life_score=float(score),
        budget=supervisor._configured_budget,
        life_kind=life_kind,
        started_at=now,
        budget_started_at=now,
        round_started_at=now,
        coverage={unit.strip(): COVERAGE_PENDING for unit in units},
        project_id=project_id,
        session_id=session_id,
        round_id=round_id,
        life_id=life_id,
        plan_hash=plan_hash,
        target_fingerprint=supervisor.target_fingerprint,
        dirty=True,
    )
    supervisor._append_event(
        state, "run_started", now, run_id=identifier,
        budget=state.budget.to_dict(), coverage_units=sorted(state.coverage),
        life_score=state.life_score,
        require_parallel=state.budget.require_parallel,
        serial_reason=state.budget.serial_reason,
        require_confirmation=state.budget.require_confirmation,
        project_id=project_id,
        session_id=session_id,
        round_id=round_id,
        life_id=life_id,
        life_kind=life_kind,
        plan_hash=plan_hash,
        target_fingerprint=supervisor.target_fingerprint,
    )
    supervisor._append_event(
        state,
        "target_fingerprint_bound",
        now,
        target_fingerprint=supervisor.target_fingerprint,
        target_identity=target_id,
    )
    return state


__all__ = ["create_run"]
