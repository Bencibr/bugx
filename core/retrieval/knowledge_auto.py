#!/usr/bin/env python3
"""Automatic project knowledge materialization for ``review_mode=mcp-auto``.

This module only turns confirmed findings into candidate ledger records and
their first bound validation feedback. It does not activate knowledge or
bypass the existing review/confirmation gates.
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
import sys
from pathlib import Path


import re
from typing import Any, Mapping


_CATEGORY_RULES = (
    ("secrets", ("secret", "credential", "password", "api key", "apikey", "凭据", "密钥")),
    ("authorization", ("authorization", "access control", "permission", "privilege", "越权", "权限")),
    ("injection", ("sql injection", "command injection", "template injection", "注入")),
    ("concurrency", ("race condition", "deadlock", "并发", "竞态", "死锁")),
    ("dependency", ("dependency", "third-party", "package", "library", "组件", "依赖")),
    ("api-contract", ("api contract", "schema", "request", "response", "接口契约")),
    ("input-boundary", ("input", "validation", "boundary", "输入", "校验", "边界")),
    ("partial-failure", ("partial failure", "rollback", "timeout", "超时", "回滚")),
    ("ordering", ("ordering", "sequence", "顺序", "时序")),
    ("type-assumption", ("type error", "type assumption", "类型假设", "类型错误")),
    ("performance", ("performance", "slow", "latency", "性能", "延迟")),
    ("ui", ("frontend", "browser", "render", "layout", "界面", "前端", "浏览器")),
    ("regression", ("regression", "回归")),
)
_ID_RE = re.compile(r"^L-[0-9a-f]{12}$")
_SOURCE_REF = "auto://confirmed-finding"
_REDACTION_PLACEHOLDER = re.compile(r"\{\{REDACTED_[A-Z0-9_]+\}\}")
_REDACTION_TOKEN = "{{REDACTED_SECRET_V1}}"
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w-])credentials?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_PATTERN_TEXT_FIELDS = (
    "category", "title", "root_cause", "reflection", "severity", "language",
    "cwe_id", "error_signature", "before_code", "after_code", "fix_summary",
    "repro_oracle",
)
_PATTERN_TEXT_LIST_FIELDS = (
    "probes", "negative_controls", "evidence_requirements", "applicability",
    "false_positive_patterns", "oracle_hints", "tags", "playbook",
    "attempts_tried", "successful_solution", "aliases", "source_refs",
)
_INCIDENT_TEXT_FIELDS = (
    "signature", "summary", "context", "before_snippet", "after_snippet", "decision",
)
_MAX_REDACT_CACHE_SIZE = 2048
_MEMO_REDACT: dict[tuple[str, str | None], Any] = {}


def _quick_redact_text(value: Any, *, language: str | None = None) -> Any:
    """带边界容量缓存的快速脱敏助手 (L5-OPT-3)。"""
    if not isinstance(value, str) or not value:
        return value
    cache_key = (value, language)
    if cache_key in _MEMO_REDACT:
        return _MEMO_REDACT[cache_key]
    from core.governance.secret_redactor import redact_text as _orig_redact
    cleaned = _orig_redact(value, language=language)
    if len(_MEMO_REDACT) >= _MAX_REDACT_CACHE_SIZE:
        _MEMO_REDACT.clear()
    _MEMO_REDACT[cache_key] = cleaned
    return cleaned


def _text(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _thaw_snapshot_value(value: Any) -> Any:
    """Copy an immutable FindingsSnapshot value into validator-compatible containers.

    Findings snapshots are recursively frozen before they cross the settlement
    boundary.  ``finding_confirmation`` intentionally validates the JSON
    protocol using ordinary ``dict``/``list`` containers, so a shallow
    ``dict(item)`` leaves nested ``MappingProxyType`` and tuple values that are
    rejected as malformed.  This copy remains transient: only the sanitized
    projection built later can reach the ledger.
    """
    if isinstance(value, Mapping):
        return {key: _thaw_snapshot_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_snapshot_value(item) for item in value]
    return value


def _category(value: Any) -> str:
    raw = _text(value, 120).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "-", raw, flags=re.UNICODE).strip("-")


def _finding_text(finding: Mapping[str, Any]) -> str:
    values = [
        finding.get(key)
        for key in ("title", "root_cause", "summary", "description", "reproduction", "error_signature", "signature")
    ]
    values.extend(
        item.get("path")
        for item in finding.get("evidence") or []
        if isinstance(item, Mapping)
    )
    return _text(" ".join(str(item) for item in values if item), 6000).casefold()


def classify_finding(finding: Mapping[str, Any], profile: Mapping[str, Any] | None = None) -> str:
    """Return an explicit category first, then a stable keyword category."""
    for key in ("category", "bug_type", "finding_type", "kind"):
        value = _category(finding.get(key))
        if value:
            return value
    haystack = _finding_text(finding)
    for category, keywords in _CATEGORY_RULES:
        if any(keyword.casefold() in haystack for keyword in keywords):
            return category
    return "uncategorized"


def _profile_tags(profile: Mapping[str, Any] | None) -> dict[str, list[str]]:
    if not profile:
        return {}
    try:
        from core.runtime import project_profile
        return project_profile.routing_tags(profile)
    except (ImportError, TypeError, ValueError, AttributeError):
        return {}


def _finding_fingerprint(finding: Mapping[str, Any]) -> str:
    from core.ledger import life_ledger
    return life_ledger.finding_fingerprint(dict(finding))


def _oracle_reference(finding: Mapping[str, Any]) -> str:
    confirmation = finding.get("confirmation")
    oracle = confirmation.get("oracle") if isinstance(confirmation, Mapping) else None
    return _text((oracle or {}).get("reference"), 1000) if isinstance(oracle, Mapping) else ""


def build_pattern_payload(
    finding: Mapping[str, Any], profile: Mapping[str, Any] | None = None,
    *, finding_fingerprints: list[str] | None = None,
) -> dict[str, Any]:
    category = classify_finding(finding, profile)
    title = _quick_redact_text(_text(finding.get("title"), 2000) or "confirmed finding")
    root_cause = _quick_redact_text(_text(finding.get("root_cause"), 4000) or title)
    reproduction = _quick_redact_text(_text(finding.get("reproduction"), 1600))
    oracle = _oracle_reference(finding)
    fingerprints = finding_fingerprints or [_finding_fingerprint(finding)]
    tags = ["auto-generated", category]
    payload: dict[str, Any] = {
        "category": category,
        "title": title,
        "root_cause": root_cause,
        "reflection": "自动复盘：confirmed Finding 已通过确认门禁，等待人工治理晋级。",
        "probes": ["confirmed-finding"],
        "negative_controls": [],
        "evidence_requirements": ["finding_confirmation:confirmed"],
        "applicability": ["project-ledger"],
        "false_positive_patterns": [],
        "oracle_hints": ["confirmation-state:confirmed"],
        "tags": tags,
        "playbook": ["复核 Finding 证据并补充修前/修后决策链"],
        "attempts_tried": [],
        "successful_solution": [],
        "aliases": [],
        "source_refs": [_SOURCE_REF],
        "finding_fingerprints": sorted(set(fingerprints)),
        "status": "candidate",
        "routing_tags": _profile_tags(profile),
        "severity": _text(finding.get("severity"), 20).upper(),
        "language": _text(finding.get("language") or finding.get("language_version"), 50),
        "cwe_id": _text(finding.get("cwe_id"), 20),
        "error_signature": _text(finding.get("error_signature") or finding.get("signature"), 500),
        "before_code": _text(finding.get("before_code"), 4000),
        "after_code": _text(finding.get("after_code"), 4000),
        "fix_summary": _text(finding.get("fix_summary"), 1800),
        "repro_oracle": reproduction or oracle or "confirmed finding",
    }
    return payload


def build_incident_payload(
    finding: Mapping[str, Any], pattern_id: str, runtime: Mapping[str, Any], round_no: int,
    profile: Mapping[str, Any] | None = None, *, finding_fingerprint: str | None = None,
) -> dict[str, Any]:
    from core.ledger import lesson_ledger as ledger

    category = classify_finding(finding, profile)
    title = _quick_redact_text(_text(finding.get("title"), 2000) or "confirmed finding")
    root_cause = _quick_redact_text(_text(finding.get("root_cause"), 3000) or title)
    reproduction = _quick_redact_text(_text(finding.get("reproduction"), 2200) or "confirmed finding")
    signature = _text(
        finding.get("error_signature") or finding.get("signature") or f"{category}: {title}",
        500,
    )
    fingerprint = finding_fingerprint or _finding_fingerprint(finding)
    return {
        # Compute the stable identity from raw text and the raw Finding fingerprint
        # before redaction. Only the opaque ID is persisted.
        "incident_id": ledger.incident_id(
            signature, title, None, finding_fingerprint=fingerprint,
        ),
        "signature": signature,
        "summary": title,
        "context": f"{root_cause}；复现：{reproduction}",
        "before_snippet": _text(finding.get("before_code"), 6000) or "",
        "after_snippet": _text(finding.get("after_code"), 6000) or "",
        "decision": _text(finding.get("fix_summary"), 4000) or "",
        "duration_seconds": 0,
        "lifecycle_id": _text(runtime.get("lifecycle_id"), 200),
        "round": round_no,
        "pattern_id": pattern_id,
        "status": "candidate",
        "routing_tags": _profile_tags(profile),
        "finding_fingerprint": fingerprint,
    }


def _guard(payload: Mapping[str, Any], label: str) -> None:
    from core.retrieval import knowledge_sync
    try:
        knowledge_sync.enforce_credential_block(payload, label=label)
    except Exception:
        # The current guard embeds a credential prefix in its exception. Keep the
        # failure closed while ensuring post/stdout/logs cannot echo Finding data.
        raise ValueError("auto_materialization_credential_blocked") from None


def _load_findings_snapshot(runtime: Mapping[str, Any]):
    from core.launch_phases.finding_settlement import capture_findings_snapshot

    return capture_findings_snapshot(dict(runtime))


def load_confirmed_findings(runtime: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return confirmed inputs from one validated, bounded Finding snapshot."""
    snapshot = _load_findings_snapshot(runtime)
    if snapshot is None:
        return []
    from core.ledger import finding_confirmation
    return [dict(item) for item in snapshot.findings if finding_confirmation.is_confirmed(item)]


