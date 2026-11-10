#!/usr/bin/env python3
"""core/host_runtime/__init__.py - 宿主 CLI 进程执行器实现体（DOC-239）。

一个宿主一个模块（``core/host_runtime/<host>.py``），导出
``execute_worker(...)``；**模块名只出现在 ``core/hosts/<host>.py`` 的
``worker_runtime.runner_module``**，``worker_runtime`` 按名字 import，不认识任何
宿主名。这与 ``core/host_acp`` 同一形状：声明在插件，实现体按名寻址。

依赖方向单向：本包可以 import 引擎模块，引擎只能按声明的模块名 import 本包。
"""

from __future__ import annotations

from . import host_probe, host_provider_arbitrator, host_registry, host_usage

__all__: list[str] = [
    "host_probe",
    "host_provider_arbitrator",
    "host_registry",
    "host_usage",
]
