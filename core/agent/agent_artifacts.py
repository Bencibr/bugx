"""Strict parsing, digesting, and storage for Agent graph artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping, TypeAlias

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # strict validation fails closed when the validator is unavailable
    Draft202012Validator = None  # type: ignore[assignment,misc]
    FormatChecker = None  # type: ignore[assignment,misc]


_this_dir = Path(__file__).resolve().parent
SCHEMA_PATH = (
    _this_dir.parent / "schemas" / "agent-graph.schema.json"
    if (_this_dir.parent / "schemas").is_dir()
    else _this_dir / "schemas" / "agent-graph.schema.json"
)
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


class ArtifactError(ValueError):
    """Base class for invalid or unsafe Agent artifacts."""


class ArtifactValidationError(ArtifactError):
    """An artifact failed strict JSON, schema, or digest validation."""


class ArtifactPathError(ArtifactError):
    """An artifact path is unsafe or escapes its configured root."""


class ArtifactType(str, Enum):
    BUG_CANDIDATE = "BugCandidate"
    REPRO_CASE = "ReproCase"
    REVIEW_RESULT = "ReviewResult"
    PATCH_CANDIDATE = "PatchCandidate"
    CHECK_PLAN = "CheckPlan"
    CHECK_RESULT = "CheckResult"
    RUN_REPORT = "RunReport"


class BugCategory(str, Enum):
    LOGIC = "logic"
    STATE = "state"
    BOUNDARY = "boundary"
    EXCEPTION = "exception"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    RESOURCE = "resource"


class CandidateSource(str, Enum):
    AGENT = "agent"
    STATIC_HINT = "static_hint"


class ReviewVerdict(str, Enum):
    SUPPORTED = "supported"
    REJECTED = "rejected"
    NEEDS_CONTEXT = "needs_context"


class CheckPhase(str, Enum):
    BASELINE = "baseline"
    PATCHED = "patched"


@dataclass(frozen=True, slots=True)
class SourceRef:
    path: str
    file_digest: str
    line_start: int
    line_end: int
    symbol: str


@dataclass(frozen=True, slots=True)
class Attachment:
    path: str
    size: int
    sha256: str
    media_type: str


@dataclass(frozen=True, slots=True)
class CaseFile:
    path: str
    content: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PlanReference:
    plan_digest: str


@dataclass(frozen=True, slots=True)
class CheckPlan:
    plan_id: str
    phase: CheckPhase
    workspace_id: str
    profile_digest: str
    argv: tuple[str, ...]
    cwd: str
    env_keys: tuple[str, ...]
    deadline: float
    max_output_bytes: int
    selected_tests: tuple[str, ...]
    expected_oracle: str


@dataclass(frozen=True, slots=True)
class NormalCase:
    description: str
    assertion: str


@dataclass(frozen=True, slots=True)
class CallEvidencePlan:
    expected_symbols: tuple[str, ...]
    match_regex: str | None = None


@dataclass(frozen=True, slots=True)
class PatchEdit:
    path: str
    old_digest: str | None
    diff: str


@dataclass(frozen=True, slots=True)
class CheckLimits:
    deadline: float
    max_output_bytes: int
    output_bytes: int
    timed_out: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class BugReference:
    bug_id: str
    artifact_digest: str


@dataclass(frozen=True, slots=True)
class FailedTask:
    task_id: str
    stage: str
    error: str


@dataclass(frozen=True, slots=True)
class UnsupportedRange:
    path: str
    line_start: int
    line_end: int
    reason: str


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    artifact_type: ArtifactType
    artifact_digest: str


@dataclass(frozen=True, slots=True)
class BugCandidate:
    bug_id: str
    source_refs: tuple[SourceRef, ...]
    category: BugCategory
    summary: str
    expected_behavior: str
    expectation_refs: tuple[SourceRef, ...]
    suspected_behavior: str
    call_path: tuple[str, ...]
    preconditions: tuple[str, ...]
    candidate_source: CandidateSource


Plan: TypeAlias = CheckPlan | PlanReference


@dataclass(frozen=True, slots=True)
class ReproCase:
    bug_id: str
    candidate_digest: str
    target_entry: str
    case_files: tuple[CaseFile, ...]
    setup_plan: Plan
    run_plan: Plan
    expected_assertion: str
    normal_cases: tuple[str | NormalCase, ...]
    call_evidence_plan: CallEvidencePlan


@dataclass(frozen=True, slots=True)
class ReviewResult:
    bug_id: str
    reviewer_task_id: str
    candidate_digest: str
    repro_digest: str
    observation_ids: tuple[str, ...]
    expectation_check: bool
    target_call_check: bool
    normal_case_check: bool
    alternative_explanations: tuple[str, ...]
    verdict: ReviewVerdict
    reason: str


@dataclass(frozen=True, slots=True)
class PatchCandidate:
    bug_id: str
    base_snapshot_digest: str
    edits: tuple[PatchEdit, ...]
    reason: str
    affected_paths: tuple[str, ...]
    repro_digest: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    plan_digest: str
    observation_id: str
    bug_id: str
    baseline_or_patched: CheckPhase
    target_called: bool
    assertion_result: bool | None
    tests_run: int
    tests_passed: int
    tests_failed: int
    tests_skipped: int
    baseline_failures: tuple[str, ...]
    new_failures: tuple[str, ...]
    limits: CheckLimits
    error: str | None


@dataclass(frozen=True, slots=True)
class RunReport:
    graph_id: str
    snapshot_digest: str
    profile_digest: str
    summary: str
    bugs: tuple[BugReference, ...]
    failed_tasks: tuple[FailedTask, ...]
    unsupported_ranges: tuple[UnsupportedRange, ...]
    metrics: Mapping[str, int | float]
    source_revision: str
    report_revision: str
    artifact_refs: tuple[ArtifactReference, ...]


ArtifactPayload: TypeAlias = (
    BugCandidate | ReproCase | ReviewResult | PatchCandidate | CheckPlan | CheckResult | RunReport
)


@dataclass(frozen=True, slots=True)
class AgentArtifact:
    schema_version: int
    artifact_type: ArtifactType
    artifact_id: str
    producer_role: str
    graph_id: str
    run_id: str
    round_no: int
    scheduler_epoch: int
    snapshot_digest: str
    task_id: str | None
    stage: str
    attempt: int
    worker_id: str | None
    lease_id: str | None
    execution_id: str | None
    parent_digests: tuple[str, ...]
    payload: ArtifactPayload
    attachments: tuple[Attachment, ...]
    created_at: str
    artifact_digest: str


def _json_value(value: Any) -> Any:
    """Return JSON-native values while rejecting unsupported Python values."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ArtifactValidationError("JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ArtifactValidationError("NaN and Infinity are forbidden in artifacts")
        return value
    raise ArtifactValidationError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON data as compact UTF-8 with recursively sorted object keys."""
    try:
        return json.dumps(
            _json_value(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise ArtifactValidationError(f"artifact cannot be serialized as canonical JSON: {exc}") from exc


def compute_artifact_digest(artifact: Mapping[str, Any] | AgentArtifact) -> str:
    """Hash the complete envelope after excluding only its artifact_digest field."""
    raw = artifact_to_dict(artifact) if isinstance(artifact, AgentArtifact) else dict(artifact)
    raw.pop("artifact_digest", None)
    return hashlib.sha256(canonical_json_bytes(raw)).hexdigest()


def seal_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an envelope, calculate its self-excluding digest, and validate it."""
    raw = dict(artifact)
    raw.pop("artifact_digest", None)
    raw["artifact_digest"] = compute_artifact_digest(raw)
    validate_artifact(raw)
    return raw


