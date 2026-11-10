#!/usr/bin/env python3
"""core/hosts/zcode.py - ZCode 插件（DOC-239 六面架构完整迁移）。

事实来源（2026-10-28 实测，``zcode 0.17.0`` / Electron Desktop ``3.16.3``）：

* identity      ← ``zcode --help``：可执行文件名为 ``zcode``；
                  桌面端标记为 ``/Applications/ZCode.app`` 等（``_zcode_app_markers``）；
                  项目标记为 ``.zcode``。
* install       ← ``kind="subagent"``；0.7.22 起主入口和 worker 入口都注册在
                  用户级 ``~/.zcode/agents/``（``entry_mode="user-registration"``）；
                  ``.zcode/agents/bugx*.md`` 仅作为旧版本项目副本清理清单，
                  不再作为安装目标。能力包含 ``distribution-plugin`` /
                  ``native-mcp-config`` / ``user-agent-registration``。
* engine_run    ← 无头通道为 ``zcode -p <prompt> --mode yolo``（若 PATH 中缺少 zcode，
                  优先降级使用桌面端内置的 Node 核心脚本
                  ``/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs``）；
                  原生支持子智能体 Task 编排（``native_task=True``）；
                  宿主形态为 ``host_surface="desktop"``。
* worker_runtime← ``mode="subagent"``，最大并发 4 路；支持自主调度与并发派发。
* mcp_write     ← 配置容器键为 ``mcp.servers``（嵌套结构），用户级全局作用域为
                  ``~/.zcode/cli/config.json``，无项目级 MCP 作用域（``project_relpath=None``）。
* acp           ← 桌面端虽然内置 ``app-server`` 子命令，但其传输的是专有 ZCode Protocol
                  （Strict JSON Schema，拒绝标准 ACP 的 ``jsonrpc`` 字段），无标准 ACP 入口，
                  由 ``core/host_acp/zcode.py`` 以 ``status="unavailable"`` 声明。
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
from typing import Mapping

from .base import (
    AcpFacet,
    EngineRunFacet,
    UsageFacet,
    HostPlugin,
    IdentityFacet,
    InstallFacet,
    McpWriteFacet,
    RunContext,
    STATUS_READY,
    WorkerRuntimeFacet,
)

__all__ = ["PLUGIN"]

_NAME = "zcode"

_PROJECT_PATHS: tuple[str, ...] = (
    ".zcode/agents/bugx.md",
    ".zcode/agents/bugx-worker.md",
)

_CUSTOM_TITLE = "# BugX\n\n这是 BugX 的 ZCode 审计智能体入口。"

_CUSTOM_PREAMBLE = (
    "先读取全局引擎协议：\n"
    "通过 `bugx engine path --name bugx-runtime-contract.md` 读取全局协议，\n"
    "并遵守其中的寿命、Supervisor、证据和报告协议。\n\n"
    "该入口只把任务交给当前 ZCode 会话；BugX CLI 只安装资源，不启动 ZCode、不执行审计。\n"
)


def _windows_drive_roots() -> tuple[Path, ...]:
    """探测 Windows 下所有可用驱动器根目录。"""
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


def _zcode_app_markers() -> tuple[Path, ...]:
    """ZCode 桌面端已知安装位置。"""
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Darwin":
        markers.extend((
            Path("/Applications/ZCode.app"),
            home / "Applications" / "ZCode.app",
        ))
    elif os.name != "nt":
        markers.append(home / ".local" / "share" / "applications" / "zcode.desktop")

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
            base / "ZCode" / "ZCode.exe",
            base / "Z.ai" / "ZCode.exe",
            base / "Z.ai" / "ZCode" / "ZCode.exe",
            base / "Programs" / "ZCode" / "ZCode.exe",
            base / "Programs" / "Z.ai" / "ZCode.exe",
        ))
    return tuple(markers)


def _headless_argv(ctx: RunContext) -> list[str]:
    command = ["zcode", "-p", ctx.prompt, "--mode", "yolo", "--cwd", str(ctx.root)]
    if ctx.session:
        command += ["--resume", ctx.session]
    return command


def _autonomous_argv(ctx: RunContext) -> list[str]:
    command = ["zcode", "-p", ctx.prompt, "--mode", "yolo", "--json", "--cwd", str(ctx.root)]
    if ctx.session:
        command += ["--resume", ctx.session]
    return command


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    """ZCode 实际只读用户级 ~/.zcode/cli/config.json（mcp.servers 嵌套）。"""
    return (Path.home() / ".zcode" / "cli" / "config.json",)


def _agent_config_path(root: Path) -> Path:
    return root / ".zcode"


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="ZCode",
        order=12,
        executable="zcode",
        markers=(".zcode",),
        aliases=(),
        app_markers=_zcode_app_markers,
        native_question=False,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="subagent",
        path=".zcode/agents/bugx.md",
        marker=".zcode",
        entry_mode="user-registration",
        required_paths=(),
        legacy_paths=_PROJECT_PATHS,
        capabilities=frozenset(
            {
                "distribution-plugin",
                "native-mcp-config",
                "user-agent-registration",
            }
        ),
        interaction={
            "tool": "AskUserQuestion",
            "single_select": "multiSelect=false",
            "multi_select": "tooling:batch 时使用 multiSelect=true",
        },
        tools_line="Bash, Read, Grep, Glob, Edit, Write, AskUserQuestion",
        mcp_relpaths=(".zcode/cli/config.json",),
        worker_subagent=True,
        worker_entry_path=".zcode/agents/bugx-worker.md",
        custom_title=_CUSTOM_TITLE,
        custom_preamble=_CUSTOM_PREAMBLE,
        generated_dirs=(),
        sweepable=True,
    ),
    engine_run=EngineRunFacet(
        supports_headless=True,
        headless_argv=_headless_argv,
        autonomous_argv=_autonomous_argv,
        mcp_config_paths=_mcp_config_paths,
        agent_config_path=_agent_config_path,
        mcp_health=None,
        host_surface="desktop",
        native_task=True,
    ),
    worker_runtime=WorkerRuntimeFacet(
        mode="multi",
        max_concurrency=4,
        requires_process_mode=False,
        sandbox_sync=(),
        sandbox_dir=".zcode/cli/agents",
        runner_module="zcode",
        provider_key="zcode",
        pid_required=False,
        autonomous_cli=True,
        concurrency_bounds=(2, 6),
    ),
    mcp_write=McpWriteFacet(
        container_key="mcp.servers",
        writer_key="zcode",
        project_relpath=None,
        global_scope="~/.zcode/cli/config.json",
        mirror_relpaths=(),
        requires_absolutize=True,
        entry_extra={},
        custom_writer=False,
        config_format="json",
        bootstrap=None,
        remove=None,
        inspect=None,
    ),
    acp=AcpFacet(plugin=_NAME),
    usage=UsageFacet(
        plugin=_NAME,
        module="_zcode_usage",
        state_filename="zcode-usage.json",
        notes="只读解析 ~/.zcode/cli/db/db.sqlite 的 model_usage 表，按 lifecycle 时间窗聚合主/Worker Token 与逐模型用量。",
    ),
)
