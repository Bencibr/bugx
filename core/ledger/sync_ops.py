#!/usr/bin/env python3
"""core/ledger/sync_ops.py - 账本云端知识同步、证据回执与审计指标 (DOC-072)。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .schema import (
    AUDITED_TABLES,
    DEFAULT_LEDGER,
    PATTERN_OUTCOMES,
    SCHEMA_VERSION,
    LessonLedgerError,
    ID_REGEX,
    INCIDENT_ID_REGEX,
    archive_manifest,
    audit_snapshot,
    canonical_bytes,
    connect_ledger,
    record_event,
    insert_pattern,
    now_iso,
    state_digest,
    sync_state_hash,
    sanitize_text,
    ledger_transaction,
    validate_pattern,
    database_path,
)
from .patterns import (
    normalize_pattern,
    pattern_value,
    patterns_are_equal,
)
from .incidents import (
    incident_content_hash,
    incident_value,
    insert_incident,
    normalize_incident,
    record_consumption_event_tx,
)
from .tool_experience import (
    import_tool_experience_tx,
    normalize_tool_experience_records,
    tool_experience_hash,
    tool_experience_level,
)


def _ledger_workspace_root(path: str | os.PathLike[str]) -> Path:
    db_path = database_path(path).resolve()
    for parent in db_path.parents:
        if parent.name == ".bugx":
            return parent.parent
    return db_path.parent


def require_verified_incident_evidence(
    path: str | os.PathLike[str],
    incident: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    """Reject ledger receipts not backed by the matching Supervisor Finding."""
    if (
        receipt.get("subject_type") != "incident"
        or receipt.get("subject_id") != incident.get("incident_id")
        or receipt.get("lifecycle_id") != incident.get("lifecycle_id")
        or receipt.get("round") != incident.get("round")
        or not incident.get("finding_fingerprint")
    ):
        raise LessonLedgerError(
            "evidence receipt 缺少与 incident 一致的 Finding 指纹与生命周期绑定"
        )
    try:
        from core.launch_phases.finding_settlement import verify_ledger_incident_receipt

        verified = verify_ledger_incident_receipt(
            _ledger_workspace_root(path),
            finding_fingerprint=str(incident["finding_fingerprint"]),
            lifecycle_id=str(receipt["lifecycle_id"]),
            round_no=int(receipt["round"]),
            execution_id=str(receipt["run_id"]),
            artifact_sha256=str(receipt["artifact_hash"]),
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise LessonLedgerError(
            "无法读取 Supervisor 工件来源，拒绝信任 evidence receipt"
        ) from exc
    if not verified:
        raise LessonLedgerError(
            "evidence receipt 未通过 Supervisor Finding、claim、覆盖记录与工件复验"
        )


def reset_cloud_scope(
    path: str | os.PathLike[str],
    *,
    reason: str = "SYNC_RESYNC_REQUIRED",
) -> dict[str, int]:
    """Clear current cloud facts after an explicit server reset requirement.

    Sync transport state is owned by ``SyncStore``. This function removes
    current patterns, incidents, tool aggregates, derived indexes, and import
    receipts from one local ledger while preserving audit/history tables,
    evidence, feedback, and consumption records for forensics. An empty pull
    alone never calls this function.
    """
    reason = sanitize_text(reason, "reason")

    def go(c: sqlite3.Connection) -> dict[str, int]:
        pattern_ids = [
            row[0] for row in c.execute("SELECT pattern_id FROM patterns").fetchall()
        ]
        incident_count = int(
            c.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        )
        tool_count = int(
            c.execute("SELECT COUNT(*) FROM tool_experience").fetchone()[0]
        )
        if pattern_ids:
            placeholders = ",".join("?" for _ in pattern_ids)
            for table, column in (
                ("probes", "pattern_id"),
                ("aliases", "pattern_id"),
                ("sources", "pattern_id"),
                ("occurrences", "pattern_id"),
            ):
                c.execute(
                    f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                    pattern_ids,
                )
        c.execute("DELETE FROM patterns_fts")
        c.execute("DELETE FROM patterns")
        c.execute("DELETE FROM incidents")
        c.execute("DELETE FROM tool_experience")
        c.execute("DELETE FROM sync_import_receipts")
        record_event(
            c,
            "cloud-scope-reset",
            {
                "reason": reason,
                "patterns": len(pattern_ids),
                "incidents": incident_count,
                "tools": tool_count,
            },
        )
        return {
            "patterns": len(pattern_ids),
            "incidents": incident_count,
            "tools": tool_count,
        }

    return ledger_transaction(path, go)


def record_evidence_receipt(
    path: str | os.PathLike[str], receipt: Mapping[str, Any]
) -> dict[str, Any]:
    """Record only an incident receipt bound to verified Supervisor evidence."""
    required = (
        "receipt_id",
        "subject_type",
        "subject_id",
        "run_id",
        "lifecycle_id",
        "artifact_hash",
    )
    for field in required:
        sanitize_text(receipt.get(field), field)
    round_no = receipt.get("round")
    if not isinstance(round_no, int) or isinstance(round_no, bool) or round_no < 1:
        raise LessonLedgerError("evidence receipt round 必须是正整数")
    artifact_hash = str(receipt["artifact_hash"])
    if not re.fullmatch(r"[0-9a-fA-F]{64}", artifact_hash):
        raise LessonLedgerError("evidence receipt artifact_hash 必须是 SHA-256")
    item = {
        "receipt_id": str(receipt["receipt_id"]),
        "subject_type": str(receipt["subject_type"]),
        "subject_id": str(receipt["subject_id"]),
        "run_id": str(receipt["run_id"]),
        "lifecycle_id": str(receipt["lifecycle_id"]),
        "round": round_no,
        "artifact_hash": artifact_hash.lower(),
        "created_at": str(receipt.get("created_at") or now_iso()),
    }
    item["content_hash"] = hashlib.sha256(canonical_bytes(item)).hexdigest()

    def go(c: sqlite3.Connection):
        subject_type = item["subject_type"]
        subject_id = item["subject_id"]
        if subject_type != "incident":
            raise LessonLedgerError(
                "evidence receipt subject_type 仅支持 incident；pattern 没有可复验的 Supervisor Finding 绑定"
            )
        if not INCIDENT_ID_REGEX.fullmatch(subject_id):
            raise LessonLedgerError("evidence receipt incident subject_id 格式无效")
        if not c.execute(
            "SELECT 1 FROM incidents WHERE incident_id=?", (subject_id,),
        ).fetchone():
            raise LessonLedgerError(f"evidence receipt incident 不存在: {subject_id}")
        incident = incident_value(c, subject_id)
        if incident.get("lifecycle_id") is not None and (
            incident.get("lifecycle_id") != item["lifecycle_id"]
            or incident.get("round") != item["round"]
        ):
            raise LessonLedgerError("evidence receipt 与 incident 生命周期上下文不一致")
        require_verified_incident_evidence(path, incident, item)
        old = c.execute(
            "SELECT content_hash FROM evidence_receipts WHERE receipt_id=?",
            (item["receipt_id"],),
        ).fetchone()
        if old:
            if old[0] != item["content_hash"]:
                raise LessonLedgerError("evidence receipt_id 内容冲突")
            return {"status": "duplicate", "receipt_id": item["receipt_id"]}
        c.execute(
            "INSERT INTO evidence_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(
                item[x]
                for x in (
                    "receipt_id",
                    "subject_type",
                    "subject_id",
                    "run_id",
                    "lifecycle_id",
                    "round",
                    "artifact_hash",
                    "created_at",
                    "content_hash",
                )
            ),
        )
        record_event(
            c,
            "evidence-receipt-recorded",
            {k: item[k] for k in item if k != "artifact_hash"},
        )
        return {"status": "recorded", "receipt_id": item["receipt_id"]}

    return ledger_transaction(path, go)


def synced_pattern(
    raw: Mapping[str, Any], *, allow_remote_active: bool
) -> dict[str, Any]:
    """把 V2 wire pattern 转成本地记录；仅云端 Supervisor 路径可保留 active。"""
    if not isinstance(raw, Mapping):
        raise LessonLedgerError("云端 pattern 必须是对象")
    payload = dict(raw)
    details = (
        payload.get("details")
        if isinstance(payload.get("details"), Mapping)
        else {}
    )
    for key in (
        "playbook",
        "attempts_tried",
        "successful_solution",
        "applicability",
        "false_positive_patterns",
        "oracle_hints",
        "tags",
        "aliases",
        "negative_controls",
        "evidence_requirements",
    ):
        if key in details:
            payload[key] = (
                list(details[key])
                if isinstance(details[key], (list, tuple))
                else []
            )
    if isinstance(payload.get("routing_tags"), Mapping):
        payload["routing_tags"] = dict(payload["routing_tags"])
    elif isinstance(details.get("routing_tags"), Mapping):
        payload["routing_tags"] = dict(details["routing_tags"])
    payload["pattern_id"] = payload.get("pattern_id") or payload.get("lesson_id")
    payload["reflection"] = payload.get("reflection") or "云端导入，等待本地证据复核"
    # probes/source_refs 优先走 details 无损列表通道（push 端会把含逗号的
    # 元素原样放这里）；仅旧记录（无该通道）才退回逗号拆分。
    for key, fallback in (("probes", ["云端导入待补充验证探针"]),
                          ("source_refs", ["cloud-sync://remote"])):
        lossless = details.get(key)
        if isinstance(lossless, (list, tuple)) and lossless:
            payload[key] = [str(x) for x in lossless]
        else:
            details.pop(key, None)
            payload[key] = split_sync_values(payload.get(key)) or fallback
    if isinstance(payload.get("tags"), str):
        payload["tags"] = split_sync_values(payload.get("tags"))
    fingerprints = payload.get("finding_fingerprints")
    if isinstance(fingerprints, str):
        try:
            decoded = json.loads(fingerprints)
        except (TypeError, ValueError):
            decoded = None
        payload["finding_fingerprints"] = (
            [str(item) for item in decoded]
            if isinstance(decoded, list)
            else split_sync_values(fingerprints)
        )
    payload["created_at"] = payload.get("created_at") or "1970-01-01T00:00:00Z"
    payload["updated_at"] = payload.get("updated_at") or payload["created_at"]
    remote_status = str(payload.get("status") or "candidate")
    if remote_status not in {"candidate", "active"}:
        raise LessonLedgerError(f"云端 pattern status 不可导入: {remote_status}")
    payload["status"] = "candidate"
    pattern = normalize_pattern(payload)
    pattern["status"] = (
        "active"
        if allow_remote_active and remote_status == "active"
        else "candidate"
    )
    return pattern


def split_sync_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def synced_incident(
    raw: Mapping[str, Any],
    *,
    allow_remote_active: bool,
    c: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """把 V2 wire incident 转成本地记录，并校验 active 的父 pattern。"""
    if not isinstance(raw, Mapping):
        raise LessonLedgerError("云端 incident 必须是对象")
    details = raw.get("details") if isinstance(raw.get("details"), Mapping) else {}
    routing_tags = raw.get("routing_tags")
    if not isinstance(routing_tags, Mapping) and isinstance(
        details.get("routing_tags"), Mapping
    ):
        routing_tags = details["routing_tags"]
    remote_status = str(raw.get("status") or "candidate")
    if remote_status not in {"candidate", "active"}:
        raise LessonLedgerError(f"云端 incident status 不可导入: {remote_status}")
    pattern_id = raw.get("lesson_id") or raw.get("pattern_id")
    payload = {
        "incident_id": raw.get("incident_id"),
        "pattern_id": pattern_id,
        "signature": (
            raw.get("signature")
            or raw.get("error_signature")
            or raw.get("title")
            or raw.get("incident_id")
        ),
        "summary": raw.get("summary") or raw.get("root_cause") or raw.get("title"),
        "context": (
            details.get("context")
            or raw.get("context")
            or raw.get("summary")
            or raw.get("root_cause")
        ),
        "before_snippet": (
            raw.get("before_code") or raw.get("before_snippet") or "redacted"
        ),
        "after_snippet": (
            raw.get("after_code") or raw.get("after_snippet") or "redacted"
        ),
        "decision": (
            raw.get("decision_chain")
            or raw.get("decision")
            or "explicit review required"
        ),
        "duration_seconds": raw.get("duration") or raw.get("duration_seconds") or 0,
        "lifecycle_id": raw.get("lifecycle_id") or "cloud-import",
        "round": raw.get("round") or 1,
        "status": "candidate",
        "actor": "supervisor",
        "created_at": raw.get("created_at") or "1970-01-01T00:00:00Z",
        "updated_at": (
            raw.get("updated_at")
            or raw.get("created_at")
            or "1970-01-01T00:00:00Z"
        ),
        "routing_tags": routing_tags or {},
    }
    incident = normalize_incident(payload)
    if allow_remote_active and remote_status == "active" and c is not None:
        if pattern_id:
            parent = c.execute(
                "SELECT status FROM patterns WHERE pattern_id=?", (pattern_id,)
            ).fetchone()
            if parent and parent[0] == "active":
                incident["status"] = "active"
    return incident


def import_synced_pattern_tx(
    c: sqlite3.Connection, item: Mapping[str, Any]
) -> str:
    key = str(item["pattern_id"])
    row = c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (key,)).fetchone()
    if row:
        old = pattern_value(c, key)
        promoted = item.get("status") == "active" and old.get("status") == "candidate"
        if promoted:
            c.execute(
                "UPDATE patterns SET status='active',updated_at=? "
                "WHERE pattern_id=? AND status='candidate'",
                (now_iso(), key),
            )
            record_event(c, "cloud-pattern-activated", {"pattern_id": key})
        if not patterns_are_equal(old, item):
            # 云端记录多半是本账本 push 的回声：经服务端 portal 存储与 wire
            # 规整后常有字段级差异（列表逗号串化、详情字段裁剪）。本地账本
            # 是该记录的权威源，回声一律保留本地并照常推进同步；真实的多设
            # 备分歧由 push 侧 base_version 冲突兜底，不在此硬失败卡死整个
            # 同步流。
            record_event(c, "cloud-echo-kept-local", {"pattern_id": key})
        for fp in item.get("finding_fingerprints", []):
            c.execute(
                "INSERT OR IGNORE INTO occurrences VALUES (?,?,?)",
                (key, fp, now_iso()),
            )
        if promoted:
            return "activated"
        return "duplicate"
    insert_pattern(c, item)
    record_event(
        c,
        "cloud-pattern-imported",
        {"pattern_id": key, "status": item.get("status", "candidate")},
    )
    return "created"


def import_synced_incident_tx(
    c: sqlite3.Connection, item: Mapping[str, Any]
) -> str:
    key = str(item["incident_id"])
    pattern_id = item.get("pattern_id")
    if pattern_id is not None and not c.execute(
        "SELECT 1 FROM patterns WHERE pattern_id=?", (pattern_id,)
    ).fetchone():
        raise LessonLedgerError(f"云端 incident 关联的 pattern 不存在: {pattern_id}")
    row = c.execute(
        "SELECT content_hash,status FROM incidents WHERE incident_id=?", (key,)
    ).fetchone()
    if row:
        promoted = item.get("status") == "active" and row[1] == "candidate"
        if promoted:
            c.execute(
                "UPDATE incidents SET status='active',updated_at=? "
                "WHERE incident_id=? AND status='candidate'",
                (now_iso(), key),
            )
            record_event(c, "cloud-incident-activated", {"incident_id": key})
        if row[0] != item["content_hash"]:
            # 与 pattern 同理：pull 回来的差异多为 push 回声经服务端规整的
            # 字段级噪声，本地保留、照常推进；push 侧版本冲突兜底真实分歧。
            record_event(c, "cloud-echo-kept-local", {"incident_id": key})
        if promoted:
            return "activated"
        return "duplicate"
    insert_incident(c, item)
    record_event(
        c,
        "cloud-incident-imported",
        {
            "incident_id": key,
            "status": item.get("status", "candidate"),
        },
    )
    return "created"


def apply_synced_tombstone_tx(
    c: sqlite3.Connection, item: Mapping[str, Any]
) -> str:
    record_type = str(item.get("record_type") or "")
    record_id = str(item.get("record_id") or "")
    final_status = str(item.get("final_status") or "revoked")
    if (
        record_type not in {"pattern", "incident", "tool_experience", "tool"}
        or not record_id
    ):
        raise LessonLedgerError("云端 tombstone 缺少合法 record_type/record_id")
    if final_status not in {"superseded", "retired", "revoked"}:
        final_status = "revoked"
    if record_type in {"tool_experience", "tool"}:
        if "/" not in record_id:
            raise LessonLedgerError(
                "tool experience tombstone record_id 必须是 tool_name/category"
            )
        tool_name, category = record_id.split("/", 1)
        row = c.execute(
            "SELECT * FROM tool_experience WHERE tool_name=? AND category=?",
            (tool_name, category),
        ).fetchone()
        if not row:
            return "missing"
        if bool(row[14]):
            return "duplicate"
        deleted_at = now_iso()
        content = {
            "tool_name": row[0],
            "category": row[1],
            "experience_level": row[2],
            "record_version": int(row[3]),
            "use_count": int(row[4]),
            "hit_count": int(row[5]),
            "miss_count": int(row[6]),
            "false_positive_count": int(row[7]),
            "rejected_count": int(row[8]),
            "independent_lifecycle_count": int(row[9]),
            "first_observed_at": row[10],
            "last_observed_at": row[11],
            "tool_version": row[12],
            "device_id": row[13],
            "deleted": True,
            "deleted_at": deleted_at,
        }
        c.execute(
            "UPDATE tool_experience SET deleted=1,deleted_at=?,content_hash=? "
            "WHERE tool_name=? AND category=?",
            (deleted_at, tool_experience_hash(content), tool_name, category),
        )
        record_event(
            c,
            "cloud-tombstone-applied",
            {
                "record_type": "tool_experience",
                "record_id": record_id,
                "final_status": "revoked",
            },
        )
        return "applied"
    table = "patterns" if record_type == "pattern" else "incidents"
    key_column = "pattern_id" if table == "patterns" else "incident_id"
    row = c.execute(
        f"SELECT status FROM {table} WHERE {key_column}=?", (record_id,)
    ).fetchone()
    if not row:
        return "missing"
    stamp = now_iso()
    changed = row[0] != final_status
    c.execute(
        f"UPDATE {table} SET status=?,updated_at=? WHERE {key_column}=?",
        (final_status, stamp, record_id),
    )
    cascaded = 0
    if record_type == "pattern":
        children = c.execute(
            "SELECT incident_id FROM incidents WHERE pattern_id=? "
            "AND status IN ('candidate','active')",
            (record_id,),
        ).fetchall()
        c.execute(
            "UPDATE incidents SET status='revoked',updated_at=? "
            "WHERE pattern_id=? AND status IN ('candidate','active')",
            (stamp, record_id),
        )
        for child in children:
            incident = incident_value(c, child[0])
            c.execute(
                "UPDATE incidents SET content_hash=? WHERE incident_id=?",
                (incident_content_hash(incident), child[0]),
            )
        cascaded = len(children)
    record_event(
        c,
        "cloud-tombstone-applied",
        {
            "record_type": record_type,
            "record_id": record_id,
            "final_status": final_status,
            "cascaded_incidents": cascaded,
        },
    )
    return "applied" if changed or cascaded else "duplicate"


def sync_import_content_hash(
    patterns: Iterable[Mapping[str, Any]],
    incidents: Iterable[Mapping[str, Any]],
    tool_records: Iterable[Mapping[str, Any]],
    tombstones: Iterable[Mapping[str, Any]],
    consumption_events: Iterable[Mapping[str, Any]] = (),
) -> str:
    """Hash one normalized import batch for crash-safe replay detection."""
    payload = {
        "patterns": sorted(
            (dict(item) for item in patterns),
            key=lambda item: str(
                item.get("pattern_id") or item.get("lesson_id") or ""
            ),
        ),
        "incidents": sorted(
            (dict(item) for item in incidents),
            key=lambda item: str(item.get("incident_id") or ""),
        ),
        "tool_experience": sorted(
            (dict(item) for item in tool_records),
            key=lambda item: (
                str(item.get("tool_name") or ""),
                str(item.get("category") or ""),
            ),
        ),
        "consumption_events": sorted(
            (dict(item) for item in consumption_events),
            key=lambda item: str(item.get("event_id") or ""),
        ),
        "tombstones": sorted(
            (dict(item) for item in tombstones),
            key=lambda item: (
                str(item.get("record_type") or ""),
                str(item.get("record_id") or ""),
            ),
        ),
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sync_import_receipt(
    path: str | os.PathLike[str], snapshot_id: str
) -> dict[str, Any] | None:
    """Return the Ledger-side receipt for one remote snapshot, if committed."""
    key = sanitize_text(snapshot_id, "snapshot_id")
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        row = c.execute(
            "SELECT snapshot_id,manifest_hash,content_hash,record_count,created_at,committed_at "
            "FROM sync_import_receipts WHERE snapshot_id=?",
            (key,),
        ).fetchone()
    return dict(row) if row else None


def import_synced_records(
    path: str | os.PathLike[str],
    patterns: Iterable[Mapping[str, Any]] = (),
    incidents: Iterable[Mapping[str, Any]] = (),
    tool_records: Iterable[Mapping[str, Any]] = (),
    tombstones: Iterable[Mapping[str, Any]] = (),
    consumption_events: Iterable[Mapping[str, Any]] = (),
    *,
    allow_remote_active: bool = True,
    actor: str = "supervisor",
    snapshot_id: str | None = None,
    manifest_hash: str = "",
) -> dict[str, Any]:
    """原子导入一个已验证的云端快照。"""
    if actor != "supervisor":
        raise LessonLedgerError("云端知识导入必须由 Supervisor 提交")
    pattern_items = [
        synced_pattern(item, allow_remote_active=allow_remote_active)
        for item in patterns
    ]
    if len({item["pattern_id"] for item in pattern_items}) != len(pattern_items):
        raise LessonLedgerError("导入批次内 pattern_id 不得重复")
    tool_items = normalize_tool_experience_records(tool_records)
    incident_raw = list(incidents)
    incident_items = [
        synced_incident(raw, allow_remote_active=allow_remote_active)
        for raw in incident_raw
    ]
    tombstone_items = [dict(item) for item in tombstones]
    consumption_items = []
    for raw in consumption_events:
        if not isinstance(raw, Mapping):
            raise LessonLedgerError("云端 consumption event 必须是对象")
        item = {key: str(raw.get(key) or "") for key in (
            "event_id", "subject_type", "subject_id", "event_type",
            "lifecycle_id", "run_id", "created_at",
        )}
        if not item["event_id"] or item["event_type"] not in {
            "selected", "retrieved", "feedback",
        }:
            raise LessonLedgerError("云端 consumption event 无效")
        consumption_items.append(item)
    snapshot_key = sanitize_text(snapshot_id, "snapshot_id") if snapshot_id else None
    remote_manifest_hash = str(manifest_hash or "")
    content_hash = sync_import_content_hash(
        pattern_items, incident_items, tool_items, tombstone_items,
        consumption_items,
    )
    record_count = (
        len(pattern_items)
        + len(incident_raw)
        + len(tool_items)
        + len(tombstone_items)
        + len(consumption_items)
    )
    if len({item["incident_id"] for item in incident_items}) != len(incident_items):
        raise LessonLedgerError("导入批次内 incident_id 不得重复")

    def go(c: sqlite3.Connection):
        if snapshot_key:
            receipt = c.execute(
                "SELECT snapshot_id,manifest_hash,content_hash,record_count,created_at,committed_at "
                "FROM sync_import_receipts WHERE snapshot_id=?",
                (snapshot_key,),
            ).fetchone()
            if receipt:
                if receipt[2] != content_hash or (
                    remote_manifest_hash
                    and receipt[1]
                    and receipt[1] != remote_manifest_hash
                ):
                    raise LessonLedgerError(
                        f"snapshot_id 已存在但内容不一致，拒绝重放: {snapshot_key}"
                    )
                return {
                    "status": "duplicate",
                    "receipt": dict(receipt),
                    "patterns": {"created": 0, "activated": 0, "duplicate": 0},
                    "incidents": {"created": 0, "activated": 0, "duplicate": 0},
                    "tools": {"imported": [], "duplicates": [], "stale": []},
                    "consumption_events": 0,
                    "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[
                        0
                    ],
                }
        pattern_result = {"created": 0, "activated": 0, "duplicate": 0}
        for item in pattern_items:
            pattern_result[import_synced_pattern_tx(c, item)] += 1
        incident_result = {"created": 0, "activated": 0, "duplicate": 0}
        for raw in incident_raw:
            item = synced_incident(
                raw, allow_remote_active=allow_remote_active, c=c
            )
            incident_result[import_synced_incident_tx(c, item)] += 1
        tool_result = import_tool_experience_tx(c, tool_items)
        consumption_result = {"recorded": 0, "duplicate": 0}
        for item in consumption_items:
            status = record_consumption_event_tx(c, item)["status"]
            consumption_result[status] += 1
        for item in tombstone_items:
            apply_synced_tombstone_tx(c, item)
        if snapshot_key:
            now = now_iso()
            c.execute(
                "INSERT INTO sync_import_receipts(snapshot_id,manifest_hash,content_hash,"
                "record_count,created_at,committed_at) VALUES(?,?,?,?,?,?)",
                (
                    snapshot_key,
                    remote_manifest_hash,
                    content_hash,
                    record_count,
                    now,
                    now,
                ),
            )
            record_event(
                c,
                "cloud-import-committed",
                {
                    "snapshot_id": snapshot_key,
                    "content_hash": content_hash,
                    "record_count": record_count,
                },
            )
        return {
            "status": "imported",
            "patterns": pattern_result,
            "incidents": incident_result,
            "tools": tool_result,
            "consumption_events": consumption_result["recorded"],
            "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }

    return ledger_transaction(path, go)


def metrics(path: str | os.PathLike[str]) -> dict[str, Any]:
    """生成错题本与工具知识库全量审计指标。"""
    db = database_path(path)
    with connect_ledger(db, readonly=True) as c:
        validate_pattern(c, db)
        counts = {x: 0 for x in PATTERN_OUTCOMES}
        for r in c.execute(
            "SELECT outcome,COUNT(*) FROM feedback GROUP BY outcome"
        ):
            counts[r[0]] = r[1]
        status = {
            r[0]: r[1]
            for r in c.execute(
                "SELECT status,COUNT(*) FROM patterns GROUP BY status"
            )
        }
        total = sum(status.values())
        feedback = sum(counts.values())
        den = counts["hit"] + counts["false_positive"] + counts["rejected"]
        replayable = c.execute(
            "SELECT COUNT(*) FROM patterns p WHERE EXISTS "
            "(SELECT 1 FROM probes WHERE pattern_id=p.pattern_id AND kind='probe') "
            "AND EXISTS (SELECT 1 FROM probes WHERE pattern_id=p.pattern_id AND kind='negative_control') "
            "AND EXISTS (SELECT 1 FROM probes WHERE pattern_id=p.pattern_id AND kind='evidence')"
        ).fetchone()[0]
        guardrail_total = c.execute(
            "SELECT COUNT(*) FROM patterns WHERE EXISTS "
            "(SELECT 1 FROM probes WHERE pattern_id=patterns.pattern_id AND kind='false_positive')"
        ).fetchone()[0]
        delayed_rows = [
            (
                json.loads(row[3]).get("pattern_id"),
                json.loads(row[3]).get("signature"),
            )
            for row in c.execute(
                "SELECT seq,type,at,payload,prev_hash,hash FROM events "
                "WHERE type='pattern-delayed-hit'"
            )
            if isinstance(json.loads(row[3]), dict)
        ]
        delayed_hits = len(delayed_rows)
        delayed_hit_pattern_ids = sorted(
            {key for key, _ in delayed_rows if key is not None}
        )
        incidents_total = c.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        incidents_with_diff = c.execute(
            "SELECT COUNT(*) FROM incidents WHERE diff_ref IS NOT NULL OR commit_ref IS NOT NULL"
        ).fetchone()[0]
        tool_feedback_total = c.execute(
            "SELECT COUNT(*) FROM tool_usage"
        ).fetchone()[0]
        tool_level_rows = c.execute(
            "SELECT tool_name,COUNT(DISTINCT lifecycle_id) FROM tool_usage "
            "WHERE outcome='hit' GROUP BY tool_name"
        ).fetchall()
        tool_levels = {"active": 0, "candidate": 0, "unproven": 0}
        seen_tools = {str(row[0]) for row in tool_level_rows}
        for row in tool_level_rows:
            level = tool_experience_level(int(row[1]))
            tool_levels[level] = tool_levels.get(level, 0) + 1
        unproven = (
            c.execute(
                "SELECT COUNT(DISTINCT tool_name) FROM tool_usage"
            ).fetchone()[0]
            - len(seen_tools)
        )
        tool_levels["unproven"] += max(0, unproven)
        knowledge_consumption_events = c.execute(
            "SELECT COUNT(*) FROM consumption_events"
        ).fetchone()[0]
        patterns_retrieved = c.execute(
            "SELECT COUNT(DISTINCT subject_id) FROM consumption_events "
            "WHERE subject_type='pattern' AND event_type='retrieved'"
        ).fetchone()[0]
        incidents_retrieved = c.execute(
            "SELECT COUNT(DISTINCT subject_id) FROM consumption_events "
            "WHERE subject_type='incident' AND event_type='retrieved'"
        ).fetchone()[0]
        tools_retrieved = c.execute(
            "SELECT COUNT(DISTINCT subject_id) FROM consumption_events "
            "WHERE subject_type='tool_experience' AND event_type='retrieved'"
        ).fetchone()[0]
        return {
            "schema_version": SCHEMA_VERSION,
            "storage_backend": "sqlite",
            "database": str(db),
            "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "events_archived": sum(
                int(entry.get("record_count", 0))
                for entry in archive_manifest(c)
            ),
            "lessons_total": total,
            "patterns_total": total,
            "lessons_active": status.get("active", 0) + status.get("candidate", 0),
            "patterns_by_status": status,
            "occurrences_total": c.execute(
                "SELECT COUNT(*) FROM occurrences"
            ).fetchone()[0],
            "probes_total": c.execute("SELECT COUNT(*) FROM probes").fetchone()[0],
            "replayable_patterns": replayable,
            "false_positive_guardrail_patterns": guardrail_total,
            "reviews_total": c.execute("SELECT COUNT(*) FROM reviews").fetchone()[
                0
            ],
            "incidents_total": incidents_total,
            "incidents_with_diff": incidents_with_diff,
            "tool_feedback_total": tool_feedback_total,
            "tools_total": c.execute(
                "SELECT COUNT(DISTINCT tool_name) FROM tool_usage"
            ).fetchone()[0],
            "tool_experience_levels": tool_levels,
            "knowledge_consumption_events": knowledge_consumption_events,
            "patterns_retrieved": patterns_retrieved,
            "incidents_retrieved": incidents_retrieved,
            "tools_retrieved": tools_retrieved,
            "delayed_hits": delayed_hits,
            "delayed_hit_pattern_ids": delayed_hit_pattern_ids,
            "lessons_used": c.execute(
                "SELECT COUNT(*) FROM patterns WHERE use_count>0"
            ).fetchone()[0],
            "usage_total": c.execute(
                "SELECT COALESCE(SUM(use_count),0) FROM patterns"
            ).fetchone()[0],
            "feedback_total": feedback,
            "outcomes": counts,
            "feedback_effective_total": feedback - counts.get("not_applicable", 0),
            "feedback_precision": round(counts["hit"] / den, 4) if den else None,
            "hit_rate": round(
                counts["hit"] / (feedback - counts.get("not_applicable", 0)), 4
            )
            if feedback - counts.get("not_applicable", 0)
            else None,
            "feedback_recall": round(
                counts["hit"] / (counts["hit"] + counts["miss"]), 4
            )
            if counts["hit"] + counts["miss"]
            else None,
            "feedback_effectiveness": round(
                counts["hit"] / (feedback - counts.get("not_applicable", 0)), 4
            )
            if feedback - counts.get("not_applicable", 0)
            else None,
            "state_hash": c.execute(
                "SELECT value FROM meta WHERE key='state_hash'"
            ).fetchone()[0],
            "head_hash": c.execute(
                "SELECT value FROM meta WHERE key='head_hash'"
            ).fetchone()[0],
            "seed_count": int(
                c.execute("SELECT value FROM meta WHERE key='seed_count'").fetchone()[
                    0
                ]
            )
            if c.execute("SELECT 1 FROM meta WHERE key='seed_count'").fetchone()
            else total,
        }
