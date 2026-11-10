#!/usr/bin/env python3
"""core/hosts/cline.py - Cline 插件（DOC-239 S2：纵切，事实只在这里声明）。

与 ``opencode`` 的首个切片（只声明、不改运行路径）不同，本切片是**纵切**：事实
产出后，原来的四处声明全部改成读本模块并删除——
``host_registry.HOST_MARKERS`` / ``bugx_cli.hosts.HOST_COMPONENTS`` /
``supervisor_host.ClineAdapter`` / ``worker_orchestrator`` 的 ``== "cline"`` 分支。
旧实现的注释被逐字搬到这里（它们是实测结论，不是解释）。

仍然在插件外、由「遗漏普查」守护登记的 Cline 事实（``tests/test_host_plugins.py``
的 ``OUTSIDE_PLUGIN`` 按文件+符号逐一核对；新增未登记字面量即测试失败）：

* ``core/worker_runtime.py``：claim 级 ``.cline_data`` 沙箱、凭据同步、provider/model
  对齐——那是**执行器实现**，不是宿主声明；清单与本模块 ``sandbox_sync`` 同源。
* ``src/bugx_cli/commands/plugin.py``：用户级数据目录的 MCP 写入实现（写入函数留在
  CLI 层，本模块只声明容器键/作用域）。
* ``core/host_acp/cline.py``：ACP 探针实现（本模块只声明模块名）。
"""

from __future__ import annotations

import os
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

_NAME = "cline"

#: 项目内必须存在的安装面（原 ``bugx_cli.hosts.registry.CLINE_PROJECT_PATHS``）。
_PROJECT_PATHS: tuple[str, ...] = (
    ".cline/agents/bugx.yaml",
    ".cline/agents/bugx-worker.yaml",
    ".cline/agents/bugx.md",
    ".cline/agents/bugx-worker.md",
)

#: claim 级 ``.cline_data`` 沙箱必须同步的认证/模型文件。三处同源：本模块、
#: ``core/worker_runtime._CLINE_SANDBOX_SYNC_FILES``、
#: ``core/host_acp/cline.CLINE_SANDBOX_SYNC_FILES``（守卫双向比对）。
_SANDBOX_SYNC_FILES: tuple[str, ...] = (
    "settings/providers.json",
    "settings/models.json",
    "settings/global-settings.json",
    "globalState.json",
)

_CUSTOM_TITLE = '# BugX\n\n这是 BugX 的 Cline 项目级 Skill 入口。'

_CUSTOM_PREAMBLE = '先读取全局引擎协议：\n通过 `bugx engine path --name bugx-runtime-contract.md` 和 `bugx engine path --name bugx-quick.md` 读取全局协议，\n并遵守其中的寿命、Supervisor、证据和报告协议。\n\n该入口只把任务交给当前 Cline；BugX CLI 只安装资源，不启动 Cline、不执行审计。\n项目未被 Cline 信任时，请先在 Cline 中信任项目；进入会话后本 Skill 由 Cline 自动发现，也可用 `/bugx` 显式触发（Cline CLI 没有 `--agent` 启动参数）。\n公开 Agent 模板来源：`{project_source}`。\n'


def _data_dir(root: Path) -> Path:
    """Cline 数据目录（``CLINE_DATA_DIR`` 可覆盖；相对路径按项目根解析）。

    Cline CLI 3.x 只读 ``<data>/settings/cline_mcp_settings.json``，**不存在**
    工作区级 MCP 作用域。2026-10-22 真机对照实验（cline 3.0.70，ACP，只差一个
    环境变量）：``CLINE_DATA_DIR`` 指到沙箱 ⇒ 沙箱配置里的 stdio server 在首个
    prompt 被真实 spawn；``CLINE_MCP_SETTINGS_PATH`` 指到同一份配置 ⇒ marker
    从未出现（该变量是死开关，继续支持只会让健康检查盯着宿主永不加载的文件）。
    2026-10-23 复核（cline 3.0.73）：``--help`` 仍声明 ``--data-dir``/
    ``--auto-approve``/``-m``，ACP 插件据此**运行时探测** flag 后再拼 argv；
    ``CLINE_MCP_SETTINGS_PATH`` 对照实验未重跑，实现侧不再引用该变量。
    """

    data_dir = os.environ.get("CLINE_DATA_DIR", "").strip()
    base = Path(data_dir).expanduser() if data_dir else Path.home() / ".cline" / "data"
    if not base.is_absolute():
        base = root / base
    return base


def _mcp_settings_path(root: Path) -> Path:
    return _data_dir(root) / "settings" / "cline_mcp_settings.json"


def _headless_argv(ctx: RunContext) -> list[str]:
    command = ["cline", "--json"]
    if ctx.session:
        command += ["--id", ctx.session]
    return [*command, ctx.prompt]


def _autonomous_argv(ctx: RunContext) -> list[str]:
    command = ["cline", "--json", "--auto-approve", "true"]
    if ctx.session:
        command += ["--id", ctx.session]
    return [*command, ctx.prompt]


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    return (_mcp_settings_path(root),)



#: Recon Scout 的最小系统指令：只要一段严格 JSON，禁止调用工具或写文件。
_RECON_SCOUT_SYSTEM = (
    "You are BugX Lead Semantic Auditor. You must analyze the input context and output "
    "strictly a JSON object matching the requested schema directly to stdout. "
    "Do NOT call any tools. Do NOT write any files."
)


