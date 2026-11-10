"""BugX AI Agent 宿主组件框架。

导出所有策略契约、HostComponent 规格类、模板引擎及注册表。
"""

from __future__ import annotations

from .base import (
    ExecutionContract,
    HostComponent,
    InstallContract,
    InteractionContract,
    McpContract,
)
from .registry import (
    CAP_DISTRIBUTION_PLUGIN,
    CAP_EXTENSION_PAYLOAD,
    CAP_EXTRA_TRACKED_PATHS,
    CAP_GLOBAL_AGENT_LINK,
    CAP_GLOBAL_SKILL_LINK,
    CAP_NATIVE_MCP_CONFIG,
    CAP_PREVALIDATE_CATALOG,
    CAP_PROJECT_MCP_CONFIG,
    CAP_PROJECT_PATCH_OVERLAY,
    CAP_USER_AGENT_REGISTRATION,
    CAP_USER_LEVEL_ARTIFACT,
    ENTRY_MULTI_PATH,
    ENTRY_PLUGIN_CATALOG,
    ENTRY_RENDERED,
    ENTRY_SHARED_SYMLINK,
    ENTRY_SKILL_PAYLOAD,
    ENTRY_USER_REGISTRATION,
    HOST_COMPONENTS,
    WORKBUDDY_PROJECT_PATHS,
    get_component,
)
from .template_engine import (
    add_locale_marker,
    get_agent_description,
    get_worker_description,
    read_locale_marker,
    render_host_entry,
    interaction_protocol,
    strip_locale_marker,
)

__all__ = [
    "ExecutionContract",
    "HostComponent",
    "InstallContract",
    "InteractionContract",
    "McpContract",
    "HOST_COMPONENTS",
    "get_component",
    "render_host_entry",
    "interaction_protocol",
    "add_locale_marker",
    "strip_locale_marker",
    "read_locale_marker",
    "get_agent_description",
    "get_worker_description",
    "ENTRY_RENDERED",
    "ENTRY_SHARED_SYMLINK",
    "ENTRY_USER_REGISTRATION",
    "ENTRY_MULTI_PATH",
    "ENTRY_SKILL_PAYLOAD",
    "ENTRY_PLUGIN_CATALOG",
    "CAP_PROJECT_MCP_CONFIG",
    "CAP_GLOBAL_AGENT_LINK",
    "CAP_PROJECT_PATCH_OVERLAY",
    "CAP_EXTRA_TRACKED_PATHS",
    "CAP_NATIVE_MCP_CONFIG",
    "CAP_USER_AGENT_REGISTRATION",
    "CAP_DISTRIBUTION_PLUGIN",
    "CAP_USER_LEVEL_ARTIFACT",
    "CAP_EXTENSION_PAYLOAD",
    "CAP_GLOBAL_SKILL_LINK",
    "CAP_PREVALIDATE_CATALOG",
    "WORKBUDDY_PROJECT_PATHS",
]
