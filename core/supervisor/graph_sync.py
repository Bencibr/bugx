#!/usr/bin/env python3
"""DOC-232 graph transactions over the Supervisor's authoritative snapshot.

Callers enter these functions from ``Supervisor._mutate``.  They do not acquire
their own lock or persist independently: graph state, worker claims, lease
events, observations, and the bug ledger commit in one Supervisor revision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .supervisor_models import (
    AgentBinding,
    BugRecord,
    BudgetExhausted,
    ClaimRejected,
    DEFAULT_MAX_RENEWALS,
    GraphRun,
    GraphTask,
    InvalidCheckpoint,
    InvalidTransition,
    MAX_LEASE_SECONDS,
    PersistenceError,
    Status,
    WorkerClaim,
    worker_artifact_root,
    process_start_token,
)

from core.supervisor.budget_governor import observe_clock


def _graph(state) -> GraphRun:
    graph = getattr(state, "agent_graph", None)
    if not isinstance(graph, GraphRun):
        raise InvalidTransition("当前 Supervisor run 尚未启动 agent graph")
    return graph


def _canonical_digest(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidCheckpoint(f"graph checkpoint payload 不是有限 JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _normalize_tasks(tasks: Sequence[GraphTask | Mapping[str, object]]) -> dict[str, GraphTask]:
    if isinstance(tasks, (str, bytes)) or not isinstance(tasks, Sequence):
        raise InvalidCheckpoint("graph tasks 必须是数组")
    normalized: dict[str, GraphTask] = {}
    for raw in tasks:
        try:
            task = GraphTask.from_dict(raw.to_dict() if isinstance(raw, GraphTask) else raw)
        except (PersistenceError, AttributeError, TypeError) as exc:
            raise InvalidCheckpoint(f"graph task 无效: {exc}") from exc
        if task.task_id in normalized:
            raise InvalidCheckpoint(f"重复 graph task_id: {task.task_id}")
        if task.state != "queued" or task.attempt != 0 or task.claim_worker_id is not None:
            raise InvalidCheckpoint("新 graph task 必须以 queued/attempt=0/无 claim 初始化")
        normalized[task.task_id] = task
    if not normalized:
        raise InvalidCheckpoint("agent graph 至少需要一个 task")
    return normalized


def graph_start_sync(
    supervisor: Any,
    state: Any,
    now: float,
    *,
    graph_id: str,
    goal: str,
    origin_root: str,
    snapshot_digest: str,
    tasks: Sequence[GraphTask | Mapping[str, object]],
    expected_revision: int | None = None,
) -> GraphRun:
    """Create and persist a graph plan inside the caller's Supervisor lock."""
    supervisor._check_revision(state, expected_revision)
    if state.status in {Status.COMPLETE, Status.FAILED, Status.INCOMPLETE}:
        raise InvalidTransition(f"run 已结束: {state.status.value}")
    if state.agent_graph is not None:
        raise InvalidTransition("当前 run 已存在 agent graph")
    graph_key = graph_id.strip() if isinstance(graph_id, str) else ""
    root = origin_root.strip() if isinstance(origin_root, str) else ""
    if not graph_key or not root:
        raise InvalidCheckpoint("graph_id/origin_root 不能为空")
    try:
        graph = GraphRun.from_dict({
            "schema_version": 1,
            "graph_id": graph_key,
            "goal": goal,
            "origin_root": root,
            "snapshot_digest": snapshot_digest,
            "tasks": {key: task.to_dict() for key, task in _normalize_tasks(tasks).items()},
            "agent_bindings": {},
            "observations": [],
            "bug_index": {},
            "review_coverage": {},
            "report_revision": 0,
        })
    except PersistenceError as exc:
        raise InvalidCheckpoint(f"agent graph 无效: {exc}") from exc
    state.agent_graph = graph
    supervisor._append_event(
        state, "graph_started", now,
        graph_id=graph.graph_id, graph_goal=graph.goal,
        origin_root=graph.origin_root, snapshot_digest=graph.snapshot_digest,
        task_ids=sorted(graph.tasks),
    )
    return graph


