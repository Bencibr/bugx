"""BugX AI Agent 宿主组件注册表。

统一管理所有受支持宿主组件声明，对外导出 HOST_COMPONENTS 清单与查找方法。
"""

from bugx_cli.infra.security import _find_repo_root

from pathlib import Path
from typing import Any

import os
import sys

from .base import (
    ExecutionContract,
    HostComponent,
    InstallContract,
    InteractionContract,
    McpContract,
)

ENTRY_PLUGIN_CATALOG = "plugin-catalog"
ENTRY_SKILL_PAYLOAD = "skill-payload"
ENTRY_SHARED_SYMLINK = "shared-symlink"
ENTRY_USER_REGISTRATION = "user-registration"
ENTRY_MULTI_PATH = "multi-path"
ENTRY_RENDERED = "rendered"

CAP_PREVALIDATE_CATALOG = "prevalidate-catalog"
CAP_PROJECT_MCP_CONFIG = "project-mcp-config"
CAP_PROJECT_PATCH_OVERLAY = "project-patch-overlay"
CAP_DISTRIBUTION_PLUGIN = "distribution-plugin"
CAP_EXTENSION_PAYLOAD = "extension-payload"
CAP_EXTRA_TRACKED_PATHS = "extra-tracked-paths"
CAP_GLOBAL_AGENT_LINK = "global-agent-link"
CAP_GLOBAL_SKILL_LINK = "global-skill-link"
CAP_NATIVE_MCP_CONFIG = "native-mcp-config"
CAP_USER_AGENT_REGISTRATION = "user-agent-registration"
CAP_USER_LEVEL_ARTIFACT = "user-level-artifact"
CAP_WORKSPACE_MIRROR = "workspace-mirror"

WORKBUDDY_PROJECT_PATHS = (
    ".workbuddy/skills/bugx/SKILL.md",
    ".codebuddy/skills/bugx/SKILL.md",
)