def _reject_constant(value: str) -> None:
    raise ArtifactValidationError(f"non-finite JSON number is forbidden: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ArtifactValidationError(f"non-finite JSON number is forbidden: {value}")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def parse_json(data: str | bytes | bytearray) -> Any:
    """Parse strict UTF-8 JSON, rejecting duplicate keys and non-finite numbers."""
    if isinstance(data, (bytes, bytearray)):
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactValidationError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
        try:
            text = bytes(data).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ArtifactValidationError("artifact JSON must be UTF-8") from exc
    elif isinstance(data, str):
        try:
            encoded = data.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ArtifactValidationError("artifact JSON contains invalid Unicode") from exc
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ArtifactValidationError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
        text = data
    else:
        raise ArtifactValidationError("artifact JSON input must be str or bytes")
    try:
        return json.loads(
            text, parse_constant=_reject_constant, parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
    except ArtifactValidationError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ArtifactValidationError(f"invalid JSON: {exc}") from exc


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    try:
        raw = SCHEMA_PATH.read_bytes()
    except OSError as exc:
        raise ArtifactValidationError(f"cannot read artifact schema: {exc}") from exc
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ArtifactValidationError("artifact schema exceeds configured size limit")
    schema = parse_json(raw)
    if not isinstance(schema, dict):
        raise ArtifactValidationError("artifact schema root must be an object")
    if Draft202012Validator is None or FormatChecker is None:
        raise ArtifactValidationError("jsonschema Draft 2020-12 validator is unavailable")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ArtifactValidationError(f"artifact JSON Schema is invalid: {exc}") from exc
    return schema


def _validation_error(instance: Any, schema: dict[str, Any], field_path: str = "$") -> None:
    if Draft202012Validator is None or FormatChecker is None:
        raise ArtifactValidationError("jsonschema Draft 2020-12 validator is unavailable")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    error = next(validator.iter_errors(instance), None)
    if error is not None:
        path = field_path
        for part in error.absolute_path:
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        raise ArtifactValidationError(f"{path}: {error.message}")


def validate_payload(artifact_type: ArtifactType | str, payload: Mapping[str, Any]) -> None:
    """Strictly validate one payload against its artifact-specific JSON Schema."""
    try:
        kind = ArtifactType(artifact_type)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"unknown artifact_type: {artifact_type!r}") from exc
    if not isinstance(payload, Mapping):
        raise ArtifactValidationError("artifact payload must be an object")
    schema = _load_schema()
    _validation_error(
        _json_value(payload),
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{_PAYLOAD_SCHEMA_NAMES[kind]}",
        },
        "$.payload",
    )
    _validate_payload_relations(kind, payload)


