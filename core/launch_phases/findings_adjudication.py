#!/usr/bin/env python3
"""Findings adjudication and ledger receipt verification for launch phases."""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.launch_phases.common import pollings_root, resolve_project_root

_ADJUDICATOR_BUILD_CACHE: str | None = None
_RECEIPT_STATUSES = frozenset({"passed", "failed", "error", "blocked", "skipped"})
_RECEIPT_FAILURE_STATUSES = frozenset({"failed", "error"})
_RECEIPT_REQUIRED_FIELDS = (
    "execution_id", "lease_id", "worker_id", "artifact_path",
    "artifact_sha256", "command", "status",
)
_MAX_LEDGER_SCANS = 64
_SHA64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _get_settlement_module() -> Any:
    return (sys.modules.get("core.launch_phases.finding_settlement")
            or sys.modules.get("finding_settlement"))


def read_json_object(path: Path) -> dict[str, Any] | None:
    mod = _get_settlement_module()
    fn = getattr(mod, "_read_json_object", None) if mod else None
    if fn is not None:
        return fn(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def adjudicator_build() -> str:
    """裁决器构建标记（core/metadata.txt 的 build_id），进程内缓存。"""
    global _ADJUDICATOR_BUILD_CACHE
    if _ADJUDICATOR_BUILD_CACHE is not None:
        return _ADJUDICATOR_BUILD_CACHE
    build = "unknown"
    try:
        metadata = (
            Path(__file__).resolve().parent.parent / "metadata.txt"
        ).read_text(encoding="utf-8")
        for line in metadata.splitlines():
            stripped = line.strip()
            if stripped.startswith("build_id:"):
                build = stripped.split(":", 1)[1].strip().strip('"') or build
                break
    except (OSError, ValueError):
        pass
    _ADJUDICATOR_BUILD_CACHE = f"build.{build}"
    return _ADJUDICATOR_BUILD_CACHE


def valid_evidence_receipt(receipt: object) -> dict | None:
    """F1a 附着的 evidence_receipt 最小形状校验；不合法按"无 receipt"处理。"""
    if not isinstance(receipt, dict):
        return None
    for field in _RECEIPT_REQUIRED_FIELDS:
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
    if not isinstance(receipt.get("artifact_sha256"), str) or \
            not _SHA64_RE.fullmatch(receipt["artifact_sha256"]):
        return None
    if receipt["status"] not in _RECEIPT_STATUSES:
        return None
    return receipt


def load_claim_ledger(root: Path) -> list[dict]:
    """加载引擎持有的 claim/execution 台账（supervisor state 快照的 claims）。"""
    candidates: list[Path] = [
        root / ".bugx" / "state" / "supervisor.json",
        *sorted((pollings_root(root)).glob(
            "*/lifecycles/*/state/supervisor.json",
        ))[:_MAX_LEDGER_SCANS],
        *sorted((pollings_root(root)).glob(
            "*/lifecycles/*/supervisor.json",
        ))[:_MAX_LEDGER_SCANS],
    ]
    claims: list[dict] = []
    for state_path in candidates[:_MAX_LEDGER_SCANS]:
        payload = read_json_object(state_path)
        if not payload:
            continue
        raw_claims = payload.get("claims")
        if not isinstance(raw_claims, dict):
            continue
        for item in raw_claims.values():
            if isinstance(item, dict):
                claims.append(item)
        if len(claims) >= 1024:
            break
    return claims


def lookup_receipt_claim(
    claims: list[dict], receipt: dict,
) -> dict | None:
    """按 execution_id/lease_id/worker_id 三元组在台账中查找匹配（非文本信任）。"""
    execution_id = str(receipt.get("execution_id") or "")
    lease_id = str(receipt.get("lease_id") or "")
    worker_id = str(receipt.get("worker_id") or "")
    if not (execution_id and lease_id and worker_id):
        return None
    for claim in claims:
        if (
            str(claim.get("execution_id") or "") == execution_id
            and str(claim.get("lease_id") or "") == lease_id
            and str(claim.get("worker_id") or "") == worker_id
        ):
            return claim
    return None


def load_evidence_store_records(root: Path) -> list[dict]:
    """收集 coverage-evidence.json 的当前记录与被替换历史（真实执行留痕）。"""
    candidates: list[Path] = [
        root / ".bugx" / "state" / "coverage-evidence.json",
        *sorted((pollings_root(root)).glob(
            "*/lifecycles/*/state/coverage-evidence.json",
        ))[:_MAX_LEDGER_SCANS],
        *sorted((pollings_root(root)).glob(
            "*/lifecycles/*/coverage-evidence.json",
        ))[:_MAX_LEDGER_SCANS],
    ]
    records: list[dict] = []
    for store_path in candidates[:_MAX_LEDGER_SCANS]:
        payload = read_json_object(store_path)
        if not payload:
            continue
        raw_records = payload.get("records")
        if isinstance(raw_records, list):
            records.extend(
                item for item in raw_records if isinstance(item, dict)
            )
        raw_superseded = payload.get("superseded")
        if isinstance(raw_superseded, list):
            for entry in raw_superseded:
                if not isinstance(entry, dict):
                    continue
                replaced = entry.get("replaced")
                if isinstance(replaced, dict):
                    records.append(replaced)
        if len(records) >= 2048:
            break
    return [
        item for item in records
        if isinstance(item.get("execution_id"), str) and item["execution_id"]
    ]


def artifact_matches(root: Path, artifact_path: str, expected_sha: str) -> bool:
    """Safely read a project-contained regular artifact and verify its current digest."""
    try:
        from core.launch_phases.post_dry_run import ARTIFACT_LIMIT, RootReader

        root_path = Path(root).resolve(strict=True)
        candidate = Path(artifact_path)
        lexical = Path(os.path.abspath(
            str(candidate if candidate.is_absolute() else root_path / candidate),
        ))
        if lexical == root_path or root_path not in lexical.parents:
            return False
        relative = lexical.relative_to(root_path).as_posix()
        reader = RootReader(root_path)
        try:
            snapshot = reader.read(relative, ARTIFACT_LIMIT, "artifact")
        finally:
            reader.close()
    except (OSError, ValueError):
        return False
    return snapshot.sha256.lower() == str(expected_sha).lower()


def ledger_supervisor_state_paths(root: Path, lifecycle_id: str) -> list[tuple[Path, str]]:
    """Resolve Supervisor state only through validated project layout manifests."""
    import core.bugx_paths as bugx_paths

    bugx_paths.require(root)
    bugx_root = root / ".bugx"
    pollings_root = bugx_root / "pollings"
    bugx_paths._validate_identifier(lifecycle_id, "lifecycle")
    bugx_paths.assert_no_symlink_components(pollings_root, error_cls=ValueError)
    if not pollings_root.is_dir() or pollings_root.is_symlink():
        return []
    polling_dirs = sorted(pollings_root.iterdir(), key=lambda item: item.name)
    if len(polling_dirs) > _MAX_LEDGER_SCANS:
        return []

    matches: list[tuple[Path, str]] = []
    for polling_dir in polling_dirs:
        if polling_dir.is_symlink():
            return []
        if not polling_dir.is_dir() or not polling_dir.name.startswith("polling-"):
            continue
        bugx_paths._validate_identifier(polling_dir.name, "polling")
        polling_manifest = bugx_paths._read_json(polling_dir / "manifest.json")
        if polling_manifest.get("polling_id") != polling_dir.name:
            return []
        lifecycle_root = bugx_paths._id_path(
            polling_dir / "lifecycles", lifecycle_id, "lifecycle",
        )
        if lifecycle_root.is_symlink():
            return []
        if not lifecycle_root.is_dir():
            continue
        lifecycle_manifest = bugx_paths._read_json(lifecycle_root / "manifest.json")
        if (
            lifecycle_manifest.get("lifecycle_id") != lifecycle_id
            or lifecycle_manifest.get("polling_id") != polling_dir.name
        ):
            return []
        state_path = lifecycle_root / "state" / "supervisor.json"
        bugx_paths.assert_no_symlink_components(state_path, error_cls=ValueError)
        matches.append((state_path, polling_dir.name))
    return matches


def read_supervisor_state_helper(reader: Any, relative: str) -> dict[str, Any]:
    mod = _get_settlement_module()
    fn = getattr(mod, "_read_supervisor_state", None) if mod else None
    if fn is not None:
        return fn(reader, relative)
    from core.launch_phases.post_dry_run import JSON_LIMIT, SnapshotInvalid, verify_supervisor_history
    from core.launch_phases.findings_snapshot import FindingsSnapshotError

    try:
        source = reader.read(relative, JSON_LIMIT, "supervisor")
        payload = json.loads(source.raw_bytes.decode("utf-8"))
    except SnapshotInvalid as exc:
        raise FindingsSnapshotError(exc.reason_code) from None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):
        raise FindingsSnapshotError("supervisor_state_invalid") from None
    if not isinstance(payload, dict):
        raise FindingsSnapshotError("supervisor_state_root_invalid")
    verify_supervisor_history(payload)
    return payload


