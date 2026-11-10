#!/usr/bin/env python3
"""core/supervisor/worker_claim.py - Worker 一次性租约与真实执行状态模型 (DOC-070 / DOC-138 / DOC-248)."""

from __future__ import annotations

import copy
import math
import re
import time
from dataclasses import dataclass, field
from typing import Mapping

from core.supervisor_types.exceptions import PersistenceError

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_PROCESS_START_TOKEN_LENGTH = 1024


@dataclass
class WorkerClaim:
    """一次性 worker lease 及其真实执行状态。"""

    worker_id: str
    lease_id: str
    round_no: int
    claimed_at: float
    lease_expires_at: float
    state: str = "active"
    submitted_at: float | None = None
    unit_ids: tuple[str, ...] = ()
    resource_keys: tuple[str, ...] = ()
    attempt: int = 1
    fenced_from_attempts: bool = False
    write_access: bool | None = None
    source_baseline: dict[str, object] | None = None
    scheduler_epoch: int = 0
    execution_id: str = ""
    execution_state: str = "claimed"
    provider: str = "unknown"
    artifact_root: str = ""
    execution_started_at: float | None = None
    execution_finished_at: float | None = None
    last_heartbeat_at: float | None = None
    last_checkpoint_id: str | None = None
    last_checkpoint_digest: str | None = None
    unit_notes: dict[str, dict[str, object]] = field(default_factory=dict)
    process_pid: int | None = None
    process_start_token: str | None = None
    heartbeat_nonce: str | None = None
    preempted_reason: str | None = None
    superseded_by: str | None = None
    renewal_count: int = 0
    total_lease_deadline: float | None = None
    tokens_consumed: int = 0
    token_method: str = "unmeasured"
    long_call_count: int = 0
    salvaged_at: float | None = None
    assignment_kind: str | None = None
    graph_task_id: str | None = None
    claimed_mono: float | None = None
    lease_expires_mono: float | None = None
    last_heartbeat_mono: float | None = None
    total_lease_deadline_mono: float | None = None
    worker_role: str = "generalist"
    domain_affinity: str = "logic"
    model_tier: str = "PRO"
    target_form: str = "backend"
    tech_stack: str = "generic"
    assigned_fixtures: tuple[str, ...] = ()
    active_probes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.claimed_mono is None:
            self.claimed_mono = time.monotonic()
        if self.lease_expires_mono is None:
            remaining = max(0.0, self.lease_expires_at - self.claimed_at)
            self.lease_expires_mono = self.claimed_mono + remaining
        if self.last_heartbeat_mono is None:
            self.last_heartbeat_mono = self.claimed_mono
        if self.total_lease_deadline_mono is None and self.total_lease_deadline is not None:
            self.total_lease_deadline_mono = self.claimed_mono + (
                self.total_lease_deadline - self.claimed_at
            )

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def lease_seconds(self) -> float:
        return max(0.0, self.lease_expires_at - self.claimed_at)

    def is_expired(self, now: float | None = None, *, now_mono: float | None = None) -> bool:
        if not self.active:
            return False
        mono_now = time.monotonic() if now_mono is None else float(now_mono)
        if self.lease_expires_mono is not None and mono_now >= self.lease_expires_mono:
            return True
        return (
            self.total_lease_deadline_mono is not None
            and mono_now >= self.total_lease_deadline_mono
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "lease_id": self.lease_id,
            "round_no": self.round_no,
            "claimed_at": self.claimed_at,
            "lease_expires_at": self.lease_expires_at,
            "state": self.state,
            "submitted_at": self.submitted_at,
            "unit_ids": list(self.unit_ids),
            "resource_keys": list(self.resource_keys),
            "attempt": self.attempt,
            "fenced_from_attempts": self.fenced_from_attempts,
            "write_access": self.write_access,
            "source_baseline": self.source_baseline,
            "scheduler_epoch": self.scheduler_epoch,
            "execution_id": self.execution_id,
            "execution_state": self.execution_state,
            "provider": self.provider,
            "artifact_root": self.artifact_root,
            "execution_started_at": self.execution_started_at,
            "execution_finished_at": self.execution_finished_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_checkpoint_id": self.last_checkpoint_id,
            "last_checkpoint_digest": self.last_checkpoint_digest,
            "unit_notes": copy.deepcopy(self.unit_notes),
            "process_pid": self.process_pid,
            "process_start_token": self.process_start_token,
            "heartbeat_nonce": self.heartbeat_nonce,
            "preempted_reason": self.preempted_reason,
            "superseded_by": self.superseded_by,
            "renewal_count": self.renewal_count,
            "total_lease_deadline": self.total_lease_deadline,
            "tokens_consumed": self.tokens_consumed,
            "token_method": self.token_method,
            "long_call_count": self.long_call_count,
            "salvaged_at": self.salvaged_at,
            "assignment_kind": self.assignment_kind,
            "graph_task_id": self.graph_task_id,
            "claimed_mono": self.claimed_mono,
            "lease_expires_mono": self.lease_expires_mono,
            "last_heartbeat_mono": self.last_heartbeat_mono,
            "total_lease_deadline_mono": self.total_lease_deadline_mono,
            "worker_role": self.worker_role,
            "domain_affinity": self.domain_affinity,
            "model_tier": self.model_tier,
            "target_form": self.target_form,
            "tech_stack": self.tech_stack,
            "assigned_fixtures": list(self.assigned_fixtures),
            "active_probes": list(self.active_probes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "WorkerClaim":
        mono_anchors: dict[str, float] = {}
        for key in ("claimed_mono", "lease_expires_mono", "last_heartbeat_mono"):
            raw_mono = value.get(key)
            if raw_mono is None:
                raise PersistenceError(
                    f"worker claim mono 时钟锚缺失: {key}；state 需重建"
                    "（旧快照无单调时钟锚，禁止静默补齐）"
                )
            try:
                mono_anchors[key] = float(raw_mono)
            except (TypeError, ValueError) as exc:
                raise PersistenceError(
                    f"worker claim mono 时钟锚无效: {key}；state 需重建"
                ) from exc
        try:
            raw_units = value.get("unit_ids", [])
            raw_resources = value.get("resource_keys", [])
            if not isinstance(raw_units, (list, tuple)) \
                    or not isinstance(raw_resources, (list, tuple)):
                raise TypeError("unit_ids/resource_keys 必须是数组")
            unit_ids = tuple(str(item).strip() for item in raw_units)
            resource_keys = tuple(str(item).strip() for item in raw_resources)
            if any(not item for item in (*unit_ids, *resource_keys)):
                raise ValueError("unit_ids/resource_keys 不能包含空值")
            fenced_from_attempts = value.get("fenced_from_attempts", False)
            if not isinstance(fenced_from_attempts, bool):
                raise TypeError("fenced_from_attempts 必须是布尔值")
            claim = cls(
                worker_id=str(value["worker_id"]),
                lease_id=str(value["lease_id"]),
                round_no=int(value["round_no"]),
                claimed_at=float(value["claimed_at"]),
                lease_expires_at=float(value["lease_expires_at"]),
                state=str(value.get("state", "active")),
                submitted_at=(
                    None if value.get("submitted_at") is None
                    else float(value["submitted_at"])
                ),
                unit_ids=unit_ids,
                resource_keys=resource_keys,
                attempt=int(value.get("attempt", 1)),
                fenced_from_attempts=fenced_from_attempts,
                write_access=(
                    None if value.get("write_access") is None
                    else bool(value.get("write_access"))
                ),
                source_baseline=(
                    value.get("source_baseline")
                    if isinstance(value.get("source_baseline"), dict) else None
                ),
                scheduler_epoch=int(value.get("scheduler_epoch", 0)),
                execution_id=str(value.get("execution_id", "")),
                execution_state=str(value.get("execution_state", "claimed")),
                provider=str(value.get("provider", "unknown")),
                artifact_root=str(value.get("artifact_root", "")),
                execution_started_at=(
                    None if value.get("execution_started_at") is None
                    else float(value["execution_started_at"])
                ),
                execution_finished_at=(
                    None if value.get("execution_finished_at") is None
                    else float(value["execution_finished_at"])
                ),
                last_heartbeat_at=(
                    None if value.get("last_heartbeat_at") is None
                    else float(value["last_heartbeat_at"])
                ),
                last_checkpoint_id=(
                    None if value.get("last_checkpoint_id") is None
                    else str(value["last_checkpoint_id"])
                ),
                last_checkpoint_digest=(
                    None if value.get("last_checkpoint_digest") is None
                    else str(value["last_checkpoint_digest"])
                ),
                unit_notes=dict(value.get("unit_notes", {})),
                process_pid=(
                    None if value.get("process_pid") is None
                    else int(value["process_pid"])
                ),
                process_start_token=(
                    None if value.get("process_start_token") is None
                    else str(value["process_start_token"])
                ),
                heartbeat_nonce=(
                    None if value.get("heartbeat_nonce") is None
                    else str(value["heartbeat_nonce"])
                ),
                preempted_reason=(
                    None if value.get("preempted_reason") is None
                    else str(value["preempted_reason"])
                ),
                superseded_by=(
                    None if value.get("superseded_by") is None
                    else str(value["superseded_by"])
                ),
                renewal_count=int(value.get("renewal_count", 0)),
                total_lease_deadline=(
                    None if value.get("total_lease_deadline") is None
                    else float(value["total_lease_deadline"])
                ),
                tokens_consumed=int(value.get("tokens_consumed", 0)),
                token_method=str(value.get("token_method", "unmeasured")),
                long_call_count=int(value.get("long_call_count", 0)),
                salvaged_at=(
                    None if value.get("salvaged_at") is None
                    else float(value["salvaged_at"])
                ),
                assignment_kind=(
                    None if value.get("assignment_kind") is None
                    else str(value["assignment_kind"])
                ),
                graph_task_id=(
                    None if value.get("graph_task_id") is None
                    else str(value["graph_task_id"]).strip()
                ),
                claimed_mono=mono_anchors["claimed_mono"],
                lease_expires_mono=mono_anchors["lease_expires_mono"],
                last_heartbeat_mono=mono_anchors["last_heartbeat_mono"],
                total_lease_deadline_mono=(
                    None if value.get("total_lease_deadline_mono") is None
                    else float(value["total_lease_deadline_mono"])
                ),
                worker_role=str(value.get("worker_role") or "generalist"),
                domain_affinity=str(value.get("domain_affinity") or "logic"),
                model_tier=str(value.get("model_tier") or "PRO"),
                target_form=str(value.get("target_form") or "backend"),
                tech_stack=str(value.get("tech_stack") or "generic"),
                assigned_fixtures=tuple(str(x) for x in (value.get("assigned_fixtures") or ())),
                active_probes=tuple(str(x) for x in (value.get("active_probes") or ())),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(f"worker claim 无效: {exc}") from exc
        if not claim.worker_id or not claim.lease_id:
            raise PersistenceError("worker claim 的 id 不能为空")
        if claim.process_pid is not None and claim.process_pid <= 0:
            raise PersistenceError("worker claim process_pid 必须是正整数或 null")
        if claim.process_start_token is not None and (
            not claim.process_start_token.strip()
            or len(claim.process_start_token) > MAX_PROCESS_START_TOKEN_LENGTH
        ):
            raise PersistenceError("worker claim process_start_token 无效")
        if claim.heartbeat_nonce is not None and (
            not claim.heartbeat_nonce.strip() or len(claim.heartbeat_nonce) > 128
        ):
            raise PersistenceError("worker claim heartbeat_nonce 无效")
        for name, number in (
            ("claimed_at", claim.claimed_at),
            ("lease_expires_at", claim.lease_expires_at),
        ):
            if not math.isfinite(number) or number <= 0:
                raise PersistenceError(f"worker claim {name} 必须是有限正数")
        if claim.submitted_at is not None and (
            not math.isfinite(claim.submitted_at) or claim.submitted_at <= 0
        ):
            raise PersistenceError("worker claim submitted_at 必须是有限正数")
        if claim.round_no < 1:
            raise PersistenceError("worker claim round_no 必须 >= 1")
        if claim.state not in {"active", "submitted", "expired", "released", "superseded"}:
            raise PersistenceError(f"未知 claim 状态: {claim.state}")
        execution_by_claim_state = {
            "active": {"claimed", "running"},
            "submitted": {"completed"},
            "expired": {"expired"},
            "released": {"released"},
            "superseded": {"released"},
        }
        if claim.execution_state not in execution_by_claim_state[claim.state]:
            raise PersistenceError(
                f"claim state={claim.state} 与 execution_state={claim.execution_state} 不一致"
            )
        if claim.state == "submitted" and claim.submitted_at is None:
            raise PersistenceError("submitted claim 必须有 submitted_at")
        if claim.state == "superseded" and not claim.superseded_by:
            raise PersistenceError("superseded claim 必须有 superseded_by")
        if len(set(claim.unit_ids)) != len(claim.unit_ids):
            raise PersistenceError("worker claim unit_ids 不能重复")
        if len(set(claim.resource_keys)) != len(claim.resource_keys):
            raise PersistenceError("worker claim resource_keys 不能重复")
        if claim.attempt < 1:
            raise PersistenceError("worker claim attempt 必须 >= 1")
        if claim.scheduler_epoch < 0:
            raise PersistenceError("worker claim scheduler_epoch 必须 >= 0")
        if claim.execution_state not in {"claimed", "running", "completed", "expired", "released"}:
            raise PersistenceError(f"未知 worker execution 状态: {claim.execution_state}")
        if not claim.execution_id and claim.execution_state not in {"claimed", "expired", "released"}:
            raise PersistenceError("running/completed worker 必须有 execution_id")
        if not claim.provider.strip():
            raise PersistenceError("worker provider 不能为空")
        for name in ("execution_started_at", "execution_finished_at", "last_heartbeat_at"):
            number = getattr(claim, name)
            if number is not None and (not math.isfinite(number) or number <= 0):
                raise PersistenceError(f"worker claim {name} 必须是有限正数或 null")
        if claim.execution_state in {"running", "completed"} and claim.execution_started_at is None:
            raise PersistenceError("running/completed worker 必须有 execution_started_at")
        if claim.last_checkpoint_id is not None and (
            not claim.last_checkpoint_id.strip()
            or len(claim.last_checkpoint_id) > 128
            or any(ord(char) < 32 for char in claim.last_checkpoint_id)
        ):
            raise PersistenceError("last_checkpoint_id 无效")
        if claim.last_checkpoint_digest is not None and not SHA256_RE.fullmatch(
            claim.last_checkpoint_digest
        ):
            raise PersistenceError("last_checkpoint_digest 必须是 SHA-256")
        if claim.renewal_count < 0:
            raise PersistenceError("worker claim 续租计数必须是非负整数")
        for name in ("claimed_mono", "lease_expires_mono", "last_heartbeat_mono"):
            number = getattr(claim, name)
            if not math.isfinite(number):
                raise PersistenceError(f"worker claim {name} 必须是有限数")
        if claim.total_lease_deadline_mono is not None and (
            not math.isfinite(claim.total_lease_deadline_mono)
        ):
            raise PersistenceError(
                "worker claim total_lease_deadline_mono 必须是有限数或 null"
            )
        if claim.long_call_count < 0:
            raise PersistenceError("worker claim long_call_count 必须是非负整数")
        if claim.salvaged_at is not None and (
            not math.isfinite(claim.salvaged_at) or claim.salvaged_at <= 0
        ):
            raise PersistenceError("worker claim salvaged_at 必须是有限正数或 null")
        if claim.total_lease_deadline is not None and (
            not math.isfinite(claim.total_lease_deadline)
            or claim.total_lease_deadline <= 0
        ):
            raise PersistenceError("worker claim total_lease_deadline 必须是有限正数或 null")
        if claim.total_lease_deadline is not None and (
            claim.total_lease_deadline < claim.claimed_at
            or claim.total_lease_deadline < claim.lease_expires_at
        ):
            raise PersistenceError(
                "worker claim total_lease_deadline 不能早于 claim 当前租约"
            )
        if claim.assignment_kind not in {None, "unit", "graph_stage"}:
            raise PersistenceError(f"未知 worker assignment_kind: {claim.assignment_kind}")
        if claim.assignment_kind == "graph_stage":
            if not claim.graph_task_id or claim.unit_ids:
                raise PersistenceError(
                    "graph_stage claim 必须绑定 graph_task_id 且 unit_ids 为空"
                )
        elif claim.graph_task_id is not None:
            raise PersistenceError("非 graph_stage claim 不能绑定 graph_task_id")
        return claim
