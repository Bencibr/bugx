#!/usr/bin/env python3
"""跨运行对比 findings；消失仅标未验证，真实目标复验后才允许 fixed。"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.agent import agent_eval
# DES-02：原先这里顶层 import finding_report（仅为复用其读取/写入原语），
# 而 finding_report.diff_reports 又延迟导入本模块的 reconcile —— 构成环。
# 原语已下沉到 finding_io，两个模块都只依赖它，方向变为单向。
from core.ledger import finding_io

SCHEMA_VERSION = 1
TRIAGE_STATES = {
    "new", "unchanged", "confirmed", "accepted", "fixed", "resolved",
    "regressed", "false_positive", "unverified", "refuted",
}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
INCIDENT_ID_RE = re.compile(r"^I-[0-9a-f]{12}$")

# ---------------------------------------------------------------------------
# NR-5（REFUTED_QUALITY_AUDIT_2026-11-16 §5 schema 级附则）：证伪台账元数据修复。
#
# 历史实证（同审计 §1.3，169 条 REFUTED 全量画像）：
#   * confirmation 字段 164/169 为同一字符串 `hypothesis-refuted-verified`，
#     零信息量，无法作为机读 oracle 区分各记录证伪方式；
#   * block_reason 全部为空串——专用于承载证伪理由的字段未使用，理由只存在
#     于自由文本 evidence；
#   * severity 全部塌缩为 info——假说原严重级丢失，无法对证伪错误做风险排序。
# 后果：下游无法机器筛假证伪（审计 §4 模式 5）。按仓库宪法允许破坏性变更，
# 旧格式（字符串 confirmation / 空 block_reason）不再适用，明确拒绝并给出
# 重建方式（补齐结构化字段后重判）。
# ---------------------------------------------------------------------------
REFUTED_BLOCK_REASONS = frozenset({
    "non_reproducible",        # 按字段语义无法复现触发路径
    "by_design",               # 语义豁免类（NR-3：必须绑定设计/数据流分析证据）
    "fixed_upstream",          # 既往修复已覆盖（对应审计 already-fixed 层）
    "evidence_insufficient",   # 现有证据不足以支撑缺陷存在
    "dynamic_probe_cleared",   # 动态反证探针已执行且未触发（唯一最强证伪型）
})
REFUTED_BLOCK_REASON_NOTE = (
    "REFUTED 必须提供结构化 block_reason（枚举: "
    + "/".join(sorted(REFUTED_BLOCK_REASONS))
    + "）；自由文本敷衍视为未提供（NR-5 fail-closed）"
)
# NR-5 重判通道：历史台账的假说原严重级已经丢失，任何人为补写都是伪造证据。
# `unknown` 是显式状态（必须携带理由与证据引用），与伪造枚举值严格区分；
# 消费方通过 refuted_strength() 把它识别为 legacy，不按证伪强度消费。
REFUTED_ORIGINAL_SEVERITY_UNKNOWN = "unknown"
REFUTED_LEGACY_STRENGTH = "legacy"
_LEGACY_REFUTED_CONFIRMATION_PREFIX = "hypothesis-refuted"


def is_legacy_refuted(finding: dict) -> bool:
    """判定未重判的历史 REFUTED（字符串 confirmation + 空 block_reason，无 lifecycle）。"""
    if not isinstance(finding, dict):
        return False
    if str(finding.get("status") or "").upper() != "REFUTED":
        return False
    lifecycle = finding.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("state") in TRIAGE_STATES:
        return False
    confirmation = finding.get("confirmation")
    return (
        isinstance(confirmation, str)
        and confirmation.strip().lower().startswith(_LEGACY_REFUTED_CONFIRMATION_PREFIX)
    )


def refuted_strength(finding: dict) -> str:
    """单一证伪强度口径：legacy / 结构化 block_reason / not_refuted。

    未重判的历史记录、或原严重级标为 unknown 的重判记录一律返回 ``legacy``，
    调用方不得把它们当作已具备可机器核验的证伪强度。
    """
    if not isinstance(finding, dict):
        return "not_refuted"
    lifecycle = finding.get("lifecycle")
    is_refuted = is_legacy_refuted(finding) or (
        isinstance(lifecycle, dict) and lifecycle.get("state") == "refuted"
    )
    if not is_refuted:
        return "not_refuted"
    validation = lifecycle.get("refuted_validation") if isinstance(lifecycle, dict) else None
    if not isinstance(validation, dict):
        return REFUTED_LEGACY_STRENGTH
    if validation.get("original_severity") == REFUTED_ORIGINAL_SEVERITY_UNKNOWN:
        return REFUTED_LEGACY_STRENGTH
    block_reason = validation.get("block_reason")
    return block_reason if block_reason in REFUTED_BLOCK_REASONS else REFUTED_LEGACY_STRENGTH


def _retriage_snapshot(finding: dict) -> dict:
    snapshot = copy.deepcopy(finding)
    snapshot.pop("retriage_history", None)
    return snapshot


def _record_retriage(finding: dict, *, reason: str, source: str, revision: int) -> None:
    """把重判前原记录写入不可篡改的 retriage_history（含 SHA 快照）。"""
    snapshot = _retriage_snapshot(finding)
    previous_sha256 = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    history = finding.get("retriage_history")
    if not isinstance(history, list):
        history = []
    if len(history) >= 100:
        raise LifecycleError("retriage_history 已达上限（100）")
    history.append({
        "reason": reason, "source": source, "revision": revision,
        "previous_sha256": previous_sha256, "previous": snapshot,
    })
    finding["retriage_history"] = history


class LifecycleError(ValueError):
    """findings 生命周期协议错误。"""


def lifecycle_key(finding: dict) -> str:
    explicit = finding.get("fingerprint")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    evidence = finding.get("evidence") or []
    primary_path = evidence[0].get("path", "") if evidence else ""
    title = re.sub(r"\s+", " ", str(finding.get("title", "")).strip().lower())
    raw = "\0".join((str(finding.get("root_cause", "")), primary_path, title))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _state(finding: dict) -> str:
    lifecycle = finding.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("state") in TRIAGE_STATES:
        return str(lifecycle["state"])
    # NR-5 消费兼容：历史 REFUTED 没有 lifecycle，但状态就是 refuted。
    # 旧实现返回 new，reconcile 会把已证伪记录重新计为新发现，并让
    # 未验证回补通道误以为它们还在追踪中。这里按显式 legacy 标记识别，
    # 强度仍需通过 refuted_strength() 判断，不能当已重判证据。
    if is_legacy_refuted(finding):
        return "refuted"
    return "new"


def reconcile(current_payload: dict, baseline_payload: dict | None = None) -> dict:
    current = agent_eval.validate_findings(current_payload)
    baseline_findings: list[dict] = []
    if baseline_payload is not None:
        baseline_findings = agent_eval.validate_findings(baseline_payload)
    baseline_by_key = {lifecycle_key(item): item for item in baseline_findings}
    current_keys: set[str] = set()
    active = []
    counts = {state: 0 for state in TRIAGE_STATES}
    current_run = current_payload.get("run_id")
    baseline_run = baseline_payload.get("run_id") if baseline_payload else None
    for finding in sorted(current, key=lambda item: item["id"]):
        key = lifecycle_key(finding)
        current_keys.add(key)
        prior = baseline_by_key.get(key)
        prior_state = _state(prior) if prior else None
        if prior_state in {"fixed", "resolved"}:
            state = "regressed"
        elif prior:
            state = "unchanged"
        else:
            state = "new"
        first_seen = (
            prior.get("lifecycle", {}).get("first_seen_run_id", baseline_run)
            if prior else current_run
        )
        item = dict(finding)
        item["fingerprint"] = key
        item["lifecycle"] = {
            "state": state,
            "first_seen_run_id": first_seen,
            "last_seen_run_id": current_run,
            "previous_state": prior_state,
        }
        if is_legacy_refuted(item):
            # 显式标记未重判，避免下游把「有 refuted 状态」误读为
            # 「已有可机器核验的证伪强度」（NR-5 消费兼容）。
            item["lifecycle"]["refuted_validation_status"] = REFUTED_LEGACY_STRENGTH
            item["lifecycle"]["refuted_strength"] = REFUTED_LEGACY_STRENGTH
        active.append(item)
        counts[state] += 1
    unverified = []
    for finding in sorted(baseline_findings, key=lambda item: item["id"]):
        key = lifecycle_key(finding)
        if key in current_keys or _state(finding) in {"fixed", "resolved", "false_positive", "refuted"}:
            continue
        item = dict(finding)
        item["fingerprint"] = key
        lifecycle = dict(item.get("lifecycle") or {})
        lifecycle.update({
            "state": "unverified",
            "last_seen_run_id": baseline_run,
            "missing_in_run_id": current_run,
            "verification_note": "本轮未发现不等于已修复；需要真实目标复验",
        })
        item["lifecycle"] = lifecycle
        unverified.append(item)
        counts["unverified"] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": current_run,
        "baseline_run_id": baseline_run,
        "duration_seconds": current_payload["duration_seconds"],
        "token_count": current_payload["token_count"],
        "findings": active,
        "unverified_findings": unverified,
        "summary": {key: value for key, value in counts.items() if value},
    }


def validate_fix_validation(value: object) -> dict:
    if not isinstance(value, dict):
        raise LifecycleError("fixed 必须提供 fix_validation")
    for field in (
        "required_platform", "executed_platform", "target_fingerprint", "test_reference",
    ):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise LifecycleError(f"fix_validation 缺 {field}")
    if value.get("executed") is not True:
        raise LifecycleError("fix_validation 必须在真实目标执行")
    if value.get("simulated") is not False:
        raise LifecycleError("fix_validation 不接受模拟执行")
    if value["required_platform"] != value["executed_platform"]:
        raise LifecycleError(
            "fix_validation 的要求平台与实际执行平台不一致，不能作为 Live 复验"
        )
    if value.get("outcome") != "pass":
        raise LifecycleError("fix_validation outcome 必须为 pass")
    errors = value.get("errors")
    if not isinstance(errors, list) or any(
        not isinstance(item, str) or not item.strip() for item in errors
    ):
        raise LifecycleError("fix_validation.errors 必须是字符串数组")
    if errors:
        raise LifecycleError("fix_validation 存在工具或基础设施错误")
    # 可选工件绑定：复验产物落盘并带哈希，让「已修复」可被独立复核。
    artifact_path = value.get("artifact_path")
    artifact_sha256 = value.get("artifact_sha256")
    if (artifact_path is None) != (artifact_sha256 is None):
        raise LifecycleError("fix_validation 的 artifact_path/artifact_sha256 必须成对提供")
    if artifact_path is not None:
        if not isinstance(artifact_path, str) or not artifact_path.strip():
            raise LifecycleError("fix_validation.artifact_path 必须是非空字符串")
        if not isinstance(artifact_sha256, str) or not SHA256_RE.fullmatch(artifact_sha256):
            raise LifecycleError("fix_validation.artifact_sha256 必须是 64 位十六进制 SHA-256")
    incident_id = value.get("incident_id")
    if incident_id is not None and (
        not isinstance(incident_id, str) or not INCIDENT_ID_RE.fullmatch(incident_id)
    ):
        raise LifecycleError("fix_validation.incident_id 格式无效（应为 I-xxxxxxxxxxxx）")
    return value


def validate_refuted_validation(value: object, finding: dict | None = None,
                                *, allow_severity_restore: bool = False) -> dict:
    """NR-5：``refuted`` 状态转移的强制结构化证伪协议（fail-closed）。

    契约（每条 REFUTED 都必须逐记录携带，禁止台账级同串复制）：
    * ``block_reason``：结构化证伪理由，必须取自 :data:`REFUTED_BLOCK_REASONS`
      枚举，自由文本敷衍视为未提供；
    * ``refuted_by``：非空判定者标识（谁执行了证伪判定）；
    * ``refuted_evidence``：反证证据引用数组——**可为空数组，但结构必须存在**；
    * ``original_severity``：假说原严重级。severity 保留原值禁止塌缩 info；
      仅当 finding 本身无 severity 时才允许落到 info。原始级别确实丢失时，
      允许显式写 ``"unknown"`` + ``severity_unknown_reason``（必须说明来源，
      禁止凭空补一个合法枚举值伪造证据），消费方按 legacy 处理。

    与既有 ``false_positive`` 强制 note（撤回级决定零理由禁止）同一模式，
    但比自由文本更强：理由字段本身结构化，下游可按 ``block_reason`` 枚举
    直接过滤证伪强度。``allow_severity_restore`` 仅供有证据的历史重判通道
    从 info 恢复原级；普通 triage 绝不开启，防止以"恢复"之名掩盖塌缩。
    """
    if not isinstance(value, dict):
        raise LifecycleError("refuted 必须提供 refuted_validation（结构化证伪协议）")
    block_reason = value.get("block_reason")
    if not isinstance(block_reason, str) or block_reason not in REFUTED_BLOCK_REASONS:
        raise LifecycleError(REFUTED_BLOCK_REASON_NOTE + f"；得到: {block_reason!r}")
    refuted_by = value.get("refuted_by")
    if not isinstance(refuted_by, str) or not refuted_by.strip():
        raise LifecycleError(
            "refuted_validation.refuted_by 必须是非空判定者标识"
            "（逐记录携带，禁止全台账同串复制）"
        )
    if len(refuted_by) > 200:
        raise LifecycleError("refuted_validation.refuted_by 不得超过 200 字符")
    refuted_evidence = value.get("refuted_evidence")
    if not isinstance(refuted_evidence, list):
        raise LifecycleError(
            "refuted_validation.refuted_evidence 必须是数组（可为空，结构必须存在）"
        )
    for item in refuted_evidence:
        if not isinstance(item, str) or not item.strip() or len(item) > 2000:
            raise LifecycleError("refuted_evidence 元素必须是非空字符串（≤2000字符）")
    severity_levels = tuple(
        getattr(agent_eval, "FINDING_SEVERITY_LEVELS",
                ("info", "low", "medium", "high", "critical"))
    )
    current = finding.get("severity") if isinstance(finding, dict) else None
    current = (
        str(current).strip().lower()
        if isinstance(current, str) and current.strip() else None
    )
    original = value.get("original_severity")
    if original == REFUTED_ORIGINAL_SEVERITY_UNKNOWN:
        # 显式「原级已丢失」通道：不是枚举补全，而是把丢失事实留给消费方。
        reason = value.get("severity_unknown_reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise LifecycleError(
                "original_severity=unknown 必须提供非空 severity_unknown_reason"
                "（≤2000字符，说明原级为何不可得，禁止凭空补枚举）"
            )
        if current is not None and current != "info":
            raise LifecycleError(
                "original_severity=unknown 不得用于有真实 severity 的记录"
                f"（当前 severity={current}；未知原级无法证明未塌缩）"
            )
        validated = dict(value)
        validated["severity_unknown_reason"] = reason.strip()
        return validated
    if original is None:
        if current is not None and current != "info":
            # finding 已带真实严重级却不随转移携带原级 ⇒ 判定塌缩，拒绝。
            raise LifecycleError(
                "refuted_validation 必须携带 original_severity"
                f"（finding 现有 severity={current}，禁止塌缩为 info）"
            )
        validated = dict(value)
        validated["original_severity"] = "info"  # 原级缺失才允许 info
        return validated
    if not isinstance(original, str) or original not in severity_levels:
        raise LifecycleError(
            f"refuted_validation.original_severity 无效: {original!r}"
            f"（合法: {'/'.join(severity_levels)}，原级丢失请用 "
            f"{REFUTED_ORIGINAL_SEVERITY_UNKNOWN} + severity_unknown_reason）"
        )
    if current is not None and current != original and not (
        allow_severity_restore and current == "info"
    ):
        raise LifecycleError(
            f"REFUTED severity 塌缩: 记录 severity={current}"
            f" 与假说原级 {original} 不一致（NR-5 禁止塌缩）"
        )
    return dict(value)


def verify_fix_validation_artifacts(fix_validation: dict, root: Path,
                                     ledger: Path | None = None) -> list[str]:
    """复核 fixed 声明：工件哈希重算 + incident 存在性互查；返回错误清单。"""
    errors: list[str] = []
    artifact_path = fix_validation.get("artifact_path")
    if artifact_path is not None:
        target = (root / str(artifact_path)).resolve()
        root_resolved = root.resolve()
        if target != root_resolved and root_resolved not in target.parents:
            errors.append(f"artifact_path 逃逸项目根: {artifact_path}")
        elif not target.is_file():
            errors.append(f"artifact_path 不存在: {artifact_path}")
        else:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual.lower() != str(fix_validation.get("artifact_sha256", "")).lower():
                errors.append(f"artifact_sha256 不匹配: {artifact_path}")
    incident_id = fix_validation.get("incident_id")
    if incident_id is not None and ledger is not None:
        try:
            from core.ledger._facade import LedgerProxy

            incidents = LedgerProxy().list_incidents(ledger)
            if not any(item.get("incident_id") == incident_id for item in incidents):
                errors.append(f"incident_id 不在错题账本中: {incident_id}")
        except (AttributeError, ImportError, OSError, ValueError) as exc:
            errors.append(f"incident 互查失败（按 fail-closed 处理）: {exc}")
    return errors


def reopen(finding: dict, *, reason: str, revision: int) -> dict:
    """Reopen a rejected record without inventing a confirmed bug."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise LifecycleError("reopen reason 必须是非空字符串（最多2000字符）")
    old_lifecycle = finding.get("lifecycle") or {}
    old_confirmation = finding.get("confirmation") or {}
    if (old_lifecycle.get("state") != "false_positive"
            and old_confirmation.get("state") != "rejected"):
        raise LifecycleError("仅可重开 false_positive/rejected Finding")
    history = copy.deepcopy(finding.get("retriage_history", []))
    if not isinstance(history, list) or len(history) >= 100:
        raise LifecycleError("retriage_history 无效或已达上限")
    snapshot = copy.deepcopy(finding)
    snapshot.pop("retriage_history", None)
    previous_sha256 = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    history.append({"reason": reason.strip(), "revision": revision,
                    "previous_sha256": previous_sha256, "previous": snapshot})
    lifecycle = copy.deepcopy(old_lifecycle)
    lifecycle.update({"state": "unverified", "previous_state": old_lifecycle.get("state"),
                      "triage_note": reason.strip()})
    finding["lifecycle"] = lifecycle
    finding["confirmation"] = {"state": "candidate"}
    finding["confirmed"] = False
    finding.pop("remediation", None)
    finding["retriage_history"] = history
    return finding


