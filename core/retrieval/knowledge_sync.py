#!/usr/bin/env python3
"""Governed project-to-global BugX knowledge synchronization.

The runtime Agent only reads the global ledger.  This command is the explicit
publisher boundary for exporting project candidates, approving a sanitized
bundle, importing it into the user's global ledger, and rolling that ledger
back to a validated pre-import backup.
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

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from typing import Any, Iterator, Mapping

from core.ledger import lesson_ledger as ledger
from core.secure_atomic import AtomicWriteError, atomic_write_json
from core.governance.secret_redactor import contains_unredacted_secrets, find_unredacted_secrets

SCHEMA_VERSION = 1
BUNDLE_KIND = "bugx-knowledge-bundle"
BUNDLE_ID_RE = re.compile(r"^B-[0-9a-f]{16,32}$")
ABSOLUTE_PATH_RE = re.compile(r"(?:^|[\s(])/(?:Users|home|private|var|tmp)/")
# DOC-008 DEC-008-103：私有同步默认完整详情，用户可按 scope 降级摘要；
# 凭据与密钥在发网前阻断。DOC-211 契约修订：出网面闸支持三档用户策略
# （block/redact/allow，默认 block 语义不变）；本地沉淀闸（knowledge_auto）
# 恒 block 不受策略影响。
DETAIL_MODES = ("full", "summary")
_TOMBSTONE_PATTERN_STATUSES = {"retired", "superseded", "revoked"}
_MACHINE_PREFIX_RE = re.compile(r"/(?:Users|home)/[^/\s\"'`,;)\]]+")

# 摘要模式的声明字段（TO-008-013）：只保留摘要、分类、指纹、计数与必要状态；
# 结构化错误上下文、before/after、决策链、探针等详情一律不随摘要出网。
SUMMARY_PATTERN_FIELDS = (
    "pattern_id", "category", "title", "root_cause", "status",
    "use_count", "hit_count", "miss_count",
    "false_positive_count", "rejected_count",
    "created_at", "updated_at", "finding_fingerprints", "routing_tags",
)
SUMMARY_INCIDENT_FIELDS = (
    "incident_id", "pattern_id", "signature", "summary", "status",
    "duration_seconds", "created_at", "updated_at", "content_hash",
)
SUMMARY_TOOL_EXPERIENCE_FIELDS = tuple(ledger.TOOL_EXPERIENCE_FIELDS)


class KnowledgeSyncError(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise KnowledgeSyncError(f"{label}不存在或不安全: {path}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        atomic_write_json(path, payload, mode=0o600, indent=2)
    except (AtomicWriteError, OSError) as exc:
        raise KnowledgeSyncError(f"安全写入失败: {path}") from exc


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _regular(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeSyncError(f"{label}无法解析: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise KnowledgeSyncError(f"{label}必须是对象: {path}")
    return value


def _safe_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeSyncError(f"{field}必须是非空字符串")
    text = value.strip()
    if contains_unredacted_secrets(text):
        raise KnowledgeSyncError(f"{field}包含疑似敏感信息，拒绝生成全局候选包")
    if ABSOLUTE_PATH_RE.search(text):
        raise KnowledgeSyncError(f"{field}包含绝对路径，拒绝生成全局候选包")
    return text


def _bundle_content(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": bundle.get("schema_version"),
        "kind": bundle.get("kind"),
        "bundle_id": bundle.get("bundle_id"),
        "source": bundle.get("source"),
        "created_at": bundle.get("created_at"),
        "contributor": bundle.get("contributor"),
        "approval": bundle.get("approval"),
        "patterns": bundle.get("patterns"),
    }


def _validate_bundle(bundle: dict[str, Any], *, require_approved: bool = False) -> dict[str, Any]:
    if bundle.get("schema_version") != SCHEMA_VERSION or bundle.get("kind") != BUNDLE_KIND:
        raise KnowledgeSyncError("bundle schema 或 kind 不受支持")
    bundle_id = bundle.get("bundle_id")
    if not isinstance(bundle_id, str) or not BUNDLE_ID_RE.fullmatch(bundle_id):
        raise KnowledgeSyncError("bundle_id 格式无效")
    patterns = bundle.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        raise KnowledgeSyncError("bundle patterns 不能为空")
    expected_hash = bundle.get("content_hash")
    if expected_hash != _hash(_bundle_content(bundle)):
        raise KnowledgeSyncError("bundle content_hash 校验失败")
    approval = bundle.get("approval")
    if not isinstance(approval, dict):
        raise KnowledgeSyncError("bundle 缺少 approval")
    if require_approved and approval.get("status") != "approved":
        raise KnowledgeSyncError("只有 approved bundle 才能导入全局库")
    if approval.get("status") not in {"pending", "approved"}:
        raise KnowledgeSyncError("bundle approval 状态无效")
    normalized = []
    for raw in patterns:
        if not isinstance(raw, dict):
            raise KnowledgeSyncError("bundle pattern 必须是对象")
        item = dict(raw)
        item["status"] = "candidate"
        # Reuse the ledger's complete schema and credential checks.
        normalized.append(ledger._pattern(item))  # noqa: SLF001 - shared validator boundary.
        serialized = json.dumps(item, ensure_ascii=False)
        if contains_unredacted_secrets(serialized) or ABSOLUTE_PATH_RE.search(serialized):
            raise KnowledgeSyncError("bundle pattern 包含敏感信息或绝对路径")
    return {"bundle_id": bundle_id, "patterns": normalized, "approval": approval}


def _portable_pattern(pattern: dict[str, Any]) -> dict[str, Any]:
    source_refs = []
    for value in pattern.get("source_refs", []):
        text = _safe_text(value, "source_refs")
        path_part = text.split(":", 1)[0]
        if Path(path_part).is_absolute() or ".." in Path(path_part).parts:
            raise KnowledgeSyncError("source_refs 不得包含绝对路径或路径逃逸")
        source_refs.append(text)
    if not source_refs:
        source_refs = ["local-reviewed-pattern"]
    result = {
        "pattern_id": _safe_text(pattern["pattern_id"], "pattern_id"),
        "category": _safe_text(pattern["category"], "category"),
        "title": _safe_text(pattern["title"], "title"),
        "root_cause": _safe_text(pattern["root_cause"], "root_cause"),
        "reflection": _safe_text(pattern["reflection"], "reflection"),
        "probes": [_safe_text(x, "probes") for x in pattern.get("probes", [])],
        "negative_controls": [_safe_text(x, "negative_controls") for x in pattern.get("negative_controls", [])],
        "evidence_requirements": [_safe_text(x, "evidence_requirements") for x in pattern.get("evidence_requirements", [])],
        "applicability": [_safe_text(x, "applicability") for x in pattern.get("applicability", [])],
        "false_positive_patterns": [
            _safe_text(x, "false_positive_patterns")
            for x in pattern.get("false_positive_patterns", [])
        ],
        "oracle_hints": [_safe_text(x, "oracle_hints") for x in pattern.get("oracle_hints", [])],
        # 方法论排查层随包导出：playbook/已试方案/成功方案是经验的「怎么想」部分，
        # 推广到其他项目时不能丢。
        "playbook": [_safe_text(x, "playbook") for x in pattern.get("playbook", [])],
        "attempts_tried": [_safe_text(x, "attempts_tried") for x in pattern.get("attempts_tried", [])],
        "successful_solution": [
            _safe_text(x, "successful_solution")
            for x in pattern.get("successful_solution", [])
        ],
        # 源码证据是经验的一部分：保留与 Finding/Incident 直接相关的修前/修后
        # 片段，供后续项目复现和定位；_safe_text 仍阻断凭据与不可移植绝对路径。
        "before_code": (
            _safe_text(pattern["before_code"], "before_code")
            if pattern.get("before_code") else ""
        ),
        "after_code": (
            _safe_text(pattern["after_code"], "after_code")
            if pattern.get("after_code") else ""
        ),
        "fix_summary": (
            _safe_text(pattern["fix_summary"], "fix_summary")
            if pattern.get("fix_summary") else ""
        ),
        "repro_oracle": (
            _safe_text(pattern["repro_oracle"], "repro_oracle")
            if pattern.get("repro_oracle") else ""
        ),
        "tags": [_safe_text(x, "tags") for x in pattern.get("tags", [])],
        "aliases": [_safe_text(x, "aliases") for x in pattern.get("aliases", [])],
        "routing_tags": {
            str(key): [_safe_text(value, "routing_tags") for value in values]
            for key, values in (pattern.get("routing_tags") or {}).items()
        },
        "source_refs": source_refs,
        "status": "candidate",
    }
    # Validate after stripping local counters, timestamps and finding fingerprints.
    ledger._pattern(result)  # noqa: SLF001
    return result


def export_candidates(project_ledger: Path, output: Path, contributor: str) -> dict[str, Any]:
    _regular(project_ledger.with_suffix(".sqlite3"), "项目 SQLite 账本")
    contributor = _safe_text(contributor, "contributor")
    state = ledger.read(project_ledger)
    patterns = [
        _portable_pattern(value)
        for value in state["lessons"].values()
        if value.get("status") == "candidate"
    ]
    if not patterns:
        raise KnowledgeSyncError("项目账本没有可发布的 candidate 模式")
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "bundle_id": "B-" + uuid.uuid4().hex[:16],
        "source": {
            "scope": "project",
            "ledger_revision": state["revision"],
            "state_hash": state["state_hash"],
        },
        "created_at": _now(),
        "contributor": contributor,
        "patterns": patterns,
        "approval": {"status": "pending"},
    }
    bundle["content_hash"] = _hash(_bundle_content(bundle))
    _atomic_json(output, bundle)
    return {"status": "exported", "bundle_id": bundle["bundle_id"],
            "patterns": len(patterns), "content_hash": bundle["content_hash"],
            "output": str(output)}


def approve_bundle(bundle_path: Path, output: Path, reviewer: str, reason: str) -> dict[str, Any]:
    bundle = _read_json(bundle_path, "候选 bundle")
    checked = _validate_bundle(bundle)
    reviewer = _safe_text(reviewer, "reviewer")
    reason = _safe_text(reason, "reason")
    contributor = _safe_text(bundle.get("contributor"), "contributor")
    if reviewer == contributor:
        raise KnowledgeSyncError("reviewer 必须不同于 contributor；禁止同一身份自审自批")
    if checked["approval"].get("status") != "pending":
        raise KnowledgeSyncError("只能批准 pending bundle")
    bundle["approval"] = {
        "status": "approved",
        "reviewer": reviewer,
        "reason": reason,
        "approved_at": _now(),
    }
    bundle["content_hash"] = _hash(_bundle_content(bundle))
    _atomic_json(output, bundle)
    return {"status": "approved", "bundle_id": checked["bundle_id"],
            "patterns": len(checked["patterns"]), "output": str(output)}


def _history_root(global_root: Path) -> Path:
    path = global_root / "versions"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(global_root, 0o700)
    os.chmod(path, 0o700)
    return path


def _sqlite_sidecars(database: Path) -> tuple[Path, Path]:
    """Return SQLite's journal sidecars for a database file."""
    return Path(f"{database}-wal"), Path(f"{database}-shm")


