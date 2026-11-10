#!/usr/bin/env python3
"""core/host_runtime/adapters.py - 宿主适配器与宿主探测实现。"""

from __future__ import annotations
from contextlib import AbstractContextManager as ContextManager

import logging
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from functools import partial
from typing import Any

from core.host_runtime import host_registry
import core.hosts as host_plugins

from core.host_runtime.host_probe import probe_active_host as _probe_active_host

from core.host_runtime.mcp_registry_gate import MCP_CATALOG


def _get_sh_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get('core.supervisor.supervisor_host')
    if mod and hasattr(mod, name):
        return getattr(mod, name)
    return default


logger = logging.getLogger(__name__)

BUGX_SUPERVISOR_MCP = "bugx-supervisor"
HOST_NAMES = host_registry.HOST_NAMES

@dataclass
class AgentRun:
    exit_code: int
    session_id: str = ""
    truncated: bool = False
    timed_out: bool = False
    error: str = ""
    output: str = ""


class HostAdapter:
    """宿主差异的抽象接口。"""

    name = "unknown"
    executable = ""
    host_surface = "tui"

    def is_callable(self, root: Path) -> bool:
        """Return whether this host has a callable local entry point.

        ``desktop`` 宿主（zcode / trea / workbuddy / funide …）没有稳定 PATH
        命令，「可调用」只能由进程祖先链探查证明；``tui``/``web`` 宿主按可执行
        文件判定。

        历史缺陷：DOC-239 把 zcode 迁进插件后，桌面宿主的探查覆写随本地类一起删
        掉，``PluginAdapter`` 退回基类的 ``shutil.which`` 判定——真实 ZCode 会话里
        ``zcode`` 通常不在 PATH，``_host_capability`` 因此把已识别的宿主判成
        ``blocked``。判定收敛回基类，所有桌面宿主统一受益。
        """
        import shutil

        if self.host_surface == "desktop":
            from core.host_runtime.host_probe import probe_active_host

            if probe_active_host() == self.name:
                return True
        return bool(self.executable and shutil.which(self.executable))

    def headless_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        """返回启动无头 agent 的 argv（不含 cwd/env，由 RealHooks 定制）。"""
        raise NotImplementedError

    def agent_config_path(self, root: Path) -> Path:
        raise NotImplementedError

    def mcp_config_paths(self, root: Path) -> tuple[Path, ...]:
        """Return config files that can register MCP servers for this host."""
        return (self.agent_config_path(root),)

    def required_mcp(self, root: Path) -> list[str]:
        return list(MCP_CATALOG)

    def mcp_health(self, name: str, cfg: dict, root: Path | None = None) -> bool:
        return True

    def supports_headless(self) -> bool:
        return False

    def autonomous_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        """Return a no-prompt command for the explicit autonomous launcher."""
        return self.headless_run(root, agent, session, prompt)

    def autonomous_environment(self, root: Path) -> ContextManager[Mapping[str, str | None]]:
        """Return temporary child-environment overrides for one autonomous call."""
        del root
        return nullcontext({})


class PluginAdapter(HostAdapter):
    """由 ``core/hosts`` 插件驱动的适配器（DOC-239）：宿主差异只存在于插件。

    本类**不含任何宿主事实**：``headless_run`` / ``autonomous_run`` /
    ``mcp_config_paths`` / ``supports_headless`` 全部转调 ``engine_run`` 面，
    ``agent_config_path`` 取 ``identity.markers[0]``。新增宿主只需加一个插件模块，
    不再需要在 ``_ADAPTERS`` 里写一个宿主类（DES-01 的成因就是这类重复声明）。
    """

    def __init__(self, plugin: host_plugins.HostPlugin) -> None:
        if not plugin.ready:
            raise RuntimeError(f"插件未就绪，不能承载适配器: {plugin.name}")
        self.plugin = plugin
        self.name = plugin.identity.name
        self.executable = plugin.identity.executable
        self.host_surface = plugin.engine_run.host_surface

    def _context(self, root: Path, agent: str, session: str, prompt: str) -> host_plugins.RunContext:
        return host_plugins.RunContext(root=root, agent=agent, session=session, prompt=prompt)

    def _engine_run_node_flags(self) -> tuple[str, ...]:
        """主执行路径需要的真 node flag（数据来自插件 facet，执行为引擎职责）。"""
        return tuple(getattr(self.plugin.engine_run, "engine_run_node_flags", ()) or ())

    def headless_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        argv = self.plugin.engine_run.headless_argv(self._context(root, agent, session, prompt))
        argv = [str(part) for part in argv]
        flags = self._engine_run_node_flags()
        # DSH Desktop 的 ``.desktop-bin/node`` 是 Electron shim，会吞掉
        # ``--expose-internals``；headless profile 的 HMR 硬要求该 flag。不重写则
        # 主挖掘必崩，而侦察路径（recon_scout_node_flags）却是好的——半修状态比
        # 完全不修更难排查，所以两条路径必须使用同一套 node 解析。
        return host_registry._with_real_node(argv, flags) if flags else argv

    def autonomous_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        argv = self.plugin.engine_run.autonomous_argv(self._context(root, agent, session, prompt))
        argv = [str(part) for part in argv]
        flags = self._engine_run_node_flags()
        return host_registry._with_real_node(argv, flags) if flags else argv

    def autonomous_environment(self, root: Path) -> ContextManager[Mapping[str, str | None]]:
        prepare = self.plugin.engine_run.autonomous_environment
        return nullcontext({}) if prepare is None else prepare(root)

    def agent_config_path(self, root: Path) -> Path:
        facet = self.plugin.engine_run.agent_config_path
        if facet is not None:
            return facet(root)
        return root / self.plugin.identity.markers[0]

    def mcp_config_paths(self, root: Path) -> tuple[Path, ...]:
        return tuple(self.plugin.engine_run.mcp_config_paths(root))

    def mcp_health(self, name: str, cfg: dict, root: Path | None = None) -> bool:
        health = self.plugin.engine_run.mcp_health
        return True if health is None else bool(health(name, cfg, root))

    def supports_headless(self) -> bool:
        return bool(self.plugin.engine_run.supports_headless)


