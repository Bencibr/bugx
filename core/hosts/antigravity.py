"""Google Antigravity desktop and ``agy`` review/worker integration.

Antigravity has two separate configuration surfaces: the user-level plugin
contains the Agent and Skill, while the workspace-level ``.agents`` file is
the only safe place for BugX's project-bound MCP servers.  Keeping those
surfaces separate is important because plugin stdio children do not reliably
inherit the active workspace as their current directory.
"""

from pathlib import Path

from .base import (
    AcpFacet, EngineRunFacet, HostPlugin, IdentityFacet,
    InstallFacet, McpWriteFacet, RunContext, STATUS_READY, WorkerRuntimeFacet,
)


PLUGIN_DIR = ".gemini/config/plugins/bugx-review"
MANIFEST = "plugin.json"
AGENT = "agents/bugx-review.md"
WORKER_AGENT = "agents/bugx-worker.md"
SKILL = "skills/bugx-review/SKILL.md"
MCP = "mcp_config.json"
LOGO = "assets/bugx-avatar.svg"
PROJECT_MCP = ".agents/mcp_config.json"
AGENT_ID = "BugX"


def _headless_argv(context: RunContext) -> list[str]:
    """Build the documented ``agy -p`` headless command.

    The installed BugX Agent is the stable entry point for this host; callers
    may pass a generic engine agent name, so it is deliberately not forwarded
    to ``agy --agent``.
    """

    command = [
        "agy", "--agent", AGENT_ID, "--mode", "auto",
        "--add-dir", str(context.root), "--output-format", "json",
        "-p", context.prompt,
    ]
    if context.session:
        command[3:3] = ["--conversation", context.session]
    return command


def _autonomous_argv(context: RunContext) -> list[str]:
    """Build the unattended command with Antigravity's official permission flag."""

    command = [
        "agy", "--agent", AGENT_ID, "--mode", "auto",
        "--dangerously-skip-permissions", "--add-dir", str(context.root),
        "--output-format", "json", "-p", context.prompt,
    ]
    if context.session:
        command[3:3] = ["--conversation", context.session]
    return command


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    """Return the workspace MCP file consumed by Antigravity."""

    return (root / PROJECT_MCP,)


def _agent_config_path(_root: Path) -> Path:
    return Path.home() / PLUGIN_DIR / AGENT


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name="antigravity", label="Google Antigravity", order=18, executable="agy",
        # Antigravity 的项目级事实入口是工作区 MCP 文件；插件本体位于用户级
        # ``~/.gemini/config/plugins``，不能拿不存在的项目插件镜像路径做识别 marker。
        markers=(PROJECT_MCP,), aliases=("agy",),
        native_question=False,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="plugin", path=f"{PLUGIN_DIR}/{MANIFEST}", entry_mode="user-registration",
        marker=PROJECT_MCP,
        capabilities=frozenset({
            "user-agent-registration", "user-level-plugin", "native-mcp-config",
        }),
        worker_subagent=True,
        worker_entry_path=WORKER_AGENT,
        interaction={"tool": "Antigravity Agent selector"},
    ),
    engine_run=EngineRunFacet(
        supports_headless=True,
        headless_argv=_headless_argv,
        autonomous_argv=_autonomous_argv,
        mcp_config_paths=_mcp_config_paths,
        agent_config_path=_agent_config_path,
        host_surface="desktop",
    ),
    worker_runtime=WorkerRuntimeFacet(
        mode="serial",
        max_concurrency=1,
        requires_process_mode=True,
        runner_module="host_runtime.antigravity",
        provider_key="antigravity",
        pid_required=True,
        autonomous_cli=True,
    ),
    mcp_write=McpWriteFacet(
        container_key="mcpServers", writer_key="antigravity",
        project_relpath=PROJECT_MCP, custom_writer=False, requires_absolutize=True,
        # ``bugx`` imports the complete CLI before handing off to the engine.
        # Antigravity ACP preloads stdio MCP with a short deadline, so the
        # writer must use the direct global-engine script launcher instead.
        entry_extra={"_bugx_direct_engine_script": True},
    ),
    acp=AcpFacet(plugin="antigravity"),
)