def triage(payload: dict, finding_id: str, state: str, note: str | None = None,
           fix_validation: dict | None = None,
           refuted_validation: dict | None = None) -> dict:
    if state not in TRIAGE_STATES - {"unchanged", "regressed", "resolved", "unverified"}:
        raise LifecycleError(f"不可人工设置 lifecycle state: {state}")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise LifecycleError("payload.findings 必须是数组")
    finding = next((item for item in findings if item.get("id") == finding_id), None)
    if finding is None:
        raise LifecycleError(f"finding 不存在: {finding_id}")
    lifecycle = dict(finding.get("lifecycle") or {})
    if (lifecycle.get("state") == "false_positive"
            or (finding.get("confirmation") or {}).get("state") == "rejected") and state != "false_positive":
        raise LifecycleError("撤回 rejected/false_positive 必须通过宿主 reopen 接口")
    if state == "fixed":
        lifecycle["fix_validation"] = validate_fix_validation(fix_validation)
    if state == "refuted":
        # NR-5（2026-11-16 证伪质量审计 §5）：REFUTED 与 false_positive 同为
        # 撤回级决定，但历史台账 169 条 REFUTED 的 block_reason 全空、
        # confirmation 164/169 同字符串、severity 全塌缩 info——证伪强度
        # 无法机器筛选。此处 fail-closed：结构化证伪协议不完整即拒绝转移。
        validated = validate_refuted_validation(refuted_validation, finding)
        original = validated["original_severity"]
        current = finding.get("severity")
        if not (isinstance(current, str) and current.strip()):
            # unknown 不是 severity 枚举值，不能写进 finding.severity。
            finding["severity"] = (
                "info" if original == REFUTED_ORIGINAL_SEVERITY_UNKNOWN else original
            )
        lifecycle["refuted_validation"] = validated
        lifecycle["refuted_validation_status"] = (
            REFUTED_LEGACY_STRENGTH
            if original == REFUTED_ORIGINAL_SEVERITY_UNKNOWN else "retriated"
        )
        lifecycle["refuted_strength"] = refuted_strength({**finding, "lifecycle": lifecycle})
        # confirmation 结构必须存在且逐记录携带判定者与反证证据引用，
        # 从 schema 层消灭「全台账同字符串复制」（审计 §1.3 异常 1）。
        finding["confirmation"] = {
            "state": "refuted",
            "block_reason": validated["block_reason"],
            "refuted_by": validated["refuted_by"],
            "refuted_evidence": list(validated["refuted_evidence"]),
            "original_severity": original,
            "refuted_validation_status": lifecycle["refuted_validation_status"],
        }
    if state == "false_positive" and not (note and note.strip()):
        # F-RSI-20261115-128：false_positive 与 rejected 同为撤回级决定——
        # 它把 finding 从 reconcile 的 unverified 追踪中消失（L128 过滤），
        # 零理由即可把真缺陷撤出追踪。对齐 finding_confirmation.rejected
        # 的强制理由语义（rejection_class + reason 必填）。
        raise LifecycleError("false_positive 必须提供理由（note）：撤回级决定不得零理由")
    lifecycle["state"] = state
    if note:
        lifecycle["triage_note"] = note
    finding["lifecycle"] = lifecycle
    return finding