def _archive_sidecars(database: Path, archive_prefix: Path) -> list[tuple[Path, Path]]:
    """Move live WAL/SHM files aside before replacing a database.

    Replacing only ``database`` is unsafe: a WAL created by the old database
    can be replayed against the newly installed main file.  Moving the
    sidecars makes the replacement recoverable and keeps them out of the
    active database directory.  The caller owns the archived files until the
    operation has been published successfully.
    """
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            if not (sidecar.exists() or sidecar.is_symlink()):
                continue
            if sidecar.is_symlink():
                raise KnowledgeSyncError(f"拒绝处理符号链接 SQLite 旁车文件: {sidecar}")
            archived = Path(f"{archive_prefix}{suffix}")
            if archived.exists() or archived.is_symlink():
                raise KnowledgeSyncError(f"SQLite 旁车备份已存在，拒绝覆盖: {archived}")
            os.replace(sidecar, archived)
            moved.append((sidecar, archived))
    except BaseException:
        _restore_archived_sidecars(moved)
        raise
    return moved


def _restore_archived_sidecars(moved: list[tuple[Path, Path]]) -> None:
    for original, archived in moved:
        if not archived.exists():
            continue
        if original.exists() or original.is_symlink():
            raise KnowledgeSyncError(f"无法恢复 SQLite 旁车文件，目标已存在: {original}")
        os.replace(archived, original)