def _packaged_asset_cores() -> tuple[Path, ...]:
    """打包载荷内的引擎 ``core/`` 候选（``share/bugx-agent/core``）。

    全新机器上全局引擎尚未安装时，``bugx init`` 自己必须先能读到宿主插件：
    若插件解析只认已安装引擎，安装入口就反过来要求「安装已完成」——自举死锁。
    载荷目录正是 ``init`` 复制引擎的源，因此必须参与候选。

    zipimport（``PYTHONPATH=<wheel>``）下载荷不在真实文件系统上，候选自然落空，
    调用方会得到明确的「插件不可用」而不是崩溃。
    """

    candidates: list[Path] = []
    override = os.environ.get("BUGX_ASSET_ROOT", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    try:
        import sysconfig

        candidates.append(
            Path(sysconfig.get_path("data")) / "share" / "bugx-agent" / "core",
        )
    except (KeyError, TypeError, ValueError):
        pass
    candidates.append(
        _find_repo_root(Path(__file__)) / "share" / "bugx-agent" / "core",
    )
    return tuple(candidates)


def _engine_root_candidates() -> tuple[Path, ...]:
    """引擎 ``core/`` 的固定候选目录（有界，不做任何目录扫描）。

    顺序：显式 ``BUGX_GLOBAL_ENGINE_ROOT`` → 仓库内 ``core/``（开发态）→
    ``$BUGX_HOME/.bugx/core``（默认 ``~/.bugx/core``，安装态）→ 打包载荷
    ``share/bugx-agent/core``（全新安装尚未 ``init`` 时的唯一可用快照）。只有真正
    声明了目标宿主模块的候选才被采用，因此**过期的引擎快照不会顶替源码树**。
    """

    candidates: list[Path] = []
    override = os.environ.get("BUGX_GLOBAL_ENGINE_ROOT", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(_find_repo_root(Path(__file__)) / "core")
    home = os.environ.get("BUGX_HOME", "").strip()
    candidates.append((Path(home).expanduser() if home else Path.home()) / ".bugx" / "core")
    candidates.extend(_packaged_asset_cores())
    return tuple(candidates)


def _engine_root_for(host: str) -> Path | None:
    """返回声明了 ``hosts/<host>.py`` 的引擎 ``core/`` 目录；找不到返回 ``None``。"""

    for root in _engine_root_candidates():
        if (root / "hosts" / f"{host}.py").is_file():
            return root
    return None


def _engine_hosts_package(host: str):
    """导入承载 ``host`` 的引擎 ``hosts`` 包（必要时**追加**引擎 ``core/``）。

    追加（不是插入）保证 ``core/`` 永远不会反向遮蔽 ``src/`` 与标准库。同一进程内
    ``hosts`` 只能绑定一个引擎快照：若已绑定但不认识目标宿主，直接 fail-closed 并给出
    可执行的处置（重启进程/重新装配），绝不在两个快照之间来回切换。
    """

    module = sys.modules.get("core.hosts")
    if module is not None:
        if module.get(host) is not None:
            return module
        raise RuntimeError(
            f"进程内已绑定的引擎 hosts 包不含宿主 {host!r}"
            f"（{getattr(module, '__file__', '?')}）；请重新启动进程或先装配正确引擎"
        )
    root = _engine_root_for(host)
    if root is None:
        raise RuntimeError(
            f"引擎宿主插件不可用：{host!r} 在任何候选 core/ 目录下都没有 "
            f"hosts/{host}.py（BUGX_GLOBAL_ENGINE_ROOT / 仓库 core/ / ~/.bugx/core）；"
            "宿主事实只能来自插件，禁止回退到 CLI 本地声明"
        )
    # 引擎脚本以 ``core.<子包>`` 绝对名互相导入（``core/`` 是命名空间包），
    # 只有父目录在 sys.path 上 ``import core.*`` 才解析得到；``root`` 本身要
    # 保留，供引擎内按路径执行的叶子脚本回指同目录兄弟模块。
    for entry in (root.parent, root):
        if str(entry) not in sys.path:
            sys.path.append(str(entry))
    import core.hosts as host_plugins
    return host_plugins


def engine_host_plugin(name: str):
    """返回引擎侧宿主插件（``core/hosts``，DOC-239 唯一事实来源）。

    依赖方向：CLI 读插件，插件不读 CLI。``core/hosts`` 只依赖标准库，因此引擎快照
    仍然可以在没有 CLI 的情况下被导入。插件缺失或未就绪即 fail-closed——静默回退到
    本地声明正是 DES-01（三份宿主清单漂移）的成因。
    """

    plugin = _engine_hosts_package(name).get(name)
    if plugin is None or not plugin.ready:
        raise RuntimeError(f"宿主 {name!r} 的引擎插件存在但未 ready（core/hosts/{name}.py）")
    return plugin


def engine_host_plugins() -> tuple[Any, ...]:
    """本进程可见引擎快照内的全部 ready 插件（确定性顺序，缺模块时返回空元组）。

    与 :func:`engine_host_plugin` 的区别：本函数用于**枚举**（推导能力清单、
    ``--host`` 选项），因此不因某个宿主缺失而抛错；单宿主的严格投影仍走前者。
    只在引擎根目录的固定候选里按 ``hosts/`` 目录判定，不做任何目录扫描。
    """

    module = sys.modules.get("core.hosts")
    if module is not None:
        return tuple(plugin for plugin in module.all() if plugin.ready)
    for root in _engine_root_candidates():
        if not (root / "hosts").is_dir():
            continue
        for entry in (root.parent, root):
            if str(entry) not in sys.path:
                sys.path.append(str(entry))
        import core.hosts as host_plugins

        return tuple(plugin for plugin in host_plugins.all() if plugin.ready)
    return ()


def _optional_engine_host_plugin(name: str):
    """``name`` 的插件；未迁移/未知/引擎缺模块时返回 ``None``（不抛错）。"""

    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return engine_host_plugin(name.strip().casefold())
    except (RuntimeError, ImportError):
        return None


def engine_host_label(name: str) -> str:
    """宿主展示名：插件 ``identity.label``；未迁移宿主回退到 CLI 组件表。"""

    plugin = _optional_engine_host_plugin(name)
    if plugin is not None:
        return plugin.identity.label
    try:
        return get_component(str(name).strip().casefold()).label
    except (ValueError, RuntimeError):
        return str(name)


def engine_host_runtime_flag(name: str, flag: str) -> bool:
    """读取插件 ``worker_runtime`` 面的布尔事实（如 ``autonomous_cli`` / ``pid_required``）。"""

    plugin = _optional_engine_host_plugin(name)
    if plugin is None:
        return False
    return bool(getattr(plugin.worker_runtime, flag, False))


def engine_host_concurrency_bounds(name: str) -> tuple[int, int] | None:
    """插件声明的 ``--workers`` 合法区间；未声明返回 ``None``。"""

    plugin = _optional_engine_host_plugin(name)
    return None if plugin is None else plugin.worker_runtime.concurrency_bounds


def engine_host_mcp_write(name: str):
    """插件的 ``mcp_write`` 面；未迁移宿主返回 ``None``。"""

    plugin = _optional_engine_host_plugin(name)
    return None if plugin is None else plugin.mcp_write


def engine_host_install(name: str):
    """插件的 ``install`` 面；未迁移宿主返回 ``None``。"""

    plugin = _optional_engine_host_plugin(name)
    return None if plugin is None else plugin.install


def engine_host_mcp_config_paths(name: str, root: Path) -> tuple[Path, ...]:
    """插件声明的 MCP 配置文件路径（全局作用域宿主返回用户级路径）。"""

    plugin = _optional_engine_host_plugin(name)
    if plugin is None:
        return ()
    return tuple(plugin.engine_run.mcp_config_paths(root))


def engine_sweepable_skill_hosts() -> tuple[str, ...]:
    """声明「按 Skill 清扫」语义的插件宿主名（``install.sweepable``）。"""

    return tuple(
        plugin.name for plugin in engine_host_plugins() if plugin.install.sweepable
    )


def sweepable_skill_adapter_names(legacy: tuple[str, ...] = ()) -> tuple[str, ...]:
    """清扫面适配器清单：``legacy`` 基线（未迁移宿主）+ 插件声明的可清扫宿主。

    两个调用点的基线本来就不同（``common`` 5 项 / ``plugin`` 7 项），因此基线由
    调用方给全；插件宿主统一追加并去重，保证已迁移宿主只声明一次。
    """

    names = list(legacy)
    for name in engine_sweepable_skill_hosts():
        if name not in names:
            names.append(name)
    return tuple(names)


def engine_mirror_root(name: str) -> str:
    """插件声明的工作区镜像根目录（``install.mcp_relpaths[0]`` 的父目录）。"""

    install = engine_host_install(name)
    if install is None:
        return ""
    for relpath in install.mcp_relpaths:
        parts = Path(relpath).parts
        if len(parts) >= 2:
            return parts[0]
    return ""


def engine_worker_entry_dir(name: str) -> str:
    """插件声明的 Worker 入口所在目录（``install.worker_entry_path`` 的父目录）。"""

    install = engine_host_install(name)
    if install is None or not install.worker_entry_path:
        return ""
    parts = Path(install.worker_entry_path).parts
    return str(Path(*parts[:-1])) if len(parts) >= 2 else ""


#: ``bugx autonomous`` / ``bugx goal`` 的 ``--host`` 展示基线（顺序即 help 顺序）。
#: 三项都是**原生 Task 派发**宿主（无 CLI 进程池执行器）；已迁移插件的取值由
#: ``worker_runtime.autonomous_cli`` 自动追加，未迁移宿主（claude）只能靠本表。
#: 这不是第二份宿主事实：宿主身份仍在插件/``SUPPORTED_HOSTS``，本表只说「CLI
#: 的 host 选项按什么顺序出现」。
AUTONOMOUS_HOST_BASELINE: tuple[str, ...] = (
    "claude", "codex", "opencode", "dsh",
)


def engine_host_cli_impl(host_name: str):
    """宿主专属 CLI 实现模块：约定名 ``bugx_cli.commands.plugin_<host>``。

    通用层（``lifecycle`` / ``agent`` / ``plugin``）不得按宿主名分支；宿主自己的安装、
    卸载、诊断实现放在约定模块里，通用层只按约定名寻址并调用其**可选钩子**
    （如 ``user_agent_surfaces`` / ``validate_install``）。找不到模块返回 ``None``。
    """

    if not isinstance(host_name, str) or not host_name.strip():
        return None
    import importlib

    module_name = f"bugx_cli.commands.plugin_{host_name.strip().casefold()}"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None


def engine_host_cli_hook(host_name: str, hook: str):
    """取 ``bugx_cli.commands.plugin_<host>`` 上的可选钩子；缺失返回 ``None``。"""

    module = engine_host_cli_impl(host_name)
    candidate = getattr(module, hook, None) if module is not None else None
    return candidate if callable(candidate) else None


def engine_host_user_agent_surfaces(host_name: str) -> list[dict[str, object]]:
    """宿主声明的用户级智能体目录清单（``user_agent_surfaces`` 钩子）。"""

    hook = engine_host_cli_hook(host_name, "user_agent_surfaces")
    return list(hook()) if hook is not None else []


def autonomous_host_names() -> tuple[str, ...]:
    """``bugx autonomous --host`` 的可选宿主：展示基线 + 声明 ``autonomous_cli`` 的插件。"""

    plugin_hosts = tuple(
        plugin.name
        for plugin in engine_host_plugins()
        if plugin.worker_runtime.autonomous_cli
    )
    return AUTONOMOUS_HOST_BASELINE + tuple(
        name for name in plugin_hosts if name not in AUTONOMOUS_HOST_BASELINE
    )


def _component_from_plugin(name: str) -> HostComponent:
    """把引擎插件投影成 CLI 侧 ``HostComponent``（不复制任何宿主事实）。"""

    plugin = engine_host_plugin(name)
    identity, install, engine = plugin.identity, plugin.install, plugin.engine_run
    return HostComponent(
        name=identity.name,
        label=identity.label,
        executable=identity.executable,
        marker=install.marker or identity.markers[0],
        install=InstallContract(
            install.kind,
            install.path,
            entry_mode=install.entry_mode,
            required_paths=install.required_paths,
            has_worker_subagent=install.worker_subagent,
            legacy_paths=install.legacy_paths,
        ),
        interaction=InteractionContract(**dict(install.interaction)),
        mcp=McpContract(config_relpaths=install.mcp_relpaths),
        execution=ExecutionContract(
            supports_headless=engine.supports_headless,
            host_surface=engine.host_surface,
        ),
        capabilities=install.capabilities,
        aliases=identity.aliases,
        tools_line=install.tools_line,
        custom_title=install.custom_title,
        custom_preamble=install.custom_preamble,
    )


#: 已完成 DOC-239 纵切的宿主：宿主事实只存在于引擎插件 ``core/hosts/<host>.py``。
#: 本清单只用于**定位/枚举插件**，不含任何宿主事实（不复制 label、路径、能力）。
#: 它随纵切推进增长；全部宿主迁移完成后本清单与 ``HOST_COMPONENTS`` 的本地字面量
#: 会一起消失。
PLUGIN_BACKED_HOSTS: tuple[str, ...] = ("cline", "codex", "claude", "pi", "zcode")


def plugin_backed_components() -> tuple[HostComponent, ...]:
    """把可用引擎快照里的插件投影成 CLI 组件；快照不可用时跳过（不抛错）。

    投影在 import 期完成，因此**必须容忍引擎缺失**：全新机器上全局引擎还没
    安装，CLI 若在这里抛错，``bugx init``（安装引擎的入口）反而无法启动。
    真正需要该宿主事实的调用点走 :func:`get_component`，那里 fail-closed。
    """

    components: list[HostComponent] = []
    for name in PLUGIN_BACKED_HOSTS:
        try:
            components.append(_component_from_plugin(name))
        except (RuntimeError, ImportError):
            continue
    return tuple(components)


HOST_COMPONENTS: tuple[HostComponent, ...] = (
    HostComponent(
        name="opencode",
        label="OpenCode",
        executable="opencode",
        marker=".opencode",
        install=InstallContract("agent", ".opencode/agents/bugx.md", entry_mode=ENTRY_SHARED_SYMLINK),
        interaction=InteractionContract(tool="question", single_select="multiple=false"),
        mcp=McpContract(config_relpaths=("opencode.json", ".opencode/opencode.json")),
        execution=ExecutionContract(supports_headless=True, host_surface="tui"),
        capabilities=frozenset({CAP_PROJECT_MCP_CONFIG, CAP_GLOBAL_AGENT_LINK}),
    ),
    HostComponent(
        name="dsh",
        label="DeepSeek Harness",
        executable="dsh",
        marker=".dsh",
        install=InstallContract(
            "patch+skill",
            ".dsh/skills/bugx/SKILL.md",
            required_paths=(
                ".dsh/patch.yml",
                ".dsh/skills/bugx/SKILL.md",
                ".dsh/skills/bugx-dsh/SKILL.md",
                ".dsh/agents/bugx.md",
            ),
        ),
        interaction=InteractionContract(
            tool="ask_user_question (@deepseek-ai/dsh-tool-ask-user)",
            single_select="等价于 multiple=false",
            multi_select="tooling:batch 时使用 multiSelect=true",
            fallback="BUGX_INTERACTION_REQUEST → DSH/Orca/ACP 按 multiple 承接；无承接者则 TTY 菜单或阻断",
            failure="无控件且无 TTY 时阻断",
            ui="@deepseek-ai/dsh-client-ui-user-questions（Web/TUI answerer）",
        ),
        custom_title="# BugX — DeepSeek Harness (dsh)\n\n这是 BugX 的 DeepSeek Harness 技能与 Agent 入口。",
        custom_preamble="先阅读项目中的 \n通过 `bugx engine path --name bugx-runtime-contract.md` 和 `bugx engine path --name bugx-quick.md` 读取全局协议，\n并遵守其中的寿命、Supervisor、证据和报告协议。\n",
        capabilities=frozenset({CAP_PROJECT_PATCH_OVERLAY, CAP_EXTRA_TRACKED_PATHS}),
    ),
    # DOC-239：Codex 已完成纵切，CLI 只做投影（事实在 core/hosts/codex.py）。
    # DOC-239：Cline 已完成纵切，CLI 只做投影（事实在 core/hosts/cline.py）。
    # DOC-239：Claude Code 已完成纵切，CLI 只做投影（事实在 core/hosts/claude.py）。
    # DOC-239：Pi 已完成纵切，CLI 只做投影（事实在 core/hosts/pi.py）。
    # DOC-239：ZCode 已完成纵切，CLI 只做投影（事实在 core/hosts/zcode.py）。
    *plugin_backed_components(),
    HostComponent(
        name="workbuddy",
        label="WorkBuddy AI",
        executable="workbuddy",
        marker=".workbuddy",
        install=InstallContract(
            "skill",
            WORKBUDDY_PROJECT_PATHS[0],
            entry_mode=ENTRY_MULTI_PATH,
            required_paths=WORKBUDDY_PROJECT_PATHS,
        ),
        interaction=InteractionContract(
            tool="WorkBuddy Skill / 应用内结构化交互",
            single_select="等价于 multiple=false",
            failure="停在当前 gate；由 WorkBuddy 应用继续承接",
        ),
        custom_title="# BugX\n\n这是 WorkBuddy AI 的 BugX Skill 入口。",
        custom_preamble="先读取全局引擎协议：\n通过 `bugx engine path --name bugx-runtime-contract.md` 和 `bugx engine path --name bugx-quick.md`（或直接读取物理文件 `~/.bugx/core/bugx-runtime-contract.md` 与 `~/.bugx/core/bugx-quick.md`）读取全局协议，\n并遵守其中的寿命、Supervisor、证据和报告协议。\n\nWorkBuddy 只负责承接 Skill 和用户交互；BugX CLI 只安装资源，不启动 WorkBuddy、\n不执行审计。\n公开 Agent 模板来源：`{project_source}`。\n",
        execution=ExecutionContract(supports_headless=False, host_surface="desktop"),
        capabilities=frozenset({CAP_USER_LEVEL_ARTIFACT}),
    ),
    HostComponent(
        name="chatgpt",
        label="ChatGPT",
        executable="chatgpt",
        marker=".agents/plugins/marketplace.json",
        install=InstallContract(
            "plugin",
            ".agents/plugins/marketplace.json",
            entry_mode=ENTRY_PLUGIN_CATALOG,
            required_paths=(
                "plugins/bugx/.codex-plugin/plugin.json",
                "plugins/bugx/skills/bugx/SKILL.md",
            ),
        ),
        interaction=InteractionContract(
            tool="ChatGPT 结构化选择控件",
            single_select="等价于 multiple=false",
            failure="控件失败时停在当前 gate",
        ),
        execution=ExecutionContract(supports_headless=False, host_surface="web"),
        capabilities=frozenset({CAP_PREVALIDATE_CATALOG}),
    ),
    HostComponent(
        name="qoder",
        label="Qoder AI",
        executable="qoder",
        marker=".qoder",
        install=InstallContract(
            "subagent",
            ".qoder/agents/bugx.md",
            required_paths=(
                ".qoder/agents/bugx-worker.md",
            ),
            has_worker_subagent=True,
        ),
        interaction=InteractionContract(
            tool="Qoder 原生选择控件；否则代理按 multiple 映射 BUGX_INTERACTION_REQUEST",
            single_select="等价于 multiple=false",
            failure="控件失败时停在当前 gate",
        ),
        mcp=McpContract(config_relpaths=(".qoder/mcp.json", ".mcp.json")),
        execution=ExecutionContract(supports_headless=True, host_surface="tui"),
        aliases=("qoder-cli", "ali-qoder"),
        capabilities=frozenset({
            CAP_USER_AGENT_REGISTRATION,
            CAP_NATIVE_MCP_CONFIG,
            CAP_EXTRA_TRACKED_PATHS,
        }),
    ),
    HostComponent(
        name="trea",
        label="Trea",
        # 真实产品 CLI 可执行名是 trae（Trae 桌面版 app bundle 内
        # Contents/Resources/app/bin/trae）或 trae-cn（Trae CN 国内版）；宿主规范名仍为 trea。
        executable="trae",
        marker=".trea",
        install=InstallContract("agent", ".trea/agents/bugx.md"),
        interaction=InteractionContract(
            tool="AskUserQuestion",
            single_select="multiSelect=false",
            multi_select="tooling:batch 时使用 multiSelect=true",
        ),
        mcp=McpContract(config_relpaths=(".trea/mcp.json", ".trae/mcp.json", ".trae-cn/mcp.json", ".mcp.json")),
        execution=ExecutionContract(supports_headless=False, host_surface="desktop"),
        aliases=("trea-cli", "trae", "trae-cn", "trea-cn", "trae_cn", "trea_cn", "trae-ai"),
        # DOC-169 / DOC-210：项目级原生 MCP 写入与双变体工作区镜像。
        capabilities=frozenset({CAP_NATIVE_MCP_CONFIG, CAP_WORKSPACE_MIRROR}),
    ),
    HostComponent(
        name="funide",
        label="FunIDE",
        executable="funide",
        marker=".github/agents/bugx.agent.md",
        install=InstallContract("agent", ".github/agents/bugx.agent.md"),
        interaction=InteractionContract(
            tool="FunIDE Copilot Agent UI",
            single_select="由 FunIDE UI 或外层控制器承接；不声明专用问答工具",
            multi_select="由 FunIDE UI 或外层控制器承接",
            availability="FunIDE 是桌面 Copilot Agent Host；当前未确认独立 AskUserQuestion 工具",
            fallback="BUGX_INTERACTION_REQUEST → FunIDE/外层控制器；无承接者则 TTY 菜单或阻断",
            failure="无结构化控件且无 TTY 时阻断",
            custom_protocol="启动交互适配：FunIDE 当前仅声明桌面 Agent 入口与项目级 MCP；不要把普通文本输出当作结构化问答确认。",
        ),
        mcp=McpContract(config_relpaths=(".mcp.json",)),
        execution=ExecutionContract(supports_headless=False, host_surface="desktop"),
        capabilities=frozenset({CAP_NATIVE_MCP_CONFIG, CAP_USER_AGENT_REGISTRATION}),
    ),
)


def get_component(name: str) -> HostComponent:
    """根据规范宿主名获取对应组件描述符。

    插件承载宿主（``PLUGIN_BACKED_HOSTS``）缺少可用引擎快照时 fail-closed：
    既不能回退到 CLI 本地声明（DES-01 三份清单漂移的成因），也不能把
    「引擎未就绪」误报成「未知宿主」——两者的修复动作完全不同。
    """

    for comp in HOST_COMPONENTS:
        if comp.name == name:
            return comp
    if name in PLUGIN_BACKED_HOSTS:
        raise RuntimeError(
            f"宿主 {name!r} 的事实来自引擎插件 core/hosts/{name}.py，但当前进程找不到"
            "可用的引擎快照（BUGX_GLOBAL_ENGINE_ROOT / 仓库 core/ / ~/.bugx/core / "
            "安装载荷 share/bugx-agent/core）；请先执行 bugx init 安装或修复全局引擎，"
            "或把 BUGX_GLOBAL_ENGINE_ROOT 指向包含该插件的引擎目录"
        )
    choices = ", ".join(comp.name for comp in HOST_COMPONENTS)
    raise ValueError(f"未知 AI Agent 宿主组件: {name}；可选：{choices}")
