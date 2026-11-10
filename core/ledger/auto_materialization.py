"""Atomic persistence for sanitized automatic Finding projections (DOC-208 D1)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .incidents import (
    incident_value,
    insert_incident,
    normalize_incident,
    record_feedback_tx,
)
from .patterns import normalize_pattern, pattern_value, patterns_are_equal, with_project_profile_defaults
from .schema import (
    CARD_FIELDS,
    CARD_OPTIONAL_FIELDS,
    LessonLedgerError,
    record_event,
    insert_pattern,
    parse_json_list,
    get_meta_value,
    now_iso,
    refresh_materialized_views,
    sanitize_text,
    sanitize_text_opt,
    ledger_transaction,
)
from .tool_experience import extract_feedback_tools


MAX_AUTO_MATERIALIZATION_BYTES = 8 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
FP_REGEX = re.compile(r"^(?:sha256:)?[A-Za-z0-9_.:/-]{8,256}$")

_PATTERN_FIELDS = frozenset({
    "pattern_id", "category", "title", "root_cause", "reflection", "probes",
    "negative_controls", "evidence_requirements", "applicability",
    "false_positive_patterns", "oracle_hints", "tags", "playbook",
    "attempts_tried", "successful_solution", "aliases", "source_refs",
    "finding_fingerprints", "status", "superseded_by", "created_at",
    "updated_at", "last_used_at", "use_count", "hit_count", "miss_count",
    "false_positive_count", "rejected_count", "routing_tags",
    *CARD_FIELDS, *CARD_OPTIONAL_FIELDS,
})
_INCIDENT_FIELDS = frozenset({
    "incident_id", "pattern_id", "signature", "summary", "context",
    "before_snippet", "after_snippet", "diff_ref", "commit_ref", "decision",
    "duration_seconds", "lifecycle_id", "round", "status", "created_at",
    "updated_at", "content_hash", "routing_tags", "finding_fingerprint",
})
_REVIEW_FIELDS = frozenset({
    "actor", "lifecycle_id", "polling_id", "round", "finding_count",
    "credited_count", "pattern_ids", "finding_fingerprints", "feedback",
    "no_new_lesson_reason", "supersede",
})
_REVIEW_FEEDBACK_FIELDS = frozenset({
    "pattern_id", "outcome", "finding_fingerprint", "confirmation_digest",
    "signature", "run_id",
})
_TOOL_FEEDBACK_FIELDS = frozenset({
    "lifecycle_id", "round", "category", "tools", "outcome",
    "finding_fingerprint", "confirmation_digest",
})
_ROOT_FIELDS = frozenset({"schema_version", "review", "patterns", "incidents", "tool_feedback"})


def _strict_mapping(value: Any, allowed: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LessonLedgerError(f"{label} 必须是对象")
    keys = set(map(str, value.keys()))
    unknown = keys - allowed
    if unknown:
        raise LessonLedgerError(f"{label} 含未声明字段，拒绝写入")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise LessonLedgerError(f"{label} 格式无效")
    return value


def _prepare_review(raw: Any, patterns: list[dict[str, Any]]) -> dict[str, Any]:
    value = _strict_mapping(raw, _REVIEW_FIELDS, "auto review projection")
    if value.get("actor", "supervisor") != "supervisor":
        raise LessonLedgerError("复盘写入必须由 Supervisor 提交")
    life = sanitize_text(value.get("lifecycle_id"), "lifecycle_id")
    polling = sanitize_text_opt(value.get("polling_id"), "polling_id", 200)
    round_no = value.get("round")
    if not isinstance(round_no, int) or isinstance(round_no, bool) or round_no < 1:
        raise LessonLedgerError("round 必须是正整数")
    finding_count = value.get("finding_count")
    credited_count = value.get("credited_count")
    if (not isinstance(finding_count, int) or isinstance(finding_count, bool)
            or not isinstance(credited_count, int) or isinstance(credited_count, bool)
            or finding_count < 0 or credited_count < 0 or credited_count > finding_count):
        raise LessonLedgerError("finding_count/credited_count 无效")
    pattern_ids = parse_json_list(value.get("pattern_ids", []), "pattern_ids", False)
    fingerprints = value.get("finding_fingerprints", [])
    if not isinstance(fingerprints, list) or any(
            not isinstance(item, str) or not FP_REGEX.fullmatch(item)
            for item in fingerprints):
        raise LessonLedgerError("finding_fingerprints 格式无效")
    fingerprints = sorted(set(fingerprints))
    if len(fingerprints) != credited_count:
        raise LessonLedgerError("finding_fingerprints 数量必须等于 credited_count")
    if not isinstance(value.get("supersede", False), bool):
        raise LessonLedgerError("supersede 必须是布尔值")
    reason = value.get("no_new_lesson_reason")
    feedback_raw = value.get("feedback", [])
    if not isinstance(feedback_raw, list):
        raise LessonLedgerError("feedback 必须是对象数组")
    feedback: list[dict[str, Any]] = []
    for raw_item in feedback_raw:
        item = _strict_mapping(raw_item, _REVIEW_FEEDBACK_FIELDS, "review feedback")
        pattern_id = sanitize_text(item.get("pattern_id"), "pattern_id")
        outcome = sanitize_text(item.get("outcome"), "outcome")
        fp = sanitize_text(item.get("finding_fingerprint"), "finding_fingerprint")
        if not FP_REGEX.fullmatch(fp):
            raise LessonLedgerError("finding_fingerprint 格式无效")
        digest = item.get("confirmation_digest")
        if outcome == "hit":
            digest = _digest(digest, "confirmation_digest")
        elif digest is not None:
            raise LessonLedgerError("非 hit feedback 不得携带 confirmation_digest")
        feedback.append({
            "pattern_id": pattern_id, "outcome": outcome,
            "finding_fingerprint": fp, "confirmation_digest": digest,
            "signature": item.get("signature"),
            "run_id": sanitize_text_opt(item.get("run_id"), "run_id", 200),
        })
    if len(feedback) > credited_count:
        raise LessonLedgerError("feedback 数量超过 credited_count")
    return {
        "actor": "supervisor", "lifecycle_id": life, "polling_id": polling,
        "round": round_no, "finding_count": finding_count,
        "credited_count": credited_count, "pattern_ids": pattern_ids,
        "finding_fingerprints": fingerprints, "feedback": feedback,
        "no_new_lesson_reason": reason.strip() if isinstance(reason, str) else None,
        "supersede": value.get("supersede", False),
    }


def _review_matches(c: sqlite3.Connection, review: Mapping[str, Any], created_ids: set[str]) -> bool:
    key = (review["polling_id"], review["lifecycle_id"], review["round"])
    row = c.execute(
        "SELECT finding_count,credited_count,reason FROM reviews "
        "WHERE polling_id=? AND lifecycle_id=? AND round=?", key,
    ).fetchone()
    if not row:
        return False
    old_fps = [str(item[0]) for item in c.execute(
        "SELECT fingerprint FROM review_findings WHERE polling_id=? AND lifecycle_id=? AND round=? ORDER BY fingerprint",
        key,
    ).fetchall()]
    old_patterns = {
        (str(item[0]), str(item[1])) for item in c.execute(
            "SELECT pattern_id,relation FROM review_patterns WHERE polling_id=? AND lifecycle_id=? AND round=?",
            key,
        ).fetchall()
    }
    expected_pattern_ids = {str(item) for item in review["pattern_ids"]} | created_ids
    return (
        int(row[0]) == review["finding_count"]
        and int(row[1]) == review["credited_count"]
        and (row[2] or None) == review["no_new_lesson_reason"]
        and old_fps == review["finding_fingerprints"]
        and {item[0] for item in old_patterns} == expected_pattern_ids
    )


def _supersede_review(c: sqlite3.Connection, review: Mapping[str, Any]) -> dict[str, Any] | None:
    key = (review["polling_id"], review["lifecycle_id"], review["round"])
    old = c.execute(
        "SELECT finding_count,credited_count,reason,created_at FROM reviews "
        "WHERE polling_id=? AND lifecycle_id=? AND round=?", key,
    ).fetchone()
    if not old:
        return None
    old_patterns = [
        {"pattern_id": row[0], "relation": row[1]}
        for row in c.execute(
            "SELECT pattern_id,relation FROM review_patterns WHERE polling_id=? AND lifecycle_id=? AND round=? ORDER BY pattern_id",
            key,
        ).fetchall()
    ]
    old_findings = [row[0] for row in c.execute(
        "SELECT fingerprint FROM review_findings WHERE polling_id=? AND lifecycle_id=? AND round=? ORDER BY fingerprint",
        key,
    ).fetchall()]
    old_feedback = [
        {"pattern_id": row[0], "fingerprint": row[1]}
        for row in c.execute(
            "SELECT pattern_id,fingerprint FROM feedback WHERE lifecycle_id=? AND round=? ORDER BY pattern_id,fingerprint",
            (review["lifecycle_id"], review["round"]),
        ).fetchall()
    ]
    stamp = now_iso()
    for item in old_patterns:
        if item["relation"] == "reused":
            c.execute(
                "UPDATE patterns SET use_count=MAX(use_count-1,0),updated_at=? WHERE pattern_id=?",
                (stamp, item["pattern_id"]),
            )
    for item in old_feedback:
        c.execute(
            "DELETE FROM consumption_events WHERE event_id=?",
            (f"feedback:{review['lifecycle_id']}:{review['round']}:{item['pattern_id']}:{item['fingerprint']}",),
        )
    c.execute("DELETE FROM feedback WHERE lifecycle_id=? AND round=?", (review["lifecycle_id"], review["round"]))
    c.execute("DELETE FROM review_findings WHERE polling_id=? AND lifecycle_id=? AND round=?", key)
    c.execute("DELETE FROM review_patterns WHERE polling_id=? AND lifecycle_id=? AND round=?", key)
    c.execute("DELETE FROM reviews WHERE polling_id=? AND lifecycle_id=? AND round=?", key)
    record_event(c, "cycle-review-superseded", {
        "polling_id": review["polling_id"], "lifecycle_id": review["lifecycle_id"],
        "round": review["round"],
        "superseded": {
            "finding_count": old[0], "credited_count": old[1], "reason": old[2],
            "created_at": old[3], "patterns": old_patterns,
            "finding_fingerprints": old_findings, "feedback": old_feedback,
        },
        "superseded_by": {
            "finding_count": review["finding_count"],
            "credited_count": review["credited_count"],
            "finding_fingerprints": review["finding_fingerprints"],
        },
    })
    return {
        "polling_id": review["polling_id"], "lifecycle_id": review["lifecycle_id"],
        "round": review["round"], "finding_count": old[0],
        "credited_count": old[1], "reason": old[2], "created_at": old[3],
    }


def apply_auto_materialization(
    path: str | Path, projection: Mapping[str, Any], expected_state_hash: str,
) -> dict[str, Any]:
    """Commit sanitized automatic knowledge as one revision-checked SQLite transaction."""
    root = _strict_mapping(projection, _ROOT_FIELDS, "auto materialization projection")
    if root.get("schema_version") != 1:
        raise LessonLedgerError("auto materialization projection 版本无效")
    try:
        encoded = json.dumps(root, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LessonLedgerError("auto materialization projection 无法规范化") from exc
    if len(encoded) > MAX_AUTO_MATERIALIZATION_BYTES:
        raise LessonLedgerError("auto materialization projection 超限")
    expected_hash = _digest(expected_state_hash, "expected_state_hash")

    patterns_raw = root.get("patterns")
    incidents_raw = root.get("incidents")
    tools_raw = root.get("tool_feedback")
    if not isinstance(patterns_raw, list) or not isinstance(incidents_raw, list) or not isinstance(tools_raw, list):
        raise LessonLedgerError("auto materialization collections 必须是数组")
    patterns: list[dict[str, Any]] = []
    for raw in patterns_raw:
        item = _strict_mapping(raw, _PATTERN_FIELDS, "pattern projection")
        if item.get("status", "candidate") != "candidate":
            raise LessonLedgerError("自动沉淀 Pattern 只能是 candidate")
        patterns.append(normalize_pattern(item))
    review = _prepare_review(root.get("review"), patterns)
    incidents: list[dict[str, Any]] = []
    for raw in incidents_raw:
        item = _strict_mapping(raw, _INCIDENT_FIELDS, "incident projection")
        if not isinstance(item.get("incident_id"), str):
            raise LessonLedgerError("incident projection 缺少稳定 incident_id")
        incidents.append(normalize_incident(with_project_profile_defaults(path, item)))
    tools: list[dict[str, Any]] = []
    for raw in tools_raw:
        item = _strict_mapping(raw, _TOOL_FEEDBACK_FIELDS, "tool feedback projection")
        life = sanitize_text(item.get("lifecycle_id"), "lifecycle_id")
        rnd = item.get("round")
        if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
            raise LessonLedgerError("工具反馈 round 必须是正整数")
        outcome = sanitize_text(item.get("outcome"), "outcome")
        if outcome not in {"hit", "miss", "false_positive", "rejected"}:
            raise LessonLedgerError("outcome 无效")
        fp = sanitize_text(item.get("finding_fingerprint"), "finding_fingerprint")
        if not FP_REGEX.fullmatch(fp):
            raise LessonLedgerError("finding_fingerprint 格式无效")
        digest = item.get("confirmation_digest")
        if outcome == "hit":
            digest = _digest(digest, "confirmation_digest")
        elif digest is not None:
            raise LessonLedgerError("非 hit tool feedback 不得携带 confirmation_digest")
        tools.append({
            "lifecycle_id": life, "round": rnd,
            "category": sanitize_text(item.get("category"), "category"),
            "tools": extract_feedback_tools(item), "outcome": outcome,
            "finding_fingerprint": fp, "confirmation_digest": digest,
        })

    def go(c: sqlite3.Connection) -> dict[str, Any]:
        # This check is deliberately the first operation in the write transaction.
        if get_meta_value(c, "state_hash") != expected_hash:
            raise LessonLedgerError("auto materialization stale state; reread and replan")

        polling, life, rnd = review["polling_id"], review["lifecycle_id"], review["round"]
        review_key = (polling, life, rnd)
        old_review = c.execute(
            "SELECT 1 FROM reviews WHERE polling_id=? AND lifecycle_id=? AND round=?",
            review_key,
        ).fetchone()

        created_pattern_ids: set[str] = set()
        for pattern in patterns:
            key = pattern["pattern_id"]
            row = c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (key,)).fetchone()
            if row:
                if not patterns_are_equal(pattern_value(c, key), pattern):
                    raise LessonLedgerError("auto materialization pattern identity conflict")
            else:
                insert_pattern(c, pattern)
                record_event(c, "pattern-created", {"pattern": pattern})
                created_pattern_ids.add(key)

        pattern_ids = {item["pattern_id"] for item in patterns}
        replaced_review = None
        review_exact = bool(old_review) and _review_matches(c, review, created_pattern_ids)
        if old_review and not review_exact:
            if not review["supersede"]:
                raise LessonLedgerError("auto materialization review identity conflict")
            replaced_review = _supersede_review(c, review)

        if not review_exact:
            if not pattern_ids and not review["no_new_lesson_reason"]:
                raise LessonLedgerError("没有新增经验时必须提供非空 no_new_lesson_reason")
            for key in review["pattern_ids"]:
                if not c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (key,)).fetchone():
                    raise LessonLedgerError("auto review references missing pattern")
            created_ids = sorted(pattern_ids)
            reused_ids = sorted(set(review["pattern_ids"]) - set(created_ids))
            stamp = now_iso()
            c.execute(
                "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
                (polling, life, rnd, review["finding_count"], review["credited_count"],
                 review["no_new_lesson_reason"], stamp),
            )
            for key in reused_ids:
                c.execute("INSERT INTO review_patterns VALUES (?,?,?,?,?)", (polling, life, rnd, key, "reused"))
                c.execute(
                    "UPDATE patterns SET use_count=use_count+1,last_used_at=?,updated_at=? WHERE pattern_id=?",
                    (stamp, stamp, key),
                )
            for key in created_ids:
                c.execute("INSERT INTO review_patterns VALUES (?,?,?,?,?)", (polling, life, rnd, key, "created"))
            c.executemany(
                "INSERT INTO review_findings VALUES (?,?,?,?)",
                [(polling, life, rnd, fp) for fp in review["finding_fingerprints"]],
            )
            review_event = {
                "polling_id": polling, "lifecycle_id": life, "round": rnd,
                "finding_count": review["finding_count"],
                "credited_count": review["credited_count"],
                "lesson_ids": list(review["pattern_ids"]),
                "pattern_ids": list(review["pattern_ids"]),
                "new_lesson_ids": created_ids, "new_pattern_ids": created_ids,
                "finding_fingerprints": list(review["finding_fingerprints"]),
                "no_new_lesson_reason": review["no_new_lesson_reason"],
                "feedback_count": len(review["feedback"]),
            }
            record_event(c, "cycle-review", review_event)
            for item in review["feedback"]:
                record_feedback_tx(
                    c, life, rnd, item["pattern_id"], item["finding_fingerprint"],
                    item["outcome"], item["confirmation_digest"],
                    item.get("signature"), item.get("run_id"),
                )

        incident_created = 0
        incident_reused = 0
        incident_ids: list[str] = []
        for item in incidents:
            key = item["incident_id"]
            if item["pattern_id"] is not None and not c.execute(
                    "SELECT 1 FROM patterns WHERE pattern_id=?", (item["pattern_id"],)
            ).fetchone():
                raise LessonLedgerError("incident references missing pattern")
            old = c.execute("SELECT content_hash FROM incidents WHERE incident_id=?", (key,)).fetchone()
            if old:
                if old[0] != item["content_hash"]:
                    raise LessonLedgerError("auto materialization incident identity conflict")
                incident_reused += 1
            else:
                insert_incident(c, item)
                record_event(c, "incident-created", {"incident": item})
                incident_created += 1
            incident_ids.append(key)

        tool_feedback_created = 0
        for item in tools:
            if not c.execute(
                "SELECT 1 FROM reviews WHERE lifecycle_id=? AND round=?",
                (item["lifecycle_id"], item["round"]),
            ).fetchone():
                raise LessonLedgerError("工具反馈必须先有对应 cycle-review")
            if not c.execute(
                "SELECT 1 FROM review_findings WHERE lifecycle_id=? AND round=? AND fingerprint=?",
                (item["lifecycle_id"], item["round"], item["finding_fingerprint"]),
            ).fetchone():
                raise LessonLedgerError("工具反馈 Finding 未在对应 cycle-review 中声明")
            stamp = now_iso()
            inserted_names: list[str] = []
            for name in item["tools"]:
                old = c.execute(
                    "SELECT outcome FROM tool_usage WHERE lifecycle_id=? AND round=? "
                    "AND tool_name=? AND category=? AND finding_fingerprint=?",
                    (item["lifecycle_id"], item["round"], name, item["category"], item["finding_fingerprint"]),
                ).fetchone()
                if old:
                    if old[0] != item["outcome"]:
                        raise LessonLedgerError("tool feedback identity conflict")
                    continue
                c.execute(
                    "INSERT INTO tool_usage VALUES (?,?,?,?,?,?,?)",
                    (item["lifecycle_id"], item["round"], name, item["category"],
                     item["outcome"], item["finding_fingerprint"], stamp),
                )
                inserted_names.append(name)
            if inserted_names:
                event = {
                    "lifecycle_id": item["lifecycle_id"], "round": item["round"],
                    "tool_names": inserted_names, "category": item["category"],
                    "finding_fingerprint": item["finding_fingerprint"],
                    "outcome": item["outcome"],
                }
                if item["confirmation_digest"]:
                    event["confirmation_digest"] = item["confirmation_digest"]
                record_event(c, "tool-feedback", event)
                tool_feedback_created += 1

        feedback_count = len(review["feedback"]) if not review_exact else 0
        return {
            "status": "superseded" if replaced_review else ("recorded" if not review_exact else "existing"),
            "review_status": "superseded" if replaced_review else ("recorded" if not review_exact else "existing"),
            "review": {
                "polling_id": polling, "lifecycle_id": life, "round": rnd,
                "finding_count": review["finding_count"],
                "credited_count": review["credited_count"],
                "finding_fingerprints": list(review["finding_fingerprints"]),
                "pattern_ids": list(review["pattern_ids"]) + sorted(created_pattern_ids),
                "feedback_count": len(review["feedback"]),
            },
            "replaced_review": replaced_review,
            "patterns": len({item["pattern_id"] for item in patterns}) + len(review["pattern_ids"]),
            "incidents_created": incident_created,
            "incidents_reused": incident_reused,
            "incident_ids": incident_ids,
            "tool_feedback": tool_feedback_created,
            "feedback_created": feedback_count,
            "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }

    return ledger_transaction(path, go)
