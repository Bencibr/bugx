#!/usr/bin/env python3
"""core/host_acp/minimax.py - MiniMax Code ACP capability declaration.

MiniMax Code's installed desktop package exposes Plugin V1, MCP and Agent
manifests. A standard ACP entry point was not found in the inspected package,
so this is an explicit upstream-capability declaration, not an unfinished local
adapter.
"""

from __future__ import annotations

from .base import AcpPlugin

__all__ = ["MiniMaxAcpPlugin", "PLUGIN"]


class MiniMaxAcpPlugin(AcpPlugin):
    name = "minimax"
    executable = "mcode"
    status = "unavailable"
    reason = (
        "MiniMax Code 3.1.0 桌面包已取证 Plugin V1、MCP 与 Agent 能力，"
        "但未取证到标准 ACP 入口；ACP 探活不伪造命令行参数。"
    )
    supports_project_permissions = False


PLUGIN = MiniMaxAcpPlugin
