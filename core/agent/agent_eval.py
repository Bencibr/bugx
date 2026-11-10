#!/usr/bin/env python3
"""BugX Agent 行为评测器。

它不调用特定模型，而是对任意 Agent 运行产出的结构化 findings 统一评分：
发现率、精确率、证据有效率、可复现率、耗时和 token。CI 使用固定 golden
golden 产物验证评分器；协议示例只验证 runner 桥接。真实 Agent 质量必须由
独立评测集和真实 runner 比较候选运行与基线，不能由示例 runner 自证。
"""

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
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# QUA-03：文件哈希只保留一份实现（``symbol_index.file_sha256``）。
# 本模块与 ``symbol_index`` 都在隔离启动目录的复制清单内，因此该导入
# 在「core/ 被整体复制到临时目录单独运行」的场景下同样可用。
AGENT_DIR = Path(__file__).resolve().parent
if ".bugx" in AGENT_DIR.parts:
    idx = AGENT_DIR.parts.index(".bugx")
    REPO_ROOT = Path(*AGENT_DIR.parts[:idx])
elif AGENT_DIR.name == "agent" and AGENT_DIR.parent.name == "core":
    REPO_ROOT = AGENT_DIR.parent.parent
else:
    REPO_ROOT = AGENT_DIR.parent

_CORE_DIR = AGENT_DIR.parent if AGENT_DIR.name == "agent" else AGENT_DIR
for _p in (str(_CORE_DIR), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.retrieval import symbol_index
_EVALS_DIR = (REPO_ROOT / "verification" / "evals") if (REPO_ROOT / "verification" / "evals").is_dir() else (REPO_ROOT / "evals")
DEFAULT_CASES = _EVALS_DIR / "cases.json"
DEFAULT_FINDINGS = _EVALS_DIR / "golden-findings.json"
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
# BX-ENGINE-18：finding 严重度的**单一真源**。此前存在三处互不一致的声明——
# 本表（无 info）、src/bugx_cli/commands/patch.py（补丁域，无 critical）、
# MCP schema（无 enum，任意 string）——导致真实项目里 1 条 severity="info" 的
# finding 让确认门 `validate_findings` 抛错、整门 24 条全部不可确认。
#
# 约定：
#   * 本表是 **finding 域** 的唯一真源，MCP schema 的 severity enum 由它渲染；
#   * `bugx patch` 的 severity 属**补丁域**（独立词表），不得与本表合并；
#   * `info` 置 0：信息性发现不满足任何 >= low 的最低严重度要求，语义正确。
FINDING_SEVERITY_LEVELS = tuple(sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get))
QUALITY_METRICS = (
    "recall", "precision", "evidence_validity", "reproducibility", "quality_score",
)
SAFE_ENV_NAMES = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONIOENCODING", "SYSTEMROOT",
    "COMSPEC", "PATHEXT", "TMPDIR", "TEMP", "TMP",
}
DEFAULT_BATCH_SIZE = 8
DEFAULT_MAX_PROMPT_BYTES = 512 * 1024
MAX_PROMPT_BYTES = 8 * 1024 * 1024
MAX_BASELINE_FIXTURE_BYTES = 16 * 1024 * 1024


class EvalError(ValueError):
    """评测输入协议错误。"""


