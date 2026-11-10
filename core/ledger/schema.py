#!/usr/bin/env python3
"""core/ledger/schema.py - 错题集 SQLite DDL、Schema 迁移与连接事务基础设施 (DOC-072)。"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

SCHEMA_VERSION = 14
DEFAULT_LEDGER = Path(".bugx/knowledge/lesson-ledger.json")


def resolve_default_ledger(db: Path) -> Path:
    """DOC-212 设计项 3：把 DEFAULT_LEDGER 相对哨兵解析为项目根锚定的绝对路径。

    R9 events=0 根因：该值是 CWD 相对路径，宿主在不同子目录操作时事件写进
    不同 db（路径分离）。解析顺序：
    ① 非哨兵路径（绝对路径/用户显式相对路径）原样返回——``--ledger`` 显式
       传参行为完全不变；
    ② ``BUGX_PROJECT_ROOT`` env 优先（与 bugx_paths.project_root 同语义）；
    ③ 从 CWD 向上找最近的项目根标记（``.git`` 目录**或文件**——worktree 是
       文件 / ``pyproject.toml``），锚定标记所在目录的账本路径（允许尚不
       存在，create 落在项目根而非子目录——这正是要修的分裂）；
    ④ 无任何标记 → 回退 CWD 相对（旧行为）。
    注意：哨兵值相等即锚定（Path 值比较），tools_kb/pattern_probe/
    fix_watchlist/regression_pack 四处同值常量与全部默认参数路径经 ``connect_ledger``
    咽喉统一覆盖。惰性调用期解析，禁止 import 期锚定。
    """
    db = Path(db)
    if db != DEFAULT_LEDGER:
        return db
    env_root = os.environ.get("BUGX_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve() / ".bugx" / "knowledge" / "lesson-ledger.json"
    cur = Path.cwd().resolve()
    for p in (cur, *cur.parents):
        if (p / ".git").exists() or (p / "pyproject.toml").is_file():
            return p / ".bugx" / "knowledge" / "lesson-ledger.json"
    return db


DEFAULT_LIMIT = 6
DEFAULT_TOKEN_BUDGET = 1500
DEFAULT_BYTE_BUDGET = 12000
MAX_EVENTS = 100000
MAX_TEXT_LENGTH = 8192
# DOC-SCAN-BOUNDARY：秘钥形 token 必须是**词边界独立**的——标识符指纹
# （如 "task-kwargs-typeerror-…"）内嵌的 "sk-" 子串不是秘钥。加负向后顾
# 断言排除紧贴标识符字符的假阳性（实测：finding fingerprint
# "models.load_tasks:task-kwargs-typeerror-schema-drift" 误触，cycle-review
# 无法落账）。
SECRET_REGEX = re.compile(
    r"(?<![A-Za-z0-9_-])(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~+/=-]{20,})",
    re.I,
)
MAX_LIST_ITEMS = 64
PATTERN_STATUSES = {"candidate", "active", "superseded", "retired", "revoked"}
LESSON_STATUSES = PATTERN_STATUSES
OUTCOMES = {"hit", "miss", "false_positive", "rejected"}
PATTERN_OUTCOMES = OUTCOMES | {"not_applicable"}
TOOL_EXPERIENCE_LEVELS = {"unproven", "candidate", "active"}
TOOL_EXPERIENCE_FIELDS = (
    "tool_name", "category", "experience_level",
    "use_count", "hit_count", "miss_count",
    "false_positive_count", "rejected_count",
    "independent_lifecycle_count",
    "first_observed_at", "last_observed_at",
    "tool_version", "record_version", "device_id",
    "deleted", "deleted_at",
)
TOOL_EXPERIENCE_FORBIDDEN_KEYS = frozenset({
    "run_id", "lifecycle_id", "lifecycle_ids", "finding", "findings",
    "command", "arguments", "file_path", "paths", "user_input",
    "signature", "summary", "context", "snippet",
})
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,127}$")
MAX_TOOLS_PER_FEEDBACK = 8
TOOL_LOOKUP_LIMIT = 4
TOOL_LOOKUP_TOKEN_BUDGET = 800
TOOL_LOOKUP_BYTE_BUDGET = 6000
AUDITED_TABLES = (
    "patterns", "probes", "aliases", "sources", "occurrences", "reviews",
    "review_patterns", "review_findings", "feedback", "incidents",
    "tool_usage", "evidence_receipts", "consumption_events",
    "tool_experience", "sync_import_receipts",
)
FTS_COLUMNS = ("pattern_id", "category", "title", "root_cause", "reflection", "probes", "aliases")
ID_REGEX = re.compile(r"^L-[0-9a-f]{12}$")
INCIDENT_ID_REGEX = re.compile(r"^I-[0-9a-f]{12}$")
FP_REGEX = re.compile(r"^(?:sha256:)?[A-Za-z0-9_.:/-]{8,256}$")
INCIDENT_STATUSES = {"candidate", "active", "retired", "revoked"}
CARD_FIELDS = (
    "severity", "language", "cwe_id", "error_signature",
    "before_code", "after_code", "fix_summary", "repro_oracle",
)
CARD_OPTIONAL_FIELDS = (
    "playbook", "attempts_tried", "successful_solution",
    "applicability", "false_positive_patterns", "oracle_hints", "tags",
    "aliases", "negative_controls", "evidence_requirements",
)
ROUTING_TAG_KEYS = (
    "forms", "languages", "runtimes", "frameworks", "surfaces", "targets",
    "check_profiles",
)
CARD_LIMITS = {
    "severity": 20, "language": 50, "cwe_id": 20, "error_signature": 500,
    "before_code": 65536, "after_code": 65536,
    "fix_summary": 2000, "repro_oracle": 2000,
}


SCHEMA = """
CREATE TABLE patterns(pattern_id TEXT PRIMARY KEY,category TEXT,title TEXT,root_cause TEXT,
 reflection TEXT,status TEXT,superseded_by TEXT,created_at TEXT,updated_at TEXT,last_used_at TEXT,
 use_count INTEGER,hit_count INTEGER,miss_count INTEGER,false_positive_count INTEGER,rejected_count INTEGER);
