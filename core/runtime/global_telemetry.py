#!/usr/bin/env python3
"""global_telemetry.py — 全局时间与 Token 计量治理存储引擎 (DOC-049)。

提供跨项目的全局时间累加、Token 消耗聚合、各项目成本排行与每日预算额度监控。
数据存储于 ~/.bugx/telemetry/stats.sqlite3（支持环境变量覆盖）。
多进程并发安全写入（WAL 模式 + busy_timeout + 独占事务）；
生命周期调用 fail-open 保护，任何遥测记录异常绝不阻断项目本身的正常审计执行。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import core.bugx_paths as bugx_paths

TELEMETRY_SCHEMA_VERSION = 3
logger = logging.getLogger("bugx.telemetry")


def _get_db_path(override_path: Path | str | None = None) -> Path:
    if override_path:
        return Path(override_path).expanduser().resolve()
    return bugx_paths.global_telemetry_path()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _today_str() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _hash_project(project_root_or_name: str) -> str:
    """计算旧版遥测项目标识，供 v1/v2 升级时保持分组一致。"""
    return hashlib.sha256(str(project_root_or_name).encode("utf-8")).hexdigest()[:16]


def _load_project_hmac_key() -> bytes:
    from core.report_engine.report_client import load_installation_secret

    return load_installation_secret()


def _project_token(project_identity: str, *, legacy_hash: bool = False) -> str:
    """Return an install-scoped opaque token without persisting project labels."""
    key = _load_project_hmac_key()
    legacy = str(project_identity) if legacy_hash else _hash_project(str(project_identity))
    return hmac.new(
        key,
        b"bugx-global-telemetry-project-v3\x00" + legacy.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _project_label(token: str) -> str:
    return f"project-{token[:12]}"


def _migrate_project_metadata(
    conn: sqlite3.Connection,
    *,
    legacy_schema: int,
) -> None:
    """Replace legacy names and hashes with stable opaque install-scoped tokens."""
    for table, key_column in (
        ("telemetry_sessions", "session_id"),
        ("telemetry_rounds", "round_id"),
    ):
        rows = conn.execute(
            f"SELECT {key_column}, project_name, project_hash FROM {table}"
        ).fetchall()
        for row_key, project_name, project_hash in rows:
            identity = str(project_hash or project_name or "unknown")
            token = _project_token(
                identity,
                legacy_hash=legacy_schema >= 2,
            )
            conn.execute(
                f"UPDATE {table} SET project_name=?, project_hash=? WHERE {key_column}=?",
                (_project_label(token), token, row_key),
            )


def _upgrade_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM telemetry_meta WHERE key='schema_version'"
    ).fetchone()
    current = int(row[0]) if row else None
    if current is not None and current > TELEMETRY_SCHEMA_VERSION:
        raise RuntimeError(
            f"telemetry schema {current} is newer than supported {TELEMETRY_SCHEMA_VERSION}"
        )
    if current != TELEMETRY_SCHEMA_VERSION:
        conn.execute("BEGIN IMMEDIATE")
        try:
            has_rows = any(
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                for table in ("telemetry_sessions", "telemetry_rounds")
            )
            if has_rows:
                # v1 stores the unkeyed caller value; v2 already stores the
                # truncated SHA-256 used by current writes. Schema-less legacy
                # files are treated as v1 so no raw value is exposed.
                legacy_schema = current if current is not None else 1
                _migrate_project_metadata(conn, legacy_schema=legacy_schema)
            conn.execute(
                "INSERT OR REPLACE INTO telemetry_meta(key, value) VALUES ('schema_version', ?);",
                (str(TELEMETRY_SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def _get_connection(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = _get_db_path(db_path)
    try:
        bugx_paths.assert_no_symlink_components(path.parent)
        bugx_paths.assert_no_symlink_components(path)
    except Exception as exc:
        raise RuntimeError(f"拒绝使用不安全或包含符号链接的遥测路径: {path}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> Path:
    """初始化全局遥测 SQLite 数据库并创建所需表格。"""
    path = _get_db_path(db_path)
    with _get_connection(path) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_sessions (
                    session_id TEXT PRIMARY KEY,
                    project_name TEXT NOT NULL,
                    project_hash TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    tokens_consumed INTEGER NOT NULL,
                    token_method TEXT NOT NULL,
                    findings_count INTEGER NOT NULL,
                    credited_count INTEGER NOT NULL,
                    rounds_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_rounds (
                    round_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    project_hash TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL,
                    tokens_consumed INTEGER NOT NULL,
                    seconds_per_credited REAL,
                    tokens_per_credited REAL,
                    token_breakdown TEXT,
                    credited INTEGER NOT NULL,
                    findings INTEGER NOT NULL,
                    timestamp TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_rollup (
                    date TEXT PRIMARY KEY,
                    total_duration_seconds REAL NOT NULL DEFAULT 0.0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    session_count INTEGER NOT NULL DEFAULT 0,
                    findings_count INTEGER NOT NULL DEFAULT 0,
                    credited_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_project ON telemetry_sessions(project_hash);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rounds_session ON telemetry_rounds(session_id);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rounds_project ON telemetry_rounds(project_hash);
            """)
            _upgrade_schema_version(conn)
    return path


