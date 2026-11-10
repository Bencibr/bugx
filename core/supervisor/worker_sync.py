#!/usr/bin/env python3
"""Supervisor Worker 启动确认的状态编排边界。"""

from __future__ import annotations

import logging
import time
import uuid

from core.host_runtime import host_registry

logger = logging.getLogger(__name__)

from .supervisor_models import (
    ClaimRejected,
    InvalidTransition,
    Status,
    WorkerClaim,
    process_start_token,
)


def apply_start_worker_operation(
    supervisor,
    state,
    now: float,
    *,
    worker_key: str,
    provider: str,
    execution_id: str | None,
    expected_revision: int | None,
    process_pid: int | None,
) -> WorkerClaim:
    """``start_worker`` 的状态变更体（P2-1 第四十切片）。"""
    supervisor._check_revision(state, expected_revision)
    supervisor._refresh(state, now)
    if state.status in {
        Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
    }:
        raise InvalidTransition(f"run 已结束: {state.status.value}")
    claim = supervisor._active_claim(state, worker_key, now)
    # 已成功获得合法 Claim 的 Worker 补登真实启动，属于履行已有租约，
    # 豁免 budget_exhausted 拦截，确保已完成的成果能够正常提交 (DOC-082)。
    if claim.execution_state not in {"claimed", "running"}:
        supervisor._ensure_live_for_work(state)
    supervisor._assert_target_fingerprint(state, now)
    requested_id = (execution_id or claim.execution_id).strip()
    if requested_id != claim.execution_id:
        raise ClaimRejected("execution_id 与 claim 不一致；不能替换执行身份")
    if process_pid is not None:
        claim.process_pid = int(process_pid)
        claim.process_start_token = process_start_token(claim.process_pid)
        claim.heartbeat_nonce = claim.heartbeat_nonce or uuid.uuid4().hex
    elif host_registry.host_requires_pid(provider):
        # DOC-239：宿主的「必须有真实 PID」是插件事实（``worker_runtime.pid_required``），
        # 调用方不再比较宿主名。
        logger.warning(
            "Worker %s 注册 provider=%s 但未提供 process_pid，孤儿租约回收将退化",
            worker_key,
            provider,
        )
    if claim.execution_state == "running":
        return claim
    if claim.execution_state != "claimed":
        raise ClaimRejected(
            f"Worker execution 不可启动: {worker_key} ({claim.execution_state})"
        )
    claim.provider = provider.strip()[:128]
    claim.execution_state = "running"
    claim.execution_started_at = now
    claim.last_heartbeat_at = now
    # A7 口径：活性/回收判定只读单调钟镜像。漏写 mono 会让 drain 把
    # ``last_heartbeat_mono`` 回退成 claim 时刻（worker_start 可能晚于
    # claim 数分钟），刚启动的 Worker 在下一次 sweep 即被
    # silent_unpid_worker_reclaimed 误杀（实测：worker_start 后 11s 被回收）。
    mono_clock = getattr(supervisor, "_mono_clock", None) or time.monotonic
    claim.last_heartbeat_mono = float(mono_clock())
    supervisor._reconcile_parallel(state, now)
    supervisor._append_event(
        state, "worker_started", now, worker_id=claim.worker_id,
        lease_id=claim.lease_id, execution_id=claim.execution_id,
        provider=claim.provider, artifact_root=claim.artifact_root,
        process_pid=claim.process_pid,
        process_start_token=claim.process_start_token,
    )
    return claim


__all__ = ["apply_start_worker_operation"]
