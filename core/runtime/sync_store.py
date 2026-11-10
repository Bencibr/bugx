#!/usr/bin/env python3
"""BugX Cloud Sync 本地状态存储（DOC-008 §6.1，ADR-008-02）。

独立于运行时 Ledger 的 sync SQLite：只拥有传输、暂存与冲突状态，
Ledger 仍是知识事实源。项目 scope 写 ``<root>/.bugx/cloud-sync/state.sqlite3``；
global scope 写 ``~/.bugx/cloud-sync/state.sqlite3``。

表：
- ``sync_scope_state``     committed cursor / snapshot / remote_version（唯一键 account+scope+project）
- ``sync_outbox``          operation 幂等（operation_id 主键）、重试、dead-letter
- ``sync_inbox_snapshot``  staged → validated → imported / rejected 快照生命周期
- ``sync_inbox_record``    每记录 payload + hash + 校验状态
- ``sync_conflict``        同 version 不同 hash 等冲突队列

关键不变量（AD-008-02/05）：
- committed cursor 只在快照成功导入后随 ``inbox_commit_import`` 原子提交；
- 相同 operation_id 重复 enqueue 不产生第二行、不改变已 acked 状态；
- 不可重试错误或超过最大尝试次数进入 dead-letter，绝不静默丢弃。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sqlite3
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

STORE_SCHEMA_VERSION = 2
OUTBOX_STATES = ("pending", "inflight", "acked", "dead_letter")
SNAPSHOT_STATES = ("staged", "validated", "imported", "rejected")
INBOX_RECORD_TYPES = (
    "patterns", "incidents", "tool_experience", "consumption_events", "tombstones",
)
CONFLICT_STATES = ("open", "resolved_local", "resolved_remote", "merged")
DEFAULT_MAX_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 30


class SyncStoreError(RuntimeError):
    """sync store 打不开、迁移不安全或状态转换非法。"""


def _validate_scope(scope: str) -> None:
    if scope not in {"global", "project"}:
        raise SyncStoreError(f"非法同步 scope: {scope}")


def _validate_nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SyncStoreError(f"{field} 必须是非负整数")
    return value


# V2 wire record_type 的新旧单复数写法在历史队列/元数据行中并存，
# 按记录身份操作（clear/resolve/prune）时必须覆盖全部别名。
_RECORD_TYPE_ALIASES = {
    "pattern": ("pattern", "patterns"),
    "patterns": ("pattern", "patterns"),
    "incident": ("incident", "incidents"),
    "incidents": ("incident", "incidents"),
    "tool": ("tool", "tool_experience"),
    "tool_experience": ("tool", "tool_experience"),
}


def _record_type_aliases(record_type: str) -> tuple[str, ...]:
    return _RECORD_TYPE_ALIASES.get(str(record_type or ""), (str(record_type or ""),))


from core.bugx_paths import assert_no_symlink_components


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _assert_no_symlink_components(path: Path) -> None:
    """拒绝让本地同步状态库跟随目录或目标文件符号链接。"""
    assert_no_symlink_components(path, error_cls=SyncStoreError)


def project_store_path(root: Path) -> Path:
    return Path(root).expanduser().absolute() / ".bugx" / "cloud-sync" / "state.sqlite3"


def global_store_path() -> Path:
    """global scope 的 sync store 路径（默认 ~/.bugx/cloud-sync/state.sqlite3）。

    BUGX_GLOBAL_KNOWLEDGE_ROOT 覆盖时跟随其父目录，便于测试隔离。
    """
    import os
    override = os.environ.get("BUGX_GLOBAL_KNOWLEDGE_ROOT")
    if override:
        return Path(override).expanduser().absolute().parent / "cloud-sync" / "state.sqlite3"
    return Path.home() / ".bugx" / "cloud-sync" / "state.sqlite3"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_store_meta(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_scope_state(
  account_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL,
  project_id TEXT NOT NULL DEFAULT '',
  committed_cursor TEXT NOT NULL DEFAULT '',
  committed_snapshot_id TEXT NOT NULL DEFAULT '',
  remote_version INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,
  last_error_code TEXT,
  last_request_id TEXT,
  protocol_version INTEGER NOT NULL DEFAULT 2,
  PRIMARY KEY(account_id, scope, project_id));
CREATE TABLE IF NOT EXISTS sync_outbox(
  operation_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL,
  project_id TEXT NOT NULL DEFAULT '',
  record_type TEXT NOT NULL DEFAULT 'batch',
  record_id TEXT NOT NULL DEFAULT '',
  local_version INTEGER NOT NULL DEFAULT 0,
  base_version INTEGER NOT NULL DEFAULT 0,
  payload_hash TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK(state IN ('pending','inflight','acked','dead_letter')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  acked_at TEXT);
CREATE TABLE IF NOT EXISTS sync_inbox_snapshot(
  snapshot_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL,
  project_id TEXT NOT NULL DEFAULT '',
  manifest_hash TEXT NOT NULL DEFAULT '',
  cursor_before TEXT NOT NULL DEFAULT '',
  cursor_after TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK(state IN ('staged','validated','imported','rejected')),
  record_count INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL,
  validated_at TEXT,
  imported_at TEXT,
  reject_code TEXT);
CREATE TABLE IF NOT EXISTS sync_inbox_record(
  snapshot_id TEXT NOT NULL,
  record_type TEXT NOT NULL,
  record_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  remote_version INTEGER NOT NULL DEFAULT 0,
  tombstone INTEGER NOT NULL DEFAULT 0,
  validation_state TEXT NOT NULL DEFAULT 'staged',
  reject_code TEXT,
  PRIMARY KEY(snapshot_id, record_type, record_id));
CREATE TABLE IF NOT EXISTS sync_conflict(
  conflict_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL,
  project_id TEXT NOT NULL DEFAULT '',
  record_type TEXT NOT NULL,
  record_id TEXT NOT NULL,
  local_version INTEGER NOT NULL DEFAULT 0,
  remote_version INTEGER NOT NULL DEFAULT 0,
  local_hash TEXT NOT NULL DEFAULT '',
  remote_hash TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open'
    CHECK(state IN ('open','resolved_local','resolved_remote','merged')),
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolver_identity TEXT);
CREATE TABLE IF NOT EXISTS sync_record_state(
  account_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL,
  project_id TEXT NOT NULL DEFAULT '',
  record_type TEXT NOT NULL,
  record_id TEXT NOT NULL,
  remote_version INTEGER NOT NULL DEFAULT 0,
  remote_hash TEXT NOT NULL DEFAULT '',
  remote_deleted INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_id, scope, project_id, record_type, record_id));
"""