def _recon_scout_argv(
    root: Path, prompt_text: str, prompt_file: Path,
    inventory: object | None = None, *, data_dir: Path | None = None,
) -> list[str]:
    """Cline 的一次性无头侦察 argv。

    ``data_dir`` 由引擎按 ``recon_scout_isolated_data`` 声明创建后注入：指向一个
    ``mcpServers`` 强制为空、但保留模型凭据的隔离目录，避免 Scout 拉起常驻
    MCP 进程锁死被测项目。未注入时退回 ``--data-dir`` 缺省（不写任何文件）。
    """
    _ = root, prompt_text, inventory
    argv = ["cline", "--json", "--thinking", "low", "-s", _RECON_SCOUT_SYSTEM]
    if data_dir is not None:
        argv.extend(["--data-dir", str(data_dir)])
    argv.extend(["-c", str(root), prompt_text or f"@{prompt_file}"])
    return argv

PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="Cline",
        order=4,
        executable="cline",
        markers=(".cline",),
        aliases=("roo", "roo-cline"),
        app_markers=None,
        native_question=True,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="skill",
        path=".cline/skills/bugx/SKILL.md",
        marker=".cline",
        required_paths=_PROJECT_PATHS,
        capabilities=frozenset(
            {
                "native-mcp-config",
                "user-agent-registration",
                "extra-tracked-paths",
                "workspace-mirror",
            }
        ),
        interaction={
            "tool": "Cline 原生选择控件；否则代理按 multiple 映射 BUGX_INTERACTION_REQUEST",
            "single_select": "等价于 multiple=false",
            "failure": "控件失败时停在当前 gate；无 TTY 时代理接管",
        },
        # Cline 的项目内 MCP 只剩 Roo Code 镜像；用户级配置在数据目录内。
        mcp_relpaths=(".roo/mcp.json",),
        worker_subagent=True,
        worker_entry_path=".cline/agents/bugx-worker.yaml",
        custom_title=_CUSTOM_TITLE,
        custom_preamble=_CUSTOM_PREAMBLE,
        # 原 ``post_phase._GIT_HOST_GENERATED_DIRS`` / ``symbol_index.HOST_RUNTIME_DIRS``
        # 的 ``.cline``：Cline 在自己的工作区目录里放会话状态，不是被审计产品。
        generated_dirs=(".cline",),
        # 原 ``common._SWEEPABLE_SKILL_ADAPTERS`` 成员：入口按 Skill 语义整体清扫。
        sweepable=True,
    ),
    engine_run=EngineRunFacet(
        supports_headless=True,
        headless_argv=_headless_argv,
        autonomous_argv=_autonomous_argv,
        mcp_config_paths=_mcp_config_paths,
        mcp_health=None,
        host_surface="tui",
        # Recon Scout：Cline 默认读 ~/.cline/data 下已安装的 bugx-supervisor，后台
        # hub 会拉起常驻 ``bugx mcp serve`` 并锁死项目 controller.lock。侦察必须用
        # 隔离 data-dir（保留 providers.json 凭据、mcpServers 强制为空）。目录的
        # 创建与落盘属于引擎侧职责——插件只声明「需要隔离数据面」这个事实。
        recon_scout_argv=_recon_scout_argv,
        recon_scout_isolated_data=True,
        # Cline 没有会话内 ``Task(subagent_type=...)``：Worker 只能串行或进程池。
        native_task=False,
    ),
    worker_runtime=WorkerRuntimeFacet(
        mode="serial",
        # 进程池模式的并发硬顶（原 ``worker_orchestrator`` 的 ``min(limit, 6)``）。
        max_concurrency=6,
        requires_process_mode=True,
        sandbox_sync=_SANDBOX_SYNC_FILES,
        # claim 级数据沙箱目录名（``worker_runtime`` 的沙箱实现落在 artifact_root 下）。
        sandbox_dir=".cline_data",
        # 原 ``worker_runtime`` 的 ``host == "cline"`` 分支：实现体在
        # ``core/host_runtime/cline.py``，本模块只声明模块名。
        runner_module="host_runtime.cline",
        # Supervisor ``provider=`` 标签（原 ``_execute_cline_cli_worker`` 的字面量）。
        provider_key=_NAME,
        # 原 ``worker_sync`` 的 ``provider == "cline"`` 告警分支。
        pid_required=True,
        # 原 ``autonomous`` 的 ``execute = host == "cline"`` 与 ``--host choices``。
        autonomous_cli=True,
        # 原 ``autonomous`` 的 ``2 <= workers <= 6`` 硬校验。
        concurrency_bounds=(2, 6),
    ),
    mcp_write=McpWriteFacet(
        container_key="mcpServers",
        writer_key=_NAME,
        # 无工作区级作用域：唯一被加载的是用户级数据目录 settings。
        project_relpath=None,
        global_scope="cline_data_dir/settings/cline_mcp_settings.json",
        mirror_relpaths=(".roo/mcp.json",),
        requires_absolutize=True,
        # Cline 的 server 条目必须带 ``disabled``/``autoApprove`` 两个字段，否则
        # Cline 视为未启用（原 ``plugin._host_supervisor_server_definitions`` 的
        # ``shape == "cline"`` 分支）。
        entry_extra={"disabled": False, "autoApprove": []},
    ),
    acp=AcpFacet(plugin=_NAME),
)

