from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..common import _ensure_core_importable

PROJECT_LABEL_COLUMN_WIDTH = 20


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m ({seconds:.0f}s)"
    hours = minutes / 60.0
    return f"{hours:.1f}h ({minutes:.0f}m)"


def format_tokens(tokens: int) -> str:
    return f"{tokens:,}"


def command_stats(args: argparse.Namespace) -> int:
    """bugx stats：全局与跨项目时间与 Token 计量统计看板。"""
    days = getattr(args, "days", 30) or 30
    if days <= 0:
        days = 30
    as_json = bool(getattr(args, "json", False))
    global_mode = bool(getattr(args, "global_mode", True))
    db_path = getattr(args, "db_path", None)

    try:
        _ensure_core_importable()
        from core.runtime import global_telemetry
    except Exception as exc:
        if as_json:
            print(json.dumps({"status": "error", "error": f"无法加载全局遥测模块: {exc}"}, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx stats] ✗ 无法加载全局遥测模块: {exc}")
        return 1

    try:
        summary = global_telemetry.query_global_summary(days=days, db_path=db_path)
        projects = global_telemetry.query_project_breakdown(days=days, db_path=db_path)
        daily = global_telemetry.query_daily_rollup(days=days, db_path=db_path)
    except Exception as exc:
        if as_json:
            print(json.dumps({"status": "error", "error": f"查询全局遥测数据失败: {exc}"}, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx stats] ✗ 查询全局遥测数据失败: {exc}")
        return 1

    if as_json:
        payload = {
            "status": "ok",
            "global_mode": global_mode,
            "days": days,
            "summary": summary,
            "projects": projects,
            "daily_rollup": daily,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # 终端看板渲染
    print("=" * 72)
    print("                 BugX 全局时间与 Token 计量治理看板")
    print("=" * 72)
    print(f"统计周期: 近 {days} 天")
    print()

    if summary.get("total_sessions", 0) == 0 and not projects:
        print("暂无全局遥测数据（尚未产生审计 Session 或数据已归档）。")
        print("=" * 72)
        return 0

    print("【全局指标概览】")
    print(f"  - 纳管工程总数: {summary.get('projects_count', 0)}")
    print(f"  - 累计审计会话: {summary.get('total_sessions', 0)} 次")
    print(f"  - 跨项目总耗时: {format_duration(summary.get('total_duration_seconds', 0.0))}")
    print(f"  - 全局消耗 Token: {format_tokens(summary.get('total_tokens', 0))}")
    print(f"  - 发现缺陷总数: {summary.get('total_findings', 0)}")
    print(f"  - 计命有效缺陷: {summary.get('total_credited', 0)}")
    sec_cred = summary.get("avg_seconds_per_credited")
    tok_cred = summary.get("avg_tokens_per_credited")
    sec_cred_str = f"{sec_cred:.1f}s / 计命 Bug" if sec_cred is not None else "-"
    tok_cred_str = f"{tok_cred:,.0f} / 计命 Bug" if tok_cred is not None else "-"
    print(f"  - 平均缺陷耗时: {sec_cred_str}")
    print(f"  - 平均缺陷 Token: {tok_cred_str}")
    print()

    if projects:
        print("【项目投入产出与成本排行 (ROI)】")
        print(f"  {'工程名称':<{PROJECT_LABEL_COLUMN_WIDTH}} {'Session':<9} {'总耗时':<12} {'Token 消耗':<14} {'计命数':<8} {'单 Bug 成本'}")
        print("  " + "-" * 70)
        for p in projects:
            p_name = str(p.get("project_name") or "unknown")
            if len(p_name) > PROJECT_LABEL_COLUMN_WIDTH:
                p_name = p_name[:PROJECT_LABEL_COLUMN_WIDTH - 3] + "..."
            s_cnt = str(p.get("session_count", 0))
            dur = f"{p.get('total_duration_seconds', 0.0):.1f}s"
            tok = format_tokens(p.get("total_tokens", 0))
            cred = str(p.get("total_credited", 0))
            p_sec = p.get("seconds_per_credited")
            p_tok = p.get("tokens_per_credited")
            if p_sec is not None and p_tok is not None:
                cost_str = f"{p_sec:.1f}s / {p_tok:,.0f} tok"
            elif p_sec is not None:
                cost_str = f"{p_sec:.1f}s"
            else:
                cost_str = "-"
            print(f"  {p_name:<{PROJECT_LABEL_COLUMN_WIDTH}} {s_cnt:<9} {dur:<12} {tok:<14} {cred:<8} {cost_str}")
        print()

    if daily:
        print(f"【近 {min(len(daily), 7)} 日消耗走势】")
        print(f"  {'日期':<14} {'耗时':<12} {'Token 消耗':<14} {'会话数':<8} {'计命数'}")
        print("  " + "-" * 60)
        for d in daily[-7:]:
            d_date = d.get("date", "")
            d_dur = f"{d.get('total_duration_seconds', 0.0):.1f}s"
            d_tok = format_tokens(d.get("total_tokens", 0))
            d_sess = str(d.get("session_count", 0))
            d_cred = str(d.get("credited_count", 0))
            print(f"  {d_date:<14} {d_dur:<12} {d_tok:<14} {d_sess:<8} {d_cred}")
        print()

    print("=" * 72)
    return 0