CREATE TABLE probes(pattern_id TEXT,kind TEXT,value TEXT,ordinal INTEGER,PRIMARY KEY(pattern_id,kind,value));
CREATE TABLE aliases(pattern_id TEXT,alias TEXT,PRIMARY KEY(pattern_id,alias));
CREATE TABLE sources(pattern_id TEXT,source_ref TEXT,PRIMARY KEY(pattern_id,source_ref));
CREATE TABLE occurrences(pattern_id TEXT,fingerprint TEXT,observed_at TEXT,PRIMARY KEY(pattern_id,fingerprint));
-- R6 复盘 S4：三张复盘表主键补 polling_id 维度。项目级账本跨 polling
-- 重用 lifecycle 编号（都是 lifecycle-000001/round=1），旧主键会让第二个
-- polling 静默覆盖第一个 polling 的复盘事实；polling_id='' 表示遗留行。
CREATE TABLE reviews(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,finding_count INTEGER,credited_count INTEGER,reason TEXT,created_at TEXT,PRIMARY KEY(polling_id,lifecycle_id,round));
CREATE TABLE review_patterns(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,pattern_id TEXT,relation TEXT,PRIMARY KEY(polling_id,lifecycle_id,round,pattern_id));
CREATE TABLE review_findings(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,fingerprint TEXT,PRIMARY KEY(polling_id,lifecycle_id,round,fingerprint));
CREATE TABLE feedback(lifecycle_id TEXT,round INTEGER,pattern_id TEXT,fingerprint TEXT,outcome TEXT,created_at TEXT,signature TEXT,PRIMARY KEY(lifecycle_id,round,pattern_id,fingerprint));
CREATE TABLE incidents(incident_id TEXT PRIMARY KEY,pattern_id TEXT,signature TEXT NOT NULL,summary TEXT NOT NULL,
 context TEXT NOT NULL,before_snippet TEXT NOT NULL,after_snippet TEXT NOT NULL,diff_ref TEXT,commit_ref TEXT,
 decision TEXT NOT NULL,duration_seconds REAL NOT NULL,lifecycle_id TEXT,round INTEGER,status TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,content_hash TEXT NOT NULL,
 routing_tags TEXT NOT NULL DEFAULT '{}',finding_fingerprint TEXT);
CREATE INDEX idx_incidents_finding_fingerprint ON incidents(finding_fingerprint);
CREATE TABLE tool_usage(lifecycle_id TEXT NOT NULL,round INTEGER NOT NULL,tool_name TEXT NOT NULL,category TEXT NOT NULL,
 outcome TEXT NOT NULL,finding_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(lifecycle_id,round,tool_name,category,finding_fingerprint));
CREATE TABLE evidence_receipts(receipt_id TEXT PRIMARY KEY,subject_type TEXT NOT NULL,subject_id TEXT NOT NULL,
 run_id TEXT NOT NULL,lifecycle_id TEXT NOT NULL,round INTEGER NOT NULL,artifact_hash TEXT NOT NULL,
 created_at TEXT NOT NULL,content_hash TEXT NOT NULL);
CREATE TABLE consumption_events(event_id TEXT PRIMARY KEY,subject_type TEXT NOT NULL,subject_id TEXT NOT NULL,
 event_type TEXT NOT NULL,lifecycle_id TEXT,run_id TEXT,created_at TEXT NOT NULL,content_hash TEXT NOT NULL);
