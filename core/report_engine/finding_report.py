#!/usr/bin/env python3
"""校验并导出 BugX 发现，支持 JSON、Markdown、SARIF 和 DefectDojo。"""
from __future__ import annotations
from typing import Any
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p
import sys
from pathlib import Path


import argparse
import hashlib
import json

_CURRENT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _CURRENT_DIR.parent
for _p in (str(_REPO_ROOT), str(_CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.agent import agent_eval
from core.ledger import finding_confirmation

# DES-02：以下原语已下沉到 finding_io（finding_lifecycle 也依赖它们，
# 原先构成 finding_report <-> finding_lifecycle 双向依赖）。这里再导出，
# 保持 finding_report.<name> 对外可用。
from core.ledger.finding_io import (  # noqa: F401  (re-export)
    load_and_validate,
    validate_run_binding,
    write_atomic,
)

VERSION = "0.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
LEVELS = {"critical": "error", "high": "error", "medium": "warning", "low": "note"}


def final_payload(payload: dict) -> dict:
    """只保留已确认 Bug；有未决候选时拒绝生成最终报告。"""
    decision = finding_confirmation.gate(payload)
    if not decision["ready"]:
        pending = ", ".join(
            item["id"] for item in decision["findings"]
            if not item["confirmed"] and item["state"] != "rejected"
        )
        raise agent_eval.EvalError(
            f"最终报告仍有 {decision['pending']} 条未决候选: {pending or 'unknown'}"
        )
    resolved = dict(payload)
    resolved["findings"] = [
        finding for finding in payload["findings"]
        if finding_confirmation.is_confirmed(finding)
    ]
    resolved["decision_summary"] = {
        "candidates": decision["total"],
        "confirmed": decision["confirmed"],
        "rejected": decision["rejected"],
        "pending": decision["pending"],
    }
    return resolved


def normalized_payload(payload: dict) -> dict:
    """输出稳定、工具无关的 findings contract，保留扩展字段。"""
    payload = final_payload(payload)
    findings = sorted(payload["findings"], key=lambda item: item["id"])
    return {
        "schema_version": 1,
        "run_id": payload.get("run_id"),
        "duration_seconds": payload["duration_seconds"],
        "token_count": payload["token_count"],
        "decision_summary": payload["decision_summary"],
        "findings": findings,
    }


def _rule_id(finding: dict) -> str:
    # ruleId 必须是稳定标识符：root_cause 是自由文本（常为整句中文），把它当
    # 规则号会让 SARIF 规则集按句子爆炸。
    return str(
        finding.get("cwe")
        or finding.get("rule_id")
        or finding.get("case_id")
        or finding.get("id")
    )


def _message(finding: dict) -> str:
    title = str(finding.get("title") or finding["id"])
    reproduction = finding.get("reproduction")
    return f"{title}。复现：{reproduction}" if reproduction else title


def _fingerprint(finding: dict) -> str:
    evidence = finding.get("evidence") or []
    primary = evidence[0] if evidence else {}
    raw = "\0".join((
        _rule_id(finding),
        str(primary.get("path", "")),
        str(primary.get("line", "")),
        str(finding.get("title", "")),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def to_sarif(payload: dict) -> dict:
    payload = final_payload(payload)
    rules: dict[str, dict] = {}
    results: list[dict] = []
    for finding in sorted(payload["findings"], key=lambda item: item["id"]):
        rule_id = _rule_id(finding)
        rules.setdefault(rule_id, {
            "id": rule_id,
            "name": str(finding.get("title") or rule_id),
            "shortDescription": {"text": str(finding.get("title") or rule_id)},
            "properties": {"security-severity": str({
                "critical": 9.5, "high": 8.0, "medium": 5.0, "low": 2.0,
            }[str(finding["severity"]).lower()])},
        })
        locations = []
        for evidence in finding.get("evidence", []):
            location: dict = {
                "physicalLocation": {
                    "artifactLocation": {"uri": evidence["path"].replace("\\", "/")},
                }
            }
            if isinstance(evidence.get("line"), int):
                location["physicalLocation"]["region"] = {
                    "startLine": evidence["line"],
                }
            locations.append(location)
        results.append({
            "ruleId": rule_id,
            "level": LEVELS[str(finding["severity"]).lower()],
            "message": {"text": _message(finding)},
            "locations": locations,
            "partialFingerprints": {"bugx/v1": _fingerprint(finding)},
            "properties": {
                "findingId": finding["id"],
                "severity": str(finding["severity"]).lower(),
                "confidence": finding["confidence"],
                "reproducible": finding.get("reproducible") is True,
                "runId": payload.get("run_id"),
                **({"tools": list(finding["tools"])} if finding.get("tools") else {}),
            },
        })
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "BugX",
                "semanticVersion": VERSION,
                "informationUri": "https://github.com/Bencibr/bugx",
                "rules": list(rules.values()),
            }},
            "invocations": [{"executionSuccessful": True}],
            "results": results,
        }],
    }


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


