#!/usr/bin/env python3
"""launch_phases/finding_settlement.py - 发现项门禁校验与自动计命结算 (DOC-069)。"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import ctypes
import errno
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from core.launch_phases.common import (
    VERIFY,
    LIFECYCLE_ROOT_WARNED,
    child_environment,
    resolve_project_root,
    supervisor_state_path,
    uses_project_layout,
)
from core.launch_phases.session_runtime import baseline_json, load_runtime


def _settlement_supervisor(root: Path):
    """Resolve the active Supervisor without creating a new run."""
    try:
        from core.supervisor import audit_supervisor
        import core.bugx_paths as bugx_paths
        scope = bugx_paths.require(root)
        return audit_supervisor.Supervisor(root, state_path=scope.supervisor)
    except (ImportError, OSError, RuntimeError, ValueError):
        return None

from core.launch_phases.findings_snapshot import (
    CREDITED_CAP_DEFAULT,
    CREDITED_CAP_ENV,
    CREDITED_CAP_MAX,
    CREDITED_CAP_MIN,
    FindingsSnapshot,
    FindingsSnapshotError,
    authoritative_round,
    baseline_run_id,
    capture_findings_snapshot,
    credited_cap,
    deep_freeze,
    deep_thaw,
    expected_findings_binding,
    findings_artifact_paths,
    findings_payload_path,
    text_line_count,
    validate_findings_payload_snapshot,
    verify_findings_binding,
)

# Compatibility aliases
_FINDINGS_SEAL_EVENT_TYPES = (
    "finding_decisions_synced", "findings_report_sealed",
)
_deep_freeze = deep_freeze
_deep_thaw = deep_thaw
_baseline_run_id = baseline_run_id
_text_line_count = text_line_count
_findings_artifact_paths = findings_artifact_paths
_authoritative_round = authoritative_round
_expected_findings_binding = expected_findings_binding
_validate_findings_payload_snapshot = validate_findings_payload_snapshot

def _warn_once_stderr(message: str, key: str) -> None:
    """stderr 一次性警告；绝不写 stdout，避免污染宿主协议输出。"""
    if key in LIFECYCLE_ROOT_WARNED:
        return
    LIFECYCLE_ROOT_WARNED.add(key)
    try:
        print(f"[launch_bugx] ⚠ {message}", file=sys.stderr)
    except OSError:
        pass


def lifecycle_root_for(runtime: dict) -> Path:
    """解析当前生命周期目录：polling_id 优先，session_id 次之。"""
    # 布局归一：项目布局的账本在 ``<root>/.bugx/pollings``；无布局的
    # 自包含引擎副本按 ``_path_values_for_root`` 把运行态放在项目根，
    # 账本因此位于 ``<root>/<polling_id>/lifecycles``。旧实现无条件拼
    # ``<root>/.bugx/pollings``，在副本布局下永远找不到目录，落到
    # ``polling-unknown`` 并让 Finding 快照校验 fail-closed。
    from core.launch_phases import common as _common

    # 布局形状按**磁盘事实**判定：本模块的 ``uses_project_layout`` 是可被
    # 调用方/测试替换的全局，而路径形状不能跟着被替换的判定漂移——夹具在
    # ``root/.bugx/pollings`` 下写入报告却把布局判定打桩为 False 时，
    # 旧写法会去 ``root/pollings`` 找，所有已达成的 Finding 都被判为"无"。
    pollings_root = _common.pollings_root(resolve_project_root())
    lifecycle_id = str(runtime.get("lifecycle_id") or "unknown")
    candidates: list[str] = []
    for key in ("polling_id", "session_id"):
        value = runtime.get(key)
        if isinstance(value, str) and value and value.startswith("polling-") \
                and value not in candidates:
            candidates.append(value)
    for candidate in candidates:
        root = pollings_root / candidate / "lifecycles" / lifecycle_id
        if root.is_dir():
            return root
        _warn_once_stderr(
            f"候选生命周期目录不存在，跳过 {candidate}: {root}",
            f"missing:{root}",
        )
    # DOC-195: 移除猜测性 polling-000001 目录回退，收敛为 fail-closed 严格路径
    preferred = candidates[0] if candidates else "polling-unknown"
    root = pollings_root / preferred / "lifecycles" / lifecycle_id
    _warn_once_stderr(
        f"生命周期目录不存在: {root}",
        f"nocandidate:{root}",
    )
    return root


def settle_findings_path(runtime: dict) -> Path | None:
    """结算读取当前生命周期；非正式测试布局才允许扁平路径。

    stale 检测（2026-11-09 E1，opencode 复盘）：文件存在但 binding 的
    run_id 属于**已终结的旧 run**（且 lifecycle 也不同）→ 视为 stale 返回
    None，让上游走 auto_build_findings_json 自动重建。返回旧路径只会在
    快照校验层炸 findings_binding_mismatch（跨 6 次 post 重试 6.5 分钟的
    实测主因）。同 lifecycle/run_id 的文件不判 stale（真损坏走快照校验）。
    """
    lifecycle_findings = _lifecycle_findings_path(runtime)
    if lifecycle_findings.is_file():
        if _findings_binding_is_stale(lifecycle_findings, runtime):
            return None
        return lifecycle_findings
    if not uses_project_layout():
        candidate = Path(".bugx/findings.json")
        if candidate.is_file() and not candidate.is_symlink():
            if _findings_binding_is_stale(candidate, runtime):
                return None
            return candidate
    return None


def _findings_binding_is_stale(path: Path, runtime: dict) -> bool:
    """binding 的 run_id 与当前 runtime 不同且 lifecycle 也不同 → stale。"""
    import json as _json

    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    binding = payload.get("run_binding")
    if not isinstance(binding, Mapping):
        return False
    old_run = str(binding.get("run_id") or "")
    old_lifecycle = str(binding.get("lifecycle_id") or "")
    current_run = str(runtime.get("run_id") or "")
    current_lifecycle = str(runtime.get("lifecycle_id") or "")
    return bool(
        old_run and current_run and old_run != current_run
        and old_lifecycle and current_lifecycle and old_lifecycle != current_lifecycle
    )


def _lifecycle_findings_path(runtime: dict) -> Path:
    return lifecycle_root_for(runtime) / "findings.json"


def archive_stale_findings_binding(runtime: dict) -> str | None:
    """findings.json 绑定指向已终结的旧 run 时归档之；返回人读原因或 None。

    实测死锁（2026-11-09）：auto 续轮换 run_id 后，上一 Life 的 findings.json
    留在原路径，post 的快照校验要求 binding 与当前 run 一致 → 整轮拒绝结算，
    且报错只有码没有原因，用户只能手动改名放行。语义上旧 findings 是旧 Life
    的合法产物：归档（原子改名）到本 lifecycle 的 snapshots/ 目录，当前 Life
    由 agent 重新产出 findings。当前会话真损坏（同 run_id）不受影响。

    判据从严：run_id **且** lifecycle_id 都与当前不同才归档；run_id 相同视为
    当前会话数据（损坏与否交由快照校验正常报错）。
    """
    import json as _json

    path = _lifecycle_findings_path(runtime)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    binding = payload.get("run_binding")
    if not isinstance(binding, Mapping):
        return None
    old_run = str(binding.get("run_id") or "")
    old_lifecycle = str(binding.get("lifecycle_id") or "")
    current_run = str(runtime.get("run_id") or "")
    current_lifecycle = str(runtime.get("lifecycle_id") or "")
    if not old_run or old_run == current_run:
        return None
    if old_lifecycle and current_lifecycle and old_lifecycle == current_lifecycle:
        return None  # 同 lifecycle：run_id 漂移交由快照校验报错，不静默归档
    target_dir = lifecycle_root_for(runtime) / "snapshots"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"findings-archived-{old_run[:12]}.json"
        if target.exists():
            return None  # 已归档过，避免覆盖；交由快照校验正常报错
        os.replace(path, target)
        return (
            f"已归档遗留 findings（旧 run_id={old_run[:12]}… → {target.name}）；"
            "当前 Life 需重新产出 findings.json 后再 post"
        )
    except OSError:
        return None




def _supervisor_state_candidates(runtime: dict) -> list[Path]:
    """supervisor state 快照的候选路径（按可信度排序，取最新登记者胜出）。"""
    lifecycle_root = lifecycle_root_for(runtime)
    return (
        lifecycle_root / "state" / "supervisor.json",
        resolve_project_root() / ".bugx" / "state" / "supervisor.json",
        lifecycle_root / "supervisor.json",
    )


def _registered_findings_report_sha256(runtime: dict) -> str | None:
    """V2：supervisor state 中登记的当前轮 findings.json 内容哈希。

    扫描 state.history 中带 report_sha256 的 seal 类事件，按生命周期与
    Round 过滤（跨轮/跨生命周期的登记视为重放噪声，不采纳），取 seq 最新
    的一条。无登记（旧轮 state）返回 None，由调用方跳过比对。
    """
    runtime_lifecycle = str(runtime.get("lifecycle_id") or "")
    runtime_round = _authoritative_round(runtime)
    best: tuple[int, str] | None = None
    for state_path in _supervisor_state_candidates(runtime):
        payload = _read_json_object(state_path)
        if not payload:
            continue
        history = payload.get("history")
        if not isinstance(history, list):
            continue
        for event in history:
            if not isinstance(event, dict):
                continue
            if event.get("type") not in _FINDINGS_SEAL_EVENT_TYPES:
                continue
            digest = event.get("report_sha256")
            if not isinstance(digest, str) or len(digest) != 64:
                continue
            try:
                int(digest, 16)
            except ValueError:
                continue
            event_lifecycle = event.get("lifecycle_id")
            if (
                runtime_lifecycle
                and isinstance(event_lifecycle, str) and event_lifecycle
                and event_lifecycle != runtime_lifecycle
            ):
                continue
            event_round = event.get("round_no")
            if (
                isinstance(runtime_round, int) and not isinstance(runtime_round, bool)
                and isinstance(event_round, int) and not isinstance(event_round, bool)
                and event_round != runtime_round
            ):
                continue
            seq = event.get("seq")
            order = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
            if best is None or order >= best[0]:
                best = (order, digest.lower())
    return best[1] if best else None


def _verify_findings_report_seal(
    runtime: dict, report_bytes: bytes,
) -> tuple[bool, str]:
    """V2 门禁：findings.json 在 sync 成功后不得被改写。

    sync（apply_decision_sync）成功时会把报告原始字节的内容哈希登记进
    supervisor state（hash-chain 审计事件）。post 结算读取登记哈希与当前
    文件实际哈希比对——不一致说明 sync 之后报告被改过，结算 fail-closed。
    返回 ``(ok, note)``；无登记（旧轮 state）返回 ``(True, "seal=unregistered")``
    并输出一次性提示，不阻断（兼容路径）。
    """
    registered = _registered_findings_report_sha256(runtime)
    if registered is None:
        _warn_once_stderr(
            "supervisor state 无 findings sync 内容哈希登记（旧轮 state），"
            "跳过 sync 后篡改比对",
            "findings-seal-unregistered",
        )
        return True, "seal=unregistered"
    actual = hashlib.sha256(report_bytes).hexdigest()
    if actual != registered:
        return False, (
            f"登记 sha256={registered}，实际 sha256={actual}；"
            "findings 在 sync 后被修改，请重新执行 findings sync 后再结算"
        )
    return True, "seal=ok"


def gate_confirmed_findings(
    runtime: dict, snapshot: FindingsSnapshot | None = None, *,
    snapshot_captured: bool = False,
    allow_incomplete: bool = False,
) -> tuple[int | None, list[str], str]:
    """外部门禁裁定的计命数：只认 finding_confirmation gate 终态 confirmed。"""
    try:
        if snapshot is None and not snapshot_captured:
            snapshot = capture_findings_snapshot(runtime)
    except FindingsSnapshotError as exc:
        return None, [], exc.reason_code
    if snapshot is None:
        return 0, [], "no-findings"
    # V2：sync 与 post 之间报告内容完整性绑定——文件哈希必须与 sync 登记
    # 一致，否则 sync 后的改写会无声覆盖宿主已复核的决策。
    seal_ok, seal_note = _verify_findings_report_seal(runtime, snapshot.raw_bytes)
    if not seal_ok:
        return None, [], f"findings 内容完整性校验失败（{seal_note}）"
    try:
        from core.ledger import finding_confirmation
    except ImportError:
        return None, [], "confirmation_module_unavailable"
    # The snapshot is deeply immutable. The adjudicator intentionally mutates
    # its working view, so it must receive a fully detached mutable tree.
    payload = _deep_thaw(snapshot.payload)
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return None, [], "findings_validation_failed"
    # DOC-188: 程序级自动裁决未终态候选（确定性优先）
    _adjudicate_candidate_findings(
        findings, resolve_project_root(),
        run_id=runtime.get("run_id") or _baseline_run_id(),
    )
    
    report = finding_confirmation.gate(payload)
    if not report.get("ready"):
        pending = report.get("pending", "?")
        # 收集未终态候选的详细信息以供宿主 Agent 智能升阶
        pending_ids = [
            str(f.get("id"))
            for f in findings if not finding_confirmation.is_confirmed(f) and f.get("confirmation", {}).get("state") != "rejected"
        ]
        escalation_note = (
            f"findings 存在 {pending} 个未终态候选（{', '.join(pending_ids[:5])}），计命结算被拒："
            "请由宿主 Agent 基于 failed_findings 进行定点裁决后重跑 post。"
        )
        if not allow_incomplete:
            return None, [], escalation_note
        print(f"[finding-settlement] ℹ --allow-incomplete: {escalation_note}（按 INCOMPLETE 封存，未终态项不计命）")
    lines: list[str] = []
    artifact_rejections: list[str] = []
    for finding in findings:
        if not finding_confirmation.is_confirmed(finding):
            continue
        conf = finding.get("confirmation")
        # F1' V4 硬门 grandfather：confirmations 分两代。
        #   - 新代际（confirmation.adjudicator_build 标记，本 build 的程序级
        #     裁决产出）：全部 failure 观测必须绑定工件哈希（require_artifacts
        #     让"未声明"也变成错误），缺/错任一环 → 该条回退 candidate 留存
        #     （内存降级 + 打印原因），不计命，不是静默丢弃；
        #   - 存量（无标记，部署前已封存的 confirmed，如 R7 的 4 条）：维持
        #     "缺省不罚"豁免，不受追溯——只校验"已声明"的哈希是否属实。
        new_generation = (
            isinstance(conf, dict) and bool(conf.get("adjudicator_build"))
        )
        artifact_errors = _snapshot_observation_artifact_errors(
            finding, snapshot, require_artifacts=new_generation,
        )
        if artifact_errors:
            finding_id = str(
                finding.get("id") or finding.get("fingerprint") or "unknown",
            )
            if new_generation and isinstance(conf, dict):
                # 回退 candidate 留存：findings.json 已被 sync 封章（V2），
                # 不能改写文件；这里在结算视图中降级并记录原因，下一轮
                # 宿主补齐真实工件后可重新走裁决。
                conf["state"] = "candidate"
                conf["decision_reason"] = (
                    "V4 新代际工件硬门：确认回退候选 —— "
                    + "；".join(artifact_errors)
                )
            artifact_rejections.append(
                f"{finding_id}: " + "；".join(artifact_errors),
            )
            continue
        lines.append(derive_settle_line(
            finding, evidence_snapshot=snapshot.artifact_snapshot, root=snapshot.root,
        ))
    confirmed = len(lines)
    credited = min(confirmed, credited_cap())
    for rejection in artifact_rejections:
        print(f"[finding-settlement] ✗ confirmed 工件复核失败，本条不计命: {rejection}")
    note = f"confirmed={confirmed}"
    if artifact_rejections:
        rejected_ids = ", ".join(
            rejection.split(":", 1)[0] for rejection in artifact_rejections
        )
        note += f", artifact_rejected={len(artifact_rejections)}({rejected_ids})"
    if seal_note != "seal=ok":
        note += f", {seal_note}"
    return credited, lines, note


def _snapshot_observation_artifact_errors(
    finding: Mapping[str, Any], snapshot: FindingsSnapshot, *, require_artifacts: bool,
) -> list[str]:
    """Check declared confirmation artifact references without reopening files."""
    confirmation = finding.get("confirmation")
    if not isinstance(confirmation, Mapping):
        return ["confirmation 缺失"]
    observations = confirmation.get("observations")
    if not isinstance(observations, list):
        return ["observations 无效"]
    from core.ledger.finding_io import snapshot_relative_path

    errors: list[str] = []
    for index, item in enumerate(observations, 1):
        if not isinstance(item, Mapping):
            errors.append(f"observation #{index} 无效")
            continue
        artifact_path = item.get("artifact_path")
        artifact_hash = item.get("artifact_sha256")
        required = require_artifacts and item.get("outcome") == "failure"
        if not isinstance(artifact_path, str) or not artifact_path.strip():
            if required:
                errors.append(f"observation #{index} 新代际 confirmed 缺工件哈希绑定")
            continue
        try:
            normalized = snapshot_relative_path(artifact_path, snapshot.root)
        except Exception:
            errors.append(f"observation #{index} artifact_path 无效")
            continue
        metadata = snapshot.artifact_snapshot.get(normalized)
        if not isinstance(metadata, Mapping):
            errors.append(f"observation #{index} artifact 快照缺失")
            continue
        if isinstance(artifact_hash, str) and metadata.get("sha256") == artifact_hash.lower():
            continue
        if required or artifact_hash is not None:
            errors.append(f"observation #{index} artifact hash 不匹配")
    negative = confirmation.get("negative_control")
    if isinstance(negative, Mapping) and negative.get("artifact_path") is not None:
        try:
            normalized = snapshot_relative_path(negative.get("artifact_path"), snapshot.root)
        except Exception:
            errors.append("negative_control artifact_path 无效")
        else:
            metadata = snapshot.artifact_snapshot.get(normalized)
            artifact_hash = negative.get("artifact_sha256")
            if not isinstance(metadata, Mapping):
                errors.append("negative_control artifact 快照缺失")
            elif artifact_hash is not None and (
                not isinstance(artifact_hash, str)
                or metadata.get("sha256") != artifact_hash.lower()
            ):
                errors.append("negative_control artifact hash 不匹配")
    return errors



# F1'：裁决器版本标记来源。core/metadata.txt 的 build_id 随引擎一起部署，
# 读不到时用 "unknown" 占位——标记的**在场**比取值更重要（V4 grandfather
# 以在场区分新代际），取值用于取证定位构建。
_ADJUDICATOR_BUILD_CACHE: str | None = None
# 与 supervisor_models.VALID_EVIDENCE_STATES 对齐的本地副本（避免结算侧
# 拉起整个 supervisor 模型层）。
_RECEIPT_STATUSES = frozenset({"passed", "failed", "error", "blocked", "skipped"})
_RECEIPT_FAILURE_STATUSES = frozenset({"failed", "error"})
_RECEIPT_REQUIRED_FIELDS = (
    "execution_id", "lease_id", "worker_id", "artifact_path",
    "artifact_sha256", "command", "status",
)
from core.launch_phases.findings_adjudication import (
    adjudicate_candidate_findings,
    adjudicator_build,
    artifact_matches,
    build_receipt_backed_confirmation,
    compact_title,
    has_fully_bound_failure_observations,
    ledger_supervisor_state_paths,
    load_claim_ledger,
    load_evidence_store_records,
    lookup_receipt_claim,
    receipt_observation,
    valid_evidence_receipt,
    verify_ledger_incident_receipt,
)

# Compatibility aliases
_MAX_LEDGER_SCANS = 64
_SHA64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_adjudicator_build = adjudicator_build
_valid_evidence_receipt = valid_evidence_receipt
_load_claim_ledger = load_claim_ledger
_lookup_receipt_claim = lookup_receipt_claim
_load_evidence_store_records = load_evidence_store_records
_artifact_matches = artifact_matches
_ledger_supervisor_state_paths = ledger_supervisor_state_paths
_receipt_observation = receipt_observation
_has_fully_bound_failure_observations = has_fully_bound_failure_observations
_build_receipt_backed_confirmation = build_receipt_backed_confirmation
_adjudicate_candidate_findings = adjudicate_candidate_findings
_compact_title = compact_title

def derive_settle_line(
    finding: dict, *, evidence_snapshot: Mapping[str, Mapping[str, Any]], root: Path,
) -> str:
    """派生 settle 引用；路径和行号必须来自已验证的 no-follow 快照。"""
    severity = str(finding.get("severity", "MEDIUM")).upper()
    title = _compact_title(str(finding.get("title", "")).strip())
    reference = ""
    from core.ledger.finding_io import snapshot_relative_path
    for item in finding.get("evidence") or []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        try:
            relative = snapshot_relative_path(item["path"], root)
        except Exception:
            continue
        metadata = evidence_snapshot.get(relative)
        if not isinstance(metadata, Mapping):
            continue
        line_no = item.get("line")
        if not isinstance(line_no, int) or isinstance(line_no, bool):
            line_no = 1
        line_count = metadata.get("line_count")
        if not isinstance(line_count, int) or isinstance(line_count, bool) or line_no > line_count:
            continue
        reference = f"{relative}:{line_no}"
        break
    # settle 行是行导向文件（verify_life evidence_bad_lines 逐行校验证据），
    # 多行 reproduction 原样内嵌会把一行撕成多行物理行，续行被判为
    # 「无证据编造」拒结算（2026-11-08 实测 sys.path.insert 续行被拒）。
    reproduction = " ".join(str(finding.get("reproduction") or "N/A").split())[:120]
    observation = " ".join(str(finding.get("root_cause") or "").split())[:80]
    return f"[{severity}] {title}。{reference}。复现：{reproduction}。观察：{observation}"


def auto_mode(runtime: dict) -> bool:
    """作战方案 auto 轮询时，结算允许自动生成 findings.json 骨架。"""
    if runtime.get("polling_policy") == "auto":
        return True
    try:
        manifest = _lifecycle_findings_path(runtime)
        payload = json.loads(
            (manifest.parent / "runtime.json").read_text(encoding="utf-8"),
        )
        return payload.get("polling_policy") == "auto"
    except (OSError, ValueError):
        return False


_TOKEN_FIELDS = ("total_tokens", "tokens_consumed", "token_count", "tokens")
_TOKEN_COMPONENT_FIELDS = (
    ("prompt_tokens", "completion_tokens"),
    ("input_tokens", "output_tokens"),
)
_TOKEN_COLLECTIONS = (
    "leases", "workers", "by_lease", "token_accounting", "claims", "executions", "records",
)


def _token_number(value: object) -> int | None:
    """Return a safe non-negative integer token count, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < 0:
        return None
    return int(value)