CREATE TABLE events(seq INTEGER PRIMARY KEY,type TEXT,at TEXT,payload TEXT,prev_hash TEXT,hash TEXT);
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE state_table_digests(table_name TEXT PRIMARY KEY,row_digest TEXT NOT NULL,row_count INTEGER NOT NULL,snapshot_json TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE tool_experience(tool_name TEXT NOT NULL,category TEXT NOT NULL,experience_level TEXT NOT NULL,
 record_version INTEGER NOT NULL,use_count INTEGER NOT NULL,hit_count INTEGER NOT NULL,miss_count INTEGER NOT NULL,
 false_positive_count INTEGER NOT NULL,rejected_count INTEGER NOT NULL,independent_lifecycle_count INTEGER NOT NULL,
 first_observed_at TEXT,last_observed_at TEXT,tool_version TEXT,device_id TEXT,
 deleted INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,origin TEXT NOT NULL,content_hash TEXT NOT NULL,
 PRIMARY KEY(tool_name,category));
CREATE TABLE sync_import_receipts(snapshot_id TEXT PRIMARY KEY,manifest_hash TEXT NOT NULL,
 content_hash TEXT NOT NULL,record_count INTEGER NOT NULL,created_at TEXT NOT NULL,
 committed_at TEXT NOT NULL);
CREATE TABLE project_profile(project_key TEXT PRIMARY KEY,schema_version INTEGER NOT NULL,
 profile_json TEXT NOT NULL,classification_json TEXT NOT NULL,profile_hash TEXT NOT NULL,
 source_fingerprint TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,
 updated_at TEXT NOT NULL,producer_version TEXT NOT NULL);
"""


class LessonLedgerError(ValueError):
    pass


class ManagedConnection(sqlite3.Connection):
    """sqlite3's context manager commits but does not close the connection."""

    def __enter__(self):
        super().__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            try:
                self.close()
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sanitize_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LessonLedgerError(f"{field} 必须是非空字符串")
    clean = " ".join(value.split())
    if len(clean) > MAX_TEXT_LENGTH:
        raise LessonLedgerError(f"{field} 超过最大长度 {MAX_TEXT_LENGTH}")
    if SECRET_REGEX.search(clean):
        raise LessonLedgerError(f"{field} 包含疑似凭据/秘钥，已拒绝写入")
    return clean


def sanitize_text_long(value: Any, field: str, max_len: int = 65536) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LessonLedgerError(f"{field} 必须是非空字符串")
    clean = " ".join(value.split())
    if len(clean) > max_len:
        raise LessonLedgerError(f"{field} 超过最大长度 {max_len}")
    if SECRET_REGEX.search(clean):
        raise LessonLedgerError(f"{field} 包含疑似凭据/秘钥，已拒绝写入")
    return clean


def parse_json_list(value: Any, field: str, required: bool = True) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise LessonLedgerError(f"{field} 必须是非空字符串数组")
    if len(value) > MAX_LIST_ITEMS:
        raise LessonLedgerError(f"{field} 项数超过 {MAX_LIST_ITEMS}")
    result = [sanitize_text(x, field) for x in value]
    if required and not result:
        raise LessonLedgerError(f"{field} 不能为空")
    if len(result) != len(set(result)):
        raise LessonLedgerError(f"{field} 不得重复")
    return result



def sanitize_text_opt(value: Any, field: str, max_len: int = 2000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LessonLedgerError(f"{field} 必须是字符串")
    clean = " ".join(value.split())
    if len(clean) > max_len:
        raise LessonLedgerError(f"{field} 超过最大长度 {max_len}")
    if SECRET_REGEX.search(clean):
        raise LessonLedgerError(f"{field} 包含疑似凭据/秘钥，已拒绝写入")
    return clean


def normalize_id(value: str) -> str:
    return " ".join(value.strip().lower().split())


def parse_not_after(stamp: str, ceiling: str) -> bool:
    try:
        ts = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        tc = dt.datetime.fromisoformat(ceiling.replace("Z", "+00:00"))
        return ts <= tc
    except Exception:
        return stamp <= ceiling


def database_path(path: str | os.PathLike[str] = DEFAULT_LEDGER) -> Path:
    # DOC-212：派生前先锚定——哨兵的 .sqlite3 派生值不等于 JSON 哨兵本体，
    # 不在此解析则 ledger_transaction/read/migrate 等库内调用方全部绕过锚定。
    p = resolve_default_ledger(Path(path))
    return p.with_suffix(".sqlite3") if p.suffix == ".json" else p


def seed_path(path: str | os.PathLike[str] = DEFAULT_LEDGER) -> Path:
    p = resolve_default_ledger(Path(path))
    return p.with_suffix(".json") if p.suffix == ".sqlite3" else p


def is_regular_file(path: Path, label: str, required: bool = True) -> None:
    if not path.exists():
        if required:
            raise LessonLedgerError(f"{label} 不存在: {path}")
        return
    if not path.is_file() or path.is_symlink():
        raise LessonLedgerError(f"{label} 必须是普通文件，拒绝符号链接与目录: {path}")


def assert_no_symlink_components(path: Path) -> None:
    from core.bugx_paths import assert_no_symlink_components
    assert_no_symlink_components(path, error_cls=LessonLedgerError)


def connect_ledger(db: Path, create: bool = False, readonly: bool = False,
          enable_wal: bool = True) -> sqlite3.Connection:
    # DOC-212：单一咽喉——默认账本哨兵在打开前锚定到项目根；symlink 词法
    # 检查锚定后的最终路径，安全链不变。
    db = resolve_default_ledger(db)
    assert_no_symlink_components(db)
    if not create:
        is_regular_file(db, "SQLite 运行库")
    elif db.is_symlink():
        raise LessonLedgerError(f"拒绝覆盖符号链接 SQLite 运行库: {db}")

    def _open(query_only: bool, wal: bool) -> sqlite3.Connection:
        if query_only:
            uri = f"file:{quote(str(db.resolve()), safe='/')}?mode=ro"
            c = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None,
                                factory=ManagedConnection)
        else:
            c = sqlite3.connect(str(db), timeout=10, isolation_level=None,
                                factory=ManagedConnection)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=10000")
            if query_only:
                c.execute("PRAGMA query_only=ON")
            elif wal:
                c.execute("PRAGMA journal_mode=WAL")
            return c
        except sqlite3.Error:
            c.close()
            raise

    if readonly:
        try:
            return _open(query_only=True, wal=False)
        except sqlite3.Error:
            try:
                return _open(query_only=False, wal=False)
            except sqlite3.Error as exc:
                raise LessonLedgerError(f"SQLite 无法打开: {exc}") from exc
    if not create and enable_wal:
        return _open_with_wal_mode(
            db, lambda: _open(query_only=False, wal=False),
        )
    try:
        return _open(query_only=False, wal=False)
    except sqlite3.Error as exc:
        raise LessonLedgerError(f"SQLite 无法打开: {exc}") from exc


_JOURNAL_MODE_THREAD_LOCK = threading.Lock()
_WAL_MODE_READY_GUARD = threading.Lock()
_WAL_MODE_READY: set[str] = set()


@contextmanager
def _journal_mode_lock(db: Path) -> Iterator[None]:
    """Serialize SQLite journal-mode transitions across threads and processes."""
    canonical_db = db.resolve(strict=False)
    lock_path = canonical_db.with_name(canonical_db.name + ".journal-mode.lock")
    assert_no_symlink_components(lock_path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise LessonLedgerError("SQLite journal-mode 锁无法安全打开") from exc

    acquired = False
    thread_acquired = False
    try:
        if not _JOURNAL_MODE_THREAD_LOCK.acquire(timeout=10.0):
            raise LessonLedgerError("SQLite journal-mode 线程锁等待超过 10 秒")
        thread_acquired = True
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LessonLedgerError("SQLite journal-mode 锁必须是普通文件")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + 10.0
        if _fcntl is not None:
            while True:
                try:
                    _fcntl.flock(
                        descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB,
                    )
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LessonLedgerError(
                            "SQLite journal-mode 锁等待超过 10 秒"
                        )
                    time.sleep(0.05)
        elif _msvcrt is not None:  # pragma: no cover - Windows
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    _msvcrt.locking(descriptor, _msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {
                        errno.EACCES, errno.EAGAIN, errno.EDEADLK,
                        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
                    }:
                        raise LessonLedgerError(
                            "SQLite journal-mode 锁获取失败"
                        ) from exc
                    if time.monotonic() >= deadline:
                        raise LessonLedgerError(
                            "SQLite journal-mode 锁等待超过 10 秒"
                        ) from exc
                    time.sleep(0.05)
        else:  # pragma: no cover - unsupported platform
            raise LessonLedgerError(
                "当前平台没有可用的 SQLite journal-mode 文件锁"
            )
        yield
    finally:
        try:
            if acquired:
                if _fcntl is not None:
                    _fcntl.flock(descriptor, _fcntl.LOCK_UN)
                elif _msvcrt is not None:  # pragma: no cover - Windows
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
        finally:
            try:
                os.close(descriptor)
            finally:
                if thread_acquired:
                    _JOURNAL_MODE_THREAD_LOCK.release()


def _ensure_wal_mode(connection: sqlite3.Connection) -> bool:
    """Ensure WAL while the caller holds the per-ledger lock before opening SQLite."""
    row = connection.execute("PRAGMA journal_mode").fetchone()
    if row is not None and str(row[0]).lower() == "wal":
        return True
    # PRAGMA returns the selected mode as a row; fetch it so the statement and
    # any SQLite lock are finalized before this connection is returned.
    row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    return row is not None and str(row[0]).lower() == "wal"


def _wal_mode_cache_key(db: Path) -> str:
    return str(db.resolve(strict=False))


def _open_with_wal_mode(
    db: Path, opener: Callable[[], sqlite3.Connection],
) -> sqlite3.Connection:
    """Open a writable connection only after first-use WAL setup is serialized."""
    key = _wal_mode_cache_key(db)
    with _WAL_MODE_READY_GUARD:
        if key in _WAL_MODE_READY:
            return opener()
        with _journal_mode_lock(db):
            if key in _WAL_MODE_READY:
                return opener()
            connection = opener()
            try:
                if _ensure_wal_mode(connection):
                    _WAL_MODE_READY.add(key)
            except BaseException:
                connection.close()
                raise
            return connection



def read_seed(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    from .patterns import normalize_pattern
    assert_no_symlink_components(path)
    is_regular_file(path, "JSON 种子")
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise LessonLedgerError(f"JSON 种子无法解析: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") not in {2, 3}:
        raise LessonLedgerError("JSON 种子 schema_version 不受支持")
    lessons = data.get("lessons")
    if not isinstance(lessons, dict) or not lessons:
        raise LessonLedgerError("JSON 种子 lessons 不能为空")
    result = {}
    for key, value in lessons.items():
        item = normalize_pattern(value)
        if item["pattern_id"] != key:
            raise LessonLedgerError("JSON 种子 key 与 pattern_id 不一致")
        result[key] = item
    return result, hashlib.sha256(raw).hexdigest()


def init_fts(c: sqlite3.Connection) -> bool:
    try:
        c.execute("CREATE VIRTUAL TABLE patterns_fts USING fts5(pattern_id UNINDEXED,category,title,root_cause,reflection,probes,aliases)")
        return True
    except sqlite3.OperationalError:
        c.execute("CREATE TABLE patterns_fts(pattern_id TEXT PRIMARY KEY,category TEXT,title TEXT,root_cause TEXT,reflection TEXT,probes TEXT,aliases TEXT)")
        return False


def execute_script(c: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        text = statement.strip()
        if text:
            c.execute(text)


_REVIEW_TABLE_POLLING_SPECS = (
    (
        "reviews",
        "CREATE TABLE {staged}(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,"
        "finding_count INTEGER,credited_count INTEGER,reason TEXT,created_at TEXT,"
        "PRIMARY KEY(polling_id,lifecycle_id,round))",
        "INSERT INTO {staged}(polling_id,lifecycle_id,round,finding_count,credited_count,reason,created_at) "
        "SELECT '',lifecycle_id,round,finding_count,credited_count,reason,created_at FROM {table}",
        ("polling_id", "lifecycle_id", "round", "finding_count", "credited_count", "reason", "created_at"),
    ),
    (
        "review_patterns",
        "CREATE TABLE {staged}(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,"
        "pattern_id TEXT,relation TEXT,PRIMARY KEY(polling_id,lifecycle_id,round,pattern_id))",
        "INSERT INTO {staged}(polling_id,lifecycle_id,round,pattern_id,relation) "
        "SELECT '',lifecycle_id,round,pattern_id,relation FROM {table}",
        ("polling_id", "lifecycle_id", "round", "pattern_id", "relation"),
    ),
    (
        "review_findings",
        "CREATE TABLE {staged}(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,"
        "fingerprint TEXT,PRIMARY KEY(polling_id,lifecycle_id,round,fingerprint))",
        "INSERT INTO {staged}(polling_id,lifecycle_id,round,fingerprint) "
        "SELECT '',lifecycle_id,round,fingerprint FROM {table}",
        ("polling_id", "lifecycle_id", "round", "fingerprint"),
    ),
)


def _rebuild_review_tables_with_polling(c: sqlite3.Connection) -> None:
    """R6 复盘 S4（v12→v13）：复盘三表主键补 polling_id 维度。

    项目级账本跨 polling 重用 lifecycle 编号，旧主键 (lifecycle_id, round)
    会让第二个 polling 静默覆盖第一个 polling 的复盘事实。旧表在**当前事务
    内**staged 重建（SQLite 原子 DDL + 事务回滚保证 crash-safe），遗留行
    polling_id 置 ''。表不存在（极早期库）或缺 polling_id 列之外的任何
    结构差异照旧交给调用方的完整性校验兜底。
    """
    for table, create_sql, copy_sql, _columns in _REVIEW_TABLE_POLLING_SPECS:
        exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        columns = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
        if "polling_id" in columns:
            continue
        staged = f"{table}__polling_migrate"
        c.execute(f"DROP TABLE IF EXISTS {staged}")
        c.execute(create_sql.format(staged=staged))
        c.execute(copy_sql.format(staged=staged, table=table))
        c.execute(f"DROP TABLE {table}")
        c.execute(f"ALTER TABLE {staged} RENAME TO {table}")


def ensure_schema(c: sqlite3.Connection, *, migrate: bool = False) -> None:
    from .incidents import incident_content_hash
    row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise LessonLedgerError("SQLite schema_version 缺失")
    version = int(row[0])
    if version == SCHEMA_VERSION:
        return
    if not migrate or version not in {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}:
        raise LessonLedgerError(f"SQLite schema_version={version}，请显式执行 lesson migrate")
    c.execute("BEGIN IMMEDIATE")
    try:
        if version == 13:
            columns = {row[1] for row in c.execute("PRAGMA table_info(incidents)")}
            if "finding_fingerprint" not in columns:
                c.execute("ALTER TABLE incidents ADD COLUMN finding_fingerprint TEXT")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_incidents_finding_fingerprint "
                "ON incidents(finding_fingerprint)"
            )
            c.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            record_event(c, "schema-migrated", {"from": version, "to": SCHEMA_VERSION})
            _sync_table_digests(c)
            c.execute("COMMIT")
            return
        if version == 2:
            execute_script(c, """
            CREATE TABLE IF NOT EXISTS reviews(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,finding_count INTEGER,credited_count INTEGER,reason TEXT,created_at TEXT,PRIMARY KEY(polling_id,lifecycle_id,round));
            CREATE TABLE IF NOT EXISTS review_patterns(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,pattern_id TEXT,relation TEXT,PRIMARY KEY(polling_id,lifecycle_id,round,pattern_id));
            CREATE TABLE IF NOT EXISTS review_findings(polling_id TEXT NOT NULL DEFAULT '',lifecycle_id TEXT,round INTEGER,fingerprint TEXT,PRIMARY KEY(polling_id,lifecycle_id,round,fingerprint));
            CREATE TABLE IF NOT EXISTS feedback(lifecycle_id TEXT,round INTEGER,pattern_id TEXT,fingerprint TEXT,outcome TEXT,created_at TEXT,PRIMARY KEY(lifecycle_id,round,pattern_id,fingerprint));
            CREATE TABLE IF NOT EXISTS patterns_fts(pattern_id TEXT PRIMARY KEY,category TEXT,title TEXT,root_cause TEXT,reflection TEXT,probes TEXT,aliases TEXT);
            """)
        if version == 9:
            pass
        _rebuild_review_tables_with_polling(c)
        execute_script(c, """
        CREATE TABLE IF NOT EXISTS incidents(incident_id TEXT PRIMARY KEY,pattern_id TEXT,signature TEXT NOT NULL,summary TEXT NOT NULL,
         context TEXT NOT NULL,before_snippet TEXT NOT NULL,after_snippet TEXT NOT NULL,diff_ref TEXT,commit_ref TEXT,
         decision TEXT NOT NULL,duration_seconds REAL NOT NULL,lifecycle_id TEXT,round INTEGER,status TEXT NOT NULL,
         created_at TEXT NOT NULL,updated_at TEXT NOT NULL,content_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tool_usage(lifecycle_id TEXT NOT NULL,round INTEGER NOT NULL,tool_name TEXT NOT NULL,category TEXT NOT NULL,
         outcome TEXT NOT NULL,finding_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,
         PRIMARY KEY(lifecycle_id,round,tool_name,category,finding_fingerprint));
        CREATE TABLE IF NOT EXISTS evidence_receipts(receipt_id TEXT PRIMARY KEY,subject_type TEXT NOT NULL,subject_id TEXT NOT NULL,
         run_id TEXT NOT NULL,lifecycle_id TEXT NOT NULL,round INTEGER NOT NULL,artifact_hash TEXT NOT NULL,
         created_at TEXT NOT NULL,content_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS consumption_events(event_id TEXT PRIMARY KEY,subject_type TEXT NOT NULL,subject_id TEXT NOT NULL,
         event_type TEXT NOT NULL,lifecycle_id TEXT,run_id TEXT,created_at TEXT NOT NULL,content_hash TEXT NOT NULL);
        """)
        execute_script(c, """
        CREATE TABLE IF NOT EXISTS tool_experience(tool_name TEXT NOT NULL,category TEXT NOT NULL,experience_level TEXT NOT NULL,
         record_version INTEGER NOT NULL,use_count INTEGER NOT NULL,hit_count INTEGER NOT NULL,miss_count INTEGER NOT NULL,
         false_positive_count INTEGER NOT NULL,rejected_count INTEGER NOT NULL,independent_lifecycle_count INTEGER NOT NULL,
         first_observed_at TEXT,last_observed_at TEXT,tool_version TEXT,device_id TEXT,
         deleted INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,origin TEXT NOT NULL,content_hash TEXT NOT NULL,
         PRIMARY KEY(tool_name,category));
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sync_import_receipts(
                snapshot_id TEXT PRIMARY KEY,
                manifest_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                committed_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS project_profile(
                project_key TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                profile_json TEXT NOT NULL,
                classification_json TEXT NOT NULL,
                profile_hash TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                producer_version TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS state_table_digests(
                table_name TEXT PRIMARY KEY,
                row_digest TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )
        """)
        digest_columns = {row[1] for row in c.execute("PRAGMA table_info(state_table_digests)")}
        if "snapshot_json" not in digest_columns:
            c.execute("ALTER TABLE state_table_digests ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '[]'")
        columns = {row[1] for row in c.execute("PRAGMA table_info(feedback)")}
        if "signature" not in columns:
            c.execute("ALTER TABLE feedback ADD COLUMN signature TEXT")
        incident_columns = {row[1] for row in c.execute("PRAGMA table_info(incidents)")}
        if "routing_tags" not in incident_columns:
            c.execute("ALTER TABLE incidents ADD COLUMN routing_tags TEXT NOT NULL DEFAULT '{}'")
            for row in c.execute(
                "SELECT incident_id,pattern_id,signature,summary,context,before_snippet,after_snippet,diff_ref,commit_ref,decision,duration_seconds,lifecycle_id,round FROM incidents"
            ).fetchall():
                content = {
                    "signature": row[2], "summary": row[3], "context": row[4],
                    "before_snippet": row[5], "after_snippet": row[6],
                    "diff_ref": row[7], "commit_ref": row[8], "decision": row[9],
                    "duration_seconds": row[10], "lifecycle_id": row[11],
                    "round": row[12], "pattern_id": row[1], "routing_tags": {},
                }
                c.execute(
                    "UPDATE incidents SET content_hash=? WHERE incident_id=?",
                    (incident_content_hash(content), row[0]),
                )
        incident_columns = {row[1] for row in c.execute("PRAGMA table_info(incidents)")}
        if "finding_fingerprint" not in incident_columns:
            c.execute("ALTER TABLE incidents ADD COLUMN finding_fingerprint TEXT")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_incidents_finding_fingerprint "
            "ON incidents(finding_fingerprint)"
        )
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('fts5','0')")
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('state_hash','')")
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('head_hash','')")
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('archive_manifest','')")
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('archive_root_hash','')")
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('archive_last_seq','')")
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('archive_last_hash','')")
        c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('archive_dir','')")
        for key in [r[0] for r in c.execute("SELECT pattern_id FROM patterns")]:
            refresh_materialized_views(c, key)
        record_event(c, "schema-migrated", {"from": version, "to": SCHEMA_VERSION})
        _sync_table_digests(c)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def refresh_materialized_views(c: sqlite3.Connection, key: str) -> None:
    row = c.execute("SELECT category,title,root_cause,reflection FROM patterns WHERE pattern_id=?", (key,)).fetchone()
    if not row:
        return
    probes = " ".join(x[0] for x in c.execute("SELECT value FROM probes WHERE pattern_id=? AND kind!='card' ORDER BY ordinal", (key,)))
    aliases = " ".join(x[0] for x in c.execute("SELECT alias FROM aliases WHERE pattern_id=?", (key,)))
    c.execute("DELETE FROM patterns_fts WHERE pattern_id=?", (key,))
    c.execute("INSERT INTO patterns_fts VALUES (?,?,?,?,?,?,?)", (key, row[0], row[1], row[2], row[3], probes, aliases))


def insert_pattern(c: sqlite3.Connection, p: Mapping[str, Any]) -> None:
    from .patterns import CARD_FIELDS
    c.execute("INSERT INTO patterns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(p[x] for x in (
        "pattern_id","category","title","root_cause","reflection","status","superseded_by",
        "created_at","updated_at","last_used_at","use_count","hit_count","miss_count","false_positive_count","rejected_count")))
    for kind, values in (
        ("probe", p["probes"]),
        ("negative_control", p["negative_controls"]),
        ("evidence", p["evidence_requirements"]),
        ("applicability", p["applicability"]),
        ("false_positive", p["false_positive_patterns"]),
        ("oracle", p["oracle_hints"]),
        ("tag", p["tags"]),
        ("playbook", p["playbook"]),
        ("attempts_tried", p["attempts_tried"]),
        ("successful_solution", p["successful_solution"]),
    ):
        c.executemany("INSERT INTO probes VALUES (?,?,?,?)", [(p["pattern_id"],kind,x,i) for i,x in enumerate(values)])
    card = {field: p[field] for field in CARD_FIELDS}
    if p.get("routing_tags"):
        card["routing_tags"] = p["routing_tags"]
    if any(card.values()):
        c.execute("INSERT INTO probes VALUES (?,?,?,?)",
                  (p["pattern_id"], "card", json.dumps(card, ensure_ascii=False, sort_keys=True), 0))
    c.executemany("INSERT INTO aliases VALUES (?,?)", [(p["pattern_id"],x) for x in p["aliases"]])
    c.executemany("INSERT INTO sources VALUES (?,?)", [(p["pattern_id"],x) for x in p["source_refs"]])
    c.executemany("INSERT INTO occurrences VALUES (?,?,?)", [(p["pattern_id"],x,p["created_at"]) for x in p["finding_fingerprints"]])
    refresh_materialized_views(c, p["pattern_id"])


def initialize(path: str | os.PathLike[str] = DEFAULT_LEDGER, *, seed: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    from .patterns import read
    from .sync_ops import sync_state_hash, validate_pattern
    logical, db = Path(path), database_path(path)
    assert_no_symlink_components(logical)
    assert_no_symlink_components(db)
    if db.exists() or db.is_symlink():
        with connect_ledger(db, readonly=True) as c:
            validate_pattern(c, db)
        return read(logical)
    patterns, seed_hash = read_seed(Path(seed) if seed else seed_path(logical))
    db.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{db.name}.", suffix=".tmp", dir=db.parent)
    os.close(fd)
    temp = Path(name)
    try:
        with connect_ledger(temp, True) as c:
            c.executescript(SCHEMA)
            fts = init_fts(c)
            c.executemany("INSERT INTO meta VALUES (?,?)", [
                ("schema_version", str(SCHEMA_VERSION)),
                ("seed_sha256", seed_hash),
                ("seed_count", str(len(patterns))),
                ("head_hash", ""),
                ("state_hash", ""),
                ("fts5", str(int(fts))),
                ("archive_manifest", ""),
                ("archive_root_hash", ""),
                ("archive_last_seq", ""),
                ("archive_last_hash", ""),
                ("archive_dir", ""),
            ])
            for p in patterns.values():
                insert_pattern(c, p)
            _sync_table_digests(c)
            sync_state_hash(c)
            validate_pattern(c, temp)
        os.replace(temp, db)
        temp = Path()
    finally:
        if temp and temp.is_file():
            temp.unlink()
    return read(logical)


def get_schema_version(db: Path) -> int | None:
    try:
        with connect_ledger(db, readonly=True) as c:
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except (LessonLedgerError, sqlite3.Error):
        return None
    if row is None:
        return None
    try:
        return int(str(row[0]))
    except (TypeError, ValueError):
        return None


def retire_migrate_backup(backup: Path) -> Path | None:
    if backup.is_symlink() or not backup.exists():
        return None
    done = backup.with_name(f"{backup.name}.done-{int(time.time() * 1000)}")
    os.replace(backup, done)
    return done


def migrate(path: str | os.PathLike[str] = DEFAULT_LEDGER) -> dict[str, Any]:
    """显式迁移并先生成同目录备份；失败时原库保持不变。"""
    from .patterns import read
    logical, db = Path(path), database_path(path)
    is_regular_file(db, "SQLite 运行库")
    backup = db.with_name(f"{db.name}.pre-migrate.bak")
    version = get_schema_version(db)
    if version == SCHEMA_VERSION:
        retire_migrate_backup(backup)
        return read(logical)
    if backup.is_symlink():
        raise LessonLedgerError(f"迁移备份已存在，请人工处理: {backup}")
    if backup.exists():
        backup.replace(db)
    with connect_ledger(db, readonly=True) as source, connect_ledger(backup, True) as target:
        source.backup(target)
    os.chmod(backup, 0o600)
    try:
        with connect_ledger(db) as c:
            ensure_schema(c, migrate=True)
            sync_state_hash(c)
            validate_pattern(c, db)
    except BaseException:
        try:
            backup.replace(db)
        except OSError as exc:
            raise LessonLedgerError(f"迁移失败且回滚失败: {exc}") from exc
        raise
    retire_migrate_backup(backup)
    return read(logical)


def get_meta_value(c: sqlite3.Connection, key: str) -> str:
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return "" if row is None else str(row[0])


def archive_manifest(c: sqlite3.Connection) -> list[dict[str, Any]]:
    raw = get_meta_value(c, "archive_manifest")
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        return list(loaded) if isinstance(loaded, list) else []
    except (TypeError, ValueError):
        return []


def record_event(c: sqlite3.Connection, typ: str, payload: dict[str, Any]) -> None:
    last = c.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    archived_seq = get_meta_value(c, "archive_last_seq")
    if last:
        seq, prev = int(last[0]) + 1, last[1]
    elif archived_seq:
        seq, prev = int(archived_seq) + 1, get_meta_value(c, "archive_last_hash")
    else:
        seq, prev = 1, ""
    # DES-02：原先这里经 ``sys.modules.get("lesson_ledger")`` 反向取 ``MAX_EVENTS``，
    # 与顶层 Facade 构成软环；而该常量**本模块即是定义方**（第 24 行），
    # Facade 只是 re-export。改为直接读本模块全局后，覆盖该上限应 patch
    # ``ledger.schema.MAX_EVENTS``（真实定义方），语义完全等价。
    if c.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= MAX_EVENTS:
        raise LessonLedgerError("账本事件超过上限；请先执行 lesson archive-events 显式归档")
    event = {"seq": seq, "type": typ, "at": now_iso(), "payload": payload, "prev_hash": prev}

    event["hash"] = hashlib.sha256(canonical_bytes(event)).hexdigest()
    c.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?)",
        (seq, typ, event["at"], json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), prev, event["hash"]),
    )
    c.execute("UPDATE meta SET value=? WHERE key='head_hash'", (event["hash"],))