def _plans(
    path: str | Path, findings: list[dict[str, Any]], profile: Mapping[str, Any] | None,
    *, ledger_state: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from core.ledger import lesson_ledger as ledger

    stored = (ledger_state or ledger.read(path)).get("lessons", {})
    by_fingerprint = {
        fingerprint: key
        for key, item in stored.items()
        for fingerprint in item.get("finding_fingerprints", [])
    }
    planned: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, Any]] = {}
    for finding in findings:
        fingerprint = _finding_fingerprint(finding)
        confirmation_digest = ledger.confirmed_finding_digest({"finding": finding})
        category = classify_finding(finding, profile)
        # DOC-203 缺陷修补（build.49）：root_cause/title 是 sync 身份锁字段，
        # 修复轮之前无法改写；当它们引用字面示例凭据形态（如 root_cause 描述
        # "凭据回显"缺陷时必引 sk- 形态示例）时，复盘 Pattern 的凭据扫描与
        # sync 身份锁互斥、post 永远无法收敛。Pattern 文本入账前先走引擎
        # 统一脱敏（typed {{REDACTED_*}} 是 enforce_credential_block 明文
        # 放行形态），技术语义保留、凭据形态消除。
        title = _quick_redact_text(
            _text(finding.get("title"), 2000) or "confirmed finding")
        root = _quick_redact_text(
            _text(finding.get("root_cause"), 4000)) or title
        explicit = finding.get("pattern_id")
        pattern_id = str(explicit) if isinstance(explicit, str) and _ID_RE.fullmatch(explicit) else None
        pattern_id = pattern_id or by_fingerprint.get(fingerprint)
        pattern_id = pattern_id or ledger.lesson_id(category, title, root)
        plan = {
            "finding": finding, "fingerprint": fingerprint,
            "confirmation_digest": confirmation_digest, "pattern_id": pattern_id,
        }
        planned.append(plan)
        if pattern_id not in grouped:
            grouped[pattern_id] = build_pattern_payload(
                finding, profile, finding_fingerprints=[fingerprint],
            )
            grouped[pattern_id]["pattern_id"] = pattern_id
        else:
            grouped[pattern_id]["finding_fingerprints"].append(fingerprint)
    return planned, {key: value for key, value in grouped.items() if key not in stored}


