#!/usr/bin/env python3
"""core/hosts/__init__.py - 宿主插件注册表（发现即注册，导入期 fail-closed）。

**新增宿主的完整动作**（DOC-239）：在 ``core/hosts/`` 下加一个模块导出 ``PLUGIN``，
再从 ``TODO_HOSTS`` 删掉该宿主名。引擎/CLI/探针任何一个调用点都不用改。

失败姿态（不静默跳过任何模块）：缺 ``PLUGIN``、重复宿主名、重复 ``order``、
``ready`` 缺面、``todo`` 带面或没给原因 —— 一律 import 期 ``RuntimeError``。
理由与 ``core/host_acp`` 相同：拿「声明支持」的半成品去跑真实宿主，得到的是
最贵也最难归因的假阴性（历史两次事故即此类）。

依赖约束：本包只依赖标准库（可被未安装 CLI 的引擎快照直接 import），
**禁止** import ``host_registry`` / ``supervisor_host`` / ``bugx_cli.*``；
依赖方向只能是它们读本包。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import (
    FACET_NAMES,
    STATUS_READY,
    STATUS_TODO,
    AcpFacet,
    EngineRunFacet,
    HostPlugin,
    HostPluginError,
    IdentityFacet,
    InstallFacet,
    McpWriteFacet,
    RunContext,
    WorkerRuntimeFacet,
    validate_plugin,
)

__all__ = [
    "FACET_NAMES",
    "PLUGINS",
    "REGISTRY_VERSION",
    "STATUS_READY",
    "STATUS_TODO",
    "TODO_HOSTS",
    "AcpFacet",
    "EngineRunFacet",
    "HostPlugin",
    "HostPluginError",
    "IdentityFacet",
    "InstallFacet",
    "McpWriteFacet",
    "RunContext",
    "WorkerRuntimeFacet",
    "all",
    "get",
    "ready_hosts",
    "todo_hosts",
]

#: 注册表 schema 版本：``HostPlugin`` 字段/面语义变化时递增。
REGISTRY_VERSION = 2

#: 显式登记为「尚未迁移到插件模型」的受支持宿主（DOC-239 逐宿主切片）：
#: 它们仍能被**识别**（走既有实现），但不得被当成插件 ``ready`` 消费。
#: 账目一致性由 ``tests/test_host_plugins.py`` 双向守护。
TODO_HOSTS: tuple[str, ...] = (
    "chatgpt",
    "workbuddy",
    "qoder",
    "trea",
    "funide",
)


def _register(registry: dict[str, HostPlugin], plugin: object, source: str) -> HostPlugin:
    if not isinstance(plugin, HostPlugin):
        raise RuntimeError(f"hosts 插件 PLUGIN 不是 HostPlugin: {source}")
    try:
        validate_plugin(plugin)
    except HostPluginError as exc:
        raise RuntimeError(f"hosts 插件契约违约（{source}）: {exc}") from exc
    name = plugin.name
    if name in registry:
        raise RuntimeError(f"hosts 插件重复注册宿主: {name}")
    if name in TODO_HOSTS:
        raise RuntimeError(f"宿主同时登记为已迁移与待迁移: {name}")
    registry[name] = plugin
    return plugin


def _discover() -> dict[str, HostPlugin]:
    registry: dict[str, HostPlugin] = {}
    seen_orders: dict[int, str] = {}
    package_dir = Path(__file__).resolve().parent
    for module_info in pkgutil.iter_modules([str(package_dir)]):
        name = module_info.name
        if name.startswith("_") or name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            raise RuntimeError(f"hosts 插件模块缺少 PLUGIN 导出: {name}")
        _register(registry, plugin, source=name)
        if plugin.identity.name != name:
            raise RuntimeError(f"hosts 插件名与模块名不一致: {plugin.identity.name} != {name}")
        order = plugin.identity.order
        if order in seen_orders:
            raise RuntimeError(f"hosts 插件 order 冲突: {seen_orders[order]} / {plugin.identity.name}")
        seen_orders[order] = plugin.identity.name
    return registry


PLUGINS: dict[str, HostPlugin] = _discover()


def all() -> tuple[HostPlugin, ...]:
    """Return every migrated plugin in declared ``order`` (deterministic)."""
    return tuple(sorted(PLUGINS.values(), key=lambda plugin: plugin.identity.order))


def get(name: object) -> HostPlugin | None:
    """Return the migrated plugin for ``name``; ``None`` means «not migrated yet»."""
    if not isinstance(name, str):
        return None
    return PLUGINS.get(name.strip().casefold())


def ready_hosts() -> tuple[str, ...]:
    """Return hosts whose six facets are declared, in ``order``."""
    return tuple(plugin.name for plugin in all() if plugin.ready)


def todo_hosts() -> tuple[str, ...]:
    """Return explicitly-registered unmigrated hosts (stable declaration order)."""
    return tuple(TODO_HOSTS)