def _discard_archived_sidecars(moved: list[tuple[Path, Path]]) -> None:
    """删除归档的 -wal/-shm 旁车文件，绝不回接到换入的主文件上。

    WAL/SHM 与产生它的主库通过 header 里的 salt/页计数绑定；backup API
    产出的主文件是自带全部已提交内容的干净拷贝。把旧库的归档 sidecar
    放回新换入的主文件旁属于跨库回放：轻则被 salt 校验忽略，重则把旧库
    的脏页污染到恢复后的数据库。因此恢复路径只能删除归档件。
    """
    for _original, archived in moved:
        try:
            archived.unlink()
        except FileNotFoundError:
            pass


def _replace_database(database: Path, replacement: Path, archive_prefix: Path) -> list[tuple[Path, Path]]:
    """Atomically replace a SQLite main file while isolating old sidecars."""
    if database.is_symlink() or replacement.is_symlink():
        raise KnowledgeSyncError("拒绝用符号链接替换 SQLite 运行库")
    moved = _archive_sidecars(database, archive_prefix)
    try:
        os.replace(replacement, database)
    except BaseException:
        _restore_archived_sidecars(moved)
        raise
    return moved


def _restore_database(database: Path, backup: Path, moved: list[tuple[Path, Path]], history: Path) -> None:
    """Restore a clean backup; archived sidecars are deleted, never replayed."""
    temporary = history / f".{backup.name}.restore.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise KnowledgeSyncError(f"恢复临时文件已存在: {temporary}")
    shutil.copy2(backup, temporary)
    os.chmod(temporary, 0o600)
    try:
        # The replacement database must not acquire sidecars while it is
        # being restored; it is a clean backup produced by backup_database.
        for sidecar in _sqlite_sidecars(database):
            if sidecar.exists() or sidecar.is_symlink():
                raise KnowledgeSyncError(f"恢复时发现未预期的 SQLite 旁车文件: {sidecar}")
        os.replace(temporary, database)
        # WAL 与主库无盐绑定，不可跨库回放：归档的 -wal/-shm 属于被替换的
        # 旧库，换入的备份主文件自带全部已提交内容，归档件直接删除。
        _discard_archived_sidecars(moved)
    finally:
        if temporary.exists():
            temporary.unlink()