def _capacity_available(supervisor: Any, state: Any, now: float, worker_id: str) -> None:
    active = state.active_claims
    capacity_ok = True
    probe_failed = False
    if (
        state.budget.adaptive_concurrency
        and state.budget.require_parallel
        and state.budget.max_concurrency > 1
    ):
        try:
            from . import task_scheduler

            controller = task_scheduler.AdaptiveConcurrencyController(
                min_concurrency=state.budget.min_concurrency,
                max_concurrency=state.budget.max_concurrency_ceiling,
            )
            capacity_ok = controller.compute_capacity(
                len(active), state.budget, state,
            ) > 0
        except Exception:
            capacity_ok = False
            probe_failed = True
    else:
        capacity_ok = len(active) < state.budget.max_concurrency
    if capacity_ok:
        return
    supervisor._append_event(
        state, "claim_rejected", now, worker_id=worker_id,
        reason=("concurrency_probe_failed" if probe_failed else "max_concurrency"),
        assignment_kind="graph_stage",
    )
    if probe_failed:
        raise ClaimRejected("并发容量探针失败，拒绝 fail-open graph claim")
    raise ClaimRejected("max_concurrency 已满")


def graph_claim_sync(
    supervisor: Any,
    state: Any,
    now: float,
    *,
    worker_id: str,
    graph_task_id: str,
    lease_seconds: float,
    expected_revision: int | None = None,
    process_pid: int | None = None,
    requested_resources: Sequence[str] = (),
) -> WorkerClaim:
    """Claim one graph task without creating or claiming a coverage unit."""
    supervisor._check_revision(state, expected_revision)
    supervisor._refresh(state, now)  # also reclaims expired claims before capacity is measured
    supervisor._assert_target_fingerprint(state, now)
    if state.status == Status.BUDGET_EXHAUSTED or state.budget_exhausted:
        raise BudgetExhausted(
            "预算已耗尽: " + ", ".join(state.budget_reasons or ["unknown"])
        )
    supervisor._ensure_live_for_work(state)
    graph = _graph(state)
    task_id = graph_task_id.strip() if isinstance(graph_task_id, str) else ""
    task = graph.tasks.get(task_id)
    if task is None:
        raise ClaimRejected(f"未知 graph task_id: {task_id}")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ClaimRejected("worker_id 不能为空")
    if not isinstance(lease_seconds, (int, float)) or isinstance(lease_seconds, bool):
        raise ClaimRejected("lease_seconds 必须是有限正数")
    lease = float(lease_seconds)
    if not math.isfinite(lease) or lease <= 0 or lease > MAX_LEASE_SECONDS:
        raise ClaimRejected(f"lease_seconds 必须在 0 到 {MAX_LEASE_SECONDS} 秒之间")
    existing = state.claims.get(worker_id)
    if existing is not None:
        raise ClaimRejected(f"重复 worker 不允许重用: {worker_id}")
    if process_pid is not None and (
        not isinstance(process_pid, int) or isinstance(process_pid, bool) or process_pid <= 0
    ):
        raise ClaimRejected("process_pid 必须是正整数")
    _capacity_available(supervisor, state, now, worker_id)
    if state.budget_tool_calls >= state.budget.max_tool_calls:
        supervisor._set_budget_exhausted(
            state, now, ["max_tool_calls"], event_type="claim_rejected",
        )
        raise BudgetExhausted("max_tool_calls 已耗尽")
    if state.budget_cost_units >= state.budget.max_cost_units:
        supervisor._set_budget_exhausted(
            state, now, ["max_cost_units"], event_type="claim_rejected",
        )
        raise BudgetExhausted("max_cost_units 已耗尽")
    if state.round_no > state.budget.max_rounds:
        supervisor._set_budget_exhausted(
            state, now, ["max_rounds"], event_type="claim_rejected",
        )
        raise BudgetExhausted("max_rounds 已耗尽")

    binding = graph.agent_bindings.get(task_id)
    if task.state in {"reserved", "running"}:
        previous = state.claims.get(binding.worker_id) if binding is not None else None
        if previous is None or previous.active or previous.state == "submitted":
            raise ClaimRejected(f"graph task 仍有未结算执行: {task_id}")
        task.state = "queued"
        task.error = previous.preempted_reason or "previous_graph_attempt_unsettled"
    elif task.state not in {"queued", "artifact_ready"}:
        raise ClaimRejected(f"graph task 不可领取: {task_id} ({task.state})")
    if task.attempt >= task.max_attempts:
        raise ClaimRejected(f"graph task 已达到最大重试次数: {task_id}")
    now = observe_clock(state, now)
    if task.deadline is not None and now >= task.deadline and task.state == "queued":
        # A stale deadline is not a new lease. Clear it only as part of the
        # retry claim that immediately installs the next authoritative deadline.
        task.deadline = None

    conflict = sorted(set(task.resource_keys) & supervisor._active_resource_keys(state))
    if requested_resources and tuple(requested_resources) != tuple(task.resource_keys):
        raise ClaimRejected("graph_stage claim 的 resource_keys 必须与 graph task 一致")
    if conflict:
        supervisor._append_event(
            state, "claim_rejected", now, worker_id=worker_id,
            assignment_kind="graph_stage", graph_task_id=task_id,
            reason="resource_conflict", resource_keys=conflict,
        )
        raise ClaimRejected("资源组正在被其他 worker 使用: " + ", ".join(conflict))

    attempt = task.attempt + 1
    lease_id = uuid.uuid4().hex
    _, artifact_root = worker_artifact_root(
        supervisor.workspace_root, supervisor.state_path, worker_id, lease_id,
    )
    mono_now = time.monotonic()
    claim = WorkerClaim(
        worker_id=worker_id,
        lease_id=lease_id,
        round_no=state.round_no,
        claimed_at=now,
        lease_expires_at=now + lease,
        unit_ids=(),
        resource_keys=tuple(task.resource_keys),
        attempt=attempt,
        scheduler_epoch=state.scheduler_epoch,
        execution_id=f"exec-{lease_id}",
        artifact_root=artifact_root,
        process_pid=process_pid,
        process_start_token=process_start_token(process_pid),
        heartbeat_nonce=(uuid.uuid4().hex if process_pid is not None else None),
        claimed_mono=mono_now,
        lease_expires_mono=mono_now + lease,
        last_heartbeat_mono=mono_now,
        total_lease_deadline_mono=mono_now + lease * (DEFAULT_MAX_RENEWALS + 1),
        total_lease_deadline=now + lease * (DEFAULT_MAX_RENEWALS + 1),
        assignment_kind="graph_stage",
        graph_task_id=task_id,
    )
    task.state = "reserved"
    task.attempt = attempt
    task.claim_worker_id = worker_id
    task.deadline = claim.lease_expires_at
    task.error = None
    graph.agent_bindings[task_id] = AgentBinding(
        task_id=task_id,
        worker_id=worker_id,
        lease_id=claim.lease_id,
        execution_id=claim.execution_id,
    )
    state.claims[worker_id] = claim
    state.scheduling_metrics.total_claims += 1
    queue_latency = max(0.0, now - state.round_started_at)
    state.scheduling_metrics.total_queue_seconds += queue_latency
    state.scheduling_metrics.max_queue_seconds = max(
        state.scheduling_metrics.max_queue_seconds, queue_latency,
    )
    if state.status in {Status.CHECKPOINT, Status.NEXT_ROUND}:
        state.status = Status.RUNNING
    supervisor._reconcile_parallel(state, now)
    supervisor._append_event(
        state, "worker_claimed", now,
        worker_id=claim.worker_id, lease_id=claim.lease_id,
        round_no=claim.round_no, lease_expires_at=claim.lease_expires_at,
        unit_ids=[], resource_keys=list(claim.resource_keys), attempt=attempt,
        scheduler_epoch=claim.scheduler_epoch, execution_id=claim.execution_id,
        artifact_root=claim.artifact_root, assignment_kind="graph_stage",
        graph_task_id=task_id,
    )
    return claim