def load_json(path: Path) -> dict:
    if not path.exists():
        p_str = str(path)
        if "/evals/" in p_str:
            alt = Path(p_str.replace("/evals/", "/verification/evals/"))
            if alt.exists():
                path = alt
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalError(f"无法读取 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"JSON 无法解析 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvalError(f"{path} 根节点必须是对象")
    return value


def _resolve_in_root(raw: str, root: Path) -> Path | None:
    path = Path(raw)
    if path.is_absolute():
        return None
    try:
        resolved_root = root.resolve(strict=True)
        candidate = root / path
        if not candidate.exists() and raw.startswith("evals/") and (root / "verification" / path).exists():
            candidate = root / "verification" / path
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return resolved


def validate_cases(payload: dict, root: Path = REPO_ROOT) -> list[dict]:
    if payload.get("schema_version") != 1:
        raise EvalError("cases.schema_version 必须为 1")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalError("cases 必须是非空数组")
    seen: set[str] = set()
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            raise EvalError(f"case #{index} 必须是对象")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise EvalError(f"case #{index} 缺 id")
        if case_id in seen:
            raise EvalError(f"case id 重复: {case_id}")
        seen.add(case_id)
        fixture = case.get("fixture")
        if not isinstance(fixture, str) or _resolve_in_root(fixture, root) is None:
            raise EvalError(f"{case_id} fixture 不存在或逃出仓库: {fixture}")
        minimum = str(case.get("minimum_severity", "")).lower()
        if minimum not in SEVERITY_ORDER:
            raise EvalError(f"{case_id} minimum_severity 无效")
        if not isinstance(case.get("root_cause"), str) or not case["root_cause"]:
            raise EvalError(f"{case_id} 缺 root_cause")
    thresholds = payload.get("thresholds")
    if not isinstance(thresholds, dict):
        raise EvalError("cases.thresholds 必须是对象")
    for key in ("min_recall", "min_precision", "min_evidence_validity", "min_reproducibility"):
        value = thresholds.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            raise EvalError(f"thresholds.{key} 必须在 0..1")
    for key in (
        "max_false_positive_rate", "max_duplicate_rate",
        "max_confidence_brier_score", "min_confirmation_rate",
    ):
        if key not in thresholds:
            continue
        value = thresholds[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
            raise EvalError(f"thresholds.{key} 必须在 0..1")
    return cases


def finding_violations(finding: object) -> list[str]:
    """返回单条 finding 的**全部**契约违规（不抛错）。

    BX-ENGINE-18：确认门需要「逐条判定」——单条数据问题只应记为该条的 issues，
    不得让整门失败。本函数是逐条检查的**唯一实现**；:func:`validate_findings`
    复用它并保持 fail-fast（抛首个违规），两者不会漂移。
    """
    try:
        from core.ledger import finding_confirmation
    except ImportError:
        from core import finding_confirmation

    if not isinstance(finding, dict):
        return ["finding 必须是对象"]
    issues: list[str] = []
    finding_id = finding.get("id")
    if not isinstance(finding_id, str) or not finding_id:
        issues.append("finding 缺 id")
        finding_id = "<unknown>"
    confirmation = finding.get("confirmation")
    confirmation_state = (
        confirmation.get("state") if isinstance(confirmation, dict) else "candidate"
    )
    severity_value = finding.get("severity")
    severity = str(severity_value or "").lower()
    if severity not in SEVERITY_ORDER and not (
        severity_value is None and confirmation_state != "confirmed"
    ):
        issues.append(f"{finding_id} severity 无效")
    for field in ("title", "root_cause"):
        value = finding.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"{finding_id} 缺 {field}")
    confidence = finding.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
            or not 0 <= confidence <= 1:
        issues.append(f"{finding_id} confidence 必须在 0..1")
    evidence = finding.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        issues.append(f"{finding_id} evidence 必须是非空数组")
    else:
        for evidence_index, item in enumerate(evidence, 1):
            if not isinstance(item, dict):
                issues.append(f"{finding_id} evidence #{evidence_index} 必须是对象")
                continue
            if not isinstance(item.get("path"), str) or not item["path"].strip():
                issues.append(f"{finding_id} evidence #{evidence_index} 缺 path")
            line = item.get("line")
            if line is not None and (
                not isinstance(line, int) or isinstance(line, bool) or line < 1
            ):
                issues.append(f"{finding_id} evidence #{evidence_index} line 必须是正整数")
    violation = finding_confirmation.reproducible_contract_violation(finding)
    if violation == "not_bool":
        issues.append(f"{finding_id} reproducible 必须是布尔值")
    remediation = finding.get("remediation")
    if remediation is not None and not isinstance(remediation, dict):
        issues.append(f"{finding_id} remediation 必须是对象")
    if violation == "missing_reproduction":
        issues.append(f"{finding_id} 标为可复现时必须提供 reproduction")
    return issues


def validate_findings(payload: dict) -> list[dict]:
    if payload.get("schema_version") != 1:
        raise EvalError("findings.schema_version 必须为 1")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise EvalError("findings 必须是数组")
    duration = payload.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
        raise EvalError("findings.duration_seconds 必须是非负数")
    token_count = payload.get("token_count")
    if token_count is not None and (not isinstance(token_count, int) or isinstance(token_count, bool) or token_count < 0):
        raise EvalError("findings.token_count 必须是非负整数或 null")
    seen: set[str] = set()
    for index, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            raise EvalError(f"finding #{index} 必须是对象")
        # id 缺失/重复的文案含序号，保持既有契约不变（逐条检查器不含序号）。
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not finding_id:
            raise EvalError(f"finding #{index} 缺 id")
        if finding_id in seen:
            raise EvalError(f"finding id 重复: {finding_id}")
        seen.add(finding_id)
        violations = finding_violations(finding)
        if violations:
            raise EvalError(violations[0])
    return findings


def _evidence_item_valid(item: object, root: Path) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        return False
    resolved = _resolve_in_root(item["path"], root)
    if resolved is None or not resolved.is_file():
        return False
    line = item.get("line")
    if line is None:
        return True
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return False
    try:
        with resolved.open("r", encoding="utf-8", errors="replace") as handle:
            return line <= sum(1 for _ in handle)
    except OSError:
        return False


def invalid_evidence_ids(findings: list[dict], root: Path = REPO_ROOT) -> list[str]:
    """列出没有任何真实仓库内证据文件的 finding。"""
    return [
        finding["id"] for finding in findings
        if not any(_evidence_item_valid(item, root) for item in finding["evidence"])
    ]


def _hard_confirmed(finding: dict) -> bool:
    """Count only findings that pass the shared confirmation gate."""
    try:
        try:
            from core.ledger import finding_confirmation
        except ImportError:
            from core import finding_confirmation

        return finding_confirmation.is_confirmed(finding)
    except (ImportError, TypeError, ValueError):
        return False


def score(cases_payload: dict, findings_payload: dict,
          root: Path = REPO_ROOT) -> dict:
    cases = validate_cases(cases_payload, root)
    findings = validate_findings(findings_payload)
    case_by_id = {case["id"]: case for case in cases}
    matched: dict[str, list[dict]] = {}
    for finding in findings:
        case_id = finding.get("case_id")
        if case_id in case_by_id:
            matched.setdefault(case_id, []).append(finding)

    found_ids: set[str] = set()
    evidence_ids: set[str] = set()
    reproducible_ids: set[str] = set()
    qualified_counts: dict[str, int] = {}
    correct_finding_ids: set[str] = set()
    true_positive_count = 0
    for case_id, candidates in matched.items():
        case = case_by_id[case_id]
        minimum = SEVERITY_ORDER[case["minimum_severity"].lower()]
        qualified = [
            finding for finding in candidates
            if finding.get("root_cause") == case["root_cause"]
            and SEVERITY_ORDER[str(finding["severity"]).lower()] >= minimum
        ]
        if not qualified:
            continue
        # 每 case 只计 1 个 TP；case 内多余的正确 finding 属于重复挖掘，
        # 计入 FP，否则低精确率会被「同一 case 堆多个正确 finding」掩盖。
        true_positive_count += 1
        qualified_counts[case_id] = len(qualified)
        correct_finding_ids.add(qualified[0]["id"])
        found_ids.add(case_id)
        if any(
            any(_evidence_item_valid(item, root) for item in finding["evidence"])
            for finding in qualified
        ):
            evidence_ids.add(case_id)
        if any(
            finding.get("reproducible") is True
            and isinstance(finding.get("reproduction"), str)
            and bool(finding["reproduction"].strip())
            for finding in qualified
        ):
            reproducible_ids.add(case_id)

    total_cases = len(cases)
    total_findings = len(findings)
    found = len(found_ids)
    recall = found / total_cases if total_cases else 0.0
    # finding 级精确率：正确命中（每 case 一个）÷ 全部 findings。
    # 旧实现用 found_cases 当分子，会把「同一 case 堆多个正确 finding」
    # 误判为低精度，同时 FPR 却漏计这些冗余——metric 失真。
    precision = true_positive_count / total_findings if total_findings else 0.0
    evidence_validity = len(evidence_ids) / found if found else 0.0
    reproducibility = len(reproducible_ids) / found if found else 0.0
    # FP = 未命中 case 的 finding + case 内超出第一个的正确 finding。
    false_positive_count = (total_findings - true_positive_count)
    duplicate_count = sum(max(0, count - 1) for count in qualified_counts.values())
    false_positive_rate = false_positive_count / total_findings if total_findings else 0.0
    duplicate_rate = duplicate_count / total_findings if total_findings else 0.0
    brier_score = sum(
        (float(finding["confidence"]) - (1.0 if finding["id"] in correct_finding_ids else 0.0)) ** 2
        for finding in findings
    ) / total_findings if total_findings else 0.0
    confirmed_findings = sum(_hard_confirmed(finding) for finding in findings)
    heuristic_only_findings = sum(
        isinstance(finding.get("confirmation"), dict)
        and isinstance(finding["confirmation"].get("oracle"), dict)
        and finding["confirmation"]["oracle"].get("tier") == "heuristic"
        for finding in findings
    )
    # 按根因类别的 recall：哪类错题挖不动一目了然（供 seed_drill/复盘对齐）。
    root_cause_buckets: dict[str, dict[str, int]] = {}
    for case in cases:
        bucket = root_cause_buckets.setdefault(
            str(case["root_cause"]), {"cases": 0, "found": 0},
        )
        bucket["cases"] += 1
        if case["id"] in found_ids:
            bucket["found"] += 1
    recall_by_root_cause = {
        cause: round(bucket["found"] / bucket["cases"], 4)
        for cause, bucket in sorted(root_cause_buckets.items())
        if bucket["cases"]
    }
    weighted = (
        recall * 0.40 + precision * 0.25
        + evidence_validity * 0.20 + reproducibility * 0.15
    )
    return {
        "cases_total": total_cases,
        "cases_found": found,
        "findings_total": total_findings,
        "missed_case_ids": sorted(set(case_by_id) - found_ids),
        "recall": round(recall, 4),
        "recall_by_root_cause": recall_by_root_cause,
        "precision": round(precision, 4),
        "evidence_validity": round(evidence_validity, 4),
        "reproducibility": round(reproducibility, 4),
        "false_positive_count": false_positive_count,
        "false_positive_rate": round(false_positive_rate, 4),
        "duplicate_count": duplicate_count,
        "duplicate_rate": round(duplicate_rate, 4),
        "confidence_brier_score": round(brier_score, 4),
        "confirmed_findings": confirmed_findings,
        "confirmation_rate": round(
            confirmed_findings / total_findings, 4,
        ) if total_findings else 0.0,
        "heuristic_only_findings": heuristic_only_findings,
        "quality_score": round(weighted * 100, 2),
        "duration_seconds": round(float(findings_payload["duration_seconds"]), 3),
        "token_count": int(findings_payload["token_count"]),
        "finding_confidence_median": round(statistics.median(
            [float(item["confidence"]) for item in findings]
        ), 4) if findings else 0.0,
    }


def threshold_failures(metrics: dict, thresholds: dict) -> list[str]:
    mapping = {
        "min_recall": "recall",
        "min_precision": "precision",
        "min_evidence_validity": "evidence_validity",
        "min_reproducibility": "reproducibility",
    }
    failures = []
    for threshold_name, metric_name in mapping.items():
        expected = float(thresholds[threshold_name])
        actual = float(metrics[metric_name])
        if actual < expected:
            failures.append(
                f"{metric_name}={actual:.4f} 低于门槛 {expected:.4f}"
            )
    for threshold_name, metric_name in (
        ("max_false_positive_rate", "false_positive_rate"),
        ("max_duplicate_rate", "duplicate_rate"),
        ("max_confidence_brier_score", "confidence_brier_score"),
    ):
        if threshold_name not in thresholds:
            continue
        expected = float(thresholds[threshold_name])
        actual = float(metrics[metric_name])
        if actual > expected:
            failures.append(f"{metric_name}={actual:.4f} 高于门槛 {expected:.4f}")
    if "min_confirmation_rate" in thresholds:
        expected = float(thresholds["min_confirmation_rate"])
        actual = float(metrics["confirmation_rate"])
        if actual < expected:
            failures.append(f"confirmation_rate={actual:.4f} 低于门槛 {expected:.4f}")
    return failures


def canonical_digest(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fixture_sha256(path: Path | None) -> str:
    """计算评测 fixture 的 SHA-256，并施加基线摘要的体积上限。

    QUA-03：分块哈希本身委托给全仓唯一实现 ``symbol_index.file_sha256``
    （同为 1 MiB 分块）；本函数只保留**本域特有**的两件事——体积上限与
    ``EvalError`` 语义的异常转换。这样摘要算法只在一处维护。
    """
    if path is None:
        raise EvalError("评测 fixture 不存在或逃出仓库根目录")
    try:
        if path.stat().st_size > MAX_BASELINE_FIXTURE_BYTES:
            raise EvalError(
                f"评测 fixture 超过基线摘要上限: {path} > {MAX_BASELINE_FIXTURE_BYTES} bytes"
            )
        return symbol_index.file_sha256(path)
    except OSError as exc:
        raise EvalError(f"无法读取评测 fixture: {path}: {exc}") from exc


def metrics_record(metrics: dict, cases_payload: dict,
                   findings_payload: dict, root: Path = REPO_ROOT) -> dict:
    """生成可持久化、可比较且不包含评测答案的基线记录。"""
    cases = validate_cases(cases_payload, root)
    cases_fingerprint = {
        "schema_version": cases_payload.get("schema_version"),
        "thresholds": cases_payload.get("thresholds"),
        "cases": [
            {
                "id": case["id"],
                "fixture": case["fixture"],
                "fixture_sha256": _fixture_sha256(_resolve_in_root(case["fixture"], root)),
            }
            for case in cases
        ],
    }
    return {
        "schema_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cases_digest": canonical_digest(cases_fingerprint),
        "findings_digest": canonical_digest(findings_payload),
        "metrics": dict(metrics),
    }


def baseline_failures(metrics: dict, baseline_payload: dict,
                      tolerance: float = 0.0) -> list[str]:
    if not 0 <= tolerance <= 1:
        raise EvalError("baseline tolerance 必须在 0..1")
    baseline = baseline_payload.get("metrics")
    if baseline_payload.get("schema_version") != 1 or not isinstance(baseline, dict):
        raise EvalError("baseline metrics 协议无效")
    failures: list[str] = []
    for name in QUALITY_METRICS:
        expected = baseline.get(name)
        actual = metrics.get(name)
        if not isinstance(expected, (int, float)) or isinstance(expected, bool):
            raise EvalError(f"baseline metrics 缺有效 {name}")
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            raise EvalError(f"当前 metrics 缺有效 {name}")
        allowed_drop = tolerance * (100.0 if name == "quality_score" else 1.0)
        if float(actual) + allowed_drop < float(expected):
            failures.append(
                f"{name}={float(actual):.4f} 相比基线 {float(expected):.4f} 回退"
            )
    return failures


def _bounded_source(source: str, max_bytes: int | None) -> tuple[str, bool, int]:
    encoded = source.encode("utf-8")
    original_bytes = len(encoded)
    if max_bytes is None or original_bytes <= max_bytes:
        return source, False, original_bytes
    marker = "\n\n[bugx] source truncated; audit only the supplied prefix.\n"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        raise EvalError("max_source_bytes 太小，无法保留截断标记")
    prefix = encoded[:max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return prefix + marker, True, original_bytes


def build_runner_request(cases_payload: dict, root: Path = REPO_ROOT,
                         case_ids: set[str] | None = None,
                         max_source_bytes: int | None = None) -> dict:
    """构造不含 root_cause 标准答案的 runner 输入。"""
    if max_source_bytes is not None and (
        not isinstance(max_source_bytes, int)
        or isinstance(max_source_bytes, bool)
        or max_source_bytes < 1024
    ):
        raise EvalError("max_source_bytes 必须为至少 1024 的整数")
    cases = validate_cases(cases_payload, root)
    if case_ids is not None:
        unknown = set(case_ids) - {case["id"] for case in cases}
        if unknown:
            raise EvalError("未知 case_id: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in case_ids]
    if not cases:
        raise EvalError("runner 请求至少需要一个 case")
    public_cases = []
    for case in cases:
        fixture = _resolve_in_root(case["fixture"], root)
        if fixture is None:
            raise EvalError(f"fixture 不可读取: {case['fixture']}")
        try:
            source = fixture.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise EvalError(f"fixture 不可读取: {case['fixture']}: {exc}") from exc
        bounded, truncated, source_bytes = _bounded_source(source, max_source_bytes)
        public_cases.append({
            "id": case["id"],
            "fixture": case["fixture"],
            "source": bounded,
            "source_bytes": source_bytes,
            "source_truncated": truncated,
        })
    return {
        "schema_version": 1,
        "task": "audit",
        "output_contract": {
            "schema_version": 1,
            "required_top_level": ["token_count", "findings"],
            "required_finding_fields": [
                "id", "case_id", "title", "root_cause", "severity", "confidence",
                "evidence", "reproducible",
            ],
            "conditional_fields": {
                "reproduction": "required when reproducible is true",
            },
        },
        "cases": public_cases,
    }


def build_runner_requests(cases_payload: dict, root: Path = REPO_ROOT,
                          batch_size: int = DEFAULT_BATCH_SIZE,
                          max_prompt_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
                          max_source_bytes: int | None = None) -> list[dict]:
    """按 case 数量和 JSON 大小上限构造确定性 runner 批次。"""
    if isinstance(batch_size, bool) or batch_size < 1:
        raise EvalError("batch_size 必须大于 0")
    if (isinstance(max_prompt_bytes, bool)
            or max_prompt_bytes < 1024 or max_prompt_bytes > MAX_PROMPT_BYTES):
        raise EvalError(
            f"max_prompt_bytes 必须在 1024..{MAX_PROMPT_BYTES}"
        )
    request = build_runner_request(
        cases_payload, root, max_source_bytes=max_source_bytes,
    )
    template = dict(request)
    template["cases"] = []
    fixed_bytes = len(json.dumps(template, ensure_ascii=False, separators=(",", ":")))
    batches: list[list[dict]] = []
    current: list[dict] = []
    for case in request["cases"]:
        candidate = [*current, case]
        candidate_bytes = fixed_bytes + len(
            json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        )
        if not current and candidate_bytes > max_prompt_bytes:
            raise EvalError(
                f"case {case['id']} 超过 max_prompt_bytes ({candidate_bytes} > "
                f"{max_prompt_bytes})；请降低 max_source_bytes 或提高 prompt 上限"
            )
        if current and (len(current) >= batch_size or candidate_bytes > max_prompt_bytes):
            batches.append(current)
            current = [case]
        else:
            current = candidate
    if current:
        batches.append(current)
    total = len(batches)
    result = []
    for index, cases in enumerate(batches, 1):
        batch = dict(request)
        batch["cases"] = cases
        batch["batch"] = {
            "index": index,
            "total": total,
            "case_count": len(cases),
            "estimated_bytes": fixed_bytes + len(
                json.dumps(cases, ensure_ascii=False, separators=(",", ":"))
            ),
        }
        result.append(batch)
    return result


def _runner_command(runner: str, runner_args: list[str], root: Path = REPO_ROOT) -> list[str]:
    executable = shutil.which(runner)
    if executable is None:
        candidate = Path(runner)
        if candidate.is_file():
            executable = str(candidate.resolve())
    if executable is None:
        raise EvalError(f"runner 不存在或不可执行: {runner}")
    resolved_args: list[str] = []
    for value in runner_args:
        candidate = Path(value)
        rooted = candidate if candidate.is_absolute() else root / candidate
        if rooted.is_file():
            resolved_args.append(str(rooted.resolve()))
        else:
            resolved_args.append(value)
    return [executable, *resolved_args]


def _runner_environment(pass_env: list[str] | None = None) -> dict[str, str]:
    names = SAFE_ENV_NAMES | set(pass_env or [])
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


class _RunnerOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.prefix = bytearray()
        self.total = 0

    @property
    def exceeded(self) -> bool:
        return self.total > self.limit

    def collect(self, stream) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            self.total += len(chunk)
            remaining = self.limit - len(self.prefix)
            if remaining > 0:
                self.prefix.extend(chunk[:remaining])

    def value(self) -> bytes:
        return bytes(self.prefix)


def _terminate_runner(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            process.send_signal(getattr(subprocess, "CTRL_BREAK_EVENT", 1))
        except (OSError, ValueError):
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass


def _run_runner_process(command: list[str], input_text: str, cwd: Path,
                        env: dict[str, str] | None, timeout: float,
                        output_limit: int) -> tuple[int, bool, bool, bytes, bytes]:
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
            close_fds=os.name != "nt",
        )
    except OSError as exc:
        raise EvalError(f"runner 启动失败: {exc}") from exc
    stdout = _RunnerOutput(output_limit)
    stderr = _RunnerOutput(output_limit)
    readers = [
        threading.Thread(target=stdout.collect, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.collect, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()
    input_bytes = input_text.encode("utf-8")

    def write_input() -> None:
        try:
            if process.stdin is not None:
                process.stdin.write(input_bytes)
                process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    output_exceeded = False
    try:
        while process.poll() is None:
            if stdout.exceeded or stderr.exceeded:
                output_exceeded = True
                _terminate_runner(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_runner(process)
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
    finally:
        if timed_out or output_exceeded:
            _terminate_runner(process)
        writer.join(timeout=1)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    output_exceeded = output_exceeded or stdout.exceeded or stderr.exceeded
    return process.returncode or 0, timed_out, output_exceeded, stdout.value(), stderr.value()


def _materialize_runner_workspace(request: dict, workspace: Path) -> None:
    """只写入公开 fixture，不复制 cases/golden 或仓库其他内容。"""
    for case in request["cases"]:
        relative = Path(case["fixture"])
        if relative.is_absolute() or ".." in relative.parts:
            raise EvalError(f"runner fixture 路径不安全: {case['fixture']}")
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(case["source"], encoding="utf-8")


def _isolate_local_runner_files(command: list[str], root: Path,
                                workspace: Path) -> list[str]:
    """复制作为参数传入的仓库内 runner 脚本，避免依赖原仓库 cwd。"""
    isolated = [command[0]]
    runner_dir = workspace / ".runner"
    for index, value in enumerate(command[1:], 1):
        candidate = Path(value)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            isolated.append(value)
            continue
        if not resolved.is_file():
            isolated.append(value)
            continue
        runner_dir.mkdir(parents=True, exist_ok=True)
        target = runner_dir / f"{index}-{resolved.name}"
        shutil.copy2(resolved, target)
        isolated.append(str(target))
    return isolated


def run_agent(cases_payload: dict, runner: str, runner_args: list[str],
              root: Path = REPO_ROOT, timeout_seconds: float = 300.0,
              isolate: bool = True, pass_env: list[str] | None = None,
              max_output_bytes: int = 2_000_000,
              batch_size: int = DEFAULT_BATCH_SIZE,
              max_prompt_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
              max_source_bytes: int | None = None) -> dict:
    """通过 JSON 协议运行 Agent；默认隔离并按 case 成本分批。"""
    if timeout_seconds <= 0:
        raise EvalError("runner timeout 必须大于 0")
    if max_output_bytes < 1024:
        raise EvalError("runner max output 必须至少为 1024 bytes")
    requests = build_runner_requests(
        cases_payload, root, batch_size, max_prompt_bytes, max_source_bytes,
    )
    command = _runner_command(runner, runner_args, root)
    environment = _runner_environment(pass_env) if isolate else None
    started = time.monotonic()
    outputs: list[dict] = []
    reported_durations: list[float] = []
    for batch_index, request in enumerate(requests, 1):
        try:
            with tempfile.TemporaryDirectory(prefix="bugx-runner-") as temporary:
                workspace = Path(temporary)
                batch_command = command
                if isolate:
                    _materialize_runner_workspace(request, workspace)
                    batch_command = _isolate_local_runner_files(command, root, workspace)
                returncode, timed_out, output_exceeded, stdout_bytes, stderr_bytes = (
                    _run_runner_process(
                        batch_command,
                        json.dumps(request, ensure_ascii=False),
                        workspace if isolate else root,
                        environment,
                        timeout_seconds,
                        max_output_bytes,
                    )
                )
        except subprocess.TimeoutExpired as exc:
            raise EvalError(
                f"runner 批次 {batch_index}/{len(requests)} 超时（{timeout_seconds:g} 秒）"
            ) from exc
        except OSError as exc:
            raise EvalError(f"runner 批次 {batch_index} 启动失败: {exc}") from exc
        if timed_out:
            raise EvalError(
                f"runner 批次 {batch_index}/{len(requests)} 超时（{timeout_seconds:g} 秒）"
            )
        if output_exceeded:
            raise EvalError(
                f"runner 批次 {batch_index} 输出过大: "
                f"stdout/stderr 超过 {max_output_bytes} bytes"
            )
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        if returncode != 0:
            detail = stderr_text.strip() or stdout_text.strip() or "无错误输出"
            raise EvalError(
                f"runner 返回 {returncode}（批次 {batch_index}/{len(requests)}）: {detail}"
            )
        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise EvalError(f"runner 批次 {batch_index} stdout 不是纯 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise EvalError("runner 输出根节点必须是对象")
        payload.setdefault("schema_version", 1)
        payload.setdefault("duration_seconds", 0.0)
        payload.setdefault("token_count", 0)
        payload.setdefault("findings", [])
        validate_findings(payload)
        if isinstance(payload["duration_seconds"], (int, float)):
            reported_durations.append(float(payload["duration_seconds"]))
        outputs.append(payload)
    elapsed = round(time.monotonic() - started, 3)
    findings = [finding for payload in outputs for finding in payload["findings"]]
    payload = dict(outputs[0])
    payload["findings"] = findings
    payload["token_count"] = sum(int(item["token_count"]) for item in outputs)
    payload["duration_seconds"] = elapsed
    if reported_durations:
        payload["runner_reported_duration_seconds"] = round(sum(reported_durations), 3)
    payload["runner_execution"] = {
        "workspace_isolated": isolate,
        "environment_allowlist": sorted(_runner_environment(pass_env)) if isolate else None,
        "network_isolated": False,
        "note": "进程隔离不等于容器/虚拟机边界；网络和宿主文件系统仍需外层沙箱",
        "batches": len(requests),
        "batch_case_counts": [len(request["cases"]) for request in requests],
    }
    validate_findings(payload)
    return payload


def _save_json_atomic(path: Path, payload: dict) -> None:
    if not path.parent.is_dir():
        raise EvalError(f"findings 输出目录不存在: {path.parent}")
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _print_score(metrics: dict, failures: list[str]) -> None:
    print("[agent_eval] Agent 行为评测")
    print(f"  发现率: {metrics['cases_found']}/{metrics['cases_total']} "
          f"({metrics['recall'] * 100:.1f}%)")
    print(f"  精确率: {metrics['precision'] * 100:.1f}%")
    print(f"  证据有效率: {metrics['evidence_validity'] * 100:.1f}%")
    print(f"  可复现率: {metrics['reproducibility'] * 100:.1f}%")
    print(f"  误报率: {metrics['false_positive_rate'] * 100:.1f}%")
    print(f"  重复率: {metrics['duplicate_rate'] * 100:.1f}%")
    print(f"  确认率: {metrics['confirmation_rate'] * 100:.1f}%")
    print(f"  综合质量分: {metrics['quality_score']:.2f}/100")
    print(f"  耗时/Token: {metrics['duration_seconds']:.3f}s / {metrics['token_count']}")
    if metrics["missed_case_ids"]:
        print("  漏检: " + ", ".join(metrics["missed_case_ids"]))
    if failures:
        print("[agent_eval] FAIL:")
        for failure in failures:
            print(f"  ✗ {failure}")
    else:
        print("[agent_eval] PASS: 所有行为指标达到基线门槛")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "score", "run"), nargs="?",
                        default="score")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--findings", type=Path, default=DEFAULT_FINDINGS)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--protocol-only", action="store_true",
        help="仅验证 run 的隔离、stdin/stdout JSON 协议与退出码，不计算质量分",
    )
    parser.add_argument("--runner", help="run 命令使用的可执行文件或 PATH 命令")
    parser.add_argument("--runner-arg", action="append", default=[],
                        help="传给 runner 的单个参数，可重复")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--no-isolate", action="store_true",
                        help="兼容模式：runner 使用仓库 cwd 和完整父进程环境")
    parser.add_argument("--pass-env", action="append", default=[],
                        help="隔离模式额外传入的环境变量名，可重复")
    parser.add_argument("--max-output-bytes", type=int, default=2_000_000)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"每个 runner 请求最多 case 数（默认 {DEFAULT_BATCH_SIZE}）")
    parser.add_argument("--max-prompt-bytes", type=int, default=DEFAULT_MAX_PROMPT_BYTES,
                        help="单个 runner 请求的 JSON 字节预算")
    parser.add_argument("--max-source-bytes", type=int,
                        help="显式截断单个 fixture 的 UTF-8 字节数（默认不截断）")
    parser.add_argument("--save-findings", type=Path)
    parser.add_argument("--save-metrics", type=Path)
    parser.add_argument("--baseline-metrics", type=Path)
    parser.add_argument("--regression-tolerance", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        if args.protocol_only and args.command != "run":
            raise EvalError("--protocol-only 仅可用于 run 命令")
        if args.protocol_only and (
            args.strict or args.baseline_metrics is not None
            or args.save_metrics is not None
        ):
            raise EvalError(
                "--protocol-only 不得与 --strict、--baseline-metrics 或 --save-metrics 混用"
            )
        cases_payload = load_json(args.cases)
        validate_cases(cases_payload, args.root)
        if args.command == "run":
            if not args.runner:
                raise EvalError("run 命令必须提供 --runner")
            findings_payload = run_agent(
                cases_payload, args.runner, args.runner_arg, args.root, args.timeout,
                isolate=not args.no_isolate, pass_env=args.pass_env,
                max_output_bytes=args.max_output_bytes,
                batch_size=args.batch_size,
                max_prompt_bytes=args.max_prompt_bytes,
                max_source_bytes=args.max_source_bytes,
            )
            if args.save_findings:
                _save_json_atomic(args.save_findings, findings_payload)
            if args.protocol_only:
                payload = {
                    "ok": True,
                    "mode": "protocol-only",
                    "findings_count": len(findings_payload["findings"]),
                    "runner_execution": findings_payload["runner_execution"],
                }
                if args.json:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print(
                        "[agent_eval] PASS: runner 协议、隔离、JSON 输出与退出码有效；"
                        "未执行质量评测"
                    )
                return 0
        else:
            findings_payload = load_json(args.findings)
        findings = validate_findings(findings_payload)
        if args.command == "validate":
            invalid_evidence = invalid_evidence_ids(findings, args.root)
            if invalid_evidence:
                raise EvalError("finding 缺真实证据: " + ", ".join(invalid_evidence))
            print("[agent_eval] OK: cases/findings 协议与证据路径有效")
            return 0
        metrics = score(cases_payload, findings_payload, args.root)
        failures = threshold_failures(metrics, cases_payload["thresholds"])
        if args.baseline_metrics:
            failures.extend(baseline_failures(
                metrics, load_json(args.baseline_metrics), args.regression_tolerance,
            ))
        if args.save_metrics:
            _save_json_atomic(
                args.save_metrics,
                metrics_record(metrics, cases_payload, findings_payload, args.root),
            )
    except EvalError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[agent_eval] ✗ {exc}")
        return 2
    if args.json:
        print(json.dumps({"ok": not failures, "metrics": metrics, "failures": failures},
                         ensure_ascii=False, indent=2))
    else:
        _print_score(metrics, failures)
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    sys.exit(main())