def verify_ledger_incident_receipt(
    root: Path,
    *,
    finding_fingerprint: str,
    lifecycle_id: str,
    round_no: int,
    execution_id: str,
    artifact_sha256: str,
) -> bool:
    """Verify a ledger receipt against the Supervisor finding, claim, and artifact."""
    if not all(isinstance(value, str) and value.strip() for value in (
        finding_fingerprint, lifecycle_id, execution_id, artifact_sha256,
    )) or not isinstance(round_no, int) or isinstance(round_no, bool) or round_no < 1:
        return False
    if not _SHA64_RE.fullmatch(artifact_sha256):
        return False
    try:
        root_path = Path(root).resolve(strict=True)
        state_paths = ledger_supervisor_state_paths(root_path, lifecycle_id)
        if not state_paths or len(state_paths) > _MAX_LEDGER_SCANS:
            return False

        import core.bugx_paths as bugx_paths
        from core.coverage import coverage_evidence
        from core.supervisor import checkpoint_pipeline
        from core.launch_phases.post_dry_run import RootReader

        matched_states = 0
        for state_path, polling_id in state_paths:
            if not state_path.exists() or state_path.is_symlink():
                continue
            bugx_paths.assert_no_symlink_components(
                state_path, error_cls=ValueError,
            )
            if not state_path.is_file() or state_path.stat().st_size > 16 * 1024 * 1024:
                continue
            reader = RootReader(root_path)
            try:
                state = read_supervisor_state_helper(
                    reader, state_path.relative_to(root_path).as_posix(),
                )
            finally:
                reader.close()
            if (
                state.get("round_id") != lifecycle_id
                or state.get("session_id") != polling_id
                or (state.get("life_id") not in (None, lifecycle_id))
            ):
                continue
            findings = state.get("findings")
            claims = state.get("claims")
            if not isinstance(findings, list) or not isinstance(claims, dict):
                continue
            matching_findings = []
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                try:
                    if checkpoint_pipeline.finding_key(finding) == finding_fingerprint:
                        matching_findings.append(finding)
                except (TypeError, ValueError):
                    continue
            if len(matching_findings) != 1:
                continue
            finding = matching_findings[0]
            receipt = valid_evidence_receipt(finding.get("evidence_receipt"))
            if receipt is None:
                continue
            try:
                normalized_receipt = checkpoint_pipeline.normalize_execution_receipt(
                    receipt,
                    str(finding.get("case_id") or finding.get("unit") or "finding"),
                )
            except (TypeError, ValueError):
                continue
            if (
                normalized_receipt.get("execution_id") != execution_id
                or str(normalized_receipt.get("artifact_sha256", "")).lower()
                != artifact_sha256.lower()
            ):
                continue
            worker_id = str(normalized_receipt.get("worker_id") or "")
            claim = claims.get(worker_id)
            claim_round = claim.get("round_no") if isinstance(claim, dict) else None
            submitted_at = claim.get("submitted_at") if isinstance(claim, dict) else None
            if not isinstance(claim, dict) or (
                claim.get("worker_id") != worker_id
                or claim.get("execution_id") != execution_id
                or claim.get("lease_id") != normalized_receipt.get("lease_id")
                or isinstance(claim_round, bool)
                or not isinstance(claim_round, int)
                or claim_round != round_no
                or isinstance(submitted_at, bool)
                or not isinstance(submitted_at, (int, float))
                or not math.isfinite(submitted_at)
                or submitted_at <= 0
            ):
                continue

            evidence_path = state_path.parent / "coverage-evidence.json"
            if not evidence_path.is_file() or evidence_path.is_symlink():
                continue
            bugx_paths.assert_no_symlink_components(
                evidence_path, error_cls=ValueError,
            )
            if evidence_path.stat().st_size > 16 * 1024 * 1024:
                continue
            evidence_payload = read_json_object(evidence_path)
            if not evidence_payload:
                continue
            records = evidence_payload.get("records", [])
            superseded = evidence_payload.get("superseded", [])
            if not isinstance(records, list) or not isinstance(superseded, list):
                continue
            candidates = [item for item in records if isinstance(item, dict)]
            candidates.extend(
                entry["replaced"]
                for entry in superseded
                if isinstance(entry, dict)
                and isinstance(entry.get("replaced"), dict)
            )
            expected_path = str(normalized_receipt.get("artifact_path") or "")
            expected_artifact = coverage_evidence._resolve_artifact(
                expected_path, root_path,
            )
            if expected_artifact.stat().st_size > checkpoint_pipeline.MAX_ARTIFACT_BYTES:
                continue
            expected_rel = expected_artifact.relative_to(root_path).as_posix()
            receipt_digest = checkpoint_pipeline.execution_receipt_sha256(
                normalized_receipt,
            )
            store = coverage_evidence.EvidenceStore(
                evidence_path, workspace_root=root_path,
            )
            for record in candidates:
                if (
                    record.get("execution_id") != execution_id
                    or record.get("lease_id") != normalized_receipt.get("lease_id")
                    or record.get("worker_id") != worker_id
                    or record.get("command") != normalized_receipt.get("command")
                    or record.get("status") != normalized_receipt.get("status")
                    or record.get("exit_code") != normalized_receipt.get("exit_code")
                    or str(record.get("artifact_sha256", "")).lower()
                    != artifact_sha256.lower()
                    or record.get("execution_receipt_sha256") != receipt_digest
                    or record.get("target_drifted") is True
                ):
                    continue
                try:
                    record_rel = coverage_evidence._resolve_artifact(
                        str(record.get("artifact_path") or ""), root_path,
                    ).relative_to(root_path).as_posix()
                except (OSError, ValueError, coverage_evidence.EvidenceError):
                    continue
                if record_rel != expected_rel:
                    continue
                verification = store.verify(record)
                if (
                    verification.valid
                    and artifact_matches(root_path, expected_rel, artifact_sha256)
                ):
                    matched_states += 1
                    break
        return matched_states == 1
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, ImportError):
        return False