def _confirmed_snapshot_inputs(
    runtime: Mapping[str, Any], round_result: Mapping[str, Any], snapshot: Any,
) -> list[dict[str, Any]]:
    from core.ledger import finding_confirmation

    settled_round = round_result.get("round")
    if (
        not isinstance(settled_round, int)
        or isinstance(settled_round, bool)
        or settled_round < 1
    ):
        raise ValueError("confirmed_finding_round_invalid")
    runtime_round = runtime.get("round")
    if runtime_round is not None:
        if (
            not isinstance(runtime_round, int)
            or isinstance(runtime_round, bool)
            or runtime_round < 1
        ):
            raise ValueError("runtime_round_invalid")
        if runtime_round != settled_round:
            # DOC-D1：恢复/续轮场景下 runtime.round 可能已推进（next-round）而
            # 被结算轮次仍是上一轮——这不是数据损坏，是轮次身份的**时序差**。
            # 判定改用事实锚：只要 runtime.round ≥ settled_round 即视为同一条
            # 结算链（forward-consistent）；回退（<）才是真异常。
            if runtime_round < settled_round:
                raise ValueError(
                    "confirmed_finding_round_mismatch: runtime.round "
                    f"({runtime_round}) 落后于被结算轮次 ({settled_round})；"
                    "请勿在 next-round 后回退 runtime"
                )
            print(
                f"[knowledge_auto] runtime.round={runtime_round} 已领先被结算"
                f"轮次 {settled_round}（next-round 先行），按结算链继续复盘"
            )

    if snapshot is None:
        findings: list[dict[str, Any]] = []
    else:
        expected_binding = getattr(snapshot, "expected_binding", None)
        if expected_binding is None:
            # ``finding_io`` deliberately preserves the existing unbound
            # report exception for the non-project legacy layout.  Keep that
            # policy at the materialization boundary too; a formal snapshot
            # can never arrive here without an expected binding because the
            # shared validator rejects it during capture.
            if getattr(snapshot, "layout_policy", None) != "legacy":
                raise ValueError("findings_snapshot_binding_missing")
        elif not isinstance(expected_binding, Mapping):
            raise ValueError("findings_snapshot_binding_missing")
        else:
            expected_runtime_binding = {
                "runtime_run_uuid": runtime.get("run_id"),
                "lifecycle_id": runtime.get("lifecycle_id"),
                "round": runtime.get("round"),
                "polling_id": runtime.get("polling_id"),
            }
            # DOC-D1 forward-consistent（与下方 confirmed 校验同语义，2026-11-09
            # 实测修复 findings_snapshot_round_mismatch 死锁）：next-round 先把
            # runtime.round 推进后，快照 binding 里记的是被结算轮次——身份
            # （run_uuid/lifecycle/polling）必须逐字一致；round 允许 runtime
            # 领先（≥ settled_round），回退才是真异常。
            binding = dict(expected_binding)
            for key in ("runtime_run_uuid", "lifecycle_id", "polling_id"):
                if binding.get(key) != expected_runtime_binding.get(key):
                    raise ValueError(
                        "findings_snapshot_binding_mismatch: "
                        f"{key} 快照={binding.get(key)!r} runtime="
                        f"{expected_runtime_binding.get(key)!r}"
                    )
            bound_round = binding.get("round")
            if bound_round != settled_round:
                raise ValueError(
                    "findings_snapshot_round_mismatch: 快照绑定轮 "
                    f"({bound_round}) 与被结算轮次 ({settled_round}) 不一致"
                )
            runtime_round = runtime.get("round")
            if (
                not isinstance(runtime_round, int) or isinstance(runtime_round, bool)
            ):
                runtime_round = bound_round
            if runtime_round < settled_round:
                raise ValueError(
                    "findings_snapshot_round_mismatch: runtime.round "
                    f"({runtime_round}) 落后于快照绑定轮 ({settled_round})"
                )
        # The snapshot constructor has already passed the shared pure validator
        # against this exact payload and its no-follow artifact metadata.
        findings = []
        for item in snapshot.findings:
            candidate = _thaw_snapshot_value(item)
            if finding_confirmation.is_confirmed(candidate):
                findings.append(candidate)
    expected_findings = round_result.get("findings", 0)
    credited = round_result.get("credited", 0)
    for name, value in (("findings", expected_findings), ("credited", credited)):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError(f"confirmed_finding_{name}_invalid")
    from core.launch_phases.finding_settlement import credited_cap
    if expected_findings != len(findings) or credited != min(len(findings), credited_cap()):
        raise ValueError(
            "confirmed_finding_settlement_mismatch: "
            f"confirmed={len(findings)} settled={expected_findings}/{credited}"
        )
    return findings


