#!/usr/bin/env python3
"""core/ledger/incidents.py - 事故故障卡模型、签名检索、复盘反馈与证据消费审计 (DOC-072)。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from .patterns import (
    decode_routing_tags,
    estimate_tokens,
    extract_routing_tags,
    insert_pattern,
    lesson_id,
    normalize_pattern,
    pattern_value,
    patterns_are_equal,
    routing_match,
    with_project_profile_defaults,
)
from .schema import (
    DEFAULT_LEDGER,
    INCIDENT_STATUSES,
    PATTERN_OUTCOMES,
    ROUTING_TAG_KEYS,
    FP_REGEX,
    ID_REGEX,
    INCIDENT_ID_REGEX,
    LessonLedgerError,
    canonical_bytes,
    connect_ledger,
    record_event,
    parse_json_list,
    normalize_id,
    now_iso,
    refresh_materialized_views,
    sanitize_text,
    sanitize_text_long,
    sanitize_text_opt,
    ledger_transaction,
    validate_pattern,
    database_path,
)

_INCIDENT_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def now_datetime() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _text_long_or_empty(value: Any, field: str, max_len: int = 65536) -> str:
    """DOC-203: allow empty evidence fields while retaining type/size checks."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    return sanitize_text_long(value, field, max_len)


def normalize_signature(value: Any) -> str:
    """Stable error-signature key: case/whitespace-insensitive, punctuation kept."""
    raw = sanitize_text(value, "signature")
    return normalize_id(raw)


def incident_id(
    signature: str, summary: str, commit_ref: str | None, *,
    finding_fingerprint: str | None = None,
) -> str:
    parts = [normalize_signature(signature), normalize_id(summary), normalize_id(commit_ref or "")]
    if finding_fingerprint:
        parts.append(normalize_id(finding_fingerprint))
    raw = "\0".join(parts)
    return "I-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def normalize_finding_fingerprint(value: Any) -> str:
    val = sanitize_text(value, "finding_fingerprint")
    if not FP_REGEX.fullmatch(val):
        raise LessonLedgerError("finding_fingerprint 格式无效")
    return val


def _incident_fingerprint(value: Any) -> str:
    val = sanitize_text(value, "finding_fingerprint")
    if not _INCIDENT_FINGERPRINT_RE.fullmatch(val):
        raise LessonLedgerError("finding_fingerprint 必须是 sha256:<64 位小写十六进制>")
    return val


def normalize_duration(value: Any) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(value) or value < 0:
        raise LessonLedgerError("incident duration_seconds 必须是非负有限数字")
    normalized = float(value)
    return int(normalized) if normalized.is_integer() else normalized


def incident_content(item: Mapping[str, Any]) -> dict[str, Any]:
    content = {
        "signature": item["signature"], "summary": item["summary"],
        "context": item["context"], "before_snippet": item["before_snippet"],
        "after_snippet": item["after_snippet"], "diff_ref": item.get("diff_ref"),
        "commit_ref": item.get("commit_ref"), "decision": item["decision"],
        "duration_seconds": normalize_duration(item.get("duration_seconds", 0)),
        "lifecycle_id": item.get("lifecycle_id"), "round": item.get("round"),
        "pattern_id": item.get("pattern_id"),
        "routing_tags": extract_routing_tags(item),
    }
    fingerprint = item.get("finding_fingerprint")
    if fingerprint is not None:
        content["finding_fingerprint"] = _incident_fingerprint(fingerprint)
    return content


def incident_content_hash(item: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(incident_content(item))).hexdigest()


def normalize_incident(payload: Mapping[str, Any], now: str | None = None) -> dict[str, Any]:
    signature = normalize_signature(payload.get("signature"))
    summary = sanitize_text(payload.get("summary"), "summary")
    context = sanitize_text_long(payload.get("context"), "context")
    before = _text_long_or_empty(payload.get("before_snippet"), "before_snippet")
    after = _text_long_or_empty(payload.get("after_snippet"), "after_snippet")
    decision = _text_long_or_empty(payload.get("decision"), "decision", max_len=16384)
    status = payload.get("status", "candidate")
    if status not in INCIDENT_STATUSES:
        raise LessonLedgerError("incident status 无效")
    if status in {"active", "retired", "revoked"}:
        raise LessonLedgerError("Agent 新增 incident 只能是 candidate；治理流程才能变更状态")
    diff_ref = sanitize_text(payload.get("diff_ref"), "diff_ref") if payload.get("diff_ref") else None
    commit_ref = sanitize_text(payload.get("commit_ref"), "commit_ref") if payload.get("commit_ref") else None
    lifecycle = sanitize_text(payload.get("lifecycle_id"), "lifecycle_id") if payload.get("lifecycle_id") else None
    rnd = payload.get("round")
    if rnd is not None and (not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1):
        raise LessonLedgerError("incident round 必须是正整数或 null")
    duration = normalize_duration(payload.get("duration_seconds", 0))
    routing_tags = extract_routing_tags(payload)
    pattern_id = payload.get("pattern_id")
    if pattern_id is not None and not ID_REGEX.fullmatch(str(pattern_id)):
        raise LessonLedgerError("incident pattern_id 格式无效")
    raw_fingerprint = payload.get("finding_fingerprint")
    finding_fingerprint = (
        _incident_fingerprint(raw_fingerprint) if raw_fingerprint is not None else None
    )
    key = payload.get("incident_id") or incident_id(
        signature, summary, commit_ref,
        finding_fingerprint=finding_fingerprint,
    )
    key = sanitize_text(key, "incident_id")
    if not INCIDENT_ID_REGEX.fullmatch(key):
        raise LessonLedgerError("incident_id 格式无效")
    timestamp = now or now_iso()
    content = incident_content({
        "signature": signature, "summary": summary, "context": context,
        "before_snippet": before, "after_snippet": after,
        "diff_ref": diff_ref, "commit_ref": commit_ref, "decision": decision,
        "duration_seconds": duration, "lifecycle_id": lifecycle, "round": rnd,
        "pattern_id": pattern_id, "routing_tags": routing_tags,
        "finding_fingerprint": finding_fingerprint,
    })
    incident = {
        **content,
        "incident_id": key, "status": status,
        "created_at": str(payload.get("created_at") or timestamp),
        "updated_at": str(payload.get("updated_at") or timestamp),
        "content_hash": incident_content_hash(content),
    }
    if finding_fingerprint is not None:
        incident["finding_fingerprint"] = finding_fingerprint
    return incident


