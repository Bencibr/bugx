"""BugX Goal 模式 CLI 命令入口（DOC-149）。

参照 Claude Code /goal 与 Ralph Loop 原语，提供目标导向长程自愈无人值守运行机制：
- 目标收敛判定（目标覆盖率 + 零未闭环 Finding）
- 结构化失败诊断与自愈（自动续约预算 Epoch、租约自愈与反思提示词注入）
- 坏单元 3-strike 自动隔离机制
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .. import __version__
from ..common import CliError, _cli_version_environment, _ensure_core_importable
from ..infra.mcp_advisor import verify_builtin_mcp_manifest
from .autonomous import _install_host


def command_goal(args: argparse.Namespace) -> int:
    with _cli_version_environment():
        return _command_goal(args)


def _command_goal(args: argparse.Namespace) -> int:
    """运行目标导向的长程自愈自主挖掘循环。"""
    root = args.root.resolve()
    host = str(args.host)
    if not root.is_dir():
        raise CliError(f"项目根目录不存在: {root}")

    verify_builtin_mcp_manifest()
    install = _install_host(root, host)
    if install["status"] != "ok":
        payload = {"status": "blocked", "reason": install, "host": host}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx] BLOCKED: {install.get('detail') or install.get('reason')}")
        return 1

    _ensure_core_importable()
    from core.governance.goal_governor import GoalSpec
    from core.supervisor.supervisor_host import AUTONOMOUS_HOST_PROMPT, _ADAPTERS, run_host_autonomous

    from core.runtime import provider_window  # 审计 #35：限流窗口跨进程生效

    if host not in _ADAPTERS:
        raise CliError(f"不支持的自治宿主: {host} (可选: {', '.join(sorted(_ADAPTERS.keys()))})")

    spec = GoalSpec(
        description=getattr(args, "target_description", "") or "全量覆盖与漏洞闭环",
        target_coverage=float(args.target_coverage),
        require_zero_open_findings=not getattr(args, "allow_open_findings", False),
        allow_quarantined_completion=not getattr(args, "disallow_quarantine", False),
        max_wall_time=float(args.max_wall_time),
        max_turns=int(args.max_turns),
        max_unit_retries=int(args.max_unit_retries),
        max_consecutive_transient_failures=int(args.max_transient_retries),
    )

    window = provider_window.active(root)
    if window is not None:
        print(
            f"[bugx:goal] BLOCKED: provider_rate_limited（限流窗口内，剩余 "
            f"{window['remaining_seconds']}s；确需提前重跑请删除 "
            f".bugx/state/provider-window.json）"
        )
        return 2

    adapter = _ADAPTERS[host]()
    if not getattr(args, "json", False):
        print(f"[bugx:goal] 启动 Goal 模式: 目标覆盖率 {spec.target_coverage:.1f}%, "
              f"零遗留漏洞={spec.require_zero_open_findings}, 宿主={host}")

    result = run_host_autonomous(
        root,
        adapter,
        max_turns=spec.max_turns,
        max_wall_time=spec.max_wall_time,
        agent_timeout=float(args.agent_timeout),
        prompt=AUTONOMOUS_HOST_PROMPT,
        goal_spec=spec,
    )
    if result.get("reason") == "provider_rate_limited":
        provider_window.record(
            root, result.get("retry_window_seconds"), reason="provider_rate_limited",
        )
    result.update({"host": host, "install": install, "goal_spec": spec.to_dict()})

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        status = str(result.get("status", "blocked")).upper()
        reason = str(result.get("reason", ""))
        progress = result.get("progress")
        prog_summary = ""
        if isinstance(progress, dict):
            prog_summary = (
                f" (覆盖率 {progress.get('coverage_percent', 0):.1f}%, "
                f"未闭环漏洞 {progress.get('open_findings', 0)})"
            )
        print(f"[bugx:goal] {status}: {reason}{prog_summary}")

    return 0 if result.get("status") == "complete" else 1