def _redact_text(redactor: Any, value: Any, *, language: str | None = None) -> Any:
    if not isinstance(value, str):
        return value
    cache_key = (value, language)
    if cache_key in _MEMO_REDACT:
        return _MEMO_REDACT[cache_key]
    try:
        # The shared redactor's assignment vocabulary deliberately covers
        # passwords/tokens/API keys; automatic Finding summaries also contain
        # the plain-English ``credential=...`` spelling, so close that local
        # gap before delegating the remaining text to the shared redactor.
        value = _CREDENTIAL_ASSIGNMENT.sub(_REDACTION_TOKEN, value)
        cleaned = redactor.redact_text(value, language=language)
        cleaned = _REDACTION_PLACEHOLDER.sub(_REDACTION_TOKEN, cleaned)
        # The shared scanner also sees JSON quoting around the text leaf. A
        # redacted assignment followed by prose can then look like one large,
        # unredacted value to its quoted-assignment rule. If that serialized
        # leaf still triggers the scanner, discard the whole free-text leaf;
        # retaining only a typed marker stays safe and keeps the ledger write
        # fail-closed for fields the redactor cannot fully normalize.
        import json
        from core.governance.secret_redactor import find_unredacted_secrets
        if find_unredacted_secrets(json.dumps(cleaned, ensure_ascii=False)):
            res = _REDACTION_TOKEN
        else:
            res = cleaned
        if len(_MEMO_REDACT) >= _MAX_REDACT_CACHE_SIZE:
            _MEMO_REDACT.clear()
        _MEMO_REDACT[cache_key] = res
        return res
    except Exception:
        raise ValueError("auto_materialization_redaction_failed") from None


