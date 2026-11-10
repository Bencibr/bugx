#!/usr/bin/env python3
"""特征面提取与跨模块业务链路发现（DES-07 二期拆分的叶子模块）。

``extract_feature_surface`` / ``business_discovery`` /
``business_discovery_rows`` / ``coverage_summary``，以及轮内共享上下文
``PassContext``（证据探测缓存 + 全仓枚举缓存）。端点/命令模式常量
（``WEB_ENDPOINT_PATTERNS`` / ``CLI_COMMAND_PATTERNS``）随簇下沉。
依赖 ``coverage_discovery``（枚举结果复用）；``test_strategy`` /
``attack_graph_store`` 仍按需延迟导入。函数体逐字搬自拆分前源码。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.runtime import project_graph
from core.retrieval import symbol_index

from core.coverage.coverage_discovery import (
    CLI_COMMAND_PATTERNS,
    PassContext,
    WEB_ENDPOINT_PATTERNS,
    announce_feature_surface,
    graph_scope,
    business_discovery,
    business_discovery_rows,
    coverage_summary,
    discover_audit_unit_paths,
    discovery_units,
    extract_feature_surface,
)
from core.coverage.coverage_manifest import ModuleRow


