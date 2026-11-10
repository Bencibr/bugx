#!/usr/bin/env python3
"""清单行级校验（DES-07 二期拆分的叶子模块）。

``validate_rows`` 及其辅助（``_internal_scope_errors`` /
``_plan_field_errors``），并承载阶段/状态白名单常量 ``VALID_STATUS`` /
``VALID_PHASES``（DES-05：与 unit_kind 同源，注释原文保留）。依赖
``coverage_business.PassContext``（轮内共享上下文）与
``coverage_discovery`` 的枚举辅助；``coverage_manifest`` 提供证据探针。
函数体逐字搬自拆分前源码。
"""

from __future__ import annotations

import math
from pathlib import Path

from core.retrieval import symbol_index
from core.planning import unit_kind

from core.coverage.coverage_business import PassContext
from core.coverage.coverage_discovery import (
    auto_business_cap,
    evidence_manifest_errors,
    participant_ids,
    scope_path,
)
from core.coverage.coverage_manifest import REPO_ROOT, SOURCE_EXTS, EvidenceProbe, ModuleRow, evidence_is_real


VALID_STATUS = {"未覆盖", "挖掘中", "挂起", "未知", "已覆盖"}
# DES-05：阶段名与单元前缀同源。此前这里独立硬编码 {"内部","业务"}，
# 而 "内部:"/"业务:" 前缀字面量散落在 6+ 模块——新增单元类型时两处会漂移，
# 漏改一处即静默误分类（例如绕过「内部优先」硬边界）。
VALID_PHASES = set(unit_kind.VALID_PHASES)


def internal_scope_errors(
    row: ModuleRow, label: str, probe: EvidenceProbe, seen: set[str],
) -> list[str]:
    """校验内部最小单元路径：真实存在、未重复、细分到文件而非目录。"""
    errors: list[str] = []
    resolved = probe.resolve(row.path)
    normalized = row.path.strip().strip("`").rstrip("/")
    if normalized in seen:
        errors.append(f"{label}: 最小单元路径重复: {row.path}")
    seen.add(normalized)
    if resolved is None:
        errors.append(f"{label}: 最小单元路径不存在或逃出仓库根目录: {row.path}")
        return errors
    info = probe.stat_info(resolved)
    if not info["is_file"] or (
        info["suffix"] not in SOURCE_EXTS
        and resolved.name not in symbol_index.AUDIT_ASSET_NAMES
        and info["suffix"] not in symbol_index.AUDIT_ASSET_EXTS
    ):
        errors.append(
            f"{label}: 内部阶段必须细分到源码文件、配置/依赖资产或 file::symbol，不能停在目录"
        )
    return errors


def plan_field_errors(
    row: ModuleRow, label: str, probe: EvidenceProbe,
) -> list[str]:
    """校验计划字段与证据真实性；证据判定走同轮共享探测缓存。"""
    errors: list[str] = []
    if not row.tool or row.tool in {"待定", "-"}:
        errors.append(f"{label}: 缺主工具（复杂项目协作责任不可为空）")
    if not row.owner or row.owner in {"待定", "-"}:
        errors.append(f"{label}: 缺负责任务（必须明确谁/哪个并行任务负责）")
    if not row.evidence or row.evidence in {"待定", "待补", "-"}:
        errors.append(f"{label}: 缺证据/测试记录")
    elif not evidence_is_real(row.evidence, probe.root, probe):
        errors.append(
            f"{label}: 证据必须引用仓库内现有文件（可带 :行号 或 ::test_name）"
        )
    return errors


