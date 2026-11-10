#!/usr/bin/env python3
"""Findings snapshot capture and binding verification for launch phases."""
from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from core.launch_phases.common import resolve_project_root, uses_project_layout
from core.launch_phases.session_runtime import baseline_json, load_runtime

CREDITED_CAP_DEFAULT = 5
CREDITED_CAP_MIN = 1
CREDITED_CAP_MAX = 200
CREDITED_CAP_ENV = "BUGX_CREDITED_CAP"

_FINDINGS_SEAL_EVENT_TYPES = (
    "finding_decisions_synced", "findings_report_sealed",
)


@dataclass(frozen=True)
class FindingsSnapshot:
    """One bounded, no-follow read shared by post gates and knowledge materialization."""

    root: Path
    path: Path
    raw_bytes: bytes = field(repr=False)
    sha256: str
    payload: Mapping[str, Any] = field(repr=False)
    findings: tuple[Mapping[str, Any], ...] = field(repr=False)
    artifact_snapshot: Mapping[str, Mapping[str, Any]] = field(repr=False)
    expected_binding: Mapping[str, Any] | None = field(repr=False)
    layout_policy: str


class FindingsSnapshotError(ValueError):
    """Stable, non-sensitive reason for rejecting a Finding snapshot.

    ``detail`` 是给宿主/用户的**人读原因**（实测痛点：post 只打印 reason_code
    如 findings_binding_mismatch，用户无从知道旧 run_id 是什么、该怎么办——
    2026-11-09 复盘固化：报错必须码+原因成对出现）。
    """

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)


def _get_settlement_module() -> Any:
    return (sys.modules.get("core.launch_phases.finding_settlement")
            or sys.modules.get("finding_settlement"))


def deep_freeze(value: Any) -> Any:
    """Copy JSON containers into immutable mappings/tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    return value


def deep_thaw(value: Any) -> Any:
    """Return a detached mutable JSON-shaped copy for adjudication."""
    if isinstance(value, Mapping):
        return {key: deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_thaw(item) for item in value]
    return value


def credited_cap() -> int:
    """D9：结算计命上限——模块常量 + 环境变量覆盖（非法值回默认）。"""
    raw = os.environ.get(CREDITED_CAP_ENV)
    if raw is None:
        return CREDITED_CAP_DEFAULT
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return CREDITED_CAP_DEFAULT
    if CREDITED_CAP_MIN <= value <= CREDITED_CAP_MAX:
        return value
    return CREDITED_CAP_DEFAULT


def findings_payload_path(runtime: dict | None = None) -> Path | None:
    """findings.json 的项目布局感知路径；缺失返回 None。"""
    try:
        mod = _get_settlement_module()
        rt = runtime if isinstance(runtime, dict) else None
        if rt is None:
            fn_load = getattr(mod, "load_runtime", load_runtime) if mod else load_runtime
            rt = fn_load()
        fn_lifecycle = getattr(mod, "_lifecycle_findings_path", None) if mod else None
        if fn_lifecycle is not None:
            settled = fn_lifecycle(rt)
            if settled is not None and settled.is_file():
                return settled
    except (OSError, ValueError, RuntimeError):
        pass
    fn_layout = getattr(mod, "uses_project_layout", uses_project_layout) if mod else uses_project_layout
    if fn_layout():
        return None
    candidate = Path(".bugx/findings.json")
    return candidate if candidate.is_file() else None


def baseline_run_id() -> str | None:
    """PE-20261005-006：无显式 runtime 时从 life 基线取运行 UUID（可缺失）。"""
    mod = _get_settlement_module()
    fn_baseline = getattr(mod, "baseline_json", baseline_json) if mod else baseline_json
    baseline = fn_baseline() or {}
    run_id = baseline.get("run_id")
    return run_id if isinstance(run_id, str) and run_id.strip() else None


def verify_findings_binding(
    runtime: dict, path: Path | None = None, *,
    snapshot: FindingsSnapshot | None = None,
    snapshot_captured: bool = False,
) -> tuple[bool, str]:
    """G2 门禁：findings 报告的 run_binding 必须与当前运行会话一致。"""
    try:
        mod = _get_settlement_module()
        fn_capture = getattr(mod, "capture_findings_snapshot", capture_findings_snapshot) if mod else capture_findings_snapshot
        if snapshot is None and not snapshot_captured:
            snapshot = fn_capture(runtime, path=path)
        elif snapshot is not None and path is not None and snapshot.path != path:
            return False, "findings_snapshot_path_mismatch"
    except FindingsSnapshotError as exc:
        return False, exc.reason_code
    if snapshot is None:
        return True, "no-findings"
    return True, "ok"


def text_line_count(raw_bytes: bytes) -> int:
    """Count text lines with universal-newline semantics from the captured bytes."""
    decoded = raw_bytes.decode("utf-8", errors="replace")
    return sum(1 for _line in io.StringIO(decoded, newline=None))


def findings_artifact_paths(payload: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Return evidence paths and separately declared confirmation artifacts."""
    evidence: set[str] = set()
    referenced: set[str] = set()
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return evidence, referenced
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        raw_evidence = finding.get("evidence")
        if isinstance(raw_evidence, list):
            for item in raw_evidence:
                if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                    evidence.add(item["path"])
        confirmation = finding.get("confirmation")
        if not isinstance(confirmation, Mapping):
            continue
        observations = confirmation.get("observations")
        if isinstance(observations, list):
            for item in observations:
                if isinstance(item, Mapping) and isinstance(item.get("artifact_path"), str):
                    referenced.add(item["artifact_path"])
        negative = confirmation.get("negative_control")
        if isinstance(negative, Mapping) and isinstance(negative.get("artifact_path"), str):
            referenced.add(negative["artifact_path"])
    return evidence, referenced


