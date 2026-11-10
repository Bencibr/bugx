#!/usr/bin/env python3
"""core/hosts/claude.py - Claude Code 插件（DOC-239 六面架构完整迁移）。

事实来源（2026-10-28 实测，``claude 2.1.296 (Claude Code)``）：

* identity      ← ``claude --help``：可执行文件名为 ``claude``，别名 ``claude-code``；
                  项目标记为 ``.mcp.json``、``.claude/mcp.json`` 与 ``.claude``。
* install       ← ``src/bugx_cli/hosts/registry.HOST_COMPONENTS`` 的 claude 条目
                  （``subagent`` 入口：``.claude/agents/bugx.md`` 与 ``.claude/agents/bugx-worker.md``；
                  能力 ``user-agent-registration`` / ``native-mcp-config`` /
                  ``distribution-plugin`` / ``skills``）。
* engine_run    ← 无头通道为 ``claude -p <prompt>``，自治执行为 ``claude -p <prompt>
                  --output-format json --permission-mode bypassPermissions
                  --dangerously-skip-permissions --mcp-config <root>/.mcp.json``；
                  原生支持 Task / Subagent 派发能力（``native_task=True``）。
* worker_runtime← ``mode="subagent"``，最大并发 4 路；支持 CLI 进程池与自治调度，
                  并发范围 2~6。
* mcp_write     ← 配置容器键为 ``mcpServers``，项目级作用域为 ``.mcp.json``，
                  镜像为 ``.claude/mcp.json``，全局作用域为 ``~/.claude/settings.json``。
* acp           ← Claude Code 上游无 ACP 子命令/flag，由 ``core/host_acp/claude.py``
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

_NAME = "claude"

_PROJECT_PATHS: tuple[str, ...] = (
    ".claude/agents/bugx.md",
    ".claude/agents/bugx-worker.md",
)

_CUSTOM_TITLE = "# BugX\n\n这是 BugX 的 Claude Code 审计智能体入口。"

_CUSTOM_PREAMBLE = (
    "先读取全局引擎协议：\n"
    "通过 `bugx engine path --name bugx-runtime-contract.md` 读取全局协议，\n"
    "并遵守其中的寿命、Supervisor、证据和报告协议。\n\n"
    "该入口只把任务交给当前 Claude Code 会话；BugX CLI 只安装资源，不启动 Claude Code、不执行审计。\n"
)


def _headless_argv(ctx: RunContext) -> list[str]:
    command = ["claude", "-p", ctx.prompt]
    if ctx.session:
        command[1:1] = ["--resume", ctx.session]
    return command


def _autonomous_argv(ctx: RunContext) -> list[str]:
    command = ["claude"]
    if ctx.session:
        command += ["--resume", ctx.session]
    command += [
        "-p", ctx.prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--mcp-config", str(ctx.root / ".mcp.json"),
    ]
    return command


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    return (root / ".mcp.json",)


def _agent_config_path(root: Path) -> Path:
    return root / ".claude"


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="Claude Code",
        order=8,
        executable="claude",
        markers=(".mcp.json", ".claude/mcp.json", ".claude"),
        aliases=("claude-code",),
        app_markers=None,
        native_question=True,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="subagent",
        path=".claude/agents/bugx.md",
        marker=".claude",
        required_paths=_PROJECT_PATHS,
        capabilities=frozenset(
            {
                "user-agent-registration",
                "native-mcp-config",
                "extra-tracked-paths",
                "skills",
            }
        ),
        interaction={
            "tool": "AskUserQuestion",
            "single_select": "multiSelect=false",
            "multi_select": "tooling:batch 时使用 multiSelect=true",
        },
        tools_line="Bash, Read, Grep, Glob, Edit, Write, AskUserQuestion",
        mcp_relpaths=(".mcp.json",),
        worker_subagent=True,
        worker_entry_path=".claude/agents/bugx-worker.md",
        custom_title=_CUSTOM_TITLE,
        custom_preamble=_CUSTOM_PREAMBLE,
        generated_dirs=(".claude",),
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
        # Recon Scout：``claude -p @<prompt_file>``（一次性无头问答）。
        recon_scout_argv=lambda _root, _text, prompt_file, _inventory=None: [
            "claude", "-p", f"@{prompt_file}",
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
        project_relpath=".mcp.json",
        global_scope="~/.claude/settings.json",
        mirror_relpaths=(".claude/mcp.json",),
        requires_absolutize=True,
        entry_extra={},
    ),
    acp=AcpFacet(plugin=_NAME),
)
