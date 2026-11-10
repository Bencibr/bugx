#!/usr/bin/env python3
"""module_coverage.py — 最小审计单元与跨模块业务链路覆盖门禁。

这是「全覆盖宪法」的外部检查器，不相信 agent 口头报告：
  discover    递归发现每个源码文件，生成最小单元清单骨架
  check       校验最小单元、业务链路、阶段顺序、工具与证据
  final-check 要求内部最小单元和跨模块业务链路全部「已覆盖」

机械可验证的最小下限是源码文件；一个文件含多个可独立验证的函数、类、路由、
命令或状态机时，用 ``path/to/file.py::symbol`` 继续拆分。必须先把全部「内部」
最小单元标为已覆盖，才能开始「业务」阶段的模块间数据流/状态流审计。

DES-07 拆分说明：实现按簇下沉为同包叶子模块，本文件只做再导出，行为与
调用面不变——
  coverage_platform   单元/宿主平台判定与 auto 工具词法（叶子）
  coverage_discovery  递归发现（``discover_minimum_unit_paths``，由
                      ``symbol_index.audit_units`` 驱动）
  coverage_business   特征面提取与业务链路发现（含轮内共享上下文）
  coverage_validate   行级校验：内部最小单元未全部覆盖前不放行，业务阶段
                      的模块间业务逻辑链路行必须带 ``require_graph`` 元数据
  coverage_gate       清单哈希对齐、门禁与状态清单错误
  coverage_backfill   auto 模式计划字段回填
  coverage_cli        check / final-check / main 命令行入口
"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import ast  # noqa: F401  (re-export; 测试以 mc.ast 引用)
import sys  # noqa: F401  (re-export)
from dataclasses import replace  # noqa: F401  (re-export; mock.patch 目标)
from pathlib import Path  # noqa: F401  (re-export)

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.retrieval import symbol_index  # noqa: F401  (re-export; 调用方用 module_coverage.symbol_index)

# DES-07：以下原语已迁到 coverage_manifest（叶子模块，见该模块 docstring）。
# 这里再导出以保持 module_coverage.<name> 对外可用（coverage_bootstrap、
# check/status/suggest_hits 等均依赖这些名字）。
from core.coverage.coverage_manifest import (  # noqa: F401  (re-export)
    AGENT_DIR,
    DISCOVERY_METADATA_END,
    DISCOVERY_METADATA_START,
    EVIDENCE_EXTS,
    EVIDENCE_PATH_RE,
    EvidenceProbe,
    MANIFEST,
    ModuleRow,
    REPO_ROOT,
    SOURCE_EXTS,
    bounded_errors,
    build_discovery_metadata,
    default_manifest_for_root,
    evidence_is_real,
    evidence_is_real_uncached,
    manifest_discovery_metadata,
    parse_cells,
    parse_manifest,
    resolve_in_root,
)

# DES-07 二期拆分：实现按簇下沉为同包叶子模块，本文件退化为再导出门面，
# 行为与对外调用面（module_coverage.<name> / mock.patch 目标）不变。

from core.coverage.coverage_platform import (  # noqa: F401  (re-export)
    AUTO_OWNER,
    AUTO_TOOL_RULES,
    UNIT_PLATFORM_TOKENS,
    derive_auto_tool,
    host_platform,
    missing_plan_field,
    unit_platform,
)
from core.coverage.coverage_discovery import (  # noqa: F401  (re-export)
    INHERITED_FROM_RE,
    auto_business_cap,
    asset_units,
    accept_inherited_evidence,
    discovery_row,
    evidence_manifest_errors,
    inherited_evidence_reference,
    inheritable_scope,
    participant_ids,
    cached_unit_risk_score,
    resolve_inherited_evidence,
    row_text,
    scope_path,
    coverage_unit_id,
    discover_asset_unit_paths,
    discover_audit_unit_paths,
    discover_minimum_unit_paths,
    discovery_rows,
    discovery_units,
)
from core.coverage.coverage_backfill import (  # noqa: F401  (re-export)
    auto_backfill_rows,
)
from core.coverage.coverage_business import (  # noqa: F401  (re-export)
    CLI_COMMAND_PATTERNS,
    PassContext,
    WEB_ENDPOINT_PATTERNS,
    announce_feature_surface,
    graph_scope,
    business_discovery,
    business_discovery_rows,
    coverage_summary,
    extract_feature_surface,
)
from core.coverage.coverage_validate import (  # noqa: F401  (re-export)
    VALID_PHASES,
    VALID_STATUS,
    internal_scope_errors,
    plan_field_errors,
    validate_rows,
)
from core.coverage.coverage_gate import (  # noqa: F401  (re-export)
    HASH_RE,
    PROJECT_GRAPH_MARKER,
    SYMBOL_INDEX_MARKER,
    manifest_discovery_mode,
    manifest_gate_errors,
    manifest_requires_graph,
    manifest_requires_symbols,
    reconcile_discovery_rows,
    row_hash,
    startup_gate_errors,
    status_manifest_errors,
)
from core.coverage.coverage_cli import (  # noqa: F401  (re-export)
    apply_auto_backfill,
    check,
    ingest_test_coverage_report,
    main,
    row_from_line,
    status,
    suggest_hits,
    write_inventory_diff,
)

_row_from_line = row_from_line


if __name__ == "__main__":
    sys.exit(main())