def record_round_telemetry(round_data: Mapping[str, Any], db_path: Path | str | None = None) -> bool:
    """写入单轮次细粒度遥测记录，并原子累加当日消耗 (fail-open，不抛出异常打断审计)。"""
    if not isinstance(round_data, Mapping) or not round_data:
        return False
    try:
        init_db(db_path)
        session_id = str(round_data.get("session_id") or round_data.get("run_id") or "standalone")
        round_no = int(round_data.get("round", 1) or 1)
        round_id = str(round_data.get("round_id") or f"{session_id}_r{round_no}")
        raw_project_name = str(round_data.get("project_name") or "default")
        project_hash = _project_token(
            str(round_data.get("project_hash") or raw_project_name),
        )
        project_name = _project_label(project_hash)

        duration = float(round_data.get("duration_seconds", 0.0) or 0.0)
        tokens = int(round_data.get("tokens_consumed") or round_data.get("token_count") or 0)
        credited = int(round_data.get("credited", 0) or 0)
        findings = int(round_data.get("findings", 0) or 0)

        seconds_per_credited = round_data.get("seconds_per_credited")
        if seconds_per_credited is not None:
            seconds_per_credited = float(seconds_per_credited)
        elif credited > 0:
            seconds_per_credited = round(duration / credited, 3)

        tokens_per_credited = round_data.get("tokens_per_credited")
        if tokens_per_credited is not None:
            tokens_per_credited = float(tokens_per_credited)
        elif credited > 0 and tokens > 0:
            tokens_per_credited = round(tokens / credited, 1)

        breakdown = round_data.get("token_breakdown")
        breakdown_json = json.dumps(breakdown, ensure_ascii=False) if isinstance(breakdown, dict) else "{}"
        ts = str(round_data.get("timestamp") or _now_iso())
        today = ts[:10] if len(ts) >= 10 else _today_str()

        with _get_connection(db_path) as conn:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO telemetry_rounds (
                        round_id, session_id, project_name, project_hash, round,
                        duration_seconds, tokens_consumed, seconds_per_credited,
                        tokens_per_credited, token_breakdown, credited, findings, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    round_id, session_id, project_name, project_hash, round_no,
                    duration, tokens, seconds_per_credited,
                    tokens_per_credited, breakdown_json, credited, findings, ts,
                ))

                # 原子累加当日汇总 daily_rollup
                conn.execute("""
                    INSERT INTO daily_rollup (
                        date, total_duration_seconds, total_tokens, session_count,
                        findings_count, credited_count, updated_at
                    ) VALUES (?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        total_duration_seconds = total_duration_seconds + excluded.total_duration_seconds,
                        total_tokens = total_tokens + excluded.total_tokens,
                        findings_count = findings_count + excluded.findings_count,
                        credited_count = credited_count + excluded.credited_count,
                        updated_at = excluded.updated_at;
                """, (
                    today, duration, tokens, findings, credited, _now_iso(),
                ))
        return True
    except Exception as exc:
        logger.debug("[global_telemetry] record_round_telemetry 记录忽略 (fail-open): %s", exc)
        return False