AUDIT_ORDER_BY = {
    "patterns": "pattern_id",
    "probes": "pattern_id,kind,value,ordinal",
    "aliases": "pattern_id,alias",
    "sources": "pattern_id,source_ref",
    "occurrences": "pattern_id,fingerprint",
    "reviews": "polling_id,lifecycle_id,round",
    "review_patterns": "polling_id,lifecycle_id,round,pattern_id",
    "review_findings": "polling_id,lifecycle_id,round,fingerprint",
    "feedback": "lifecycle_id,round,pattern_id,fingerprint",
    "incidents": "incident_id",
    "tool_usage": "lifecycle_id,round,tool_name,category,finding_fingerprint",
    "evidence_receipts": "receipt_id",
    "consumption_events": "event_id",
    "tool_experience": "tool_name,category",
    "sync_import_receipts": "snapshot_id",
    "events": "seq",
}


def audit_snapshot(c: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Return a deterministic digest input for every mutable business table."""
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for table in AUDITED_TABLES:
        order_by = AUDIT_ORDER_BY.get(table, "rowid")
        rows = c.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
        snapshot[table] = [dict(row) for row in rows]
    return snapshot


def _table_snapshot_payload(c: sqlite3.Connection, table: str) -> tuple[str, str, int]:
    """Return canonical rows, digest, and count for one audited table."""
    order_by = AUDIT_ORDER_BY.get(table, "rowid")
    rows = [
        dict(row)
        for row in c.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
    ]
    payload = canonical_bytes(rows)
    return payload.decode("utf-8"), hashlib.sha256(payload).hexdigest(), len(rows)


def _table_digest_record(c: sqlite3.Connection, table: str) -> tuple[str, int]:
    """Return the deterministic digest/count for one audited table."""
    _payload, digest, count = _table_snapshot_payload(c, table)
    return digest, count


def _sync_table_digests(
    c: sqlite3.Connection,
    tables: Iterable[str] | None = None,
) -> dict[str, tuple[str, str, int]]:
    """Persist per-table summaries without changing the legacy global digest."""
    selected = AUDITED_TABLES if tables is None else tuple(dict.fromkeys(tables))
    unknown = set(selected) - set(AUDITED_TABLES)
    if unknown:
        raise LessonLedgerError(f"未知审计表摘要: {sorted(unknown)}")
    stamp = now_iso()
    summaries: dict[str, tuple[str, str, int]] = {}
    for table in selected:
        payload, digest, count = _table_snapshot_payload(c, table)
        summaries[table] = (payload, digest, count)
        c.execute(
            "INSERT INTO state_table_digests(table_name,row_digest,row_count,snapshot_json,updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(table_name) DO UPDATE SET "
            "row_digest=excluded.row_digest,row_count=excluded.row_count,"
            "snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at",
            (table, digest, count, payload, stamp),
        )
    return summaries


def _validate_table_digests(
    c: sqlite3.Connection,
    tables: Iterable[str] | None = None,
    *,
    cached_snapshots: Mapping[str, tuple[str, str, int]] | None = None,
) -> None:
    """Verify persisted table summaries; full validation also rejects stale extras."""
    selected = AUDITED_TABLES if tables is None else tuple(dict.fromkeys(tables))
    unknown = set(selected) - set(AUDITED_TABLES)
    if unknown:
        raise LessonLedgerError(f"未知审计表摘要: {sorted(unknown)}")
    query = "SELECT table_name,row_digest,row_count,snapshot_json FROM state_table_digests"
    params: tuple[str, ...] = ()
    if tables is not None:
        placeholders = ",".join("?" for _ in selected)
        query += f" WHERE table_name IN ({placeholders})"
        params = selected
    stored = {
        str(row[0]): (str(row[1]), int(row[2]), str(row[3]))
        for row in c.execute(query, params)
    }
    if tables is None and set(stored) != set(AUDITED_TABLES):
        raise LessonLedgerError("state_table_digests 表集合不完整")
    for table in selected:
        cached = cached_snapshots.get(table) if cached_snapshots is not None else None
        if cached is None:
            expected_payload, expected_digest, expected_count = _table_snapshot_payload(c, table)
        else:
            expected_payload, expected_digest, expected_count = cached
        actual = stored.get(table)
        if actual is None or actual[:2] != (expected_digest, expected_count):
            raise LessonLedgerError(f"审计表摘要无效: {table}")
        if actual[2] != expected_payload:
            message = "审计表摘要无效" if tables is None else "审计表快照无效"
            raise LessonLedgerError(f"{message}: {table}")


def state_digest(c: sqlite3.Connection) -> str:
    return hashlib.sha256(canonical_bytes(audit_snapshot(c))).hexdigest()


def _state_digest_from_table_snapshots(c: sqlite3.Connection) -> str:
    """Rebuild the legacy global digest from persisted canonical table rows."""
    stored = {
        str(row[0]): (str(row[1]), int(row[2]), str(row[3]))
        for row in c.execute(
            "SELECT table_name,row_digest,row_count,snapshot_json FROM state_table_digests"
        )
    }
    if set(stored) != set(AUDITED_TABLES):
        raise LessonLedgerError("state_table_digests 表集合不完整")
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for table in AUDITED_TABLES:
        try:
            digest, count, payload = stored[table]
            rows = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LessonLedgerError(f"审计表快照无效: {table}") from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise LessonLedgerError(f"审计表快照无效: {table}")
        if hashlib.sha256(canonical_bytes(rows)).hexdigest() != digest or len(rows) != count:
            raise LessonLedgerError(f"审计表快照无效: {table}")
        snapshot[table] = rows
    return hashlib.sha256(canonical_bytes(snapshot)).hexdigest()


def sync_state_hash(
    c: sqlite3.Connection,
    dirty_tables: Iterable[str] | None = None,
    *,
    digest_cache: dict[str, tuple[str, str, int]] | None = None,
) -> str:
    if dirty_tables is not None:
        summaries = _sync_table_digests(c, dirty_tables)
        if digest_cache is not None:
            digest_cache.update(summaries)
        value = _state_digest_from_table_snapshots(c)
    else:
        value = state_digest(c)
    c.execute("UPDATE meta SET value=? WHERE key='state_hash'", (value,))
    return value


class _TransactionSummary:
    """记录当前写事务触碰的审计表。

    这里只用于安全地识别「没有业务表变更」的事务；有任一审计表写入时仍
    走原有全量摘要算法。这样不会改变旧账本的 ``state_hash`` 定义，也不
    把事件链校验交给增量缓存。
    """

    def __init__(self) -> None:
        self.dirty_tables: set[str] = set()

    def authorize(
        self,
        action_code: int,
        table: str | None,
        _column: str | None,
        _database: str | None,
        _source: str | None,
    ) -> int:
        if action_code in (
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        ) and table in AUDITED_TABLES:
            self.dirty_tables.add(table)
        return sqlite3.SQLITE_OK


def validate_pattern(
    c: sqlite3.Connection,
    db: Path,
    *,
    expected_state_hash: str | None = None,
    dirty_tables: Iterable[str] | None = None,
    cached_snapshots: Mapping[str, tuple[str, str, int]] | None = None,
) -> None:
    if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise LessonLedgerError(f"SQLite integrity_check 失败: {db}")
    schema = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if schema is None:
        raise LessonLedgerError("SQLite schema_version 缺失；无法确认账本世代，请从备份恢复或显式重建")
    if schema[0] != str(SCHEMA_VERSION):
        if schema[0].isdigit() and 2 <= int(schema[0]) < SCHEMA_VERSION:
            raise LessonLedgerError(
                f"SQLite schema_version={schema[0]}，请显式执行 lesson migrate "
                f"（python3 lesson_ledger.py migrate --ledger {db}）后再重试"
            )
        raise LessonLedgerError("SQLite schema_version 缺失或不受支持")
    indexes = {str(row[1]) for row in c.execute("PRAGMA index_list('incidents')")}
    if "idx_incidents_finding_fingerprint" not in indexes:
        raise LessonLedgerError("incident finding_fingerprint 索引缺失，请执行 lesson migrate")
    archived_seq = get_meta_value(c, "archive_last_seq")
    prev = get_meta_value(c, "archive_last_hash") if archived_seq else ""
    expected_seq = int(archived_seq) + 1 if archived_seq else 1
    for r in c.execute("SELECT * FROM events ORDER BY seq"):
        try:
            payload = json.loads(r[3])
        except (TypeError, json.JSONDecodeError) as exc:
            raise LessonLedgerError("events payload 不是有效 JSON") from exc
        event = {"seq": r[0], "type": r[1], "at": r[2], "payload": payload, "prev_hash": r[4], "hash": r[5]}
        if event["seq"] != expected_seq:
            raise LessonLedgerError("events seq 不连续")
        if event["prev_hash"] != prev or hashlib.sha256(canonical_bytes({k: v for k, v in event.items() if k != "hash"})).hexdigest() != event["hash"]:
            raise LessonLedgerError("events hash-chain 无效")
        prev = event["hash"]
        expected_seq += 1
    head_row = c.execute("SELECT value FROM meta WHERE key='head_hash'").fetchone()
    if head_row is None:
        raise LessonLedgerError("SQLite head_hash 缺失")
    head = head_row[0]
    if head != prev:
        raise LessonLedgerError("head_hash 无效")
    state_hash = c.execute("SELECT value FROM meta WHERE key='state_hash'").fetchone()
    expected = expected_state_hash if expected_state_hash is not None else state_digest(c)
    if state_hash is None or state_hash[0] != expected:
        raise LessonLedgerError("state_hash 无效")
    _validate_table_digests(c, dirty_tables, cached_snapshots=cached_snapshots)


def ledger_transaction(path: str | os.PathLike[str], fn: Callable[[sqlite3.Connection], Any]) -> Any:
    db = database_path(path)
    if not db.exists():
        initialize(path)
    with connect_ledger(db) as c:
        ensure_schema(c)
        c.execute("BEGIN IMMEDIATE")
        summary = _TransactionSummary()
        c.set_authorizer(summary.authorize)
        try:
            res = fn(c)
            # A read-only/no-op transaction cannot change the state digest.  Keep
            # the old digest and still run integrity plus event-chain validation;
            # any audited-table DML takes the unchanged full-digest path.
            if not summary.dirty_tables:
                state_hash = get_meta_value(c, "state_hash")
            else:
                # ponytail: external SQL cannot identify its dirty tables; scan the
                # untouched snapshots before composing a new aggregate. Keep the
                # faster per-table commit path for dirty tables, and revisit with
                # a durable SQLite change marker if write throughput matters.
                untouched = tuple(
                    table for table in AUDITED_TABLES
                    if table not in summary.dirty_tables
                )
                _validate_table_digests(c, untouched)
                dirty_snapshots: dict[str, tuple[str, str, int]] = {}
                state_hash = sync_state_hash(
                    c, summary.dirty_tables, digest_cache=dirty_snapshots,
                )
            validate_pattern(
                c,
                db,
                expected_state_hash=state_hash,
                # A no-op transaction still verifies every persisted table
                # digest, so metadata tampering cannot hide behind the fast
                # path.  A dirty transaction verifies the tables it changed;
                # ordinary read/validate_deep remains the full fail-closed scan.
                dirty_tables=summary.dirty_tables or None,
                cached_snapshots=dirty_snapshots if summary.dirty_tables else None,
            )
            c.execute("COMMIT")
            if (
                os.environ.get("BUGX_CLOUD_AUTOSYNC_WORKER") != "1"
                and os.environ.get("BUGX_CLOUD_SYNC_ACTIVE") != "1"
            ):
                try:
                    from core.cloud_sync.auto_sync import schedule_ledger_sync
                    schedule_ledger_sync(path)
                except Exception:
                    # Cloud sync is best-effort after the local commit; it must
                    # never turn a durable local lesson into a failed write.
                    pass
            return res
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.set_authorizer(None)