RECEIPT_START_TAG = "<!-- BUGX-EVIDENCE-RECEIPT-START"
RECEIPT_END_TAG = "BUGX-EVIDENCE-RECEIPT-END -->"


class ReceiptError(agent_eval.EvalError):
    """BugX 报告防伪收据校验失败 (DOC-216)。"""


def make_evidence_receipt(payload: dict) -> dict[str, object]:
    """生成具备防伪哈希的报告收据 (DOC-216 / G3 门禁)。"""
    run_id = str(payload.get("run_id") or "unbound")
    findings = payload.get("findings", [])
    duration = payload.get("duration_seconds", 0)
    tokens = payload.get("token_count", 0)
    salt = f"{run_id}:{duration}:{tokens}:{len(findings)}"
    digest = hashlib.sha256(salt.encode("utf-8")).hexdigest()
    return {
        "receipt_version": 1,
        "run_id": run_id,
        "findings_count": len(findings),
        "token_count": tokens,
        "duration_seconds": duration,
        "digest": digest,
    }


def verify_markdown_receipt(text: str) -> dict[str, object]:
    """从 Markdown 文本中提取并强验防伪收据 (G3 / G4 门禁)。"""
    if RECEIPT_START_TAG not in text or RECEIPT_END_TAG not in text:
        raise ReceiptError("报告缺少 BugX 证据凭据块 (Receipt)；拒绝非 BugX 协议产物")
    try:
        start = text.index(RECEIPT_START_TAG) + len(RECEIPT_START_TAG)
        end = text.index(RECEIPT_END_TAG, start)
        raw_json = text[start:end].strip()
        receipt = json.loads(raw_json)
    except Exception as exc:
        raise ReceiptError(f"防伪收据损坏或非合法 JSON: {exc}") from exc
    if not isinstance(receipt, dict):
        raise ReceiptError("防伪收据格式无效")
    for key in ("receipt_version", "run_id", "findings_count", "token_count", "duration_seconds", "digest"):
        if key not in receipt:
            raise ReceiptError(f"防伪收据缺失关键字段: {key}")
    salt = f"{receipt['run_id']}:{receipt['duration_seconds']}:{receipt['token_count']}:{receipt['findings_count']}"
    expected_digest = hashlib.sha256(salt.encode("utf-8")).hexdigest()
    if receipt["digest"] != expected_digest:
        raise ReceiptError("防伪收据哈希不匹配，存在篡改风险")
    return receipt


def to_markdown(payload: dict) -> str:
    payload = final_payload(payload)
    lines = [
        "# BugX Findings",
        "",
        f"Run ID: `{_markdown_cell(payload.get('run_id') or 'unknown')}`  ",
        f"耗时: {payload['duration_seconds']} 秒 · Token: {payload['token_count']}",
        "",
        "| ID | 严重度 | 标题 | 置信度 | 证据 | 工具归因 | 可复现 |",
        "|---|---|---|---:|---|---|---|",
    ]
    for finding in sorted(payload["findings"], key=lambda item: item["id"]):
        evidence = ", ".join(
            f"{item['path']}:{item['line']}" if item.get("line") else item["path"]
            for item in finding.get("evidence", [])
        )
        tools = ", ".join(finding["tools"]) if finding.get("tools") else "—"
        lines.append(
            f"| {_markdown_cell(finding['id'])} | {_markdown_cell(finding['severity'])} | "
            f"{_markdown_cell(finding.get('title', finding['id']))} | "
            f"{float(finding['confidence']):.2f} | {_markdown_cell(evidence)} | "
            f"{_markdown_cell(tools)} | "
            f"{'是' if finding.get('reproducible') is True else '否'} |"
        )
    receipt = make_evidence_receipt(payload)
    lines.append("")
    lines.append(RECEIPT_START_TAG)
    lines.append(json.dumps(receipt, ensure_ascii=False, indent=2))
    lines.append(RECEIPT_END_TAG)
    return "\n".join(lines) + "\n"


