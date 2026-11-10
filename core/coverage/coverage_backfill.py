#!/usr/bin/env python3
"""auto 模式的最小单元行回填（DES-07 二期拆分的叶子模块）。

回填所需的词法判定（``AUTO_OWNER`` / ``derive_auto_tool`` /
``missing_plan_field``）随簇下沉到 ``coverage_platform``，使本模块只依赖
``coverage_platform`` 与 ``coverage_manifest``，不再与「发现/门禁/命令行」
簇互指。调用方 ``coverage_cli``（check 的 auto 分支）主动 import 本模块——
DES-07 最小反馈边集中被拆除的 I→H 边由此改为单向。
函数体逐字搬自拆分前的 ``module_coverage.py``，行为不变。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Mapping

from core.coverage.coverage_manifest import ModuleRow
from core.coverage.coverage_platform import AUTO_OWNER, derive_auto_tool, missing_plan_field


def auto_backfill_rows(
    rows: list[ModuleRow],
    root: Path,
    *,
    todo_reference: str | None = None,
    covered_evidence: Mapping[str, str] | None = None,
) -> tuple[list[ModuleRow], dict[str, int]]:
    """auto 模式计划字段补全：只填记账元数据，绝不改写覆盖状态。

    - 主工具按文件类型推导；负责任务统一 host-auto；
    - 证据优先取 checkpoint 覆盖证据（真实文件），未覆盖单元指向
      lifecycle 审计待办文件（同样必须真实存在），诚实表达"计划记录"，
      不伪造"已覆盖"。
    """
    covered = covered_evidence or {}
    stats = {"tool": 0, "owner": 0, "evidence": 0, "business": 0, "changed": 0}
    out: list[ModuleRow] = []
    for row in rows:
        tool, owner, evidence = row.tool, row.owner, row.evidence
        if missing_plan_field(tool):
            tool = derive_auto_tool(row.path) if row.phase == "内部" \
                else "business-flow-review"
            stats["tool"] += 1
        if missing_plan_field(owner):
            owner = AUTO_OWNER
            stats["owner"] += 1
        if missing_plan_field(evidence):
            scope = row.path.strip().strip("`")
            evidence = covered.get(scope) or covered.get(row.number) or todo_reference or ""
            if evidence:
                stats["evidence"] += 1
        out.append(replace(row, tool=tool, owner=owner, evidence=evidence))
    return out, stats