def graph_worker_started(state: Any, claim: WorkerClaim, now: float, append_event) -> None:
    """Advance the graph task only after the authoritative worker-start passes."""
    if claim.assignment_kind != "graph_stage" or not claim.graph_task_id:
        return
    graph = _graph(state)
    task = graph.tasks.get(claim.graph_task_id)
    binding = graph.agent_bindings.get(claim.graph_task_id)
    if task is None or binding is None or (
        binding.worker_id != claim.worker_id
        or binding.lease_id != claim.lease_id
        or binding.execution_id != claim.execution_id
    ):
        raise ClaimRejected("graph task binding 与 worker lease/execution 不一致")
    if task.state == "running":
        return
    if task.state != "reserved":
        raise ClaimRejected(f"graph task 不可启动: {claim.graph_task_id} ({task.state})")
    task.state = "running"
    binding.wait_state = "running"
    append_event(
        state, "graph_task_started", now,
        graph_id=graph.graph_id, graph_task_id=task.task_id,
        worker_id=claim.worker_id, lease_id=claim.lease_id,
        execution_id=claim.execution_id,
    )


def graph_worker_failed(state: Any, claim: WorkerClaim, now: float, reason: str) -> None:
    """Return an unsettled graph task to the retry queue after a real failure."""
    if claim.assignment_kind != "graph_stage" or not claim.graph_task_id:
        return
    graph = _graph(state)
    task = graph.tasks.get(claim.graph_task_id)
    binding = graph.agent_bindings.get(claim.graph_task_id)
    if task is None or binding is None or binding.lease_id != claim.lease_id:
        return
    task.state = "queued"
    task.deadline = None
    task.error = reason.strip()[:2000]
    binding.wait_state = "failed"


