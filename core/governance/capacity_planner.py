#!/usr/bin/env python3
"""并发容量规划：多源约束取最小值（never exceed the weakest link）。

**为什么需要它**：并发上限是**多个独立约束**的交集，任何单源决定都会越界——

* 系统资源（逻辑核数 / 配置 / 环境变量）：机器只扛得住 2 路时，接口给 10 也没用；
* 宿主容量（``HostPlugin.worker_runtime.max_concurrency`` / ``concurrency_bounds``）：
  宿主只支持 5 路时，机器给 10 也只能跑 5；
* provider 配额：接口只给 10 路时，前两者再高也不该超过它。

因此有效并发 = ``min(所有已知约束)``。**未知约束不参与**（不能凭空猜，
也不能把「没声明」当成「无限」）；所有非 None 约束都必须被 min 覆盖。

**它不是什么**：不做「按剩余预算调低」（那只会更慢），也不改派发顺序
（顺序由 LPT 负责，见 CORE-308）。
"""

from __future__ import annotations

import os

from core.supervisor.supervisor_models import resolve_system_concurrency

#: provider 并发上限的显式入口。provider 侧没有统一可探测的配额事实，
#: 因此只接受显式声明（环境变量 / 配置），声明缺失时不参与 min。
PROVIDER_CONCURRENCY_ENV = "BUGX_PROVIDER_MAX_CONCURRENCY"


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _provider_capacity(provider: str | None) -> tuple[int | None, str]:
    """provider 并发上限（只有显式声明才算数）。"""

    raw = (os.environ.get(PROVIDER_CONCURRENCY_ENV) or "").strip()
    declared = _positive_int(raw)
    if declared is None:
        return None, "undeclared"
    label = (provider or "default").strip() or "default"
    return declared, f"env:{PROVIDER_CONCURRENCY_ENV}({label})"


def _host_capacity(host: str | None) -> tuple[int | None, str]:
    """宿主侧并发容量：优先 ``max_concurrency``，其次 ``concurrency_bounds`` 上限。

    读插件事实（``core/hosts/<host>.py``），与 `HostPlugin` 契约同源；
    未声明（None）返回 ``(None, "undeclared")``——**不猜测**宿主能力。
    """

    name = (host or "").strip()
    if not name:
        return None, "no-host"
    try:
        from core.hosts import PLUGINS  # 插件导入期已完成发现与 fail-closed 校验
    except Exception:  # noqa: BLE001 - 插件加载失败不应让派发崩掉
        PLUGINS = {}  # type: ignore[assignment]
    plugin = PLUGINS.get(name.casefold()) if isinstance(PLUGINS, dict) else None
    runtime = getattr(plugin, "worker_runtime", None) if plugin is not None else None
    if runtime is None:
        return None, f"host:{name}(not-plugin-driven)"
    declared = _positive_int(getattr(runtime, "max_concurrency", None))
    if declared is not None:
        return declared, f"host:{name}.max_concurrency"
    bounds = getattr(runtime, "concurrency_bounds", None)
    if isinstance(bounds, tuple) and len(bounds) == 2:
        upper = _positive_int(bounds[1])
        if upper is not None:
            return upper, f"host:{name}.concurrency_bounds.max"
    return None, f"host:{name}(undeclared)"


def effective_worker_concurrency(
    *, host: str | None = None, provider: str | None = None,
    requested: int | None = None, pending: int | None = None,
) -> dict[str, object]:
    """解析有效并发度。返回机读回执，便于运维解释「为什么是这个数」。

    返回字段：
      ``value``       最终并发（≥1，且不超过 pending）；
      ``constraints`` 每个来源的取值（None = 该源未声明，不参与 min）；
      ``binding``     真正生效的那个来源（最弱环节）；
      ``sources``     人类可读的来源标签列表。
    """

    system_value, system_ceiling = resolve_system_concurrency()
    host_value, host_label = _host_capacity(host)
    provider_value, provider_label = _provider_capacity(provider)
    requested_value = _positive_int(requested)

    constraints: dict[str, int | None] = {
        "system": _positive_int(system_value),
        "host": host_value,
        "provider": provider_value,
        "requested": requested_value,
        "pending": _positive_int(pending),
    }
    labels = {
        "system": f"system:cpu/config (ceiling={system_ceiling})",
        "host": host_label,
        "provider": provider_label,
        "requested": "requested:workers",
        "pending": "pending:jobs",
    }
    known = {key: value for key, value in constraints.items() if value is not None}
    if not known:
        value = 1
        binding = "none"
    else:
        binding = min(known, key=lambda key: known[key])
        value = known[binding]
    return {
        "value": max(1, int(value)),
        "binding": binding,
        "constraints": constraints,
        "sources": labels,
        "ceiling": int(system_ceiling),
    }
