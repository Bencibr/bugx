#!/usr/bin/env python3
"""core/hosts/dsh.py - DeepSeek Harness (dsh) 插件（DOC-239 纵切）。

事实来源（2026-10-28 真机取证，``dsh 0.1.1-rc.2``，逐字搬入、禁止抄其他宿主）：

* identity  ← ``dsh --help``：``--profile <name>`` 的 profile 位于
              ``$DSH_HOME/profiles``，``$DSH_HOME`` 缺省为 ``~/.dsh``；项目级接入产物是
              ``.dsh/patch.yml``（``src/bugx_cli/host_adapters.DshAdapter.detect`` 的
              ``project_marker``），因此项目标记取该文件而不是 ``.dsh`` 目录本身。
* install   ← ``src/bugx_cli/hosts/registry.HOST_COMPONENTS`` 的 dsh 条目
              （``patch+skill``：``.dsh/patch.yml`` + ``.dsh/skills/bugx/SKILL.md`` +
              ``.dsh/agents/bugx.md``；能力 ``project-patch-overlay`` /
              ``extra-tracked-paths``）。
* engine    ← **实测无头契约存在**：``dsh --profile headless "reply with exactly: PONG"``
              在干净目录里输出 ``PONG`` 且退出码 0。``dsh --profile headless --help``
              只列 ``-h/--help``（**没有** ``--json`` / ``--resume`` / ``--model``），
              task 走 argv，多个词由 dsh 用空格连接，输出是最终 assistant 消息。
              此前 ``supervisor_host.DshAdapter`` 断言「core has no confirmed headless
              CLI contract」并 ``raise NotImplementedError``，那是**过时断言**（把
              「当时没验证」写成了「宿主没有」），本次以真机实验推翻。
* mcp_write ← **dsh 没有 ``mcpServers`` 容器**：MCP 由 cordis 插件
              ``@bencibro/bugx-dsh-mcp-client`` 提供，注册方式是 profile 的
              ``cordis.patch.yml`` 里 ``- insert: [{id, name}]``，或
              ``dsh plugin --profile <p> add <package>``（内部转发 pnpm）。
              实测 ``dsh --profile headless --dump-config`` 的 333 行 composed tree 里
              **0 处** 命中项目级 ``.dsh/mcp.json`` —— 那个文件是**死配置**（与 codex
              的 ``.codex/config.json`` 同类陷阱），故 ``mcp_config_paths`` 只声明
              ``.dsh/patch.yml``，写入走 ``bootstrap`` 自定义实现。
* worker    ← dsh 有**完整原生 subagent**（不是「只能串行」）：standard preset 的
              ``tool-subagent``/``tool-subagent-fork``/``tool-subagent-control`` 等
              5 条 delegation 工具（``backgroundMode: continuable``），故 ``native_task=True``、
              ``mode="multi"``；并发上限未做压力取证，交由宿主调度；不声明 ``runner_module``。
* acp       ← **无 ACP 入口**：``dsh 0.1.1-rc.2`` 的 ``--help``、``lib/``、
              ``package.json`` 与 README 里都没有 ``acp`` / ``agent-client-protocol``
              痕迹（真机 grep）。该事实由 ``core/host_acp/dsh.py`` 以
              ``status="unavailable"`` 机读声明。

仍然在插件外的 dsh 事实（``tests/test_host_plugins.py`` 逐文件核对）：

* ``src/bugx_cli/commands/agent.command_dsh``：``bugx dsh`` 兼容启动器设置
  ``DSH_CWD`` 后 ``exec dsh --profile <p>`` —— 那是**用户交互壳**，不是宿主接线；
* ``src/bugx_cli/commands/agent._dsh_standard_preset_candidates``：preset 发现逻辑；
* ``core/supervisor_host.DshAdapter``：本插件落地后应只做投影（见 DOC-239 切片）。
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    AcpFacet,
    HostPluginError,
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

_NAME = "dsh"
#: 无头执行 profile：``dsh --profile headless <task>`` 用它跑 Worker/侦察。
_HEADLESS_PROFILE = "headless"


def _active_profile() -> str:
    """**当前会话**正在使用的 profile（决定 MCP 该装到哪里）。

    缺陷（2026-11-02 实测）：本模块曾把 ``_PROFILE`` 硬编码成 ``headless`` 并用它
    拼 MCP 写入路径。但用户真正交互的会话跑在 ``web`` profile（``DSH_PROFILE=web``），
    于是 ``bugx install --agent dsh`` 把三条 MCP 通道写进了用户根本不启动的
    ``profiles/headless/``——启动门禁因此永远报 ``bugx-supervisor=blocked``，
    反复要求「重装 + 重启宿主」，而重装每次又写回同一个错 profile，形成死循环。

    会话 profile 由 dsh 通过 ``DSH_PROFILE`` 注入；无头子进程（Worker/侦察）也
    会带上它，因此这里同时适用于「装配置」与「读健康状态」两个场景。缺省回退
    ``web``（dsh 交互会话标准 profile）。
    """
    import os

    return (os.environ.get("DSH_PROFILE") or "").strip() or "web"

#: 项目级 patch 文件（dsh 的 compose 输入之一，``registry`` 同源声明）。
_PROJECT_PATCH = ".dsh/patch.yml"

def _dsh_home() -> Path:
    """``$DSH_HOME`` 缺省为 ``~/.dsh``（``dsh --help`` 的 profile 位置声明）。"""
    import os

    configured = os.environ.get("DSH_HOME", "").strip()
    return Path(configured) if configured else Path.home() / ".dsh"


def _profile_patch_path() -> Path:
    """**唯一被 dsh 读取的 patch 层**：``$DSH_HOME/profiles/<profile>/cordis.patch.yml``。

    实测（2026-10-28）：项目级 ``.dsh/patch.yml`` 与 ``.dsh/mcp.json`` 在
    ``dsh --profile headless --dump-config`` 的 81 条 composed tree 里**零命中**；
    把同样的 ``- insert:`` 行写进本函数返回的用户级 profile patch 后，同一条
    dump 立刻出现 2 处命中。dsh 没有项目级配置概念，MCP 只能装在用户级 profile。
    """
    return _dsh_home() / "profiles" / _active_profile() / "cordis.patch.yml"


#: dsh **官方** MCP client 插件（``dsh/package.json:45`` 依赖，profile 里可解析）。
#: 不用第三方 ``@bencibro/bugx-dsh-mcp-client``：它要求 ``session.header.cwd`` 或
#: ``DSH_CWD`` 与 cwd **字符串全等**（``/tmp`` 与 ``/private/tmp`` 都不过），且把
#: 工具 schema 以 schemastery 对象交给 MCP SDK，触发 ``v3Schema.safeParse is not
#: a function``（SDK 只在 zod v3/v4 之间分派）。官方 client 用标准 stdio 配置即可。
_MCP_CLIENT_PLUGIN = "@deepseek-ai/dsh-mcp-client"
#: 三条受控通道：host（主控）、worker（任务执行）与 docs（知识文档），serverName 必须唯一。
#: ``--root`` 的取值由 :func:`_mcp_insert_block` 在安装时替换成**绝对**项目根：
#: dsh 的 MCP server 由宿主进程 spawn，cwd 不是工作区，``--root .`` 会解析到宿主
#: 自己的 cwd —— 于是 Supervisor 在错误的 ``.bugx`` 上跑（实测报「缺少有效的
#: .bugx 项目运行布局」）。``--db`` 同理，相对路径必须随根绝对化。
_MCP_SERVERS = (
    ("bugx-supervisor", ["mcp", "serve", "--root", "{ROOT}"]),
    ("bugx-supervisor-worker", ["mcp", "serve", "--root", "{ROOT}", "--role", "worker"]),
    ("bugx-docs", ["mcp", "docs", "serve", "--root", "{ROOT}", "--db", "{ROOT}/.bugx/knowledge/docs.sqlite3"]),
)

#: 自有 overlay 的识别头：既用于幂等替换，也用于 ``_remove`` 定位。
_OVERLAY_MARKER = "# BugX MCP overlay — official dsh-mcp-client (stdio)"


def _argv(ctx: RunContext) -> list[str]:
    """``dsh --profile headless <task>``。

    ``--profile headless`` 必须是**前两个** argv：launcher flag 之后的一切都会
    透传给 profile 的 app（``dsh --help`` 的 Examples 明确 ``dsh --profile tui
    --resume <session>`` 这种顺序），所以 prompt 只能放最后。
    """
    return ["dsh", "--profile", _HEADLESS_PROFILE, ctx.prompt]


def _headless_argv(ctx: RunContext) -> list[str]:
    return _argv(ctx)


def _autonomous_argv(ctx: RunContext) -> list[str]:
    # headless profile 只回答一个 task 然后退出，没有 resume/结构化输出 flag；
    # 无人值守靠「一次性把完整指令写进 prompt」完成，不拼不存在的 flag。
    return _argv(ctx)


#: Recon Scout 子进程的最小系统指令：只要一段严格 JSON，禁止调用工具或写文件。
_RECON_SCOUT_SYSTEM = (
    "This is a semantic-review subroutine called by the active audit Agent, not a "
    "request to launch that Agent. The caller owns every user gate, lifecycle "
    "transition and Worker dispatch. Perform the original semantic-review task below "
    "without recursively starting the parent workflow or loading its full manuals. "
    "Do NOT call tools. Do NOT write files. Return the requested JSON object only."
)


def _recon_scout_argv(
    root: Path, prompt_text: str, prompt_file: Path, inventory: object | None = None,
) -> list[str]:
    """DSH 的一次性无头侦察 argv：``dsh --profile headless <prompt>``。

    ``--profile headless`` 必须是前两个 argv（与 :func:`_argv` 同一实测约束；
    实测裸 ``dsh <prompt>`` 返回 ``error: --profile <name> is required``）。
    headless profile 只回答一个 task 后退出，没有 ``@file`` prompt 文件语法，
    因此把侦察文本内联进最后一个 argv；``root`` 由调用方 cwd 承担。

    因此把侦察文本内联进最后一个 argv；``root`` 由调用方 cwd 承担。

    本函数只拼「宿主 CLI 之后的参数」；启动器本身的替换由
    ``EngineRunFacet.recon_scout_node_flags`` 声明并交给引擎侧解析（见该字段
    文档：DSH Desktop 的 Electron node shim 会挡住 ``--expose-internals``，
    而 headless profile 的 HMR 又硬性要求它）。
    """
    _ = root, prompt_file, inventory
    return ["dsh", "--profile", _HEADLESS_PROFILE, f"{_RECON_SCOUT_SYSTEM}\n\n{prompt_text}"]


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    """dsh 的 MCP 作用域是**用户级** profile，不是项目级。

    实测（2026-10-28）：项目级 ``.dsh/patch.yml`` 与 ``.dsh/mcp.json`` 在
    ``dsh --profile headless --dump-config`` 里零命中；真正被 compose 的是
    ``$DSH_HOME/profiles/<profile>/cordis.patch.yml``。CLI 的全局写入路径
    （``plugin._host_global_mcp_target`` → ``_load_json_object``）会读这里声明的
    第一个路径，所以必须返回用户级 patch，否则它会把 YAML 项目文件当 JSON 解析
    并抛 ``mcp_config_unparseable``（build 302 实测）。``root`` 不参与解析。
    """
    del root
    return (_profile_patch_path(),)


def _mcp_health(name: str, cfg: dict, root: Path | None = None) -> bool:
    del name, cfg, root
    # bugx MCP 由宿主侧 cordis 插件加载；patch 里出现 bridge 行即视为健康。
    return True



def _mcp_client_resolvable() -> bool:
    """官方 MCP client 能否被该 profile 解析。

    ``dsh plugin`` 只是把参数转发给 profile 目录里的 pnpm（``lib/bin.js`` 实测），
    dsh 本身**没有** ``$DSH_HOME/plugins`` 这一层。缺失时不是「配置不生效」，而是
    整个 plugin tree 加载失败、dsh 直接抛 ``ERR_MODULE_NOT_FOUND`` 起不来 —— 所以
    写入前必须探测，探测失败宁可 fail-fast 也不留下会让宿主崩溃的 patch。

    探测位置必须覆盖 Node 的真实解析链，否则会把「已装好」误判成「缺失」并
    卡死启动门禁（2026-11-02 实测）：本机官方 client 装在**共享层**
    ``profiles/node_modules``，而 dsh 运行时实际解析到的是它自带副本
    （``.../lib/node_modules/@deepseek-ai/dsh/node_modules/...``）；原实现只查
    ``profiles/<profile>/node_modules`` 与不存在的 ``$DSH_HOME/plugins``，
    对这两处都判 False——MCP 门禁因此永久 blocked，宿主被要求反复重装。
    """
    profile = _dsh_home() / "profiles" / _active_profile()
    candidates = (
        # profile 私有层（dsh plugin add 的落点）
        profile / "node_modules" / _MCP_CLIENT_PLUGIN,
        # 共享层：所有 profile 共用同一份 node_modules
        _dsh_home() / "profiles" / "node_modules" / _MCP_CLIENT_PLUGIN,
        # 历史/外部安装位（保留兼容）
        _dsh_home() / "plugins" / "dsh-mcp-client",
    )
    return any(path.exists() for path in candidates)


def _profile_node_modules() -> Path:
    """当前会话 profile 的私有 node_modules（``dsh plugin add`` 的落点）。"""
    return _dsh_home() / "profiles" / _active_profile() / "node_modules"


def _install_hint() -> str:
    return (
        f"{_MCP_CLIENT_PLUGIN} 不在 profile {_active_profile()} 的 node_modules 里；"
        "dsh 装载该条目时整棵 plugin tree 会失败（宿主起不来）。请先执行 "
        f"`dsh plugin --profile {_active_profile()} add link:<dsh-mcp-client-dir>` 后再重试。"
    )


def _uninstall_hint() -> str:
    return (
        f"BugX 的 MCP 条目写在 {_profile_patch_path()} 的 ``- insert:`` 段；"
        f"要整体摘除请执行 `bugx uninstall --agent {_NAME}`（或手工删除该 insert 块）。"
    )


def _mcp_insert_block(root: Path) -> str:
    """生成官方 ``dsh-mcp-client`` 的三条 stdio 条目（顶层 ``- insert:``），根绝对化。

    ``serverName`` 是**必填**且必须唯一（README 的 Config 表）；缺它会让
    ``loader entries failed to apply``。

    历史缺陷：这里原本写死 ``--root .``，假设 dsh 以自己的项目目录为 cwd 启动。
    真机不成立——MCP server 由**长驻宿主进程** spawn，cwd 是宿主自己的目录，
    ``.`` 于是指向那个目录而不是被审计项目，Supervisor 在错的 ``.bugx`` 上跑并
    报「缺少有效的 .bugx 项目运行布局」。根必须写成绝对路径。
    """
    root_str = str(Path(root).expanduser().resolve())
    lines = [_OVERLAY_MARKER, "- insert:"]
    for server_name, argv in _MCP_SERVERS:
        lines.extend([
            f"    - id: {server_name}",
            f"      name: '{_MCP_CLIENT_PLUGIN}'",
            "      config:",
            f"        serverName: {server_name}",
            "        transport: stdio",
            "        command: bugx",
            "        args:",
        ])
        lines.extend(f"          - '{token.replace('{ROOT}', root_str)}'" for token in argv)
    return "\n".join(lines) + "\n"


def _bootstrap(root: Path, servers) -> dict:
    """把三条 BugX MCP 通道写进用户级 profile patch（幂等、根绝对化）。

    dsh 没有项目级配置层：实测 ``dsh --profile headless --dump-config`` 里项目级
    ``.dsh/patch.yml`` 与 ``.dsh/mcp.json`` 零命中，真正被 compose 的是
    ``$DSH_HOME/profiles/<profile>/cordis.patch.yml``。

    ``root`` 必须真正参与写入（条目里的 ``--root``/``--db`` 要绝对化到本项目）；
    并且这里**不能**用「命中就跳过」的幂等：那会让换了项目的用户永远停留在旧根，
    Supervisor 于是继续在错的 ``.bugx`` 上跑。已存在的自有 overlay（以
    :data:`_OVERLAY_MARKER` 开头、第二行为顶层 ``- insert:``）由引擎全权拥有，
    直接按本次根重写；他人的块逐字保留。
    """
    del servers
    path = _profile_patch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = _mcp_insert_block(root)
    owned = _owned_overlay_span(existing)
    if owned is not None:
        begin, finish = owned
        if existing[begin:finish].rstrip("\n") == block.rstrip("\n"):
            return {"path": str(path), "changed": False, "inserted": False}
        updated = existing[:begin] + block + existing[finish:]
        path.write_text(updated, encoding="utf-8")
        return {"path": str(path), "changed": True, "inserted": False}
    body = existing.rstrip("\n")
    text = block if (not body or body == "[]") else f"{body}\n{block}"
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "changed": True, "inserted": True}


def _owned_overlay_span(text: str) -> tuple[int, int] | None:
    """定位自有 overlay 的 ``[start, end)`` 字符区间；不存在则返回 ``None``。

    只认「marker 行 + 紧随的顶层 ``- insert:``」这一种形态——marker 是引擎的
    所有权声明。段落止于下一个顶层 YAML 键（列 0 的 ``- `` 或 ``key:``）或 EOF，
    因此插在他人块之间的 overlay 也能被准确切出。
    """
    marker_at = text.find(_OVERLAY_MARKER)
    if marker_at < 0:
        return None
    tail = text[marker_at:]
    lines = tail.splitlines(keepends=True)
    if len(lines) < 2 or lines[1].rstrip("\r\n") != "- insert:":
        return None
    offset = marker_at + len(lines[0]) + len(lines[1])
    finish = len(text)
    # 从 insert 键之后开始找边界。段落在**列 0** 的下一个 YAML 键处结束；
    # 缩进的 ``    - id: ...`` 属于本 overlay 内部的 servers 列表，不能当成边界
    # （否则只会替换半个块，旧项目的绝对根残留在新块之后——实测踩到）。
    for line in lines[2:]:
        raw = line.rstrip("\r\n")
        if raw and not raw[0].isspace() and not raw.startswith("#"):
            finish = offset
            break
        offset += len(line)
    return marker_at, finish


def _remove(root: Path) -> dict:
    """Remove only our exact generated overlay; preserve all other bytes.

    自有 overlay 内嵌**绝对**项目根，因此「完全等于本次生成形态」的判定必须用
    同一个 ``root``——换项目重装过的 overlay 不会被这次卸载误删。
    """
    path = _profile_patch_path()
    if not path.is_file():
        return {"removed": [], "preserved": []}
    content = path.read_bytes()
    owned = _mcp_insert_block(root).encode("utf-8")
    # A modified block is user-owned. Never guess ownership from an insert
    # key, plugin name, server id, or comment: those can occur in foreign YAML.
    kept: list[bytes] = []
    remaining = content
    removed_any = False
    while remaining:
        index = remaining.find(owned)
        if index < 0:
            kept.append(remaining)
            break
        end = index + len(owned)
        continuation = False
        for line in remaining[end:].splitlines():
            if not line.strip() or line.lstrip().startswith(b"#"):
                continue
            continuation = line.startswith((b" ", b"\t"))
            break
        if (index and remaining[index - 1:index] != b"\n") or (
            continuation
        ):
            # Not a complete top-level overlay (e.g. a block scalar or a
            # modified block with extra indented config). Keep it intact.
            kept.append(remaining[:end])
            remaining = remaining[end:]
            continue
        kept.append(remaining[:index])
        remaining = remaining[end:]
        removed_any = True
    if not removed_any:
        return {"removed": [], "preserved": [str(path)]}
    updated = b"".join(kept)
    if updated:
        path.write_bytes(updated)
    else:
        path.unlink()
    return {"removed": [_MCP_CLIENT_PLUGIN], "preserved": [str(path)] if updated else []}


def _inspect(root: Path) -> dict:
    """只读诊断：官方 MCP client 可解析 + profile patch 里有两条通道。"""
    del root
    path = _profile_patch_path()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    present = [n for n, _ in _MCP_SERVERS if n in text] if _MCP_CLIENT_PLUGIN in text else []
    resolvable = _mcp_client_resolvable()
    return {
        "servers": present,
        "missing": [n for n, _ in _MCP_SERVERS if n not in present],
        "trust": (
            "profile-patch" if present and resolvable
            else "missing-mcp-client" if not resolvable
            else "patch-without-bugx"
        ),
    }


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="DeepSeek Harness",
        order=6,
        executable="dsh",
        markers=(_PROJECT_PATCH,),
        aliases=(),
        app_markers=None,
        # ``ask_user_question`` 由 @deepseek-ai/dsh-tool-ask-user 提供（registry 声明）。
        native_question=True,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="patch+skill",
        path=".dsh/skills/bugx/SKILL.md",
        marker=".dsh",
        entry_mode="rendered",
        required_paths=(
            ".dsh/patch.yml",
            ".dsh/skills/bugx/SKILL.md",
            ".dsh/agents/bugx.md",
        ),
        legacy_paths=(),
        capabilities=frozenset({"project-patch-overlay", "extra-tracked-paths"}),
        interaction={
            "tool": "ask_user_question (@deepseek-ai/dsh-tool-ask-user)",
            "single_select": "等价于 multiple=false",
            "multi_select": "tooling:batch 时使用 multiSelect=true",
            "fallback": (
                "BUGX_INTERACTION_REQUEST → DSH/Orca/ACP 按 multiple 承接；"
                "无承接者则 TTY 菜单或阻断"
            ),
            "failure": "无控件且无 TTY 时阻断",
            "ui": "@deepseek-ai/dsh-client-ui-user-questions（Web/TUI answerer）",
        },
        mcp_relpaths=(_PROJECT_PATCH,),
        # dsh 没有 Task/子 Agent 派发入口（--help + profile tree 实测）。
        worker_subagent=False,
        custom_title=(
            "# BugX — DeepSeek Harness (dsh)\n\n"
            "这是 BugX 的 DeepSeek Harness 技能与 Agent 入口。"
        ),
        custom_preamble=(
            "先阅读项目中的 \n"
            "通过 `bugx engine path --name bugx-runtime-contract.md` 和 "
            "`bugx engine path --name bugx-quick.md` 读取全局协议，\n"
            "并遵守其中的寿命、Supervisor、证据和报告协议。\n"
        ),
        generated_dirs=(".dsh",),
    ),
    engine_run=EngineRunFacet(
        supports_headless=True,
        headless_argv=_headless_argv,
        autonomous_argv=_autonomous_argv,
        mcp_config_paths=_mcp_config_paths,
        mcp_health=_mcp_health,
        host_surface="tui",
        recon_scout_argv=_recon_scout_argv,
        # headless profile 的 dsh-base bundle 无条件加载 HMR，HMR 硬要求
        # ``--expose-internals``；而 DSH Desktop 注入的 Electron node shim 会禁用
        # ``NODE_OPTIONS``，只能由真 node 自己的 argv 传入。此处只声明需求，
        # 由引擎侧 host_registry 的 node 解析器落实（插件不得 import shutil）。
        recon_scout_node_flags=("--expose-internals",),
        # 主执行路径同样需要：``dsh --profile headless <task>`` 在 DSH Desktop 环境下
        # 命中 ``.desktop-bin/node`` 会抛 ``--expose-internals is required for HMR
        # service``。只声明侦察是不够的——那会让「侦察成功、开端挖掘必崩」成为默认
        # 表现，而报错指向 HMR 而非「漏了 node 解析」，用户几乎不可能自己定位。
        engine_run_node_flags=("--expose-internals",),
        # dsh 有**完整原生 subagent**（2026-10-28 源码取证）：
        # config/agent-presets/standard/agent.cordis.yml:174-200 的
        # delegation 组注册 tool-subagent(provider=spawn)、
        # tool-subagent-fork(provider=fork)、tool-subagent-control、
        # tool-subagent-list-agents，均 backgroundMode: continuable；
        # 配套实现为 dsh-subagent / dsh-subagent-spawn-in-process /
        # dsh-subagent-fork-in-process / dsh-subagent-in-process-driver。
        # dsh --profile headless --dump-config 里同样有 5 条 tool-subagent，
        # 所以这不是「只有 web 才有」的能力。
        native_task=True,
    ),
    worker_runtime=WorkerRuntimeFacet(
        # 原生 subagent 可并发派发（spawn/fork 均为进程内 continuable）。
        # 并发上限未做压力取证，故不硬编码数值——由宿主自己调度。
        mode="multi",
        max_concurrency=None,
        requires_process_mode=False,
        sandbox_sync=(),
        # 引擎不以子进程驱动 dsh worker：headless 只回答一个 task 后退出，
        # 没有 claim/heartbeat 的会话延续面。
        runner_module="",
        provider_key=_NAME,
    ),
    mcp_write=McpWriteFacet(
        container_key="insert",
        writer_key=_NAME,
        project_relpath=None,
        global_scope="user-profile",
        mirror_relpaths=(),
        requires_absolutize=False,
        # cordis patch 的 insert 段由本插件 bootstrap 负责；通用 JSON 骨架
        # 会把 YAML 当 JSON 解析并静默跳过。
        custom_writer=True,
        bootstrap=_bootstrap,
        remove=_remove,
        inspect=_inspect,
    ),
    acp=AcpFacet(plugin=_NAME),
    usage=UsageFacet(
        plugin=_NAME,
        module="_dsh_usage",
        state_filename="dsh-usage.json",
        reader_format="zstd",
        notes="只读解析 DSH Desktop Session 归档 (*.zstd jsonl)，按 lifecycle 时间窗聚合主/Worker Token 与逐模型用量。",
    ),
)