def _normalize_observation(
    raw: Mapping[str, object], *, observation_id: str, graph_id: str,
    task_id: str, stage: str, worker_id: str, lease_id: str, execution_id: str,
    artifact_digest: str, now: float,
) -> dict[str, object]:
    result = copy.deepcopy(dict(raw))
    identifier = result.get("observation_id", result.get("id", observation_id))
    if not isinstance(identifier, str) or not identifier.strip():
        raise InvalidCheckpoint("observation_id 不能为空")
    fixed = {
        "observation_id": identifier.strip(),
        "graph_id": graph_id,
        "task_id": task_id,
        "stage": stage,
        "worker_id": worker_id,
        "lease_id": lease_id,
        "execution_id": execution_id,
        "artifact_digest": artifact_digest,
        "observed_at": now,
    }
    for key, value in fixed.items():
        if key in result and result[key] != value and key != "observed_at":
            raise InvalidCheckpoint(f"observation.{key} 与当前 graph checkpoint 不一致")
    result.update(fixed)
    try:
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidCheckpoint(f"observation 不是有限 JSON: {exc}") from exc
    return result


def _normalize_bugs(bugs: Sequence[BugRecord | Mapping[str, object]]) -> list[BugRecord]:
    if isinstance(bugs, (str, bytes)) or not isinstance(bugs, Sequence):
        raise InvalidCheckpoint("bugs 必须是对象数组")
    result: list[BugRecord] = []
    seen: set[str] = set()
    for raw in bugs:
        try:
            record = BugRecord.from_dict(raw.to_dict() if isinstance(raw, BugRecord) else raw)
        except (PersistenceError, AttributeError, TypeError) as exc:
            raise InvalidCheckpoint(f"bug record 无效: {exc}") from exc
        if record.bug_id in seen:
            raise InvalidCheckpoint(f"checkpoint 包含重复 bug_id: {record.bug_id}")
        seen.add(record.bug_id)
        result.append(record)
    return result