def validate_artifact(artifact: Mapping[str, Any] | AgentArtifact) -> None:
    """Validate exact envelope/payload shape and verify the canonical digest."""
    raw = artifact_to_dict(artifact) if isinstance(artifact, AgentArtifact) else _json_value(artifact)
    if not isinstance(raw, dict):
        raise ArtifactValidationError("artifact envelope must be an object")
    _validation_error(raw, _load_schema())
    kind = ArtifactType(raw["artifact_type"])
    _validate_payload_relations(kind, raw["payload"])
    computed = compute_artifact_digest(raw)
    if raw["artifact_digest"] != computed:
        raise ArtifactValidationError(
            f"artifact_digest mismatch: expected {computed}, got {raw['artifact_digest']}"
        )


def _validate_payload_relations(kind: ArtifactType, payload: Mapping[str, Any]) -> None:
    """Enforce simple cross-field facts that JSON Schema cannot express portably."""
    refs: list[Mapping[str, Any]] = []
    if kind is ArtifactType.BUG_CANDIDATE:
        refs.extend(payload["source_refs"])
        refs.extend(payload["expectation_refs"])
    elif kind is ArtifactType.RUN_REPORT:
        refs.extend(payload["unsupported_ranges"])
    for ref in refs:
        if ref["line_end"] < ref["line_start"]:
            raise ArtifactValidationError("line_end must be greater than or equal to line_start")


