#!/usr/bin/env python3
"""core/supervisor/graph_models.py - 任务图谱、Agent 绑定与 Bug 索引数据模型 (DOC-070)."""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import Mapping

from core.supervisor_types.exceptions import PersistenceError

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

GRAPH_GOALS = {"find_bugs", "repair_preview"}
GRAPH_STAGES = {"recon", "repro", "review", "patch", "check", "report"}
GRAPH_TASK_STATES = {
    "queued", "reserved", "running", "artifact_ready", "submitted", "completed",
}
GRAPH_ASSESSMENTS = {"candidate", "confirmed", "rejected", "needs_context", "static_supported"}
GRAPH_EVIDENCE_LEVELS = {"source_review", "reproduced", "regression_verified"}
GRAPH_REMEDIATIONS = {
    "not_requested", "proposed", "fix_verified", "fix_failed", "regression_incomplete",
}


def _graph_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PersistenceError(f"{name} 必须是数组")
    if any(not isinstance(item, str) for item in value):
        raise PersistenceError(f"{name} 必须只包含字符串")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result) or len(set(result)) != len(result):
        raise PersistenceError(f"{name} 不能为空或重复")
    return result


def _graph_digest(value: object, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PersistenceError(f"{name} 必须是 SHA-256")
    return value.lower()


@dataclass
class GraphTask:
    """One durable unit of graph-stage work."""

    task_id: str
    stage: str
    subject_id: str
    source_unit_ids: tuple[str, ...] = ()
    parent_artifact_digests: tuple[str, ...] = ()
    resource_keys: tuple[str, ...] = ()
    state: str = "queued"
    attempt: int = 0
    max_attempts: int = 3
    claim_worker_id: str | None = None
    deadline: float | None = None
    result_digest: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "subject_id": self.subject_id,
            "source_unit_ids": list(self.source_unit_ids),
            "parent_artifact_digests": list(self.parent_artifact_digests),
            "resource_keys": list(self.resource_keys),
            "state": self.state,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "claim_worker_id": self.claim_worker_id,
            "deadline": self.deadline,
            "result_digest": self.result_digest,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "GraphTask":
        try:
            task = cls(
                task_id=str(value["task_id"]).strip(),
                stage=str(value["stage"]),
                subject_id=str(value["subject_id"]).strip(),
                source_unit_ids=_graph_strings(value.get("source_unit_ids", []), "source_unit_ids"),
                parent_artifact_digests=_graph_strings(
                    value.get("parent_artifact_digests", []), "parent_artifact_digests",
                ),
                resource_keys=_graph_strings(value.get("resource_keys", []), "resource_keys"),
                state=str(value.get("state", "queued")),
                attempt=int(value.get("attempt", 0)),
                max_attempts=int(value.get("max_attempts", 3)),
                claim_worker_id=(
                    None if value.get("claim_worker_id") is None
                    else str(value["claim_worker_id"]).strip()
                ),
                deadline=(None if value.get("deadline") is None else float(value["deadline"])),
                result_digest=_graph_digest(value.get("result_digest"), "result_digest", optional=True),
                error=(None if value.get("error") is None else str(value["error"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(f"graph task 无效: {exc}") from exc
        if not task.task_id or not task.subject_id:
            raise PersistenceError("graph task id/subject_id 不能为空")
        if task.stage not in GRAPH_STAGES:
            raise PersistenceError(f"未知 graph task stage: {task.stage}")
        if task.state not in GRAPH_TASK_STATES:
            raise PersistenceError(f"未知 graph task 状态: {task.state}")
        if task.attempt < 0 or task.max_attempts < 1 or task.attempt > task.max_attempts:
            raise PersistenceError("graph task attempt/max_attempts 无效")
        if task.deadline is not None and (
            not math.isfinite(task.deadline) or task.deadline <= 0
        ):
            raise PersistenceError("graph task deadline 必须是有限正数或 null")
        for digest in task.parent_artifact_digests:
            _graph_digest(digest, "parent_artifact_digests")
        if task.result_digest is not None:
            _graph_digest(task.result_digest, "result_digest")
        if task.claim_worker_id == "":
            raise PersistenceError("graph task claim_worker_id 不能为空或 null")
        return task


@dataclass
class AgentBinding:
    """The current lease/execution identity observed for a graph task."""

    task_id: str
    worker_id: str
    lease_id: str
    execution_id: str
    native_agent_id: str | None = None
    spawn_observed_at: float | None = None
    wait_state: str = "pending"
    cancel_state: str = "not_requested"

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "lease_id": self.lease_id,
            "execution_id": self.execution_id,
            "native_agent_id": self.native_agent_id,
            "spawn_observed_at": self.spawn_observed_at,
            "wait_state": self.wait_state,
            "cancel_state": self.cancel_state,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "AgentBinding":
        try:
            binding = cls(
                task_id=str(value["task_id"]).strip(),
                worker_id=str(value["worker_id"]).strip(),
                lease_id=str(value["lease_id"]).strip(),
                execution_id=str(value["execution_id"]).strip(),
                native_agent_id=(
                    None if value.get("native_agent_id") is None
                    else str(value["native_agent_id"]).strip()
                ),
                spawn_observed_at=(
                    None if value.get("spawn_observed_at") is None
                    else float(value["spawn_observed_at"])
                ),
                wait_state=str(value.get("wait_state", "pending")),
                cancel_state=str(value.get("cancel_state", "not_requested")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(f"agent binding 无效: {exc}") from exc
        if not all((binding.task_id, binding.worker_id, binding.lease_id, binding.execution_id)):
            raise PersistenceError("agent binding 身份字段不能为空")
        if binding.native_agent_id == "":
            raise PersistenceError("native_agent_id 不能为空或 null")
        if binding.spawn_observed_at is not None and (
            not math.isfinite(binding.spawn_observed_at) or binding.spawn_observed_at <= 0
        ):
            raise PersistenceError("spawn_observed_at 必须是有限正数或 null")
        if not binding.wait_state or not binding.cancel_state:
            raise PersistenceError("agent binding wait_state/cancel_state 不能为空")
        return binding


@dataclass
class BugRecord:
    """Evidence and adjudication ledger entry for one candidate bug."""

    bug_id: str
    candidate_digest: str
    repro_digests: tuple[str, ...] = ()
    review_digests: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    assessment: str = "candidate"
    evidence_level: str = "source_review"
    remediation: str = "not_requested"
    history: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "bug_id": self.bug_id,
            "candidate_digest": self.candidate_digest,
            "repro_digests": list(self.repro_digests),
            "review_digests": list(self.review_digests),
            "observation_ids": list(self.observation_ids),
            "assessment": self.assessment,
            "evidence_level": self.evidence_level,
            "remediation": self.remediation,
            "history": copy.deepcopy(self.history),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BugRecord":
        try:
            raw_history = value.get("history", [])
            if not isinstance(raw_history, list) or any(
                not isinstance(item, Mapping) for item in raw_history
            ):
                raise TypeError("history 必须是对象数组")
            bug = cls(
                bug_id=str(value["bug_id"]).strip(),
                candidate_digest=str(_graph_digest(value["candidate_digest"], "candidate_digest")),
                repro_digests=_graph_strings(value.get("repro_digests", []), "repro_digests"),
                review_digests=_graph_strings(value.get("review_digests", []), "review_digests"),
                observation_ids=_graph_strings(value.get("observation_ids", []), "observation_ids"),
                assessment=str(value.get("assessment", "candidate")),
                evidence_level=str(value.get("evidence_level", "source_review")),
                remediation=str(value.get("remediation", "not_requested")),
                history=[copy.deepcopy(dict(item)) for item in raw_history],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(f"bug record 无效: {exc}") from exc
        if not bug.bug_id:
            raise PersistenceError("bug_id 不能为空")
        if bug.assessment not in GRAPH_ASSESSMENTS:
            raise PersistenceError(f"未知 bug assessment: {bug.assessment}")
        if bug.evidence_level not in GRAPH_EVIDENCE_LEVELS:
            raise PersistenceError(f"未知 bug evidence_level: {bug.evidence_level}")
        if bug.remediation not in GRAPH_REMEDIATIONS:
            raise PersistenceError(f"未知 bug remediation: {bug.remediation}")
        for name, values in (
            ("repro_digests", bug.repro_digests),
            ("review_digests", bug.review_digests),
        ):
            for digest in values:
                _graph_digest(digest, name)
        return bug


@dataclass
class GraphRun:
    """Versioned graph run state persisted inside the Supervisor snapshot."""

    graph_id: str
    goal: str
    origin_root: str
    snapshot_digest: str
    schema_version: int = 1
    tasks: dict[str, GraphTask] = field(default_factory=dict)
    agent_bindings: dict[str, AgentBinding] = field(default_factory=dict)
    observations: list[dict[str, object]] = field(default_factory=list)
    bug_index: dict[str, BugRecord] = field(default_factory=dict)
    review_coverage: dict[str, str] = field(default_factory=dict)
    report_revision: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "goal": self.goal,
            "origin_root": self.origin_root,
            "snapshot_digest": self.snapshot_digest,
            "tasks": {
                key: value.to_dict() for key, value in sorted(self.tasks.items())
            },
            "agent_bindings": {
                key: value.to_dict() for key, value in sorted(self.agent_bindings.items())
            },
            "observations": copy.deepcopy(self.observations),
            "bug_index": {
                key: value.to_dict() for key, value in sorted(self.bug_index.items())
            },
            "review_coverage": dict(sorted(self.review_coverage.items())),
            "report_revision": self.report_revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "GraphRun":
        try:
            schema_version = value.get("schema_version", 1)
            if isinstance(schema_version, bool) or schema_version != 1:
                raise ValueError("不支持的 GraphRun schema_version")
            raw_tasks = value.get("tasks", {})
            raw_bindings = value.get("agent_bindings", {})
            raw_observations = value.get("observations", [])
            raw_bugs = value.get("bug_index", {})
            raw_review = value.get("review_coverage", {})
            if not isinstance(raw_tasks, Mapping) or not isinstance(raw_bindings, Mapping):
                raise TypeError("tasks/agent_bindings 必须是对象")
            if any(not isinstance(item, Mapping) for item in raw_tasks.values()):
                raise TypeError("tasks 的每项必须是对象")
            if any(not isinstance(item, Mapping) for item in raw_bindings.values()):
                raise TypeError("agent_bindings 的每项必须是对象")
            if not isinstance(raw_observations, list) or any(
                not isinstance(item, Mapping) for item in raw_observations
            ):
                raise TypeError("observations 必须是对象数组")
            if not isinstance(raw_bugs, Mapping) or not isinstance(raw_review, Mapping):
                raise TypeError("bug_index/review_coverage 必须是对象")
            if any(not isinstance(item, Mapping) for item in raw_bugs.values()):
                raise TypeError("bug_index 的每项必须是对象")
            tasks = {str(key): GraphTask.from_dict(item) for key, item in raw_tasks.items()}
            bindings = {
                str(key): AgentBinding.from_dict(item)
                for key, item in raw_bindings.items()
            }
            bugs = {str(key): BugRecord.from_dict(item) for key, item in raw_bugs.items()}
            graph = cls(
                graph_id=str(value["graph_id"]).strip(),
                goal=str(value["goal"]),
                origin_root=str(value["origin_root"]).strip(),
                snapshot_digest=str(_graph_digest(value["snapshot_digest"], "snapshot_digest")),
                schema_version=1,
                tasks=tasks,
                agent_bindings=bindings,
                observations=[copy.deepcopy(dict(item)) for item in raw_observations],
                bug_index=bugs,
                review_coverage={str(key): str(item) for key, item in raw_review.items()},
                report_revision=int(value.get("report_revision", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(f"agent graph 无效: {exc}") from exc
        if not graph.graph_id or not graph.origin_root:
            raise PersistenceError("graph_id/origin_root 不能为空")
        if graph.goal not in GRAPH_GOALS:
            raise PersistenceError(f"未知 graph goal: {graph.goal}")
        if graph.report_revision < 0:
            raise PersistenceError("report_revision 不能为负")
        for key, task in tasks.items():
            if key != task.task_id:
                raise PersistenceError(f"tasks key '{key}' 与 task_id 不一致")
            binding = bindings.get(key)
            if task.claim_worker_id is not None and (
                binding is None or binding.worker_id != task.claim_worker_id
            ):
                raise PersistenceError(f"graph task {key} claim_worker_id 与 binding 不一致")
        for key, binding in bindings.items():
            if key != binding.task_id or key not in tasks:
                raise PersistenceError(f"agent_bindings key '{key}' 无效")
        for key, bug in bugs.items():
            if key != bug.bug_id:
                raise PersistenceError(f"bug_index key '{key}' 与 bug_id 不一致")
        if any(not key or not value for key, value in graph.review_coverage.items()):
            raise PersistenceError("review_coverage 的 key/value 不能为空")
        return graph