def import_approved(global_ledger: Path, global_root: Path, bundle_path: Path,
                    reviewer: str) -> dict[str, Any]:
    bundle = _read_json(bundle_path, "approved bundle")
    checked = _validate_bundle(bundle, require_approved=True)
    approval_reviewer = checked["approval"].get("reviewer")
    if not isinstance(approval_reviewer, str) or not approval_reviewer.strip():
        raise KnowledgeSyncError("approved bundle 缺少审核人")
    reviewer = _safe_text(reviewer, "reviewer")
    _regular(global_ledger.with_suffix(".sqlite3"), "全局 SQLite 账本")
    before = ledger.read(global_ledger)
    history = _history_root(global_root)
    bundle_id = checked["bundle_id"]
    for event in before.get("events", []):
        if event.get("type") != "knowledge-imported":
            continue
        payload = event.get("payload") or {}
        if payload.get("bundle_id") == bundle_id:
            return {
                "status": "already_imported",
                "bundle_id": bundle_id,
                "created": [],
                "activated": [],
                "duplicates": [item["pattern_id"] for item in checked["patterns"]],
                "before_state_hash": before["state_hash"],
                "after_state_hash": before["state_hash"],
            }
    before_backup = history / f"{bundle_id}.before.sqlite3"
    if before_backup.exists() or before_backup.is_symlink():
        raise KnowledgeSyncError(f"该 bundle 已有导入版本记录，拒绝覆盖: {before_backup}")
    ledger.backup_database(global_ledger, before_backup)
    global_db = global_ledger.with_suffix(".sqlite3")
    result: dict[str, Any] | None = None
    try:
        result = ledger.import_patterns(
            global_ledger, checked["patterns"], bundle_id=bundle_id,
            reviewer=reviewer,
        )
        after = ledger.read(global_ledger)
        ledger.export_snapshot(global_ledger, global_root / "global.json")
        _atomic_json(history / f"{bundle_id}.json", {
            "schema_version": SCHEMA_VERSION,
            "record_type": "import",
            "bundle_id": bundle_id,
            "reviewer": reviewer,
            "bundle_reviewer": approval_reviewer,
            "before_state_hash": before["state_hash"],
            "after_state_hash": after["state_hash"],
            "backup": before_backup.name,
            "imported_at": _now(),
        })
        return {**result, "before_state_hash": before["state_hash"],
                "after_state_hash": after["state_hash"], "backup": str(before_backup)}
    except BaseException:
        # The SQLite transaction is atomic, but the database commit and the
        # version-record publication are separate files.  If publication
        # fails, restore the pre-import snapshot instead of leaving a global
        # database with no auditable version record.
        if result is not None and result.get("status") == "imported":
            moved: list[tuple[Path, Path]] = []
            try:
                moved = _archive_sidecars(global_db, before_backup)
                _restore_database(global_db, before_backup, moved, history)
                restored = ledger.read(global_ledger)
                if restored["state_hash"] != before["state_hash"]:
                    raise KnowledgeSyncError("导入失败后的全局库恢复校验失败")
                ledger.export_snapshot(global_ledger, global_root / "global.json")
            except BaseException as restore_error:
                raise KnowledgeSyncError(
                    f"全局知识导入发布失败，且自动恢复失败；请保留备份 {before_backup}: {restore_error}"
                ) from restore_error
        for path in (before_backup, *_sqlite_sidecars(before_backup)):
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _record_backup_path(history: Path, bundle_id: str, record: dict[str, Any]) -> Path:
    """版本记录里的 backup 字段是不可信输入，必须钉死在 history 目录内。

    只接受 ``{bundle_id}.before.sqlite3``，或严格文件名形态（仅
    ``[A-Za-z0-9._-]``，且解析后的父目录必须就是 history）。杜绝 ``../``
    穿越或绝对路径把回滚的读/写引导到 versions 目录之外。
    """
    value = record.get("backup")
    if not isinstance(value, str) or not value:
        raise KnowledgeSyncError("版本记录缺少 backup 字段，拒绝回滚")
    if value == f"{bundle_id}.before.sqlite3":
        return history / value
    if value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise KnowledgeSyncError(f"版本记录 backup 字段不合法，拒绝回滚: {value!r}")
    path = (history / value).resolve()
    if path.parent != history.resolve():
        raise KnowledgeSyncError(f"版本记录 backup 字段越界，拒绝回滚: {value!r}")
    return path


