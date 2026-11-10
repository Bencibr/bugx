"""BugX AI Agent 宿主组件框架。

定义正交的宿主策略契约（InteractionContract, InstallContract, McpContract, ExecutionContract）
以及 HostComponent 统一规格描述符。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class InteractionContract:
    """人机交互控件协议声明。"""

    tool: str
    single_select: str = "multiple=false"
    multi_select: str = "question_id=tooling:batch 时按 multiple=true 使用多选"
    failure: str = "重问当前 gate；不得降级为普通文本"
    availability: str = ""
    fallback: str = ""
    ui: str = ""
    custom_protocol: str = ""

    def to_dict(self) -> dict[str, str]:
        res = {
            "tool": self.tool,
            "single_select": self.single_select,
            "multi_select": self.multi_select,
            "failure": self.failure,
        }
        if self.availability:
            res["availability"] = self.availability
        if self.fallback:
            res["fallback"] = self.fallback
        if self.ui:
            res["ui"] = self.ui
        return res


@dataclass(frozen=True)
class InstallContract:
    """静态资源与入口安装协议。"""

    install_kind: str  # "agent" | "skill" | "plugin" | "subagent" | "patch+skill"
    install_path: str
    entry_mode: str = "rendered"
    required_paths: tuple[str, ...] = ()
    has_worker_subagent: bool = False
    #: DOC-169：历史版本写入过、现已被 install_path 取代的遗留路径。安装与
    #: 无登记卸载清扫都会按当前模板逐字比对清理这些路径（用户改动保留）。
    legacy_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpContract:
    """MCP 配置协议声明。"""

    config_relpaths: tuple[str, ...] = ()
    user_config_dirs: tuple[str, ...] = ()
    cli_arg_injectable: bool = False


@dataclass(frozen=True)
class ExecutionContract:
    """无头启动与运行时协议。"""

    supports_headless: bool = False
    host_surface: str = "tui"  # "tui" | "desktop" | "web" | "unknown"
    headless_cmd_builder: Callable[[Path, str, str, str], list[str]] | None = None
    autonomous_cmd_builder: Callable[[Path, str, str, str], list[str]] | None = None


@dataclass
class HostComponent:
    """一个完整的 AI Agent 宿主兼容组件描述符。"""

    name: str
    label: str
    executable: str
    marker: str
    install: InstallContract
    interaction: InteractionContract
    mcp: McpContract = field(default_factory=McpContract)
    execution: ExecutionContract = field(default_factory=ExecutionContract)
    capabilities: frozenset[str] = field(default_factory=frozenset)
    aliases: tuple[str, ...] = ()
    tools_line: str = ""
    custom_title: str = ""
    custom_preamble: str = ""
    app_markers_fn: Callable[[], tuple[Path, ...]] | None = None
    running_markers_fn: Callable[[], tuple[Path, ...]] | None = None
    custom_detector: Callable[[Path, "HostComponent"], dict[str, Any]] | None = None
    custom_renderer: Callable[["HostComponent", Path, str | None], str] | None = None

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities
