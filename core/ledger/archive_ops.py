#!/usr/bin/env python3
"""core/ledger/archive_ops.py - FTS 审计重建与事件分段归档 (DOC-036)。"""

from __future__ import annotations

import datetime as dt
import hashlib
import gzip
import io
import json
import os
import sqlite3
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any, Mapping

import sys

#: 归档段**压缩**体积上限。gzip 压缩比可超过 1000x，几 KB 的炸弹就能膨胀到
#: 数十 GB，因此压缩侧与解压侧都必须设限（SEC-08）。
MAX_SEGMENT_BYTES = 64 * 1024 * 1024
#: 归档段**解压后**累计字节上限。
MAX_SEGMENT_INFLATED_BYTES = 256 * 1024 * 1024
#: 分块解压的块大小。
_SEGMENT_CHUNK_BYTES = 1 << 20

# DES-02：原先这里的 ``_LedgerProxy`` 经 ``sys.modules.get("lesson_ledger")``
# 反向取符号，与顶层 Facade 构成双向依赖环；现改为经同级解析表
# （``core/ledger/_facade.py``）取**定义该符号的兄弟模块**，且延迟导入，
# 因此 ``ledger/*`` 不再依赖 ``lesson_ledger``，方向变为单向。
from core.ledger._facade import LedgerProxy

_ledger = LedgerProxy()
# _ledger.SCHEMA_VERSION delegates to _ledger.SCHEMA_VERSION
FTS_COLUMNS = ("pattern_id", "category", "title", "root_cause", "reflection", "probes", "aliases")


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
def _archive_manifest(*args, **kwargs): return _ledger.archive_manifest(*args, **kwargs)
def _incident_content(*args, **kwargs): return _ledger.incident_content(*args, **kwargs)

# ---- DOC-008 TASK-008-012/013：FTS 深度校验与原子重建、event 分段归档 ----

def fts_expected_row(c: sqlite3.Connection, key: str) -> list[str] | None:
    row = c.execute(
        "SELECT category,title,root_cause,reflection FROM patterns WHERE pattern_id=?", (key,)
    ).fetchone()
    if not row:
        return None
    probes = " ".join(x[0] for x in c.execute(
        "SELECT value FROM probes WHERE pattern_id=? AND kind!='card' ORDER BY ordinal", (key,)))
    aliases = " ".join(x[0] for x in c.execute(
        "SELECT alias FROM aliases WHERE pattern_id=?", (key,)))
    return [key, str(row[0]), str(row[1]), str(row[2]), str(row[3]), probes, aliases]


def fts_audit(c: sqlite3.Connection) -> list[str]:
    """比对事实表与 FTS 投影，返回 issue 列表（空列表即一致）。

    覆盖：missing row、orphan row、pattern_id 重复行，以及 category/title/
    root_cause/reflection/probes/aliases 七列投影不一致。
    """
    try:
        actual_rows = c.execute(
            f"SELECT {','.join(FTS_COLUMNS)} FROM patterns_fts"
        ).fetchall()
    except sqlite3.Error as exc:
        return [f"patterns_fts 无法读取: {exc}"]
    issues: list[str] = []
    seen: dict[str, list[list[str]]] = {}
    for row in actual_rows:
        key = str(row[0])
        seen.setdefault(key, []).append(["" if value is None else str(value) for value in row])
    for key, rows in sorted(seen.items()):
        if len(rows) > 1:
            issues.append(f"fts duplicate row: {key}")
    pattern_keys = {str(r[0]) for r in c.execute("SELECT pattern_id FROM patterns")}
    for key in sorted(pattern_keys - set(seen)):
        issues.append(f"fts missing row: {key}")
    for key in sorted(set(seen) - pattern_keys):
        issues.append(f"fts orphan row: {key}")
    for key in sorted(pattern_keys & set(seen)):
        expected = fts_expected_row(c, key)
        if expected is not None and seen[key][0] != expected:
            issues.append(f"fts projection mismatch: {key}")
    return issues


