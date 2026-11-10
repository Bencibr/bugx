#!/usr/bin/env python3
"""跨宿主的单选菜单控件。

优先在真实 TTY 中提供方向键单选控件；不具备终端能力时返回
``RADIO_UNAVAILABLE``，由调用方回退到编号菜单或返回结构化待决策状态。
该模块只负责选项呈现，不负责持久化或业务决策。
"""

from __future__ import annotations

import curses
import os
import sys
from collections.abc import Sequence

RADIO_UNAVAILABLE = object()


def _real_tty() -> bool:
    if os.environ.get("BUGX_TEXT_MENU") == "1":
        return False
    if os.environ.get("TERM", "") in {"", "dumb"}:
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, OSError):
        return False


def select_one(
    title: str,
    options: Sequence[tuple[str, str]],
    *,
    default: int = 0,
) -> int | None | object:
    """显示单选控件并返回选项下标。

    返回 ``None`` 表示用户取消（Esc/q），返回 ``RADIO_UNAVAILABLE`` 表示
    当前宿主不支持 TTY 控件，调用方应使用自己的文本菜单回退。
    """
    if not _real_tty() or not options:
        return RADIO_UNAVAILABLE
    selected = min(max(default, 0), len(options) - 1)

    def render(stdscr):
        nonlocal selected
        curses.curs_set(0)
        stdscr.keypad(True)
        while True:
            stdscr.erase()
            lines = [title, "", "↑/↓ 选择，Enter 确认，Esc 取消"]
            for index, (label, description) in enumerate(options):
                marker = "●" if index == selected else "○"
                lines.append(f"{marker} {label}")
                if description:
                    lines.append(f"    {description}")
            for row, line in enumerate(lines):
                try:
                    stdscr.addstr(row, 0, line)
                except curses.error:
                    # 终端窗口过小时仍保留可操作性，不因绘制溢出崩溃。
                    pass
            stdscr.refresh()
            key = stdscr.get_wch()
            if key in (curses.KEY_UP, "k"):
                selected = (selected - 1) % len(options)
            elif key in (curses.KEY_DOWN, "j"):
                selected = (selected + 1) % len(options)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                return selected
            elif key == "\x1b":
                # 某些 ACP/PTY 会把 CSI 箭头序列分段送入 get_wch；短暂等待
                # 后补读序列，避免把真实的“↓”误判为 Esc 取消。
                stdscr.timeout(120)
                try:
                    prefix = stdscr.get_wch()
                    suffix = stdscr.get_wch() if prefix == "[" else None
                except curses.error:
                    return None
                finally:
                    stdscr.timeout(-1)
                if prefix == "[" and suffix == "A":
                    selected = (selected - 1) % len(options)
                elif prefix == "[" and suffix == "B":
                    selected = (selected + 1) % len(options)
                else:
                    return None
            elif key in ("q", "Q"):
                return None

    try:
        return curses.wrapper(render)
    except (curses.error, OSError, RuntimeError):
        # TTY 被宿主包装但 curses 初始化失败时，安全回到文本菜单。
        return RADIO_UNAVAILABLE