def rollback(global_ledger: Path, global_root: Path, bundle_id: str, reviewer: str) -> dict[str, Any]:
    if not BUNDLE_ID_RE.fullmatch(bundle_id):
        raise KnowledgeSyncError("bundle_id 格式无效")
    reviewer = _safe_text(reviewer, "reviewer")
    history = _history_root(global_root)
    record = _read_json(history / f"{bundle_id}.json", "版本记录")
    backup = _record_backup_path(history, bundle_id, record)
    _regular(backup, "回滚备份")
    backup_state = ledger.read(backup)
    current = ledger.read(global_ledger)
    if current["state_hash"] != record.get("after_state_hash"):
        raise KnowledgeSyncError("全局库已发生后续变更，拒绝盲目回滚；请先确认当前版本")
    current_backup = history / f"{bundle_id}.rollback-from.sqlite3"
    if current_backup.exists() or current_backup.is_symlink():
        raise KnowledgeSyncError(f"回滚备份已存在，拒绝覆盖: {current_backup}")
    ledger.backup_database(global_ledger, current_backup)
    temporary = history / f".{bundle_id}.rollback.tmp"
    moved: list[tuple[Path, Path]] = []
    try:
        shutil.copy2(backup, temporary)
        os.chmod(temporary, 0o600)
        moved = _replace_database(global_ledger.with_suffix(".sqlite3"), temporary, current_backup)
        restored = ledger.read(global_ledger)
        if restored["state_hash"] != backup_state["state_hash"]:
            raise KnowledgeSyncError("回滚后 state_hash 不一致")
        ledger.export_snapshot(global_ledger, global_root / "global.json")
        _atomic_json(history / f"{bundle_id}.rollback.json", {
            "schema_version": SCHEMA_VERSION,
            "record_type": "rollback",
            "bundle_id": bundle_id,
            "reviewer": reviewer,
            "from_state_hash": current["state_hash"],
            "to_state_hash": restored["state_hash"],
            "rollback_backup": current_backup.name,
            "rolled_back_at": _now(),
        })
        return {"status": "rolled_back", "bundle_id": bundle_id,
                "state_hash": restored["state_hash"], "backup": str(current_backup)}
    except BaseException:
        # If validation or snapshot publication fails after the swap, restore
        # the exact pre-rollback database and its sidecars.  Keep the backup
        # files so an operator can recover even if this second restoration
        # fails.
        try:
            if moved:
                _restore_database(global_ledger.with_suffix(".sqlite3"), current_backup, moved, history)
        except BaseException as restore_error:
            raise KnowledgeSyncError(
                f"回滚失败且自动恢复失败；请保留备份 {current_backup}: {restore_error}"
            ) from restore_error
        raise
    finally:
        if temporary.exists():
            temporary.unlink()