def _sanitize_projection(projection: dict[str, Any]) -> dict[str, Any]:
    """Redact only explicitly allowlisted free-text leaves in the projection."""
    try:
        from core.governance.secret_redactor import SecretRedactor
        with SecretRedactor(max_mappings=4096, redact_pii=True) as redactor:
            for item in projection["patterns"]:
                language = item.get("language") if isinstance(item.get("language"), str) else None
                for key in _PATTERN_TEXT_FIELDS:
                    if key in item:
                        item[key] = _redact_text(redactor, item[key], language=language)
                for key in _PATTERN_TEXT_LIST_FIELDS:
                    values = item.get(key)
                    if isinstance(values, list):
                        item[key] = [_redact_text(redactor, value, language=language) for value in values]
                tags = item.get("routing_tags")
                if isinstance(tags, Mapping):
                    item["routing_tags"] = {
                        key: [_redact_text(redactor, value) for value in values]
                        if isinstance(values, list) else values
                        for key, values in tags.items()
                    }
            for item in projection["incidents"]:
                for key in _INCIDENT_TEXT_FIELDS:
                    if key in item:
                        item[key] = _redact_text(redactor, item[key])
                tags = item.get("routing_tags")
                if isinstance(tags, Mapping):
                    item["routing_tags"] = {
                        key: [_redact_text(redactor, value) for value in values]
                        if isinstance(values, list) else values
                        for key, values in tags.items()
                    }
            review = projection["review"]
            if isinstance(review.get("no_new_lesson_reason"), str):
                review["no_new_lesson_reason"] = _redact_text(
                    redactor, review["no_new_lesson_reason"],
                )
            for item in review.get("feedback", []):
                item["signature"] = _redact_text(redactor, item.get("signature"))
            for item in projection["tool_feedback"]:
                item["category"] = _redact_text(redactor, item.get("category"))
    except ValueError as exc:
        if str(exc).startswith("auto_materialization_"):
            raise
        raise ValueError("auto_materialization_redaction_failed") from None
    except Exception:
        raise ValueError("auto_materialization_redaction_failed") from None
    return projection