def receipt_observation(receipt: dict, fingerprint: str) -> dict:
    """从引擎核验过的 receipt 构建单一真实执行通道的 observation。"""
    status = str(receipt.get("status") or "")
    if status in _RECEIPT_FAILURE_STATUSES:
        outcome = "failure"
    elif status == "passed":
        outcome = "pass"
    else:
        outcome = "blocked"
    execution_id = str(receipt["execution_id"])
    return {
        "run_id": execution_id,
        "channel": f"checkpoint:{execution_id}",
        "fingerprint": fingerprint,
        "reference": str(receipt["command"])[:80],
        "outcome": outcome,
        "artifact_path": str(receipt["artifact_path"]),
        "artifact_sha256": str(receipt["artifact_sha256"]),
    }


def has_fully_bound_failure_observations(conf: object) -> bool:
    """Path 1 判据：worker 提交的真实 observations 中 failure 观测全部绑定工件。"""
    if not isinstance(conf, dict):
        return False
    observations = conf.get("observations")
    if not isinstance(observations, list) or not observations:
        return False
    failures = [
        item for item in observations
        if isinstance(item, dict) and item.get("outcome") == "failure"
    ]
    if not failures:
        return False
    from core.ledger import finding_confirmation as _fc
    return all(
        isinstance(item.get("artifact_path"), str) and item["artifact_path"].strip()
        and isinstance(item.get("artifact_sha256"), str)
        and _fc.SHA256_RE.fullmatch(item["artifact_sha256"])
        for item in failures
    )