_PAYLOAD_SCHEMA_NAMES: dict[ArtifactType, str] = {
    ArtifactType.BUG_CANDIDATE: "bug_candidate",
    ArtifactType.REPRO_CASE: "repro_case",
    ArtifactType.REVIEW_RESULT: "review_result",
    ArtifactType.PATCH_CANDIDATE: "patch_candidate",
    ArtifactType.CHECK_PLAN: "check_plan",
    ArtifactType.CHECK_RESULT: "check_result",
    ArtifactType.RUN_REPORT: "run_report",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ArtifactValidationError("expected an object with string keys")
    return value


def _source_ref(value: Any) -> SourceRef:
    raw = _mapping(value)
    return SourceRef(**raw)


def _check_plan(value: Any) -> CheckPlan:
    raw = _mapping(value)
    return CheckPlan(
        plan_id=raw["plan_id"], phase=CheckPhase(raw["phase"]),
        workspace_id=raw["workspace_id"], profile_digest=raw["profile_digest"],
        argv=tuple(raw["argv"]), cwd=raw["cwd"], env_keys=tuple(raw["env_keys"]),
        deadline=raw["deadline"], max_output_bytes=raw["max_output_bytes"],
        selected_tests=tuple(raw["selected_tests"]), expected_oracle=raw["expected_oracle"],
    )


def _plan(value: Any) -> Plan:
    raw = _mapping(value)
    if "plan_digest" in raw:
        return PlanReference(plan_digest=raw["plan_digest"])
    return _check_plan(raw)


def _parse_payload_unchecked(kind: ArtifactType, value: Any) -> ArtifactPayload:
    raw = _mapping(value)
    if kind is ArtifactType.BUG_CANDIDATE:
        return BugCandidate(
            bug_id=raw["bug_id"], source_refs=tuple(_source_ref(item) for item in raw["source_refs"]),
            category=BugCategory(raw["category"]), summary=raw["summary"],
            expected_behavior=raw["expected_behavior"],
            expectation_refs=tuple(_source_ref(item) for item in raw["expectation_refs"]),
            suspected_behavior=raw["suspected_behavior"], call_path=tuple(raw["call_path"]),
            preconditions=tuple(raw["preconditions"]),
            candidate_source=CandidateSource(raw["candidate_source"]),
        )
    if kind is ArtifactType.REPRO_CASE:
        return ReproCase(
            bug_id=raw["bug_id"], candidate_digest=raw["candidate_digest"],
            target_entry=raw["target_entry"],
            case_files=tuple(CaseFile(**_mapping(item)) for item in raw["case_files"]),
            setup_plan=_plan(raw["setup_plan"]), run_plan=_plan(raw["run_plan"]),
            expected_assertion=raw["expected_assertion"],
            normal_cases=tuple(
                NormalCase(**_mapping(item)) if isinstance(item, Mapping) else item
                for item in raw["normal_cases"]
            ),
            call_evidence_plan=CallEvidencePlan(**_mapping(raw["call_evidence_plan"])),
        )
    if kind is ArtifactType.REVIEW_RESULT:
        return ReviewResult(
            bug_id=raw["bug_id"], reviewer_task_id=raw["reviewer_task_id"],
            candidate_digest=raw["candidate_digest"], repro_digest=raw["repro_digest"],
            observation_ids=tuple(raw["observation_ids"]),
            expectation_check=raw["expectation_check"], target_call_check=raw["target_call_check"],
            normal_case_check=raw["normal_case_check"],
            alternative_explanations=tuple(raw["alternative_explanations"]),
            verdict=ReviewVerdict(raw["verdict"]), reason=raw["reason"],
        )
    if kind is ArtifactType.PATCH_CANDIDATE:
        return PatchCandidate(
            bug_id=raw["bug_id"], base_snapshot_digest=raw["base_snapshot_digest"],
            edits=tuple(PatchEdit(**_mapping(item)) for item in raw["edits"]),
            reason=raw["reason"], affected_paths=tuple(raw["affected_paths"]),
            repro_digest=raw["repro_digest"],
        )
    if kind is ArtifactType.CHECK_PLAN:
        return _check_plan(raw)
    if kind is ArtifactType.CHECK_RESULT:
        return CheckResult(
            plan_digest=raw["plan_digest"], observation_id=raw["observation_id"],
            bug_id=raw["bug_id"], baseline_or_patched=CheckPhase(raw["baseline_or_patched"]),
            target_called=raw["target_called"], assertion_result=raw["assertion_result"],
            tests_run=raw["tests_run"], tests_passed=raw["tests_passed"],
            tests_failed=raw["tests_failed"], tests_skipped=raw["tests_skipped"],
            baseline_failures=tuple(raw["baseline_failures"]), new_failures=tuple(raw["new_failures"]),
            limits=CheckLimits(**_mapping(raw["limits"])), error=raw["error"],
        )
    if kind is not ArtifactType.RUN_REPORT:
        raise ArtifactValidationError(f"unsupported artifact_type: {kind.value}")
    return RunReport(
        graph_id=raw["graph_id"], snapshot_digest=raw["snapshot_digest"],
        profile_digest=raw["profile_digest"], summary=raw["summary"],
        bugs=tuple(BugReference(**_mapping(item)) for item in raw["bugs"]),
        failed_tasks=tuple(FailedTask(**_mapping(item)) for item in raw["failed_tasks"]),
        unsupported_ranges=tuple(UnsupportedRange(**_mapping(item)) for item in raw["unsupported_ranges"]),
        metrics=dict(raw["metrics"]), source_revision=raw["source_revision"],
        report_revision=raw["report_revision"],
        artifact_refs=tuple(
            ArtifactReference(
                artifact_id=item["artifact_id"], artifact_type=ArtifactType(item["artifact_type"]),
                artifact_digest=item["artifact_digest"],
            )
            for item in raw["artifact_refs"]
        ),
    )


def parse_bug_candidate(payload: Mapping[str, Any]) -> BugCandidate:
    validate_payload(ArtifactType.BUG_CANDIDATE, payload)
    return _parse_payload_unchecked(ArtifactType.BUG_CANDIDATE, payload)  # type: ignore[return-value]


def parse_repro_case(payload: Mapping[str, Any]) -> ReproCase:
    validate_payload(ArtifactType.REPRO_CASE, payload)
    return _parse_payload_unchecked(ArtifactType.REPRO_CASE, payload)  # type: ignore[return-value]


def parse_review_result(payload: Mapping[str, Any]) -> ReviewResult:
    validate_payload(ArtifactType.REVIEW_RESULT, payload)
    return _parse_payload_unchecked(ArtifactType.REVIEW_RESULT, payload)  # type: ignore[return-value]


def parse_patch_candidate(payload: Mapping[str, Any]) -> PatchCandidate:
    validate_payload(ArtifactType.PATCH_CANDIDATE, payload)
    return _parse_payload_unchecked(ArtifactType.PATCH_CANDIDATE, payload)  # type: ignore[return-value]


def parse_check_plan(payload: Mapping[str, Any]) -> CheckPlan:
    validate_payload(ArtifactType.CHECK_PLAN, payload)
    return _parse_payload_unchecked(ArtifactType.CHECK_PLAN, payload)  # type: ignore[return-value]


def parse_check_result(payload: Mapping[str, Any]) -> CheckResult:
    validate_payload(ArtifactType.CHECK_RESULT, payload)
    return _parse_payload_unchecked(ArtifactType.CHECK_RESULT, payload)  # type: ignore[return-value]


def parse_run_report(payload: Mapping[str, Any]) -> RunReport:
    validate_payload(ArtifactType.RUN_REPORT, payload)
    return _parse_payload_unchecked(ArtifactType.RUN_REPORT, payload)  # type: ignore[return-value]


def parse_artifact(value: Mapping[str, Any] | AgentArtifact) -> AgentArtifact:
    """Validate and parse one complete envelope into immutable typed records."""
    validate_artifact(value)
    raw = _json_value(value)
    kind = ArtifactType(raw["artifact_type"])
    payload = _parse_payload_unchecked(kind, raw["payload"])
    return AgentArtifact(
        schema_version=raw["schema_version"], artifact_type=kind,
        artifact_id=raw["artifact_id"], producer_role=raw["producer_role"],
        graph_id=raw["graph_id"], run_id=raw["run_id"], round_no=raw["round_no"],
        scheduler_epoch=raw["scheduler_epoch"], snapshot_digest=raw["snapshot_digest"],
        task_id=raw["task_id"], stage=raw["stage"], attempt=raw["attempt"],
        worker_id=raw["worker_id"], lease_id=raw["lease_id"], execution_id=raw["execution_id"],
        parent_digests=tuple(raw["parent_digests"]), payload=payload,
        attachments=tuple(Attachment(**_mapping(item)) for item in raw["attachments"]),
        created_at=raw["created_at"], artifact_digest=raw["artifact_digest"],
    )


def parse_artifact_json(data: str | bytes | bytearray) -> AgentArtifact:
    value = parse_json(data)
    if not isinstance(value, Mapping):
        raise ArtifactValidationError("artifact JSON root must be an object")
    return parse_artifact(value)


def artifact_to_dict(artifact: AgentArtifact | Mapping[str, Any]) -> dict[str, Any]:
    value = _json_value(artifact)
    if not isinstance(value, dict):
        raise ArtifactValidationError("artifact envelope must be an object")
    return value


def _confined_path(root: Path, relative_path: str, *, create_parent: bool) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ArtifactPathError("artifact path must be a non-empty relative string")
    windows_path = PureWindowsPath(relative_path)
    if "\\" in relative_path or windows_path.is_absolute() or windows_path.drive:
        raise ArtifactPathError(f"artifact path is not a canonical relative path: {relative_path}")
    if Path(relative_path).is_absolute():
        raise ArtifactPathError(f"absolute artifact path is forbidden: {relative_path}")
    parts = relative_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ArtifactPathError(f"path traversal or non-canonical component is forbidden: {relative_path}")
    if len(relative_path.encode("utf-8")) > 4096:
        raise ArtifactPathError("artifact path exceeds 4096 UTF-8 bytes")
    try:
        if create_parent:
            root.mkdir(parents=True, exist_ok=True)
        safe_root = root.resolve(strict=True)
    except OSError as exc:
        raise ArtifactPathError(f"artifact root is unavailable: {exc}") from exc
    target = safe_root.joinpath(*parts)
    try:
        target.relative_to(safe_root)
    except ValueError as exc:
        raise ArtifactPathError(f"artifact path escapes root: {relative_path}") from exc
    cursor = safe_root
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ArtifactPathError(f"symbolic links are forbidden in artifact paths: {relative_path}")
    if create_parent:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ArtifactPathError(f"cannot create artifact directory: {exc}") from exc
        cursor = safe_root
        for part in parts[:-1]:
            cursor = cursor / part
            if cursor.is_symlink() or not cursor.is_dir():
                raise ArtifactPathError(f"artifact parent is not a safe directory: {cursor}")
    return target


def store_artifact(
    root: str | os.PathLike[str],
    relative_path: str,
    artifact: AgentArtifact | Mapping[str, Any],
) -> Path:
    """Validate and create a new canonical artifact file under ``root``."""
    raw = artifact_to_dict(artifact)
    validate_artifact(raw)
    encoded = canonical_json_bytes(raw)
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ArtifactValidationError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    root_path = Path(root)
    target = _confined_path(root_path, relative_path, create_parent=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise ArtifactPathError(f"artifact already exists: {relative_path}") from exc
    except OSError as exc:
        raise ArtifactPathError(f"cannot safely create artifact: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise ArtifactPathError(f"cannot write artifact: {exc}") from exc
    return target


def load_artifact(
    root: str | os.PathLike[str],
    relative_path: str,
) -> AgentArtifact:
    """Read a confined, bounded-size JSON artifact and validate it strictly."""
    target = _confined_path(Path(root), relative_path, create_parent=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise ArtifactPathError(f"cannot safely open artifact: {exc}") from exc
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactPathError("artifact must be a regular file")
            if info.st_size > MAX_ARTIFACT_BYTES:
                raise ArtifactValidationError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
            contents = stream.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise ArtifactPathError(f"cannot read artifact: {exc}") from exc
    if len(contents) > MAX_ARTIFACT_BYTES:
        raise ArtifactValidationError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    return parse_artifact_json(contents)

