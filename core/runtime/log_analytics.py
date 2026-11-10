#!/usr/bin/env python3
"""BugX 日志分析与可观测性工具模块 (DOC-133)。

提供日志流式检索、错误过滤、链路跟踪、终端耗时瀑布图与一键脱敏诊断包导出。
独立于 cli.py，保持 CLI 入口精简。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any

from core.runtime.bugx_logging import log_path, safe_value


def get_log_records(root: Path, max_lines: int | None = None) -> list[dict[str, Any]]:
    """读取诊断日志记录列表。"""
    path = log_path(root)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        records.append(data)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    if max_lines is not None and len(records) > max_lines:
        records = records[-max_lines:]
    return records


def tail_logs(root: Path, n: int = 20) -> list[dict[str, Any]]:
    """获取最新 N 条日志。"""
    if n <= 0:
        n = 20
    return get_log_records(root, max_lines=n)


def stream_watch(root: Path, *, poll_interval: float = 0.5, follow: bool = True) -> int:
    """实时流式监听日志新增行，并格式化输出（支持 Ctrl-C 退出）。"""
    import time
    path = log_path(root)
    print(f"[bugx watch] 正在监听: {path} (按 Ctrl-C 退出)...")
    last_size = 0
    if path.is_file():
        last_size = path.stat().st_size
        recent = tail_logs(root, n=10)
        if recent:
            print("--- 最新 10 条日志 ---")
            for r in recent:
                print(format_record_line(r))
            print("--- 实时流 ---")

    try:
        while True:
            if not path.is_file():
                time.sleep(poll_interval)
                continue
            cur_size = path.stat().st_size
            if cur_size > last_size:
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        lines = f.readlines()
                        last_size = f.tell()
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                record = json.loads(line)
                                if isinstance(record, dict):
                                    print(format_record_line(record))
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    pass
            elif cur_size < last_size:
                # 日志轮转发生了
                last_size = 0
            if not follow:
                break
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n[bugx watch] 停止监听。")
        return 0
    return 0


def filter_errors(root: Path, n: int = 50) -> list[dict[str, Any]]:
    """筛选包含警告、错误或异常的日志。"""
    all_records = get_log_records(root)
    matched: list[dict[str, Any]] = []
    for r in all_records:
        level = str(r.get("level", "")).upper()
        if level in {"WARNING", "ERROR", "CRITICAL"}:
            matched.append(r)
        elif (
            r.get("status") == "failed"
            or "exception" in r
            or (isinstance(r.get("exit_code"), int) and r["exit_code"] != 0)
        ):
            matched.append(r)
    return matched[-n:] if n > 0 else matched


def filter_trace(root: Path, trace_id: str) -> list[dict[str, Any]]:
    """按 trace_id 聚合关联的所有日志行。"""
    target = trace_id.strip().lower()
    all_records = get_log_records(root)
    return [
        r for r in all_records
        if str(r.get("trace_id", "")).lower() == target
        or str(r.get("parent_span_id", "")).lower() == target
        or target in str(r.get("traceparent", "")).lower()
    ]


def mcp_polling_report(root: Path, method: str = "tools/list") -> dict[str, Any]:
    """Summarize server-side evidence for a repetitive MCP read.

    The report intentionally separates observable server facts from client
    behavior. A server can prove how many requests arrived and whether its
    own catalogue cache was used; it cannot prove that an external client
    cached the result or subscribed to ``list_changed``.
    """
    records = get_log_records(root)
    batches = [
        record for record in records
        if record.get("event") == "mcp_request_batch_finished"
        and record.get("method") == method
    ]
    individual = [
        record for record in records
        if record.get("event") == "mcp_request_finished"
        and record.get("method") == method
    ]
    request_count = sum(
        int(record.get("request_count", 0))
        for record in batches
        if isinstance(record.get("request_count"), int)
        and not isinstance(record.get("request_count"), bool)
    ) + len(individual)
    cache_hits = sum(
        int(record.get("cache_hits", 0))
        for record in batches
        if isinstance(record.get("cache_hits"), int)
        and not isinstance(record.get("cache_hits"), bool)
    )
    cache_misses = sum(
        int(record.get("cache_misses", 0))
        for record in batches
        if isinstance(record.get("cache_misses"), int)
        and not isinstance(record.get("cache_misses"), bool)
    )
    interval_records = [
        record for record in batches
        if isinstance(record.get("interval_count"), int)
        and record.get("interval_count", 0) > 0
    ]
    intervals: dict[str, Any] = {
        "count": sum(int(record["interval_count"]) for record in interval_records),
        "min_ms": min(
            (float(record["interval_ms_min"]) for record in interval_records
             if isinstance(record.get("interval_ms_min"), (int, float))),
            default=None,
        ),
        "max_ms": max(
            (float(record["interval_ms_max"]) for record in interval_records
             if isinstance(record.get("interval_ms_max"), (int, float))),
            default=None,
        ),
        "avg_ms": None,
        "p50_ms": None,
    }
    weighted_intervals = [
        (int(record["interval_count"]), float(record["interval_ms_avg"]))
        for record in interval_records
        if isinstance(record.get("interval_ms_avg"), (int, float))
    ]
    if weighted_intervals:
        total = sum(count for count, _ in weighted_intervals)
        intervals["avg_ms"] = round(
            sum(count * average for count, average in weighted_intervals) / total,
            2,
        )
    p50_values = [
        float(record["interval_ms_p50"]) for record in interval_records
        if isinstance(record.get("interval_ms_p50"), (int, float))
    ]
    # A p50 cannot be reconstructed exactly from already aggregated batches;
    # expose it only for the common single-connection/single-batch case.
    if len(interval_records) == 1 and p50_values:
        intervals["p50_ms"] = round(p50_values[0], 2)
    repeated_batches = [
        record for record in batches
        if isinstance(record.get("request_count"), int)
        and not isinstance(record.get("request_count"), bool)
        and record.get("request_count", 0) >= 2
    ]
    # A one-request batch only proves a cache miss.  It must not satisfy the
    # "all later requests were hits" claim through vacuous truth.
    per_batch_cache_verified = bool(repeated_batches) and len(
        repeated_batches
    ) == len(batches) and all(
        isinstance(record.get("request_count"), int)
        and isinstance(record.get("cache_misses"), int)
        and isinstance(record.get("cache_hits"), int)
        and record.get("cache_misses") == 1
        and record.get("cache_hits") == record.get("request_count") - 1
        for record in batches
    )

    return {
        "schema_version": 1,
        "status": "observed" if request_count else "no_data",
        "source": "server_diagnostic_log",
        "method": method,
        "request_count": request_count,
        "batch_count": len(batches),
        "individual_event_count": len(individual),
        "server_cache": {
            "observable": bool(batches),
            "hits": cache_hits,
            "misses": cache_misses,
            "repeated_batch_count": len(repeated_batches),
            "per_batch_verified": per_batch_cache_verified,
            "all_batched_requests_cached_after_first": per_batch_cache_verified,
            "all_requests_cached_after_first": bool(
                per_batch_cache_verified and not individual
            ),
        },
        "request_intervals": intervals,
        "client_behavior": {
            "cache_verified": False,
            "polling_schedule_verified": False,
            "boundary": "server logs cannot prove external client cache or notification subscription",
        },
    }


def format_record_line(record: dict[str, Any]) -> str:
    """格式化输出单条终端友好日志。"""
    ts = record.get("timestamp", "")
    level = str(record.get("level", "INFO")).ljust(7)
    event = record.get("event", "log")
    msg = record.get("message", "")
    parts = [f"[{ts}] {level} {event}"]
    if msg and msg != event:
        parts.append(f"- {msg}")

    # 提取关键指标字段
    extras: list[str] = []
    if "duration_seconds" in record:
        extras.append(f"duration={record['duration_seconds']}s")
    elif "duration_ms" in record:
        extras.append(f"duration={record['duration_ms']}ms")
    if "tokens_consumed" in record and record["tokens_consumed"] is not None:
        extras.append(f"tokens={record['tokens_consumed']}")
    if "status" in record:
        extras.append(f"status={record['status']}")
    if "phase" in record and record["phase"] != event:
        extras.append(f"phase={record['phase']}")
    if "command" in record:
        extras.append(f"cmd={record['command']}")
    if "tool_name" in record:
        extras.append(f"tool={record['tool_name']}")
    if "exit_code" in record and record["exit_code"] != 0:
        extras.append(f"exit={record['exit_code']}")
    if "decision_reason" in record and record["decision_reason"]:
        extras.append(f"reason={record['decision_reason']}")
    elif "error" in record and record["error"] and str(record["error"]) not in msg:
        extras.append(f"error={record['error']}")
    if extras:
        parts.append(f"({', '.join(extras)})")

    summary = record.get("summary")
    if isinstance(summary, dict):
        if "arguments" in summary:
            parts.append(f"\n    [Tool Args] {json.dumps(summary['arguments'], ensure_ascii=False)}")
        if "result" in summary:
            parts.append(f"\n    [Tool Result] {json.dumps(summary['result'], ensure_ascii=False)}")

    line = " ".join(parts)
    exc = record.get("exception")
    if exc:
        line += f"\n    [Exception] {exc}"
    return line


def _parse_iso_ts(ts_str: Any) -> dt.datetime | None:
    """安全解析 ISO 8601 时间戳为 datetime 对象。"""
    if not isinstance(ts_str, str) or not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return dt.datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def timing_summary(records: list[dict[str, Any]], *, trace_id: str | None = None) -> dict[str, Any]:
    """Bound collection windows and union observed intervals, never infer thought time."""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if trace_id and record.get("trace_id") != trace_id:
            continue
        if record.get("command") == "log":
            continue
        identity = str(record.get("event_id") or json.dumps(record, sort_keys=True, default=str))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(record)
    # An explicit trace is exact. Otherwise choose the latest continuous capture
    # window that contains measured work; later diagnostic reads cannot extend it.
    timed = [i for i, r in enumerate(unique) if "duration_seconds" in r or "duration_ms" in r or "tokens_consumed" in r]
    window = unique[:timed[-1] + 1] if timed else []
    if not trace_id:
        boundary, previous = 0, None
        for i, record in enumerate(window):
            stamp = _parse_iso_ts(record.get("timestamp"))
            if stamp is not None and previous is not None and (stamp - previous).total_seconds() > 1800:
                boundary = i
            if stamp is not None:
                previous = stamp
        window = window[boundary:]
    entries: list[dict[str, Any]] = []
    intervals: list[tuple[float, float]] = []
    stamps: list[float] = []
    for record in window:
        stamp = _parse_iso_ts(record.get("timestamp"))
        end = stamp.timestamp() if stamp else None
        if end is not None:
            stamps.append(end)
        raw_duration = record.get("duration_seconds")
        if raw_duration is None and "duration_ms" in record:
            raw_duration = record["duration_ms"] / 1000 if isinstance(record["duration_ms"], (int, float)) else None
        duration = float(raw_duration) if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool) and math.isfinite(raw_duration) and raw_duration >= 0 else None
        raw_tokens = record.get("tokens_consumed")
        tokens = int(raw_tokens) if isinstance(raw_tokens, (int, float)) and not isinstance(raw_tokens, bool) and math.isfinite(raw_tokens) and raw_tokens >= 0 else None
        method = record.get("token_method")
        if method == "unmeasured" or (tokens == 0 and not method):
            tokens = None
        if duration is None and tokens is None:
            continue
        if duration is not None and end is not None:
            intervals.append((end - duration, end))
        entries.append({
            "name": str(record.get("step") or record.get("gate") or record.get("phase") or record.get("event") or "unnamed"),
            "duration": duration, "tokens": tokens,
            "status": record.get("status", "ok"),
            "is_token_anomaly": tokens is not None and isinstance(record.get("token_budget"), (int, float)) and tokens > record["token_budget"],
        })
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    observed = sum(end - start for start, end in merged) if merged else None
    start = min(stamps + [i[0] for i in intervals]) if stamps else None
    end = max(stamps) if stamps else None
    wall = end - start if start is not None and end is not None else None
    measured_tokens = [entry["tokens"] for entry in entries if entry["tokens"] is not None]
    return {
        "scope": "trace" if trace_id else "latest_continuous_capture",
        "trace_id": trace_id, "window_start": start, "window_end": end,
        "window_seconds": wall, "observed_seconds": observed,
        "unobserved_seconds": max(0, wall - observed) if wall is not None and observed is not None else None,
        "tokens_consumed": sum(measured_tokens) if measured_tokens else None,
        "token_method": "logged_events" if measured_tokens else "unmeasured",
        "missing_token_events": sum(entry["tokens"] is None for entry in entries),
        "entries": entries,
    }


def render_waterfall(root: Path, *, trace_id: str | None = None) -> str:
    records = get_log_records(root)
    if not records:
        return "未找到任何诊断日志（.bugx/logs/bugx.jsonl 为空或不存在）。"
    summary = timing_summary(records, trace_id=trace_id)
    entries = summary["entries"]
    if not entries:
        return "未找到阶段耗时或 Token 记录（请先执行一次 pre/workers/post 等命令）。"
    title = "阶段双轨瀑布图 (Timing & Tokens)" if summary["tokens_consumed"] is not None else "阶段耗时瀑布图"
    lines = ["=" * 80, f"📊 {title} · {len(entries)} 个观测阶段", f"  范围: {summary['scope']} · trace={trace_id or '未指定'}"]
    if summary["window_seconds"] is not None:
        observed = f"{summary['observed_seconds']:.3f}s" if summary["observed_seconds"] is not None else "unmeasured"
        gap = f"{summary['unobserved_seconds']:.3f}s" if summary["unobserved_seconds"] is not None else "unmeasured"
        lines.append(f"  采集窗口: {summary['window_seconds']:.3f}s · 观测区间并集: {observed}")
        lines.append(f"  未观测间隔: {gap}（不能据此推断宿主思考或网络耗时）")
    else:
        lines.append("  墙钟与重叠: unmeasured（缺少时间戳，不将阶段累计冒充墙钟）")
    token_text = f"{summary['tokens_consumed']:,}" if summary["tokens_consumed"] is not None else "unmeasured"
    lines.append(f"  Token: {token_text} · 缺失测量阶段: {summary['missing_token_events']}")
    lines.append("  阶段允许嵌套；累计阶段时长不代表用户等待。默认窗口按超过 30 分钟的采集中断分段。")
    maximum = max((entry["duration"] or 0 for entry in entries), default=1) or 1
    for entry in entries[-30:]:
        duration = entry["duration"]
        filled = max(1, round(duration / maximum * 20)) if duration else 0
        bar = "█" * filled + "░" * (20 - filled)
        duration_text = f"{duration:.3f}s" if duration is not None else "unmeasured"
        tokens = f"{entry['tokens']:,} tok" if entry["tokens"] is not None else "unmeasured"
        flag = "✓" if entry["status"] in {"ok", "success", "passed"} else "✗"
        if entry["is_token_anomaly"]:
            flag += " ⚠超标"
        lines.append(f"{entry['name'][:32]:32} [{duration_text:>11}] {bar} [{tokens}] {flag}")
    if len(entries) > 30:
        lines.append(f"  表格仅显示末 30 项；汇总覆盖窗口内全部 {len(entries)} 项。")
    lines.append("=" * 80)
    return "\n".join(lines)


def render_token_ranking(root: Path) -> str:
    """按 Token 消耗绝对量输出各阶段贡献度排行榜 (Pareto Ranking)。"""
    records = get_log_records(root)
    if not records:
        return "未找到任何诊断日志（.bugx/logs/bugx.jsonl 为空或不存在）。"

    phase_tokens: dict[str, dict[str, Any]] = {}
    total_tokens = 0
    for r in records:
        tok = r.get("tokens_consumed")
        if isinstance(tok, (int, float)) and tok > 0:
            name = r.get("phase") or r.get("event") or "unnamed"
            tok_int = int(tok)
            if name not in phase_tokens:
                phase_tokens[name] = {"tokens": 0, "count": 0, "categories": {}}
            phase_tokens[name]["tokens"] += tok_int
            phase_tokens[name]["count"] += 1
            breakdown = r.get("token_breakdown")
            if isinstance(breakdown, dict):
                for cat, count in breakdown.items():
                    if isinstance(count, int):
                        phase_tokens[name]["categories"][cat] = (
                            phase_tokens[name]["categories"].get(cat, 0) + count
                        )
            total_tokens += tok_int

    if total_tokens <= 0:
        return "未找到任何 Token 消耗记录（所有阶段未产生模型交互或 Token 均为 0）。"

    sorted_phases = sorted(phase_tokens.items(), key=lambda x: x[1]["tokens"], reverse=True)
    lines = [
        "=" * 74,
        f"🏆 阶段 Token 消耗排行榜 (Top Token Consumers) · 累计: {total_tokens:,} Tokens",
        "-" * 74,
        f"{'排名':<4} {'阶段名称':<22} {'消耗 Tokens':<14} {'单项占比':<10} {'累积占比':<10} {'Token 分布条'}",
        "-" * 74,
    ]

    running_tokens = 0
    bar_width = 16
    max_phase_tokens = sorted_phases[0][1]["tokens"] if sorted_phases else 1
    for rank, (name, data) in enumerate(sorted_phases, 1):
        tok = data["tokens"]
        running_tokens += tok
        pct = (tok / total_tokens * 100.0) if total_tokens > 0 else 0.0
        cum_pct = (running_tokens / total_tokens * 100.0) if total_tokens > 0 else 0.0
        filled = int(round((tok / max_phase_tokens) * bar_width)) if max_phase_tokens > 0 else 0
        filled = max(1 if tok > 0 else 0, min(bar_width, filled))
        bar = "█" * filled + "░" * (bar_width - filled)
        cat_notes = []
        for cat, cnt in sorted(data["categories"].items(), key=lambda x: -x[1]):
            cat_notes.append(f"{cat}:{cnt}")
        sub_desc = f" ({', '.join(cat_notes)})" if cat_notes else ""
        lines.append(
            f"#{rank:<3} {name[:20]:<22} {tok:>10,} tok   {pct:>6.1f}%    {cum_pct:>6.1f}%    {bar}{sub_desc}"
        )

    lines.append("=" * 74)
    return "\n".join(lines)


def bundle_diagnostics(root: Path, output_file: Path | None = None) -> Path:
    """生成脱敏诊断打包文件 (.tar.gz)。"""
    root = root.resolve()
    logs_dir = root / ".bugx" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(logs_dir, 0o700)

    now_str = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if output_file is None:
        target_path = logs_dir / f"bugx-diagnostics-{now_str}.tar.gz"
    else:
        target_path = Path(output_file).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bugx-bundle-") as tmp_dir:
        tmp = Path(tmp_dir)

        # 1. 抓取最新 100 条日志并脱敏
        raw_records = get_log_records(root, max_lines=100)
        sanitized_records = [safe_value(r) for r in raw_records]
        log_out = tmp / "bugx_recent.jsonl"
        with log_out.open("w", encoding="utf-8") as f:
            for r in sanitized_records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        # 2. 抓取运行态 state.json (若存在) 并脱敏
        state_path = root / ".bugx" / "runtime" / "state.json"
        if state_path.is_file():
            try:
                state_data = json.loads(state_path.read_text(encoding="utf-8"))
                safe_state = safe_value(state_data)
                (tmp / "runtime_state.json").write_text(
                    json.dumps(safe_state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass

        # 3. 收集环境与 Git 信息
        git_commit = "unknown"
        git_branch = "unknown"
        try:
            p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                               capture_output=True, text=True, timeout=5)
            if p.returncode == 0:
                git_commit = p.stdout.strip()
            p_b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root),
                                 capture_output=True, text=True, timeout=5)
            if p_b.returncode == 0:
                git_branch = p_b.stdout.strip()
        except Exception:
            pass

        env_info = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "os_name": os.name,
            "git_branch": git_branch,
            "git_commit": git_commit,
            "project_root": str(root),
        }
        (tmp / "environment.json").write_text(
            json.dumps(env_info, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 4. 打包 tar.gz
        with tarfile.open(target_path, "w:gz") as tar:
            for item in tmp.iterdir():
                tar.add(item, arcname=f"diagnostics/{item.name}")

    os.chmod(target_path, 0o600)
    return target_path


def _scan_daemon_anomalies(root: Path) -> list[dict[str, Any]]:
    """扫描 daemon.log 中的空转、告警与异常事件。

    daemon.log 的格式为 ``[timestamp] message``，不像 bugx.jsonl 是结构化日志。
    本函数逐行扫描，检测：
    - 反复出现的 "等待 Supervisor 状态就绪"（空转）
    - 巡检告警
    - 硬熔断
    - 任何包含"异常"/"失败"/"告警"的行
    """
    daemon_log = root / ".bugx" / "logs" / "daemon.log"
    if not daemon_log.is_file():
        return []
    anomalies: list[dict[str, Any]] = []
    patrol_wait_count = 0
    patrol_wait_first: str | None = None
    patrol_wait_last: str | None = None
    try:
        with open(daemon_log, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # 提取时间戳
                ts = line[1:20] if line.startswith("[") else ""
                msg = line[20:].strip() if line.startswith("[") else line

                if "等待 Supervisor 状态就绪" in msg:
                    patrol_wait_count += 1
                    if patrol_wait_first is None:
                        patrol_wait_first = ts
                    patrol_wait_last = ts
                    continue

                if any(kw in msg for kw in (
                    "告警", "异常", "失败", "熔断", "崩溃",
                    "自愈失败", "沙盒告警", "检测告警",
                )):
                    anomalies.append({
                        "type": "daemon_anomaly",
                        "timestamp": ts,
                        "message": msg,
                    })

        if patrol_wait_count > 0:
            anomalies.append({
                "type": "daemon_idle",
                "count": patrol_wait_count,
                "first_seen": patrol_wait_first,
                "last_seen": patrol_wait_last,
                "message": (
                    f"守护进程空转 {patrol_wait_count} 次"
                    "（等待 Supervisor 状态就绪），"
                    "可能原因：无 host agent 创建运行态"
                ),
            })
    except OSError:
        pass
    return anomalies


def render_timeline(root: Path, max_events: int = 60) -> str:
    """按时间先后回放每一步操作、对话与门禁评估 (DOC-169)。"""
    records = get_log_records(root)
    if not records:
        return "未找到任何诊断日志（.bugx/logs/bugx.jsonl 为空或不存在）。"

    timeline_items: list[str] = []
    for r in records:
        ts = r.get("timestamp", "")
        event = r.get("event", "")
        level = r.get("level", "INFO")
        build_id = r.get("build_id", "")
        b_str = f" [B#{build_id}]" if build_id else ""

        if event == "dialogue_message":
            role = str(r.get("role", "unknown")).upper()
            content = str(r.get("content_summary", r.get("message", "")))
            interaction = r.get("interaction", "")
            i_str = f" (type={interaction})" if interaction else ""
            timeline_items.append(f"[{ts}]{b_str} 💬 DIALOGUE [{role}]{i_str}: {content}")
        elif event == "step_finished":
            step = r.get("step", r.get("phase", "unnamed"))
            dur_ms = r.get("duration_ms", 0)
            status = r.get("status", "ok")
            cmd = r.get("command", "")
            c_str = f" cmd={cmd}" if cmd else ""
            icon = "✓" if status == "ok" else "✗"
            timeline_items.append(f"[{ts}]{b_str} ⚙️ STEP [{step}] {icon} ({dur_ms}ms){c_str}")
        elif event == "gate_finished":
            gate = r.get("gate_name", r.get("gate", r.get("phase", "unnamed")))
            dur_ms = r.get("duration_ms", 0)
            status = r.get("status", "ok")
            icon = "✓" if status == "ok" else "✗"
            timeline_items.append(f"[{ts}]{b_str} ⏱️ GATE [{gate}] {icon} ({dur_ms}ms)")
        elif event == "slow_command_warning":
            cmd = r.get("command", "")
            dur_ms = r.get("duration_ms", 0)
            thresh = r.get("threshold_ms", 3000)
            timeline_items.append(f"[{ts}]{b_str} ⚠️ SLOW OPERATION: {cmd} ({dur_ms}ms > {thresh}ms 阈值)")
        elif event in {"run_started", "run_completed", "run_failed"}:
            timeline_items.append(f"[{ts}]{b_str} 🚀 LIFECYCLE [{event}]: {r.get('message', '')}")
        elif level in {"WARNING", "ERROR", "CRITICAL"}:
            timeline_items.append(f"[{ts}]{b_str} 🚨 {level}: {r.get('message', '')} {r.get('exception', '')}".strip())

    if not timeline_items:
        return "未匹配到任何对话、步骤或门禁事件（日志中无 timeline 事件）。"

    shown = timeline_items[-max_events:] if max_events > 0 else timeline_items
    lines = [
        "=" * 80,
        f"📜 全链路操作与对话时间轴回放 (Timeline Replay) · 最近 {len(shown)} 步",
        "-" * 80,
    ]
    lines.extend(shown)
    lines.append("=" * 80)
    return "\n".join(lines)


def detect_anomalies(root: Path) -> dict[str, list[dict[str, Any]]]:
    """智能审计日志中的慢操作、违规全盘搜索反模式与死循环自旋 (DOC-169)。"""
    records = get_log_records(root)
    anomalies: dict[str, list[dict[str, Any]]] = {
        "slow_operations": [],
        "forbidden_scans": [],
        "command_loops": [],
        "errors": [],
    }
    if not records:
        return anomalies

    recent_cmds: list[str] = []
    _FORBIDDEN_SCAN_PATTERNS = ["find /", "find ~", "find /Users", "find $HOME", "grep -r /", "grep -r ~"]

    for r in records:
        event = r.get("event", "")
        cmd = str(r.get("command", "")).strip()
        dur_ms = r.get("duration_ms")
        dur_sec = r.get("duration_seconds")
        if dur_ms is None and dur_sec is not None:
            dur_ms = int(dur_sec * 1000)

        # 1. 慢操作判定 (>3000ms 或显式告警)
        if (dur_ms is not None and dur_ms >= 3000) or event == "slow_command_warning":
            anomalies["slow_operations"].append({
                "timestamp": r.get("timestamp", ""),
                "event": event,
                "command": cmd or r.get("step") or r.get("gate") or "unnamed",
                "duration_ms": dur_ms,
                "message": r.get("message", ""),
            })

        # 2. 违规跨目录/全盘扫描反模式
        if cmd:
            for pat in _FORBIDDEN_SCAN_PATTERNS:
                if pat in cmd:
                    anomalies["forbidden_scans"].append({
                        "timestamp": r.get("timestamp", ""),
                        "command": cmd,
                        "pattern": pat,
                        "reason": f"命中禁止跨目录/全盘扫描模式 '{pat}'，极易造成数分钟超时黑洞",
                    })
                    break

            # 3. 命令死循环自旋判定（连续 3 次执行完全相同命令）
            recent_cmds.append(cmd)
            if len(recent_cmds) >= 3:
                last_three = recent_cmds[-3:]
                if len(set(last_three)) == 1:
                    anomalies["command_loops"].append({
                        "timestamp": r.get("timestamp", ""),
                        "command": last_three[0],
                        "repeat_count": 3,
                        "reason": f"连续 3 次执行相同命令 '{last_three[0]}'，疑似进入死循环重试",
                    })

        # 4. 严重异常与非零退出
        level = str(r.get("level", "")).upper()
        if level in {"ERROR", "CRITICAL"} or r.get("status") == "failed":
            anomalies["errors"].append({
                "timestamp": r.get("timestamp", ""),
                "event": event,
                "level": level,
                "message": r.get("message", ""),
                "exception": r.get("exception", ""),
            })

    return anomalies


def render_anomalies(root: Path) -> str:
    """格式化终端输出异常模式审计报告 (DOC-169)。"""
    data = detect_anomalies(root)
    total_issues = (
        len(data["slow_operations"])
        + len(data["forbidden_scans"])
        + len(data["command_loops"])
        + len(data["errors"])
    )
    lines = [
        "=" * 80,
        f"🔍 耗时与策略异常智能审计报告 (Anomaly Audit) · 发现 {total_issues} 处异常",
        "-" * 80,
    ]
    if total_issues == 0:
        lines.append("✓ 未发现慢命令（>3s）、全盘搜索反模式或死循环自旋，运行状态健康。")
        lines.append("=" * 80)
        return "\n".join(lines)

    if data["forbidden_scans"]:
        lines.append(f"🚨 [高危耗时黑洞] 违规跨目录/全盘扫描命令 ({len(data['forbidden_scans'])} 项):")
        for it in data["forbidden_scans"]:
            lines.append(f"  - [{it['timestamp']}] {it['command']}")
            lines.append(f"    原因: {it['reason']}")

    if data["slow_operations"]:
        lines.append(f"\n⏱️ [慢操作排查] 耗时超出 3000ms 阈值 ({len(data['slow_operations'])} 项):")
        for it in data["slow_operations"]:
            dur_str = f"{it['duration_ms']}ms" if it.get("duration_ms") else "超限"
            lines.append(f"  - [{it['timestamp']}] {it['command']} (耗时: {dur_str})")

    if data["command_loops"]:
        lines.append(f"\n🔄 [死循环重试] 连续执行相同命令自旋 ({len(data['command_loops'])} 项):")
        for it in data["command_loops"]:
            lines.append(f"  - [{it['timestamp']}] {it['command']}")
            lines.append(f"    提示: {it['reason']}")

    if data["errors"]:
        lines.append(f"\n❌ [执行与门禁失败] 错误记录 ({len(data['errors'])} 项):")
        for it in data["errors"]:
            msg = it['message'] or it.get('exception') or it['event']
            lines.append(f"  - [{it['timestamp']}] {it['level']}: {msg}")

    lines.append("=" * 80)
    lines.append("💡 优化修复建议：")
    if data["forbidden_scans"]:
        lines.append("  1. 立即修正 Bash 策略，限定在项目根目录下定向搜索，禁止搜索 /Users、/、~。")
    if data["slow_operations"]:
        lines.append("  2. 查看慢命令执行参数，加设 --maxdepth 或改用 Git 索引/codebase-memory-mcp 知识图谱。")
    if data["command_loops"]:
        lines.append("  3. 针对重复失败命令实施 1 轮 fail-fast，遇阻断立即输出阻塞原因，禁止盲目重试。")
    lines.append("=" * 80)
    return "\n".join(lines)


def run_log_command(root: Path, args: Any) -> int:
    """CLI 处理入口。"""
    if getattr(args, "mcp_polling", False):
        report = mcp_polling_report(root)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if getattr(args, "timeline", False) or getattr(args, "replay", False):
        timeline_text = render_timeline(root)
        if getattr(args, "json", False):
            print(json.dumps({"timeline": timeline_text}, ensure_ascii=False))
        else:
            print(timeline_text)
        return 0

    if getattr(args, "anomalies", False):
        # 智能审计：合并结构化日志异常和 daemon 日志异常
        records = filter_errors(root)
        daemon_anomalies = _scan_daemon_anomalies(root)
        all_anomalies = list(records) + daemon_anomalies
        if not all_anomalies:
            print("[bugx log] ✓ 未发现异常或告警")
            return 0
        print(f"=== 异常与告警审计 (共 {len(all_anomalies)} 条) ===")
        for r in all_anomalies:
            if isinstance(r, dict) and r.get("type") == "daemon_idle":
                print(f"  [空转] {r['message']}")
                if r.get("first_seen"):
                    print(f"         首次: {r['first_seen']}，末次: {r.get('last_seen', '-')}")
            elif isinstance(r, dict) and r.get("type") == "daemon_anomaly":
                print(f"  [守护进程] [{r.get('timestamp', '')}] {r.get('message', '')}")
            else:
                print(format_record_line(r))
        return 0

    if getattr(args, "bundle", False):
        out_file = Path(args.output) if getattr(args, "output", None) else None
        bundle_path = bundle_diagnostics(root, output_file=out_file)
        print(f"[bugx log] ✓ 诊断包已生成: {bundle_path}")
        return 0

    if getattr(args, "tokens", False):
        ranking_text = render_token_ranking(root)
        print(ranking_text)
        return 0

    if getattr(args, "timing", False) or getattr(args, "waterfall", False):
        trace_id = getattr(args, "trace", None)
        if getattr(args, "json", False):
            print(json.dumps(timing_summary(get_log_records(root), trace_id=trace_id), ensure_ascii=False))
        else:
            print(render_waterfall(root, trace_id=trace_id))
        return 0

    if getattr(args, "trace", None):
        trace_id = args.trace
        records = filter_trace(root, trace_id)
        if not records:
            print(f"[bugx log] 未找到 trace_id={trace_id} 的相关日志")
            return 0
        print(f"=== 链路追踪 Trace: {trace_id} (共 {len(records)} 条) ===")
        for r in records:
            print(format_record_line(r))
        return 0

    if getattr(args, "errors", False):
        records = filter_errors(root)
        if not records:
            print("[bugx log] ✓ 未发现异常或错误日志")
            return 0
        print(f"=== 错误与告警日志 (共 {len(records)} 条) ===")
        for r in records:
            print(format_record_line(r))
        return 0

    if getattr(args, "watch", False) or getattr(args, "follow", False):
        return stream_watch(root)

    # 默认执行 tail
    tail_n = getattr(args, "tail", None)
    n = int(tail_n) if tail_n is not None else 20
    records = tail_logs(root, n=n)
    if not records:
        print(f"[bugx log] 日志文件为空或不存在: {log_path(root)}")
        return 0
    print(f"=== 最新 {len(records)} 条诊断日志 ===")
    for r in records:
        print(format_record_line(r))
    return 0


__all__ = [
    "bundle_diagnostics",
    "detect_anomalies",
    "filter_errors",
    "filter_trace",
    "format_record_line",
    "get_log_records",
    "mcp_polling_report",
    "render_anomalies",
    "render_timeline",
    "render_token_ranking",
    "render_waterfall",
    "run_log_command",
    "stream_watch",
    "tail_logs",
]