def build_receipt_backed_confirmation(
    finding: dict,
    receipt: dict,
    claims: list[dict],
    records: list[dict],
    run_id: str | None,
    root: Path,
) -> dict | None:
    """反查命中后，以 receipt 及其同 unit 的其他受监督真实执行构建确认 proto。"""
    fid = str(finding.get("fingerprint") or finding.get("id") or "finding")
    if not artifact_matches(root, receipt["artifact_path"], receipt["artifact_sha256"]):
        return None
    if receipt["status"] not in _RECEIPT_FAILURE_STATUSES:
        return None
    primary_record = next(
        (
            item for item in records
            if item.get("execution_id") == receipt["execution_id"]
            and item.get("lease_id") == receipt["lease_id"]
        ),
        None,
    )
    target_fingerprint = str(
        (primary_record or {}).get("target_fingerprint") or "",
    )
    if not target_fingerprint:
        return None
    unit_id = str((primary_record or {}).get("unit_id") or "")

    observations = [receipt_observation(receipt, fid)]
    for record in records:
        if len(observations) >= 2:
            break
        if record is primary_record:
            continue
        if unit_id and str(record.get("unit_id") or "") != unit_id:
            continue
        if str(record.get("command") or "") != str(receipt["command"]):
            continue
        if record.get("status") not in _RECEIPT_FAILURE_STATUSES:
            continue
        if lookup_receipt_claim(claims, {
            "execution_id": record.get("execution_id"),
            "lease_id": record.get("lease_id"),
            "worker_id": record.get("worker_id"),
        }) is None:
            continue
        sibling_path = str(record.get("artifact_path") or "")
        sibling_sha = str(record.get("artifact_sha256") or "")
        if not sibling_path or not artifact_matches(root, sibling_path, sibling_sha):
            continue
        execution_id = str(record["execution_id"])
        observations.append({
            "run_id": execution_id,
            "channel": f"checkpoint:{execution_id}",
            "fingerprint": fid,
            "reference": str(record.get("command") or "")[:80],
            "outcome": "failure",
            "artifact_path": sibling_path,
            "artifact_sha256": sibling_sha,
        })
    if len(observations) < 2:
        return None

    negative_control = None
    existing = finding.get("confirmation")
    if isinstance(existing, dict):
        candidate_negative = existing.get("negative_control")
        if isinstance(candidate_negative, dict) and candidate_negative.get(
            "outcome",
        ) == "pass" and isinstance(
            candidate_negative.get("artifact_path"), str,
        ) and candidate_negative["artifact_path"].strip() and isinstance(
            candidate_negative.get("artifact_sha256"), str,
        ):
            negative_control = {
                "outcome": "pass",
                "reference": str(candidate_negative.get("reference")
                                 or candidate_negative["artifact_path"])[:80],
                "artifact_path": candidate_negative["artifact_path"],
                "artifact_sha256": candidate_negative["artifact_sha256"],
            }

    proto = {
        "state": "candidate",
        "oracle": {
            "tier": "contract",
            "reference": str(finding.get("title") or "Contract violation"),
            "provenance": {
                "kind": "repository-contract",
                "independent_from_change": True,
                "verified": True,
            },
        },
        "observations": observations,
        "root_cause_verified": bool(
            len(observations) >= 2 and negative_control is not None
        ),
        "target_execution": {
            "target_platform": "source",
            "executed_platform": "source",
            "target_fingerprint": target_fingerprint,
            "run_reference": (run_id or str(receipt["execution_id"])),
            "executed": True,
            "simulated": False,
            "outcome": "failure",
            "errors": [],
        },
    }
    if negative_control is not None:
        proto["negative_control"] = negative_control
    return proto


