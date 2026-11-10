#!/usr/bin/env python3
"""单元/宿主平台判定与 auto 工具词法（DES-07 二期拆分的叶子模块）。

只依赖标准库 ``platform`` / ``re`` 与词法常量，不 import module_coverage
及其余 coverage_* 模块——它同时被「发现簇」（coverage_discovery）与
「自动回填簇」（coverage_backfill）依赖，正是拆掉 D→H 依赖环的公共下游。
函数体逐字搬自拆分前的 ``module_coverage.py``，行为不变。
"""

from __future__ import annotations

import platform
import re


AUTO_OWNER = "host-auto"
AUTO_TOOL_RULES = (
    ("_test.go", "go-test"),
    (".tsx", "web-static-read+web_conformance"),
    (".ts", "web-static-read+web_conformance"),
    (".jsx", "web-static-read+web_conformance"),
    (".js", "web-static-read+web_conformance"),
    (".go", "go-static-read+pattern_probe"),
    (".py", "python-static-read+pattern_probe"),
    (".json", "config-review"),
    (".yaml", "config-review"),
    (".yml", "config-review"),
    (".toml", "config-review"),
    (".xml", "config-review"),
    (".md", "doc-config-review"),
)


UNIT_PLATFORM_TOKENS = {
    "darwin": "darwin",
    "macos": "darwin",
    "windows": "windows",
    "win32": "windows",
    "linux": "linux",
}


def unit_platform(path_or_unit: str) -> str | None:
    """按路径词元推断单元专属平台（darwin/windows/linux）；通用单元返回 None。

    词元切分避免子串误报：util.go::windowsHelper 不会命中 windows，
    darwin.go / windows_tun.go 会分别命中 darwin/windows。
    """
    clean = path_or_unit.strip().strip("`")
    if "::" in clean:
        clean = clean.split("::", 1)[0]
    tokens = re.split(r"[^a-z0-9]+", clean.casefold())
    for token in tokens:
        platform_name = UNIT_PLATFORM_TOKENS.get(token)
        if platform_name:
            return platform_name
    return None


def host_platform() -> str:
    """当前宿主平台（windows/darwin/linux），供调度与清单标注统一取用。"""
    return platform.system().lower()


def derive_auto_tool(path: str) -> str:
    clean = path.strip().strip("`")
    if "::" in clean:
        clean = clean.split("::", 1)[0]
    lower = clean.casefold()
    for suffix, tool in AUTO_TOOL_RULES:
        if lower.endswith(suffix):
            return tool
    return "static-read"


def missing_plan_field(value: str) -> bool:
    return not value or value in {"待定", "待补", "-"}
