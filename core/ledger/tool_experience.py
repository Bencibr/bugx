#!/usr/bin/env python3
"""core/ledger/tool_experience.py - 工具经验模型、归因打分与云端协同 (DOC-069)。"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


TOOL_LOOKUP_LIMIT = 4
TOOL_LOOKUP_TOKEN_BUDGET = 800
TOOL_LOOKUP_BYTE_BUDGET = 6000
TOOL_EXPERIENCE_LEVELS = {"unproven", "candidate", "active"}


# DES-02：原先这里的 ``_LedgerProxy`` 经 ``sys.modules.get("lesson_ledger")``
# 反向取符号，与顶层 Facade 构成双向依赖环；现改为经同级解析表
# （``core/ledger/_facade.py``）取**定义该符号的兄弟模块**，且延迟导入，
# 因此 ``ledger/*`` 不再依赖 ``lesson_ledger``，方向变为单向。
from core.ledger._facade import LedgerProxy

_ledger = LedgerProxy()
class _ErrorMeta(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, _ledger.LessonLedgerError)

    def __subclasscheck__(cls, subclass):
        return issubclass(subclass, _ledger.LessonLedgerError)


class LessonLedgerError(Exception, metaclass=_ErrorMeta):
    def __new__(cls, *args, **kwargs):
        return _ledger.LessonLedgerError(*args, **kwargs)


def _tx(*args, **kwargs): return _ledger.ledger_transaction(*args, **kwargs)
def _conn(*args, **kwargs): return _ledger.connect_ledger(*args, **kwargs)
def _meta_value(*args, **kwargs): return _ledger.get_meta_value(*args, **kwargs)
def database_path(*args, **kwargs): return _ledger.database_path(*args, **kwargs)
def _validate(*args, **kwargs): return _ledger.validate_pattern(*args, **kwargs)
def _event(*args, **kwargs): return _ledger.record_event(*args, **kwargs)
def _canon(*args, **kwargs): return _ledger.canonical_bytes(*args, **kwargs)
def _now(*args, **kwargs): return _ledger.now_iso(*args, **kwargs)
def _text(*args, **kwargs): return _ledger.sanitize_text(*args, **kwargs)
def _fp(*args, **kwargs): return _ledger.normalize_finding_fingerprint(*args, **kwargs)
def _confirmed_finding_digest(*args, **kwargs): return _ledger.confirmed_finding_digest(*args, **kwargs)
def _norm(*args, **kwargs): return _ledger.normalize_id(*args, **kwargs)
def estimate_tokens(*args, **kwargs): return _ledger.estimate_tokens(*args, **kwargs)


def normalize_tool_name(value: Any) -> str:
    value = _text(value, "tool_name")
    if not _ledger.TOOL_NAME_RE.fullmatch(value):
        raise LessonLedgerError("tool_name 格式无效（允许字母数字与 ._/+-，且不以分隔符开头）")
    if _ledger.SECRET_REGEX.search(value):
        raise LessonLedgerError("tool_name 包含疑似凭据")
    return value


def extract_feedback_tools(payload: Mapping[str, Any]) -> list[str]:
    raw = payload.get("tools")
    if raw is None and isinstance(payload.get("tool"), str):
        raw = [payload["tool"]]
    if raw is None and isinstance(payload.get("finding"), Mapping):
        raw = payload["finding"].get("tools")
    if not isinstance(raw, list) or not raw:
        raise LessonLedgerError("tool-feedback 必须提供非空 tools 数组（或 finding.tools）")
    if len(raw) > _ledger.MAX_TOOLS_PER_FEEDBACK:
        raise LessonLedgerError(f"tools 单次反馈不得超过 {_ledger.MAX_TOOLS_PER_FEEDBACK} 个")
    result = [normalize_tool_name(x) for x in raw]
    if len(result) != len(set(result)):
        raise LessonLedgerError("tools 不得重复")
    return result


def record_tool_feedback(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> dict[str, Any]:
    """记录「工具 × 问题类别」归因反馈；hit 必须绑定已确认 finding。"""
    if payload.get("actor", "supervisor") != "supervisor":
        raise LessonLedgerError("工具反馈写入必须由 Supervisor 提交")
    life = _text(payload.get("lifecycle_id"), "lifecycle_id")
    rnd = payload.get("round")
    if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
        raise LessonLedgerError("工具反馈 round 必须是正整数")
    outcome = _text(payload.get("outcome"), "outcome")
    if outcome not in _ledger.OUTCOMES:
        raise LessonLedgerError("outcome 无效")
    category = _text(payload.get("category"), "category")
    tools = extract_feedback_tools(payload)
    fp = _fp(payload.get("finding_fingerprint"))
    confirmation_digest = _confirmed_finding_digest(payload) if outcome == "hit" else None

    def go(c):
        if not c.execute(
            "SELECT 1 FROM reviews WHERE lifecycle_id=? AND round=?", (life, rnd),
        ).fetchone():
            raise LessonLedgerError("工具反馈必须先有对应 cycle-review")
        if not c.execute(
            "SELECT 1 FROM review_findings WHERE lifecycle_id=? AND round=? AND fingerprint=?",
            (life, rnd, fp),
        ).fetchone():
            raise LessonLedgerError("工具反馈的 finding_fingerprint 未在对应 cycle-review 中声明")
        stamp = _now()
        for name in tools:
            try:
                c.execute(
                    "INSERT INTO tool_usage VALUES (?,?,?,?,?,?,?)",
                    (life, rnd, name, category, outcome, fp, stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise LessonLedgerError(
                    f"同一轮内工具反馈不得重复提交: {name}/{category}/{fp}"
                ) from exc
        event = {
            "lifecycle_id": life, "round": rnd, "tool_names": tools,
            "category": category, "finding_fingerprint": fp, "outcome": outcome,
        }
        if confirmation_digest:
            event["confirmation_digest"] = confirmation_digest
        _event(c, "tool-feedback", event)
        return {
            "status": "recorded", "tool_names": tools, "category": category,
            "outcome": outcome,
            "revision": c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }
    return _tx(path, go)


def tool_experience_level(hit_lifecycles: int) -> str:
    if hit_lifecycles >= 2:
        return "active"
    if hit_lifecycles == 1:
        return "candidate"
    return "unproven"


def fetch_tool_aggregates(c: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    aggregates: dict[str, dict[str, Any]] = {}
    for tool_name, category, outcome, life, stamp in c.execute(
        "SELECT tool_name,category,outcome,lifecycle_id,created_at FROM tool_usage"
    ):
        item = aggregates.setdefault(str(tool_name), {
            "tool_name": str(tool_name), "use_count": 0,
            "hit_count": 0, "miss_count": 0,
            "false_positive_count": 0, "rejected_count": 0,
            "hit_lifecycles": set(), "categories": {},
            "first_used_at": str(stamp), "last_used_at": str(stamp),
        })
        item["use_count"] += 1
        field = {
            "hit": "hit_count", "miss": "miss_count",
            "false_positive": "false_positive_count", "rejected": "rejected_count",
        }[str(outcome)]
        item[field] += 1
        if str(outcome) == "hit":
            item["hit_lifecycles"].add(str(life))
        cat = item["categories"].setdefault(str(category), {"category": str(category), "feedback": 0, "hits": 0})
        cat["feedback"] += 1
        if str(outcome) == "hit":
            cat["hits"] += 1
        if str(stamp) < item["first_used_at"]:
            item["first_used_at"] = str(stamp)
        if str(stamp) > item["last_used_at"]:
            item["last_used_at"] = str(stamp)
    # Cloud/global records are governed snapshots, not lifecycle detail. Only
    # explicitly activated snapshots enter the runtime aggregate.
    for row in c.execute(
        "SELECT tool_name,category,use_count,hit_count,miss_count,"
        "false_positive_count,rejected_count,independent_lifecycle_count,"
        "first_observed_at,last_observed_at FROM tool_experience "
        "WHERE experience_level='active' AND deleted=0"
    ):
        (tool_name, category, use_count, hit_count, miss_count,
         false_positive_count, rejected_count, independent_lifecycles,
         first_seen, last_seen) = row
        first_seen = str(first_seen or last_seen or "")
        last_seen = str(last_seen or first_seen)
        item = aggregates.setdefault(str(tool_name), {
            "tool_name": str(tool_name), "use_count": 0,
            "hit_count": 0, "miss_count": 0,
            "false_positive_count": 0, "rejected_count": 0,
            "hit_lifecycles": set(), "categories": {},
            "first_used_at": first_seen, "last_used_at": last_seen,
        })
        item["use_count"] += int(use_count or 0)
        item["hit_count"] += int(hit_count or 0)
        item["miss_count"] += int(miss_count or 0)
        item["false_positive_count"] += int(false_positive_count or 0)
        item["rejected_count"] += int(rejected_count or 0)
        item["hit_lifecycles"].update(
            f"imported:{tool_name}:{category}:{index}"
            for index in range(int(independent_lifecycles or 0))
        )
        cat = item["categories"].setdefault(
            str(category), {"category": str(category), "feedback": 0, "hits": 0},
        )
        cat["feedback"] += int(use_count or 0)
        cat["hits"] += int(hit_count or 0)
        if first_seen and (not item["first_used_at"] or first_seen < item["first_used_at"]):
            item["first_used_at"] = first_seen
        if last_seen > item["last_used_at"]:
            item["last_used_at"] = last_seen
    return aggregates


def normalize_tool_value(item: Mapping[str, Any]) -> dict[str, Any]:
    hit_lifecycles = len(item["hit_lifecycles"])
    denominator = (
        item["hit_count"] + item["false_positive_count"] + item["rejected_count"]
    )
    categories = sorted(
        item["categories"].values(),
        key=lambda x: (-x["hits"], -x["feedback"], x["category"]),
    )
    return {
        "tool_name": item["tool_name"],
        "experience_level": tool_experience_level(hit_lifecycles),
        "hit_lifecycles": hit_lifecycles,
        "use_count": item["use_count"], "hit_count": item["hit_count"],
        "miss_count": item["miss_count"],
        "false_positive_count": item["false_positive_count"],
        "rejected_count": item["rejected_count"],
        "precision": round(item["hit_count"] / denominator, 4) if denominator else None,
        "top_categories": categories[:3],
        "first_used_at": item["first_used_at"],
        "last_used_at": item["last_used_at"],
    }


def tool_metrics(path: str | os.PathLike[str]) -> dict[str, Any]:
    """按工具聚合归因反馈并派生经验等级（unproven/candidate/active）。"""
    db = database_path(path)
    with _conn(db, readonly=True) as c:
        _validate(c, db)
        tools = [
            normalize_tool_value(item)
            for item in fetch_tool_aggregates(c).values()
        ]
        tools.sort(key=lambda x: (
            -x["hit_lifecycles"], -x["hit_count"], x["tool_name"],
        ))
        return {
            "schema_version": _ledger.SCHEMA_VERSION,
            "tools_total": len(tools),
            "tool_feedback_total": c.execute(
                "SELECT COUNT(*) FROM tool_usage"
            ).fetchone()[0],
            "levels": {
                level: sum(1 for x in tools if x["experience_level"] == level)
                for level in sorted(_ledger.TOOL_EXPERIENCE_LEVELS)
            },
            "tools": tools,
        }


def lookup_tools(path: str | os.PathLike[str], *, query: str = "", category: str | None = None,
                 limit: int = TOOL_LOOKUP_LIMIT,
                 token_budget: int | None = TOOL_LOOKUP_TOKEN_BUDGET,
                 byte_budget: int | None = TOOL_LOOKUP_BYTE_BUDGET) -> list[dict[str, Any]]:
    """有预算约束的工具经验卡片检索：名字/类别词命中 + 命中战果加权。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise LessonLedgerError("limit 必须在 1..20")
    tokens = [_norm(x) for x in re.findall(r"[\w-]+", query, re.UNICODE)]
    cjk = re.findall(r"[\u4e00-\u9fff]", query)
    if len(cjk) >= 2:
        tokens.extend(_norm(a + b) for a, b in zip(cjk, cjk[1:]))
    db = database_path(path)
    with _conn(db, readonly=True) as c:
        _validate(c, db)
        rows = []
        for item in fetch_tool_aggregates(c).values():
            value = normalize_tool_value(item)
            categories = [x["category"] for x in value["top_categories"]]
            if category and _norm(category) not in map(_norm, categories) \
                    and _norm(category) not in map(_norm, item["categories"]):
                continue
            hay = _norm(" ".join([value["tool_name"], *categories]))
            matches = sum(t in hay for t in tokens)
            if tokens and not matches:
                continue
            score = (
                matches * 10
                + (8 if category else 0)
                + value["hit_lifecycles"] * 3
                + value["hit_count"]
                + (3 if value["experience_level"] == "active" else 0)
            )
            rows.append({**value, "match_score": score})
        rows.sort(key=lambda x: (-x["match_score"], x["tool_name"]))
        result = []
        total_b = total_t = 0
        for row in rows:
            data = _canon(row)
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