def rebuild_fts(path: str | os.PathLike[str]) -> dict[str, Any]:
    """在临时表重建 FTS，逐条生成后原子替换；不改变任何业务语义。

    重建前后 facts/state_hash 完全不变（FTS 是派生数据，不入 state hash）；
    发现过投影损坏时先写 fts-degraded 事件，重建完成写 fts-rebuilt 事件。
    """
    def go(c):
        issues_before = fts_audit(c)
        backend = "fts5" if _meta_value(c, "fts5") == "1" else "fallback"
        keys = [str(r[0]) for r in c.execute("SELECT pattern_id FROM patterns ORDER BY pattern_id")]
        rebuilt = 0
        c.execute("DROP TABLE IF EXISTS patterns_fts_rebuild")
        try:
            if backend == "fts5":
                c.execute(
                    "CREATE VIRTUAL TABLE patterns_fts_rebuild USING fts5("
                    "pattern_id UNINDEXED,category,title,root_cause,reflection,probes,aliases)"
                )
            else:
                c.execute(
                    "CREATE TABLE patterns_fts_rebuild(pattern_id TEXT PRIMARY KEY,category TEXT,"
                    "title TEXT,root_cause TEXT,reflection TEXT,probes TEXT,aliases TEXT)"
                )
            for key in keys:
                row = fts_expected_row(c, key)
                if row is None:
                    continue
                c.execute(
                    f"INSERT INTO patterns_fts_rebuild VALUES ({','.join('?' * len(FTS_COLUMNS))})",
                    row,
                )
                rebuilt += 1
            # 逐条比对临时表结果后才允许替换；比对失败则整个事务回滚。
            for key in keys:
                expected = fts_expected_row(c, key)
                got = c.execute(
                    f"SELECT {','.join(FTS_COLUMNS)} FROM patterns_fts_rebuild WHERE pattern_id=?",
                    (key,),
                ).fetchone()
                actual = ["" if value is None else str(value) for value in got] if got else None
                if expected is not None and actual != expected:
                    raise LessonLedgerError(f"FTS 重建逐条比对失败: {key}")
            c.execute("DROP TABLE IF EXISTS patterns_fts")
            c.execute("ALTER TABLE patterns_fts_rebuild RENAME TO patterns_fts")
        finally:
            c.execute("DROP TABLE IF EXISTS patterns_fts_rebuild")
        if issues_before:
            _event(c, "fts-degraded", {"issues": issues_before})
        _event(c, "fts-rebuilt", {
            "backend": backend, "rebuilt_rows": rebuilt,
            "degraded_issues_found": len(issues_before),
        })
        remaining_issues = fts_audit(c)
        if remaining_issues:
            raise LessonLedgerError("FTS 重建后仍不一致: " + "; ".join(remaining_issues))
        return {"status": "rebuilt", "backend": backend, "rebuilt_rows": rebuilt,
                "degraded_issues_found": len(issues_before),
                "issues": issues_before}
    return _tx(path, go)


