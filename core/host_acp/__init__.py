#!/usr/bin/env python3
"""core/host_acp/__init__.py - 宿主 ACP 插件注册表（发现即注册）。

**新增宿主的完整动作**：在 ``core/host_acp/`` 下加一个模块（导出 ``PLUGIN``），
再从下面的 ``TODO_HOSTS`` 删掉该宿主名。探针代码一行都不用改——这就是
``tests/test_host_acp_plugins.py`` 里「探针源码不得出现宿名字面量」守卫要保护的性质。

失败姿态：注册期不静默跳过任何模块。缺 ``PLUGIN``、重复名字、``ready`` 却没有实现
``acp_argv``，一律在 import 期 ``RuntimeError``——用声明支持的空实现去跑真实宿主，
得到的会是「宿主静默」这种最贵、最难归因的假阴性。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import AcpPlugin, McpServersBuilder

__all__ = [
    "AcpPlugin",
    "McpServersBuilder",
    "PLUGINS",
    "REGISTRY_VERSION",
    "TODO_HOSTS",
    "get",
    "ready_hosts",
    "todo_hosts",
    "unavailable_hosts",
]

# 注册表 schema 版本：插件字段（status/acp_argv/launch_policy/prepare_isolation）变化时递增。
REGISTRY_VERSION = 1

# 显式登记为「本仓尚未适配 ACP」的受支持宿主。它们是合法一等状态（探针据此直接
# BLOCKED + adapter_status=todo），不是特权默认值。账目一致性由
# tests/test_host_acp_plugins.py 双向守护：ready ∪ todo ∪ unavailable 必须等于
# ``host_registry.SUPPORTED_HOSTS``，且三者两两不相交。
# 反例（务必别再把它们混进来）：``codex`` 是 ``unavailable``（上游无 ACP 面，
# 见 ``core/host_acp/codex.py``），不是本仓拖欠的 TODO。
TODO_HOSTS: tuple[str, ...] = (
    "chatgpt",
    "workbuddy",
    "qoder",
    "trea",
    "funide",
)


def _register(
    registry: dict[str, AcpPlugin],
    plugin_cls: object,
    source: str = "<inline>",
) -> AcpPlugin:
    """Validate one plugin class and add it to ``registry`` (fail-closed)."""
    if not (isinstance(plugin_cls, type) and issubclass(plugin_cls, AcpPlugin)):
        raise RuntimeError(f"host_acp 插件 PLUGIN 不是 AcpPlugin 子类: {source}")
    plugin = plugin_cls()
    host = plugin.name.strip().casefold()
    if not host:
        raise RuntimeError(f"host_acp 插件未声明宿主名: {source}")
    if host in registry:
        raise RuntimeError(f"host_acp 插件重复注册宿主: {host}")
    if plugin.status not in {"ready", "todo", "unavailable"}:
        raise RuntimeError(f"host_acp 插件状态非法: {host}={plugin.status!r}")
    if plugin.status == "ready" and type(plugin).acp_argv is AcpPlugin.acp_argv:
        raise RuntimeError(f"host_acp 插件声明 ready 但未实现 acp_argv: {host}")
    if plugin.status != "ready" and not plugin.reason.strip():
        raise RuntimeError(f"host_acp 插件声明 {plugin.status} 但未说明原因: {host}")
    if host in TODO_HOSTS:
        raise RuntimeError(f"宿主同时登记为已适配与待适配: {host}")
    registry[host] = plugin
    return plugin


def _discover() -> dict[str, AcpPlugin]:
    """Import every plugin module in this package and validate it."""
    registry: dict[str, AcpPlugin] = {}
    package_dir = Path(__file__).resolve().parent
    for module_info in pkgutil.iter_modules([str(package_dir)]):
        name = module_info.name
        if name.startswith("_") or name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        plugin_cls = getattr(module, "PLUGIN", None)
        if plugin_cls is None:
            raise RuntimeError(f"host_acp 插件模块缺少 PLUGIN 导出: {name}")
        _register(registry, plugin_cls, source=name)
    return registry


PLUGINS: dict[str, AcpPlugin] = _discover()


def get(name: object) -> AcpPlugin | None:
    """Return the plugin for ``name``; ``None`` means «not adapted yet»."""
    if not isinstance(name, str):
        return None
    return PLUGINS.get(name.strip().casefold())


def ready_hosts() -> tuple[str, ...]:
    """Return adapted host names in stable (registry) order."""
    return tuple(name for name, plugin in PLUGINS.items() if plugin.supports_acp())


def todo_hosts() -> tuple[str, ...]:
    """Return explicitly-registered unadapted hosts in stable order."""
    return tuple(name for name in TODO_HOSTS)


def unavailable_hosts() -> tuple[str, ...]:
    """Return hosts whose CLI has no ACP entry point at all (product fact).

    与 :func:`todo_hosts` 的区别是责任归属：``todo`` 是本仓欠的适配，``unavailable``
    是上游没有该协议面。两者在探针报告里必须是不同的 ``adapter_status``。
    """
    return tuple(
        name for name, plugin in PLUGINS.items() if plugin.status == "unavailable"
    )
