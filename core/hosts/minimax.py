#!/usr/bin/env python3
"""core/hosts/minimax.py - MiniMax Code desktop Plugin V1 host.

Facts captured from the installed MiniMax Code 3.1.17 package on Windows and its
embedded local-plugin-v1 contract:

* user plugins are discovered below ~/.minimax/plugins/<plugin>/;
* the package manifest is .minimax-plugin/plugin.json;
* MCP servers are declared in servers.mcp.json;
* plugin subagents are Markdown files under agents/ and Skills under skills/;
* the Agents roster uses ~/.minimax/agents/<name>/agent.md for Custom Agents.

The desktop package exposes the PluginSystem/MCP/Agent surface, but this
repository has not verified a MiniMax Code headless or standard ACP entry point.
Those channels therefore fail closed instead of borrowing Kimi or another host's
flags.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

from .base import (
    AcpFacet,
    EngineRunFacet,
    HostPlugin,
    HostPluginError,
    IdentityFacet,
    InstallFacet,
    McpWriteFacet,
    RunContext,
    STATUS_READY,
    WorkerRuntimeFacet,
)

__all__ = ["PLUGIN", "is_available", "run_static_degraded"]

_NAME = "minimax"
_DATA_ENV = "MINIMAX_DATA_DIR"
_DEFAULT_DATA_DIR_NAME = ".minimax"
_PLUGIN_RELATIVE_ROOT = ("plugins", "bugx")


def _minimax_data_dir() -> Path:
    configured = os.environ.get(_DATA_ENV)
    if configured is not None:
        if not configured.strip():
            raise HostPluginError(f"{_DATA_ENV} 不能为空")
        return Path(configured).expanduser()
    return Path.home() / _DEFAULT_DATA_DIR_NAME


def _minimax_plugin_root() -> Path:
    return _minimax_data_dir().joinpath(*_PLUGIN_RELATIVE_ROOT)


def _windows_drive_roots() -> tuple[Path, ...]:
    if platform.system() != "Windows":
        return ()
    try:
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except (AttributeError, OSError, TypeError, ValueError):
        return ()
    return tuple(
        Path(f"{chr(ord('A') + index)}:/")
        for index in range(26)
        if mask & (1 << index)
    )


def _minimax_app_markers() -> tuple[Path, ...]:
    """Return bounded MiniMax Code installation candidates; never scan a drive."""
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Darwin":
        markers.extend((
            Path("/Applications/MiniMax Code.app"),
            home / "Applications" / "MiniMax Code.app",
        ))
    elif os.name != "nt":
        markers.extend((
            home / ".local" / "share" / "applications" / "minimax-code.desktop",
            Path("/opt/minimax/MiniMax Code/MiniMax Code"),
        ))

    bases: list[Path] = []
    for variable in (
        "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    ):
        value = os.environ.get(variable)
        if value:
            bases.append(Path(value))
    bases.extend(_windows_drive_roots())

    for base in bases:
        markers.extend((
            base / "minimax" / "MiniMax Code" / "MiniMax Code.exe",
            base / "MiniMax" / "MiniMax Code" / "MiniMax Code.exe",
            base / "Programs" / "minimax" / "MiniMax Code" / "MiniMax Code.exe",
            base / "Programs" / "MiniMax" / "MiniMax Code" / "MiniMax Code.exe",
            base / "Program Files" / "minimax" / "MiniMax Code" / "MiniMax Code.exe",
            base / "Program Files (x86)" / "minimax" / "MiniMax Code" / "MiniMax Code.exe",
        ))
    return tuple(dict.fromkeys(markers))


def _unsupported_headless(_ctx: RunContext) -> list[str]:
    raise HostPluginError(
        "MiniMax Code 当前仅完成桌面 Plugin V1 适配；headless CLI 入口和 flag 尚未取证，拒绝伪造启动参数"
    )


def _unsupported_autonomous(_ctx: RunContext) -> list[str]:
    raise HostPluginError(
        "MiniMax Code 当前仅完成桌面 Agent/MCP 插件适配；自治 CLI 入口尚未取证，拒绝伪造启动参数"
    )


def _mcp_config_paths(_root: Path) -> tuple[Path, ...]:
    return (_minimax_plugin_root() / "servers.mcp.json",)


def _agent_config_path(_root: Path) -> Path:
    return _minimax_data_dir() / "agents" / "bugx" / "agent.md"


def _resolve_minimax_cli() -> str | None:
    """Only report a PATH-resolved CLI; do not infer its command-line contract."""
    return shutil.which("mcode")


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="MiniMax Code",
        order=16,
        executable="mcode",
        markers=(".minimax/plugins/bugx/.minimax-plugin/plugin.json", ".minimax"),
        aliases=("minimax-code", "minimaxcode", "mcode"),
        app_markers=_minimax_app_markers,
        native_question=False,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="plugin",
        path=".minimax/plugins/bugx",
        entry_mode="user-registration",
        marker=".minimax",
        required_paths=(),
        capabilities=frozenset({
            "user-agent-registration",
            "user-level-plugin",
        }),
        interaction={
            "tool": "MiniMax Code Agent UI",
            "single_select": "host-native",
            "multi_select": "host-native",
        },
        tools_line="read, write, edit, bash, grep, glob, web_fetch",
        mcp_relpaths=(),
        worker_subagent=True,
        worker_entry_path=".minimax/plugins/bugx/agents/bugx-worker.md",
        generated_dirs=(),
        sweepable=False,
    ),
    engine_run=EngineRunFacet(
        supports_headless=False,
        headless_argv=_unsupported_headless,
        autonomous_argv=_unsupported_autonomous,
        mcp_config_paths=_mcp_config_paths,
        agent_config_path=_agent_config_path,
        mcp_health=None,
        host_surface="desktop",
        native_task=False,
    ),
    worker_runtime=WorkerRuntimeFacet(
        mode="serial",
        max_concurrency=1,
        requires_process_mode=False,
        sandbox_sync=(),
        provider_key=_NAME,
        autonomous_cli=False,
    ),
    mcp_write=McpWriteFacet(
        container_key="mcpServers",
        writer_key=_NAME,
        project_relpath=None,
        global_scope="~/.minimax/plugins/bugx/servers.mcp.json",
        mirror_relpaths=(),
        requires_absolutize=False,
        entry_extra={},
        custom_writer=True,
    ),
    acp=AcpFacet(plugin=_NAME),
)


def is_available() -> tuple[bool, str]:
    """Report the local CLI without claiming its unverified flags are supported."""
    path = _resolve_minimax_cli()
    if path:
        return True, path
    app_path = next((path for path in _minimax_app_markers() if path.is_file()), None)
    if app_path:
        return True, str(app_path)
    return False, "MiniMax Code desktop/CLI 未在已知位置找到"


def run_static_degraded(root: Path | None = None) -> dict:
    """Return static plugin facts; never report a live desktop session as healthy."""
    available, availability_detail = is_available()
    plugin_root = _minimax_plugin_root()
    manifest = plugin_root / ".minimax-plugin" / "plugin.json"
    return {
        "plugin": _NAME,
        "schema_version": 1,
        "success": available,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": "static_facts_only" if available else "minimax_host_not_found",
        "probe_mode": "static_degraded",
        "headless_available": False,
        "native_task_available": False,
        "identity": {
            "executable": PLUGIN.identity.executable,
            "aliases": list(PLUGIN.identity.aliases),
            "markers": list(PLUGIN.identity.markers),
        },
        "agent_entry": str(_agent_config_path(root or Path.cwd())),
        "worker_entry": str(plugin_root / "agents" / "bugx-worker.md"),
        "plugin_manifest": str(manifest),
        "mcp_config": str(plugin_root / "servers.mcp.json"),
        "findings": [],
        "evidence": {
            "availability_detail": availability_detail,
            "project_root": str(root) if root else None,
            "plugin_installed": manifest.is_file(),
        },
        "metadata": {
            "degraded_mode": "static_plugin_facts_only",
            "acp": "unavailable（尚未取证到 MiniMax Code 标准 ACP 入口）",
            "fallback": "MiniMax Code 以桌面 Plugin V1 运行；BugX 不代装宿主、不伪造 headless/ACP",
        },
    }