def _append_unique_json(target: list[dict[str, object]], values: Sequence[Mapping[str, object]]) -> None:
    known = {_canonical_digest(item) for item in target}
    for value in values:
        normalized = copy.deepcopy(dict(value))
        digest = _canonical_digest(normalized)
        if digest not in known:
            target.append(normalized)
            known.add(digest)


def _merge_bug(graph: GraphRun, incoming: BugRecord, *, now: float, checkpoint_id: str) -> None:
    existing = graph.bug_index.get(incoming.bug_id)
    if existing is None:
        created = BugRecord.from_dict(incoming.to_dict())
        _append_unique_json(created.history, [{
            "event": "created", "at": now, "checkpoint_id": checkpoint_id,
            "assessment": created.assessment,
            "evidence_level": created.evidence_level,
            "remediation": created.remediation,
        }])
        graph.bug_index[created.bug_id] = created
        return
    if existing.candidate_digest != incoming.candidate_digest:
        raise InvalidCheckpoint(
            f"bug_id {incoming.bug_id} 已绑定不同 candidate_digest"
        )
    next_assessment = (
        incoming.assessment
        if incoming.assessment != "candidate" or existing.assessment == "candidate"
        else existing.assessment
    )
    evidence_rank = {"source_review": 0, "reproduced": 1, "regression_verified": 2}
    next_evidence = (
        incoming.evidence_level
        if evidence_rank[incoming.evidence_level] >= evidence_rank[existing.evidence_level]
        else existing.evidence_level
    )
    next_remediation = (
        incoming.remediation
        if incoming.remediation != "not_requested" or existing.remediation == "not_requested"
        else existing.remediation
    )
    changed = (
        existing.assessment != next_assessment
        or existing.evidence_level != next_evidence
        or existing.remediation != next_remediation
    )
    if changed:
        _append_unique_json(existing.history, [{
            "event": "updated", "at": now, "checkpoint_id": checkpoint_id,
            "previous_assessment": existing.assessment,
            "previous_evidence_level": existing.evidence_level,
            "previous_remediation": existing.remediation,
            "assessment": next_assessment,
            "evidence_level": next_evidence,
            "remediation": next_remediation,
        }])
    existing.repro_digests = tuple(dict.fromkeys((*existing.repro_digests, *incoming.repro_digests)))
    existing.review_digests = tuple(dict.fromkeys((*existing.review_digests, *incoming.review_digests)))
    existing.observation_ids = tuple(dict.fromkeys((*existing.observation_ids, *incoming.observation_ids)))
    existing.assessment = next_assessment
    existing.evidence_level = next_evidence
    existing.remediation = next_remediation
    _append_unique_json(existing.history, incoming.history)


