#!/usr/bin/env python3
"""core/host_acp/claude.py - Claude Code 的 ACP 状态声明（宿主产品无 ACP 面）。

事实（2026-10-28 实测，``claude 2.1.296 (Claude Code)``）：

* 顶层 ``claude --help`` 的子命令只有 ``agents`` / ``attach`` / ``auth`` / ``doctor`` /
  ``gateway`` / ``import`` / ``install`` / ``logs`` / ``mcp`` / ``plugin`` / ``purge`` /
  ``respawn`` / ``rm`` / ``setup-token`` / ``stop`` / ``ultrareview`` / ``update`` 等，
  **没有** ``acp``，也没有 ``--acp`` flag；
* 因此本插件声明 ``status = "unavailable"``：这不是「本仓还没适配」，而是
  **上游没有该协议面**。探针据此报 ``BLOCKED(adapter_status=unavailable)``，并把
  Claude Code 的受支持通道指向无头 ``claude -p`` 与标准插件机制（见
  ``core/hosts/claude.py`` 的 ``engine_run`` 面）。

上游一旦发布 ACP 入口：把 ``status`` 改成 ``ready`` 并实现 ``acp_argv`` 即可，
探针代码一行都不用改（连同 ``core/hosts/claude.py`` 的 ``acp`` 面一起核对）。
"""

from __future__ import annotations

from .base import AcpPlugin

__all__ = ["ClaudeAcpPlugin", "PLUGIN"]

#: 宿主名由模块名派生（``core/host_acp/<host>.py``），实现体不写字面量。
_HOST_NAME = __name__.rsplit(".", 1)[-1]


class ClaudeAcpPlugin(AcpPlugin):
    """Claude Code 的 ACP 面：上游不存在，显式声明为 ``unavailable``。"""

    name = _HOST_NAME
    executable = _HOST_NAME
    status = "unavailable"
    reason = (
        "claude 2.1.296 的 --help 无 acp 子命令/--acp flag，"
        "宿主产品本身没有 ACP 入口；受支持通道是无头 claude -p 与 Claude Code 插件"
    )


PLUGIN = ClaudeAcpPlugin