def tool_matrix(path: str | os.PathLike[str]) -> dict[str, Any]:
    """数据驱动的「问题类别 → 工具」映射：按实测命中排序，供场景选型校正。"""
    db = database_path(path)
    with _conn(db, readonly=True) as c:
        _validate(c, db)
        matrix: dict[str, list[dict[str, Any]]] = {}
        for item in fetch_tool_aggregates(c).values():
            value = normalize_tool_value(item)
            for cat in item["categories"].values():
                if cat["hits"] <= 0:
                    continue
                matrix.setdefault(cat["category"], []).append({
                    "tool_name": value["tool_name"],
                    "hits": cat["hits"],
                    "feedback": cat["feedback"],
                    "experience_level": value["experience_level"],
                })
        categories = [
            {
                "category": name,
                "tools": sorted(
                    rows, key=lambda x: (-x["hits"], -x["feedback"], x["tool_name"])
                )[:4],
            }
            for name, rows in sorted(matrix.items())
        ]
        return {
            "schema_version": _ledger.SCHEMA_VERSION,
            "categories_total": len(categories),
            "categories": categories,
        }


def tool_experience_content(item: Mapping[str, Any]) -> dict[str, Any]:
    """Typed projection used both for hashing and persistence."""
    content = {field: item[field] for field in _ledger.TOOL_EXPERIENCE_FIELDS}
    return {key: content[key] for key in sorted(content)}