def _build_projection(
    ledger_path: str | Path, runtime: Mapping[str, Any], round_result: Mapping[str, Any],
    profile: Mapping[str, Any] | None, snapshot: Any, *, include_incidents: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], str]:
    from core.ledger import lesson_ledger as ledger

    findings = _confirmed_snapshot_inputs(runtime, round_result, snapshot)
    state = ledger.read(ledger_path)
    plans, missing_patterns = _plans(
        ledger_path, findings, profile, ledger_state=state,
    )
    round_no = int(round_result.get("round") or 0)
    if round_no < 1:
        raise ValueError("自动复盘缺少有效 round")
    life = _text(runtime.get("lifecycle_id"), 200)
    polling = _text(runtime.get("polling_id"), 200)
    credited = int(round_result.get("credited", 0) or 0)
    credited_plans = plans[:credited]
    new_pattern_ids = set(missing_patterns)
    all_pattern_ids = sorted({str(item["pattern_id"]) for item in plans})

    review_details = ledger.review_details(ledger_path, life, round_no, polling) or {}
    expected_review_fingerprints = sorted(
        item["fingerprint"] for item in credited_plans
    )
    supersede_stale = bool(review_details) and (
        int(review_details.get("finding_count", -1)) != len(findings)
        or int(review_details.get("credited_count", -1)) != credited
        or sorted(review_details.get("finding_fingerprints", []))
        != expected_review_fingerprints
        or sorted(review_details.get("pattern_ids", [])) != all_pattern_ids
    )
    if review_details and not supersede_stale:
        no_new_lesson_reason = review_details.get("reason")
    else:
        no_new_lesson_reason = (
            None if new_pattern_ids else (
                "自动复盘：本轮 confirmed Finding 已匹配现有 Pattern"
                if findings else "自动复盘：本轮没有 confirmed Finding"
            )
        )
    review = {
        "actor": "supervisor", "lifecycle_id": life, "polling_id": polling,
        "round": round_no, "finding_count": len(findings),
        "credited_count": credited,
        "pattern_ids": all_pattern_ids,
        "finding_fingerprints": [item["fingerprint"] for item in credited_plans],
        "feedback": [
            {
                "pattern_id": item["pattern_id"], "outcome": "hit",
                "finding_fingerprint": item["fingerprint"],
                "confirmation_digest": item["confirmation_digest"],
                "signature": (
                    item["finding"].get("error_signature")
                    or item["finding"].get("signature")
                ),
                "run_id": runtime.get("run_id"),
            }
            for item in credited_plans
        ],
        "no_new_lesson_reason": no_new_lesson_reason,
        "supersede": supersede_stale,
    }

    incidents: list[dict[str, Any]] = []
    all_incident_ids: list[str] = []
    existing_incident_ids = {
        str(item.get("incident_id")) for item in ledger.list_incidents(ledger_path)
        if item.get("incident_id")
    } if include_incidents else set()
    tool_feedback: list[dict[str, Any]] = []
    for item in plans:
        if include_incidents:
            raw_incident = build_incident_payload(
                item["finding"], item["pattern_id"], runtime, round_no, profile,
                finding_fingerprint=item["fingerprint"],
            )
            incident_id = str(raw_incident["incident_id"])
            all_incident_ids.append(incident_id)
            if incident_id not in existing_incident_ids:
                incidents.append(raw_incident)
        if include_incidents and item in credited_plans and item["finding"].get("tools"):
            tool_feedback.append({
                "lifecycle_id": life, "round": round_no,
                "category": classify_finding(item["finding"], profile),
                "tools": item["finding"].get("tools"), "outcome": "hit",
                "finding_fingerprint": item["fingerprint"],
                "confirmation_digest": item["confirmation_digest"],
            })

    projection = _sanitize_projection({
        "schema_version": 1,
        "review": review,
        "patterns": list(missing_patterns.values()),
        "incidents": incidents,
        "tool_feedback": tool_feedback,
    })
    _guard(projection, "自动知识 projection")
    return projection, plans, all_incident_ids, str(state["state_hash"])