def _sum_token_records(value: object) -> int:
    """Read aggregate or per-lease token records without double counting.

    Supervisor snapshots have existed with both an aggregate field and a
    per-lease mapping.  Once an aggregate is present it is authoritative for
    that subtree; otherwise nested records are summed.
    """
    if isinstance(value, Mapping):
        for key in _TOKEN_FIELDS:
            number = _token_number(value.get(key))
            if number is not None:
                return number
        for input_key, output_key in _TOKEN_COMPONENT_FIELDS:
            input_tokens = _token_number(value.get(input_key))
            output_tokens = _token_number(value.get(output_key))
            if input_tokens is not None or output_tokens is not None:
                return (input_tokens or 0) + (output_tokens or 0)
        for key in _TOKEN_COLLECTIONS:
            if key in value:
                return _sum_token_records(value[key])
        numbers = [_token_number(item) for item in value.values()]
        if numbers and all(item is not None for item in numbers):
            return sum(item for item in numbers if item is not None)
        return sum(_sum_token_records(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_sum_token_records(item) for item in value)
    number = _token_number(value)
    return number or 0


def _supervisor_token_total(payload: Mapping[str, Any]) -> int:
    """Get the complete Worker lease token total from a Supervisor snapshot.

    ``execution_metrics`` is the preferred full-run ledger, followed by the
    per-lease ``claims`` and the cumulative ``tokens_consumed`` field.  The
    DOC-196 ``token_accounting`` map is only a last fallback because older
    writers keyed it by unit and therefore may repeat one lease's cost for a
    bundled claim.
    """
    if "execution_metrics" in payload:
        execution_total = _sum_token_records(payload.get("execution_metrics"))
        if execution_total > 0:
            return execution_total
    if "claims" in payload:
        claim_total = _sum_token_records(payload.get("claims"))
        if claim_total > 0:
            return claim_total
    top_level = _token_number(payload.get("tokens_consumed"))
    if top_level is not None:
        return top_level
    return (
        _sum_token_records(payload.get("token_accounting"))
        if "token_accounting" in payload else 0
    )


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _absolute_reader_path(path: Path, project_root: Path, project_reader: Any) -> tuple[Any, str, bool]:
    """Map an absolute state path to a bounded no-follow reader and relative name."""
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = candidate.relative_to(project_root).as_posix()
        return project_reader, relative, False
    except ValueError:
        anchor = Path(candidate.anchor)
        try:
            relative = candidate.relative_to(anchor).as_posix()
        except ValueError:
            raise FindingsSnapshotError("supervisor_state_path_invalid") from None
        try:
            from core.launch_phases.post_dry_run import RootReader
            reader = RootReader(anchor)
        except Exception:
            raise FindingsSnapshotError("supervisor_state_unavailable_or_unsafe") from None
        return reader, relative, True


def _read_supervisor_state(reader: Any, relative: str) -> dict[str, Any]:
    from core.launch_phases.post_dry_run import JSON_LIMIT, SnapshotInvalid, verify_supervisor_history

    try:
        source = reader.read(relative, JSON_LIMIT, "supervisor")
        payload = json.loads(source.raw_bytes.decode("utf-8"))
    except SnapshotInvalid as exc:
        raise FindingsSnapshotError(exc.reason_code) from None
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise FindingsSnapshotError("supervisor_state_invalid") from None
    if not isinstance(payload, dict):
        raise FindingsSnapshotError("supervisor_state_invalid")
    revision = payload.get("revision")
    run_id = payload.get("run_id")
    round_id = payload.get("round_id")
    session_id = payload.get("session_id")
    if (
        not isinstance(run_id, str) or not run_id.strip()
        or not isinstance(round_id, str) or not round_id.strip()
        or (session_id is not None and (not isinstance(session_id, str) or not session_id.strip()))
        or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
        or not isinstance(payload.get("audit_head"), str)
        or not verify_supervisor_history(payload)
    ):
        raise FindingsSnapshotError("supervisor_state_invalid")
    return payload


def _optional_project_file(reader: Any, relative: str) -> bool:
    """Check an optional mirror path without following any component."""
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
        raise FindingsSnapshotError("supervisor_mirror_path_invalid")
    parent = os.dup(reader.fd)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                return False
            except OSError:
                raise FindingsSnapshotError("supervisor_mirror_unavailable_or_unsafe") from None
            os.close(parent)
            parent = child
        try:
            item = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            raise FindingsSnapshotError("supervisor_mirror_unavailable_or_unsafe") from None
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
            raise FindingsSnapshotError("supervisor_mirror_unavailable_or_unsafe")
        return True
    finally:
        os.close(parent)


def _supervisor_state_identity(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    """Fields that must be identical between the authority and any state mirror."""
    history = payload.get("history")
    if not isinstance(history, list):
        raise FindingsSnapshotError("supervisor_state_invalid")
    base_seq = payload.get("history_base_seq", 1)
    base_hash = payload.get("history_base_hash", "")
    head_event = history[-1] if history else None
    head_seq = head_event.get("seq") if isinstance(head_event, Mapping) else base_seq - 1
    head_hash = head_event.get("hash") if isinstance(head_event, Mapping) else base_hash
    return (
        payload.get("run_id"), payload.get("session_id"), payload.get("round_id"),
        payload.get("revision"), base_seq, base_hash, head_seq, head_hash,
        payload.get("audit_head"),
    )


def _verify_auto_supervisor_identity(
    state: Mapping[str, Any], runtime: Mapping[str, Any],
) -> None:
    lifecycle_id = runtime.get("lifecycle_id")
    runtime_run_id = runtime.get("run_id")
    polling_id = runtime.get("polling_id")
    allowed_run_ids = {runtime_run_id} if isinstance(runtime_run_id, str) else set()
    if isinstance(polling_id, str) and polling_id.strip():
        allowed_run_ids.update({
            f"{polling_id}-{lifecycle_id}",
            f"lifecycle-{polling_id}-{lifecycle_id}",
        })
        if state.get("session_id") != polling_id:
            raise FindingsSnapshotError("supervisor_state_identity_mismatch")
    else:
        allowed_run_ids.add(f"lifecycle-{lifecycle_id}")
        # 扁平（非项目）布局没有 polling 命名空间：Supervisor 的 run_id 由
        # session_runtime 写入的 `session_id` 派生（polling_id 缺失时该字段回落为
        # 占位值，见 session_runtime._start_or_resume_session）。这里必须按**同一**
        # session_id 校验，否则扁平布局下 Supervisor 身份永远无法匹配
        # （实测 4 个 post 用例命中 supervisor_state_identity_mismatch）。
        session_id = runtime.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            allowed_run_ids.add(f"lifecycle-{session_id}-{lifecycle_id}")
            state_session = state.get("session_id")
            if state_session is not None and state_session != session_id:
                raise FindingsSnapshotError("supervisor_state_identity_mismatch")
    if (
        state.get("run_id") not in allowed_run_ids
        or state.get("round_id") != lifecycle_id
    ):
        raise FindingsSnapshotError("supervisor_state_identity_mismatch")


def _load_authoritative_supervisor(
    runtime: Mapping[str, Any], root: Path, project_reader: Any,
) -> tuple[dict[str, Any] | None, Any | None, bool]:
    """Load the sole configured Supervisor authority and verify present mirrors."""
    from core.launch_phases.post_dry_run import SnapshotInvalid

    authority_path = Path(os.path.abspath(os.fspath(supervisor_state_path())))
    authority_reader, authority_relative, close_authority = _absolute_reader_path(
        authority_path, root, project_reader,
    )
    try:
        try:
            if not authority_reader.exists(authority_relative):
                return None, authority_reader, close_authority
        except SnapshotInvalid as exc:
            raise FindingsSnapshotError(exc.reason_code) from None
        authority = _read_supervisor_state(authority_reader, authority_relative)
        _verify_auto_supervisor_identity(authority, runtime)
        authority_identity = _supervisor_state_identity(authority)

        for candidate in _supervisor_state_candidates(dict(runtime)):
            candidate = Path(os.path.abspath(os.fspath(candidate)))
            if candidate == authority_path:
                continue
            try:
                relative = candidate.relative_to(root).as_posix()
                if not _optional_project_file(project_reader, relative):
                    continue
                mirror = _read_supervisor_state(project_reader, relative)
            except FindingsSnapshotError:
                raise
            except SnapshotInvalid as exc:
                raise FindingsSnapshotError(exc.reason_code) from None
            except ValueError:
                raise FindingsSnapshotError("supervisor_mirror_path_invalid") from None
            if _supervisor_state_identity(mirror) != authority_identity:
                raise FindingsSnapshotError("supervisor_mirror_mismatch")
        return authority, authority_reader, close_authority
    except BaseException:
        if close_authority:
            authority_reader.close()
        raise


def _open_output_parent(root: Path, relative: str) -> tuple[int, str]:
    """Open an output's parent through no-follow directory descriptors."""
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
        raise FindingsSnapshotError("findings_output_path_invalid")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(root, flags)
    except OSError:
        raise FindingsSnapshotError("findings_output_unavailable_or_unsafe") from None
    try:
        for part in parts[:-1]:
            child_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd, parts[-1]
    except OSError:
        os.close(parent_fd)
        raise FindingsSnapshotError("findings_output_unavailable_or_unsafe") from None


def _read_at_no_follow(parent_fd: int, name: str, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        raise FindingsSnapshotError("findings_output_unsafe") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise FindingsSnapshotError("findings_output_unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise FindingsSnapshotError("findings_output_unsafe")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        ) or total != after.st_size:
            raise FindingsSnapshotError("findings_output_changed_during_read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
    """Atomically publish a temp file without replacing an existing report."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(parent_fd, source_bytes, parent_fd, target_bytes, 1)  # RENAME_NOREPLACE
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), target)
        if error not in {errno.ENOSYS, errno.EINVAL, getattr(errno, "EOPNOTSUPP", errno.EINVAL)}:
            raise OSError(error, os.strerror(error), target)
    elif hasattr(libc, "renameatx_np"):
        rename = libc.renameatx_np
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(parent_fd, source_bytes, parent_fd, target_bytes, 0x00000004)  # RENAME_EXCL
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), target)
        if error not in {errno.ENOSYS, errno.EINVAL, getattr(errno, "EOPNOTSUPP", errno.EINVAL)}:
            raise OSError(error, os.strerror(error), target)
    # Portable same-directory fallback still publishes atomically and cannot
    # replace a raced-in destination; link/unlink has the same durable outcome.
    os.link(source, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
    os.unlink(source, dir_fd=parent_fd)


def _write_findings_atomic_no_replace(path: Path, root: Path, raw_report: bytes) -> bool:
    """Write a report through dir_fd/no-follow, idempotently only by exact hash."""
    from core.launch_phases.post_dry_run import FINDINGS_LIMIT

    if len(raw_report) > FINDINGS_LIMIT:
        raise FindingsSnapshotError("findings_size_limit")
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        raise FindingsSnapshotError("findings_outside_project_root") from None
    parent_fd, name = _open_output_parent(root, relative)
    temp_name: str | None = None
    temp_fd: int | None = None
    expected_hash = hashlib.sha256(raw_report).hexdigest()
    try:
        try:
            existing_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing_stat = None
        except OSError:
            raise FindingsSnapshotError("findings_output_unsafe") from None
        if existing_stat is not None:
            if not stat.S_ISREG(existing_stat.st_mode):
                raise FindingsSnapshotError("findings_output_unsafe")
            existing = _read_at_no_follow(parent_fd, name, FINDINGS_LIMIT)
            if hashlib.sha256(existing).hexdigest() == expected_hash:
                return False
            raise FindingsSnapshotError("findings_output_conflict")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        for _attempt in range(5):
            temp_name = f".{name}.{os.urandom(12).hex()}.tmp"
            try:
                temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
                break
            except FileExistsError:
                temp_name = None
        if temp_fd is None or temp_name is None:
            raise FindingsSnapshotError("findings_temp_create_failed")
        offset = 0
        while offset < len(raw_report):
            written = os.write(temp_fd, raw_report[offset:])
            if written <= 0:
                raise FindingsSnapshotError("findings_output_write_failed")
            offset += written
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        try:
            _rename_noreplace(parent_fd, temp_name, name)
            temp_name = None
        except FileExistsError:
            existing = _read_at_no_follow(parent_fd, name, FINDINGS_LIMIT)
            if hashlib.sha256(existing).hexdigest() == expected_hash:
                return False
            raise FindingsSnapshotError("findings_output_conflict") from None
        os.fsync(parent_fd)
        return True
    except FindingsSnapshotError:
        raise
    except OSError:
        raise FindingsSnapshotError("findings_output_write_failed") from None
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def auto_build_findings_json(runtime: dict) -> str | None:
    """auto 模式：从 supervisor findings 自动生成结算 findings.json。

    携带合法 confirmation 块的 finding 计入 confirmed；其余诚实保持
    candidate（不写 confirmation 块），确认工作由后续 Life 继续。
    """
    runtime_snapshot = dict(runtime) if isinstance(runtime, Mapping) else {}
    run_id = runtime_snapshot.get("run_id")
    lifecycle_id = runtime_snapshot.get("lifecycle_id")
    round_no = _authoritative_round(runtime_snapshot)
    polling_id = runtime_snapshot.get("polling_id")
    if (
        not isinstance(run_id, str) or not run_id.strip()
        or not isinstance(lifecycle_id, str) or not lifecycle_id.strip()
        or not isinstance(round_no, int) or isinstance(round_no, bool) or round_no < 1
        or (polling_id is not None and (
            not isinstance(polling_id, str) or not polling_id.strip()
        ))
    ):
        raise FindingsSnapshotError("findings_binding_mismatch")

    out_path = _lifecycle_findings_path(runtime_snapshot)
    root = Path(os.path.abspath(os.fspath(resolve_project_root())))
    from core.launch_phases.post_dry_run import FINDINGS_LIMIT, SnapshotInvalid, RootReader

    try:
        project_reader = RootReader(root)
    except SnapshotInvalid as exc:
        raise FindingsSnapshotError(exc.reason_code) from None
    authority_reader = None
    close_authority = False
    try:
        supervisor_payload, authority_reader, close_authority = _load_authoritative_supervisor(
            runtime_snapshot, root, project_reader,
        )
        if supervisor_payload is None:
            return None
        lifecycle_root = _lifecycle_findings_path(runtime_snapshot).parent
        supervisor_tokens = _supervisor_token_total(supervisor_payload)
        raw_findings = supervisor_payload.get("findings", [])
        findings = [] if raw_findings is None else raw_findings
        if not isinstance(findings, list):
            raise FindingsSnapshotError("supervisor_state_invalid")
        if not findings and supervisor_tokens <= 0:
            return None

        from core.ledger import finding_confirmation as _fc
        try:
            from core.supervisor.checkpoint_pipeline import enrich_gate_finding
        except ImportError:
            enrich_gate_finding = None
        merged: list[dict] = []
        for item in findings:
            if not isinstance(item, dict):
                continue
            row = (
                enrich_gate_finding(item, lifecycle_root)
                if enrich_gate_finding else dict(item)
            )
            if isinstance(row.get("title"), str):
                row["title"] = _compact_title(row["title"])
            confirmation = row.get("confirmation")
            if isinstance(confirmation, dict):
                try:
                    _fc.validate_confirmation(row)
                except Exception:
                    row.pop("confirmation", None)
            merged.append(row)
        if not merged and supervisor_tokens <= 0:
            return None
        if merged:
            # DOC-188: adjudication mutates only this detached working copy.
            _adjudicate_candidate_findings(merged, root, run_id=run_id)
        confirmed = sum(1 for item in merged if _fc.is_confirmed(item))

        # DOC-196: the authoritative Supervisor lease ledger includes workers
        # that produced no Finding; per-Finding token data remains a fallback.
        finding_tokens = sum(
            int(item.get("token_count", 0)) for item in findings
            if isinstance(item, dict) and isinstance(item.get("token_count"), (int, float))
        )
        total_tokens = supervisor_tokens or finding_tokens
        has_measured = supervisor_tokens > 0 or any(
            isinstance(item, dict) and item.get("token_method") not in (None, "unmeasured")
            for item in findings
        )

        now_ts = time.time()
        elapsed_candidates: list[float] = []
        eff = runtime_snapshot.get("effective_total_seconds")
        if isinstance(eff, (int, float)) and eff > 0:
            elapsed_candidates.append(float(eff))
        cur_elapsed = runtime_snapshot.get("current_elapsed_seconds")
        if isinstance(cur_elapsed, (int, float)) and cur_elapsed > 0:
            elapsed_candidates.append(float(cur_elapsed))
        live_elapsed = runtime_snapshot.get("session_elapsed_seconds")
        if isinstance(live_elapsed, (int, float)) and live_elapsed > 0:
            elapsed_candidates.append(float(live_elapsed))
        started_epoch = runtime_snapshot.get("segment_started_epoch") or runtime_snapshot.get("started_epoch")
        if isinstance(started_epoch, (int, float)) and started_epoch > 0 and now_ts > started_epoch:
            elapsed_candidates.append(now_ts - float(started_epoch))
        total_elapsed = runtime_snapshot.get("total_elapsed_seconds")
        if isinstance(total_elapsed, (int, float)) and total_elapsed > 0:
            elapsed_candidates.append(float(total_elapsed))

        if isinstance(supervisor_payload, dict):
            sv_started = supervisor_payload.get("round_started_at") or supervisor_payload.get("started_at")
            high_water = supervisor_payload.get("clock_high_water")
            if isinstance(high_water, (int, float)) and isinstance(sv_started, (int, float)) and high_water > sv_started:
                elapsed_candidates.append(float(high_water - sv_started))
            elif isinstance(sv_started, (int, float)) and sv_started > 0 and now_ts > sv_started:
                elapsed_candidates.append(now_ts - float(sv_started))

        duration_sec = (
            max(1, int(round(max(elapsed_candidates))))
            if elapsed_candidates and max(elapsed_candidates) > 0
            else 0
        )

        report = {
            "schema_version": 1,
            "duration_seconds": duration_sec,
            "token_count": total_tokens,
            "token_method": "aggregated" if has_measured else "unmeasured",
            "host": str(runtime_snapshot.get("host") or os.environ.get("BUGX_HOST") or "cli").lower(),
            "run_id": run_id,
            "run_binding": {
                "run_id": run_id,
                "lifecycle_id": lifecycle_id,
                "round": round_no,
            },
            "findings": merged,
        }
        try:
            raw_report = (json.dumps(
                report, ensure_ascii=False, indent=1, allow_nan=False,
            ) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise FindingsSnapshotError("findings_validation_failed") from None
        if len(raw_report) > FINDINGS_LIMIT:
            raise FindingsSnapshotError("findings_size_limit")
        _validate_findings_payload_snapshot(
            report, runtime_snapshot, root, project_reader, initial_size=len(raw_report),
        )
        _write_findings_atomic_no_replace(out_path, root, raw_report)
        runtime["lifecycle_token_count"] = (
            runtime_snapshot.get("lifecycle_token_count", 0) + total_tokens
        )
        return f"findings={len(merged)} confirmed={confirmed}"
    finally:
        if close_authority and authority_reader is not None:
            authority_reader.close()
        project_reader.close()


def auto_settle(
    active_runtime: dict, snapshot: FindingsSnapshot | None = None, *,
    snapshot_captured: bool = False,
) -> bool:
    """宿主独占结算：以 finding_confirmation 门禁裁定的 confirmed 数计命。"""
    state = baseline_json() or {}
    history = state.get("history") or []
    current_round = state.get("round", 1)
    last_history = history[-1] if history else None
    if (isinstance(last_history, dict)
            and last_history.get("round") == current_round):
        return True
    if (
        snapshot is None and not snapshot_captured
        and auto_mode(active_runtime)
        and settle_findings_path(active_runtime) is None
    ):
        try:
            built = auto_build_findings_json(active_runtime)
        except FindingsSnapshotError as exc:
            print(f"[auto-settle] ✗ {exc.reason_code}")
            return False
        if built:
            print(f"[auto-settle] 已自动生成 findings.json（{built}）；"
                  "候选未经确认不计命，确认列入后续 Life")
        try:
            snapshot = capture_findings_snapshot(active_runtime)
            snapshot_captured = True
        except FindingsSnapshotError as exc:
            print(f"[auto-settle] ✗ {exc.reason_code}")
            return False
    elif not snapshot_captured:
        try:
            snapshot = capture_findings_snapshot(active_runtime)
            snapshot_captured = True
        except FindingsSnapshotError as exc:
            print(f"[auto-settle] ✗ {exc.reason_code}")
            return False
    round_at_start = active_runtime.get("round_at_start")
    if (isinstance(last_history, dict)
            and isinstance(current_round, int)
            and isinstance(round_at_start, int)
            and current_round > round_at_start
            and last_history.get("round") == current_round - 1):
        return True
    supervisor = _settlement_supervisor(resolve_project_root()) if uses_project_layout() else None
    frozen = None
    if supervisor is not None:
        try:
            frozen = supervisor.freeze_report_snapshot()
        except Exception:
            try:
                if supervisor.status().report_consumption.get("phase") == "POSTING":
                    print("[auto-settle] ✗ report 已处于 POSTING，等待同次消费恢复")
                    return False
            except Exception:
                pass
            frozen = None
    credited, lines, note = gate_confirmed_findings(
        active_runtime, snapshot, snapshot_captured=snapshot_captured,
    )
    if credited is None:
        print(f"[auto-settle] ✗ {note}")
        return False
    lifecycle_root = lifecycle_root_for(active_runtime)
    findings_file = lifecycle_root / f"findings_round{current_round}.txt"
    findings_file.parent.mkdir(parents=True, exist_ok=True)
    findings_file.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8",
    )
    cmd = [
        sys.executable, str(VERIFY),
        "settle", "--credited", str(credited), "--fraud", "0",
        "--findings-file", str(findings_file),
        "--round", str(current_round),
    ]
    result = None
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(resolve_project_root()),
            timeout=30, env=child_environment(),
        )
    except subprocess.TimeoutExpired:
        print("[auto-settle] ✗ settle 子进程超时（30s），拒绝结算")
        return False
    except OSError as exc:
        print(f"[auto-settle] ✗ settle 子进程启动失败: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        print(f"[auto-settle] ✗ settle 失败: {detail}")
        return False
    if supervisor is not None and isinstance(frozen, dict) and frozen.get("phase") == "POSTING":
        try:
            supervisor.settle_report({
                "report_sha256": (frozen.get("seal") or {}).get("report_sha256"),
                "settlement_output_sha256": hashlib.sha256(
                    (result.stdout or "").encode("utf-8")
                ).hexdigest(),
            })
        except Exception as exc:
            print(f"[auto-settle] ✗ Supervisor SETTLED 写入失败: {exc}")
            return False
    print(f"[auto-settle] ✓ 结算完成：{note}，credited={credited}, round={current_round}")
    return True
