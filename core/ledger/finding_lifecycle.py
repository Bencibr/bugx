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
    "regressed", "false_positive", "unverified",
}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
INCIDENT_ID_RE = re.compile(r"^I-[0-9a-f]{12}$")


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
        active.append(item)
        counts[state] += 1
    unverified = []
    for finding in sorted(baseline_findings, key=lambda item: item["id"]):
        key = lifecycle_key(finding)
        if key in current_keys or _state(finding) in {"fixed", "resolved", "false_positive"}:
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
           fix_validation: dict | None = None) -> dict:
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
    lifecycle["state"] = state
    if note:
        lifecycle["triage_note"] = note
    finding["lifecycle"] = lifecycle
    return finding


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compare", "triage"))
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id")
    parser.add_argument("--state", choices=sorted(TRIAGE_STATES))
    parser.add_argument("--note")
    parser.add_argument("--fix-validation", type=Path,
                        help="fixed 状态所需的真实目标复验 JSON")
    parser.add_argument("--ledger", type=Path,
                        help="可选错题账本；提供后对 incident_id 做存在性互查")
    args = parser.parse_args(argv)
    try:
        current = agent_eval.load_json(args.current)
        if args.command == "compare":
            finding_io.load_and_validate(args.current, args.root)
            baseline = agent_eval.load_json(args.baseline) if args.baseline else None
            payload = reconcile(current, baseline)
        else:
            if not args.id or not args.state:
                raise LifecycleError("triage 必须提供 --id 和 --state")
            fix_validation = (
                agent_eval.load_json(args.fix_validation) if args.fix_validation else None
            )
            triage(current, args.id, args.state, args.note, fix_validation)
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