def retriage_legacy_refuted(payload: dict, decisions: dict,
                            *, source: str | None = None) -> dict:
    """NR-5 方案 A：按显式裁决重判历史 REFUTED，绝不批量补造丢失字段。

    输入 ``decisions`` 必须是人工/审计逐条裁决文件，格式::

        {
          "source": "DYNAMIC_REFUTATION_SAMPLE_...md#结论",
          "decisions": [
            {"id": "F-RSI-...", "block_reason": "dynamic_probe_cleared",
             "refuted_by": "审计员/agent 标识",
             "refuted_evidence": ["报告路径#章节", "工件 sha256"],
             "original_severity": "unknown",
             "severity_unknown_reason": "原始假说严重级未存档，禁止补写",
             "occurrence": 1,
             "note": "逐条判定说明"}
          ]
        }

    ``occurrence`` 是同一 id 在台账中重复出现时的 1-based 序号（默认 1），
    让重复 id 也能逐副本裁决，绝不静默跳过其中一份。

    原级有确证材料时才允许填合法枚举，且必须附 ``severity_evidence``；
    否则只能填 ``unknown`` 并说明理由。未被裁决的历史记录保持原样，
    输出 summary 中计入 ``unchanged_legacy``，由消费方按 legacy 处理。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        raise LifecycleError("重判输入 payload.findings 必须是数组")
    if not isinstance(decisions, dict) or not isinstance(decisions.get("decisions"), list):
        raise LifecycleError("重判裁决文件必须含 decisions 数组")
    adjudication_source = source if source is not None else decisions.get("source")
    if (not isinstance(adjudication_source, str) or not adjudication_source.strip()
            or len(adjudication_source) > 500):
        raise LifecycleError("重判必须提供非空 source（≤500字符，指向裁决依据）")
    source = adjudication_source.strip()
    findings = payload["findings"]
    by_id: dict[str, list[dict]] = {}
    for index, item in enumerate(findings, 1):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise LifecycleError(f"重判输入 finding #{index} 缺 id")
        by_id.setdefault(item["id"], []).append(item)

    seen: set[tuple[str, int]] = set()
    retriated: list[str] = []
    retriated_unknown: list[str] = []
    transformed: set[int] = set()
    for index, decision in enumerate(decisions["decisions"], 1):
        if not isinstance(decision, dict):
            raise LifecycleError(f"重判裁决 #{index} 必须是对象")
        finding_id = decision.get("id")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise LifecycleError(f"重判裁决 #{index} 缺 id")
        finding_id = finding_id.strip()
        occurrence = decision.get("occurrence", 1)
        if (not isinstance(occurrence, int) or isinstance(occurrence, bool)
                or occurrence < 1):
            raise LifecycleError(f"重判裁决 #{index} occurrence 必须是正整数")
        key = (finding_id, occurrence)
        if key in seen:
            raise LifecycleError(
                f"重判裁决重复: {finding_id}#{occurrence}"
                "（同一 id 的不同副本请用不同 occurrence）"
            )
        seen.add(key)
        copies = by_id.get(finding_id, [])
        if occurrence > len(copies):
            raise LifecycleError(
                f"重判裁决指向不存在的 finding 副本: {finding_id}#{occurrence}"
                f"（实际只有 {len(copies)} 份）"
            )
        finding = copies[occurrence - 1]
        label = finding_id if occurrence == 1 else f"{finding_id}#{occurrence}"
        if not is_legacy_refuted(finding):
            raise LifecycleError(
                f"{label} 不是未重判的 legacy REFUTED，拒绝覆盖已有裁决"
            )
        evidence = decision.get("refuted_evidence")
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(item, str) or not item.strip() or len(item) > 2000
                       for item in evidence)):
            raise LifecycleError(
                f"{finding_id} 重判必须提供非空 refuted_evidence（逐条证据引用）"
            )
        original = decision.get("original_severity")
        severity_evidence = decision.get("severity_evidence")
        if original == REFUTED_ORIGINAL_SEVERITY_UNKNOWN:
            if severity_evidence:
                raise LifecycleError(
                    f"{finding_id} original_severity=unknown 不得同时携带 severity_evidence"
                )
        elif isinstance(original, str) and original.strip():
            if (not isinstance(severity_evidence, list) or not severity_evidence
                    or any(not isinstance(item, str) or not item.strip()
                           or len(item) > 2000 for item in severity_evidence)):
                raise LifecycleError(
                    f"{finding_id} 填写原严重级必须提供 severity_evidence 证据引用，"
                    "禁止凭空补枚举值"
                )
        validated = validate_refuted_validation(
            decision, finding,
            allow_severity_restore=(
                isinstance(original, str)
                and original not in {REFUTED_ORIGINAL_SEVERITY_UNKNOWN, "info"}
            ),
        )
        note = decision.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise LifecycleError(f"{finding_id} note 必须是字符串（≤2000字符）")
        reason = (note or "NR-5 逐条 triage 重判").strip()

        _record_retriage(finding, reason=reason, source=source, revision=len(
            finding.get("retriage_history") or []))
        lifecycle = dict(finding.get("lifecycle") or {})
        lifecycle.update({
            "state": "refuted",
            "previous_state": "refuted",
            "refuted_validation": validated,
            "refuted_validation_status": (
                REFUTED_LEGACY_STRENGTH
                if validated["original_severity"] == REFUTED_ORIGINAL_SEVERITY_UNKNOWN
                else "retriated"
            ),
            "retriage_source": source,
            "triage_note": reason,
        })
        if validated["original_severity"] != REFUTED_ORIGINAL_SEVERITY_UNKNOWN:
            # 有证据支持原级时写回，撤销历史 info 塌缩。
            finding["severity"] = validated["original_severity"]
            lifecycle["severity_evidence"] = list(severity_evidence)
        finding["lifecycle"] = lifecycle
        lifecycle["refuted_strength"] = refuted_strength(finding)
        finding["block_reason"] = validated["block_reason"]
        finding["confirmation"] = {
            "state": "refuted",
            "block_reason": validated["block_reason"],
            "refuted_by": validated["refuted_by"],
            "refuted_evidence": list(validated["refuted_evidence"]),
            "original_severity": validated["original_severity"],
            "refuted_validation_status": lifecycle["refuted_validation_status"],
        }
        # NR-5 §1.3 异常 2：证伪记录保留 reproducible=true 自相矛盾，
        # 重判时显式纠正并在 lifecycle 留痕。
        if finding.get("reproducible") is True:
            finding["reproducible"] = False
            lifecycle["reproducible_corrected"] = True
        retriated.append(label)
        transformed.add(id(finding))
        if validated["original_severity"] == REFUTED_ORIGINAL_SEVERITY_UNKNOWN:
            retriated_unknown.append(label)

    unchanged_legacy = [
        item["id"] for item in findings
        if is_legacy_refuted(item) and id(item) not in transformed
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "retriage_source": source,
        "findings": findings,
        "summary": {
            "total": len(findings),
            "retriated": len(retriated),
            "retriated_unknown": len(retriated_unknown),
            "unchanged_legacy": len(unchanged_legacy),
        },
        "retriated_ids": retriated,
        "retriated_unknown_ids": retriated_unknown,
        "unchanged_legacy_ids": unchanged_legacy,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compare", "triage", "retriage-legacy"))
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id")
    parser.add_argument("--state", choices=sorted(TRIAGE_STATES))
    parser.add_argument("--note")
    parser.add_argument("--fix-validation", type=Path,
                        help="fixed 状态所需的真实目标复验 JSON")
    parser.add_argument("--refuted-validation", type=Path,
                        help="refuted 状态所需的结构化证伪协议 JSON"
                             "（block_reason/refuted_by/refuted_evidence/original_severity）")
    parser.add_argument("--ledger", type=Path,
                        help="可选错题账本；提供后对 incident_id 做存在性互查")
    parser.add_argument("--decisions", type=Path,
                        help="retriage-legacy 所需的逐条裁决 JSON")
    parser.add_argument("--source",
                        help="retriage-legacy 裁决依据（覆盖裁决文件中的 source）")
    args = parser.parse_args(argv)
    try:
        current = agent_eval.load_json(args.current)
        if args.command == "compare":
            finding_io.load_and_validate(args.current, args.root)
            baseline = agent_eval.load_json(args.baseline) if args.baseline else None
            payload = reconcile(current, baseline)
        elif args.command == "retriage-legacy":
            if not args.decisions:
                raise LifecycleError("retriage-legacy 必须提供 --decisions")
            decisions = agent_eval.load_json(args.decisions)
            payload = retriage_legacy_refuted(
                current, decisions, source=args.source,
            )
        else:
            if not args.id or not args.state:
                raise LifecycleError("triage 必须提供 --id 和 --state")
            fix_validation = (
                agent_eval.load_json(args.fix_validation) if args.fix_validation else None
            )
            refuted_validation = (
                agent_eval.load_json(args.refuted_validation)
                if args.refuted_validation else None
            )
            triage(current, args.id, args.state, args.note, fix_validation,
                   refuted_validation)
            if args.state == "fixed" and isinstance(fix_validation, dict):
                artifact_errors = verify_fix_validation_artifacts(
                    fix_validation, args.root, args.ledger,
                )
                if artifact_errors:
                    raise LifecycleError(
                        "fixed 工件复核失败: " + "; ".join(artifact_errors)
                    )
            payload = current
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            finding_io.write_atomic(args.output, content)
        else:
            sys.stdout.write(content)
        return 0
    except (agent_eval.EvalError, LifecycleError, OSError) as exc:
        print(f"[finding_lifecycle] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