def to_defectdojo(payload: dict) -> dict:
    payload = final_payload(payload)
    findings = []
    severity_names = {
        "critical": "Critical", "high": "High", "medium": "Medium", "low": "Low",
    }
    for finding in sorted(payload["findings"], key=lambda item: item["id"]):
        evidence = finding.get("evidence") or []
        primary = evidence[0] if evidence else {}
        description = str(finding.get("root_cause") or finding.get("title") or finding["id"])
        exported = {
            "title": str(finding.get("title") or finding["id"]),
            "description": description,
            "severity": severity_names[str(finding["severity"]).lower()],
            "active": finding.get("status") not in {"resolved", "fixed", "false_positive"},
            "verified": finding.get("reproducible") is True and float(finding["confidence"]) >= 0.8,
            "false_p": finding.get("status") == "false_positive",
            "is_mitigated": finding.get("status") in {"resolved", "fixed"},
            "scanner_confidence": round(float(finding["confidence"]) * 100),
            "unique_id_from_tool": finding["id"],
            "vuln_id_from_tool": _rule_id(finding),
            "static_finding": True,
            "dynamic_finding": finding.get("reproducible") is True,
            "tags": ["bugx", f"run:{payload.get('run_id') or 'unknown'}"],
        }
        if finding.get("tools"):
            exported["tags"].extend(f"tool:{name}" for name in finding["tools"])
        if finding.get("reproduction"):
            exported["steps_to_reproduce"] = str(finding["reproduction"])
        if primary.get("path"):
            exported["file_path"] = primary["path"]
        if isinstance(primary.get("line"), int):
            exported["line"] = primary["line"]
        findings.append(exported)
    return {
        "name": f"BugX {payload.get('run_id') or 'audit'}",
        "type": "BugX",
        "findings": findings,
    }


def diff_reports(payload_a: dict, payload_b: dict) -> dict:
    """两轮 findings 的三分类对比：新发 / 依旧存在 / 消失（候选回归）。

    指纹沿用 SARIF 的稳定规则（根因+首证据位置+标题）；消失项不代表
    已修复——只是本轮没发现，与 lifecycle 的 unverified 语义一致，
    交给 fix_watchlist / regression_pack 做存在性复核。
    """
    def index(payload: dict) -> dict[str, dict]:
        return {
            _fingerprint(finding): finding
            for finding in payload.get("findings", [])
        }
    left, right = index(payload_a), index(payload_b)
    diff_res: dict[str, Any] = {
        "schema_version": 1,
        "evaluation": "findings-diff",
        "run_a": payload_a.get("run_id"),
        "run_b": payload_b.get("run_id"),
        "persisted": [
            {
                "fingerprint": key,
                "id": right[key].get("id"),
                "title": right[key].get("title"),
            }
            for key in sorted(set(left) & set(right))
        ],
        "new_in_b": [
            {
                "fingerprint": key, "id": right[key].get("id"),
                "title": right[key].get("title"),
            }
            for key in sorted(set(right) - set(left))
        ],
        "missing_in_b": [
            {
                "fingerprint": key, "id": left[key].get("id"),
                "title": left[key].get("title"),
                "note": "本轮未发现≠已修复；用 fix_watchlist/regression_pack 复核",
            }
            for key in sorted(set(left) - set(right))
        ],
        "summary": {
            "persisted": len(set(left) & set(right)),
            "new_in_b": len(set(right) - set(left)),
            "missing_in_b": len(set(left) - set(right)),
        },
    }

    # DOC-065: 串联跨轮次 Finding 生命周期状态自动收敛
    try:
        from core.ledger import finding_lifecycle
        reconciled = finding_lifecycle.reconcile(dict(payload_b), dict(payload_a))
        diff_res["lifecycle_reconciled"] = reconciled
        diff_res["lifecycle_summary"] = reconciled.get("summary", {})
    except Exception:
        pass

    return diff_res


def render(payload: dict, output_format: str) -> str:
    if output_format == "markdown":
        return to_markdown(payload)
    value = to_sarif(payload) if output_format == "sarif" else (
        to_defectdojo(payload) if output_format == "defectdojo" else normalized_payload(payload)
    )
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "export", "diff", "verify-report"), nargs="?",
                        default="validate")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-b", type=Path,
                        help="diff 第二输入：两轮 findings 的三分类对比")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=("json", "markdown", "sarif", "defectdojo"),
                        default="sarif")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify-report":
            text = args.input.read_text(encoding="utf-8")
            receipt = verify_markdown_receipt(text)
            print(
                f"[finding_report] OK: 凭据有效，Run ID: {receipt['run_id']}, "
                f"Findings: {receipt['findings_count']}, Digest: {receipt['digest'][:16]}..."
            )
            return 0
        if args.command == "diff":
            if args.input_b is None:
                raise agent_eval.EvalError("diff 必须提供 --input-b")
            payload_a = load_and_validate(args.input, args.root)
            payload_b = load_and_validate(args.input_b, args.root)
            result = diff_reports(payload_a, payload_b)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        payload = load_and_validate(args.input, args.root)
        if args.command == "validate":
            decision = finding_confirmation.gate(payload)
            print(
                f"[finding_report] OK: {len(payload['findings'])} 条发现协议与证据有效；"
                f"confirmed={decision['confirmed']} rejected={decision['rejected']} "
                f"pending={decision['pending']}"
            )
            return 0
        content = render(payload, args.format)
        if args.output:
            write_atomic(args.output, content)
            print(f"[finding_report] 已导出 {args.format}: {args.output}")
        else:
            sys.stdout.write(content)
        return 0
    except (agent_eval.EvalError, OSError) as exc:
        print(f"[finding_report] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