def tool_experience_hash(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canon(content)).hexdigest()


def validate_tool_experience_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """DOC-008 §7 强类型校验：未知字段直接拒绝（fail-closed）。"""
    if not isinstance(raw, Mapping):
        raise LessonLedgerError("tool experience 记录必须是对象")
    keys = set(map(str, raw.keys()))
    forbidden = sorted(keys & _ledger.TOOL_EXPERIENCE_FORBIDDEN_KEYS)
    if forbidden:
        raise LessonLedgerError(f"tool experience 含禁止上云字段，拒绝导入: {', '.join(forbidden)}")
    unknown = sorted(keys - set(_ledger.TOOL_EXPERIENCE_FIELDS))
    if unknown:
        raise LessonLedgerError(f"tool experience 含未声明字段，拒绝导入: {', '.join(unknown)}")
    missing = [field for field in ("tool_name", "category", "record_version") if field not in raw]
    if missing:
        raise LessonLedgerError(f"tool experience 缺少必填字段: {', '.join(missing)}")
    record: dict[str, Any] = {}
    record["tool_name"] = normalize_tool_name(raw.get("tool_name"))
    record["category"] = _text(raw.get("category"), "category")
    level = raw.get("experience_level")
    if level not in _ledger.TOOL_EXPERIENCE_LEVELS:
        raise LessonLedgerError("tool experience experience_level 无效")
    version = raw.get("record_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise LessonLedgerError("tool experience record_version 必须是正整数")
    record["experience_level"] = str(level)
    record["record_version"] = int(version)
    for field in ("use_count", "hit_count", "miss_count", "false_positive_count",
                  "rejected_count", "independent_lifecycle_count"):
        value = raw.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LessonLedgerError(f"tool experience {field} 必须是非负整数")
        record[field] = int(value)
    for field in ("first_observed_at", "last_observed_at", "tool_version", "device_id"):
        value = raw.get(field)
        record[field] = _text(value, field) if value not in (None, "") else None
    deleted = raw.get("deleted", False)
    if not isinstance(deleted, bool):
        raise LessonLedgerError("tool experience deleted 必须是布尔值")
    record["deleted"] = deleted
    deleted_at = raw.get("deleted_at")
    record["deleted_at"] = _text(deleted_at, "deleted_at") if deleted_at not in (None, "") else None
    if record["deleted"] and not record["deleted_at"]:
        raise LessonLedgerError("tombstone 记录必须携带 deleted_at")
    return record


def export_tool_experience(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """从本地事实表导出强类型聚合 Tool Experience（只含聚合，无 lifecycle 明细）。"""
    db = database_path(path)
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    with _conn(db, readonly=True) as c:
        _validate(c, db)
        for row in c.execute(
            "SELECT tool_name,category,COUNT(*) AS feedback_total,"
            "SUM(outcome='hit'),SUM(outcome='miss'),SUM(outcome='false_positive'),"
            "SUM(outcome='rejected'),COUNT(DISTINCT lifecycle_id),"
            "COUNT(DISTINCT CASE WHEN outcome='hit' THEN lifecycle_id END),"
            "MIN(created_at),MAX(created_at) FROM tool_usage "
            "GROUP BY tool_name,category ORDER BY tool_name,category"
        ):
            (name, category, total, hits, misses, fps, rejected,
             lifecycles, hit_lifecycles, first_seen, last_seen) = row
            content = {
                "tool_name": str(name),
                "category": str(category),
                "experience_level": tool_experience_level(int(hit_lifecycles or 0)),
                "use_count": int(total),
                "hit_count": int(hits or 0),
                "miss_count": int(misses or 0),
                "false_positive_count": int(fps or 0),
                "rejected_count": int(rejected or 0),
                "independent_lifecycle_count": int(lifecycles or 0),
                "first_observed_at": str(first_seen),
                "last_observed_at": str(last_seen),
                "tool_version": None,
                "record_version": max(1, int(total)),
                "device_id": None,
                "deleted": False,
                "deleted_at": None,
            }
            normalized = tool_experience_content(content)
            records_by_key[(normalized["tool_name"], normalized["category"])] = normalized
        for row in c.execute(
            "SELECT tool_name,category,experience_level,record_version,use_count,"
            "hit_count,miss_count,false_positive_count,rejected_count,"
            "independent_lifecycle_count,first_observed_at,last_observed_at,"
            "tool_version,device_id,deleted,deleted_at FROM tool_experience "
            "WHERE deleted=1 ORDER BY tool_name,category"
        ):
            content = dict(zip((
                "tool_name", "category", "experience_level", "record_version",
                "use_count", "hit_count", "miss_count", "false_positive_count",
                "rejected_count", "independent_lifecycle_count", "first_observed_at",
                "last_observed_at", "tool_version", "device_id", "deleted", "deleted_at",
            ), row))
            content["tool_name"] = str(content["tool_name"])
            content["category"] = str(content["category"])
            content["deleted"] = bool(content["deleted"])
            records_by_key[(content["tool_name"], content["category"])] = (
                tool_experience_content(content)
            )
        # Publisher-activated remote snapshots have no local tool_usage rows,
        # but they still must be propagated by the next private sync.  Local
        # usage aggregation wins for an identical key to avoid double-counting;
        # deleted snapshots below remain authoritative tombstones.
        for row in c.execute(
            "SELECT tool_name,category,experience_level,record_version,use_count,"
            "hit_count,miss_count,false_positive_count,rejected_count,"
            "independent_lifecycle_count,first_observed_at,last_observed_at,"
            "tool_version,device_id,deleted,deleted_at FROM tool_experience "
            "WHERE deleted=0 AND experience_level='active' ORDER BY tool_name,category"
        ):
            content = dict(zip((
                "tool_name", "category", "experience_level", "record_version",
                "use_count", "hit_count", "miss_count", "false_positive_count",
                "rejected_count", "independent_lifecycle_count", "first_observed_at",
                "last_observed_at", "tool_version", "device_id", "deleted", "deleted_at",
            ), row))
            content["tool_name"] = str(content["tool_name"])
            content["category"] = str(content["category"])
            content["deleted"] = bool(content["deleted"])
            records_by_key.setdefault(
                (content["tool_name"], content["category"]),
                tool_experience_content(content),
            )
    return [records_by_key[key] for key in sorted(records_by_key)]


def normalize_tool_experience_records(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(records, Iterable) or isinstance(records, (str, bytes, Mapping)):
        raise LessonLedgerError("records 必须是对象数组")
    normalized = []
    for raw in records:
        item = validate_tool_experience_record(raw)
        if item["experience_level"] == "active":
            item["experience_level"] = "candidate"
        item["origin"] = "cloud"
        item["content_hash"] = tool_experience_hash(tool_experience_content(item))
        normalized.append(item)
    keys = [(item["tool_name"], item["category"]) for item in normalized]
    if len(keys) != len(set(keys)):
        raise LessonLedgerError("导入批次内 tool/category 不得重复")
    return normalized


def import_tool_experience_tx(
    c: sqlite3.Connection, normalized: list[dict[str, Any]],
) -> dict[str, Any]:
    imported: list[list[str]] = []
    duplicates: list[list[str]] = []
    stale: list[list[str]] = []
    tombstone_protected: list[list[str]] = []
    columns = ("tool_name", "category", "experience_level", "record_version",
               "use_count", "hit_count", "miss_count", "false_positive_count",
               "rejected_count", "independent_lifecycle_count",
               "first_observed_at", "last_observed_at", "tool_version",
               "device_id", "deleted", "deleted_at", "origin", "content_hash")
    for item in normalized:
        key = [item["tool_name"], item["category"]]
        row = c.execute(
            "SELECT experience_level,record_version,deleted,content_hash FROM tool_experience "
            "WHERE tool_name=? AND category=?", key,
        ).fetchone()
        if row is None:
            values = tuple(item[name] for name in columns[:14])
            values += (1 if item["deleted"] else 0, item["deleted_at"],
                       item["origin"], item["content_hash"])
            c.execute(
                f"INSERT INTO tool_experience VALUES ({','.join('?' * len(columns))})",
                values,
            )
            imported.append(key)
            continue
        _, stored_version, stored_deleted, stored_hash = (
            row[0], int(row[1]), bool(row[2]), row[3],
        )
        if item["content_hash"] == stored_hash:
            duplicates.append(key)
            continue
        if item["record_version"] == stored_version \
                and not (item["deleted"] and not stored_deleted):
            raise LessonLedgerError(
                "tool experience 版本冲突（同版本不同内容），拒绝覆盖: "
                f"{item['tool_name']}/{item['category']}"
            )
        if stored_deleted:
            if item["deleted"] and item["record_version"] > stored_version:
                c.execute(
                    "UPDATE tool_experience SET experience_level=?,record_version=?,"
                    "use_count=?,hit_count=?,miss_count=?,false_positive_count=?,"
                    "rejected_count=?,independent_lifecycle_count=?,first_observed_at=?,"
                    "last_observed_at=?,tool_version=?,device_id=?,deleted=1,"
                    "deleted_at=?,origin=?,content_hash=? WHERE tool_name=? AND category=?",
                    (item["experience_level"], item["record_version"], item["use_count"],
                     item["hit_count"], item["miss_count"], item["false_positive_count"],
                     item["rejected_count"], item["independent_lifecycle_count"],
                     item["first_observed_at"], item["last_observed_at"], item["tool_version"],
                     item["device_id"], item["deleted_at"], item["origin"],
                     item["content_hash"], *key),
                )
                imported.append(key)
            elif item["deleted"]:
                stale.append(key)
            else:
                tombstone_protected.append(key)
            continue
        if item["record_version"] < stored_version:
            stale.append(key)
            continue
        c.execute(
            "UPDATE tool_experience SET experience_level=?,record_version=?,use_count=?,"
            "hit_count=?,miss_count=?,false_positive_count=?,rejected_count=?,"
            "independent_lifecycle_count=?,first_observed_at=?,last_observed_at=?,"
            "tool_version=?,device_id=?,deleted=?,deleted_at=?,content_hash=? "
            "WHERE tool_name=? AND category=?",
            (item["experience_level"], item["record_version"], item["use_count"],
             item["hit_count"], item["miss_count"], item["false_positive_count"],
             item["rejected_count"], item["independent_lifecycle_count"],
             item["first_observed_at"], item["last_observed_at"], item["tool_version"],
             item["device_id"], 1 if item["deleted"] else 0, item["deleted_at"],
             item["content_hash"], *key),
        )
        imported.append(key)
    if imported or duplicates or stale or tombstone_protected:
        _event(c, "tool-experience-imported", {
            "imported": [f"{k[0]}/{k[1]}" for k in imported],
            "duplicates": [f"{k[0]}/{k[1]}" for k in duplicates],
            "stale": [f"{k[0]}/{k[1]}" for k in stale],
            "tombstone_protected": [f"{k[0]}/{k[1]}" for k in tombstone_protected],
        })
    return {"status": "imported" if imported else "no-op",
            "imported": [f"{k[0]}/{k[1]}" for k in imported],
            "duplicates": [f"{k[0]}/{k[1]}" for k in duplicates],
            "stale": [f"{k[0]}/{k[1]}" for k in stale],
            "tombstone_protected": [f"{k[0]}/{k[1]}" for k in tombstone_protected]}


def import_tool_experience(path: str | os.PathLike[str], records: Iterable[Mapping[str, Any]],
                           *, actor: str = "supervisor") -> dict[str, Any]:
    """原子导入云端聚合 Tool Experience；导入后等级最多 candidate。"""
    if actor != "supervisor":
        raise LessonLedgerError("tool experience 导入必须由 Supervisor 提交")
    normalized = normalize_tool_experience_records(records)

    def go(c):
        result = import_tool_experience_tx(c, normalized)
        result["revision"] = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return result
    return _tx(path, go)


def activate_tool_experience(
    path: str | os.PathLike[str], tool_name: str, category: str,
    *, actor: str = "publisher",
) -> dict[str, Any]:
    """Activate a governed imported tool snapshot after two lifecycle hits."""
    if actor != "publisher":
        raise LessonLedgerError("全局工具经验激活必须由 publisher 执行")
    name = normalize_tool_name(tool_name)
    cat = _text(category, "category")

    def go(c):
        row = c.execute(
            "SELECT tool_name,category,experience_level,record_version,use_count,"
            "hit_count,miss_count,false_positive_count,rejected_count,"
            "independent_lifecycle_count,first_observed_at,last_observed_at,"
            "tool_version,device_id,deleted,deleted_at FROM tool_experience "
            "WHERE tool_name=? AND category=?", (name, cat),
        ).fetchone()
        if not row:
            raise LessonLedgerError("待激活的工具经验不存在")
        if row[2] != "candidate":
            raise LessonLedgerError("只有 candidate 工具经验可以激活")
        if bool(row[14]):
            raise LessonLedgerError("已删除的工具经验不能激活")
        if int(row[9] or 0) < 2 or int(row[5] or 0) < 2:
            raise LessonLedgerError("工具经验至少需要两个独立 lifecycle hit")
        fields = (
            "tool_name", "category", "experience_level", "record_version",
            "use_count", "hit_count", "miss_count", "false_positive_count",
            "rejected_count", "independent_lifecycle_count", "first_observed_at",
            "last_observed_at", "tool_version", "device_id", "deleted", "deleted_at",
        )
        content = dict(zip(fields, row))
        content["experience_level"] = "active"
        content["deleted"] = bool(content["deleted"])
        content_hash = tool_experience_hash(tool_experience_content(content))
        c.execute(
            "UPDATE tool_experience SET experience_level='active',content_hash=? "
            "WHERE tool_name=? AND category=?",
            (content_hash, name, cat),
        )
        _event(c, "tool-experience-activated", {
            "tool_name": name, "category": cat,
            "independent_lifecycle_count": int(row[9]),
        })
        return {"status": "active", "tool_name": name, "category": cat}

    return _tx(path, go)


def list_tool_experience(path: str | os.PathLike[str], *,
                         include_deleted: bool = False) -> list[dict[str, Any]]:
    """显式查询已持久化的 Tool Experience（本地聚合 + 远端 candidate 快照）。"""
    db = database_path(path)
    with _conn(db, readonly=True) as c:
        _validate(c, db)
        rows = c.execute(
            "SELECT tool_name,category,experience_level,record_version,use_count,hit_count,"
            "miss_count,false_positive_count,rejected_count,independent_lifecycle_count,"
            "first_observed_at,last_observed_at,tool_version,device_id,deleted,deleted_at,"
            "origin,content_hash FROM tool_experience ORDER BY tool_name,category"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(zip((
                "tool_name", "category", "experience_level", "record_version",
                "use_count", "hit_count", "miss_count", "false_positive_count",
                "rejected_count", "independent_lifecycle_count", "first_observed_at",
                "last_observed_at", "tool_version", "device_id", "deleted",
                "deleted_at", "origin", "content_hash",
            ), row))
            item["deleted"] = bool(item["deleted"])
            if item["deleted"] and not include_deleted:
                continue
            result.append(item)
        return result
