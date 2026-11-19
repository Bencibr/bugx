#!/usr/bin/env python3
"""provider_facts.py - 宿主通道可用性事实共享提取模块（E1 观察 #2 评估层补全）。

bugx 受控事实库（存储侧 0600 落盘）：~/.bugx/facts/cline_provider_facts.json。
通道声明未携带余额字段时，从这里读取机器本地实测可用性事实；用户充值/事实变化后
删除或改写该文件即可恢复通道优先级（显式通道声明优先于本文件）。

消费方（依赖方向单向，本模块仅依赖标准库，处于依赖图叶子，可被上下游同时导入）：
- host_provider_arbitrator（聚合层守卫：rank 分区降级留痕）；
- host_evaluators.evaluate_cline_provider（评估层守卫：is_free_model 命名短路判定
  前查事实，credits 耗尽通道跳过 TIER_1_FREE 短路，按常规梯队参与排序）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "PROVIDER_FACTS_FILE",
    "load_provider_availability_facts",
    "credits_exhausted",
]

# bugx 受控事实库路径（存储侧以 0600 权限落盘；读取侧 fail-open：缺失/损坏返回空，不阻断裁决）。
PROVIDER_FACTS_FILE = Path.home() / ".bugx" / "facts" / "cline_provider_facts.json"


def load_provider_availability_facts(facts_file: Path | None = None) -> dict[str, Any]:
    """读取通道可用性事实声明（缺失/损坏时 fail-open 返回空，不阻断裁决）。"""
    path = facts_file or PROVIDER_FACTS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    provs = raw.get("providers", {}) if isinstance(raw, Mapping) else {}
    return dict(provs) if isinstance(provs, Mapping) else {}


def credits_exhausted(
    p_name: str,
    p_info: Mapping[str, Any],
    external_facts: Mapping[str, Any] | None = None,
) -> bool:
    """判定通道是否携带 credits 耗尽 / 0 余额可用性事实。

    优先级：通道条目显式声明（facts/条目/settings 下的 credits_exhausted 布尔值
    或 balance/credits 数值）> bugx 事实库声明 > 无事实（返回 False，行为不变）。
    """
    entry = p_info if isinstance(p_info, Mapping) else {}
    settings = entry.get("settings") if isinstance(entry.get("settings"), Mapping) else {}
    facts = entry.get("facts") if isinstance(entry.get("facts"), Mapping) else {}
    for scope in (facts, entry, settings):
        flag = scope.get("credits_exhausted")
        if isinstance(flag, bool):
            return flag
        for key in ("balance", "credits", "credits_remaining", "creditsRemaining"):
            val = scope.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return val <= 0
    prov_fact = (external_facts or {}).get(p_name)
    if isinstance(prov_fact, Mapping):
        return prov_fact.get("credits_exhausted") is True
    return False