def graph_checkpoint_sync(
    supervisor: Any,
    state: Any,
    now: float,
    *,
    worker_id: str,
    graph_task_id: str,
    lease_id: str,
    execution_id: str,
    artifact_digest: str,
    observation: Mapping[str, object],
    bugs: Sequence[BugRecord | Mapping[str, object]] = (),
    completed: bool = True,
    review_coverage: Mapping[str, str] | None = None,
    checkpoint_id: str | None = None,
    expected_revision: int | None = None,
) -> Any:
    """Commit an artifact, observation, task state, and bug ledger atomically."""
    if not isinstance(graph_task_id, str) or not graph_task_id.strip():
        raise InvalidCheckpoint("graph_task_id 不能为空")
    graph_task_id = graph_task_id.strip()
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise InvalidCheckpoint("worker_id 不能为空")
    if not isinstance(lease_id, str) or not lease_id.strip():
        raise InvalidCheckpoint("lease_id 不能为空")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise InvalidCheckpoint("execution_id 不能为空")
    lease_id = lease_id.strip()
    execution_id = execution_id.strip()
    if not isinstance(artifact_digest, str) or len(artifact_digest) != 64:
        raise InvalidCheckpoint("artifact_digest 必须是 SHA-256")
    try:
        int(artifact_digest, 16)
    except ValueError as exc:
        raise InvalidCheckpoint("artifact_digest 必须是 SHA-256") from exc
    artifact_digest = artifact_digest.lower()
    if not isinstance(completed, bool):
        raise InvalidCheckpoint("completed 必须是布尔值")
    if not isinstance(observation, Mapping):
        raise InvalidCheckpoint("observation 必须是对象")
    if review_coverage is not None and not isinstance(review_coverage, Mapping):
        raise InvalidCheckpoint("review_coverage 必须是对象")
    records = _normalize_bugs(bugs)
    for record in records:
        _canonical_digest(record.to_dict())
    coverage_updates = {
        str(key).strip(): str(value).strip()
        for key, value in (review_coverage or {}).items()
    }
    if any(not key or not value for key, value in coverage_updates.items()):
        raise InvalidCheckpoint("review_coverage key/value 不能为空")
    observation_input = copy.deepcopy(dict(observation))
    payload = {
        "graph_task_id": graph_task_id,
        "artifact_digest": artifact_digest,
        "observation": observation_input,
        "bugs": [record.to_dict() for record in records],
        "completed": completed,
        "review_coverage": coverage_updates,
    }
    payload_digest = _canonical_digest(payload)
    checkpoint_key = checkpoint_id.strip() if isinstance(checkpoint_id, str) and checkpoint_id.strip() else f"graph-{payload_digest}"
    if len(checkpoint_key) > 128:
        raise InvalidCheckpoint("checkpoint_id 过长")

    supervisor._check_revision(state, expected_revision)
    supervisor._refresh(state, now)
    supervisor._assert_target_fingerprint(state, now)
    graph = _graph(state)
    claim = state.claims.get(worker_id)
    if (
        claim is None
        or claim.assignment_kind != "graph_stage"
        or claim.graph_task_id != graph_task_id
        or claim.lease_id != lease_id
        or claim.execution_id != execution_id
    ):
        raise ClaimRejected("graph checkpoint 的 worker/lease/execution 身份不匹配")
    task = graph.tasks.get(graph_task_id)
    binding = graph.agent_bindings.get(graph_task_id)
    if task is None or binding is None or (
        binding.worker_id != worker_id
        or binding.lease_id != lease_id
        or binding.execution_id != execution_id
    ):
        raise ClaimRejected("graph checkpoint 与当前 AgentBinding 不匹配")
    for record in records:
        current = graph.bug_index.get(record.bug_id)
        if current is not None and current.candidate_digest != record.candidate_digest:
            raise InvalidCheckpoint(
                f"bug_id {record.bug_id} 已绑定不同 candidate_digest"
            )
    for recorded in state.claims.values():
        if recorded.last_checkpoint_id != checkpoint_key:
            continue
        if recorded.worker_id != worker_id:
            raise ClaimRejected(f"checkpoint_id 已被其他 worker 使用: {checkpoint_key}")
        if recorded.last_checkpoint_digest != payload_digest:
            raise InvalidCheckpoint("同一 checkpoint_id 的 payload 不一致，拒绝重放")
        if recorded.state == "submitted":
            return state
    if not claim.active:
        raise ClaimRejected(f"graph claim 不可提交: {worker_id} ({claim.state})")
    claim = supervisor._active_claim(state, worker_id, now, require_running=True)
    if task.state != "running":
        raise ClaimRejected(f"graph task 尚未 running: {graph_task_id} ({task.state})")
    raw_observation_id = observation_input.get(
        "observation_id", observation_input.get("id", checkpoint_key),
    )
    if not isinstance(raw_observation_id, str) or not raw_observation_id.strip():
        raise InvalidCheckpoint("observation_id 不能为空")
    observation_id = raw_observation_id.strip()
    normalized_observation = _normalize_observation(
        observation_input, observation_id=observation_id,
        graph_id=graph.graph_id, task_id=graph_task_id, stage=task.stage,
        worker_id=worker_id, lease_id=lease_id, execution_id=execution_id,
        artifact_digest=artifact_digest, now=now,
    )
    if any(record.remediation == "fix_verified" for record in records):
        if (
            task.stage != "check"
            or not completed
            or normalized_observation.get("status") != "passed"
            or normalized_observation.get("exit_code") != 0
        ):
            raise InvalidCheckpoint(
                "fix_verified 必须由已完成且 status=passed/exit_code=0 的 check task 提交"
            )
        invalid_fix_records = sorted(
            record.bug_id for record in records
            if record.remediation == "fix_verified"
            and (
                record.bug_id != task.subject_id
                or record.assessment != "confirmed"
                or record.evidence_level != "regression_verified"
            )
        )
        if invalid_fix_records:
            raise InvalidCheckpoint(
                "fix_verified 必须绑定同 subject 的 confirmed regression_verified bug: "
                + ", ".join(invalid_fix_records)
            )
    for existing_observation in graph.observations:
        if existing_observation.get("observation_id") == observation_id:
            if _canonical_digest(existing_observation) != _canonical_digest(normalized_observation):
                raise InvalidCheckpoint(f"observation_id 已绑定不同内容: {observation_id}")

    task.result_digest = artifact_digest
    task.state = "completed" if completed else "submitted"
    task.deadline = None
    task.error = None
    binding.wait_state = "completed"
    claim.state = "submitted"
    claim.submitted_at = now
    claim.execution_state = "completed"
    claim.execution_finished_at = now
    claim.last_checkpoint_id = checkpoint_key
    claim.last_checkpoint_digest = payload_digest
    if not any(item.get("observation_id") == observation_id for item in graph.observations):
        graph.observations.append(normalized_observation)
    for record in records:
        if observation_id not in record.observation_ids:
            record.observation_ids = tuple((*record.observation_ids, observation_id))
        if task.stage == "repro" and artifact_digest not in record.repro_digests:
            record.repro_digests = tuple((*record.repro_digests, artifact_digest))
        if task.stage == "review" and artifact_digest not in record.review_digests:
            record.review_digests = tuple((*record.review_digests, artifact_digest))
        _merge_bug(graph, record, now=now, checkpoint_id=checkpoint_key)
    graph.review_coverage.update(coverage_updates)
    if task.stage == "review":
        graph.review_coverage[task.subject_id] = "completed" if completed else "submitted"
    if task.stage == "report" and completed:
        graph.report_revision += 1

    state.scheduling_metrics.total_completed += 1
    if claim.execution_started_at is not None:
        duration = max(0.0, now - claim.execution_started_at)
        state.scheduling_metrics.total_execution_seconds += duration
        state.scheduling_metrics.max_execution_seconds = max(
            state.scheduling_metrics.max_execution_seconds, duration,
        )
    supervisor._reconcile_parallel(state, now)
    supervisor._append_event(
        state, "graph_checkpoint_submitted", now,
        graph_id=graph.graph_id, graph_task_id=task.task_id,
        worker_id=worker_id, lease_id=lease_id, execution_id=execution_id,
        checkpoint_id=checkpoint_key, payload_digest=payload_digest,
        artifact_digest=artifact_digest, observation_id=observation_id,
        task_state=task.state, bug_ids=sorted(record.bug_id for record in records),
        report_revision=graph.report_revision,
    )
    if state.status not in {
        Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
    }:
        state.status = Status.BUDGET_EXHAUSTED if state.budget_exhausted else Status.CHECKPOINT
    return state


__all__ = [
    "graph_start_sync",
    "graph_claim_sync",
    "graph_worker_started",
    "graph_worker_failed",
    "graph_checkpoint_sync",
]