def resolve_detail_mode(value: str | None) -> str:
    """未显式选择时使用 full；显式给出但非法时 fail-closed 拒绝。

    默认值属**产品决策**，不得由实现方擅自调整（build 299 曾被我误改为
    summary 并已回滚）。需要更保守上传时由用户显式设置
    ``bugx config set cloud.sync_detail_mode summary``。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "full"
    mode = value.strip().lower()
    if mode not in DETAIL_MODES:
        raise KnowledgeSyncError(
            f"detail_mode 只支持 {'/'.join(DETAIL_MODES)}，收到: {value!r}")
    return mode


def _iter_string_values(value: Any) -> Iterator[str]:
    """Yield every string leaf or key-value pair in nested records (no JSON escaping)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(v, str):
                yield f"{k}: {v}"
            else:
                yield from _iter_string_values(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_string_values(v)


def enforce_credential_block(records: Any, *, label: str = "私有同步记录",
                             policy: str = "block",
                             hint_exits: bool = False) -> None:
    """凭据/密钥阻断（full 与 summary 都必须过这道闸，DEC-008-103）。

    ``secret_redactor`` owns the shape-aware scan.  This boundary deliberately
    does not redact: an unredacted credential is a hard failure, while typed
    ``{{REDACTED_*}}`` values and documented fixture/hash/route values pass.

    DOC-211 契约修订（出网面三档策略）：
    - ``block``（默认）：命中即硬失败，语义与 build 54 逐字节一致；
    - ``redact``：调用方应已先经 ``cloud_client.prepare_records_for_sync``
      完成 span 脱敏，此处仅做形状复扫（redact 不适用形态仍拒）；
    - ``allow``：跳过扫描（审计由 prepare 落盘）。
    本地沉淀闸（knowledge_auto._guard）恒走默认 block，不吃策略。
    ``hint_exits`` 仅出网侧调用点（sync_push/:251 与 build_private_sync_records）
    置 True——失败信息追加 DOC-211 配置出口；post 复盘通道不追加。
    """
    if policy == "allow":
        return
    # DOC-204 缺陷修补（build.51）：v1 对 json.dumps(records) 之后的 blob
    # 做扫描——JSON 序列化会把字符串值内的合法双引号转义成 \"，转义反斜杠
    # 被 SECRET_REDACTOR 规则命中 → 任何含引号的 confirmed finding 都阻断
    # 复盘（R9 post 第 12 次实证）。改为对 records 的原始字符串值逐字段
    # 扫描：不经 JSON 转义，凭据形态的检出语义不变，序列化噪声归零。
    matches: list[str] = []
    for text in _iter_string_values(records):
        matches.extend(find_unredacted_secrets(text))
    if matches:
        suffix = ""
        if hint_exits:
            if policy == "redact":
                suffix = ("；该形态 redact 不适用，可移除该内容，或 "
                          "bugx config set cloud.sensitive_policy allow（原样上传，留审计）")
            else:
                suffix = ("；可选项：bugx config set cloud.sensitive_policy redact"
                          "（按命中脱敏后上传）或 allow（原样上传，留审计）")
        raise KnowledgeSyncError(
            f"{label}包含疑似凭据或密钥（命中 {matches[0][:12]!r}…），已整体阻断发网{suffix}")


def strip_to_summary(record: dict[str, Any], kind: str) -> dict[str, Any]:
    """按声明字段投影为摘要记录；summary 模式下结构化详情一律不出网。"""
    fields = {
        "pattern": SUMMARY_PATTERN_FIELDS,
        "incident": SUMMARY_INCIDENT_FIELDS,
        "tool_experience": SUMMARY_TOOL_EXPERIENCE_FIELDS,
    }.get(kind)
    if fields is None:
        raise KnowledgeSyncError(f"未知记录类型，拒绝降级: {kind}")
    return {key: record[key] for key in fields if key in record}


def _scrub_machine_prefixes(value: Any) -> tuple[Any, int]:
    """把绝对路径中的机器前缀（/Users/<name>/、/home/<name>/）改写为 ~。

    §8.3：项目相对路径优先保留；无法安全转换的绝对路径只脱敏机器段，
    不因“可能详细”整体拒绝。
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        total = 0
        for key, item in value.items():
            new_item, count = _scrub_machine_prefixes(item)
            cleaned[key] = new_item
            total += count
        return cleaned, total
    if isinstance(value, list):
        cleaned_list = []
        total = 0
        for item in value:
            new_item, count = _scrub_machine_prefixes(item)
            cleaned_list.append(new_item)
            total += count
        return cleaned_list, total
    if isinstance(value, str):
        scrubbed = _MACHINE_PREFIX_RE.sub("~", value)
        return scrubbed, (1 if scrubbed != value else 0)
    return value, 0


def is_baseline_seed_pattern(pattern: dict[str, Any]) -> bool:
    """系统内置的防御基准模式（14条经典模式）。
    这些基线模式统一托管在全局层，项目层仅按需继承，在项目私有同步出网前一律拦截，避免浪费云端存储。
    """
    pid = str(pattern.get("pattern_id") or pattern.get("lesson_id") or "")
    if pid.startswith("L-00000000000"):
        return True
    refs = pattern.get("source_refs")
    if isinstance(refs, list) and any("mistake-book.md" in str(r) for r in refs):
        return True
    if isinstance(refs, str) and "mistake-book.md" in refs:
        return True
    return False


def is_baseline_seed_tombstone(item: dict[str, Any]) -> bool:
    rec_type = str(item.get("record_type") or "")
    if rec_type not in ("pattern", "patterns"):
        return False
    rec_id = str(item.get("record_id") or "")
    return rec_id.startswith("L-00000000000")


# PE-20261004-005：服务端 validateLessonRecord 的字段字节上限（Go len=字节数）。
# 客户端导出必须按此归一化，否则超长记录令服务端整包拒收（SYNC_INVALID_ENVELOPE）。
_WIRE_FIELD_BYTE_LIMITS = {
    "lesson_id": 50, "title": 500, "root_cause": 65536, "reflection": 65536,
    "probes": 65536, "source_refs": 65536, "error_signature": 500,
    "before_code": 65536, "after_code": 65536, "fix_summary": 65536,
    "repro_oracle": 65536, "tags": 500, "finding_fingerprints": 65536,
}


def _utf8_truncate(value: str, limit: int) -> str:
    """UTF-8 安全截断：不劈多字节字符。"""
    data = value.encode("utf-8")
    if len(data) <= limit:
        return value
    return data[:limit].decode("utf-8", errors="ignore")


def _normalize_wire_field_lengths(records: dict[str, Any]) -> int:
    """按服务端 lesson 字段字节上限截断 patterns/incidents 的超长字段。

    只发生在出网边界（本地账本保持原文）；返回截断的字段数。
    """
    truncated = 0
    for kind in ("patterns", "incidents"):
        for item in records.get(kind) or []:
            if not isinstance(item, dict):
                continue
            for field, limit in _WIRE_FIELD_BYTE_LIMITS.items():
                value = item.get(field)
                if isinstance(value, str) and len(value.encode("utf-8")) > limit:
                    item[field] = _utf8_truncate(value, limit)
                    truncated += 1
    return truncated


def build_private_sync_records(project_ledger: str | os.PathLike[str],
                               *, detail_mode: str | None = None,
                               exclude_baseline: bool = False,
                               policy: str = "allow") -> dict[str, Any]:
    """构造私有 Cloud Sync 的 V2 records（客户端侧字段策略单一实现点）。

    - detail_mode=full（默认）：完整详情 + 机器前缀脱敏；
    - detail_mode=summary（用户按 scope 显式选择）：仅摘要/分类/指纹/计数/状态；
    - exclude_baseline=True（项目 scope 默认启用）：拦截内置系统防御基线，由云端全局层统一继承；
    - tombstone（retired/superseded/revoked）以 deleted 记录单独携带；
    - 出网前按服务端 validateLessonRecord 字段字节上限归一化（PE-20261004-005）；
    - 凭据与密钥在返回前整体阻断（DOC-211：policy 允许 redact=形状复扫 /
      allow=跳过，span 脱敏由调用方在 manifest 之后重算的链路完成）；
    - 输出 manifest_hash 覆盖完整 records 对象（§5.4 canonical 口径）。
    """
    mode = resolve_detail_mode(detail_mode)
    state = ledger.read(project_ledger)
    patterns_by_id = {
        str(item.get("pattern_id")): item
        for item in (state.get("lessons") or {}).values()
        if isinstance(item, dict)
    }
    incidents: list[dict[str, Any]] = []
    incident_offset = 0
    incident_page_size = 5000
    while True:
        page = ledger.list_incidents(
            project_ledger, limit=incident_page_size, offset=incident_offset,
        )
        incidents.extend(page)
        if len(page) < incident_page_size:
            break
        incident_offset += incident_page_size
    tool_experience = ledger.export_tool_experience(project_ledger)
    consumption_events = ledger.export_consumption_events(project_ledger)

    patterns_all = sorted(state["lessons"].values(), key=lambda item: item["pattern_id"])
    live_patterns: list[dict[str, Any]] = []
    tombstones: list[dict[str, Any]] = []
    for pattern in patterns_all:
        if exclude_baseline and is_baseline_seed_pattern(pattern):
            continue
        if pattern["status"] in _TOMBSTONE_PATTERN_STATUSES:
            tombstones.append({
                "record_type": "pattern", "record_id": pattern["pattern_id"],
                "version": max(1, int(pattern.get("version", 0) or 0)),
                "deleted": True, "updated_at": pattern["updated_at"],
                "final_status": pattern["status"],
            })
        else:
            live_patterns.append(pattern)
    live_incidents: list[dict[str, Any]] = []
    for incident in incidents:
        if incident.get("status") == "revoked":
            tombstones.append({
                "record_type": "incident", "record_id": incident["incident_id"],
                "version": max(1, int(incident.get("version", 0) or 0)),
                "deleted": True, "updated_at": incident["updated_at"],
                "final_status": "revoked",
            })
        else:
            # Incident 本地模型把可复用的复现判据放在父 Pattern；出网前
            # 展开为独立 wire 字段，同时补齐服务端要求的标题/根因/分类。
            enriched = dict(incident)
            parent = patterns_by_id.get(str(incident.get("pattern_id") or ""), {})
            for field in ("category", "title", "root_cause", "repro_oracle"):
                if not enriched.get(field) and parent.get(field):
                    enriched[field] = parent[field]
            if not enriched.get("error_signature"):
                enriched["error_signature"] = enriched.get("signature", "")
            live_incidents.append(enriched)

    if mode == "summary":
        records = {
            "patterns": [strip_to_summary(item, "pattern") for item in live_patterns],
            "incidents": [strip_to_summary(item, "incident") for item in live_incidents],
            "tool_experience": [strip_to_summary(item, "tool_experience")
                                for item in tool_experience],
            "consumption_events": consumption_events,
            "tombstones": tombstones,
        }
        scrubs = 0
    else:
        scrubbed_patterns, count_p = _scrub_machine_prefixes(live_patterns)
        scrubbed_incidents, count_i = _scrub_machine_prefixes(live_incidents)
        # tool_experience 的 category 同样可能携带机器前缀绝对路径，
        # full 模式必须与 patterns/incidents 一样过 _scrub_machine_prefixes。
        scrubbed_tools: list[dict[str, Any]] = []
        count_t = 0
        for item in tool_experience:
            cleaned_tool = dict(item)
            new_category, n = _scrub_machine_prefixes(
                str(item.get("category") or ""))
            cleaned_tool["category"] = new_category
            count_t += n
            scrubbed_tools.append(cleaned_tool)
        records = {
            "patterns": scrubbed_patterns,
            "incidents": scrubbed_incidents,
            "tool_experience": scrubbed_tools,
            "consumption_events": consumption_events,
            "tombstones": tombstones,
        }
        scrubs = count_p + count_i + count_t
    # DOC-211：凭据硬门禁必须在**内容成形之前**判定，且扫的对象要区分模式：
    #   * full  → 扫**脱敏前**的原始记录。``_scrub_machine_prefixes`` 会把
    #     ``token: xxx`` 这类机器前缀洗掉，若在脱敏后再扫就永远扫不到，历史脏
    #     数据里的凭据会被静默洗掉而不是阻断发网（实测
    #     ``test_credentials_blocked_even_when_summary_strips_details`` 因此失败）；
    #   * summary → 扫 strip 之后的记录：``reflection``/``decision`` 本来就不发网，
    #     不该因它们阻断；但保留字段（如 ``title``）里的凭据仍必须拦。
    # redact 策略不走这里：span 脱敏 + 双重复扫由调用方
    # ``prepare_records_for_sync`` 在 manifest 之后完成。
    if policy != "redact":
        scan_target = records if mode == "summary" else {
            "patterns": live_patterns,
            "incidents": live_incidents,
            "tool_experience": tool_experience,
            "consumption_events": consumption_events,
            "tombstones": tombstones,
        }
        enforce_credential_block(scan_target, policy=policy, hint_exits=True)
    field_truncations = _normalize_wire_field_lengths(records)
    manifest_hash = "sha256:" + hashlib.sha256(_canon(records)).hexdigest()
    return {
        "detail_mode": mode,
        "records": records,
        "manifest_hash": manifest_hash,
        "source_state_hash": state["state_hash"],
        "machine_prefix_scrubs": scrubs,
        "field_truncations": field_truncations,
    }


def status(project_ledger: Path, global_ledger: Path, global_root: Path) -> dict[str, Any]:
    project = ledger.metrics(project_ledger)
    global_db = global_ledger.with_suffix(".sqlite3")
    global_value: dict[str, Any]
    if not global_db.is_file() or global_db.is_symlink():
        global_value = {"status": "missing", "database": str(global_db)}
    else:
        global_value = ledger.metrics(global_ledger)
    versions = []
    version_root = global_root / "versions"
    if version_root.is_dir():
        for path in sorted(version_root.glob("B-*.json")):
            try:
                item = _read_json(path, "版本记录")
                # Rollback records live next to import records but are not
                # import versions and must not appear as half-empty entries
                # in the import history reported by status.
                if item.get("record_type") == "rollback" or item.get("rolled_back_at"):
                    continue
                versions.append({"bundle_id": item.get("bundle_id"),
                                 "after_state_hash": item.get("after_state_hash"),
                                 "imported_at": item.get("imported_at")})
            except KnowledgeSyncError:
                continue
    return {"status": "ok", "project": project, "global": global_value,
            "global_versions": versions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "approve", "import", "rollback", "status"))
    parser.add_argument("--project-ledger", type=Path)
    parser.add_argument("--global-ledger", type=Path)
    parser.add_argument("--global-root", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bundle-id")
    parser.add_argument("--contributor", default="local-supervisor")
    parser.add_argument("--reviewer")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            if not all((args.project_ledger, args.output)):
                raise KnowledgeSyncError("export 需要 --project-ledger 和 --output")
            result = export_candidates(args.project_ledger, args.output, args.contributor)
        elif args.command == "approve":
            if not all((args.bundle, args.output, args.reviewer, args.reason)):
                raise KnowledgeSyncError("approve 需要 --bundle --output --reviewer --reason")
            result = approve_bundle(args.bundle, args.output, args.reviewer, args.reason)
        elif args.command == "import":
            if not all((args.global_ledger, args.global_root, args.bundle, args.reviewer)):
                raise KnowledgeSyncError("import 需要 --global-ledger --global-root --bundle --reviewer")
            result = import_approved(args.global_ledger, args.global_root, args.bundle, args.reviewer)
        elif args.command == "rollback":
            if not all((args.global_ledger, args.global_root, args.bundle_id, args.reviewer)):
                raise KnowledgeSyncError("rollback 需要 --global-ledger --global-root --bundle-id --reviewer")
            result = rollback(args.global_ledger, args.global_root, args.bundle_id, args.reviewer)
        else:
            if not all((args.project_ledger, args.global_ledger, args.global_root)):
                raise KnowledgeSyncError("status 需要 --project-ledger --global-ledger --global-root")
            result = status(args.project_ledger, args.global_ledger, args.global_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KnowledgeSyncError, ledger.LessonLedgerError, OSError, json.JSONDecodeError) as exc:
        print(f"[knowledge_sync] ✗ {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