def event_from_row_values(seq: int, typ: str, at: str, payload_text: str,
                           prev_hash: str, hash_value: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LessonLedgerError(f"归档事件 payload 不是有效 JSON (seq={seq})") from exc
    event = {"seq": int(seq), "type": typ, "at": at, "payload": payload,
             "prev_hash": prev_hash, "hash": hash_value}
    recomputed = hashlib.sha256(_canon({k: v for k, v in event.items() if k != "hash"})).hexdigest()
    if recomputed != hash_value:
        raise LessonLedgerError(f"归档事件 hash 校验失败 (seq={seq})")
    return event


def _confined_segment(outdir: Path, raw: object) -> Path:
    """把 manifest 里的 ``segment`` 名解析为 ``outdir`` 内的普通文件。

    SEC-08：旧实现只做 ``outdir / str(entry.get("segment"))`` 加
    ``is_symlink/is_file`` 检查。``outdir / "/etc/passwd"`` 会得到绝对路径、
    ``../x`` 会越出目录，两者都能通过旧检查，从而读取 outdir 之外的任意文件。
    这里强制 canonical 前缀约束 + 拒绝绝对路径与 ``..``。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise LessonLedgerError(f"归档段名无效: {raw!r}")
    name = raw.strip()
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise LessonLedgerError(f"归档段路径越界: {name}")
    try:
        base = outdir.resolve()
        lexical = base / candidate
        resolved = lexical.resolve()
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LessonLedgerError(f"归档段路径越界: {name}") from exc
    if lexical.is_symlink() or resolved.is_symlink():
        raise LessonLedgerError(f"归档段不能是符号链接: {name}")
    if not resolved.is_file():
        raise LessonLedgerError(f"归档段缺失或不安全: {name}")
    return resolved


def _read_segment_blob(segment: Path, *, max_bytes: int | None = None) -> bytes:
    """按上限读取压缩段，避免把超大文件整体读入内存。

    上限在**调用时**解析模块常量（而非默认参数），这样上限只有一个真源，
    也便于测试注入更小的阈值。
    """
    limit = MAX_SEGMENT_BYTES if max_bytes is None else max_bytes
    try:
        size = segment.stat().st_size
    except OSError as exc:
        raise LessonLedgerError(f"归档段不可读: {segment.name}") from exc
    if size > limit:
        raise LessonLedgerError(
            f"归档段超过 {limit} 字节上限（{size}）: {segment.name}"
        )
    try:
        return segment.read_bytes()
    except OSError as exc:
        raise LessonLedgerError(f"归档段不可读: {segment.name}") from exc


def _inflate_segment(
    blob: bytes,
    *,
    name: str,
    max_bytes: int | None = None,
) -> str:
    """分块解压并累计字节上限（替代一次性 ``gzip.decompress``）。

    SEC-08：``gzip.decompress`` 会把完整膨胀结果放进内存，项目内投放一个
    gzip 炸弹即可把审计宿主打到 OOM。这里逐块读取并在超限时立即中止。

    上限在调用时解析模块常量，理由同 :func:`_read_segment_blob`。
    """
    limit = MAX_SEGMENT_INFLATED_BYTES if max_bytes is None else max_bytes
    chunks: list[bytes] = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as handle:
            while True:
                chunk = handle.read(_SEGMENT_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise LessonLedgerError(
                        f"归档段解压后超过 {limit} 字节上限: {name}"
                    )
                chunks.append(chunk)
    except LessonLedgerError:
        raise
    except (OSError, EOFError, zlib.error) as exc:
        raise LessonLedgerError(f"归档段无法解压: {name}: {exc}") from exc
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LessonLedgerError(f"归档段无法解压: {name}: {exc}") from exc


def load_archived_events(output_dir: str | os.PathLike[str],
                         *, manifest: list[dict[str, Any]] | None = None,
                         verify_manifest: bool = True) -> list[dict[str, Any]]:
    """读取并校验 event 归档段；可独立使用，也可按 DB manifest 校验。

    无 manifest 时按文件名排序做内部链与跨段链接校验；提供 manifest 时
    额外核对 segment_sha256 / record_count / 边界字段。历史链由此完整
    可证明（归档不截断审计证明）。

    ``verify_manifest=True`` 且**提供了 manifest** 时，``segment_sha256`` 与
    ``record_count`` 是必填字段：可选字段意味着「删掉它就能跳过校验」，
    等于把完整性证明降级为自选动作（SEC-08）。
    """
    outdir = Path(output_dir)
    if not outdir.is_dir():
        raise LessonLedgerError(f"归档目录不存在: {outdir}")
    # 字段强制只对「调用方提供了 manifest」生效；无 manifest 时本就没有可核对的
    # 声明值，此时仅做内部链校验（既有语义）。
    enforce_fields = verify_manifest and manifest is not None
    if manifest is None:
        names = sorted(p.name for p in outdir.glob("events-*.jsonl.gz"))
        entries: list[dict[str, Any]] = [{"segment": name} for name in names]
    else:
        entries = manifest
    if not entries:
        return []
    events: list[dict[str, Any]] = []
    prev_last_hash = ""
    for entry in entries:
        if not isinstance(entry, dict):
            raise LessonLedgerError(f"归档 manifest 条目必须是对象: {entry!r}")
        segment = _confined_segment(outdir, entry.get("segment"))
        blob = _read_segment_blob(segment)
        if enforce_fields:
            expected_digest = entry.get("segment_sha256")
            if not isinstance(expected_digest, str) or not expected_digest:
                raise LessonLedgerError(
                    f"归档段缺少 segment_sha256，拒绝未经完整性校验的归档段: {segment.name}"
                )
            if hashlib.sha256(blob).hexdigest() != expected_digest:
                raise LessonLedgerError(f"归档段 sha256 不一致: {segment.name}")
        elif verify_manifest and entry.get("segment_sha256"):
            if hashlib.sha256(blob).hexdigest() != entry["segment_sha256"]:
                raise LessonLedgerError(f"归档段 sha256 不一致: {segment.name}")
        text = _inflate_segment(blob, name=segment.name)
        lines = [line for line in text.splitlines() if line.strip()]
        if enforce_fields:
            if entry.get("record_count") is None:
                raise LessonLedgerError(f"归档段缺少 record_count: {segment.name}")
            if len(lines) != int(entry["record_count"]):
                raise LessonLedgerError(f"归档段记录数不一致: {segment.name}")
        elif verify_manifest and entry.get("record_count") is not None \
                and len(lines) != int(entry["record_count"]):
            raise LessonLedgerError(f"归档段记录数不一致: {segment.name}")
        running_prev = prev_last_hash
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LessonLedgerError(f"归档段行不是有效 JSON: {segment.name}") from exc
            event = event_from_row_values(
                raw.get("seq"), str(raw.get("type")), str(raw.get("at")),
                json.dumps(raw.get("payload"), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
                str(raw.get("prev_hash")), str(raw.get("hash")),
            )
            if event["prev_hash"] != running_prev:
                raise LessonLedgerError(f"归档段 hash 链断裂: {segment.name}")
            running_prev = event["hash"]
            events.append(event)
        first_seq: int | None = None
        last_seq: int | None = None
        if lines:
            first_seq = events[len(events) - len(lines)]["seq"]
            last_seq = events[-1]["seq"]
        if verify_manifest and entry.get("first_seq") is not None \
                and (first_seq != int(entry["first_seq"]) or last_seq != int(entry["last_seq"])):
            raise LessonLedgerError(f"归档段 seq 边界不一致: {segment.name}")
        prev_last_hash = running_prev
    return events


def validate_deep(path: str | os.PathLike[str]) -> dict[str, Any]:
    """DOC-008 §9 深度校验：事实表、事件链、state hash、receipt、tombstone、
    FTS 投影与归档段全部独立验证；任何一项失败都整体失败。"""
    db = database_path(path)
    issues: list[str] = []
    with _conn(db, readonly=True) as c:
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            issues.append(f"integrity_check 失败: {db}")
        schema = _meta_value(c, "schema_version")
        if schema != str(_ledger.SCHEMA_VERSION):
            issues.append(f"schema_version 无效: {schema!r}")
        try:
            _validate(c, db)
        except LessonLedgerError as exc:
            issues.append(str(exc))
        # evidence receipt 内容哈希
        for r in c.execute("SELECT * FROM evidence_receipts ORDER BY receipt_id"):
            item = {
                "receipt_id": r[0], "subject_type": r[1], "subject_id": r[2],
                "run_id": r[3], "lifecycle_id": r[4], "round": int(r[5]),
                "artifact_hash": r[6], "created_at": r[7],
            }
            if hashlib.sha256(_canon(item)).hexdigest() != r[8]:
                issues.append(f"evidence_receipt 哈希不一致: {r[0]}")
        # consumption event 内容哈希
        for r in c.execute("SELECT * FROM consumption_events ORDER BY event_id"):
            item = {
                "event_id": r[0], "subject_type": r[1], "subject_id": r[2],
                "event_type": r[3], "lifecycle_id": r[4], "run_id": r[5],
                "created_at": r[6],
            }
            if hashlib.sha256(_canon(item)).hexdigest() != r[7]:
                issues.append(f"consumption_event 哈希不一致: {r[0]}")
        # incident 内容哈希（tombstone/status 变更会更新 updated_at，
        # 但 content 只覆盖业务字段，因此仍可独立复算）
        for r in c.execute("SELECT * FROM incidents ORDER BY incident_id"):
            try:
                routing_tags = json.loads(r[17]) if r[17] else {}
            except (TypeError, ValueError):
                issues.append(f"incident routing_tags JSON 无效: {r[0]}")
                routing_tags = {}
            content = {
                "signature": r[2], "summary": r[3], "context": r[4],
                "before_snippet": r[5], "after_snippet": r[6], "diff_ref": r[7],
                "commit_ref": r[8], "decision": r[9], "duration_seconds": r[10],
                "lifecycle_id": r[11], "round": r[12], "pattern_id": r[1],
                "routing_tags": routing_tags,
            }
            # v13 had no fingerprint column; preserve its legacy hash projection.
            fingerprint = r[18] if len(r) > 18 else None
            if fingerprint is not None:
                content["finding_fingerprint"] = fingerprint
            try:
                content_hash = hashlib.sha256(_canon(_incident_content(content))).hexdigest()
            except LessonLedgerError as exc:
                issues.append(f"incident 内容无效: {r[0]} ({exc})")
                continue
            if content_hash != r[16]:
                issues.append(f"incident 内容哈希不一致: {r[0]}")
        # tool experience 内容哈希
        for r in c.execute("SELECT * FROM tool_experience ORDER BY tool_name,category"):
            content = {
                "tool_name": r[0], "category": r[1], "experience_level": r[2],
                "record_version": int(r[3]), "use_count": int(r[4]),
                "hit_count": int(r[5]), "miss_count": int(r[6]),
                "false_positive_count": int(r[7]), "rejected_count": int(r[8]),
                "independent_lifecycle_count": int(r[9]),
                "first_observed_at": r[10], "last_observed_at": r[11],
                "tool_version": r[12], "device_id": r[13],
                "deleted": bool(r[14]), "deleted_at": r[15],
            }
            if hashlib.sha256(_canon(
                    {k: content[k] for k in sorted(content)})).hexdigest() != r[17]:
                issues.append(f"tool_experience 哈希不一致: {r[0]}/{r[1]}")
        issues.extend(fts_audit(c))
        # 归档段与 active 链边界
        manifest = _archive_manifest(c)
        archived_total = sum(int(entry.get("record_count", 0)) for entry in manifest)
        active_first = c.execute(
            "SELECT seq,prev_hash FROM events ORDER BY seq LIMIT 1"
        ).fetchone()
        archived_seq = _meta_value(c, "archive_last_seq")
        if manifest:
            root = hashlib.sha256(_canon(manifest)).hexdigest()
            if _meta_value(c, "archive_root_hash") != root:
                issues.append("archive_root_hash 与 manifest 不一致")
            archive_dir = _meta_value(c, "archive_dir")
            if not archive_dir or not Path(archive_dir).is_dir():
                issues.append("archive_dir 缺失，无法验证归档段")
            else:
                try:
                    archived = load_archived_events(archive_dir, manifest=manifest)
                    if len(archived) != archived_total:
                        issues.append("归档段事件总数与 manifest 不一致")
                    if archived and active_first:
                        if archived[-1]["hash"] != active_first[1] \
                                or int(archived_seq) + 1 != int(active_first[0]):
                            issues.append("归档链末端与 active 链起点不连续")
                except LessonLedgerError as exc:
                    issues.append(str(exc))
        elif active_first and int(active_first[0]) != 1:
            issues.append("无归档时首个 active 事件必须是 seq=1")
        counts = {
            "patterns": c.execute("SELECT COUNT(*) FROM patterns").fetchone()[0],
            "incidents": c.execute("SELECT COUNT(*) FROM incidents").fetchone()[0],
            "receipts": c.execute("SELECT COUNT(*) FROM evidence_receipts").fetchone()[0],
            "consumption_events": c.execute("SELECT COUNT(*) FROM consumption_events").fetchone()[0],
            "tool_experience": c.execute("SELECT COUNT(*) FROM tool_experience").fetchone()[0],
        }
    if issues:
        raise LessonLedgerError("深度校验失败:\n- " + "\n- ".join(sorted(issues)))
    return {"status": "valid", "database": str(db), "counts": counts,
            "events_active": count_active_events(db), "events_archived": archived_total,
            "fts_backend": "fts5"}


def count_active_events(path: str | os.PathLike[str]) -> int:
    db = database_path(path)
    with _conn(db, readonly=True) as c:
        return int(c.execute("SELECT COUNT(*) FROM events").fetchone()[0])


def archive_events(path: str | os.PathLike[str], output_dir: str | os.PathLike[str],
                   *, keep_tail: int = 0) -> dict[str, Any]:
    """把闭合区间 events 写入压缩 JSONL segment 并从 active 库删除。

    崩溃安全顺序（DOC-008 §10.7）：
      1) 先在一致性读边界取事件快照并生成确定性 segment 字节；
      2) segment 以临时文件写入、fsync 后原子发布到最终路径；
      3) 再开写事务：校验 head 未变 → 删除已归档区间 → 写入 manifest、
         archive root hash、最后归档 seq/hash（新的连续起点证明）→ 提交；
      4) 发布成功但事务失败的进程内异常会删除未登记的 segment 文件；
      5) 进程中断留下的孤儿文件内容是确定性的，重跑会用相同字节覆盖收敛。
    """
    if not isinstance(keep_tail, int) or isinstance(keep_tail, bool) or keep_tail < 0:
        raise LessonLedgerError("keep_tail 必须是非负整数")
    db = database_path(path)
    outdir = Path(output_dir)
    if outdir.is_symlink():
        raise LessonLedgerError(f"拒绝使用符号链接归档目录: {outdir}")
    outdir.mkdir(parents=True, exist_ok=True)
    with _conn(db, readonly=True) as src:
        _validate(src, db)
        manifest = _archive_manifest(src)
        head_snapshot = _meta_value(src, "head_hash")
        rows = [
            {"seq": int(r[0]), "type": str(r[1]), "at": str(r[2]),
             "payload": json.loads(r[3]), "prev_hash": str(r[4]), "hash": str(r[5])}
            for r in src.execute("SELECT * FROM events ORDER BY seq")
        ]
    cutoff = len(rows) - keep_tail
    if cutoff <= 0:
        return {"status": "no-op", "archived_events": 0,
                "active_events": len(rows),
                "events_archived_total": sum(int(e.get("record_count", 0)) for e in manifest)}
    segment_rows = rows[:cutoff]
    first, last = segment_rows[0], segment_rows[-1]
    lines = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in segment_rows
    )
    # mtime=0 保证同区间字节级确定：中断后的重跑可以无损覆盖孤儿文件。
    blob = gzip.compress(lines.encode("utf-8"), mtime=0)
    name = f"events-{first['seq']:012d}-{last['seq']:012d}.jsonl.gz"
    final = outdir / name
    entry = {
        "segment": name,
        "first_seq": first["seq"], "last_seq": last["seq"],
        "previous_hash": first["prev_hash"], "last_hash": last["hash"],
        "segment_sha256": hashlib.sha256(blob).hexdigest(),
        "record_count": len(segment_rows),
    }
    new_manifest = manifest + [entry]
    root_hash = hashlib.sha256(_canon(new_manifest)).hexdigest()
    fd, tmp_name = tempfile.mkstemp(prefix=".segment-", suffix=".tmp", dir=outdir)
    tmp = Path(tmp_name)
    published = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final)
        published = True

        def go(c):
            if _meta_value(c, "head_hash") != head_snapshot:
                raise LessonLedgerError("归档期间账本发生变化，拒绝提交本次归档")
            c.execute("DELETE FROM events WHERE seq<=?", (last["seq"],))
            c.executemany(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                [
                    ("archive_manifest", json.dumps(new_manifest, ensure_ascii=False,
                                                    sort_keys=True)),
                    ("archive_root_hash", root_hash),
                    ("archive_last_seq", str(last["seq"])),
                    ("archive_last_hash", last["hash"]),
                    ("archive_dir", str(outdir)),
                ],
            )

        _tx(path, go)
        return {
            "status": "archived", "segment": str(final),
            "archived_events": len(segment_rows),
            "first_seq": first["seq"], "last_seq": last["seq"],
            "archive_root_hash": root_hash,
            "active_events": count_active_events(path),
            "events_archived_total": sum(int(e.get("record_count", 0)) for e in new_manifest),
        }
    except BaseException:
        try:
            if published:
                final.unlink()
            elif tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        raise