def adjudicate_candidate_findings(
    findings: list[dict],
    root: Path | None = None,
    run_id: str | None = None,
) -> None:
    """程序级确定性自动裁决器（DOC-188/DOC-193；F1' 重写：证据真实性优先）。"""
    from core.ledger import finding_confirmation as _fc
    mod = _get_settlement_module()
    fn_resolve = getattr(mod, "resolve_project_root", resolve_project_root) if mod else resolve_project_root
    ledger_root = root if root is not None else fn_resolve()
    claims: list[dict] | None = None
    records: list[dict] | None = None
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        conf = finding.get("confirmation")
        state = conf.get("state") if isinstance(conf, dict) else "candidate"
        if state in {"confirmed", "rejected"}:
            continue
        original_conf = conf if isinstance(conf, dict) else None

        if has_fully_bound_failure_observations(conf):
            try:
                _fc.transition(finding, "confirmed")
            except Exception:
                continue
            finding.setdefault("confirmation", {})["adjudicator_build"] = (
                adjudicator_build()
            )
            continue

        receipt = valid_evidence_receipt(finding.get("evidence_receipt"))
        if receipt is None:
            continue
        if isinstance(conf, dict) and conf.get("observations"):
            continue
        if claims is None:
            claims = load_claim_ledger(ledger_root)
        claim = lookup_receipt_claim(claims, receipt)
        if claim is None:
            continue
        if records is None:
            records = load_evidence_store_records(ledger_root)
        proto = build_receipt_backed_confirmation(
            finding, receipt, claims, records, run_id, ledger_root,
        )
        if proto is None:
            continue
        finding["confirmation"] = proto
        try:
            _fc.transition(finding, "confirmed")
        except Exception:
            if original_conf is not None:
                finding["confirmation"] = original_conf
            else:
                finding.pop("confirmation", None)
            continue
        finding.setdefault("confirmation", {})["adjudicator_build"] = (
            adjudicator_build()
        )


