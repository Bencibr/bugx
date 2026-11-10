#!/usr/bin/env python3
"""bugx_cli/commands/daemon.py - 常驻守护进程与无头自治挖掘控制面。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def command_daemon(args: argparse.Namespace) -> int:
    """管理 BugX 自治持续挖掘守护进程。"""
    root = getattr(args, "root", Path.cwd()).resolve()
    action = getattr(args, "daemon_action", "status")

    try:
        from core.runtime import daemon_runner
    except ImportError:
        # 源码树不可导入时（例如仅装了全局引擎），退到全局引擎快照。
        # ``core/`` 是命名空间包，父目录必须在 sys.path 上。
        global_core = Path.home() / ".bugx" / "core"
        for entry in (global_core.parent, global_core):
            if str(entry) not in sys.path:
                sys.path.insert(0, str(entry))
        from core.runtime import daemon_runner

    max_wall_time = getattr(args, "max_wall_time", 14400.0)
    interval = getattr(args, "interval", 5.0)
    runner = daemon_runner.DaemonRunner(
        root=root,
        max_wall_time=max_wall_time,
        interval=interval,
    )

    if action == "start":
        foreground = bool(getattr(args, "foreground", False))
        return runner.start(foreground=foreground)
    elif action == "stop":
        force = bool(getattr(args, "force", False))
        return runner.stop(force=force)
    elif action == "status":
        as_json = bool(getattr(args, "json", False))
        return runner.status(as_json=as_json)
    elif action == "attach":
        lines = int(getattr(args, "lines", 50))
        return runner.attach(lines=lines)
    else:
        print(f"[BugX Daemon] 未知动作: {action}", file=sys.stderr)
        return 1