class WorkBuddyAdapter(HostAdapter):
    """WorkBuddy AI desktop adapter (CodeBuddy is its code-mode surface)."""

    name = "workbuddy"
    executable = "workbuddy"
    host_surface = "desktop"

    def headless_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        raise NotImplementedError("WorkBuddy 没有受本适配器确认的无头调用契约")

    def agent_config_path(self, root: Path) -> Path:
        return root / ".workbuddy"

    def mcp_config_paths(self, root: Path) -> tuple[Path, ...]:
        return (root / ".workbuddy" / "mcp.json", root / ".codebuddy" / "mcp.json")

    def required_mcp(self, root: Path) -> list[str]:
        return list(MCP_CATALOG)

    def mcp_health(self, name: str, cfg: dict, root: Path | None = None) -> bool:
        return super().mcp_health(name, cfg, root)

    def supports_headless(self) -> bool:
        return False


class ChatGptAdapter(HostAdapter):
    """ChatGPT plugin/marketplace adapter.

    ChatGPT consumes BugX through a marketplace plugin manifest rather than a
    local MCP config file, so there is no MCP document to read.
    """

    name = "chatgpt"
    executable = "chatgpt"
    host_surface = "web"

    def headless_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        raise NotImplementedError("ChatGPT 通过插件市场接入，没有本地无头调用契约")

    def agent_config_path(self, root: Path) -> Path:
        return root / ".agents" / "plugins" / "marketplace.json"

    def mcp_config_paths(self, root: Path) -> tuple[Path, ...]:
        return (root / ".agents" / "mcp.json",)

    def required_mcp(self, root: Path) -> list[str]:
        return list(MCP_CATALOG)

    def mcp_health(self, name: str, cfg: dict, root: Path | None = None) -> bool:
        return super().mcp_health(name, cfg, root)

    def is_callable(self, root: Path) -> bool:
        # PE-20261004-004：env 声明废除，改主动探查。
        return _probe_active_host() == self.name

    def supports_headless(self) -> bool:
        return False


class UnknownAdapter(HostAdapter):
    """Fail-closed adapter used when the current host cannot be identified."""

    name = "unknown"
    host_surface = "unknown"

    def headless_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        raise NotImplementedError("未知宿主不能推断无头调用命令")

    def agent_config_path(self, root: Path) -> Path:
        return root / ".bugx" / "unknown-host-mcp.json"

    def required_mcp(self, root: Path) -> list[str]:
        return list(MCP_CATALOG)

    def mcp_health(self, name: str, cfg: dict, root: Path | None = None) -> bool:
        return False


class QoderAdapter(HostAdapter):
    """Qoder CLI adapter."""

    name = "qoder"
    executable = "qoder"

    def headless_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        return ["qoder", "run", "--prompt", prompt]

    def agent_config_path(self, root: Path) -> Path:
        return root / ".qoder"

    def mcp_config_paths(self, root: Path) -> tuple[Path, ...]:
        return (root / ".qoder" / "mcp.json",)

    def required_mcp(self, root: Path) -> list[str]:
        return list(MCP_CATALOG)

    def mcp_health(self, name: str, cfg: dict, root: Path | None = None) -> bool:
        return super().mcp_health(name, cfg, root)

    def supports_headless(self) -> bool:
        return True


class TreaAdapter(HostAdapter):
    """Trea desktop adapter."""

    name = "trea"
    executable = "trae"
    host_surface = "desktop"

    def headless_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        raise NotImplementedError("Trea 暂无标准无头命令行调用契约")

    def agent_config_path(self, root: Path) -> Path:
        return root / ".trea"

    def mcp_config_paths(self, root: Path) -> tuple[Path, ...]:
        return (root / ".trea" / "mcp.json",)

    def required_mcp(self, root: Path) -> list[str]:
        return list(MCP_CATALOG)

    def mcp_health(self, name: str, cfg: dict, root: Path | None = None) -> bool:
        return super().mcp_health(name, cfg, root)

    def supports_headless(self) -> bool:
        return False


