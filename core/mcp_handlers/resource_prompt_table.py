#!/usr/bin/env python3
"""core/mcp_handlers/resource_prompt_table.py - MCP Resource 与 Prompt 模板元数据声明。"""

from __future__ import annotations

from typing import Any


def _i18n_prompt(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        import core.i18n as i18n
        return i18n.prompt_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback

def _resource_definitions() -> list[dict[str, object]]:
    """声明 BugX 5 大核心只读事实态 Resource。"""
    return [
        {
            "uri": "bugx://state/status",
            "name": "Supervisor Status",
            "description": "当前 Supervisor 运行态（Life/Round/并发门禁/预算）",
            "mimeType": "application/json",
        },
        {
            "uri": "bugx://state/coverage",
            "name": "Coverage Status",
            "description": "代码覆盖矩阵与债务单元清单",
            "mimeType": "application/json",
        },
        {
            "uri": "bugx://findings/recent",
            "name": "Recent Findings",
            "description": "已确认缺陷与待复现缺陷清单",
            "mimeType": "application/json",
        },
        {
            "uri": "bugx://knowledge/mistakes",
            "name": "Mistakes Notebook",
            "description": "项目错题本与高频踩坑反模式",
            "mimeType": "text/markdown",
        },
        {
            "uri": "bugx://profile/target",
            "name": "Target Profile",
            "description": "目标项目画像与语言/技术栈定性",
            "mimeType": "application/json",
        },
    ]


def _prompt_definitions() -> list[dict[str, object]]:
    """声明 BugX 4 大阶段专精工作流 Prompt。"""
    return [
        {
            "name": "bugx-audit-explore",
            "description": _i18n_prompt("mcp.explore_desc", "勘察与证据挖掘阶段 Harness，装配专精规则与 AST 上下文"),
            "arguments": [
                {
                    "name": "unit_id",
                    "description": _i18n_prompt("mcp.explore_unit_id_desc", "待勘察的目标覆盖单元 ID"),
                    "required": False,
                },
                {
                    "name": "rule",
                    "description": _i18n_prompt("mcp.explore_rule_desc", "专精规则名称（如 concurrency, memory_leak 等）"),
                    "required": False,
                },
            ],
        },
        {
            "name": "bugx-fuzz-mutate",
            "description": _i18n_prompt("mcp.mutate_desc", "模糊测试与状态变异阶段 Harness，针对边界条件进行变异挖掘"),
            "arguments": [
                {
                    "name": "unit_id",
                    "description": _i18n_prompt("mcp.mutate_unit_id_desc", "目标覆盖单元 ID"),
                    "required": False,
                },
                {
                    "name": "rule",
                    "description": _i18n_prompt("mcp.mutate_rule_desc", "专精规则名称（可选）"),
                    "required": False,
                },
            ],
        },
        {
            "name": "bugx-repro-verify",
            "description": _i18n_prompt("mcp.repair_desc", "最小复现与差分修复阶段 Harness，生成独立复现用例与 TDD 闭环"),
            "arguments": [
                {
                    "name": "unit_id",
                    "description": _i18n_prompt("mcp.repair_unit_id_desc", "待复现单元 ID"),
                    "required": False,
                },
            ],
        },
        {
            "name": "bugx-gate-decision",
            "description": _i18n_prompt("mcp.gate_desc", "会话启动与门禁裁决阶段 Harness，综合评估覆盖度与退出标准"),
            "arguments": [
                {
                    "name": "decision_type",
                    "description": _i18n_prompt("mcp.gate_decision_type_desc", "裁决类型 (round_end | finish | next_round)"),
                    "required": False,
                },
            ],
        },
    ]