def ensure_auto_review(
    ledger_path: str | Path, runtime: Mapping[str, Any], round_result: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None, *, findings_snapshot: Any = None,
    findings_snapshot_loaded: bool = False,
) -> dict[str, Any]:
    """Atomically create the current review and candidate Patterns."""
    from core.ledger import lesson_ledger as ledger
    snapshot = (
        findings_snapshot
        if findings_snapshot_loaded or findings_snapshot is not None
        else _load_findings_snapshot(runtime)
    )
    projection, plans, _incident_ids, expected_hash = _build_projection(
        ledger_path, runtime, round_result, profile, snapshot, include_incidents=False,
    )
    result = ledger.apply_auto_materialization(ledger_path, projection, expected_hash)
    result["plans"] = [
        {"finding_fingerprint": item["fingerprint"], "pattern_id": item["pattern_id"]}
        for item in plans
    ]
    return result


def materialize_confirmed_knowledge(
    ledger_path: str | Path, runtime: Mapping[str, Any], round_result: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None, *, findings_snapshot: Any = None,
    findings_snapshot_loaded: bool = False,
) -> dict[str, Any]:
    """Atomically materialize sanitized Patterns, review, Incidents and tool feedback."""
    from core.ledger import lesson_ledger as ledger
    snapshot = (
        findings_snapshot
        if findings_snapshot_loaded or findings_snapshot is not None
        else _load_findings_snapshot(runtime)
    )
    projection, plans, incident_ids, expected_hash = _build_projection(
        ledger_path, runtime, round_result, profile, snapshot, include_incidents=True,
    )
    result = ledger.apply_auto_materialization(ledger_path, projection, expected_hash)
    already_present = len(incident_ids) - len(projection["incidents"])
    result.update({
        "patterns": len({item["pattern_id"] for item in plans}),
        "incidents_reused": int(result.get("incidents_reused", 0)) + already_present,
        "incident_ids": incident_ids,
        "signature_keys": [f"sig-v1:{item['fingerprint']}" for item in plans],
        "review_status": result.get("review_status") or result.get("status"),
    })
    return result


def _self_check() -> None:
    finding = {"id": "x", "title": "SQL injection", "root_cause": "untrusted query input"}
    assert classify_finding(finding) == "injection"
    assert classify_finding({**finding, "category": "custom"}) == "custom"
    assert _SOURCE_REF == "auto://confirmed-finding"


if __name__ == "__main__":
    _self_check()
