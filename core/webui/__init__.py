"""BugX WebUI package.

Provides AtomCode/DSH-derived AI Agent Web Studio for multi-project management,
workspace MCP inspection, and independent task dispatching to CLI hosts (Codex, Claude, OpenCode, BugX).
"""

from .dispatcher import LiveSessionDispatcher
from .handler import WebUIHandler
from .mcp_inspector import get_mcp_detail, get_online_mcp_catalog, inspect_workspace_mcp

__all__ = ["WebUIHandler", "LiveSessionDispatcher", "inspect_workspace_mcp", "get_mcp_detail", "get_online_mcp_catalog"]