def authoritative_round(runtime: Mapping[str, Any]) -> int | None:
    """当前 Round 的权威取值。"""
    if isinstance(runtime, Mapping):
        for key in ("round", "round_at_start"):
            value = runtime.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                return value
    return None


def expected_findings_binding(runtime: Mapping[str, Any]) -> dict[str, Any] | None:
    mod = _get_settlement_module()
    fn_auth = getattr(mod, "_authoritative_round", authoritative_round) if mod else authoritative_round
    round_no = fn_auth(runtime)
    run_id = runtime.get("run_id")
    lifecycle_id = runtime.get("lifecycle_id")
    polling_id = runtime.get("polling_id")
    if (
        isinstance(run_id, str) and run_id.strip()
        and isinstance(lifecycle_id, str) and lifecycle_id.strip()
        and isinstance(round_no, int) and not isinstance(round_no, bool) and round_no >= 1
        and (polling_id is None or isinstance(polling_id, str) and polling_id.strip())
    ):
        return {
            "runtime_run_uuid": run_id,
            "lifecycle_id": lifecycle_id,
            "round": round_no,
            "polling_id": polling_id,
        }
    return None


def validate_findings_payload_snapshot(
    payload: dict[str, Any], runtime: Mapping[str, Any], root: Path, reader: Any, *,
    initial_size: int, report_relative: str | None = None, report_source: Any = None,
) -> tuple[dict[str, Any] | None, Mapping[str, Mapping[str, Any]], str]:
    """Validate a parsed payload against bounded, no-follow artifact bytes."""
    from core.launch_phases.post_dry_run import ARTIFACT_LIMIT, SNAPSHOT_LIMIT, SnapshotInvalid

    mod = _get_settlement_module()
    fn_expected = getattr(mod, "_expected_findings_binding", expected_findings_binding) if mod else expected_findings_binding
    fn_layout = getattr(mod, "uses_project_layout", uses_project_layout) if mod else uses_project_layout
    fn_paths = getattr(mod, "_findings_artifact_paths", findings_artifact_paths) if mod else findings_artifact_paths
    fn_line_count = getattr(mod, "_text_line_count", text_line_count) if mod else text_line_count

    expected_binding = fn_expected(runtime)
    layout_policy = "formal" if fn_layout() else "legacy"
    evidence_paths, referenced_paths = fn_paths(payload)
    try:
        from core.ledger.finding_io import snapshot_relative_path, validate_payload_snapshot

        normalized_evidence: set[str] = set()
        for item in evidence_paths:
            try:
                normalized_evidence.add(snapshot_relative_path(item, root))
            except Exception:
                continue
        normalized_references = {
            snapshot_relative_path(item, root) for item in referenced_paths
        }
        normalized_paths = normalized_evidence | normalized_references
        artifact_snapshot: dict[str, Mapping[str, Any]] = {}
        total_size = initial_size
        for artifact_path in sorted(normalized_paths):
            if artifact_path == report_relative and report_source is not None:
                captured = report_source
            else:
                try:
                    captured = reader.read(artifact_path, ARTIFACT_LIMIT, "artifact")
                except SnapshotInvalid:
                    if artifact_path in normalized_evidence and artifact_path not in normalized_references:
                        continue
                    raise
            if not (artifact_path == report_relative and report_source is not None):
                total_size += captured.size
            if total_size > SNAPSHOT_LIMIT:
                raise SnapshotInvalid("snapshot_size_limit")
            metadata: dict[str, Any] = {
                "path": artifact_path,
                "device": captured.device,
                "inode": captured.inode,
                "size": captured.size,
                "sha256": captured.sha256,
            }
            if artifact_path in normalized_evidence:
                metadata["line_count"] = fn_line_count(captured.raw_bytes)
            artifact_snapshot[artifact_path] = MappingProxyType(metadata)
        validate_payload_snapshot(
            payload, root, expected_binding, artifact_snapshot, layout_policy,
        )
        return expected_binding, MappingProxyType(artifact_snapshot), layout_policy
    except SnapshotInvalid as exc:
        raise FindingsSnapshotError(exc.reason_code) from None
    except FindingsSnapshotError:
        raise
    except Exception as exc:
        message = str(exc)
        if (
            "run_binding" in message
            or "runtime/polling 身份" in message
            or "当前 runtime" in message
            or "expected_binding" in message
        ):
            # detail 带原始差异（旧 run_id vs 当前 run_id 等），post 打印给
            # 宿主/用户——只给码不给原因曾让遗留文件死锁无从下手（实测）。
            raise FindingsSnapshotError("findings_binding_mismatch", message[:500]) from None
        raise FindingsSnapshotError("findings_validation_failed", message[:500]) from None