class FunIdeAdapter(HostAdapter):
    """FunIDE desktop Copilot Agent Host adapter."""

    name = "funide"
    executable = "funide"
    host_surface = "desktop"

    def headless_run(self, root: Path, agent: str, session: str, prompt: str) -> list[str]:
        raise NotImplementedError("FunIDE 暂无标准无头命令行调用契约")

    def agent_config_path(self, root: Path) -> Path:
        return root / ".github" / "agents"

    def mcp_config_paths(self, root: Path) -> tuple[Path, ...]:
        return (root / ".mcp.json",)

    def required_mcp(self, root: Path) -> list[str]:
        return list(MCP_CATALOG)

    def mcp_health(self, name: str, cfg: dict, root: Path | None = None) -> bool:
        return super().mcp_health(name, cfg, root)

    def is_callable(self, root: Path) -> bool:
        # FunIDE has no stable PATH command.  The active-host probe provides
        # the desktop process evidence; a PATH entry remains a valid fallback.
        return _probe_active_host() == self.name or super().is_callable(root)

    def supports_headless(self) -> bool:
        return False


_ADAPTERS = {
    "chatgpt": ChatGptAdapter,
    "workbuddy": WorkBuddyAdapter,
    "qoder": QoderAdapter,
    "trea": TreaAdapter,
    "funide": FunIdeAdapter,
}

# DOC-239：插件 ``ready`` 的宿主一律由 ``PluginAdapter`` 承载——插件是唯一事实来源，
# 上面表里的同名本地类必须已经删除（重复声明正是 DES-01 的成因）。尚未迁移的宿主
# 继续用本地类，每完成一个纵切就删掉一个。
_ADAPTERS.update(
    {
        name: partial(PluginAdapter, plugin)
        for plugin in host_plugins.all()
        if plugin.ready
        for name in (plugin.name, *plugin.identity.aliases)
    }
)


def host_adapter(name: str) -> HostAdapter:
    """按宿主名返回新的适配器实例；未知宿主抛 ``KeyError``（不静默退化）。"""

    factory = _ADAPTERS.get(name)
    if factory is None:
        raise KeyError(f"未知宿主: {name}")
    return factory()

# 每个受支持宿主都必须有适配器；缺失即为注册表漂移，必须在导入期暴露而不是
# 在 detect_host 时静默退化成 UnknownAdapter（那会让真实宿主的 Supervisor MCP
# 被误判为硬缺失）。
_MISSING_ADAPTERS = [name for name in host_registry.SUPPORTED_HOSTS if name not in _ADAPTERS]
if _MISSING_ADAPTERS:  # pragma: no cover - 由 tests/test_supervisor_host.py::HostHeadlessCapabilityTestCase 守护
    raise RuntimeError(f"宿主注册表与适配器不一致，缺少: {', '.join(_MISSING_ADAPTERS)}")


def _host_marker(root: Path, name: str) -> bool:
    """Return whether the project carries a marker for ``name``.

    Marker table is owned by ``host_registry`` so a newly supported host cannot
    be silently absent here (previously ``dsh`` / ``chatgpt`` / ``workbuddy``
    had no branch and could never be detected).
    """
    return host_registry.host_marker(root, name)


def detect_host(root: Path) -> HostAdapter:
    """按主动探查、项目标记和唯一可执行文件选择宿主；探查失败即 broken。

    PE-20261004-004（用户指令）：进程祖先链探查是宿主身份的唯一权威
    证据；**禁止 BUGX_HOST 环境变量覆盖或兜底**——陈旧全局 env 在多宿主
    并存/换宿主场景必然错绑宿主（workbuddy 残留 env 曾把 ZCode 会话错绑
    成 workbuddy）。探查失败返回 UnknownAdapter（broken），由引擎把
    "宿主无法识别" 显式上报宿主 agent 处理，绝不猜测。

    A missing or ambiguous host is a first-class ``unknown`` result.  In
    particular, this function never turns an unidentified process into
    OpenCode merely because that executable happens to be installed.
    """
    probed = _get_sh_attr("_probe_active_host", _probe_active_host)()
    if probed:
        adapter_type = _ADAPTERS.get(probed) or _ADAPTERS.get(
            host_registry.normalize_host_name(probed) or ""
        )
        if adapter_type:
            return adapter_type()

    configured = host_registry.configured_hosts(root)
    available = [name for name in HOST_NAMES[:-1]
                 if _ADAPTERS[name]().is_callable(root)]
    # A project marker is only authoritative when it identifies one host and
    # that host is callable.  If the executable is absent, still return the
    # adapter so its result can say configured-but-not-callable.
    if len(configured) == 1:
        name = configured[0]
        return _ADAPTERS[name]()
    if len(configured) > 1:
        # A desktop host such as ZCode may have no executable while another
        # installed CLI is callable.  Selecting that CLI here misbinds MCP
        # to the wrong host.  探查失败 + 多标记 = 无法安全推断，broken。
        return UnknownAdapter()
    if len(available) == 1:
        return _ADAPTERS[available[0]]()
    return UnknownAdapter()


