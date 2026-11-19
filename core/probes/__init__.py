#!/usr/bin/env python3
"""core/probes - 形态专属黑盒探针系统 (DOC-248 Dynamic Probes)。

统一导出所有领域专属探针基类与实现类。

W-004 惰性化（耗时账本）：本包历史上 eager import 全部 19 个探针模块
（含 playwright 链的 web_probe/differential_probe 等重型依赖），被引擎
门禁链（launch_phases.common → governance → supervisor → coverage）
连带 import，每次引擎启动固定支付 ~190ms。改为 PEP 562 模块级
__getattr__ 按需加载：BaseProbe（轻量基类）保持 eager（probe_registry
注册面依赖），其余探针在首次属性访问时才 import——消费方三种形态
（from core.probes import X / from core.probes.X import Y / 子模块直
import）语义全部不变。
"""

from __future__ import annotations

from typing import Any

from .base import BaseProbe

__all__ = [
    "BaseProbe",
    "WebDynamicProbe",
    "AndroidProbe",
    "MiniProgramProbe",
    "sast_probe",
    "sast_rules",
    "sca_scanner",
    "desktop_probe",
    "mobile_probe",
    "native_probe",
    "platform_probe",
    "memory_probe",
    "efficiency_probe",
    "differential_probe",
    "pattern_probe",
    "ios_binary_probe",
    "codebase_memory_probe",
    "openwiki_probe",
    "probe_registry",
]

# 惰性成员 → 所属子模块（属性名与历史 eager 导出逐一对应，语义不变）
_LAZY_MEMBERS: dict[str, str] = {
    "WebDynamicProbe": "web_probe",
    "AndroidProbe": "android_probe",
    "MiniProgramProbe": "miniprogram_probe",
    "sast_probe": "sast_probe",
    "sast_rules": "sast_rules",
    "sca_scanner": "sca_scanner",
    "desktop_probe": "desktop_probe",
    "mobile_probe": "mobile_probe",
    "native_probe": "native_probe",
    "platform_probe": "platform_probe",
    "memory_probe": "memory_probe",
    "efficiency_probe": "efficiency_probe",
    "differential_probe": "differential_probe",
    "pattern_probe": "pattern_probe",
    "ios_binary_probe": "ios_binary_probe",
    "codebase_memory_probe": "codebase_memory_probe",
    "openwiki_probe": "openwiki_probe",
    "probe_registry": "probe_registry",
}


def __getattr__(name: str) -> Any:
    """PEP 562 按需加载探针成员（W-004：消除门禁链 190ms eager 税）。"""
    target = _LAZY_MEMBERS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{target}")
    globals()[name] = module  # 缓存：后续访问零开销
    return module