def capture_findings_snapshot(
    runtime: dict, *, path: Path | None = None,
) -> FindingsSnapshot | None:
    """Read findings and all referenced artifacts once, then validate that snapshot."""
    from core.launch_phases.post_dry_run import FINDINGS_LIMIT, SnapshotInvalid, RootReader

    mod = _get_settlement_module()
    fn_settle_path = getattr(mod, "settle_findings_path", None) if mod else None
    selected = path if path is not None else (fn_settle_path(runtime) if fn_settle_path else None)
    if selected is None:
        return None
    fn_root = getattr(mod, "resolve_project_root", resolve_project_root) if mod else resolve_project_root
    root = Path(os.path.abspath(os.fspath(fn_root())))
    candidate = Path(os.path.abspath(os.fspath(selected)))
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        raise FindingsSnapshotError("findings_outside_project_root") from None
    try:
        reader = RootReader(root)
    except SnapshotInvalid as exc:
        raise FindingsSnapshotError(exc.reason_code) from None
    try:
        source = reader.read(relative, FINDINGS_LIMIT, "findings")
        payload = json.loads(source.raw_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise FindingsSnapshotError("findings_root_invalid")
        runtime_snapshot = dict(runtime) if isinstance(runtime, Mapping) else {}
        fn_val = getattr(mod, "_validate_findings_payload_snapshot", validate_findings_payload_snapshot) if mod else validate_findings_payload_snapshot
        expected_binding, artifact_snapshot, layout_policy = fn_val(
            payload, runtime_snapshot, root, reader, initial_size=source.size,
            report_relative=relative, report_source=source,
        )

        findings = payload.get("findings")
        if not isinstance(findings, list):
            raise FindingsSnapshotError("findings_validation_failed") from None
        return FindingsSnapshot(
            root=root,
            path=candidate,
            raw_bytes=source.raw_bytes,
            sha256=source.sha256,
            payload=deep_freeze(payload),
            findings=tuple(deep_freeze(item) for item in findings),
            artifact_snapshot=artifact_snapshot,
            expected_binding=(
                MappingProxyType(expected_binding) if expected_binding is not None else None
            ),
            layout_policy=layout_policy,
        )
    except FindingsSnapshotError:
        raise
    except SnapshotInvalid as exc:
        raise FindingsSnapshotError(exc.reason_code) from None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):
        raise FindingsSnapshotError("findings_snapshot_invalid") from None
    finally:
        reader.close()


# Compatibility aliases
_deep_freeze = deep_freeze
_deep_thaw = deep_thaw
_baseline_run_id = baseline_run_id
_text_line_count = text_line_count
_findings_artifact_paths = findings_artifact_paths
_authoritative_round = authoritative_round
_expected_findings_binding = expected_findings_binding
_validate_findings_payload_snapshot = validate_findings_payload_snapshot
