#!/usr/bin/env python3
"""core/hosts/pi.py - Pi 插件（DOC-239 六面架构完整迁移）。

事实来源（2026-10-28 实测，``pi 0.85.1``）：

* identity      ← ``pi --help``：可执行文件名为 ``pi``；
                  项目标记为 ``.pi``。
* install       ← ``kind="skill"`` 入口为 ``.pi/skills/bugx/SKILL.md``；
                  必需依赖扩展面 ``.pi/extensions/bugx/``（index.ts, manifest.json, README.md）；
                  能力包括 ``extra-tracked-paths`` / ``extension-payload`` /
                  ``global-skill-link`` / ``native-mcp-config``。
* engine_run    ← 无头通道为 ``pi --approve -p <prompt>``，自治执行为 ``pi --approve -p <prompt> --mode json``；
                  无原生 Task / Subagent 编排（``native_task=False``）。
* worker_runtime← ``mode="process"``，最大并发 4 路；独立进程模式。
* mcp_write     ← 配置容器键为 ``mcpServers``，项目级作用域为 ``.pi/mcp.json``。
* acp           ← 上游 ``pi 0.85.1`` 无 ACP 子命令/flag，由 ``core/host_acp/pi.py``
                  以 ``status="unavailable"`` 声明。
"""

from __future__ import annotations

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

__all__ = ["PLUGIN"]

_NAME = "pi"

_PROJECT_PATHS: tuple[str, ...] = (
    ".pi/extensions/bugx/index.ts",
    ".pi/extensions/bugx/manifest.json",
    ".pi/extensions/bugx/README.md",
)

_CUSTOM_TITLE = "# BugX\n\n这是 BugX 的 Pi 项目级 Skill 入口。"

_CUSTOM_PREAMBLE = (
    "执行前必须先阅读项目中的 `bugx engine path --name bugx-runtime-contract.md` "
    "和 `bugx engine path --name bugx-quick.md`，再按需阅读 "
    "`bugx engine path --name AGENT_GUIDE.md` 和 `bugx engine path --name bugx.md`，"
    "并遵守其中的寿命、Supervisor、证据和报告协议。\n\n"
    "该入口只把任务交给当前 Pi；BugX CLI 只安装资源，不启动 Pi、不执行审计。"
    "Pi 的完整适配包还包含 `.pi/extensions/bugx/`，用于状态控制、轮询控制和受保护运行时拦截；"
    "项目未被 Pi 信任时，请先在 Pi 中信任项目，或使用 "
    "`pi --approve --skill .pi/skills/bugx/SKILL.md` 显式加载本入口。\n"
)


def _headless_argv(ctx: RunContext) -> list[str]:
    command = ["pi", "--approve", "-p", ctx.prompt]
    if ctx.session:
        command += ["--session", ctx.session]
    return command


def _autonomous_argv(ctx: RunContext) -> list[str]:
    command = [
        "pi", "--approve",
        "-p", ctx.prompt,
        "--mode", "json",
    ]
    if ctx.session:
        command += ["--session", ctx.session]
    return command


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    return (root / ".pi" / "mcp.json",)


def _agent_config_path(root: Path) -> Path:
    return root / ".pi"


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="Pi",
        order=10,
        executable="pi",
        markers=(".pi",),
        aliases=(),
        app_markers=None,
        native_question=False,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="skill",
        path=".pi/skills/bugx/SKILL.md",
        marker=".pi",
        required_paths=_PROJECT_PATHS,
        capabilities=frozenset(
            {
                "extra-tracked-paths",
                "extension-payload",
                "global-skill-link",
                "native-mcp-config",
            }
        ),
        interaction={
            "tool": "Pi 原生选择控件；否则 TTY 方向键菜单",
            "single_select": "等价于 multiple=false",
            "failure": "无控件且无 TTY 时阻断",
        },
        tools_line="read, bash, edit, write, grep, find, ls",
        mcp_relpaths=(".pi/mcp.json",),
        worker_subagent=False,
        worker_entry_path=None,
        custom_title=_CUSTOM_TITLE,
        custom_preamble=_CUSTOM_PREAMBLE,
        generated_dirs=(".pi",),
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
        native_task=False,
    ),
    worker_runtime=WorkerRuntimeFacet(
        mode="serial",
        max_concurrency=1,
        requires_process_mode=True,
        sandbox_sync=(),
    ),
    mcp_write=McpWriteFacet(
        container_key="mcpServers",
        writer_key=_NAME,
        project_relpath=".pi/mcp.json",
        global_scope=None,
        mirror_relpaths=(),
        requires_absolutize=True,
        entry_extra={},
    ),
    acp=AcpFacet(plugin=_NAME),
)
