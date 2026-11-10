#!/usr/bin/env python3
"""Supervisor Life context 绑定与事件编排边界。

输入入口、锁和持久化仍由 ``audit_supervisor.Supervisor`` 负责；本模块只
组合已校验的上下文字段并复用 Supervisor 的状态机窄边界。
"""

from __future__ import annotations

from .supervisor_models import InvalidCheckpoint, InvalidTransition, RunState, SHA256_RE


def apply_bind_context_operation(
    supervisor,
    state: RunState,
    now: float,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
    round_id: str | None = None,
    life_id: str | None = None,
    plan_hash: str | None = None,
) -> RunState:
    """``bind_context`` 的状态变更体（DES-03）。"""
    if state.active_claims:
        raise InvalidTransition("仍有 active worker，不能切换 Life 上下文")
    supervisor._ensure_live_for_work(state)
    supervisor._assert_target_fingerprint(state, now)
    if plan_hash is not None and (
        not isinstance(plan_hash, str) or not SHA256_RE.fullmatch(plan_hash)
    ):
        raise InvalidCheckpoint("plan_hash 必须是 64 位十六进制 SHA-256")
    for name, value in {
        "project_id": project_id,
        "session_id": session_id,
        "round_id": round_id,
        "life_id": life_id,
        "plan_hash": plan_hash,
    }.items():
        if value is not None:
            setattr(state, name, value)
    supervisor._append_event(
        state,
        "life_context_bound",
        now,
        project_id=state.project_id,
        session_id=state.session_id,
        round_id=state.round_id,
        life_id=state.life_id,
        plan_hash=state.plan_hash,
    )
    return state


__all__ = ["apply_bind_context_operation"]
