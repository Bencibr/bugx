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
* mcp_write     ← 配置容器键为 ``mcpServers``，项目级作用域为 ``.pi/mcp.json``；
                  根绝对化在插件 ``bootstrap`` 写入路径落地（``Path.resolve()``
                  后落盘，dsh 同类 cwd 陷阱已消解）。
* acp           ← 上游 ``pi 0.85.1`` 无 ACP 子命令/flag，由 ``core/host_acp/pi.py``
                  以 ``status="unavailable"`` 声明。
"""

from __future__ import annotations

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


_MCP_SERVERS_KEY = "mcpServers"


def _absolutize_mcp_args(args: list[str], root_str: str, root: Path) -> list[str]:
    """把 server argv 里的 ``--root`` 与项目内 ``--db`` 绝对化到本项目。

    只改写这两个 token 的**下一个**参数；其余 argv 逐字保留。
    """
    resolved = list(args)
    for index, token in enumerate(resolved):
        if token == "--root" and index + 1 < len(resolved):
            resolved[index + 1] = root_str
        elif (
            token == "--db"
            and index + 1 < len(resolved)
            and str(resolved[index + 1]).startswith(".bugx/")
        ):
            resolved[index + 1] = str(root / str(resolved[index + 1]))
    return resolved


def _bootstrap(root: Path, servers) -> dict:
    """把 BugX 的 MCP 通道写进项目级 ``.pi/mcp.json``（幂等、根绝对化）。

    历史缺陷（与 dsh 已记录陷阱同类）：条目曾写死 ``--root .``，假设 Pi 以被测
    项目为 cwd spawn stdio MCP server。MCP server 由长驻 Pi 宿主进程 spawn，
    cwd 是 Pi 自己的目录，``.`` 于是指向宿主目录而不是被审计项目。插件声明
    ``requires_absolutize=True`` 的语义在**本写入路径**落地：root 先
    ``Path.resolve()`` 绝对化，再落盘 ``--root``/``--db``。

    幂等语义与 dsh 一致：不能用「命中就跳过」——那会让换了项目的用户永远停留
    在旧根；只按传入的 BugX 自有 server 名逐字段比对重写，``mcpServers`` 下
    他人的配置逐字保留。非对象形态的既有配置 fail-fast，绝不静默覆盖。
    """
    import json

    import core.secure_atomic as secure_atomic

    resolved = Path(root).expanduser().resolve()
    target = resolved / ".pi" / "mcp.json"
    if target.is_symlink():
        raise OSError(f"拒绝通过符号链接写入 Pi MCP 配置: {target}")
    existed = target.exists()
    payload: dict = {}
    if existed:
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise OSError(f"Pi MCP 配置无法解析: {target}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise OSError(
                f"Pi MCP 配置顶层必须是对象: {target}。"
                '合法形态为 {"mcpServers": {"<name>": {...}}}；'
                "若文件为空或只有一个列表，请重建为对象后再重试。"
            )
        payload = loaded
    container = payload.get(_MCP_SERVERS_KEY)
    if container is None:
        container = {}
        payload[_MCP_SERVERS_KEY] = container
    if not isinstance(container, dict):
        raise OSError(f"Pi MCP 配置 mcpServers 必须是对象: {target}")
    root_str = str(resolved)
    desired: dict[str, dict] = {}
    for name, entry in dict(servers).items():
        normalized = dict(entry)
        args = normalized.get("args")
        if isinstance(args, list) and all(isinstance(token, str) for token in args):
            normalized["args"] = _absolutize_mcp_args(args, root_str, resolved)
        desired[str(name)] = normalized
    changed = any(container.get(name) != entry for name, entry in desired.items())
    if not changed:
        return {
            "path": str(target), "changed": False, "created": False,
            "servers": sorted(desired),
        }
    mode = target.stat().st_mode & 0o777 if existed else 0o600
    container.update(dict(desired))
    target.parent.mkdir(parents=True, exist_ok=True)
    secure_atomic.atomic_write_json(target, payload, mode=mode)
    return {
        "path": str(target), "changed": True, "created": not existed,
        "servers": sorted(desired),
    }


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
        # pi 无自有 CLI worker 协议：审计循环仍由引擎默认执行器
        # （``worker_runtime._execute_autonomous_worker``）独家承载，本模块
        # （``host_runtime.pi``）只做宿主 profile 预算透传（见下）。
        runner_module="host_runtime.pi",
        # 单 claim 墙钟预算：集成验证实测（2026-11-17）——pi 无 runner_module 时
        # 模型 CLI 超时回落 tier 阶梯 recon=180s，codex 模型在 checkpoint 准备
        # 阶段超过 180s 即被 ``run_model`` 击杀（与 E1 cline FAIL 同根因，
        # build.566 只覆盖了 cline）。与 cline 同值 600s。
        worker_timeout_seconds=600.0,
    ),
    mcp_write=McpWriteFacet(
        container_key="mcpServers",
        writer_key=_NAME,
        project_relpath=".pi/mcp.json",
        global_scope=None,
        mirror_relpaths=(),
        requires_absolutize=True,
        # ``requires_absolutize`` 的落地点：引擎侧启动/自治引导（bootstrap_host_mcp）
        # 委托本实现，落盘前把 --root/--db 绝对化（dsh 同类 cwd 陷阱）。
        bootstrap=_bootstrap,
        entry_extra={},
    ),
    acp=AcpFacet(plugin=_NAME),
)


def is_available() -> tuple[bool, str]:
    """降级契约：机械探活 Pi CLI 是否安装（零子进程 ``shutil.which``）。

    宿主探活语义：CLI 缺失时无头 ``pi --approve -p``、自治通道与 Pi 扩展面
    全部不可用，调用方必须走 ``run_static_degraded`` 输出静态事实声明，
    禁止伪装宿主可用。
    """
    path = shutil.which("pi")
    if path is None:
        return False, "pi 可执行文件未安装；headless（pi --approve -p）与自治通道不可用"
    return True, path


def run_static_degraded(root: Path | None = None) -> dict:
    """Pi CLI 缺失时的静态降级输出：只声明插件事实，绝不伪装宿主健康。

    输出与宿主真实可用状态字段级可区分：``status="degraded"`` + ``degraded=True``
    + ``degraded_reason``，且 ``headless_available`` / ``native_task_available``
    跟随真实探活结果；MCP 健康检查在 CLI 缺失时显式声明 ``unknown``。
    """
    available, availability_detail = is_available()
    return {
        "plugin": _NAME,
        "schema_version": 1,
        "success": available,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": (
            "static_facts_only" if available else "pi_cli_not_installed"
        ),
        "probe_mode": "static_degraded",
        "headless_available": available,
        "native_task_available": bool(available and PLUGIN.engine_run.native_task),
        "mcp_health": "unknown" if not available else "not_probed",
        "identity": {
            "executable": PLUGIN.identity.executable,
            "aliases": list(PLUGIN.identity.aliases),
            "markers": list(PLUGIN.identity.markers),
        },
        "agent_entry": PLUGIN.install.path,
        "mcp_project_relpath": PLUGIN.mcp_write.project_relpath,
        "mcp_global_scope": PLUGIN.mcp_write.global_scope,
        "mcp_config_format": getattr(PLUGIN.mcp_write, "config_format", "json"),
        "findings": [],
        "evidence": {
            "availability_detail": availability_detail,
            "project_root": str(root) if root else None,
        },
        "metadata": {
            "degraded_mode": "static_plugin_facts_only",
            "acp": "unavailable（机读声明，见 core/host_acp/pi.py）",
            "fallback": "宿主 CLI 缺失时由外层宿主/用户决策接管；BugX 不代装宿主 CLI",
        },
    }