def record_session_telemetry(session_data: Mapping[str, Any], db_path: Path | str | None = None) -> bool:
    """写入 Session 级最终汇总记录，并原子更新当日汇总 daily_rollup (fail-open)。"""
    if not isinstance(session_data, Mapping) or not session_data:
        return False
    try:
        init_db(db_path)
        session_id = str(session_data.get("session_id") or f"sess-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}")
        raw_project_name = str(session_data.get("project_name") or "default")
        project_hash = _project_token(
            str(session_data.get("project_hash") or raw_project_name),
        )
        project_name = _project_label(project_hash)

        start_time = str(session_data.get("start_time") or _now_iso())
        end_time = str(session_data.get("end_time") or _now_iso())
        duration = float(session_data.get("duration_seconds", 0.0) or 0.0)
        tokens = int(session_data.get("tokens_consumed") or session_data.get("token_count") or 0)
        token_method = str(session_data.get("token_method") or "self-reported")
        findings_count = int(session_data.get("findings_count", 0) or 0)
        credited_count = int(session_data.get("credited_count", 0) or 0)
        rounds_count = int(session_data.get("rounds_count", 1) or 1)
        status = str(session_data.get("status") or "completed")
        created_at = _now_iso()
        today = end_time[:10] if len(end_time) >= 10 else _today_str()

        with _get_connection(db_path) as conn:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO telemetry_sessions (
                        session_id, project_name, project_hash, start_time, end_time,
                        duration_seconds, tokens_consumed, token_method, findings_count,
                        credited_count, rounds_count, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    session_id, project_name, project_hash, start_time, end_time,
                    duration, tokens, token_method, findings_count,
                    credited_count, rounds_count, status, created_at,
                ))

                # 计算已记录在 rounds 中的增量，避免双重计数
                cur = conn.execute("""
                    SELECT
                        COUNT(round_id),
                        COALESCE(SUM(tokens_consumed), 0),
                        COALESCE(SUM(duration_seconds), 0.0),
                        COALESCE(SUM(findings), 0),
                        COALESCE(SUM(credited), 0)
                    FROM telemetry_rounds
                    WHERE session_id = ?;
                """, (session_id,))
                r_cnt, r_tokens, r_duration, r_findings, r_credited = cur.fetchone()
                if r_cnt == 0:
                    delta_tokens = tokens
                    delta_duration = duration
                    delta_findings = findings_count
                    delta_credited = credited_count
                else:
                    delta_tokens = max(0, tokens - int(r_tokens))
                    delta_duration = max(0.0, duration - float(r_duration))
                    delta_findings = max(0, findings_count - int(r_findings))
                    delta_credited = max(0, credited_count - int(r_credited))

                # 更新 daily_rollup
                conn.execute("""
                    INSERT INTO daily_rollup (
                        date, total_duration_seconds, total_tokens, session_count,
                        findings_count, credited_count, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        total_duration_seconds = total_duration_seconds + excluded.total_duration_seconds,
                        total_tokens = total_tokens + excluded.total_tokens,
                        session_count = session_count + 1,
                        findings_count = findings_count + excluded.findings_count,
                        credited_count = credited_count + excluded.credited_count,
                        updated_at = excluded.updated_at;
                """, (
                    today, delta_duration, delta_tokens, delta_findings, delta_credited, _now_iso(),
                ))
        return True
    except Exception as exc:
        logger.debug("[global_telemetry] record_session_telemetry 记录忽略 (fail-open): %s", exc)
        return False


def get_daily_token_consumption(date_str: str | None = None, db_path: Path | str | None = None) -> int:
    """获取指定日期（默认今日）全机跨项目累计消耗的 Token 总量。"""
    target_date = date_str or _today_str()
    try:
        path = _get_db_path(db_path)
        if not path.is_file():
            return 0
        with _get_connection(path) as conn:
            cur = conn.execute("SELECT total_tokens FROM daily_rollup WHERE date = ?;", (target_date,))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def get_daily_duration_consumption(date_str: str | None = None, db_path: Path | str | None = None) -> float:
    """获取指定日期（默认今日）全机跨项目累计总审计耗时（秒）。"""
    target_date = date_str or _today_str()
    try:
        path = _get_db_path(db_path)
        if not path.is_file():
            return 0.0
        with _get_connection(path) as conn:
            cur = conn.execute("SELECT total_duration_seconds FROM daily_rollup WHERE date = ?;", (target_date,))
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0
    except Exception:
        return 0.0


