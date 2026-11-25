"""BugX Issue Reporting & Diagnostic Feedback CLI Commands.

Provides `bugx issue report` and `bugx feedback` commands to collect conversation
history, error logs, and environment context, scrub sensitive tokens, and report
to BugX Server for diagnostics and bug resolution.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..common import _ensure_core_importable


def register_issue_parser(target: Any) -> Any:
    """注册 issue 与 feedback 命令行解析器。

    兼容两种调用形式：
    1. 传入 subparsers (_SubParsersAction, CLI 模块化集成模式): 注册 issue 与 feedback 子命令。
    2. 传入 parser (ArgumentParser, 独立测试用例模式): 注册 report/issue/feedback 子解析器。
    """
    if hasattr(target, "add_subparsers") and not hasattr(target, "add_parser"):
        sub = target.add_subparsers(dest="issue_command")
        report_parser = sub.add_parser(
            "report",
            help="采集最近会话现场与错误日志并上报至 BugX 服务端",
        )
        _add_report_arguments(report_parser)
        report_parser.set_defaults(handler=command_issue)

        issue_parser = sub.add_parser(
            "issue",
            help="问题上报与会话异常诊断（将对话现场脱敏后提交至 BugX 服务端排查）",
        )
        _setup_issue_subparsers(issue_parser)
        issue_parser.set_defaults(handler=command_issue)

        feedback_parser = sub.add_parser(
            "feedback",
            help="问题与会话现场快速反馈（等同于 bugx issue report）",
        )
        _add_report_arguments(feedback_parser)
        feedback_parser.set_defaults(handler=command_feedback)

        return sub

    parser = target.add_parser(
        "issue",
        help="问题上报与会话异常诊断（将对话现场脱敏后提交至 BugX 服务端排查）",
    )
    _setup_issue_subparsers(parser)
    parser.set_defaults(handler=command_issue)

    # 别名 feedback：直接挂载为顶层快捷命令
    feedback_parser = target.add_parser(
        "feedback",
        help="问题与会话现场快速反馈（等同于 bugx issue report）",
    )
    _add_report_arguments(feedback_parser)
    feedback_parser.set_defaults(handler=command_feedback)

    return parser


def _setup_issue_subparsers(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="issue_command")
    report_parser = sub.add_parser(
        "report",
        help="采集最近会话现场与错误日志并上报至 BugX 服务端",
    )
    _add_report_arguments(report_parser)
    report_parser.set_defaults(handler=command_issue)


def _add_report_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="项目根路径（默认当前工作目录或 BUGX_PROJECT_ROOT）",
    )
    parser.add_argument(
        "--session",
        dest="session_id",
        default=None,
        help="指定上报的会话 ID；省略时自动探测最近一次发生异常的会话",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="问题简要标题（省略时从错误信息或交互式提示中提取）",
    )
    parser.add_argument(
        "--desc",
        default="",
        help="问题详细现象或复现步骤补充说明",
    )
    parser.add_argument(
        "--no-logs",
        action="store_false",
        dest="include_logs",
        default=True,
        help="不附带 .bugx/logs/ 核心日志切片",
    )
    parser.add_argument(
        "--mask-source",
        action="store_true",
        default=False,
        help="对会话中出现的源代码块强制遮蔽为 [SOURCE_CODE_REDACTED]",
    )
    parser.add_argument(
        "--anonymous",
        action="store_true",
        default=False,
        help="匿名上报，不携带设备指纹与账号信息",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="仅在本地提取并打印脱敏后的上报数据包，不发起网络请求",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=False,
        help="跳过终端确认提示，直接上报",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help="自定义 BugX 服务端 API 根地址 (如 https://bugx.dev)",
    )


def command_feedback(args: argparse.Namespace) -> int:
    """feedback 顶层别名处理器，直接转发至 command_issue。"""
    return command_issue(args)


def command_issue(args: argparse.Namespace) -> int:
    """执行问题现场收集与上报主流程。"""
    _ensure_core_importable()
    from core.cloud_sync.issue_uploader import (
        build_issue_payload,
        prepare_issue_gzip_package,
        upload_issue,
    )

    project_root = args.root or Path(os.environ.get("BUGX_PROJECT_ROOT", Path.cwd()))
    title = args.title

    # 若未指定标题且在交互式终端中，提示用户输入简要标题
    if not title and sys.stdin.isatty() and not getattr(args, "yes", False):
        try:
            user_input = input("请输入遇到的问题简述 (直接回车将自动提取错误首行): ").strip()
            if user_input:
                title = user_input
        except (KeyboardInterrupt, EOFError):
            print("\n已取消上报。")
            return 1

    print(f"[*] 正在分析与提取项目现场数据: {project_root}...")

    try:
        payload, redacted_categories = build_issue_payload(
            project_root=project_root,
            title=title or "",
            desc=args.desc,
            session_id=args.session_id,
            include_logs=args.include_logs,
            mask_source=args.mask_source,
            anonymous=args.anonymous,
        )
    except Exception as err:
        print(f"[!] 提取会话现场数据失败: {err}", file=sys.stderr)
        return 1

    compressed_bytes = prepare_issue_gzip_package(payload)
    raw_json_str = json.dumps(payload, ensure_ascii=False)
    raw_size_kb = len(raw_json_str.encode("utf-8")) / 1024.0
    comp_size_kb = len(compressed_bytes) / 1024.0

    session_info = payload.get("session_id") or "未关联会话 (纯日志与环境)"
    error_summary = payload.get("error_message") or payload.get("title") or "无"
    log_lines = len(payload.get("logs_slice") or [])

    # 打印脱敏审计摘要
    print("\n" + "=" * 60)
    print(" 📋 BugX 会话现场诊断包信息清单 (已完成三级安全脱敏)")
    print("=" * 60)
    print(f" • 问题标题: {payload.get('title')}")
    print(f" • 关联会话: {session_info}")
    print(f" • 异常摘要: {error_summary[:80]}")
    print(f" • 宿主环境: {payload.get('host')} (OS: {payload.get('environment', {}).get('os')})")
    print(f" • 日志附带: {log_lines} 行")
    print(f" • 数据包大小: {raw_size_kb:.1f} KB (Gzip 压缩后: {comp_size_kb:.1f} KB)")
    print(f" • 隐私脱敏项: {', '.join(redacted_categories) if redacted_categories else '未检出敏感凭据 (安全)'}")
    print("=" * 60)

    # 提取前 2~3 条消息进行终端脱敏预览
    messages = []
    sess_obj = payload.get("session_data", {}).get("session") or {}
    if isinstance(sess_obj, dict):
        messages = sess_obj.get("messages") or []

    if messages:
        print("\n🔍 会话消息脱敏预览 (前 2 轮):")
        for i, msg in enumerate(messages[:2], start=1):
            role = msg.get("role", "unknown")
            content = (msg.get("content") or "").strip().replace("\n", " ")
            if len(content) > 100:
                content = content[:100] + "..."
            print(f"   [{i}] ({role}): {content}")
        print("-" * 60)

    if args.dry_run:
        print("\n💡 [Dry-Run 模式] 本地脱敏包生成完毕，已跳过网络请求。")
        print(f"数据包预览 (JSON 截断 500 字符):\n{raw_json_str[:500]}...\n")
        return 0

    # 终端确认门禁
    if sys.stdin.isatty() and not getattr(args, "yes", False):
        try:
            confirm = input("\n确认将上述脱敏后的现场诊断数据上报至 BugX 服务端？[y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("[-] 用户取消上报。")
                return 0
        except (KeyboardInterrupt, EOFError):
            print("\n[-] 已取消。")
            return 1

    print("[*] 正在通过安全通道上传现场诊断包...")
    try:
        res = upload_issue(
            payload=payload,
            api_url=args.api_base,
            timeout=30.0,
        )
    except Exception as err:
        print(f"\n[!] 上报失败: {err}", file=sys.stderr)
        return 1

    data = res.get("data") or {}
    issue_no = data.get("issue_no") or res.get("issue_no") or "已记录"
    view_url = data.get("view_url") or res.get("view_url") or ""

    print("\n" + "=" * 60)
    print(" 🎉 会话异常与诊断现场已成功提交至 BugX 服务端！")
    print(f" • 工单编号: {issue_no}")
    if view_url:
        print(f" • 追踪与回放链接: {view_url}")
    print(" • 处理进展: 我们的研发团队将根据会话回放与日志快速排查与修复。")
    print("=" * 60 + "\n")

    return 0
