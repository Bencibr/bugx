#!/usr/bin/env python3
"""core/ledger/patterns.py - 经验模式卡模型、正规化、检索与生命周期 (DOC-072)。"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from .schema import (
    AUDITED_TABLES,
    CARD_FIELDS,
    DEFAULT_BYTE_BUDGET,
    DEFAULT_LEDGER,
    DEFAULT_LIMIT,
    DEFAULT_TOKEN_BUDGET,
    FTS_COLUMNS,
    PATTERN_STATUSES,
    ROUTING_TAG_KEYS,
    SCHEMA_VERSION,
    CARD_LIMITS,
    ID_REGEX,
    LessonLedgerError,
    archive_manifest,
    assert_no_symlink_components,
    canonical_bytes,
    connect_ledger,
    record_event,
    insert_pattern,
    parse_json_list,
    normalize_id,
    parse_not_after,
    now_iso,
    refresh_materialized_views,
    sanitize_text,
    sanitize_text_opt,
    ledger_transaction,
    validate_pattern,
    database_path,
)

from core.governance.token_budget import estimate_tokens


def extract_card_fields(payload: Mapping[str, Any]) -> dict[str, str]:
    card = payload.get("card") if isinstance(payload.get("card"), Mapping) else {}
    return {
        field: sanitize_text_opt(
            payload.get(field) if field in payload else card.get(field),
            field, max_len=CARD_LIMITS[field],
        )
        for field in CARD_FIELDS
    }


def extract_routing_tags(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    card = payload.get("card") if isinstance(payload.get("card"), Mapping) else {}
    raw = payload.get("routing_tags") if "routing_tags" in payload else card.get("routing_tags")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise LessonLedgerError("routing_tags 必须是对象")
    unknown = sorted(set(map(str, raw)) - set(ROUTING_TAG_KEYS))
    if unknown:
        raise LessonLedgerError("routing_tags 含未声明维度: " + ", ".join(unknown))
    result: dict[str, list[str]] = {}
    for key in ROUTING_TAG_KEYS:
        value = raw.get(key, [])
        if value is None:
            continue
        if not isinstance(value, list) or len(value) > 24 \
                or any(not isinstance(item, str) or not item.strip() for item in value):
            raise LessonLedgerError(f"routing_tags.{key} 必须是最多 24 项的字符串数组")
        values = sorted({re.sub(r"\s+", "-", item.strip().casefold()) for item in value})
        if values:
            result[key] = values
    return result


def project_profile_defaults(path: str | os.PathLike[str]) -> dict[str, list[str]]:
    try:
        from core.runtime import project_profile

        profile = project_profile.load_project_profile(path)
        if not profile:
            return {}
        return extract_routing_tags({"routing_tags": project_profile.routing_tags(profile)})
    except (ImportError, LessonLedgerError, OSError, ValueError, TypeError, sqlite3.Error):
        return {}


def with_project_profile_defaults(
    path: str | os.PathLike[str], payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    defaults = project_profile_defaults(path)
    if not defaults:
        return payload

    card = payload.get("card") if isinstance(payload.get("card"), Mapping) else {}
    explicit_tags = extract_routing_tags(payload)
    merged = {
        key: sorted(set(defaults.get(key, [])) | set(explicit_tags.get(key, [])))
        for key in ROUTING_TAG_KEYS
        if defaults.get(key) or explicit_tags.get(key)
    }
    enriched = dict(payload)
    enriched["routing_tags"] = merged

    raw_language = payload.get("language") if "language" in payload else card.get("language")
    if (raw_language is None or (isinstance(raw_language, str) and not raw_language.strip())) \
            and len(defaults.get("languages", [])) == 1:
        enriched["language"] = defaults["languages"][0]
    return enriched


def decode_routing_tags(raw: Any) -> dict[str, list[str]]:
    if not raw:
        return {}
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    try:
        return extract_routing_tags({"routing_tags": value})
    except LessonLedgerError:
        return {}


def lesson_id(category: str, title: str, root_cause: str) -> str:
    raw = "\0".join(map(normalize_id, (category, title, root_cause)))
    return "L-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def normalize_pattern(payload: Mapping[str, Any], now: str | None = None) -> dict[str, Any]:
    category, title, root = (sanitize_text(payload.get(x), x) for x in ("category", "title", "root_cause"))
    key = payload.get("pattern_id") or payload.get("lesson_id") or lesson_id(category, title, root)
    key = sanitize_text(key, "pattern_id")
    if not ID_REGEX.fullmatch(key):
        raise LessonLedgerError("pattern_id 格式无效")
    status = payload.get("status", "candidate")
    if status not in PATTERN_STATUSES:
        raise LessonLedgerError("status 无效")
    timestamp = now or now_iso()
    result = {
        "pattern_id": key, "lesson_id": key, "category": category, "title": title,
        "root_cause": root, "reflection": sanitize_text(payload.get("reflection"), "reflection"),
        "probes": parse_json_list(payload.get("probes"), "probes"),
        "negative_controls": parse_json_list(payload.get("negative_controls", []), "negative_controls", False),
        "evidence_requirements": parse_json_list(payload.get("evidence_requirements", []), "evidence_requirements", False),
        "applicability": parse_json_list(payload.get("applicability", []), "applicability", False),
        "false_positive_patterns": parse_json_list(
            payload.get("false_positive_patterns", []),
            "false_positive_patterns", False,
        ),
        "oracle_hints": parse_json_list(payload.get("oracle_hints", []), "oracle_hints", False),
        "tags": parse_json_list(payload.get("tags", []), "tags", False),
        "playbook": parse_json_list(payload.get("playbook", []), "playbook", False),
        "attempts_tried": parse_json_list(payload.get("attempts_tried", []), "attempts_tried", False),
        "successful_solution": parse_json_list(
            payload.get("successful_solution", []), "successful_solution", False,
        ),
        "aliases": parse_json_list(payload.get("aliases", []), "aliases", False),
        "source_refs": parse_json_list(payload.get("source_refs"), "source_refs"),
        "finding_fingerprints": parse_json_list(payload.get("finding_fingerprints", []), "finding_fingerprints", False),
        "status": status, "superseded_by": payload.get("superseded_by"),
        "created_at": str(payload.get("created_at") or timestamp),
        "updated_at": str(payload.get("updated_at") or timestamp),
        "last_used_at": payload.get("last_used_at"),
        "use_count": int(payload.get("use_count", 0)), "hit_count": int(payload.get("hit_count", 0)),
        "miss_count": int(payload.get("miss_count", 0)),
        "false_positive_count": int(payload.get("false_positive_count", 0)),
        "rejected_count": int(payload.get("rejected_count", 0)),
        **extract_card_fields(payload),
        "routing_tags": extract_routing_tags(payload),
    }
    for field in ("use_count", "hit_count", "miss_count", "false_positive_count", "rejected_count"):
        if result[field] < 0:
            raise LessonLedgerError(f"{field} 不得小于 0")
    return result


def patterns_are_equal(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in (
        "category", "title", "root_cause", "reflection", "probes",
        "source_refs", "applicability", "false_positive_patterns",
        "negative_controls", "evidence_requirements", "oracle_hints", "tags",
        "aliases", "playbook", "attempts_tried",
        "successful_solution",
        "routing_tags",
        *CARD_FIELDS,
    ))


def pattern_value(c: sqlite3.Connection, key: str) -> dict[str, Any]:
    cursor = c.cursor()
    try:
        r = cursor.execute("SELECT pattern_id,category,title,root_cause,reflection,status,superseded_by,created_at,updated_at,last_used_at,use_count,hit_count,miss_count,false_positive_count,rejected_count FROM patterns WHERE pattern_id=?", (key,)).fetchone()
        if not r:
            raise LessonLedgerError(f"pattern_id 不存在: {key}")
        pid, cat, title, cause, refl, stat_v, sup, c_at, u_at, lu_at, u_cnt, h_cnt, m_cnt, fp_cnt, r_cnt = (
            str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]),
            str(r[5]), str(r[6]) if r[6] else None, str(r[7]), str(r[8]),
            str(r[9]) if r[9] else None, int(r[10]), int(r[11]), int(r[12]), int(r[13]), int(r[14])
        )
        groups = {
            "probe": [], "negative_control": [], "evidence": [],
            "applicability": [], "false_positive": [], "oracle": [], "tag": [],
            "playbook": [], "attempts_tried": [], "successful_solution": [],
        }
        probes_rows = cursor.execute("SELECT kind,value FROM probes WHERE pattern_id=? ORDER BY ordinal", (key,)).fetchall()
        for x in probes_rows:
            if str(x[0]) == "card":
                continue
            groups[str(x[0])].append(str(x[1]))
        aliases = [str(x[0]) for x in cursor.execute("SELECT alias FROM aliases WHERE pattern_id=?", (key,)).fetchall()]
        sources = [str(x[0]) for x in cursor.execute("SELECT source_ref FROM sources WHERE pattern_id=?", (key,)).fetchall()]
        occurs = [str(x[0]) for x in cursor.execute("SELECT fingerprint FROM occurrences WHERE pattern_id=?", (key,)).fetchall()]
        playbook = [str(x[0]) for x in cursor.execute("SELECT value FROM probes WHERE pattern_id=? AND kind='playbook' ORDER BY ordinal", (key,)).fetchall()]
        attempts = [str(x[0]) for x in cursor.execute("SELECT value FROM probes WHERE pattern_id=? AND kind='attempts_tried' ORDER BY ordinal", (key,)).fetchall()]
        success = [str(x[0]) for x in cursor.execute("SELECT value FROM probes WHERE pattern_id=? AND kind='successful_solution' ORDER BY ordinal", (key,)).fetchall()]
        card_raw = cursor.execute("SELECT value FROM probes WHERE pattern_id=? AND kind='card' ORDER BY ordinal LIMIT 1", (key,)).fetchone()
        try:
            card = json.loads(str(card_raw[0])) if card_raw else {}
        except (TypeError, ValueError):
            card = {}
        card = card if isinstance(card, dict) else {}
        return {
            "pattern_id": pid, "lesson_id": pid, "category": cat, "title": title,
            "root_cause": cause, "reflection": refl, "probes": groups["probe"],
            "negative_controls": groups["negative_control"],
            "evidence_requirements": groups["evidence"],
            "applicability": groups["applicability"],
            "false_positive_patterns": groups["false_positive"],
            "oracle_hints": groups["oracle"], "tags": groups["tag"],
            "playbook": playbook, "attempts_tried": attempts, "successful_solution": success,
            "aliases": aliases, "source_refs": sources, "finding_fingerprints": occurs,
            "status": stat_v, "superseded_by": sup, "created_at": c_at,
            "updated_at": u_at, "last_used_at": lu_at, "use_count": u_cnt,
            "hit_count": h_cnt, "miss_count": m_cnt,
            "false_positive_count": fp_cnt, "rejected_count": r_cnt,
            **{field: str(card.get(field) or "") for field in CARD_FIELDS},
            "routing_tags": card.get("routing_tags") if isinstance(card.get("routing_tags"), dict) else {},
        }
    finally:
        cursor.close()


def lessons_dict(c: sqlite3.Connection) -> dict[str, Any]:
    keys = [x[0] for x in c.execute("SELECT pattern_id FROM patterns ORDER BY pattern_id").fetchall()]
    return {key: pattern_value(c, key) for key in keys}


def read(path: str | os.PathLike[str] = DEFAULT_LEDGER) -> dict[str, Any]:
    from .sync_ops import validate_pattern
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        lessons = lessons_dict(c)
        events = [{"seq":r[0],"type":r[1],"at":r[2],"payload":json.loads(r[3]),"prev_hash":r[4],"hash":r[5]} for r in c.execute("SELECT * FROM events ORDER BY seq")]
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": len(events),
            "lessons": lessons,
            "state_hash": c.execute("SELECT value FROM meta WHERE key='state_hash'").fetchone()[0],
            "events": events,
            "head_hash": c.execute("SELECT value FROM meta WHERE key='head_hash'").fetchone()[0],
            "storage_backend": "sqlite",
            "database": str(db),
            "events_archived": sum(int(entry.get("record_count", 0)) for entry in archive_manifest(c)),
        }


def add_lesson(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> dict[str, Any]:
    actor = payload.get("actor", "supervisor")
    if actor not in {"supervisor", "seed"}:
        raise LessonLedgerError("账本写入必须来自 Supervisor 或 seed 导入")
    p = normalize_pattern(with_project_profile_defaults(path, payload))
    if p["status"] != "candidate":
        raise LessonLedgerError("Agent 新增经验只能是 candidate；active/superseded/retired 必须经过治理流程")

    def go(c):
        exists = c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (p["pattern_id"],)).fetchone()
        if exists:
            old = pattern_value(c, p["pattern_id"])
            if not patterns_are_equal(old, p):
                raise LessonLedgerError("pattern_id 已存在但内容不一致")
            for fp in p["finding_fingerprints"]:
                c.execute("INSERT OR IGNORE INTO occurrences VALUES (?,?,?)", (p["pattern_id"], fp, now_iso()))
            return {
                "status": "duplicate",
                "lesson": pattern_value(c, p["pattern_id"]),
                "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            }
        insert_pattern(c, p)
        record_event(c, "pattern-created", {"pattern": p})
        return {
            "status": "created",
            "lesson": pattern_value(c, p["pattern_id"]),
            "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }

    return ledger_transaction(path, go)


def read_pattern(path: str | os.PathLike[str], pattern_id: str) -> dict[str, Any] | None:
    from .sync_ops import validate_pattern
    key = sanitize_text(pattern_id, "pattern_id")
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        if not c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (key,)).fetchone():
            return None
        return pattern_value(c, key)


def created_not_after(stamp: str, ceiling: str) -> bool:
    return parse_not_after(stamp, ceiling)


def merge_patterns(path: str | os.PathLike[str], source_id: str, target_id: str,
                   reason: str, *, actor: str = "supervisor") -> dict[str, Any]:
    from .incidents import incident_content_hash, incident_value
    if actor != "supervisor":
        raise LessonLedgerError("模式合并必须由 Supervisor 提交")
    source = sanitize_text(source_id, "source_pattern_id")
    target = sanitize_text(target_id, "target_pattern_id")
    why = sanitize_text(reason, "reason")
    if source == target:
        raise LessonLedgerError("source_pattern_id 与 target_pattern_id 不能相同")

    def go(c):
        left, right = pattern_value(c, source), pattern_value(c, target)
        if left["status"] in {"retired", "superseded", "revoked"}:
            raise LessonLedgerError("来源模式已不可合并")
        if right["status"] in {"retired", "superseded", "revoked"}:
            raise LessonLedgerError("目标模式必须可用")
        stamp = now_iso()
        c.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (target, left["title"]))
        c.executemany("INSERT OR IGNORE INTO aliases VALUES (?,?)", [(target, x) for x in left["aliases"]])
        c.executemany("INSERT OR IGNORE INTO sources VALUES (?,?)", [(target, x) for x in left["source_refs"]])
        c.execute("INSERT OR IGNORE INTO occurrences SELECT ?,fingerprint,observed_at FROM occurrences WHERE pattern_id=?", (target, source))
        c.execute("UPDATE patterns SET status='superseded',superseded_by=?,updated_at=? WHERE pattern_id=?", (target, stamp, source))
        incident_ids = [row[0] for row in c.execute(
            "SELECT incident_id FROM incidents WHERE pattern_id=? "
            "AND status NOT IN ('retired','revoked')", (source,),
        ).fetchall()]
        c.execute("UPDATE incidents SET pattern_id=?,updated_at=? WHERE pattern_id=? AND status NOT IN ('retired','revoked')", (target, stamp, source))
        for incident_key in incident_ids:
            refreshed = incident_value(c, incident_key)
            c.execute(
                "UPDATE incidents SET content_hash=? WHERE incident_id=?",
                (incident_content_hash(refreshed), incident_key),
            )
        refresh_materialized_views(c, target)
        refresh_materialized_views(c, source)
        record_event(c, "pattern-merged", {"source_pattern_id": source, "target_pattern_id": target, "reason": why})
        return {"status": "merged", "source_pattern_id": source, "target_pattern_id": target}

    return ledger_transaction(path, go)


def retire_pattern(path: str | os.PathLike[str], pattern_id: str, reason: str, *,
                   actor: str = "supervisor") -> dict[str, Any]:
    if actor != "supervisor":
        raise LessonLedgerError("模式退役必须由 Supervisor 提交")
    key, why = sanitize_text(pattern_id, "pattern_id"), sanitize_text(reason, "reason")

    def go(c):
        value = pattern_value(c, key)
        if value["status"] in {"retired", "superseded", "revoked"}:
            raise LessonLedgerError("模式已不可退役")
        stamp = now_iso()
        c.execute("UPDATE patterns SET status='retired',updated_at=? WHERE pattern_id=?", (stamp, key))
        c.execute("UPDATE incidents SET status='retired',updated_at=? WHERE pattern_id=? AND status='active'", (stamp, key))
        record_event(c, "pattern-retired", {"pattern_id": key, "reason": why})
        return {"status": "retired", "pattern_id": key}

    return ledger_transaction(path, go)


def revoke_pattern(path: str | os.PathLike[str], pattern_id: str, reason: str, *,
                   actor: str = "supervisor") -> dict[str, Any]:
    if actor != "supervisor":
        raise LessonLedgerError("模式撤销必须由 Supervisor 提交")
    key, why = sanitize_text(pattern_id, "pattern_id"), sanitize_text(reason, "reason")

    def go(c):
        value = pattern_value(c, key)
        if value["status"] == "revoked":
            raise LessonLedgerError("模式已撤销")
        stamp = now_iso()
        c.execute("UPDATE patterns SET status='revoked',updated_at=? WHERE pattern_id=?", (stamp, key))
        c.execute("UPDATE incidents SET status='revoked',updated_at=? WHERE pattern_id=? AND status IN ('candidate','active')", (stamp, key))
        record_event(c, "pattern-revoked", {"pattern_id": key, "reason": why})
        return {"status": "revoked", "pattern_id": key}

    return ledger_transaction(path, go)


def hygiene(path: str | os.PathLike[str], *, min_feedback: int = 3) -> dict[str, Any]:
    """只报告可治理对象；绝不自行合并或退役历史经验。"""
    if not isinstance(min_feedback, int) or min_feedback < 1:
        raise LessonLedgerError("min_feedback 必须是正整数")
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        groups: dict[tuple[str, str], list[str]] = {}
        low = []
        for row in c.execute(
            "SELECT pattern_id,category,root_cause,status,hit_count,false_positive_count,rejected_count FROM patterns WHERE status IN ('active','candidate')"
        ):
            key, category, root, status, hits, false, rejected = row
            groups.setdefault((normalize_id(category), normalize_id(root)), []).append(key)
            total = int(hits) + int(false) + int(rejected)
            if total >= min_feedback and (int(hits) / total) < 0.34:
                low.append({
                    "pattern_id": key,
                    "status": status,
                    "feedback_total": total,
                    "precision": round(int(hits) / total, 4),
                    "suggestion": "review_then_retire",
                })
        duplicates = [
            {"pattern_ids": sorted(ids), "suggestion": "review_then_merge"}
            for ids in groups.values()
            if len(ids) > 1
        ]
        growth = {
            table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in AUDITED_TABLES
        }
        page_count = int(c.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(c.execute("PRAGMA page_size").fetchone()[0])
        return {
            "schema_version": SCHEMA_VERSION,
            "low_quality": sorted(
                low, key=lambda x: (x["precision"], x["pattern_id"])
            ),
            "duplicate_root_cause_groups": duplicates,
            "automatic_action": "none",
            "growth": {
                "table_rows": growth,
                "database_bytes": page_count * page_size,
                "note": "events 有 10 万硬上限，接近上限用 archive-events 显式归档后可继续写入；incidents/tool_usage 无上限，体积过大时用 incident-archive 收缩正文",
            },
        }



def import_patterns(path: str | os.PathLike[str], patterns: Iterable[Mapping[str, Any]], *,
                    bundle_id: str, reviewer: str,
                    actor: str = "publisher") -> dict[str, Any]:
    if actor != "publisher":
        raise LessonLedgerError("全局模式导入必须由 publisher 执行")
    bundle_id = sanitize_text(bundle_id, "bundle_id")
    reviewer = sanitize_text(reviewer, "reviewer")
    if not isinstance(patterns, Iterable) or isinstance(patterns, (str, bytes, Mapping)):
        raise LessonLedgerError("patterns 必须是对象数组")
    normalized = []
    for raw in patterns:
        if not isinstance(raw, Mapping):
            raise LessonLedgerError("patterns 必须是对象数组")
        item = dict(raw)
        item["status"] = "candidate"
        normalized.append(normalize_pattern(item))
    if not normalized:
        raise LessonLedgerError("导入包不能为空")
    keys = [item["pattern_id"] for item in normalized]
    if len(keys) != len(set(keys)):
        raise LessonLedgerError("导入包中的 pattern_id 不得重复")

    def go(c):
        for row in c.execute("SELECT type,payload FROM events WHERE type='knowledge-imported'"):
            try:
                event_payload = json.loads(row[1])
            except json.JSONDecodeError:
                continue
            if event_payload.get("bundle_id") == bundle_id:
                return {
                    "status": "already_imported",
                    "bundle_id": bundle_id,
                    "count": len(keys),
                    "patterns": [pattern_value(c, k) for k in keys],
                    "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                }
        stamp = now_iso()
        imported_patterns = []
        for item in normalized:
            existing = c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (item["pattern_id"],)).fetchone()
            if existing:
                raise LessonLedgerError(f"导入包与已有模式冲突: {item['pattern_id']}")
            item_to_insert = dict(item)
            item_to_insert["status"] = "active"
            item_to_insert["updated_at"] = stamp
            insert_pattern(c, item_to_insert)
            imported_patterns.append(pattern_value(c, item["pattern_id"]))
        record_event(c, "knowledge-imported", {
            "bundle_id": bundle_id, "reviewer": reviewer,
            "patterns": keys, "count": len(keys), "imported_at": stamp,
        })
        return {
            "status": "imported", "bundle_id": bundle_id,
            "count": len(keys), "patterns": imported_patterns,
            "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }

    return ledger_transaction(path, go)


def routing_match(card: Mapping[str, Any], requested: Mapping[str, list[str]]) -> tuple[float, list[str]]:
    stored = card.get("routing_tags") if isinstance(card.get("routing_tags"), Mapping) else {}
    weights = {
        "forms": 45.0, "languages": 35.0, "runtimes": 30.0,
        "frameworks": 25.0, "surfaces": 25.0, "targets": 20.0,
        "check_profiles": 15.0,
    }
    score = 0.0
    reasons: list[str] = []
    for key, weight in weights.items():
        wanted = {str(x).casefold() for x in requested.get(key, [])}
        available = {str(x).casefold() for x in stored.get(key, [])} if isinstance(stored, Mapping) else set()
        if not wanted:
            continue
        overlap = wanted & available
        if overlap:
            score += weight * min(1.0, len(overlap) / max(1, len(wanted)))
            reasons.append(f"{key}:{','.join(sorted(overlap))}")
    if requested and not stored:
        reasons.append("generic:no-routing-tags")
    elif requested and not reasons:
        reasons.append("generic:no-overlap")
    return score, reasons


def retrieve(path: str | os.PathLike[str], query: str = "", category: str | None = None,
             limit: int = DEFAULT_LIMIT, *, token_budget: int | None = DEFAULT_TOKEN_BUDGET,
             byte_budget: int | None = DEFAULT_BYTE_BUDGET,
             exclude_lesson_ids: Iterable[str] | None = None,
             exclude_pattern_ids: Iterable[str] | None = None,
             include_pattern_ids: Iterable[str] | None = None,
             created_before: str | None = None, runtime_only: bool = False,
             routing_tags: Mapping[str, Iterable[str]] | None = None) -> list[dict[str, Any]]:
    from .sync_ops import validate_pattern
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise LessonLedgerError("limit 必须在 1..100")
    tokens = [normalize_id(x) for x in re.findall(r"[\w-]+", query, re.UNICODE)]
    _cjk = re.findall(r"[\u4e00-\u9fff]", query)
    if len(_cjk) >= 2:
        tokens.extend(normalize_id(a + b) for a, b in zip(_cjk, _cjk[1:]))
    requested_tags = {
        key: [str(value).casefold() for value in values]
        for key, values in (routing_tags or {}).items()
        if key in ROUTING_TAG_KEYS
    }
    excluded = set(exclude_lesson_ids or []) | set(exclude_pattern_ids or [])
    db = database_path(path)
    forced: set[str] = set()
    include_keys = set(include_pattern_ids or [])
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", query)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        rows = []
        candidate_keys = None
        fts_rank: dict[str, float] = {}
        if tokens:
            try:
                match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
                indexed = {r[0] for r in c.execute("SELECT pattern_id FROM patterns_fts WHERE patterns_fts MATCH ?", (match,))}
                candidate_keys = indexed or None
                if cjk_chars:
                    candidate_keys = None
                else:
                    try:
                        fts_rank = {
                            str(r[0]): float(r[1])
                            for r in c.execute(
                                "SELECT pattern_id, bm25(patterns_fts) FROM patterns_fts "
                                "WHERE patterns_fts MATCH ?", (match,),
                            )
                        }
                    except sqlite3.Error:
                        fts_rank = {}
            except sqlite3.Error:
                candidate_keys = None
        for key, status in c.execute("SELECT pattern_id,status FROM patterns WHERE status IN ('active','candidate')"):
            pushed = key in include_keys
            if runtime_only and status != "active" and not pushed:
                continue
            if key in excluded and not pushed:
                continue
            if candidate_keys is not None and key not in candidate_keys and not pushed:
                continue
            p = pattern_value(c, key)
            if created_before and not parse_not_after(p["created_at"], created_before):
                continue
            hay = normalize_id(" ".join([
                p["category"], p["title"], p["root_cause"], p["reflection"],
                *p["probes"], *p["aliases"], *p["applicability"], *p["tags"],
                *p["false_positive_patterns"], *p["oracle_hints"],
                *p["playbook"], *p["attempts_tried"], *p["successful_solution"],
                *[p[field] for field in CARD_FIELDS
                  if field not in {"before_code", "after_code"}],
                *p["finding_fingerprints"],
            ]))
            matches = sum(t in hay for t in tokens)
            if tokens and not matches and not pushed:
                continue
            if category and normalize_id(p["category"]) != normalize_id(category) and not pushed:
                continue
            if not tokens and status == "candidate" and p["hit_count"] == 0 and not pushed:
                continue
            observations = sum(p[field] for field in (
                "hit_count", "miss_count", "false_positive_count", "rejected_count",
            ))
            quality = (
                2 * p["hit_count"] - 2 * p["miss_count"]
                - 3 * p["false_positive_count"] - 3 * p["rejected_count"]
            ) / (observations + 1)
            score = matches * 10 + (5 if category else 0) + (3 if status == "active" else 0)
            routing_score, match_reasons = routing_match(p, requested_tags)
            score += routing_score
            score += quality * 10
            relevance = fts_rank.get(key)
            if relevance is not None:
                score += min(20.0, max(0.0, -relevance))
            if pushed:
                score += 1000
                forced.add(key)
            rows.append({
                "pattern_id": key, "lesson_id": key, "category": p["category"],
                "applicability": p["applicability"][:6] or [p["category"]],
                "title": p["title"], "checks": p["probes"][:6],
                "probes": p["probes"][:6],
                "negative_controls": p["negative_controls"][:4],
                "evidence_requirements": p["evidence_requirements"][:4],
                "false_positive_patterns": p["false_positive_patterns"][:4],
                "oracle_hints": p["oracle_hints"][:4], "tags": p["tags"][:6],
                "playbook": p["playbook"][:6],
                "attempts_tried": p["attempts_tried"][:4],
                "successful_solution": p["successful_solution"][:4],
                "status": status, "match_score": score,
                "routing_tags": p.get("routing_tags", {}),
                "match_reasons": match_reasons,
                "updated_at": p["updated_at"], "last_used_at": p["last_used_at"],
                "use_count": p["use_count"], "hit_count": p["hit_count"],
                "miss_count": p["miss_count"],
                "false_positive_count": p["false_positive_count"],
                "rejected_count": p["rejected_count"],
                **{field: p[field] for field in CARD_FIELDS},
                "finding_fingerprints": p["finding_fingerprints"][:16],
            })
        rows.sort(key=lambda x: (-x["match_score"], x["pattern_id"]))
        result = []
        used = set()
        total_b = total_t = 0
        for row in rows:
            data = canonical_bytes(row)
            size = len(data)
            toks, _ = estimate_tokens(data)
            if byte_budget is not None and total_b + size > byte_budget:
                continue
            if token_budget is not None and total_t + toks > token_budget:
                continue
            if row["pattern_id"] not in forced and row["category"] in used \
                    and len(used) < 3 and not requested_tags:
                continue
            used.add(row["category"])
            result.append(row)
            total_b += size
            total_t += toks
            if len(result) >= limit:
                break
        return result