class SyncStore:
    """sync state SQLite 封装：所有变更单事务，打开即幂等迁移。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().absolute()
        try:
            _assert_no_symlink_components(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _assert_no_symlink_components(self.path)
            if self.path.parent.stat().st_mode & 0o777 != 0o700:
                os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            raise SyncStoreError(f"sync store 目录不可创建: {self.path}: {exc}") from exc
        try:
            if self.path.exists() and not self.path.is_file():
                raise SyncStoreError(f"sync store 不是普通文件: {self.path}")
            self._conn = sqlite3.connect(str(self.path), timeout=10)
            if self.path.stat().st_mode & 0o777 != 0o600:
                os.chmod(self.path, 0o600)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate()
        except sqlite3.Error as exc:
            raise SyncStoreError(f"sync store 无法打开: {self.path}: {exc}") from exc

    # ---- 生命周期 ----
    def _migrate(self) -> None:
        with self._conn:
            row = self._conn.execute(
                "SELECT value FROM sync_store_meta WHERE key='store_schema_version'"
            ).fetchone() if self._table_exists("sync_store_meta") else None
            current = int(row["value"]) if row else 0
            if current > STORE_SCHEMA_VERSION:
                raise SyncStoreError(
                    f"sync store schema 版本更新({current} > {STORE_SCHEMA_VERSION})，"
                    "拒绝以旧代码写入"
                )
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT INTO sync_store_meta(key,value) VALUES('store_schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(STORE_SCHEMA_VERSION),),
            )

    def _table_exists(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """check-then-act 序列必须整体跑在一个显式 ``BEGIN IMMEDIATE`` 事务里。

        连接默认是 deferred 隔离：守卫 SELECT 不持写锁，另一个进程完全可以在
        本进程守卫与写入之间提交同键行，把"已存在检查"变成竞态窗口。
        ``BEGIN IMMEDIATE`` 在第一条语句前就取得写保留，使并发写方排队而非
        穿插；与 core/lesson_ledger.py 的 ``_tx`` 同型。异常时 ROLLBACK，
        成功时 COMMIT。
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SyncStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @staticmethod
    def record_payload_hash(record: dict[str, Any]) -> str:
        canonical = json_canonical(record)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ---- scope state / committed cursor ----
    def get_scope_state(self, account_id: str, scope: str,
                        project_id: str = "") -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sync_scope_state WHERE account_id=? AND scope=? AND project_id=?",
            (account_id, scope, project_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def inspect_scope(path: str | Path, account_id: str, scope: str,
                      project_id: str = "") -> dict[str, Any]:
        """以只读方式查看一个 scope 的同步状态，不创建或迁移数据库。

        ``cloud status`` 必须是诊断操作：不存在的 store 不能因为查看状态而被
        创建，已有 store 也不能触发 schema 写入。因此这里使用 SQLite
        ``mode=ro``，并把不存在/尚未初始化与损坏明确区分。
        """
        if scope not in {"global", "project"}:
            raise SyncStoreError(f"非法同步 scope: {scope}")
        target = Path(path).expanduser().absolute()
        _assert_no_symlink_components(target)
        base: dict[str, Any] = {
            "path": str(target), "scope": scope, "project_id": project_id,
            "state": "not_initialized",
            "scope_state": None,
            "outbox": {state: 0 for state in OUTBOX_STATES},
            "inbox": {state: 0 for state in SNAPSHOT_STATES},
            "conflicts": {state: 0 for state in CONFLICT_STATES},
        }
        if not target.exists():
            return base
        if target.is_symlink() or not target.is_file():
            raise SyncStoreError(f"sync store 不是安全的普通文件: {target}")
        uri = "file:" + urllib.parse.quote(str(target), safe="/") + "?mode=ro"
        try:
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=10)
                try:
                    # WAL 库在 -wal/-shm 不存在或状态过期时，mode=ro 连接
                    # 无法创建索引文件而报 "unable to open database file"。
                    # 该失败可能在 connect 时惰性出现，用一次探测查询触发。
                    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                except sqlite3.OperationalError:
                    conn.close()
                    raise
            except sqlite3.OperationalError:
                # 降级为普通连接 + query_only（同 lesson_ledger._conn 的
                # 0.6.76 模式）：连接可补建 -shm/-wal，任何数据写入仍被
                # SQLite 拒绝，只读语义不变。
                conn = sqlite3.connect(str(target), timeout=10)
                conn.execute("PRAGMA query_only=ON")
            conn.row_factory = sqlite3.Row
            try:
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                required = {"sync_store_meta", "sync_scope_state", "sync_outbox",
                            "sync_inbox_snapshot", "sync_inbox_record",
                            "sync_record_state", "sync_conflict"}
                if not required.issubset(tables):
                    base["state"] = "uninitialized"
                    return base
                row = conn.execute(
                    "SELECT * FROM sync_scope_state "
                    "WHERE account_id=? AND scope=? AND project_id=?",
                    (account_id, scope, project_id),
                ).fetchone()
                base["scope_state"] = dict(row) if row else None
                for table, key in (("sync_outbox", "outbox"),
                                   ("sync_inbox_snapshot", "inbox"),
                                   ("sync_conflict", "conflicts")):
                    rows = conn.execute(
                        f"SELECT state, COUNT(*) AS count FROM {table} "
                        "WHERE account_id=? AND scope=? AND project_id=? GROUP BY state",
                        (account_id, scope, project_id),
                    ).fetchall()
                    for item in rows:
                        if item["state"] in base[key]:
                            base[key][item["state"]] = int(item["count"])
                base["state"] = "ready"
                return base
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise SyncStoreError(f"sync store 只读检查失败: {target}: {exc}") from exc

    def commit_scope_sync(self, account_id: str, scope: str, project_id: str, *,
                          cursor: str, snapshot_id: str, remote_version: int,
                          request_id: str | None = None,
                          record_states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """committed cursor 只能伴随成功导入推进；单事务 upsert。"""
        _validate_scope(scope)
        if not isinstance(cursor, str) or not isinstance(snapshot_id, str):
            raise SyncStoreError("cursor/snapshot_id 必须是字符串")
        remote_version = _validate_nonnegative_int(remote_version, "remote_version")
        now = _now()
        with self._tx():
            current = self._conn.execute(
                "SELECT * FROM sync_scope_state WHERE account_id=? AND scope=? AND project_id=?",
                (account_id, scope, project_id),
            ).fetchone()
            if current and remote_version < int(current["remote_version"] or 0) \
                    and (str(current["committed_cursor"] or "") != cursor
                         or str(current["committed_snapshot_id"] or "") != snapshot_id):
                raise SyncStoreError(
                    "remote_version 下降且 cursor/snapshot 不同，拒绝回退同步状态"
                )
            if current and (str(current["committed_snapshot_id"] or "")
                            or str(current["committed_cursor"] or "")) \
                    and (str(current["committed_snapshot_id"] or "") != snapshot_id
                         or str(current["committed_cursor"] or "") != cursor):
                raise SyncStoreError(
                    "已有不同的 committed snapshot/cursor，必须通过 inbox 暂存并校验后推进"
                )
            self._conn.execute(
                "INSERT INTO sync_scope_state(account_id,scope,project_id,committed_cursor,"
                "committed_snapshot_id,remote_version,last_success_at,last_request_id,"
                "protocol_version) VALUES(?,?,?,?,?,?,?,?,2) "
                "ON CONFLICT(account_id,scope,project_id) DO UPDATE SET "
                "committed_cursor=excluded.committed_cursor,"
                "committed_snapshot_id=excluded.committed_snapshot_id,"
                "remote_version=MAX(sync_scope_state.remote_version,excluded.remote_version),"
                "last_success_at=excluded.last_success_at,"
                "last_request_id=excluded.last_request_id,"
                "last_error_code=NULL",
                (account_id, scope, project_id, cursor, snapshot_id,
                 remote_version, now, request_id),
            )
            self._upsert_record_states_tx(
                account_id, scope, project_id, record_states or [], now,
            )
        return self.get_scope_state(account_id, scope, project_id) or {}

    def adopt_import_snapshot(self, account_id: str, scope: str, project_id: str,
                              *, snapshot_id: str, remote_version: int) -> dict[str, Any]:
        """Adopt an explicit local-authoritative import without inventing a cursor."""
        _validate_scope(scope)
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise SyncStoreError("snapshot_id 必须是非空字符串")
        remote_version = _validate_nonnegative_int(remote_version, "remote_version")
        now = _now()
        with self._tx():
            current = self._conn.execute(
                "SELECT remote_version FROM sync_scope_state "
                "WHERE account_id=? AND scope=? AND project_id=?",
                (account_id, scope, project_id),
            ).fetchone()
            if current and remote_version < int(current["remote_version"] or 0):
                raise SyncStoreError("导入 snapshot 的 remote_version 不能回退")
            self._conn.execute(
                "INSERT INTO sync_scope_state(account_id,scope,project_id,"
                "committed_cursor,committed_snapshot_id,remote_version,last_success_at,"
                "protocol_version) VALUES(?,?,?,?,?,?,?,2) "
                "ON CONFLICT(account_id,scope,project_id) DO UPDATE SET "
                "committed_cursor='',committed_snapshot_id=excluded.committed_snapshot_id,"
                "remote_version=MAX(sync_scope_state.remote_version,excluded.remote_version),"
                "last_success_at=excluded.last_success_at,last_error_code=NULL",
                (account_id, scope, project_id, "", snapshot_id, remote_version, now),
            )
            self._conn.execute(
                "UPDATE sync_conflict SET state='resolved_local',resolved_at=?,"
                "resolver_identity='cloud-upload-local-authoritative' "
                "WHERE account_id=? AND scope=? AND project_id=? AND state='open'",
                (now, account_id, scope, project_id),
            )
            self._conn.execute(
                "UPDATE sync_outbox SET state='acked',acked_at=?,updated_at=?,"
                "last_error_code=NULL WHERE account_id=? AND scope=? AND project_id=? "
                "AND state IN ('pending','inflight')",
                (now, now, account_id, scope, project_id),
            )
        self.prune_converged_conflicts(account_id, scope, project_id, resolver_identity="adopt-import-snapshot")
        return self.get_scope_state(account_id, scope, project_id) or {}

    def record_scope_error(self, account_id: str, scope: str, project_id: str, *,
                           error_code: str, request_id: str | None = None) -> None:
        """只记录诊断信息，绝不触碰 committed cursor/snapshot。"""
        _validate_scope(scope)
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_scope_state(account_id,scope,project_id,"
                "last_error_code,last_request_id,protocol_version) "
                "VALUES(?,?,?,?,?,2) "
                "ON CONFLICT(account_id,scope,project_id) DO UPDATE SET "
                "last_error_code=excluded.last_error_code,"
                "last_request_id=excluded.last_request_id",
                (account_id, scope, project_id, error_code, request_id),
            )

    def reset_scope(self, account_id: str, scope: str, project_id: str = "",
                    *, error_code: str = "SYNC_RESYNC_REQUIRED",
                    request_id: str | None = None) -> None:
        """Discard transport state for exactly one scope.

        A server tombstone floor can make a committed cursor unrecoverable.
        Reset removes staged snapshots, remote record metadata, conflicts and
        all pending/acked outbox rows for this account/scope/project, then
        leaves an explicit empty scope row so the next pull is a baseline.
        Other accounts and scopes remain untouched.
        """
        _validate_scope(scope)
        if not isinstance(error_code, str) or not error_code.strip():
            raise SyncStoreError("error_code 必须是非空字符串")
        with self._tx():
            self._conn.execute(
                "DELETE FROM sync_inbox_record WHERE snapshot_id IN ("
                "SELECT snapshot_id FROM sync_inbox_snapshot "
                "WHERE account_id=? AND scope=? AND project_id=? )",
                (account_id, scope, project_id),
            )
            self._conn.execute(
                "DELETE FROM sync_inbox_snapshot "
                "WHERE account_id=? AND scope=? AND project_id=?",
                (account_id, scope, project_id),
            )
            self._conn.execute(
                "DELETE FROM sync_outbox WHERE account_id=? AND scope=? AND project_id=?",
                (account_id, scope, project_id),
            )
            self._conn.execute(
                "DELETE FROM sync_record_state WHERE account_id=? AND scope=? AND project_id=?",
                (account_id, scope, project_id),
            )
            self._conn.execute(
                "DELETE FROM sync_conflict WHERE account_id=? AND scope=? AND project_id=?",
                (account_id, scope, project_id),
            )
            self._conn.execute(
                "INSERT INTO sync_scope_state(account_id,scope,project_id,"
                "committed_cursor,committed_snapshot_id,remote_version,last_success_at,"
                "last_error_code,last_request_id,protocol_version) VALUES(?,?,?,?,?,?,?,?,?,2) "
                "ON CONFLICT(account_id,scope,project_id) DO UPDATE SET "
                "committed_cursor='',committed_snapshot_id='',remote_version=0,"
                "last_success_at=NULL,last_error_code=excluded.last_error_code,"
                "last_request_id=excluded.last_request_id,protocol_version=2",
                (account_id, scope, project_id, "", "", 0, None,
                 error_code.strip(), request_id),
            )

    # ---- per-record remote metadata ----
    def get_record_state(self, account_id: str, scope: str,
                         project_id: str, record_type: str,
                         record_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sync_record_state "
            "WHERE account_id=? AND scope=? AND project_id=? "
            "AND record_type=? AND record_id=?",
            (account_id, scope, project_id, record_type, record_id),
        ).fetchone()
        return dict(row) if row else None

    def get_record_states(self, account_id: str, scope: str,
                          project_id: str = "") -> dict[tuple[str, str], dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sync_record_state "
            "WHERE account_id=? AND scope=? AND project_id=?",
            (account_id, scope, project_id),
        ).fetchall()
        return {(str(row["record_type"]), str(row["record_id"])): dict(row)
                for row in rows}

    def upsert_record_states(self, account_id: str, scope: str,
                             project_id: str,
                             records: list[dict[str, Any]]) -> None:
        """批量记录远端版本，和 snapshot/cursor 使用同一 sync-store 事务。

        该表是传输元数据，不是知识事实源；因此只保存远端版本、内容哈希和
        删除标记，不能用它直接把本地 candidate 晋级为 active。
        """
        _validate_scope(scope)
        if not records:
            return
        now = _now()
        for item in records:
            record_type = str(item.get("record_type") or "")
            record_id = str(item.get("record_id") or "")
            if not record_type or not record_id:
                raise SyncStoreError("远端记录元数据缺少 record_type/record_id")
        with self._conn:
            self._upsert_record_states_tx(account_id, scope, project_id, records, now)

    def clear_record_state(self, account_id: str, scope: str,
                           project_id: str, record_type: str,
                           record_id: str) -> int:
        """resolve --side local：删除该记录的远端版本元数据。

        本地权威但**不发明版本**（与 adopt_import_snapshot 同语义）：清掉
        record_state 后，下一轮正常 push 以变更内容走服务器分配新版本，
        客户端绝不自增版本号。record_type 接受新旧单复数写法。
        """
        _validate_scope(scope)
        if not record_type or not record_id:
            raise SyncStoreError("清除记录元数据缺少 record_type/record_id")
        deleted = 0
        with self._conn:
            for alias in _record_type_aliases(record_type):
                deleted += self._conn.execute(
                    "DELETE FROM sync_record_state "
                    "WHERE account_id=? AND scope=? AND project_id=? "
                    "AND record_type=? AND record_id=?",
                    (account_id, scope, project_id, alias, record_id),
                ).rowcount
        return deleted

    def _upsert_record_states_tx(self, account_id: str, scope: str,
                                 project_id: str,
                                 records: list[dict[str, Any]], now: str) -> None:
        values = []
        for item in records:
            record_type = str(item.get("record_type") or "")
            record_id = str(item.get("record_id") or "")
            if not record_type or not record_id:
                raise SyncStoreError("远端记录元数据缺少 record_type/record_id")
            values.append((
                account_id, scope, project_id, record_type, record_id,
                int(item.get("remote_version", item.get("version", 0)) or 0),
                str(item.get("remote_hash") or item.get("content_hash") or ""),
                1 if bool(item.get("remote_deleted", item.get("deleted", False))) else 0,
                now,
            ))
        if values:
            self._conn.executemany(
                "INSERT INTO sync_record_state(account_id,scope,project_id,"
                "record_type,record_id,remote_version,remote_hash,remote_deleted,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,scope,project_id,record_type,record_id) DO UPDATE SET "
                "remote_version=CASE WHEN excluded.remote_version >= remote_version "
                "THEN excluded.remote_version ELSE remote_version END,"
                "remote_hash=CASE WHEN excluded.remote_version >= remote_version "
                "THEN excluded.remote_hash ELSE remote_hash END,"
                "remote_deleted=CASE WHEN excluded.remote_version >= remote_version "
                "THEN excluded.remote_deleted ELSE remote_deleted END,"
                "updated_at=CASE WHEN excluded.remote_version >= remote_version "
                "THEN excluded.updated_at ELSE updated_at END",
                values,
            )

    # ---- outbox（幂等 operation / 重试 / dead-letter）----
    def outbox_enqueue(self, *, operation_id: str, account_id: str = "",
                       scope: str, project_id: str = "",
                       payload_hash: str, base_version: int = 0,
                       local_version: int = 0,
                       record_type: str = "batch",
                       record_id: str = "") -> tuple[dict[str, Any], bool]:
        """相同 operation_id 幂等：返回 (row, created)；已存在行原样返回。"""
        _validate_scope(scope)
        if not operation_id or not payload_hash:
            raise SyncStoreError("operation_id/payload_hash 不能为空")
        local_version = _validate_nonnegative_int(local_version, "local_version")
        base_version = _validate_nonnegative_int(base_version, "base_version")
        now = _now()
        with self._tx():
            existing = self._conn.execute(
                "SELECT * FROM sync_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                identity = {
                    "account_id": account_id, "scope": scope,
                    "project_id": project_id, "record_type": record_type,
                    "record_id": record_id, "local_version": local_version,
                    # base_version is a retry fence, not operation identity:
                    # a deterministic replay may observe a newer remote
                    # version after a successful pull/rebase.
                    "payload_hash": payload_hash,
                }
                if any(existing[key] != value for key, value in identity.items()):
                    raise SyncStoreError(
                        f"operation_id 已存在但请求身份/内容不一致，拒绝复用: {operation_id}"
                    )
                return dict(existing), False
            self._conn.execute(
                "INSERT INTO sync_outbox(operation_id,account_id,scope,project_id,"
                "record_type,record_id,local_version,base_version,payload_hash,"
                "state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (operation_id, account_id, scope, project_id, record_type,
                 record_id, local_version, base_version, payload_hash,
                 now, now),
            )
        row = self._conn.execute(
            "SELECT * FROM sync_outbox WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return dict(row), True

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sync_outbox WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return dict(row) if row else None

    def active_operation_ids(self, *, account_id: str, scope: str,
                             project_id: str = "") -> list[str]:
        """List pending/inflight operations without exposing the SQLite connection."""
        _validate_scope(scope)
        rows = self._conn.execute(
            "SELECT operation_id FROM sync_outbox WHERE account_id=? AND scope=? "
            "AND project_id=? AND state IN ('pending','inflight') ORDER BY created_at",
            (account_id, scope, project_id),
        ).fetchall()
        return [str(row["operation_id"]) for row in rows]

    def has_active_operations(self, *, account_id: str, scope: str,
                              project_id: str = "") -> bool:
        _validate_scope(scope)
        return self._conn.execute(
            "SELECT 1 FROM sync_outbox WHERE account_id=? AND scope=? AND project_id=? "
            "AND state IN ('pending','inflight') LIMIT 1",
            (account_id, scope, project_id),
        ).fetchone() is not None

    def has_acked_payload(self, *, account_id: str, scope: str,
                          project_id: str, payload_hash: str) -> bool:
        _validate_scope(scope)
        return self._conn.execute(
            "SELECT 1 FROM sync_outbox WHERE account_id=? AND scope=? AND project_id=? "
            "AND payload_hash=? AND state='acked' LIMIT 1",
            (account_id, scope, project_id, payload_hash),
        ).fetchone() is not None

    def due_operations(self, *, account_id: str = "", scope: str,
                       project_id: str = "", now: str | None = None) -> list[dict[str, Any]]:
        stamp = now or _now()
        rows = self._conn.execute(
            "SELECT * FROM sync_outbox WHERE account_id=? AND scope=? AND project_id=? "
            "AND state='pending' AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "ORDER BY created_at",
            (account_id, scope, project_id, stamp),
        ).fetchall()
        return [dict(r) for r in rows]

    def outbox_mark_inflight(self, operation_id: str) -> None:
        with self._conn:
            changed = self._conn.execute(
                "UPDATE sync_outbox SET state='inflight',updated_at=? "
                "WHERE operation_id=? AND state IN ('pending','inflight')",
                (_now(), operation_id),
            ).rowcount
        if not changed:
            raise SyncStoreError(f"operation {operation_id} 不能进入 inflight")

    def outbox_requeue(self, operation_id: str) -> dict[str, Any]:
        """显式重放 dead-letter operation，保留原记录与内容指纹。

        dead-letter 表示本次尝试已停止，不表示 operation 永久失效。服务端
        升级、凭据恢复或人工修复后，下一次显式同步应能复用同一 operation_id
        做幂等重放，而不是卡在 ``不能进入 inflight``。重置本地尝试次数，
        但保留 ``last_error_code`` 作为诊断证据。
        """
        now = _now()
        with self._conn:
            row = self._conn.execute(
                "SELECT state FROM sync_outbox WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if not row:
                raise SyncStoreError(f"operation 不存在: {operation_id}")
            if row["state"] != "dead_letter":
                raise SyncStoreError(
                    f"operation {operation_id} 状态 {row['state']} 不是 dead_letter，不能重放"
                )
            self._conn.execute(
                "UPDATE sync_outbox SET state='pending',attempts=0,next_retry_at=NULL,"
                "updated_at=? WHERE operation_id=? AND state='dead_letter'",
                (now, operation_id),
            )
        return self.get_operation(operation_id) or {}

    def outbox_ack(self, operation_id: str, *, remote_version: int | None = None,
                   request_id: str | None = None) -> None:
        if remote_version is not None:
            remote_version = _validate_nonnegative_int(remote_version, "remote_version")
        now = _now()
        with self._conn:
            row = self._conn.execute(
                "SELECT state,account_id,scope,project_id FROM sync_outbox "
                "WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if not row:
                raise SyncStoreError(f"operation 不存在: {operation_id}")
            if row["state"] in {"dead_letter"}:
                raise SyncStoreError(f"operation {operation_id} 已进入 dead_letter，不能 ack")
            changed = self._conn.execute(
                "UPDATE sync_outbox SET state='acked',acked_at=?,updated_at=?,"
                "last_error_code=NULL "
                "WHERE operation_id=? AND state IN ('pending','inflight','acked')",
                (now, now, operation_id),
            ).rowcount
            if not changed:
                raise SyncStoreError(f"operation {operation_id} 不能 ack")
            # 注意：outbox_ack 刻意不写 sync_scope_state.remote_version。
            # outbox_ack 也被 200-partial（服务端已提交但存在冲突）路径调用，
            # 若在此推进 scope 版本，pull 回退守卫会拒绝更低的权威快照、
            # rebase 永不发生，而 operation_id 幂等又会永久抑制重发——
            # 真分歧的本地知识将静默欠提交（Round 7 红队 BLOCK 条款）。
            # clean-200 的版本回写必须走 advance_remote_version 窄方法。
            if request_id is not None:
                self._conn.execute(
                    "UPDATE sync_scope_state SET last_request_id=? "
                    "WHERE account_id=? AND scope=? AND project_id=?",
                    (request_id, row["account_id"], row["scope"], row["project_id"]),
                )

    def advance_remote_version(self, account_id: str, scope: str,
                               project_id: str, remote_version: int) -> None:
        """clean-200 push 专属的窄版本回写：MAX 守卫只升不降。

        只允许在 push 返回 200 且零冲突的路径调用；只动 remote_version，
        绝不触碰 committed_cursor/committed_snapshot_id——否则 pull 侧的
        版本回退守卫与幂等重放豁免会失真（Round 7 红队 BLOCK 约束）。
        """
        _validate_scope(scope)
        remote_version = _validate_nonnegative_int(remote_version, "remote_version")
        with self._conn:
            self._conn.execute(
                "INSERT INTO sync_scope_state(account_id,scope,project_id,"
                "remote_version,protocol_version) VALUES(?,?,?,?,2) "
                "ON CONFLICT(account_id,scope,project_id) DO UPDATE SET "
                "remote_version=MAX(sync_scope_state.remote_version,excluded.remote_version)",
                (account_id, scope, project_id, remote_version),
            )

    def recover_stale_inflight(self, *, max_age_seconds: int = 900,
                               error_code: str = "SYNC_WORKER_INTERRUPTED") -> int:
        """将被杀死或断网遗留的 inflight 操作恢复为 pending。"""
        if not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool) \
                or max_age_seconds < 1:
            raise SyncStoreError("max_age_seconds 必须是正整数")
        cutoff = (dt.datetime.now(dt.timezone.utc)
                   - dt.timedelta(seconds=max_age_seconds)
                   ).isoformat(timespec="seconds")
        now = _now()
        with self._conn:
            return self._conn.execute(
                "UPDATE sync_outbox SET state='pending',next_retry_at=?,"
                "last_error_code=?,updated_at=? "
                "WHERE state='inflight' AND updated_at<?",
                (now, error_code, now, cutoff),
            ).rowcount

    def outbox_fail(self, operation_id: str, error_code: str, *,
                    retryable: bool = True, retry_after_seconds: int | None = None,
                    max_attempts: int | None = DEFAULT_MAX_ATTEMPTS) -> dict[str, Any]:
        """可重试错误按退避排队；网络故障可保持 pending 等待恢复。"""
        now = _now()
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM sync_outbox WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if not row:
                raise SyncStoreError(f"operation 不存在: {operation_id}")
            if row["state"] not in {"pending", "inflight"}:
                raise SyncStoreError(
                    f"operation {operation_id} 状态 {row['state']} 不能重试或进入 dead_letter"
                )
            if max_attempts is not None and (
                not isinstance(max_attempts, int) or isinstance(max_attempts, bool)
                or max_attempts < 1
            ):
                raise SyncStoreError("max_attempts 必须是正整数")
            attempts = int(row["attempts"]) + 1
            # ponytail: network retries stay pending with capped backoff; a
            # durable daemon is unnecessary because startup/boundary triggers
            # already provide the next recovery opportunity.
            dead = (not retryable) or (
                max_attempts is not None and attempts >= max_attempts
            )
            next_retry = None
            if not dead:
                delay = retry_after_seconds if retry_after_seconds is not None \
                    else _RETRY_BASE_SECONDS * (2 ** (attempts - 1))
                next_retry = (dt.datetime.now(dt.timezone.utc)
                              + dt.timedelta(seconds=min(delay, 3600))
                              ).isoformat(timespec="seconds")
            self._conn.execute(
                "UPDATE sync_outbox SET attempts=?,state=?,next_retry_at=?,"
                "last_error_code=?,updated_at=? WHERE operation_id=?",
                (attempts, "dead_letter" if dead else "pending",
                 next_retry, error_code, now, operation_id),
            )
        return self.get_operation(operation_id) or {}

    def list_dead_letters(self, *, older_than_days: int = 0) -> list[dict[str, Any]]:
        """列出 outbox 中的 dead_letter 行（死信治理用，按 created_at 排序）。

        ``older_than_days`` 为 0 时返回全部死信；大于 0 时只返回
        ``created_at`` 早于该天数的滞留行（ISO UTC 字符串可直接字典序比较）。
        """
        if not isinstance(older_than_days, int) or isinstance(older_than_days, bool) \
                or older_than_days < 0:
            raise SyncStoreError("older_than_days 必须是非负整数")
        query = "SELECT * FROM sync_outbox WHERE state='dead_letter'"
        params: list[Any] = []
        if older_than_days > 0:
            cutoff = (dt.datetime.now(dt.timezone.utc)
                      - dt.timedelta(days=older_than_days)).isoformat(timespec="seconds")
            query += " AND created_at <= ?"
            params.append(cutoff)
        query += " ORDER BY created_at"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def purge_dead_letters(self, operation_ids: list[str]) -> int:
        """按 operation_id 删除 dead_letter 行，返回实际删除数。

        死信清理只能由显式的治理命令在归档留档后调用；DELETE 语句限定
        ``state='dead_letter'``，pending/inflight/acked 行绝不会因此丢失。
        """
        if not isinstance(operation_ids, list) \
                or any(not isinstance(op, str) or not op for op in operation_ids):
            raise SyncStoreError("operation_ids 必须是非空字符串列表")
        if not operation_ids:
            return 0
        deleted = 0
        with self._conn:
            for operation_id in operation_ids:
                deleted += self._conn.execute(
                    "DELETE FROM sync_outbox WHERE operation_id=? AND state='dead_letter'",
                    (operation_id,),
                ).rowcount
        return deleted

    # ---- inbox（staged → validated → imported；rejected 保留诊断）----
    def inbox_stage_snapshot(self, *, snapshot_id: str, account_id: str = "",
                             scope: str, project_id: str = "",
                             manifest_hash: str, cursor_before: str = "",
                             cursor_after: str = "",
                             records: dict[str, list[dict[str, Any]]]) -> int:
        """整页/全量暂存进 inbox；同 snapshot 追加仅限 staged 态。"""
        _validate_scope(scope)
        if not snapshot_id or not isinstance(manifest_hash, str):
            raise SyncStoreError("snapshot_id 非空且 manifest_hash 必须是字符串")
        if not isinstance(records, dict):
            raise SyncStoreError("inbox records 必须是对象")
        unknown_types = set(records) - set(INBOX_RECORD_TYPES)
        if unknown_types:
            raise SyncStoreError(f"inbox 含未知 record type: {', '.join(sorted(unknown_types))}")
        now = _now()
        total = 0
        with self._conn:
            existing = self._conn.execute(
                "SELECT * FROM sync_inbox_snapshot WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if existing and existing["state"] != "staged":
                raise SyncStoreError(
                    f"snapshot {snapshot_id} 已处于 {existing['state']}，不可改写"
                )
            if existing:
                for field, expected in (
                    ("account_id", account_id), ("scope", scope),
                    ("project_id", project_id), ("cursor_before", cursor_before),
                ):
                    if str(existing[field] or "") != str(expected or ""):
                        raise SyncStoreError(
                            f"snapshot {snapshot_id} {field} 不匹配，拒绝跨 scope 改写"
                        )
                if (existing["manifest_hash"] and manifest_hash
                        and existing["manifest_hash"] != manifest_hash):
                    raise SyncStoreError(
                        f"snapshot {snapshot_id} manifest_hash 不匹配，拒绝覆盖"
                    )
                if not existing["manifest_hash"] and manifest_hash:
                    self._conn.execute(
                        "UPDATE sync_inbox_snapshot SET manifest_hash=? WHERE snapshot_id=?",
                        (manifest_hash, snapshot_id),
                    )
            if not existing:
                self._conn.execute(
                    "INSERT INTO sync_inbox_snapshot(snapshot_id,account_id,scope,"
                    "project_id,manifest_hash,cursor_before,cursor_after,state,"
                    "record_count,received_at) VALUES(?,?,?,?,?,?,'','staged',0,?)",
                    (snapshot_id, account_id, scope, project_id, manifest_hash,
                     cursor_before, now),
                )
            for kind, items in records.items():
                if not isinstance(items, list):
                    raise SyncStoreError(f"inbox {kind} 必须是数组")
                for record in items:
                    if not isinstance(record, dict):
                        raise SyncStoreError(f"inbox 记录必须是对象: {kind}")
                    if kind == "tool_experience":
                        tool_name = str(record.get("tool_name") or "")
                        category = str(record.get("category") or "")
                        rid = f"{tool_name}/{category}" if tool_name and category else tool_name
                    elif kind == "consumption_events":
                        rid = str(record.get("event_id") or "")
                    else:
                        rid = str(record.get("incident_id") or record.get("lesson_id")
                                  or record.get("record_id") or "")
                    if not rid:
                        raise SyncStoreError(f"{kind} 记录缺少主键字段")
                    version = record.get("version", record.get("record_version", 0))
                    try:
                        version = _validate_nonnegative_int(version or 0, "record version")
                    except (SyncStoreError, TypeError, ValueError) as exc:
                        raise SyncStoreError(f"{kind}/{rid} version 无效") from exc
                    payload_json = json_compact(record)
                    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                    previous = self._conn.execute(
                        "SELECT payload_hash,remote_version,tombstone "
                        "FROM sync_inbox_record WHERE snapshot_id=? AND record_type=? "
                        "AND record_id=?", (snapshot_id, kind, rid),
                    ).fetchone()
                    if previous:
                        if (previous["payload_hash"] != payload_hash
                                or int(previous["remote_version"]) != version
                                or bool(previous["tombstone"]) != (kind == "tombstones")):
                            # Keep the first immutable payload.  The complete
                            # pull still sees both copies and performs the
                            # version/conflict decision before import; this
                            # prevents INSERT OR REPLACE from hiding a conflict.
                            total += 1
                            continue
                        total += 1
                        continue
                    self._conn.execute(
                        "INSERT INTO sync_inbox_record(snapshot_id,record_type,"
                        "record_id,payload_json,payload_hash,remote_version,tombstone,"
                        "validation_state) VALUES(?,?,?,?,?,?,?,'staged')",
                        (snapshot_id, kind, rid, payload_json, payload_hash,
                         version, 1 if kind == "tombstones" else 0),
                    )
                    total += 1
            count = self._conn.execute(
                "SELECT COUNT(*) FROM sync_inbox_record WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()[0]
            self._conn.execute(
                "UPDATE sync_inbox_snapshot SET record_count=?,cursor_after="
                "CASE WHEN ? <> '' THEN ? ELSE cursor_after END "
                "WHERE snapshot_id=?",
                (count, cursor_after, cursor_after, snapshot_id),
            )
        return total

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sync_inbox_snapshot WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        return dict(row) if row else None

    def inbox_records(self, snapshot_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sync_inbox_record WHERE snapshot_id=? ORDER BY record_type,record_id",
            (snapshot_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def inbox_mark_validated(self, snapshot_id: str) -> None:
        with self._conn:
            snap = self._conn.execute(
                "SELECT state FROM sync_inbox_snapshot WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if not snap:
                raise SyncStoreError(f"snapshot 不存在: {snapshot_id}")
            invalid = self._conn.execute(
                "SELECT record_type,record_id,validation_state FROM sync_inbox_record "
                "WHERE snapshot_id=? AND validation_state NOT IN ('staged','validated')",
                (snapshot_id,),
            ).fetchone()
            if invalid:
                raise SyncStoreError(
                    f"snapshot {snapshot_id} 含未通过校验的记录 "
                    f"{invalid['record_type']}/{invalid['record_id']}="
                    f"{invalid['validation_state']}"
                )
            self._conn.execute(
                "UPDATE sync_inbox_record SET validation_state='validated',reject_code=NULL "
                "WHERE snapshot_id=? AND validation_state='staged'",
                (snapshot_id,),
            )
            changed = self._conn.execute(
                "UPDATE sync_inbox_snapshot SET state='validated',validated_at=? "
                "WHERE snapshot_id=? AND state IN ('staged','validated')",
                (_now(), snapshot_id),
            ).rowcount
        if not changed:
            raise SyncStoreError(f"snapshot {snapshot_id} 不能标记 validated")

    def inbox_mark_rejected(self, snapshot_id: str, reject_code: str) -> None:
        with self._conn:
            changed = self._conn.execute(
                "UPDATE sync_inbox_snapshot SET state='rejected',reject_code=? "
                "WHERE snapshot_id=? AND state IN ('staged','validated')",
                (reject_code, snapshot_id),
            ).rowcount
            if not changed:
                raise SyncStoreError(f"snapshot {snapshot_id} 不能标记 rejected")
            self._conn.execute(
                "UPDATE sync_inbox_record SET validation_state='rejected',"
                "reject_code=COALESCE(reject_code,?) WHERE snapshot_id=?",
                (reject_code, snapshot_id),
            )

    def inbox_commit_import(self, snapshot_id: str, *, account_id: str,
                            scope: str, project_id: str, cursor: str,
                            remote_version: int,
                            record_states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """单一事务：snapshot→imported 且 committed cursor 推进（原子，AD-008-02）。

        Ledger 写入由调用方在进入本事务前完成；本方法保证「cursor 提交」与
        「inbox imported」要么同时发生、要么都不发生。失败抛错时两者均不变。
        """
        remote_version = _validate_nonnegative_int(remote_version, "remote_version")
        now = _now()
        with self._tx():
            snap = self._conn.execute(
                "SELECT * FROM sync_inbox_snapshot WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if not snap:
                raise SyncStoreError(f"snapshot 不存在: {snapshot_id}")
            if snap["state"] != "validated":
                raise SyncStoreError(f"snapshot {snapshot_id} 状态 {snap['state']} 不可导入")
            invalid = self._conn.execute(
                "SELECT record_type,record_id,validation_state FROM sync_inbox_record "
                "WHERE snapshot_id=? AND validation_state != 'validated'",
                (snapshot_id,),
            ).fetchone()
            if invalid:
                raise SyncStoreError(
                    f"snapshot {snapshot_id} 含未校验记录 "
                    f"{invalid['record_type']}/{invalid['record_id']}="
                    f"{invalid['validation_state']}"
                )
            for field, expected in (
                ("account_id", account_id), ("scope", scope),
                ("project_id", project_id),
            ):
                if str(snap[field] or "") != str(expected or ""):
                    raise SyncStoreError(
                        f"snapshot {snapshot_id} {field} 不匹配，拒绝跨 scope 导入"
                    )
            current = self._conn.execute(
                "SELECT committed_cursor FROM sync_scope_state "
                "WHERE account_id=? AND scope=? AND project_id=?",
                (account_id, scope, project_id),
            ).fetchone()
            current_cursor = str(current["committed_cursor"] if current else "")
            current_meta = self._conn.execute(
                "SELECT committed_snapshot_id,remote_version FROM sync_scope_state "
                "WHERE account_id=? AND scope=? AND project_id=?",
                (account_id, scope, project_id),
            ).fetchone()
            if current_meta and remote_version < int(current_meta["remote_version"] or 0) \
                    and (current_cursor != cursor
                         or str(current_meta["committed_snapshot_id"] or "") != snapshot_id):
                raise SyncStoreError(
                    "remote_version 下降且 cursor/snapshot 不同，拒绝回退同步状态"
                )
            snapshot_cursor_before = str(snap["cursor_before"] or "")
            if current_cursor != snapshot_cursor_before:
                raise SyncStoreError(
                    f"snapshot {snapshot_id} cursor_before={snapshot_cursor_before!r} "
                    f"与当前 committed_cursor={current_cursor!r} 不一致"
                )
            snapshot_cursor_after = str(snap["cursor_after"] or "")
            if snapshot_cursor_after and cursor != snapshot_cursor_after:
                raise SyncStoreError(
                    f"snapshot {snapshot_id} cursor={cursor!r} 与快照 cursor_after="
                    f"{snapshot_cursor_after!r} 不一致"
                )
            self._conn.execute(
                "UPDATE sync_inbox_snapshot SET state='imported',imported_at=? "
                "WHERE snapshot_id=?",
                (now, snapshot_id),
            )
            self._conn.execute(
                "UPDATE sync_inbox_record SET validation_state='imported' "
                "WHERE snapshot_id=?",
                (snapshot_id,),
            )
            self._upsert_record_states_tx(
                account_id, scope, project_id, record_states or [], now,
            )
            self._conn.execute(
                "INSERT INTO sync_scope_state(account_id,scope,project_id,"
                "committed_cursor,committed_snapshot_id,remote_version,"
                "last_success_at,protocol_version) VALUES(?,?,?,?,?,?,?,2) "
                "ON CONFLICT(account_id,scope,project_id) DO UPDATE SET "
                "committed_cursor=excluded.committed_cursor,"
                "committed_snapshot_id=excluded.committed_snapshot_id,"
                "remote_version=MAX(sync_scope_state.remote_version,excluded.remote_version),"
                "last_success_at=excluded.last_success_at,last_error_code=NULL",
                (account_id, scope, project_id, cursor, snapshot_id,
                 int(remote_version), now),
            )
        return self.get_scope_state(account_id, scope, project_id) or {}

    # ---- conflict queue ----
    def record_conflict(self, *, account_id: str = "", scope: str,
                        project_id: str = "", record_type: str, record_id: str,
                        local_version: int, remote_version: int,
                        local_hash: str, remote_hash: str,
                        reason: str) -> tuple[str, bool]:
        """确定性 conflict_id 保证同一冲突重复上报只入队一次。"""
        _validate_scope(scope)
        local_version = _validate_nonnegative_int(local_version, "local_version")
        remote_version = _validate_nonnegative_int(remote_version, "remote_version")
        if not record_type or not record_id:
            raise SyncStoreError("冲突记录缺少 record_type/record_id")
        raw = "|".join((account_id, scope, project_id, record_type, record_id,
                        str(local_version), str(remote_version), local_hash, remote_hash))
        conflict_id = "cf-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        try:
            with self._tx():
                # 同一份本地/远端内容在一次启动的 baseline pull 与 final pull
                # 中可能各被观测一次；版本号可能已由第一次观测写入 metadata，
                # 但它们仍是同一个待治理冲突，不能重复堆积。
                same_open = self._conn.execute(
                    "SELECT conflict_id FROM sync_conflict WHERE account_id=? AND scope=? "
                    "AND project_id=? AND record_type=? AND record_id=? "
                    "AND local_hash=? AND remote_hash=? AND state='open' "
                    "ORDER BY created_at LIMIT 1",
                    (account_id, scope, project_id, record_type, record_id,
                     local_hash, remote_hash),
                ).fetchone()
                if same_open:
                    return str(same_open[0]), False
                exists = self._conn.execute(
                    "SELECT 1 FROM sync_conflict WHERE conflict_id=?", (conflict_id,)
                ).fetchone()
                if exists:
                    return conflict_id, False
                self._conn.execute(
                    "INSERT INTO sync_conflict(conflict_id,account_id,scope,project_id,"
                    "record_type,record_id,local_version,remote_version,local_hash,"
                    "remote_hash,reason,state,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                    (conflict_id, account_id, scope, project_id, record_type,
                     record_id, local_version, remote_version,
                     local_hash, remote_hash, reason, _now()),
                )
        except sqlite3.IntegrityError:
            # 并发写方在本事务守卫 SELECT 之后插入了同一 conflict_id（确定性
            # 主键撞车）：整个事务已回滚，这里退化为幂等返回，不堆积重复冲突。
            return conflict_id, False
        return conflict_id, True

    def get_conflict(self, conflict_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sync_conflict WHERE conflict_id=?", (conflict_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_conflicts(self, *, state: str = "open", scope: str | None = None,
                       project_id: str | None = None) -> list[dict[str, Any]]:
        if state not in CONFLICT_STATES:
            raise SyncStoreError(f"非法冲突状态: {state}")
        if scope is not None:
            _validate_scope(scope)
        query = "SELECT * FROM sync_conflict WHERE state=?"
        params: list[Any] = [state]
        if scope is not None:
            query += " AND scope=?"
            params.append(scope)
        if project_id is not None:
            query += " AND project_id=?"
            params.append(project_id)
        query += " ORDER BY created_at"
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def resolve_conflict(self, conflict_id: str, resolution: str, *,
                         resolver_identity: str = "") -> None:
        if resolution not in ("resolved_local", "resolved_remote", "merged"):
            raise SyncStoreError(f"非法冲突解决状态: {resolution}")
        now = _now()
        with self._conn:
            conflict_row = self._conn.execute(
                "SELECT account_id,scope,project_id,record_type,record_id,remote_version,remote_hash "
                "FROM sync_conflict WHERE conflict_id=? AND state='open'",
                (conflict_id,),
            ).fetchone()
            if not conflict_row:
                raise SyncStoreError(f"冲突 {conflict_id} 不存在或已解决")
            changed = self._conn.execute(
                "UPDATE sync_conflict SET state=?,resolved_at=?,resolver_identity=? "
                "WHERE conflict_id=? AND state='open'",
                (resolution, now, resolver_identity, conflict_id),
            ).rowcount
            if not changed:
                raise SyncStoreError(f"冲突 {conflict_id} 不存在或已解决")
            if resolution == "resolved_remote":
                self._upsert_record_states_tx(
                    conflict_row["account_id"], conflict_row["scope"], conflict_row["project_id"],
                    [{
                        "record_type": conflict_row["record_type"],
                        "record_id": conflict_row["record_id"],
                        "remote_version": conflict_row["remote_version"],
                        "remote_hash": conflict_row["remote_hash"],
                    }],
                    now,
                )
            elif resolution == "resolved_local":
                # 本地权威但**不发明版本**：清除该记录的远端版本元数据，
                # 让下一轮正常 push 以变更内容走服务器分配新版本
                # （Round 7 审计 C：与 adopt_import_snapshot 同语义）。
                for alias in _record_type_aliases(conflict_row["record_type"]):
                    self._conn.execute(
                        "DELETE FROM sync_record_state "
                        "WHERE account_id=? AND scope=? AND project_id=? "
                        "AND record_type=? AND record_id=?",
                        (conflict_row["account_id"], conflict_row["scope"],
                         conflict_row["project_id"], alias, conflict_row["record_id"]),
                    )

    def resolve_remote_conflicts(self, account_id: str, scope: str,
                                 project_id: str,
                                 records: list[dict[str, Any]], *,
                                 resolver_identity: str = "sync") -> int:
        """关闭已被权威远端快照收敛的冲突。

        这里只处理调用方已经确认导入/保留的记录。版本冲突以观测到的
        ``remote_version`` 作为下界；内容冲突还必须拥有完全相同的非空
        ``remote_hash``。因此一次普通 pull 不会因为“版本变大”而误关闭
        仍然存在的内容分歧。

        record_type 同时接受新旧单复数写法，便于升级期间收敛历史队列。
        """
        _validate_scope(scope)
        if not records:
            return 0
        aliases = {
            "pattern": ("pattern", "patterns"),
            "patterns": ("pattern", "patterns"),
            "incident": ("incident", "incidents"),
            "incidents": ("incident", "incidents"),
            "tool": ("tool", "tool_experience"),
            "tool_experience": ("tool", "tool_experience"),
        }
        changed_total = 0
        now = _now()
        with self._conn:
            for item in records:
                record_type = str(item.get("record_type") or "")
                record_id = str(item.get("record_id") or "")
                if not record_type or not record_id:
                    continue
                try:
                    remote_version = _validate_nonnegative_int(
                        int(item.get("remote_version") or 0), "remote_version",
                    )
                except (TypeError, ValueError, SyncStoreError):
                    continue
                remote_hash = str(item.get("remote_hash") or "")
                if remote_version <= 0 and not remote_hash:
                    continue
                record_types = aliases.get(record_type, (record_type,))
                placeholders = ",".join("?" for _ in record_types)
                params: list[Any] = [
                    now, resolver_identity, account_id, scope, project_id,
                    record_id, *record_types, remote_version,
                ]
                hash_clause = ""
                if remote_hash:
                    hash_clause = " OR (remote_hash <> '' AND remote_hash = ?)"
                    params.append(remote_hash)
                changed_total += self._conn.execute(
                    "UPDATE sync_conflict SET state='resolved_remote',"
                    "resolved_at=?,resolver_identity=? "
                    "WHERE account_id=? AND scope=? AND project_id=? "
                    "AND record_id=? AND record_type IN (" + placeholders + ") "
                    "AND state='open' AND remote_version <= ? AND ("
                    "reason='stale_version'" + hash_clause + ")",
                    params,
                ).rowcount
        return changed_total

    def prune_converged_conflicts(self, account_id: str, scope: str,
                                  project_id: str = "", *,
                                  resolver_identity: str = "sync-prune") -> int:
        """自动修剪和关闭已收敛的历史冲突。

        比对当前 sync_record_state 中的最新状态：
        1. 若冲突为 stale_version，且当前记录的 remote_version >= conflict.remote_version；
        2. 或冲突的 local_hash == remote_hash（零内容分歧的版本漂移冲突）；
        3. 或当前记录的 remote_hash == conflict.local_hash（本地版本已被远端吸收）；
        自动将其标记为 resolved_remote。
        """
        _validate_scope(scope)
        now = _now()
        with self._conn:
            open_conflicts = self._conn.execute(
                "SELECT conflict_id, record_type, record_id, local_version, remote_version, "
                "local_hash, remote_hash, reason FROM sync_conflict "
                "WHERE account_id=? AND scope=? AND project_id=? AND state='open'",
                (account_id, scope, project_id),
            ).fetchall()
            if not open_conflicts:
                return 0

            states = self.get_record_states(account_id, scope, project_id)
            pruned = 0
            aliases = {
                "pattern": ("pattern", "patterns"),
                "patterns": ("pattern", "patterns"),
                "incident": ("incident", "incidents"),
                "incidents": ("incident", "incidents"),
                "tool": ("tool", "tool_experience"),
                "tool_experience": ("tool", "tool_experience"),
            }
            for conf in open_conflicts:
                cid = conf["conflict_id"]
                rtype = conf["record_type"]
                rid = conf["record_id"]
                c_lhash = conf["local_hash"] or ""
                c_rhash = conf["remote_hash"] or ""
                c_rver = int(conf["remote_version"] or 0)
                reason = conf["reason"] or ""

                if c_lhash and c_rhash and c_lhash == c_rhash:
                    self._conn.execute(
                        "UPDATE sync_conflict SET state='resolved_remote', resolved_at=?, resolver_identity=? "
                        "WHERE conflict_id=? AND state='open'",
                        (now, resolver_identity, cid),
                    )
                    pruned += 1
                    continue

                curr_state = None
                for alias in aliases.get(rtype, (rtype,)):
                    if (alias, rid) in states:
                        curr_state = states[(alias, rid)]
                        break
                if curr_state:
                    curr_rver = int(curr_state.get("remote_version") or 0)
                    curr_rhash = curr_state.get("remote_hash") or ""
                    if (reason == "stale_version" and curr_rver >= c_rver) or (c_lhash and curr_rhash == c_lhash):
                        self._conn.execute(
                            "UPDATE sync_conflict SET state='resolved_remote', resolved_at=?, resolver_identity=? "
                            "WHERE conflict_id=? AND state='open'",
                            (now, resolver_identity, cid),
                        )
                        pruned += 1
            return pruned



# ---- 小工具（避免循环依赖 cloud_schema）----
def json_canonical(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_compact(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
