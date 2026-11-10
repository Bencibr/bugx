#!/usr/bin/env python3
"""core/supervisor/checkpoint_pipeline.py - Checkpoint 校验与发布流水线 (DOC-070)。

负责 Checkpoint 数据清洗、大小/深度门禁、Finding/Evidence 去重与规整、
签名提取与覆盖证据发布到 coverage-evidence.json。
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

try:
    from core.coverage import module_coverage
except ImportError:
    from coverage import module_coverage

from .supervisor_models import (
    MAX_ARTIFACT_PATH_LENGTH,
    MAX_COMMAND_LENGTH,
    MAX_COVERAGE_PER_CHECKPOINT,
    MAX_EVIDENCE_PER_CHECKPOINT,
    MAX_FINDING_FIELD_LENGTH,
    MAX_FINDING_FINGERPRINT_LENGTH,
    MAX_FINDINGS_PER_CHECKPOINT,
    MAX_ARTIFACT_BYTES,
    ARTIFACT_HASH_CHUNK_BYTES,
    MAX_SIGNATURE_LENGTH,
    MAX_SIGNATURES_PER_CHECKPOINT,
    MAX_UNIT_NOTE_LENGTH,
    SHA256_RE,
    VALID_COVERAGE_STATES,
    TERMINAL_COVERED,
    COVERAGE_COVERED,
    COVERAGE_PENDING,
    unit_source_path,
    COVERAGE_REMOVED,
    COVERAGE_CORRUPTED,
    COVERAGE_STALE,
    VALID_EVIDENCE_STATES,
    InvalidCheckpoint,
    finding_severity_levels,
    normalize_finding_severity,
    RunState,
    WorkspacePathError,
    check_depth_and_keys,
    canonical_json,
    confined_regular_file,
    confined_worker_artifact,
    default_coverage_manifest,
    invalidate_git_head_cache,
    workspace_fingerprint,
    target_identity,
    atomic_write_bytes,
)

_workspace_fingerprint = workspace_fingerprint
_target_identity = target_identity

from core.supervisor.lease_manager import append_event


# A passed checkpoint must point at a generated execution artifact, not the
# source file used to run the check.  The Supervisor cannot observe an
# external worker's stdout directly, so this bounded denylist closes the
# reported ``verify.py``/``repro.py`` loophole without pretending to prove
# process execution.  ponytail: suffix denylist; add an explicit artifact
# kind/receipt contract if generated source-suffix outputs become necessary.
_SOURCE_ARTIFACT_SUFFIXES = frozenset({
    ".bash", ".c", ".cc", ".cjs", ".cpp", ".cs", ".cxx", ".fish",
    ".go", ".h", ".hpp", ".java", ".js", ".jsx", ".kt", ".kts",
    ".mjs", ".php", ".pl", ".pm", ".py", ".pyw", ".rb", ".rs",
    ".scala", ".sh", ".swift", ".ts", ".tsx", ".zsh",
})
EXECUTION_RECEIPT_KIND = "bugx-worker-execution-receipt/v1"
UNVERIFIED_EXECUTION = "UNVERIFIED_EXECUTION"
_EXECUTION_RECEIPT_FIELDS = frozenset({
    "schema_version", "execution_id", "worker_id", "lease_id", "command",
    "exit_code", "artifact_path", "artifact_sha256", "status",
})
# Worker 输出契约要求候选自带的结构化复现三件套（worker_runtime/worker_orchestrator）。
# 这是结算自动裁决器（finding_settlement._adjudicate_candidate_findings）的关键
# 复现信号：归一化时只在非空字符串时透传，半截/垃圾值一律剔除，防止污染下游。
_POC_CARRY_FIELDS = ("poc_cmd", "poc_input", "poc_negative_input")
MAX_CHECKPOINT_BYTES = 512 * 1024
_STAGED_EVIDENCE_TTL_NS = 10 * 60 * 1_000_000_000


def _preserve_poc_carry_fields(item: dict[str, object]) -> None:
    """就地保留结构化复现三件套：非空字符串才保留，其余剔除（不新增事实）。"""
    for field in _POC_CARRY_FIELDS:
        value = item.get(field)
        if not (isinstance(value, str) and value.strip()):
            item.pop(field, None)


def execution_receipt_sha256(receipt: Mapping[str, object]) -> str:
    """Return the durable digest for a normalized claim-bound receipt.

    The digest preserves the receipt identity when the coverage manifest stores
    only the compact execution metadata.  It does not turn a Worker assertion
    into independent proof that a command ran.
    """
    return hashlib.sha256(canonical_json(receipt)).hexdigest()


def normalize_execution_receipt(
    receipt: Mapping[str, object], unit: str,
) -> dict[str, object]:
    """Normalize the worker-produced receipt without trusting its claims yet."""
    if not isinstance(receipt, Mapping):
        raise InvalidCheckpoint(f"evidence[{unit}].execution_receipt 必须是对象")
    check_depth_and_keys(receipt)
    missing = _EXECUTION_RECEIPT_FIELDS - set(receipt)
    unknown = set(receipt) - _EXECUTION_RECEIPT_FIELDS
    if missing:
        raise InvalidCheckpoint(
            f"evidence[{unit}].execution_receipt 缺字段: {sorted(missing)}"
        )
    if unknown:
        raise InvalidCheckpoint(
            f"evidence[{unit}].execution_receipt 含未知字段: {sorted(unknown)}"
        )
    if receipt["schema_version"] != 1:
        raise InvalidCheckpoint(
            f"evidence[{unit}].execution_receipt schema_version 不支持"
        )
    for field in (
        "execution_id", "worker_id", "lease_id", "command", "artifact_path",
        "artifact_sha256", "status",
    ):
        value = receipt[field]
        if not isinstance(value, str) or not value.strip():
            raise InvalidCheckpoint(
                f"evidence[{unit}].execution_receipt.{field} 无效"
            )
    if not isinstance(receipt["exit_code"], int) or isinstance(receipt["exit_code"], bool):
        raise InvalidCheckpoint(
            f"evidence[{unit}].execution_receipt.exit_code 无效"
        )
    if not SHA256_RE.fullmatch(str(receipt["artifact_sha256"])):
        raise InvalidCheckpoint(
            f"evidence[{unit}].execution_receipt.artifact_sha256 无效"
        )
    if receipt["status"] not in VALID_EVIDENCE_STATES:
        raise InvalidCheckpoint(
            f"evidence[{unit}].execution_receipt.status 无效"
        )
    return copy.deepcopy(dict(receipt))


def validate_execution_receipt(
    item: Mapping[str, object],
    *,
    unit: str,
    worker_id: str,
    lease_id: str,
    execution_id: str,
    trusted_execution: bool,
) -> None:
    """Bind a receipt to the Supervisor-owned claim and evidence metadata."""
    receipt = item.get("execution_receipt")
    artifact = str(item.get("artifact_path") or "")
    if receipt is None:
        if item.get("status") == "passed":
            if Path(artifact).suffix.lower() in _SOURCE_ARTIFACT_SUFFIXES:
                raise InvalidCheckpoint(
                    f"evidence[{unit}].artifact_path 必须是执行输出，不能直接引用源码脚本"
                )
            raise InvalidCheckpoint(
                f"evidence[{unit}] passed 必须绑定受信 worker execution receipt"
                "（缺失项是 execution_receipt 对象：schema_version=1 + execution_id/"
                "worker_id/lease_id/command/exit_code/artifact_path/artifact_sha256/"
                "status 与本条 evidence 逐字一致；可用 "
                "`worker_checkpoint.py finalize` 按 claim 自动生成）"
            )
        return
    if not trusted_execution:
        raise InvalidCheckpoint(
            f"evidence[{unit}] 缺少受信 worker execution 记录"
        )
    expected = {
        "execution_id": execution_id,
        "worker_id": worker_id,
        "lease_id": lease_id,
        "command": item["command"],
        "exit_code": item["exit_code"],
        "artifact_path": item["artifact_path"],
        "artifact_sha256": item["artifact_sha256"],
        "status": item["status"],
    }
    for field, value in expected.items():
        if receipt[field] != value:
            raise InvalidCheckpoint(
                f"evidence[{unit}].execution_receipt.{field} 与执行证据不一致"
            )
    if receipt["status"] == "passed" and receipt["exit_code"] != 0:
        raise InvalidCheckpoint(
            f"evidence[{unit}].execution_receipt passed 时 exit_code 必须为 0"
        )


def finding_key(finding: Mapping[str, object]) -> str:
    explicit = finding.get("fingerprint")
    if explicit is not None and not isinstance(explicit, str):
        raise InvalidCheckpoint("finding fingerprint 必须是字符串")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        from core.ledger.life_ledger import finding_fingerprint

        return finding_fingerprint(finding)
    except (ImportError, TypeError, ValueError) as exc:
        raise InvalidCheckpoint(
            "finding 必须包含可识别的 finding_id/id/root_cause/title"
        ) from exc


def _unit_source_path(unit: object) -> str:
    """兼容别名：唯一实现已收敛到 ``supervisor_models.unit_source_path``。

    保留本名是因为 checkpoint 侧的调用点与回归用例按该名字断言；解析逻辑不得
    在本文件里再复制一份（claim 期的同文件互斥键与证据回填必须同源）。
    """
    return unit_source_path(unit)


def merge_coverage_updates(
    current: Mapping[str, str], updates: Mapping[str, str],
) -> dict[str, str]:
    """Apply checkpoint coverage updates while preserving covered monotonicity."""
    merged = dict(current)
    for unit, status in updates.items():
        if merged.get(unit) == COVERAGE_COVERED and status != COVERAGE_STALE:
            continue
        merged[unit] = status
    return dict(sorted(merged.items()))


def migrate_coverage(
    previous: Mapping[str, str], current_units: Sequence[str],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Reconcile coverage history against a newly trusted manifest."""
    current = set(current_units)
    migrated = dict(previous)
    added: list[str] = []
    removed: list[str] = []
    stale: list[str] = []
    for unit, status in tuple(migrated.items()):
        if unit not in current:
            if status not in {COVERAGE_CORRUPTED, COVERAGE_REMOVED}:
                migrated[unit] = COVERAGE_REMOVED
                removed.append(unit)
        elif status in TERMINAL_COVERED and status != COVERAGE_CORRUPTED:
            migrated[unit] = COVERAGE_STALE
            stale.append(unit)
    for unit in sorted(current - set(previous)):
        migrated[unit] = COVERAGE_PENDING
        added.append(unit)
    return dict(sorted(migrated.items())), {
        "added": added,
        "removed": sorted(removed),
        "stale": sorted(stale),
    }