def query_global_summary(days: int = 30, db_path: Path | str | None = None) -> dict[str, Any]:
    """查询最近 N 天的全局时间与 Token 汇总及综合 ROI 指标。"""
    try:
        path = _get_db_path(db_path)
        if not path.is_file():
            return {
                "status": "empty",
                "days": days,
                "total_duration_seconds": 0.0,
                "total_tokens": 0,
                "total_sessions": 0,
                "total_findings": 0,
                "total_credited": 0,
                "avg_seconds_per_credited": None,
                "avg_tokens_per_credited": None,
                "projects_count": 0,
            }
        since_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(1, days))).strftime("%Y-%m-%d")
        with _get_connection(path) as conn:
            cur = conn.execute("""
                SELECT
                    COUNT(session_id) as total_sessions,
                    COALESCE(SUM(duration_seconds), 0.0) as total_duration,
                    COALESCE(SUM(tokens_consumed), 0) as total_tokens,
                    COALESCE(SUM(findings_count), 0) as total_findings,
                    COALESCE(SUM(credited_count), 0) as total_credited,
                    COUNT(DISTINCT project_hash) as projects_count
                FROM telemetry_sessions
                WHERE end_time >= ?;
            """, (since_date,))
            row = cur.fetchone()
            sessions = int(row[0] or 0)
            duration = float(row[1] or 0.0)
            tokens = int(row[2] or 0)
            findings = int(row[3] or 0)
            credited = int(row[4] or 0)
            projects = int(row[5] or 0)

            sec_per_cred = round(duration / credited, 3) if credited > 0 else None
            tok_per_cred = round(tokens / credited, 1) if credited > 0 and tokens > 0 else None

            return {
                "status": "ok",
                "days": days,
                "since_date": since_date,
                "total_duration_seconds": round(duration, 3),
                "total_tokens": tokens,
                "total_sessions": sessions,
                "total_findings": findings,
                "total_credited": credited,
                "avg_seconds_per_credited": sec_per_cred,
                "avg_tokens_per_credited": tok_per_cred,
                "projects_count": projects,
            }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "days": days,
            "total_duration_seconds": 0.0,
            "total_tokens": 0,
            "total_sessions": 0,
            "total_findings": 0,
            "total_credited": 0,
            "avg_seconds_per_credited": None,
            "avg_tokens_per_credited": None,
            "projects_count": 0,
        }


def query_project_breakdown(days: int = 30, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """按不透明项目标签分组查询近 N 天的消耗明细与 ROI 排行。"""
    try:
        path = _get_db_path(db_path)
        if not path.is_file():
            return []
        # A stats/MCP read is also an upgrade boundary. Never return legacy
        # project names merely because this process has not written telemetry.
        init_db(path)
        since_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(1, days))).strftime("%Y-%m-%d")
        with _get_connection(path) as conn:
            cur = conn.execute("""
                SELECT
                    project_name,
                    project_hash,
                    COUNT(session_id) as session_count,
                    COALESCE(SUM(duration_seconds), 0.0) as total_duration,
                    COALESCE(SUM(tokens_consumed), 0) as total_tokens,
                    COALESCE(SUM(findings_count), 0) as total_findings,
                    COALESCE(SUM(credited_count), 0) as total_credited
                FROM telemetry_sessions
                WHERE end_time >= ?
                GROUP BY project_hash, project_name
                ORDER BY total_tokens DESC, total_duration DESC;
            """, (since_date,))
            rows = cur.fetchall()
            result = []
            for r in rows:
                p_name, p_hash, s_cnt, dur, tok, f_cnt, c_cnt = r
                dur = float(dur or 0.0)
                tok = int(tok or 0)
                c_cnt = int(c_cnt or 0)
                sec_per_cred = round(dur / c_cnt, 3) if c_cnt > 0 else None
                tok_per_cred = round(tok / c_cnt, 1) if c_cnt > 0 and tok > 0 else None
                result.append({
                    "project_name": p_name,
                    "project_hash": p_hash,
                    "session_count": int(s_cnt),
                    "total_duration_seconds": round(dur, 3),
                    "total_tokens": tok,
                    "total_findings": int(f_cnt),
                    "total_credited": c_cnt,
                    "seconds_per_credited": sec_per_cred,
                    "tokens_per_credited": tok_per_cred,
                })
            return result
    except Exception:
        return []


def query_daily_rollup(days: int = 30, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """查询最近 N 天的每日汇总时间与 Token 消耗走势。"""
    try:
        path = _get_db_path(db_path)
        if not path.is_file():
            return []
        since_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(1, days))).strftime("%Y-%m-%d")
        with _get_connection(path) as conn:
            cur = conn.execute("""
                SELECT date, total_duration_seconds, total_tokens, session_count, findings_count, credited_count
                FROM daily_rollup
                WHERE date >= ?
                ORDER BY date ASC;
            """, (since_date,))
            rows = cur.fetchall()
            return [
                {
                    "date": r[0],
                    "total_duration_seconds": round(float(r[1] or 0.0), 3),
                    "total_tokens": int(r[2] or 0),
                    "session_count": int(r[3] or 0),
                    "findings_count": int(r[4] or 0),
                    "credited_count": int(r[5] or 0),
                }
                for r in rows
            ]
    except Exception:
        return []

