"""Build truthful audit report data from a persisted Supervisor bug index."""

from __future__ import annotations

import importlib
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


class ReportDataError(ValueError):
    """The graph ledger or its referenced evidence is incomplete or invalid."""


def _plain(value: object) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        value = asdict(value)
    return value


def _mapping(value: object, name: str) -> dict[str, Any]:
    raw = _plain(value)
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise ReportDataError(f"{name} must be an object with string keys")
    return dict(raw)


def _relative_source_path(value: object, project_root: Path) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ReportDataError("source reference path must be a project-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReportDataError(f"source reference escapes the project: {value!r}")
    normalized = path.as_posix()
    try:
        (project_root / Path(*path.parts)).resolve(strict=False).relative_to(project_root)
    except ValueError as exc:
        raise ReportDataError(f"source reference escapes the project: {value!r}") from exc
    return normalized


def _artifact(
    digest: str,
    loader: Callable[[str], object],
    artifact_module: Any,
) -> dict[str, Any]:
    if not isinstance(digest, str) or len(digest) != 64:
        raise ReportDataError("artifact reference must contain a SHA-256 digest")
    try:
        raw_value = loader(digest)
        raw = artifact_module.artifact_to_dict(raw_value)
        artifact_module.validate_artifact(raw)
    except Exception as exc:
        raise ReportDataError(f"cannot validate referenced artifact {digest}: {exc}") from exc
    if raw.get("artifact_digest") != digest:
        raise ReportDataError(f"artifact digest does not match bug index reference: {digest}")
    return raw


def _source_file_facts(
    path: str,
    project_root: Path,
    cache: dict[str, tuple[str, int]],
) -> tuple[str, int]:
    """Return the current project file digest and physical line count safely."""
    if path in cache:
        return cache[path]
    candidate = project_root
    for part in PurePosixPath(path).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ReportDataError(f"source reference traverses a symbolic link: {path}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root)
        if not resolved.is_file():
            raise ReportDataError(f"source reference is not a regular file: {path}")
        digest = hashlib.sha256()
        line_breaks = 0
        pending_cr = False
        last_byte: int | None = None
        size = 0
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                for byte in chunk:
                    if pending_cr:
                        line_breaks += 1
                        pending_cr = False
                        if byte == 10:
                            last_byte = byte
                            continue
                    if byte == 13:
                        pending_cr = True
                    elif byte == 10:
                        line_breaks += 1
                    last_byte = byte
        if pending_cr:
            line_breaks += 1
        line_count = line_breaks + int(size > 0 and last_byte not in (10, 13))
    except ReportDataError:
        raise
    except (OSError, ValueError) as exc:
        raise ReportDataError(f"source reference cannot be read safely: {path}: {exc}") from exc
    facts = (digest.hexdigest(), line_count)
    cache[path] = facts
    return facts


def _source_refs(
    values: object,
    project_root: Path,
    label: str,
    source_cache: dict[str, tuple[str, int]],
) -> list[dict[str, object]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ReportDataError(f"{label} must be an array")
    result: list[dict[str, object]] = []
    for raw_value in values:
        ref = _mapping(raw_value, label)
        path = _relative_source_path(ref.get("path"), project_root)
        start = ref.get("line_start")
        end = ref.get("line_end")
        expected_digest = ref.get("file_digest")
        if (
            isinstance(start, bool) or not isinstance(start, int) or start < 1
            or isinstance(end, bool) or not isinstance(end, int) or end < start
        ):
            raise ReportDataError(f"{label} line range is invalid for {path}")
        if (
            not isinstance(expected_digest, str) or len(expected_digest) != 64
            or any(char not in "0123456789abcdef" for char in expected_digest)
        ):
            raise ReportDataError(f"{label} file_digest is invalid for {path}")
        actual_digest, line_count = _source_file_facts(path, project_root, source_cache)
        if actual_digest != expected_digest:
            raise ReportDataError(f"{label} file_digest does not match project source: {path}")
        if end > line_count:
            raise ReportDataError(f"{label} line range exceeds project source: {path}")
        result.append({
            "path": path,
            "line_start": start,
            "line_end": end,
            "symbol": ref.get("symbol"),
            "file_digest": expected_digest,
        })
    return result


def _observation_summary(value: object) -> dict[str, object]:
    observation = _mapping(value, "observation")
    observation_id = observation.get("observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise ReportDataError("observation_id must be a non-empty string")
    allowed = (
        "observation_id", "graph_id", "task_id", "worker_id", "lease_id",
        "execution_id", "artifact_digest", "observed_at", "status", "exit_code",
        "command", "stdout", "stderr", "actual_behavior", "observed_behavior",
        "target_call_evidence", "assertion_result", "tests_run", "tests_passed",
        "tests_failed", "tests_skipped", "timed_out", "cancelled", "outcome_unknown",
    )
    result = {key: observation[key] for key in allowed if key in observation}
    try:
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReportDataError(f"observation is not finite JSON: {observation_id}") from exc
    return result


def build_report_data(
    graph: object,
    *,
    run_id: str,
    artifact_loader: Callable[[str], object],
    project_root: str | Path,
) -> dict[str, object]:
    """Serialize a Supervisor GraphRun without inventing evidence or metrics.

    ``graph`` must expose the persisted ``bug_index`` and ``observations``. The
    loader resolves content-addressed artifacts referenced by BugRecord and
    observations; every envelope digest and schema is checked before use.
    """
    graph_data = _mapping(graph, "graph")
    graph_id = graph_data.get("graph_id")
    goal = graph_data.get("goal")
    bug_index = graph_data.get("bug_index")
    observations = graph_data.get("observations")
    if not isinstance(graph_id, str) or not graph_id:
        raise ReportDataError("graph_id is missing")
    if not isinstance(goal, str) or not goal:
        raise ReportDataError("graph goal is missing")
    if isinstance(bug_index, Mapping):
        normalized_bug_index = dict(bug_index)
    elif isinstance(bug_index, Sequence) and not isinstance(
        bug_index, (str, bytes, bytearray),
    ):
        normalized_bug_index: dict[str, object] = {}
        for position, raw_bug in enumerate(bug_index):
            record = _mapping(raw_bug, f"bug_index[{position}]")
            bug_id = record.get("bug_id")
            if not isinstance(bug_id, str) or not bug_id:
                raise ReportDataError(f"bug_index[{position}] has no bug_id")
            if bug_id in normalized_bug_index:
                raise ReportDataError(f"duplicate bug_index entry: {bug_id}")
            normalized_bug_index[bug_id] = record
    else:
        raise ReportDataError("authoritative bug_index is missing")
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes, bytearray)):
        raise ReportDataError("graph observations must be an array")
    if not isinstance(run_id, str) or not run_id:
        raise ReportDataError("run_id is missing")

    root = Path(project_root).expanduser().resolve(strict=True)
    try:
        from core.agent import agent_artifacts as artifact_module
    except ImportError as exc:
        raise ReportDataError(f"artifact schema module unavailable: {exc}") from exc

    observation_index: dict[str, dict[str, object]] = {}
    for raw_observation in observations:
        summary = _observation_summary(raw_observation)
        observation_id = str(summary["observation_id"])
        if observation_id in observation_index:
            raise ReportDataError(f"duplicate observation_id: {observation_id}")
        observation_index[observation_id] = summary

    findings: list[dict[str, object]] = []
    confirmed_count = 0
    static_supported_count = 0
    artifact_cache: dict[str, dict[str, Any]] = {}
    source_cache: dict[str, tuple[str, int]] = {}

    def load(digest: str) -> dict[str, Any]:
        if digest not in artifact_cache:
            artifact_cache[digest] = _artifact(digest, artifact_loader, artifact_module)
        return artifact_cache[digest]

    for bug_key, raw_bug in sorted(normalized_bug_index.items(), key=lambda item: str(item[0])):
        if not isinstance(bug_key, str) or not bug_key:
            raise ReportDataError("bug_index keys must be non-empty strings")
        bug = _mapping(raw_bug, f"bug_index[{bug_key}]")
        bug_id = bug.get("bug_id")
        if bug_id != bug_key:
            raise ReportDataError(f"bug_index key does not match bug_id: {bug_key}")
        assessment = bug.get("assessment")
        if assessment == "confirmed":
            confirmed_count += 1
        elif assessment == "static_supported":
            static_supported_count += 1

        candidate_digest = bug.get("candidate_digest")
        candidate_artifact = load(candidate_digest)
        if candidate_artifact.get("artifact_type") != "BugCandidate":
            raise ReportDataError(f"candidate digest resolves to a non-candidate artifact: {bug_id}")
        candidate = candidate_artifact.get("payload")
        if not isinstance(candidate, Mapping) or candidate.get("bug_id") != bug_id:
            raise ReportDataError(f"candidate payload does not match bug_index entry: {bug_id}")

        source_refs = _source_refs(
            candidate.get("source_refs"), root, "source_refs", source_cache,
        )
        expectation_refs = _source_refs(
            candidate.get("expectation_refs"), root, "expectation_refs", source_cache,
        )
        raw_observation_ids = bug.get("observation_ids", [])
        if not isinstance(raw_observation_ids, Sequence) or isinstance(
            raw_observation_ids, (str, bytes, bytearray),
        ):
            raise ReportDataError(f"observation_ids must be an array for {bug_id}")
        bug_observations: list[dict[str, object]] = []
        for observation_id in raw_observation_ids:
            if not isinstance(observation_id, str) or observation_id not in observation_index:
                raise ReportDataError(f"bug {bug_id} references an unknown observation")
            observed = dict(observation_index[observation_id])
            digest = observed.get("artifact_digest")
            if not isinstance(digest, str) or not digest:
                raise ReportDataError(f"observation artifact digest is missing: {observation_id}")
            result_artifact = load(digest)
            payload = result_artifact.get("payload")
            if result_artifact.get("artifact_type") != "CheckResult":
                continue
            if (
                not isinstance(payload, Mapping)
                or payload.get("bug_id") != bug_id
                or payload.get("observation_id") != observation_id
            ):
                raise ReportDataError(
                    f"CheckResult does not match bug observation: {bug_id}/{observation_id}"
                )
            observed["execution_result"] = {
                key: payload.get(key)
                for key in (
                    "baseline_or_patched", "target_called", "assertion_result",
                    "tests_run", "tests_passed", "tests_failed", "tests_skipped",
                    "error",
                )
                if key in payload
            }
            bug_observations.append(observed)

        actual_behavior = next((
            item.get("actual_behavior") or item.get("observed_behavior")
            for item in bug_observations
            if item.get("execution_result")
            and (item.get("actual_behavior") or item.get("observed_behavior"))
        ), None)
        behavior_difference = None
        if isinstance(actual_behavior, str) and actual_behavior:
            behavior_difference = {
                "expected": candidate.get("expected_behavior"),
                "actual": actual_behavior,
            }

        repro_digests = bug.get("repro_digests", [])
        review_digests = bug.get("review_digests", [])
        if not isinstance(repro_digests, Sequence) or isinstance(repro_digests, (str, bytes, bytearray)):
            raise ReportDataError(f"repro_digests must be an array for {bug_id}")
        if not isinstance(review_digests, Sequence) or isinstance(review_digests, (str, bytes, bytearray)):
            raise ReportDataError(f"review_digests must be an array for {bug_id}")
        repro_evidence = []
        for digest in repro_digests:
            artifact = load(digest)
            payload = artifact.get("payload")
            if artifact.get("artifact_type") != "ReproCase" or not isinstance(payload, Mapping) or payload.get("bug_id") != bug_id:
                raise ReportDataError(f"reproduction artifact does not match bug {bug_id}")
            repro_evidence.append({
                "artifact_digest": digest,
                "target_entry": payload.get("target_entry"),
                "expected_assertion": payload.get("expected_assertion"),
            })
        review_evidence = []
        for digest in review_digests:
            artifact = load(digest)
            payload = artifact.get("payload")
            if artifact.get("artifact_type") != "ReviewResult" or not isinstance(payload, Mapping) or payload.get("bug_id") != bug_id:
                raise ReportDataError(f"review artifact does not match bug {bug_id}")
            review_evidence.append({
                "artifact_digest": digest,
                "verdict": payload.get("verdict"),
                "reason": payload.get("reason"),
                "expectation_check": payload.get("expectation_check"),
                "target_call_check": payload.get("target_call_check"),
                "normal_case_check": payload.get("normal_case_check"),
            })

        findings.append({
            "bug_id": bug_id,
            "assessment": assessment,
            "evidence_level": bug.get("evidence_level"),
            "remediation": bug.get("remediation"),
            "summary": candidate.get("summary"),
            "category": candidate.get("category"),
            "expected_behavior": candidate.get("expected_behavior"),
            "suspected_behavior": candidate.get("suspected_behavior"),
            "actual_behavior": actual_behavior,
            "behavior_difference": behavior_difference,
            "source_refs": source_refs,
            "expectation_refs": expectation_refs,
            "execution_observations": bug_observations,
            "reproduction_evidence": repro_evidence,
            "review_evidence": review_evidence,
        })

    report = {
        "schema_version": 1,
        "run_id": run_id,
        "graph_id": graph_id,
        "goal": goal,
        "confirmed_count": confirmed_count,
        "static_supported_count": static_supported_count,
        "validation_state": None,
        "repair_state": None,
        "metrics": None,
        "findings": findings,
    }
    try:
        json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReportDataError(f"report data is not finite JSON: {exc}") from exc
    return report