def load_manifest_units(root: Path, manifest: Path) -> list[str]:
    """Load trusted coverage IDs; callers cannot invent queue units."""
    try:
        # Reject symlinked manifests before parsing. A project-local link must
        # not redirect queue construction to the shared engine or another
        # workspace.
        manifest = confined_regular_file(root, str(manifest))
    except WorkspacePathError as exc:
        raise InvalidCheckpoint(
            "coverage manifest 必须是 workspace 内的普通文件，不能经过符号链接"
        ) from exc
    try:
        manifest.relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidCheckpoint("coverage manifest 路径必须位于 workspace 内") from exc
    try:
        rows, errors = module_coverage.parse_manifest(manifest)
    except (OSError, ValueError) as exc:
        raise InvalidCheckpoint(f"覆盖清单无法加载: {exc}") from exc
    if errors:
        raise InvalidCheckpoint("覆盖清单无效: " + "; ".join(errors))
    units = sorted({module_coverage.coverage_unit_id(row) for row in rows})
    if not units:
        raise InvalidCheckpoint(f"覆盖清单没有可调度单元: {manifest}")
    return units


def load_checkpoint_payload(path: Path) -> Mapping[str, object]:
    """Read and structurally bound one CLI checkpoint JSON object."""
    try:
        if path.stat().st_size > MAX_CHECKPOINT_BYTES:
            raise InvalidCheckpoint(f"checkpoint JSON 过大（>512 KiB）: {path}")
        with path.open("rb") as stream:
            raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
        if len(raw) > MAX_CHECKPOINT_BYTES:
            raise InvalidCheckpoint(f"checkpoint JSON 过大（>512 KiB）: {path}")
        text = raw.decode("utf-8")
        payload = json.loads(text)
        check_depth_and_keys(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCheckpoint(f"checkpoint JSON 无法读取: {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise InvalidCheckpoint("checkpoint JSON 必须是对象")
    return payload


def _stage_owner_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM


def _reconcile_staged_evidence(target: Path) -> None:
    """Delete only dead or expired stage files for this locked evidence target."""
    prefix = f".{target.name}.stage-"
    now_ns = time.time_ns()
    for candidate in target.parent.glob(prefix + "*"):
        if candidate.is_symlink():
            raise WorkspacePathError(f"拒绝清理符号链接暂存证据: {candidate}")
        if not candidate.is_file():
            continue
        stem = candidate.name.removesuffix(".tmp.lock").removesuffix(".tmp")
        suffix = stem[len(prefix):]
        fields = suffix.split("-", 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        pid, created_ns = int(fields[0]), int(fields[1])
        if _stage_owner_alive(pid) and now_ns - created_ns < _STAGED_EVIDENCE_TTL_NS:
            continue
        candidate.unlink()


def checkpoint_payload_digest(
    *,
    findings: Sequence[Mapping[str, object]],
    coverage: Mapping[str, str],
    evidence: Mapping[str, Mapping[str, object]],
    notes: Mapping[str, str],
    signatures: Sequence[str],
    suggested_modules: Sequence[str],
    life_delta: float,
) -> str:
    """Digest the normalized checkpoint payload for retry/fencing checks."""
    payload = {
        "findings": [dict(item) for item in findings],
        "coverage": dict(coverage),
        "evidence": {key: dict(value) for key, value in evidence.items()},
        "notes": dict(notes),
        "signatures": list(signatures),
        "suggested_modules": list(suggested_modules),
        "life_delta": float(life_delta),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _derive_run_reference(
    failure_refs: list[tuple[str, str]],
    enriched: Mapping[str, object],
) -> str:
    """PE-20261005-006：target_execution.run_reference 取运行身份。

    优先候选 evidence 里的 run_id（checkpoint 契约要求其等于 state.run_id，
    见 checkpoint_sync evidence 校验）；无从取得时回退既有 channel:reference
    拼接，保持旧数据行为不变。
    """
    evidence = enriched.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                run_id = item.get("run_id")
                if isinstance(run_id, str) and run_id.strip():
                    return run_id
    return "; ".join(
        f"{channel}:{reference}" for channel, reference in failure_refs
    )


def enrich_gate_finding(
    finding: Mapping[str, object],
    workspace_root: Path | None = None,
) -> dict[str, object]:
    """把 worker 侧自由 schema 的 finding 归一化为确认门禁兼容契约。

    摄入即补齐 agent_eval 必需字段（id/title/root_cause/confidence/
    evidence 数组），确认门禁从"永远 invalid"变为可裁决；语义状态
    （candidate）与既有字段原样保留，不伪造确认。
    """
    enriched = dict(finding)
    # 结构化复现三件套透传：非空字符串才保留（旧候选无 poc 字段时行为不变）。
    _preserve_poc_carry_fields(enriched)
    if "severity" in enriched:
        enriched["severity"] = normalize_finding_severity(enriched["severity"])
    fingerprint = str(enriched.get("fingerprint") or enriched.get("id") or "")
    if not str(enriched.get("id") or "").strip():
        enriched["id"] = fingerprint
    if not str(enriched.get("title") or "").strip():
        enriched["title"] = str(
            enriched.get("description") or enriched["id"]
        )[:120]
    if not str(enriched.get("root_cause") or "").strip():
        enriched["root_cause"] = str(
            enriched.get("trigger_condition")
            or enriched.get("description")
            or enriched["title"]
        )
    confidence = enriched.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool)             or not 0 <= confidence <= 1:
        evidence_blob = json.dumps(
            enriched.get("evidence", ""), ensure_ascii=False, default=str,
        ).lower()
        blob = evidence_blob + str(enriched.get("trigger_condition", "")).lower()
        enriched["confidence"] = 0.6 if (
            "repro" in blob or "复现" in blob
        ) else 0.35
    # 确认包缺 target_execution 时，只能从带哈希且已重算通过的观测工件派生。
    # reference/channel 是 worker 自报字段，单独存在不能证明发生过目标执行。
    confirmation = enriched.get("confirmation")
    if (
        isinstance(confirmation, dict)
        and confirmation.get("state") == "confirmed"
        and not isinstance(confirmation.get("target_execution"), dict)
    ):
        import platform as _platform_mod

        observations = [
            dict(item) for item in confirmation.get("observations", [])
            if isinstance(item, dict)
        ]
        failure_refs = [
            (str(item.get("channel") or "unknown"),
             str(item.get("reference") or item.get("artifact_path") or ""))
            for item in observations if item.get("outcome") == "failure"
        ]
        failure_observations = [
            item for item in observations if item.get("outcome") == "failure"
        ]
        artifacts_bound = bool(failure_observations) and all(
            isinstance(item.get("artifact_path"), str)
            and bool(item.get("artifact_path"))
            and isinstance(item.get("artifact_sha256"), str)
            and bool(SHA256_RE.fullmatch(item["artifact_sha256"]))
            for item in failure_observations
        )
        artifact_errors = (
            [] if workspace_root is None or not artifacts_bound
            else _verify_observation_artifacts(enriched, workspace_root)
        )
        if (
            failure_refs
            and all(reference for _c, reference in failure_refs)
            and workspace_root is not None
            and artifacts_bound
            and not artifact_errors
        ):
            channel_platform = {
                "wsl2": "linux", "docker": "linux", "docker-desktop": "linux",
            }
            executed_platform = next(
                (channel_platform.get(channel, _platform_mod.system().lower())
                 for channel, _ref in failure_refs),
                _platform_mod.system().lower(),
            )
            confirmation["target_execution"] = {
                "target_platform": executed_platform,
                "executed_platform": executed_platform,
                "target_fingerprint": (
                    _workspace_fingerprint(workspace_root)
                    if workspace_root is not None else ""
                ),
                # PE-20261005-006：run_reference 必须引用运行身份使 sync 可判定
                # "报告属于本次运行"；优先候选 evidence 的 run_id（checkpoint 契约
                # 要求其等于 state.run_id），旧数据回退 channel:reference。
                "run_reference": _derive_run_reference(failure_refs, enriched),
                "executed": True,
                "simulated": False,
                "outcome": "failure",
                "errors": [],
            }

    evidence = enriched.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        source = str(enriched.get("file") or "").strip()
        if not source:
            unit = str(enriched.get("unit") or "")
            source = _unit_source_path(unit)
        # ``id`` is an identity, never a source path.  Keep an empty list when
        # no source was supplied so the checkpoint boundary can reject it
        # instead of manufacturing a path that only looks like evidence.
        enriched["evidence"] = [{"path": source}] if source else []
    return enriched


def _verify_observation_artifacts(finding: dict, workspace_root: Path) -> list[str]:
    """Reuse the confirmation verifier without making it a module import cycle."""
    from core.ledger.finding_confirmation import verify_observation_artifacts

    return verify_observation_artifacts(finding, workspace_root)


def normalize_findings(
    findings: Iterable[Mapping[str, object]] | None,
    workspace_root: Path | None = None,
) -> list[dict[str, object]]:
    input_findings: list[Mapping[str, object]] = []
    if findings is not None:
        if isinstance(findings, (str, bytes)):
            raise InvalidCheckpoint("findings 必须是数组")
        try:
            iterator = iter(findings)
        except TypeError as exc:
            raise InvalidCheckpoint("findings 必须是可迭代数组") from exc
        # Materialize exactly once. This preserves generator payloads and
        # stops an unbounded iterable at the protocol limit.
        for item in iterator:
            if len(input_findings) >= MAX_FINDINGS_PER_CHECKPOINT:
                raise InvalidCheckpoint(
                    f"findings 条数过多（>{MAX_FINDINGS_PER_CHECKPOINT}）"
                )
            input_findings.append(item)
        # Depth/keys guard after bounded materialization (fail-closed).
        check_depth_and_keys(input_findings)
    normalized: dict[str, dict[str, object]] = {}
    for finding in input_findings:
        if not isinstance(finding, Mapping):
            raise InvalidCheckpoint("findings 成员必须是对象")
        check_depth_and_keys(finding)
        key = finding_key(finding)
        if len(key) > MAX_FINDING_FINGERPRINT_LENGTH:
            raise InvalidCheckpoint(
                f"fingerprint 过长（>{MAX_FINDING_FINGERPRINT_LENGTH}）"
            )
        for fname, fval in finding.items():
            if isinstance(fval, str) and len(fval) > MAX_FINDING_FIELD_LENGTH:
                raise InvalidCheckpoint(
                    f"findings[{fname}] 过长（>{MAX_FINDING_FIELD_LENGTH}）"
                )
            if isinstance(fval, str) and "\x00" in fval:
                raise InvalidCheckpoint("findings 不得包含空字节")
        # C11（第三轮审计）：DOC-230 §3.8 第 2 项要求"写入期即校验"，但此前
        # `severity` 的 enum 只落在 MCP schema 层——核心摄入路径（含引擎 CLI
        # `supervisor checkpoint`）可写入任意字符串（实测 `severity='urgent'`
        # 落盘成功），把失败推迟到确认门，正是 §3.8 要消除的形态。此处把校验
        # 下沉到核心边界：归一（p0-p3 别名）后必须命中唯一词表，否则拒绝。
        # 注意：必须放在上面的字段循环**之后**——首次插入时误置于循环中间，
        # 把 NUL 字节检查吞进了本分支（已由 test_null_byte_in_a_field_is_rejected
        # 捕获并修复）。
        declared = finding.get("severity")
        if declared is not None:
            normalized_severity = normalize_finding_severity(declared)
            if normalized_severity not in finding_severity_levels():
                raise InvalidCheckpoint(
                    f"findings[{key}] severity 必须属于词表 "
                    f"{list(finding_severity_levels())}，收到: {declared!r}"
                )
        item = copy.deepcopy(dict(finding))
        if not (isinstance(item.get("fingerprint"), str) and item["fingerprint"].strip()):
            item["fingerprint"] = key
        item.setdefault("reproducible", False)
        item.setdefault("id", key)
        # 结构化复现三件套透传：非空字符串才保留（旧候选无 poc 字段时行为不变），
        # 使其随 checkpoint 完整落到 supervisor state 的候选上。
        _preserve_poc_carry_fields(item)
        if workspace_root is not None:
            item = enrich_gate_finding(item, workspace_root)
        if key not in normalized:
            normalized[key] = item
    if len(normalized) > MAX_FINDINGS_PER_CHECKPOINT:
        raise InvalidCheckpoint(
            f"findings 去重后仍超过 {MAX_FINDINGS_PER_CHECKPOINT} 条上限"
        )
    result = [normalized[key] for key in sorted(normalized)]
    if workspace_root is not None and result:
        try:
            from core.agent import agent_eval

            invalid = agent_eval.invalid_evidence_ids(result, Path(workspace_root))
        except (ImportError, OSError, TypeError, ValueError) as exc:
            raise InvalidCheckpoint(f"finding evidence 校验失败: {exc}") from exc
        if invalid:
            raise InvalidCheckpoint(
                "finding 缺真实 workspace evidence: " + ", ".join(invalid)
            )
    return result


def attach_evidence_receipts(
    findings: list[dict[str, object]],
    evidence: Mapping[str, Mapping[str, object]] | None,
) -> None:
    """F1'（Round7 审计条件一）：checkpoint 摄入侧 receipt 关联管道。

    worker findings schema 不携带 execution_receipt；同一 checkpoint 的
    per-unit evidence 项携带（且已经 :func:`validate_execution_receipt`
    与 Supervisor 持有的 claim/execution 记录绑定）。这里在摄入时把
    finding 能关联到的 receipt 附到 finding 上（``evidence_receipt``），
    使结算自动裁决器可以"查找匹配"引擎台账而不是文本信任 worker 自述：

    - 关联键：finding 的 ``case_id`` 或 ``unit`` 精确等于某个带 receipt 的
      evidence unit（worker runtime 约定 case_id 即 coverage unit id）；
    - 附着内容是已归一化 receipt 的拷贝（execution_id/lease_id/worker_id/
      artifact_path/artifact_sha256/command/exit_code/status）；
    - 关联不上的 finding 保持原样（不报错、不猜测）。
    """
    if not findings or not isinstance(evidence, Mapping) or not evidence:
        return
    receipt_by_unit: dict[str, Mapping[str, object]] = {}
    for unit, item in evidence.items():
        if not isinstance(item, Mapping):
            continue
        receipt = item.get("execution_receipt")
        if isinstance(receipt, Mapping) and receipt:
            receipt_by_unit[str(unit).strip()] = receipt
    if not receipt_by_unit:
        return
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if isinstance(finding.get("evidence_receipt"), Mapping) and finding["evidence_receipt"]:
            continue
        for key in ("case_id", "unit"):
            unit_key = str(finding.get(key) or "").strip()
            if unit_key and unit_key in receipt_by_unit:
                finding["evidence_receipt"] = dict(receipt_by_unit[unit_key])
                break


def normalize_notes(notes: Mapping[str, str] | None) -> dict[str, str]:
    """校验断点手记：unit_id → 非空文本（≤2000 字符）。"""
    if notes is None:
        return {}
    if not isinstance(notes, Mapping):
        raise InvalidCheckpoint("notes 必须是 unit_id -> 文本 对象")
    check_depth_and_keys(notes)
    if len(notes) > MAX_COVERAGE_PER_CHECKPOINT:
        raise InvalidCheckpoint(
            f"notes 单元过多（>{MAX_COVERAGE_PER_CHECKPOINT}）；超出单次提交上限"
        )
    result: dict[str, str] = {}
    for unit_id, note in notes.items():
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise InvalidCheckpoint("notes 的 unit_id 必须是非空字符串")
        if len(unit_id.strip()) > 1024:
            raise InvalidCheckpoint(f"notes unit_id 过长（>1024）: {unit_id[:64]}")
        if "\x00" in unit_id:
            raise InvalidCheckpoint("notes unit_id 不得包含空字节")
        if not isinstance(note, str) or not note.strip():
            raise InvalidCheckpoint(f"notes[{unit_id}] 必须是非空文本")
        if len(note) > MAX_UNIT_NOTE_LENGTH:
            raise InvalidCheckpoint(
                f"notes[{unit_id}] 超过 {MAX_UNIT_NOTE_LENGTH} 字符上限"
            )
        if "\x00" in note:
            raise InvalidCheckpoint("notes 不得包含空字节")
        result[unit_id] = note
    return result


def normalize_signatures(signatures: Iterable[str] | None) -> list[str]:
    """校验并去重错误签名；None/空列表返回空。"""
    if signatures is None:
        return []
    if isinstance(signatures, (str, bytes)) or not isinstance(signatures, Iterable):
        raise InvalidCheckpoint("signatures 必须是字符串数组")
    result: list[str] = []
    for signature in signatures:
        if not isinstance(signature, str) or not signature.strip():
            raise InvalidCheckpoint("signatures 成员必须是非空字符串")
        value = signature.strip()
        if len(value) > MAX_SIGNATURE_LENGTH:
            raise InvalidCheckpoint(
                f"签名超过 {MAX_SIGNATURE_LENGTH} 字符上限；请用错误摘要而非完整日志"
            )
        if "\x00" in value:
            raise InvalidCheckpoint("签名不得包含空字节")
        if value not in result:
            result.append(value)
    if len(result) > MAX_SIGNATURES_PER_CHECKPOINT:
        raise InvalidCheckpoint(
            f"单次 checkpoint 签名不得超过 {MAX_SIGNATURES_PER_CHECKPOINT} 条"
        )
    return result


def confirmation_gate(findings: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Evaluate the shared Finding confirmation protocol without waiting."""
    if not findings:
        return {
            "schema_version": 1, "total": 0, "confirmed": 0,
            "rejected": 0, "pending": 0, "ready": True,
        }
    try:
        from core.agent import agent_eval
        from core.ledger import finding_confirmation

        total_toks = sum(
            int(item.get("token_count", 0)) for item in findings
            if isinstance(item, dict) and isinstance(item.get("token_count"), (int, float))
        )
        payload = {
            "schema_version": 1, "duration_seconds": 0,
            "token_count": total_toks, "findings": [dict(item) for item in findings],
        }
        return finding_confirmation.gate(payload)
    except Exception as exc:
        # BX-ENGINE-18 / A6: an aggregation failure does not prove that there
        # are zero findings.  Keep the shape stable for consumers, but mark every
        # unavailable numeric metric as unknown instead of fabricating a count.
        return {
            "schema_version": 1, "total": None,
            "confirmed": None, "rejected": None, "pending": None,
            "ready": False, "degraded": True,
            "error": f"confirmation protocol invalid: {exc}",
            "states": {},
            # A6：降级时不得伪造数值（详见 finding_confirmation.gate 同处说明）。
            "assurance": {
                "average_score": None, "minimum_score": None,
                "fully_confirmed_rate": None, "blocking_reasons": {},
            },
            "findings": [],
        }


def refresh_confirmation_gate(state: RunState) -> dict[str, object]:
    state.confirmation_gate = confirmation_gate(state.findings)
    return state.confirmation_gate


def normalize_coverage(
    coverage: Mapping[str, str] | None,
) -> dict[str, str]:
    if coverage is None:
        return {}
    if not isinstance(coverage, Mapping):
        raise InvalidCheckpoint("coverage 必须是 unit -> status 对象")
    check_depth_and_keys(coverage)
    if len(coverage) > MAX_COVERAGE_PER_CHECKPOINT:
        raise InvalidCheckpoint(
            f"coverage 单元过多（>{MAX_COVERAGE_PER_CHECKPOINT}）；超出单次提交上限"
        )
    normalized: dict[str, str] = {}
    for unit, status in coverage.items():
        if not isinstance(unit, str) or not unit.strip():
            raise InvalidCheckpoint("coverage unit 不能为空")
        if len(unit.strip()) > 1024:
            raise InvalidCheckpoint(f"coverage unit 过长（>1024）: {unit[:64]}")
        if "\x00" in unit:
            raise InvalidCheckpoint("coverage unit 不得包含空字节")
        if status not in VALID_COVERAGE_STATES:
            raise InvalidCheckpoint(
                f"coverage[{unit}] 状态无效: {status}; "
                f"允许 {sorted(VALID_COVERAGE_STATES)}"
            )
        normalized[unit.strip()] = status
    return normalized


def session_coverage_complete(
    state: RunState,
    ledger_path: Path | None,
) -> bool:
    """Check the session ledger without mutating Supervisor state (P2-1)."""
    if ledger_path is None or not ledger_path.is_file():
        return False
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        records = data.get("records", [])
        covered_units = {
            record.get("unit_id")
            for record in records
            if isinstance(record, dict) and record.get("unit_id")
        }
        if not state.coverage:
            return False
        return all(
            status in TERMINAL_COVERED or unit in covered_units
            for unit, status in state.coverage.items()
        )
    except Exception:
        return False


def resolve_session_coverage_ledger_path(
    workspace_root: Path,
    configured_path: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Resolve the Session coverage ledger without owning Supervisor state."""
    if configured_path is not None:
        return Path(configured_path)
    try:
        import core.bugx_paths as bugx_paths

        scope = bugx_paths.require(workspace_root)
        return scope.polling_root / "coverage-ledger.json"
    except Exception:
        pollings_dir = workspace_root / ".bugx" / "pollings"
        if not pollings_dir.is_dir():
            return None
        candidates = list(pollings_dir.glob("*/coverage-ledger.json"))
        if not candidates:
            return None
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return candidates[0]


def normalize_evidence(
    evidence: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, dict[str, object]]:
    if evidence is None:
        return {}
    if not isinstance(evidence, Mapping):
        raise InvalidCheckpoint("evidence 必须是 unit -> object 对象")
    check_depth_and_keys(evidence)
    if len(evidence) > MAX_EVIDENCE_PER_CHECKPOINT:
        raise InvalidCheckpoint(
            f"evidence 单元过多（>{MAX_EVIDENCE_PER_CHECKPOINT}）；超出单次提交上限"
        )
    normalized: dict[str, dict[str, object]] = {}
    required = {
        "command", "exit_code", "artifact_path", "artifact_sha256",
        "target_fingerprint", "timestamp", "worker_id", "run_id",
        "round_no", "lease_id", "status",
    }
    for unit, item in evidence.items():
        if not isinstance(unit, str) or not unit.strip():
            raise InvalidCheckpoint("evidence unit 不能为空")
        if len(unit.strip()) > 1024:
            raise InvalidCheckpoint(f"evidence unit 过长（>1024）: {unit[:64]}")
        if "\x00" in unit:
            raise InvalidCheckpoint("evidence unit 不得包含空字节")
        if not isinstance(item, Mapping):
            raise InvalidCheckpoint(f"evidence[{unit}] 必须是对象")
        check_depth_and_keys(item)
        missing = required - set(item)
        if missing:
            raise InvalidCheckpoint(
                f"evidence[{unit}] 缺字段: {sorted(missing)}"
            )
        command = item["command"]
        exit_code = item["exit_code"]
        artifact_path = item["artifact_path"]
        digest = item["artifact_sha256"]
        target = item["target_fingerprint"]
        timestamp = item["timestamp"]
        worker_id = item["worker_id"]
        run_id = item["run_id"]
        round_no = item["round_no"]
        lease_id = item["lease_id"]
        status = item["status"]
        if not isinstance(command, str) or not command.strip():
            raise InvalidCheckpoint(f"evidence[{unit}].command 无效")
        if len(command) > MAX_COMMAND_LENGTH:
            raise InvalidCheckpoint(
                f"evidence[{unit}].command 过长（>{MAX_COMMAND_LENGTH}）"
            )
        if "\x00" in command:
            raise InvalidCheckpoint(f"evidence[{unit}].command 不得包含空字节")
        if not isinstance(artifact_path, str) or not artifact_path.strip():
            raise InvalidCheckpoint(f"evidence[{unit}].artifact_path 无效")
        if len(artifact_path) > MAX_ARTIFACT_PATH_LENGTH:
            raise InvalidCheckpoint(
                f"evidence[{unit}].artifact_path 过长（>{MAX_ARTIFACT_PATH_LENGTH}）"
            )
        if "\x00" in artifact_path:
            raise InvalidCheckpoint(f"evidence[{unit}].artifact_path 不得包含空字节")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise InvalidCheckpoint(f"evidence[{unit}].exit_code 无效")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise InvalidCheckpoint(f"evidence[{unit}].artifact_sha256 无效")
        if not isinstance(target, str) or not target.strip():
            raise InvalidCheckpoint(
                f"evidence[{unit}].target_fingerprint 无效"
            )
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) \
                or not math.isfinite(float(timestamp)) or timestamp <= 0:
            raise InvalidCheckpoint(f"evidence[{unit}].timestamp 无效")
        timestamp = float(timestamp)
        if timestamp >= 1_000_000_000_000:
            timestamp /= 1000
        try:
            datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise InvalidCheckpoint(
                f"evidence[{unit}].timestamp 超出可表示范围"
            ) from exc
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise InvalidCheckpoint(f"evidence[{unit}].worker_id 无效")
        if not isinstance(run_id, str) or not run_id.strip():
            raise InvalidCheckpoint(f"evidence[{unit}].run_id 无效")
        if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 1:
            raise InvalidCheckpoint(f"evidence[{unit}].round_no 无效")
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise InvalidCheckpoint(f"evidence[{unit}].lease_id 无效")
        if status not in VALID_EVIDENCE_STATES:
            raise InvalidCheckpoint(
                f"evidence[{unit}].status 无效；允许 {sorted(VALID_EVIDENCE_STATES)}"
            )
        if status == "passed" and exit_code != 0:
            raise InvalidCheckpoint(f"evidence[{unit}] passed 时 exit_code 必须为 0")
        normalized_item = copy.deepcopy(dict(item))
        normalized_item["timestamp"] = timestamp
        if "execution_receipt" in item:
            normalized_item["execution_receipt"] = normalize_execution_receipt(
                item["execution_receipt"], unit.strip(),
            )
        normalized[unit.strip()] = normalized_item
    return normalized


def assert_artifact_not_before_claim(
    *,
    artifact: Path,
    claimed_at: float,
    unit: str,
    channel: str,
    grace_seconds: float = 5.0,
) -> None:
    """拒绝早于本 claim 领取时刻写入的工件（跨纪元证据）。

    worker 通道与 salvage 通道共用本判据，落实 `L3-C2` 要求的「代交通道与
    worker 通道同强度」：`artifact_root` 可被 supersede/recover 继承，两通道
    都只信任本 claim 领取时刻之后写入的工件。

    边界：``mtime + grace_seconds < claimed_at`` 拒绝；
    ``mtime + grace_seconds == claimed_at`` 放行（与原 salvage 内联实现一致）。

    残留弱点（本轮不解决）：``st_mtime`` 可被 ``os.utime`` 任意改写，本函数
    只消除两条通道的不对称，**不提供防伪造能力**；不可伪造锚（内容寻址快照）
    由缺陷 B6 在后续 Wave 承接。
    """
    try:
        mtime = artifact.stat().st_mtime
    except OSError as exc:
        raise InvalidCheckpoint(
            f"{channel} evidence 工件不可读: {unit}: {artifact}: {exc}"
        ) from exc
    if mtime + grace_seconds < claimed_at:
        raise InvalidCheckpoint(
            f"{channel} evidence 工件早于本 claim 领取时刻（跨纪元证据）: "
            f"{unit}: {artifact}"
        )


def validate_worker_artifacts(
    *,
    workspace_root: Path,
    artifact_root: str,
    evidence: Mapping[str, Mapping[str, object]],
    findings: Iterable[Mapping[str, object]],
    claimed_at: float,
    worker_id: str = "",
    lease_id: str = "",
    execution_id: str = "",
    trusted_execution: bool = False,
) -> None:
    """Validate worker-owned checkpoint artifacts before state mutation."""
    owners: dict[Path, str] = {}
    for unit, item in evidence.items():
        validate_execution_receipt(
            item,
            unit=unit,
            worker_id=worker_id or str(item["worker_id"]),
            lease_id=lease_id or str(item["lease_id"]),
            execution_id=execution_id,
            trusted_execution=trusted_execution,
        )
        try:
            artifact = confined_worker_artifact(
                workspace_root, artifact_root, str(item["artifact_path"]),
            )
            if artifact.suffix.lower() in _SOURCE_ARTIFACT_SUFFIXES:
                raise InvalidCheckpoint(
                    f"evidence[{unit}].artifact_path 必须是执行输出，"
                    "不能直接引用源码脚本"
                )
            previous_unit = owners.get(artifact)
            if previous_unit is not None:
                raise InvalidCheckpoint(
                    f"evidence[{unit}].artifact_path 与 {previous_unit} 复用同一 artifact；"
                    "每个 coverage unit 必须使用独立产物"
                )
            owners[artifact] = unit
            stat_result = artifact.stat()
            if stat_result.st_size > MAX_ARTIFACT_BYTES:
                raise InvalidCheckpoint(
                    f"evidence[{unit}] artifact 超过 {MAX_ARTIFACT_BYTES} 字节上限"
                )
            assert_artifact_not_before_claim(
                artifact=artifact,
                claimed_at=claimed_at,
                unit=unit,
                channel="worker",
            )
            digest = hashlib.sha256()
            with artifact.open("rb") as artifact_fh:
                while True:
                    chunk = artifact_fh.read(ARTIFACT_HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
            actual = digest.hexdigest()
        except InvalidCheckpoint:
            raise
        except (OSError, WorkspacePathError) as exc:
            raise InvalidCheckpoint(
                f"evidence[{unit}] artifact 无法读取: {exc}"
            ) from exc
        if actual.lower() != str(item["artifact_sha256"]).lower():
            raise InvalidCheckpoint(
                f"evidence[{unit}].artifact_sha256 与 artifact 内容不匹配"
            )
        # Worker Runtime artifacts carry a copy of the claim fields.  Check
        # that copy when present so the hashed artifact and submitted receipt
        # cannot silently describe different work.  The digest is intentionally
        # blank in the embedded copy because hashing a file that contains its
        # own final hash is circular; the outer artifact hash remains the
        # authoritative integrity check.
        if item.get("execution_receipt") is not None:
            try:
                artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                artifact_payload = None
            if isinstance(artifact_payload, Mapping) and artifact_payload.get(
                "artifact_type"
            ) == "bugx-worker-execution/v1":
                embedded = artifact_payload.get("execution_receipt")
                submitted = item["execution_receipt"]
                if not isinstance(embedded, Mapping):
                    raise InvalidCheckpoint(
                        f"evidence[{unit}] Worker Runtime artifact 缺少 receipt"
                    )
                for field in _EXECUTION_RECEIPT_FIELDS - {"artifact_sha256"}:
                    if embedded.get(field) != submitted[field]:
                        raise InvalidCheckpoint(
                            f"evidence[{unit}] artifact receipt.{field} 与提交 receipt 不一致"
                        )
                if embedded.get("artifact_sha256") not in {
                    "", submitted["artifact_sha256"],
                }:
                    raise InvalidCheckpoint(
                        f"evidence[{unit}] artifact receipt.artifact_sha256 无效"
                    )
                if item.get("status") == "passed":
                    raise InvalidCheckpoint(
                        f"evidence[{unit}] {UNVERIFIED_EXECUTION}: "
                        "Worker Runtime receipt 不是外部命令执行证明"
                    )

    for finding in findings:
        bundle = finding.get("evidence_bundle")
        if not isinstance(bundle, Mapping):
            continue
        for artifact_item in bundle.get("artifacts", []):
            if not isinstance(artifact_item, Mapping):
                raise InvalidCheckpoint("evidence_bundle.artifact 必须是对象")
            try:
                bundle_artifact = confined_worker_artifact(
                    workspace_root, artifact_root, str(artifact_item["path"]),
                )
                if not bundle_artifact.is_file():
                    raise InvalidCheckpoint("evidence_bundle artifact 必须是文件")
                if bundle_artifact.stat().st_size > MAX_ARTIFACT_BYTES:
                    raise InvalidCheckpoint("evidence_bundle artifact 超过大小上限")
                digest = hashlib.sha256()
                with bundle_artifact.open("rb") as bundle_fh:
                    for chunk in iter(
                        lambda: bundle_fh.read(ARTIFACT_HASH_CHUNK_BYTES), b"",
                    ):
                        digest.update(chunk)
            except InvalidCheckpoint:
                raise
            except (KeyError, OSError, WorkspacePathError) as exc:
                raise InvalidCheckpoint(
                    f"evidence_bundle artifact 不可用: {exc}"
                ) from exc
            if digest.hexdigest().lower() != str(artifact_item["sha256"]).lower():
                raise InvalidCheckpoint("evidence_bundle artifact sha256 不匹配")


def coverage_evidence_path(workspace_root: Path, state_path: Path) -> Path | None:
    """Return the current Life evidence store when a project manifest exists."""
    manifest = default_coverage_manifest(workspace_root)
    if not manifest.is_file() or manifest.is_symlink():
        return None
    target = state_path.with_name("coverage-evidence.json")
    if target.is_symlink():
        raise WorkspacePathError(f"拒绝使用符号链接覆盖证据清单: {target}")
    return target


def assert_target_fingerprint(
    workspace_root: Path,
    expected_fingerprint: str | None,
    state: RunState,
    now: float,
) -> None:
    """Reject work against a changed source/coverage target.

    BX-R4-02：drift 判定比较 `target_identity`（git tree + manifest
    units，跨 worktree 稳定），而非指纹字符串相等。v1 `workspace:<sha>`
    （含绝对路径）与 v2（路径无关）即使字符串不同，只要目标身份一致
    就放行——同 HEAD 多 worktree 不再误杀；源码/计划真变（身份不同）
    才拒绝。拒绝时给出 rebind 迁移指引而非死胡同。
    """
    invalidate_git_head_cache(workspace_root)
    current = _workspace_fingerprint(workspace_root)
    current_identity = _target_identity(workspace_root)
    expected = state.target_fingerprint or expected_fingerprint
    if state.target_fingerprint is None:
        state.target_fingerprint = current
        append_event(
            state, "target_fingerprint_bound", now,
            target_fingerprint=current,
            target_identity=current_identity,
        )
        return
    if current == expected:
        return
    # v1 指纹含绝对路径，不可逆；但"同目标换路径"有一个可验证的必要
    # 条件：state 绑定的旧身份 == 当前工作区的目标身份。旧身份取自
    # state 历史中最近一次 target_fingerprint_bound / target_rebound /
    # target_forward_rolled 事件记录的 target_identity。老版本 state 没有
    # 该事件时，先以当前 canonical identity 重锚并写入前滚事件，后续调用
    # 即恢复严格的历史身份比较。
    bound_identity: str | None = None
    for event in reversed(state.history):
        if not isinstance(event, dict):
            continue
        if event.get("type") in (
            "target_fingerprint_bound",
            "target_rebound",
            "target_forward_rolled",
        ):
            candidate = event.get("target_identity")
            if isinstance(candidate, str) and candidate:
                bound_identity = candidate
                break
    legacy_identity_fallback = bound_identity is None and any(
        isinstance(event, dict)
        and event.get("type") == "run_started"
        and event.get("target_fingerprint") == expected
        for event in state.history
    )
    if legacy_identity_fallback:
        # Legacy states predate the bound-identity event.  Re-anchor them to
        # the current canonical target identity once, so the next persisted
        # forward-roll event restores the strict historical comparison.
        bound_identity = current_identity
    if bound_identity == current_identity:
        # 同源码树 + 同覆盖单元，仅工作区路径不同 → 前滚指纹，放行。
        append_event(
            state, "target_forward_rolled", now,
            previous_target_fingerprint=expected,
            target_fingerprint=current,
            target_identity=current_identity,
            reason=(
                "legacy state re-anchored to current target identity"
                if legacy_identity_fallback
                else "same source tree and coverage units; workspace path only"
            ),
        )
        state.target_fingerprint = current
        return
    append_event(
        state, "target_drift_rejected", now,
        expected_target_fingerprint=expected,
        actual_target_fingerprint=current,
        actual_target_identity=current_identity,
        bound_target_identity=bound_identity,
    )
    raise InvalidCheckpoint(
        "审计目标已变化；请重新生成当前覆盖计划后再继续"
        "（若仅切换 worktree 且源码/计划未变，请执行 rebind_target 迁移指纹）"
    )


def publish_checkpoint_evidence(
    workspace_root: Path,
    state_path: Path,
    evidence: Mapping[str, Mapping[str, object]],
) -> Callable[[], None] | None:
    """Prepare and publish evidence, returning a best-effort rollback hook.

    The staged manifest makes a batch all-or-nothing at the evidence-file level.
    The returned hook closes the remaining cross-file gap when run-state
    persistence fails; this is a recovery boundary, not cross-file atomicity.
    """
    if not evidence:
        return None
    target = coverage_evidence_path(workspace_root, state_path)
    if target is None:
        return None
    try:
        from core.coverage import coverage_evidence

        lock_path = target.with_name(target.name + ".lock")
        with coverage_evidence._exclusive_lock(lock_path):
            _reconcile_staged_evidence(target)
            if target.is_symlink():
                raise WorkspacePathError(f"拒绝使用符号链接覆盖证据清单: {target}")
            original_exists = target.exists()
            if original_exists and not target.is_file():
                raise WorkspacePathError(f"覆盖证据清单不是普通文件: {target}")
            original_payload = target.read_bytes() if original_exists else None

            descriptor, staged_name = tempfile.mkstemp(
                prefix=(f".{target.name}.stage-{os.getpid()}-{time.time_ns()}-"),
                suffix=".tmp", dir=str(target.parent),
            )
            os.close(descriptor)
            staged = Path(staged_name)
            staged.unlink()
            staged_lock = staged.with_name(staged.name + ".lock")
            try:
                if original_payload is not None:
                    atomic_write_bytes(staged, original_payload)
                store = coverage_evidence.EvidenceStore(
                    staged, workspace_root=workspace_root,
                )
                for unit_id, item in evidence.items():
                    artifact = confined_regular_file(
                        workspace_root, str(item["artifact_path"]),
                    )
                    observed_at = datetime.fromtimestamp(
                        float(item["timestamp"]), tz=timezone.utc,
                    ).isoformat()
                    record = {
                        "unit_id": unit_id,
                        "command": item["command"],
                        "exit_code": item["exit_code"],
                        "artifact_path": artifact.relative_to(
                            workspace_root,
                        ).as_posix(),
                        "artifact_sha256": item["artifact_sha256"],
                        "target_fingerprint": item["target_fingerprint"],
                        "observed_at": observed_at,
                        "worker_id": item["worker_id"],
                        "status": item["status"],
                    }
                    if item.get("target_drifted") is True:
                        record["target_drifted"] = True
                    if item.get("reproduced_crash") is True:
                        record["reproduced_crash"] = True
                    if item.get("evidence_type") is not None:
                        record["evidence_type"] = item["evidence_type"]
                    receipt = item.get("execution_receipt")
                    if isinstance(receipt, Mapping):
                        record["execution_id"] = receipt["execution_id"]
                        record["lease_id"] = receipt["lease_id"]
                        record["execution_receipt_sha256"] = execution_receipt_sha256(
                            receipt
                        )
                    # Supervisor has already validated this checkpoint as a
                    # re-verification boundary; prepare every record off-target.
                    # BX-ENGINE-21：复验路径显式允许跨 target 替换——target 重绑后
                    # 旧记录的目标已不是当前目标，若不允许则同一 unit 永久不可提交。
                    result = store.store(
                        record, allow_supersede=True, allow_target_rebind=True,
                    )
                    if result.status == "conflict":
                        reason = f": {result.reason}" if getattr(result, "reason", "") else ""
                        raise InvalidCheckpoint(
                            f"coverage evidence[{unit_id}] 与已有证据冲突，拒绝覆盖{reason}"
                        )
                if staged_lock.exists():
                    staged_lock.unlink()
                if target.is_symlink():
                    raise WorkspacePathError(
                        f"拒绝使用符号链接覆盖证据清单: {target}"
                    )
                os.replace(staged, target)
            finally:
                for temporary in (staged, staged_lock):
                    if temporary.is_symlink():
                        raise WorkspacePathError(
                            f"拒绝清理符号链接临时证据: {temporary}"
                        )
                    if temporary.exists():
                        temporary.unlink()

        def rollback() -> None:
            with coverage_evidence._exclusive_lock(lock_path):
                if target.is_symlink():
                    raise WorkspacePathError(
                        f"证据回滚遇到符号链接，拒绝继续: {target}"
                    )
                if original_payload is None:
                    if target.exists():
                        if not target.is_file():
                            raise WorkspacePathError(
                                f"证据回滚目标不是普通文件: {target}"
                            )
                        target.unlink()
                else:
                    atomic_write_bytes(target, original_payload)

        return rollback
    except InvalidCheckpoint:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise InvalidCheckpoint(
            f"无法发布 checkpoint 覆盖证据: {exc}"
        ) from exc
