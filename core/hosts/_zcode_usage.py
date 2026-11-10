#!/usr/bin/env python3
"""zcode_usage.py — **ZCode 宿主专属**的本地 Token 用量读取器（只读）。

专属性边界：本模块只服务于 runtime.host 为 ZCode 的会话（调用方按宿主名门控），读取的是
ZCode CLI 自己的本地 SQLite（``~/.zcode/cli/db/db.sqlite``，只读模式，
绝不写入）；其它宿主（codex/cline/...）没有这张表，通用引擎代码**不得**
依赖本模块——所有调用方必须按宿主名门控并在异常时静默降级。

能力（用户确认过的两个维度）：

1. **项目级**：ZCode 会话按 ``session.directory`` 精确匹配被审计项目根；
   worker 子会话（提示词前缀 ``你是 BugX 并发 Worker``，ZCode 自动以首条
   消息生成标题）与主会话分开统计。
2. **天/小时粒度**：``model_usage.started_at``（毫秒 epoch）支持任意时间
   窗聚合；本模块按 **lifecycle 时间窗**取增量（``started_epoch`` → 现在），
   作为独立字段输出，与 ``worker_reported`` 精确上报互不覆盖。

输出只进本地运行态（runtime 报告行 + lifecycle ``state/zcode-usage.json``
明细），**不上云**——云端 run summary 的 schema 白名单不认宿主专属字段
（build 294 教训：未知键整包 400）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

#: ZCode worker 子会话的标题前缀（宿主以首条消息自动生成标题）。
WORKER_TITLE_PREFIX = "你是 BugX 并发 Worker"

_DEFAULT_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


class ZcodeUsageError(RuntimeError):
    """ZCode 本地用量读取失败（DB 缺失/锁定/无该项目会话）。"""


def _db_path() -> Path:
    override = os.environ.get("ZCODE_USAGE_DB")
    return Path(override).expanduser() if override else _DEFAULT_DB


def _connect(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise ZcodeUsageError(f"ZCode 本地数据库不存在: {db}")
    try:
        # 先按标准只读打开（尊重 WAL，能读到 ZCode 运行中的最新数据）。
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError:
        # WAL 忙锁等场景退化为 immutable 快照读（可能略旧，但绝不阻塞）。
        return sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)


def _project_sessions(conn: sqlite3.Connection, project_root: Path) -> list[dict[str, Any]]:
    """按目录**精确匹配**项目根（resolve 后）的 ZCode 会话列表。"""
    rows = conn.execute(
        "SELECT id, title, directory, time_created FROM session WHERE directory = ?",
        (str(project_root),),
    ).fetchall()
    return [
        {
            "session_id": row[0],
            "title": row[1] or "",
            "directory": row[2],
            "time_created_ms": int(row[3] or 0),
            "is_worker": (row[1] or "").startswith(WORKER_TITLE_PREFIX),
        }
        for row in rows
    ]


def _window_tokens(
    conn: sqlite3.Connection,
    session_ids: list[str],
    since_ms: int,
    until_ms: int,
) -> dict[str, int]:
    """按会话聚合时间窗内的 computed_total_tokens（毫秒 epoch 半开区间）。"""
    if not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"""
        SELECT session_id, SUM(computed_total_tokens) total
        FROM model_usage
        WHERE session_id IN ({placeholders})
          AND started_at >= ? AND started_at < ?
        GROUP BY session_id
        """,
        [*session_ids, since_ms, until_ms],
    ).fetchall()
    return {row[0]: int(row[1] or 0) for row in rows}


def _window_models(
    conn: sqlite3.Connection,
    session_ids: list[str],
    since_ms: int,
    until_ms: int,
) -> list[dict[str, Any]]:
    """按模型聚合时间窗内的调用/分项 token/时长（``models`` 数组，至少一项）。"""
    if not session_ids:
        return []
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"""
        SELECT model_id, COUNT(*) calls,
               SUM(input_tokens) inp, SUM(output_tokens) outp,
               SUM(reasoning_tokens) reasoning, SUM(cache_read_input_tokens) cache_read,
               SUM(computed_total_tokens) total, SUM(duration_ms) duration_ms
        FROM model_usage
        WHERE session_id IN ({placeholders})
          AND started_at >= ? AND started_at < ?
        GROUP BY model_id
        ORDER BY total DESC
        """,
        [*session_ids, since_ms, until_ms],
    ).fetchall()
    return [
        {
            "model": row[0] or "unknown",
            "calls": int(row[1] or 0),
            "input_tokens": int(row[2] or 0),
            "output_tokens": int(row[3] or 0),
            "reasoning_tokens": int(row[4] or 0),
            "cache_read_tokens": int(row[5] or 0),
            "total_tokens": int(row[6] or 0),
            "duration_ms": int(row[7] or 0),
        }
        for row in rows
    ]


def _fmt_duration(ms: int) -> str:
    """毫秒 → 人读时长（``Xh Ym Zs``，不足一小时省略小时段）。"""
    seconds = int(round(ms / 1000))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def collect_lifecycle_usage(
    project_root: str | os.PathLike[str],
    runtime: Mapping[str, Any],
    *,
    until_epoch: float | None = None,
) -> dict[str, Any]:
    """汇总本 lifecycle 时间窗内、本项目的 ZCode 本地 token 用量。

    ``runtime`` 需要 ``started_epoch``（秒，pre 基线时刻）；窗口终点缺省取
    当前时间。返回聚合结果并把逐会话明细写入 lifecycle
    ``state/zcode-usage.json``（0600），供审计回看。
    """
    root = Path(project_root).expanduser().resolve()
    started_epoch = runtime.get("started_epoch")
    if not isinstance(started_epoch, (int, float)) or started_epoch <= 0:
        raise ZcodeUsageError("runtime 缺少 started_epoch，无法确定用量时间窗")
    since_ms = int(started_epoch * 1000)
    until = float(until_epoch if until_epoch is not None else time.time())
    until_ms = int(until * 1000)

    conn = _connect(_db_path())
    try:
        sessions = _project_sessions(conn, root)
        if not sessions:
            raise ZcodeUsageError(
                f"ZCode 本地数据库中没有目录精确匹配 {root} 的会话"
            )
        all_session_ids = [s["session_id"] for s in sessions]
        by_id = _window_tokens(conn, all_session_ids, since_ms, until_ms)
        models = _window_models(conn, all_session_ids, since_ms, until_ms)
    finally:
        conn.close()

    worker_sessions: list[dict[str, Any]] = []
    main_sessions: list[dict[str, Any]] = []
    worker_total = 0
    main_total = 0
    for session in sessions:
        tokens = by_id.get(session["session_id"], 0)
        entry = {**session, "tokens_in_window": tokens}
        if session["is_worker"]:
            worker_sessions.append(entry)
            worker_total += tokens
        else:
            main_sessions.append(entry)
            main_total += tokens

    duration_ms_total = sum(m["duration_ms"] for m in models)

    detail_path: Path | None = None
    try:
        import core.bugx_paths as bugx_paths
        import core.secure_atomic as secure_atomic

        scope = bugx_paths.require(root)
        detail_path = scope.state_root / "zcode-usage.json"
        detail = {
            "schema_version": 1,
            "project_root": str(root),
            "window": {"since_epoch": started_epoch, "until_epoch": until},
            "db_path": str(_db_path()),
            "worker_sessions": worker_sessions,
            "main_sessions": main_sessions,
            "worker_sessions_tokens": worker_total,
            "scheduler_tokens": main_total,
            "models": models,
            "duration_ms_total": duration_ms_total,
        }
        detail_path.parent.mkdir(parents=True, exist_ok=True)
        secure_atomic.atomic_write_json(detail_path, detail, mode=0o600)
    except (OSError, ValueError, RuntimeError, ImportError):
        # 明细落盘失败不影响聚合结果返回（报告行仍可用）。
        detail_path = None

    return {
        "scheduler_tokens": main_total,
        "worker_sessions_tokens": worker_total,
        "worker_sessions": worker_sessions,
        "main_sessions": main_sessions,
        "main_session_count": len(main_sessions),
        "worker_session_count": len(worker_sessions),
        # 模型数组（至少一项）：逐模型调用数/分项 token/时长 + 总时长汇总
        "models": models,
        "duration_ms_total": duration_ms_total,
        "detail_path": str(detail_path) if detail_path else None,
        "window": {"since_epoch": started_epoch, "until_epoch": until},
    }


def verify_worker_tokens(
    reported_total: int,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    """建议②的核对视图：worker ``worker_reported`` 上报值 vs ZCode DB 实测值。

    两侧口径不同（上报=Agent 结果精确值；DB=会话聚合含窗口裁剪误差），
    因此只产出审计对比行，**不回写** claim——已封存 checkpoint 的归属
    不被宿主侧事后改写。
    """
    db_total = int(usage.get("worker_sessions_tokens") or 0)
    delta = db_total - int(reported_total or 0)
    ratio = (db_total / reported_total) if reported_total else None
    return {
        "reported_total": int(reported_total or 0),
        "db_total": db_total,
        "delta": delta,
        "ratio": round(ratio, 3) if ratio else None,
        "verdict": (
            "match" if reported_total and abs(delta) <= max(64, reported_total // 10)
            else "mismatch" if reported_total else "no_report"
        ),
    }


__all__ = [
    "ZcodeUsageError",
    "WORKER_TITLE_PREFIX",
    "collect_lifecycle_usage",
    "verify_worker_tokens",
]
