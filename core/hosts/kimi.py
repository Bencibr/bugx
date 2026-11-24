#!/usr/bin/env python3
"""core/hosts/kimi.py - Kimi Code CLI 插件（DOC-239 六面架构完整适配）。

事实来源（2026-11-23 实测，Kimi Code 1.4.2 / ``KIMI_CODE_HOME``）：

* identity      ← ``kimi --help``：可执行文件名为 ``kimi``，别名 ``kimi-code`` / ``kimicode``；
                  项目标记为 ``.kimi-code/mcp.json`` 与 ``.kimi-code``。
* install       ← ``subagent`` 入口：``.kimi-code/agents/bugx.md`` 与
                  ``.kimi-code/agents/bugx-worker.md``；支持项目级与用户级
                  ``~/.kimi-code/skills/`` 及原生 ``mcpServers`` 配置。
* engine_run    ← 无头通道为 ``kimi -p <prompt>``，自治执行为
                  ``kimi -p <prompt> --output-format stream-json --auto``；
                  支持自定义 subagent 定义（``--agent`` / ``--agent-file``）。
* worker_runtime← ``mode="multi"``，支持 CLI 并发调度。
* mcp_write     ← 配置容器键为 ``mcpServers``，项目级作用域为 ``.kimi-code/mcp.json``，
                  全局作用域为 ``<KIMI_CODE_HOME>/mcp.json``（默认 ``~/.kimi-code/mcp.json``）。
* distribution  ← 桌面端插件目录为 ``<KIMI_CODE_HOME>/plugins/managed/<id>``，
                  启用注册表为 ``<KIMI_CODE_HOME>/plugins/installed.json``。
* acp           ← ``kimi acp`` 原生支持标准 JSON-RPC 2.0 ACP 协议，由
                  ``core/host_acp/kimi.py`` 以 ``status="ready"`` 声明。
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
    IdentityFacet,
    InstallFacet,
    McpWriteFacet,
    RunContext,
    STATUS_READY,
    WorkerRuntimeFacet,
)

__all__ = ["PLUGIN", "is_available", "run_static_degraded"]

_NAME = "kimi"

_PROJECT_PATHS: tuple[str, ...] = (
    ".kimi-code/agents/bugx.md",
    ".kimi-code/agents/bugx-worker.md",
)

_CUSTOM_TITLE = "# BugX\n\n这是 BugX 的 Kimi Code 审计智能体入口。"

_CUSTOM_PREAMBLE = (
    "先读取全局引擎协议：\n"
    "通过 `bugx engine path --name bugx-runtime-contract.md` 读取全局协议，\n"
    "并遵守其中的寿命、Supervisor、证据和报告协议。\n\n"
    "该入口只把任务交给当前 Kimi Code 会话；BugX CLI 只安装资源，不启动 Kimi Code、不执行审计。\n"
)


def _kimi_code_home() -> Path:
    configured = os.environ.get("KIMI_CODE_HOME")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".kimi-code"


def _resolve_kimi_executable() -> str | None:
    found = shutil.which("kimi")
    if found:
        return found
    bin_dir = _kimi_code_home() / "bin"
    names = ("kimi.exe", "kimi.cmd", "kimi.bat", "kimi") if os.name == "nt" else ("kimi",)
    for name in names:
        fallback = bin_dir / name
        if fallback.is_file():
            return str(fallback)
    return None


def _windows_drive_roots() -> tuple[Path, ...]:
    """返回 Windows 当前可用的逻辑驱动器根目录。"""
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


def _headless_argv(ctx: RunContext) -> list[str]:
    command = [_resolve_kimi_executable() or "kimi", "-p", ctx.prompt]
    if ctx.session:
        command[1:1] = ["--session", ctx.session]
    if ctx.model:
        command += ["-m", ctx.model]
    return command


def _autonomous_argv(ctx: RunContext) -> list[str]:
    command = [_resolve_kimi_executable() or "kimi"]
    if ctx.session:
        command += ["--session", ctx.session]
    if ctx.model:
        command += ["-m", ctx.model]
    command += [
        "-p",
        ctx.prompt,
        "--output-format",
        "stream-json",
        "--auto",
    ]
    return command


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    return (root / ".kimi-code" / "mcp.json",)


def _kimi_app_markers() -> tuple[Path, ...]:
    """Kimi 桌面端已知安装位置。"""
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Darwin":
        markers.extend((
            Path("/Applications/Kimi.app"),
            Path("/Applications/Kimi Code.app"),
            home / "Applications" / "Kimi.app",
            home / "Applications" / "Kimi Code.app",
            home / "Library" / "Application Support" / "kimi-desktop",
            home / "Library" / "Application Support" / "Kimi Code",
        ))
    elif os.name != "nt":
        markers.extend((
            home / ".local" / "share" / "applications" / "kimi.desktop",
            home / ".local" / "share" / "applications" / "kimi-code.desktop",
            Path("/usr/bin/kimi-desktop"),
            Path("/usr/bin/kimi-code"),
            Path("/opt/Kimi/kimi"),
            Path("/opt/Kimi Code/kimi-code"),
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
            base / "Kimi" / "Kimi.exe",
            base / "kimi-desktop" / "Kimi.exe",
            base / "Kimi Code" / "Kimi Code.exe",
            base / "Programs" / "Kimi" / "Kimi.exe",
            base / "Programs" / "kimi-desktop" / "Kimi.exe",
            base / "Programs" / "Kimi Code" / "Kimi Code.exe",
            base / "Program Files" / "Kimi Code" / "Kimi Code.exe",
            base / "Program Files (x86)" / "Kimi Code" / "Kimi Code.exe",
        ))
    return tuple(markers)


def _agent_config_path(root: Path) -> Path:
    return root / ".kimi-code"


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="Kimi Code",
        order=14,
        executable="kimi",
        markers=(".kimi-code/mcp.json", ".kimi-code"),
        aliases=("kimi-code", "kimicode"),
        app_markers=_kimi_app_markers,
        native_question=True,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="subagent",
        path=".kimi-code/agents/bugx.md",
        marker=".kimi-code",
        required_paths=_PROJECT_PATHS,
        capabilities=frozenset(
            {
                "native-mcp-config",
                "extra-tracked-paths",
                "skills",
                "user-agent-registration",
            }
        ),
        interaction={
            "tool": "AskUserQuestion",
            "single_select": "multiSelect=false",
            "multi_select": "tooling:batch 时使用 multiSelect=true",
        },
        tools_line="Bash, Read, Grep, Glob, Edit, Write",
        mcp_relpaths=(".kimi-code/mcp.json",),
        worker_subagent=True,
        worker_entry_path=".kimi-code/agents/bugx-worker.md",
        custom_title=_CUSTOM_TITLE,
        custom_preamble=_CUSTOM_PREAMBLE,
        generated_dirs=(".kimi-code",),
        sweepable=True,
    ),
    engine_run=EngineRunFacet(
        supports_headless=True,
        headless_argv=_headless_argv,
        autonomous_argv=_autonomous_argv,
        mcp_config_paths=_mcp_config_paths,
        agent_config_path=_agent_config_path,
        mcp_health=None,
        host_surface="tui",
        recon_scout_argv=lambda _root, text, _prompt_file, _inventory=None: [
            _resolve_kimi_executable() or "kimi",
            "-p",
            text,
            "--output-format",
            "text",
            "--auto",
        ],
        native_task=True,
    ),
    worker_runtime=WorkerRuntimeFacet(
        mode="multi",
        max_concurrency=None,
        requires_process_mode=False,
        sandbox_sync=(),
    ),
    mcp_write=McpWriteFacet(
        container_key="mcpServers",
        writer_key=_NAME,
        project_relpath=".kimi-code/mcp.json",
        global_scope="~/.kimi-code/mcp.json",
        mirror_relpaths=(),
        requires_absolutize=True,
        entry_extra={},
    ),
    acp=AcpFacet(plugin=_NAME),
)


def is_available() -> tuple[bool, str]:
    """降级契约：机械探活 Kimi Code CLI 是否安装（零子进程）。"""
    path = _resolve_kimi_executable()
    if path is None:
        return False, "kimi 可执行文件未安装；headless（kimi -p）与 ACP（kimi acp）通道不可用"
    return True, path


def run_static_degraded(root: Path | None = None) -> dict:
    """Kimi Code CLI 缺失时的静态降级输出：只声明插件事实，绝不伪装宿主健康。"""
    available, availability_detail = is_available()
    return {
        "plugin": _NAME,
        "schema_version": 1,
        "success": available,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": (
            "static_facts_only" if available else "kimi_cli_not_installed"
        ),
        "probe_mode": "static_degraded",
        "headless_available": available,
        "native_task_available": bool(available and PLUGIN.engine_run.native_task),
        "identity": {
            "executable": PLUGIN.identity.executable,
            "aliases": list(PLUGIN.identity.aliases),
            "markers": list(PLUGIN.identity.markers),
        },
        "agent_entry": PLUGIN.install.path,
        "mcp_project_relpath": PLUGIN.mcp_write.project_relpath,
        "mcp_global_scope": PLUGIN.mcp_write.global_scope,
        "findings": [],
        "evidence": {
            "availability_detail": availability_detail,
            "project_root": str(root) if root else None,
        },
        "metadata": {
            "degraded_mode": "static_plugin_facts_only",
            "acp": "ready（kimi acp 原生支持标准 ACP，见 core/host_acp/kimi.py）",
            "fallback": "宿主 CLI 缺失时由外层宿主/用户决策接管；BugX 不代装宿主 CLI",
        },
    }