def compact_title(title: str, max_chars: int = 60) -> str:
    """PE-20261004-005：finding 标题压实为可读短语（≤max_chars + 省略号）。"""
    cleaned = " ".join(str(title or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "…"


# Compatibility aliases
_ADJUDICATOR_BUILD_CACHE = _ADJUDICATOR_BUILD_CACHE
_RECEIPT_STATUSES = _RECEIPT_STATUSES
_RECEIPT_FAILURE_STATUSES = _RECEIPT_FAILURE_STATUSES
_RECEIPT_REQUIRED_FIELDS = _RECEIPT_REQUIRED_FIELDS
_MAX_LEDGER_SCANS = _MAX_LEDGER_SCANS
_SHA64_RE = _SHA64_RE
_adjudicator_build = adjudicator_build
_valid_evidence_receipt = valid_evidence_receipt
_load_claim_ledger = load_claim_ledger
_lookup_receipt_claim = lookup_receipt_claim
_load_evidence_store_records = load_evidence_store_records
_artifact_matches = artifact_matches
_ledger_supervisor_state_paths = ledger_supervisor_state_paths
_read_supervisor_state = read_supervisor_state_helper
_receipt_observation = receipt_observation
_has_fully_bound_failure_observations = has_fully_bound_failure_observations
_build_receipt_backed_confirmation = build_receipt_backed_confirmation
_adjudicate_candidate_findings = adjudicate_candidate_findings
_compact_title = compact_title