_INCIDENT_INSERT_COLUMNS = (
    "incident_id", "pattern_id", "signature", "summary", "context",
    "before_snippet", "after_snippet", "diff_ref", "commit_ref", "decision",
    "duration_seconds", "lifecycle_id", "round", "status", "created_at",
    "updated_at", "content_hash", "routing_tags", "finding_fingerprint",
)


def insert_incident(c: sqlite3.Connection, item: Mapping[str, Any]) -> None:
    """Insert an incident with an explicit schema mapping, including local fingerprint."""
    columns = ",".join(_INCIDENT_INSERT_COLUMNS)
    placeholders = ",".join("?" for _ in _INCIDENT_INSERT_COLUMNS)
    values = tuple(
        json.dumps(item.get(name) or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if name == "routing_tags" else item.get(name)
        for name in _INCIDENT_INSERT_COLUMNS
    )
    c.execute(f"INSERT INTO incidents ({columns}) VALUES ({placeholders})", values)


def add_incident(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("actor", "supervisor") != "supervisor":
        raise LessonLedgerError("incident 写入必须由 Supervisor 提交")
    item = normalize_incident(with_project_profile_defaults(path, payload))

    def go(c):
        if item["pattern_id"] is not None and not c.execute(
                "SELECT 1 FROM patterns WHERE pattern_id=?", (item["pattern_id"],)
        ).fetchone():
            raise LessonLedgerError(
                f"incident 关联的 pattern 不存在: {item['pattern_id']}"
            )
        row = c.execute("SELECT content_hash FROM incidents WHERE incident_id=?", (item["incident_id"],)).fetchone()
        if row:
            if row[0] != item["content_hash"]:
                raise LessonLedgerError(
                    f"incident_id 已存在但内容不一致，拒绝覆盖: {item['incident_id']}"
                )
            return {
                "status": "duplicate", "incident": incident_value(c, item["incident_id"]),
                "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            }
        insert_incident(c, item)
        record_event(c, "incident-created", {"incident": item})
        return {
            "status": "created", "incident": incident_value(c, item["incident_id"]),
            "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }

    return ledger_transaction(path, go)


def activate_incident(path: str | os.PathLike[str], incident_key: str, *,
                      evidence_receipt_id: str, actor: str = "supervisor") -> dict[str, Any]:
    if actor != "supervisor":
        raise LessonLedgerError("incident 激活必须由 Supervisor 提交")
    key = sanitize_text(incident_key, "incident_id")
    receipt = sanitize_text(evidence_receipt_id, "evidence_receipt_id")

    def go(c):
        value = incident_value(c, key)
        if value["status"] != "candidate":
            raise LessonLedgerError("只有 candidate incident 可以激活")
        receipt_row = c.execute(
            "SELECT subject_type,subject_id,lifecycle_id,round,run_id,artifact_hash "
            "FROM evidence_receipts WHERE receipt_id=?", (receipt,),
        ).fetchone()
        if not receipt_row:
            raise LessonLedgerError("incident 激活缺少 evidence receipt")
        if receipt_row[0] != "incident" or receipt_row[1] != key:
            raise LessonLedgerError("evidence receipt 未绑定当前 incident")
        if value["lifecycle_id"] is not None \
                and receipt_row[2] != value["lifecycle_id"]:
            raise LessonLedgerError("evidence receipt lifecycle 与 incident 不匹配")
        if value["round"] is not None and int(receipt_row[3]) != int(value["round"]):
            raise LessonLedgerError("evidence receipt round 与 incident 不匹配")
        from .sync_ops import require_verified_incident_evidence
        require_verified_incident_evidence(path, value, {
            "subject_type": receipt_row[0], "subject_id": receipt_row[1],
            "lifecycle_id": receipt_row[2], "round": receipt_row[3],
            "run_id": receipt_row[4], "artifact_hash": receipt_row[5],
        })
        if value["pattern_id"]:
            parent = c.execute("SELECT status FROM patterns WHERE pattern_id=?", (value["pattern_id"],)).fetchone()
            if not parent or parent[0] != "active":
                raise LessonLedgerError("incident 关联的 Pattern 未 active")
        stamp = now_iso()
        c.execute("UPDATE incidents SET status='active',updated_at=? WHERE incident_id=?", (stamp, key))
        record_event(c, "incident-activated", {"incident_id": key, "evidence_receipt_id": receipt})
        return {"status": "active", "incident_id": key, "evidence_receipt_id": receipt}

    return ledger_transaction(path, go)


def fetch_incident_tools(c: sqlite3.Connection, lifecycle_id: str | None,
                         round_no: int | None) -> tuple[str, str]:
    if not lifecycle_id or round_no is None:
        return "", ""
    rows = c.execute(
        "SELECT tool_name, category, outcome FROM tool_usage "
        "WHERE lifecycle_id=? AND round=? ORDER BY created_at, rowid",
        (lifecycle_id, round_no),
    ).fetchall()
    if not rows:
        return "", ""
    tools: list[str] = []
    attempts: list[dict[str, Any]] = []
    for tool_name, category, outcome in rows:
        name = str(tool_name)
        if name not in tools:
            tools.append(name)
        attempts.append({
            "seq": len(attempts) + 1, "tool": name, "action": str(category or ""),
            "outcome": str(outcome or ""), "note": "",
        })
    try:
        payload = json.dumps(attempts, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = ""
    return ",".join(tools), payload


def incident_value(c: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = c.execute(
        "SELECT incident_id,pattern_id,signature,summary,context,before_snippet,after_snippet,diff_ref,commit_ref,decision,duration_seconds,lifecycle_id,round,status,created_at,updated_at,content_hash,routing_tags,finding_fingerprint FROM incidents WHERE incident_id=?",
        (key,),
    ).fetchone()
    if not row:
        raise LessonLedgerError(f"incident_id 不存在: {key}")
    item = {
        "incident_id": row[0], "pattern_id": row[1], "signature": row[2],
        "summary": row[3], "context": row[4], "before_snippet": row[5],
        "after_snippet": row[6], "diff_ref": row[7], "commit_ref": row[8],
        "decision": row[9], "duration_seconds": row[10], "lifecycle_id": row[11],
        "round": row[12], "status": row[13], "created_at": row[14],
        "updated_at": row[15], "content_hash": row[16],
        "routing_tags": decode_routing_tags(row[17]),
        "finding_fingerprint": (
            _incident_fingerprint(row[18]) if row[18] is not None else None
        ),
    }
    item["tools_used"], item["tool_attempts"] = fetch_incident_tools(
        c, item["lifecycle_id"], item["round"],
    )
    raw_oracle = ""
    if "复现：" in item.get("context", ""):
        tail = item["context"].split("复现：", 1)[1]
        if "confirmed finding" not in tail:
            raw_oracle = tail.strip()
    item["repro_oracle"] = raw_oracle[:300].strip()
    return item


def lookup_incidents(
    path: str | os.PathLike[str],
    *,
    query: str = "",
    signatures: Iterable[str] = (),
    pattern_id: str | None = None,
    limit: int = 2,
    token_budget: int | None = 3072,
    byte_budget: int | None = 20480,
    runtime_only: bool = False,
    routing_tags: Mapping[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    from .sync_ops import validate_pattern
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        raise LessonLedgerError("limit 必须在 1..10")
    signature_fingerprints: set[str] = set()
    wanted: list[str] = []
    for raw_signature in signatures:
        if isinstance(raw_signature, str) and raw_signature.lstrip().casefold().startswith("sig-v"):
            match = re.fullmatch(r"sig-v1:sha256:([0-9a-f]{64})", raw_signature)
            if not match:
                raise LessonLedgerError("versioned signature 格式无效")
            signature_fingerprints.add(f"sha256:{match.group(1)}")
        else:
            wanted.append(normalize_signature(raw_signature))
    tokens = [normalize_id(x) for x in re.findall(r"[\w-]+", query, re.UNICODE)]
    requested_tags = {
        key: [str(value).casefold() for value in values]
        for key, values in (routing_tags or {}).items()
        if key in ROUTING_TAG_KEYS
    }
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        identity_ids: set[str] = set()
        for fingerprint in sorted(signature_fingerprints):
            identity_ids.update(
                str(row[0])
                for row in c.execute(
                    "SELECT incident_id FROM incidents "
                    "INDEXED BY idx_incidents_finding_fingerprint "
                    "WHERE finding_fingerprint=?",
                    (fingerprint,),
                )
            )
        rows = []
        for row in c.execute(
            "SELECT i.incident_id,i.status FROM incidents i "
            "LEFT JOIN patterns p ON p.pattern_id=i.pattern_id "
            "WHERE i.status IN ('candidate','active')"
        ):
            item = incident_value(c, row[0])
            # DOC-203: candidate incidents are runtime-eligible only with an
            # executable reproduction oracle; placeholder prose is insufficient.
            has_valid_oracle = bool(
                item.get("repro_oracle")
                or ("复现：" in item.get("context", "")
                    and "confirmed finding" not in item.get("context", ""))
            )
            identity_exact_hit = item["incident_id"] in identity_ids
            if runtime_only and item["status"] != "active":
                if not (has_valid_oracle and len(item.get("context", "")) >= 15):
                    continue
            if runtime_only and item["pattern_id"]:
                parent = c.execute("SELECT status FROM patterns WHERE pattern_id=?", (item["pattern_id"],)).fetchone()
                if parent and parent[0] not in {"active", "candidate"}:
                    continue
            if pattern_id and item["pattern_id"] != pattern_id:
                continue
            exact = any(item["signature"] == value for value in wanted)
            partial = sum(w in item["signature"] or item["signature"] in w for w in wanted)
            clean_snippets = [
                s for s in (item.get("before_snippet", ""),
                            item.get("after_snippet", ""), item.get("decision", ""))
                if s and "未提供" not in s and "自动沉淀" not in s
            ]
            hay = normalize_id(" ".join([
                item["signature"], item["summary"], item["context"], *clean_snippets,
            ]))
            matches = sum(t in hay for t in tokens)
            score = (
                (100 if exact else 0)
                + (35 if not exact and partial else 0)
                + matches * 8
            )
            routing_score, match_reasons = routing_match(item, requested_tags)
            score += routing_score
            if has_valid_oracle:
                score += 25
                match_reasons.append("has_repro_oracle")
            if not identity_exact_hit and not exact and tokens and not matches:
                continue
            candidate = {
                **item,
                "lesson_id": item["pattern_id"] or item["incident_id"],
                "match_score": score,
                "match_reasons": match_reasons,
            }
            # The index result and text scan are joined by Incident ID. The identity
            # flag is computed once from that union, equivalent to OR-ing both origins.
            rows.append((identity_exact_hit, candidate))
        rows.sort(key=lambda pair: (
            not pair[0], -pair[1]["match_score"], pair[1]["incident_id"],
        ))
        result = []
        total_b = total_t = 0
        for _identity_exact_hit, row in rows:
            data = canonical_bytes(row)
            size = len(data)
            toks, _ = estimate_tokens(data)
            if byte_budget is not None and total_b + size > byte_budget:
                continue
            if token_budget is not None and total_t + toks > token_budget:
                continue
            result.append(row)
            total_b += size
            total_t += toks
            if len(result) >= limit:
                break
        return result


def confirmed_finding_digest(item: Mapping[str, Any]) -> str:
    finding = item.get("finding")
    if not isinstance(finding, Mapping):
        raise LessonLedgerError("hit feedback 必须包含已确认的 finding 对象")
    try:
        from . import finding_confirmation
        normalized = dict(finding)
        if not finding_confirmation.is_confirmed(normalized):
            raise LessonLedgerError("hit feedback 的 finding 尚未通过真实 Bug 确认门禁")
    except LessonLedgerError:
        raise
    except (ImportError, TypeError, ValueError) as exc:
        raise LessonLedgerError(f"hit feedback 的 finding 确认协议无效: {exc}") from exc
    return hashlib.sha256(canonical_bytes(normalized)).hexdigest()


def apply_review(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> dict[str, Any]:
    actor = payload.get("actor", "supervisor")
    if actor != "supervisor":
        raise LessonLedgerError("复盘写入必须由 Supervisor 提交")
    life = sanitize_text(payload.get("lifecycle_id"), "lifecycle_id")
    rnd = payload.get("round")
    if not isinstance(rnd, int) or rnd < 1:
        raise LessonLedgerError("round 必须是正整数")
    finding = int(payload.get("finding_count", 0))
    credited = int(payload.get("credited_count", 0))
    if finding < 0 or credited < 0 or credited > finding:
        raise LessonLedgerError("finding_count/credited_count 无效")
    ids = parse_json_list(payload.get("pattern_ids", payload.get("lesson_ids", [])), "lesson_ids", False)
    fps = [normalize_finding_fingerprint(x) for x in payload.get("finding_fingerprints", [])]
    if len(fps) != credited:
        raise LessonLedgerError("finding_fingerprints 数量必须等于 credited_count")
    if len(fps) != len(set(fps)):
        raise LessonLedgerError("finding_fingerprints 不得重复")
    new = payload.get("new_patterns", payload.get("new_lessons", []))
    reason = payload.get("no_new_lesson_reason")
    feedback_items = payload.get("feedback", [])
    if not isinstance(feedback_items, list) or any(not isinstance(item, Mapping) for item in feedback_items):
        raise LessonLedgerError("feedback 必须是对象数组")
    if not isinstance(new, list) or (not new and (not isinstance(reason, str) or not reason.strip())):
        raise LessonLedgerError("没有新增经验时必须提供非空 no_new_lesson_reason")
    supersede = payload.get("supersede", False)
    if not isinstance(supersede, bool):
        raise LessonLedgerError("supersede 必须是布尔值")
    # R6 复盘 S4：三张复盘表主键含 polling_id（'' 表示遗留行/未提供）。
    # 缺省 '' 保持旧调用方（外部 Supervisor CLI）的写入语义不变。
    polling = sanitize_text_opt(payload.get("polling_id"), "polling_id", 200)

    def go(c):
        old = c.execute(
            "SELECT finding_count,credited_count,reason,created_at "
            "FROM reviews WHERE polling_id=? AND lifecycle_id=? AND round=?",
            (polling, life, rnd),
        ).fetchone()
        if old:
            if not supersede:
                raise LessonLedgerError("同一 polling + lifecycle_id + round 不得重复提交 cycle-review")
            # 显式替换（supersede）通道：reviews/review_patterns/review_findings/
            # feedback 都是派生态，旧 review 可在删除后重录；events 哈希链只追加，
            # 旧 cycle-review 事件保留，另落一条 cycle-review-superseded 审计事件。
            # polling_id 维度加入后，替换只在**同一 polling 作用域**内生效；
            # 跨 polling 的同号复盘是独立行，无需替换即可共存。
            old_patterns = [
                {"pattern_id": row[0], "relation": row[1]}
                for row in c.execute(
                    "SELECT pattern_id,relation FROM review_patterns "
                    "WHERE polling_id=? AND lifecycle_id=? AND round=? "
                    "ORDER BY pattern_id",
                    (polling, life, rnd),
                ).fetchall()
            ]
            old_findings = [
                row[0] for row in c.execute(
                    "SELECT fingerprint FROM review_findings "
                    "WHERE polling_id=? AND lifecycle_id=? AND round=? "
                    "ORDER BY fingerprint",
                    (polling, life, rnd),
                ).fetchall()
            ]
            old_feedback = [
                {"pattern_id": row[0], "fingerprint": row[1]}
                for row in c.execute(
                    "SELECT pattern_id,fingerprint FROM feedback "
                    "WHERE lifecycle_id=? AND round=? ORDER BY pattern_id,fingerprint",
                    (life, rnd),
                ).fetchall()
            ]
            stamp = now_iso()
            # 'reused' 的 use_count 是旧 review 造成的增量，替换时回滚；
            # 'created' 的 pattern 是真实知识产物，保留且不动 patterns 表。
            for item in old_patterns:
                if item["relation"] == "reused":
                    c.execute(
                        "UPDATE patterns SET use_count=MAX(use_count-1,0),updated_at=? "
                        "WHERE pattern_id=?",
                        (stamp, item["pattern_id"]),
                    )
            # feedback 行与其消费事件一并清除：supersede 重录时同
            # (pattern_id, fingerprint) 会撞 feedback 主键与 consumption_events
            # event_id。patterns 上的 hit_count 等质量计数保持单调，不回滚。
            for item in old_feedback:
                c.execute(
                    "DELETE FROM consumption_events WHERE event_id=?",
                    (f"feedback:{life}:{rnd}:{item['pattern_id']}:{item['fingerprint']}",),
                )
            c.execute("DELETE FROM feedback WHERE lifecycle_id=? AND round=?", (life, rnd))
            c.execute(
                "DELETE FROM review_findings WHERE polling_id=? AND lifecycle_id=? AND round=?",
                (polling, life, rnd),
            )
            c.execute(
                "DELETE FROM review_patterns WHERE polling_id=? AND lifecycle_id=? AND round=?",
                (polling, life, rnd),
            )
            c.execute(
                "DELETE FROM reviews WHERE polling_id=? AND lifecycle_id=? AND round=?",
                (polling, life, rnd),
            )
            record_event(c, "cycle-review-superseded", {
                "polling_id": polling, "lifecycle_id": life, "round": rnd,
                "superseded": {
                    "finding_count": old[0], "credited_count": old[1],
                    "reason": old[2], "created_at": old[3],
                    "patterns": old_patterns, "finding_fingerprints": old_findings,
                    "feedback": old_feedback,
                },
                "superseded_by": {
                    "finding_count": finding, "credited_count": credited,
                    "finding_fingerprints": fps,
                },
            })
        created = []
        for raw in new:
            p = normalize_pattern(raw)
            row = c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (p["pattern_id"],)).fetchone()
            if p["status"] != "candidate":
                raise LessonLedgerError("复盘新增经验只能是 candidate；状态晋级必须经过独立治理流程")
            if row:
                if not patterns_are_equal(pattern_value(c, p["pattern_id"]), p):
                    raise LessonLedgerError("新增 pattern 内容冲突")
            else:
                insert_pattern(c, p)
                created.append(p["pattern_id"])
        for key in ids:
            if not c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (key,)).fetchone():
                raise LessonLedgerError("复盘引用不存在的 pattern_id: " + key)
        stamp = now_iso()
        c.execute(
            "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
            (polling, life, rnd, finding, credited,
             reason.strip() if isinstance(reason, str) else None, stamp),
        )
        for key in ids:
            c.execute(
                "INSERT INTO review_patterns VALUES (?,?,?,?,?)",
                (polling, life, rnd, key, "reused"),
            )
            c.execute("UPDATE patterns SET use_count=use_count+1,last_used_at=?,updated_at=? WHERE pattern_id=?", (stamp, stamp, key))
        for key in created:
            c.execute(
                "INSERT INTO review_patterns VALUES (?,?,?,?,?)",
                (polling, life, rnd, key, "created"),
            )
        c.executemany(
            "INSERT INTO review_findings VALUES (?,?,?,?)",
            [(polling, life, rnd, x) for x in fps],
        )
        review = {
            "polling_id": polling,
            "lifecycle_id": life, "round": rnd, "finding_count": finding, "credited_count": credited,
            "lesson_ids": ids, "pattern_ids": ids, "new_lesson_ids": created, "new_pattern_ids": created,
            "finding_fingerprints": fps, "no_new_lesson_reason": reason.strip() if isinstance(reason, str) else None,
            "feedback_count": len(feedback_items),
        }
        record_event(c, "cycle-review", review)
        feedback_results = []
        for item in feedback_items:
            key = sanitize_text(item.get("pattern_id") or item.get("lesson_id"), "pattern_id")
            outcome = sanitize_text(item.get("outcome"), "outcome")
            digest = confirmed_finding_digest(item) if outcome == "hit" else None
            feedback_results.append(record_feedback_tx(
                c, life, rnd, key, normalize_finding_fingerprint(item.get("finding_fingerprint")),
                outcome, digest, item.get("signature"), item.get("run_id"),
            ))
        result = {
            "status": "recorded", "review": review, "feedback": feedback_results,
            "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }
        if old:
            result["replaced_review"] = {
                "polling_id": polling, "lifecycle_id": life, "round": rnd,
                "finding_count": old[0], "credited_count": old[1],
                "reason": old[2], "created_at": old[3],
            }
        return result

    return ledger_transaction(path, go)


def record_feedback_tx(
    c: sqlite3.Connection, life: str, rnd: int, key: str, fp: str,
    outcome: str, confirmation_digest: str | None = None,
    signature: str | None = None, run_id: str | None = None,
) -> dict[str, Any]:
    if outcome not in PATTERN_OUTCOMES:
        raise LessonLedgerError("feedback 参数无效")
    if not c.execute("SELECT 1 FROM review_patterns WHERE lifecycle_id=? AND round=? AND pattern_id=?", (life, rnd, key)).fetchone():
        raise LessonLedgerError("反馈的 pattern_id 未在对应 cycle-review 中复用或新增")
    if not c.execute("SELECT 1 FROM review_findings WHERE lifecycle_id=? AND round=? AND fingerprint=?", (life, rnd, fp)).fetchone():
        raise LessonLedgerError("反馈的 finding_fingerprint 未在对应 cycle-review 中声明")
    normalized_signature = normalize_signature(signature) if signature else None
    try:
        c.execute(
            "INSERT INTO feedback(lifecycle_id,round,pattern_id,fingerprint,outcome,created_at,signature) "
            "VALUES (?,?,?,?,?,?,?)",
            (life, rnd, key, fp, outcome, now_iso(), normalized_signature),
        )
    except sqlite3.IntegrityError as exc:
        raise LessonLedgerError("同一反馈不得重复提交") from exc
    stamp = now_iso()
    field = {
        "hit": "hit_count", "miss": "miss_count",
        "false_positive": "false_positive_count",
        "rejected": "rejected_count",
    }.get(outcome)
    if field:
        c.execute(
            f"UPDATE patterns SET {field}={field}+1,updated_at=? WHERE pattern_id=?",
            (stamp, key),
        )
        c.execute("INSERT OR IGNORE INTO occurrences VALUES (?,?,?)", (key, fp, stamp))
    promoted = outcome == "hit" and c.execute("SELECT status FROM patterns WHERE pattern_id=?", (key,)).fetchone()[0] == "candidate" and c.execute("SELECT COUNT(DISTINCT lifecycle_id) FROM feedback WHERE pattern_id=? AND outcome='hit'", (key,)).fetchone()[0] >= 2
    if promoted:
        c.execute("UPDATE patterns SET status='active',updated_at=? WHERE pattern_id=? AND status='candidate'", (stamp, key))
    refresh_materialized_views(c, key)
    event = {
        "lifecycle_id": life, "round": rnd, "pattern_id": key,
        "lesson_id": key, "finding_fingerprint": fp, "outcome": outcome,
    }
    if confirmation_digest:
        event["confirmation_digest"] = confirmation_digest
    if normalized_signature:
        event["signature"] = normalized_signature
    if outcome == "hit" and normalized_signature and c.execute(
        "SELECT 1 FROM feedback WHERE pattern_id=? AND outcome='miss' "
        "AND signature=? AND lifecycle_id<>? LIMIT 1",
        (key, normalized_signature, life),
    ).fetchone():
        event["delayed_hit"] = True
        record_event(c, "pattern-delayed-hit", {
            "pattern_id": key, "lesson_id": key, "lifecycle_id": life,
            "round": rnd, "signature": normalized_signature,
        })
    record_event(c, "pattern-feedback", event)
    record_consumption_event_tx(c, {
        "event_id": f"feedback:{life}:{rnd}:{key}:{fp}",
        "subject_type": "pattern", "subject_id": key, "event_type": "feedback",
        "lifecycle_id": life, "run_id": str(run_id or ""),
        "created_at": now_iso(),
    })
    if promoted:
        record_event(c, "pattern-promoted", {"pattern_id": key, "lesson_id": key, "reason": "two independent lifecycle hits"})
    return {"status": "recorded", "lesson_id": key, "pattern_id": key, "outcome": outcome}


def record_feedback(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("actor", "supervisor") != "supervisor":
        raise LessonLedgerError("反馈写入必须由 Supervisor 提交")
    life = sanitize_text(payload.get("lifecycle_id"), "lifecycle_id")
    rnd = payload.get("round")
    key = sanitize_text(payload.get("pattern_id") or payload.get("lesson_id"), "pattern_id")
    outcome = sanitize_text(payload.get("outcome"), "outcome")
    fp = normalize_finding_fingerprint(payload.get("finding_fingerprint"))
    if not isinstance(rnd, int) or rnd < 1:
        raise LessonLedgerError("feedback 参数无效")
    confirmation_digest = confirmed_finding_digest(payload) if outcome == "hit" else None
    signature = payload.get("signature")
    run_id = payload.get("run_id")

    def go(c):
        result = record_feedback_tx(
            c, life, rnd, key, fp, outcome, confirmation_digest, signature, run_id,
        )
        result["revision"] = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return result

    return ledger_transaction(path, go)


def feedback_outcomes(path: str | os.PathLike[str] = DEFAULT_LEDGER, *,
                      lifecycle_id: str | None = None,
                      limit: int = 100) -> list[dict[str, Any]]:
    from .sync_ops import validate_pattern
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise LessonLedgerError("limit 必须在 1..100")
    uploadable = {"hit": "hit", "miss": "miss", "false_positive": "false_positive", "rejected": "rejected"}
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        sql = "SELECT pattern_id, outcome FROM feedback "
        args: list[Any] = []
        if lifecycle_id:
            sql += "WHERE lifecycle_id=? "
            args.append(lifecycle_id)
        sql += "GROUP BY pattern_id, outcome ORDER BY MAX(created_at) DESC"
        rows = c.execute(sql, args).fetchall()
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pattern_id, outcome in rows:
        mapped = uploadable.get(str(outcome))
        if not mapped:
            continue
        key = (str(pattern_id), mapped)
        if key in seen:
            continue
        seen.add(key)
        items.append({"lesson_id": key[0], "outcome": mapped})
        if len(items) >= limit:
            break
    return items


def list_incidents(path: str | os.PathLike[str], *, pattern_id: str | None = None,
                   limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
    from .sync_ops import validate_pattern
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
        raise LessonLedgerError("limit 必须在 1..5000")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise LessonLedgerError("offset 必须是非负整数")
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        if pattern_id:
            rows = c.execute(
                "SELECT incident_id FROM incidents WHERE pattern_id=? "
                "ORDER BY incident_id LIMIT ? OFFSET ?",
                (pattern_id, limit, offset),
            )
        else:
            rows = c.execute(
                "SELECT incident_id FROM incidents ORDER BY incident_id LIMIT ? OFFSET ?",
                (limit, offset),
            )
        result = []
        for row in rows:
            value = incident_value(c, row[0])
            result.append(value)
        return result


def archive_incidents(path: str | os.PathLike[str], *, older_than_days: int = 90,
                      max_archive: int = 50) -> dict[str, Any]:
    if not isinstance(older_than_days, int) or isinstance(older_than_days, bool) \
            or older_than_days < 1:
        raise LessonLedgerError("older_than_days 必须是正整数")
    if not isinstance(max_archive, int) or isinstance(max_archive, bool) or max_archive < 1:
        raise LessonLedgerError("max_archive 必须是正整数")
    cutoff = (now_datetime() - dt.timedelta(days=older_than_days)).isoformat(timespec="seconds")

    def go(c):
        archived: list[dict[str, Any]] = []
        marker = "archived-sha256:"
        for row in c.execute(
            "SELECT incident_id,updated_at,content_hash FROM incidents "
            "WHERE updated_at < ? ORDER BY updated_at LIMIT ?",
            (cutoff, max_archive),
        ).fetchall():
            incident_id, updated_at, original_hash = row[0], row[1], row[2]
            current = incident_value(c, incident_id)
            if str(current["before_snippet"]).startswith(marker):
                continue
            stamp = now_iso()
            current["before_snippet"] = f"{marker}{original_hash}:before_snippet"
            current["after_snippet"] = f"{marker}{original_hash}:after_snippet"
            current["decision"] = f"{marker}{original_hash}:decision"
            new_hash = incident_content_hash(current)
            c.execute(
                "UPDATE incidents SET before_snippet=?,after_snippet=?,decision=?,"
                "updated_at=?,content_hash=? WHERE incident_id=?",
                (current["before_snippet"], current["after_snippet"],
                 current["decision"], stamp, new_hash, incident_id),
            )
            archived.append({
                "incident_id": incident_id, "content_hash": new_hash,
                "archived_from_content_hash": original_hash,
                "archived_from_updated_at": updated_at,
            })
        if archived:
            record_event(c, "incidents-archived", {
                "older_than_days": older_than_days,
                "archived": archived,
            })
        return {
            "status": "archived" if archived else "no-op",
            "archived": len(archived),
            "incidents": archived,
            "older_than_days": older_than_days,
        }

    return ledger_transaction(path, go)


def revoke_incident(path: str | os.PathLike[str], incident_key: str, reason: str, *,
                    actor: str = "supervisor") -> dict[str, Any]:
    if actor != "supervisor":
        raise LessonLedgerError("incident 撤销必须由 Supervisor 提交")
    key = sanitize_text(incident_key, "incident_id")
    why = sanitize_text(reason, "reason")

    def go(c):
        value = incident_value(c, key)
        if value["status"] == "revoked":
            raise LessonLedgerError("incident 已撤销")
        stamp = now_iso()
        c.execute("UPDATE incidents SET status='revoked',updated_at=? WHERE incident_id=?", (stamp, key))
        record_event(c, "incident-revoked", {"incident_id": key, "reason": why})
        return {"status": "revoked", "incident_id": key}

    return ledger_transaction(path, go)


def record_consumption_event(path: str | os.PathLike[str], event: Mapping[str, Any]) -> dict[str, Any]:
    for field in ("event_id", "subject_type", "subject_id", "event_type"):
        sanitize_text(event.get(field), field)
    if event["event_type"] not in {"selected", "retrieved", "feedback"}:
        raise LessonLedgerError("event_type 必须是 selected/retrieved/feedback")
    item = {k: str(event.get(k) or "") for k in ("event_id", "subject_type", "subject_id", "event_type", "lifecycle_id", "run_id")}
    item["created_at"] = str(event.get("created_at") or now_iso())

    def go(c):
        return record_consumption_event_tx(c, item)

    return ledger_transaction(path, go)


def record_consumption_event_tx(c: sqlite3.Connection, item: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(item)
    value["content_hash"] = hashlib.sha256(canonical_bytes(value)).hexdigest()
    old = c.execute(
        "SELECT subject_type,subject_id,event_type,lifecycle_id,run_id,created_at,content_hash "
        "FROM consumption_events WHERE event_id=?",
        (value["event_id"],),
    ).fetchone()
    if old:
        # event_id is the retry/idempotency key.  A retry normally gets a new
        # wall-clock timestamp, so created_at cannot participate in the
        # semantic conflict check.  All other fields still must match: reusing
        # one id for another subject/event remains fail-closed.
        stored_semantics = tuple(old[index] for index in (0, 1, 2, 3, 4))
        incoming_semantics = tuple(value[key] for key in (
            "subject_type", "subject_id", "event_type", "lifecycle_id", "run_id",
        ))
        if stored_semantics != incoming_semantics:
            raise LessonLedgerError("consumption event_id 内容冲突")
        return {"status": "duplicate", "event_id": value["event_id"]}
    c.execute(
        "INSERT INTO consumption_events VALUES (?,?,?,?,?,?,?,?)",
        tuple(value[x] for x in (
            "event_id", "subject_type", "subject_id", "event_type",
            "lifecycle_id", "run_id", "created_at", "content_hash",
        )),
    )
    record_event(c, "knowledge-consumed", {
        k: value[k] for k in value if k != "content_hash"
    })
    return {"status": "recorded", "event_id": value["event_id"]}


def export_consumption_events(
    path: str | os.PathLike[str], *, limit: int = 500,
) -> list[dict[str, Any]]:
    """Export bounded, body-free consumption facts for V2 cloud sync."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
        raise LessonLedgerError("consumption event limit 必须在 1..5000")
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        rows = c.execute(
            "SELECT event_id,subject_type,subject_id,event_type,lifecycle_id,run_id,created_at "
            "FROM consumption_events ORDER BY created_at,event_id LIMIT ?", (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def review_exists(path: str | os.PathLike[str], lifecycle_id: str, round_no: int,
                  polling_id: str | None = None) -> bool:
    """检查 cycle-review 是否存在。

    polling_id 传入时精确匹配该 polling 作用域；None/''（旧调用方语义）
    只匹配 polling_id='' 的遗留行。
    """
    db = database_path(path)
    scope = polling_id or ""
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        return c.execute(
            "SELECT 1 FROM reviews WHERE polling_id=? AND lifecycle_id=? AND round=?",
            (scope, lifecycle_id, round_no),
        ).fetchone() is not None


def review_details(path: str | os.PathLike[str], lifecycle_id: str,
                   round_no: int, polling_id: str | None = None) -> dict[str, Any] | None:
    """读取可验证的复盘内容，供宿主结算门禁使用。

    polling_id 传入时精确匹配该 polling 作用域；None/''（旧调用方语义）
    只读取 polling_id='' 的遗留行。
    """
    db = database_path(path)
    scope = polling_id or ""
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        row = c.execute(
            "SELECT lifecycle_id,round,finding_count,credited_count,reason,created_at "
            "FROM reviews WHERE polling_id=? AND lifecycle_id=? AND round=?",
            (scope, lifecycle_id, round_no),
        ).fetchone()
        if not row:
            return None
        details = dict(row)
        details["polling_id"] = scope
        details["finding_fingerprints"] = [
            str(item[0]) for item in c.execute(
                "SELECT fingerprint FROM review_findings "
                "WHERE polling_id=? AND lifecycle_id=? AND round=? "
                "ORDER BY fingerprint",
                (scope, lifecycle_id, round_no),
            ).fetchall()
        ]
        details["pattern_ids"] = [
            str(item[0]) for item in c.execute(
                "SELECT pattern_id FROM review_patterns "
                "WHERE polling_id=? AND lifecycle_id=? AND round=? "
                "ORDER BY pattern_id",
                (scope, lifecycle_id, round_no),
            ).fetchall()
        ]
        # feedback 表没有 polling 维度（主键 (lifecycle_id, round, pattern_id,
        # fingerprint)），计数保持跨 polling 聚合的旧口径。
        details["feedback_count"] = int(c.execute(
            "SELECT COUNT(*) FROM feedback WHERE lifecycle_id=? AND round=?",
            (lifecycle_id, round_no),
        ).fetchone()[0])
        return details