def validate_rows(rows: list[ModuleRow], root: Path = REPO_ROOT,
                 final: bool = False, require_symbols: bool = False,
                 require_graph: bool = False,
                 evidence_manifest: Path | None = None,
                 require_evidence: bool = False,
                 discovery_mode: str | None = None,
                 require_plan_fields: bool = True,
                 allow_business_partial: bool = False,
                 context: PassContext | None = None) -> list[str]:
    # 同一轮校验共享一个上下文：证据文件事实与全仓枚举结果只计算一次。
    ctx = context if context is not None else PassContext(root)
    errors: list[str] = []
    seen: set[str] = set()
    if not rows:
        errors.append("覆盖清单没有真实最小单元（不能用空清单伪造 100% 覆盖）")
    identifiers = {row.number for row in rows}
    if "" in identifiers or len(identifiers) != len(rows):
        errors.append("清单 # 标识不能为空且不能重复")
    internal_ids = {row.number for row in rows if row.phase == "内部"}
    for row in rows:
        label = f"#{row.number} {row.name}"
        if row.phase not in VALID_PHASES:
            errors.append(f"{label}: 阶段无效: {row.phase!r}（仅内部/业务）")
        if row.phase == "内部" and (not row.parent or row.parent in {"待定", "-"}):
            errors.append(f"{label}: 内部最小单元缺父模块（顶层使用 ROOT）")
        if not row.minimality or row.minimality in {"待定", "-"}:
            errors.append(f"{label}: 缺最小性依据")
        if row.phase == "内部":
            errors.extend(internal_scope_errors(row, label, ctx.probe, seen))
        elif row.phase == "业务":
            participants = participant_ids(row.participants)
            if len(participants) < 2 and row.status != "挂起":
                errors.append(f"{label}: 业务链路至少引用两个内部单元")
            unknown = sorted(participants - internal_ids)
            if unknown and row.status != "挂起":
                errors.append(f"{label}: 业务链路引用未知内部单元: {', '.join(unknown)}")
        try:
            difficulty = int(row.difficulty)
            hit = int(row.hit)
            if not 1 <= difficulty <= 5 or not 1 <= hit <= 5:
                raise ValueError
        except ValueError:
            errors.append(f"{label}: 难度/命中必须是 1-5")
        if row.status not in VALID_STATUS:
            errors.append(f"{label}: 状态无效: {row.status!r}")
        try:
            if int(row.findings) < 0:
                raise ValueError
        except ValueError:
            errors.append(f"{label}: 发现数必须是非负整数")
        if require_plan_fields:
            errors.extend(plan_field_errors(row, label, ctx.probe))
    listed = {
        scope_path(row.path) for row in rows
        if row.phase == "内部" and row.path.strip() not in {"", "-", "待定"}
    }
    try:
        discovered = {
            p.rstrip("/") for p in ctx.audit_unit_paths()
        }
    except symbol_index.SourceDiscoveryError as exc:
        errors.append(f"源码枚举不完整，不能判定全覆盖: {exc}")
        discovered = set()
    omitted = sorted(p for p in discovered if p not in listed)
    if omitted:
        errors.append("最小单元清单漏列源码或行为资产: " + ", ".join(omitted))
    # A parser-partial manifest is an explicit, persisted file-level fallback.
    # It remains schedulable and honest, but cannot be forced back through the
    # symbol/graph gates that caused the fallback in the first place.
    partial_files = discovery_mode in {"files", "files-only"}
    if require_symbols and not partial_files:
        units = ctx.units()
        unit_errors = [unit for unit in units if unit.get("error")]
        if unit_errors:
            errors.append(
                "符号索引不完整，不能判定全覆盖: "
                + "; ".join(
                    f"{unit['path']}: {unit['error']}" for unit in unit_errors[:5]
                )
            )
        required_scopes = {unit["scope"] for unit in units}
        listed_scopes = {
            row.path.strip().strip("`").rstrip("/") for row in rows
            if row.phase == "内部"
        }
        omitted_scopes = sorted(required_scopes - listed_scopes)
        if omitted_scopes:
            errors.append("符号级最小单元漏列: " + ", ".join(omitted_scopes))
        known_scopes = {
            unit["scope"] for unit in units
        }
        unknown_scopes = sorted(
            scope for scope in listed_scopes
            if "::" in scope and scope not in known_scopes
        )
        if unknown_scopes:
            errors.append("清单引用不存在的源码符号: " + ", ".join(unknown_scopes))

    if require_graph and not partial_files:
        discovery = ctx.business(rows)
        if not discovery["complete"]:
            errors.append(
                "项目关系图不完整，不能判定业务链路全覆盖: "
                + "; ".join(
                    f"{item['path']}: {item['error']}"
                    for item in discovery["errors"][:5]
                )
            )
        business_participants = [
            participant_ids(row.participants) for row in rows if row.phase == "业务"
        ]
        # DOC-203 D1-c：与生成侧 business_discovery_rows 同源同序截断——
        # 清单只承载 confidence 最高的前 N 条自动链路，被截单元视同发现
        # 范围之外；校验器不得对截断后的清单要求全集。
        cap = auto_business_cap()
        flows = sorted(
            discovery["flows"],
            key=lambda flow: (
                -flow["confidence"]
                if isinstance(flow.get("confidence"), (int, float))
                and math.isfinite(flow["confidence"]) else float("inf"),
                str(flow.get("id", "")),
            ),
        )[:cap]
        for flow in flows:
            required_ids = set(flow["participant_ids"])
            if len(required_ids) < 2:
                continue
            if not any(required_ids <= participants for participants in business_participants):
                errors.append(
                    f"自动业务链路漏列 {flow['id']}: "
                    + ", ".join(sorted(required_ids))
                )

    internals = [row for row in rows if row.phase == "内部"]
    business = [row for row in rows if row.phase == "业务"]
    unfinished_internal = [row for row in internals if row.status != "已覆盖"]
    started_business = [row for row in business if row.status != "未覆盖"]
    if unfinished_internal and started_business and not allow_business_partial:
        errors.append(
            "阶段顺序错误：内部最小单元未全部覆盖前，不得开始模块间业务逻辑审计"
        )

    parent_modules = {row.parent for row in internals if row.parent not in {"", "ROOT", "-"}}
    if len(parent_modules) >= 2 and not business:
        errors.append("存在多个父模块，但未建立模块间业务逻辑链路清单")
    if final:
        uncovered = [r for r in rows if r.status != "已覆盖"]
        if uncovered:
            errors.append(
                "最终覆盖门禁失败: "
                + ", ".join(f"#{r.number} {r.name}={r.status}" for r in uncovered)
            )
    errors.extend(evidence_manifest_errors(
        rows, root, evidence_manifest, final=final, require=require_evidence,
    ))
    return errors
