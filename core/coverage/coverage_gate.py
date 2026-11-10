#!/usr/bin/env python3
"""清单哈希对齐、启动门禁与状态清单错误（DES-07 二期拆分的叶子模块）。

``reconcile_discovery_rows``（字节哈希对齐，编号稳定、不伪造覆盖）、
``startup_gate_errors`` / ``manifest_gate_errors`` /
``status_manifest_errors``，以及符号索引/关系图谱标记常量
（``SYMBOL_INDEX_MARKER`` / ``PROJECT_GRAPH_MARKER``）与 ``HASH_RE``。
依赖方向单向：gate → {discovery, business, validate, manifest}，无回边。
函数体逐字搬自拆分前源码。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from core.retrieval import symbol_index

from core.coverage.coverage_business import PassContext
from core.coverage.coverage_discovery import discovery_row, participant_ids, row_text, discovery_units
from core.coverage.coverage_manifest import ModuleRow, manifest_discovery_metadata, parse_manifest
from core.coverage.coverage_validate import VALID_PHASES, VALID_STATUS, validate_rows


SYMBOL_INDEX_MARKER = "<!-- bugx-symbol-index: 1 -->"
PROJECT_GRAPH_MARKER = "<!-- bugx-project-graph: 1 -->"


HASH_RE = re.compile(r"sha256[:=]([0-9a-f]{64})", re.IGNORECASE)


def row_hash(row: ModuleRow) -> str | None:
    match = HASH_RE.search(row.note or "")
    return match.group(1).lower() if match else None


def manifest_discovery_mode(manifest: Path | None) -> str:
    """Read the persisted discovery mode without introducing an import cycle."""
    if manifest is None:
        return "symbols"
    try:
        # DES-02：读取原语已下沉到本模块，无需再延迟导入 coverage_bootstrap。
        metadata = manifest_discovery_metadata(manifest)
        return str(metadata.get("discovery_mode") or "unknown")
    except (OSError, ValueError, TypeError, AttributeError):
        return "unknown"


def reconcile_discovery_rows(
    previous_rows: list[ModuleRow], root: Path, *, symbols: bool | None = None,
) -> tuple[list[str], dict]:
    """Rescan a new lifecycle while retaining only evidence for unchanged bytes.

    Unit numbers of existing internal scopes remain stable, new scopes are
    appended, and changed/deleted scopes lose their old coverage claim.  This
    is deliberately a hash-based, last-known-evidence policy rather than a
    second conflict/merge system.
    """
    use_symbols = True if symbols is None else symbols
    previous_scope_rows: dict[str, list[ModuleRow]] = {}
    for row in previous_rows:
        if row.phase != "内部":
            continue
        scope = row.path.strip().strip("`").rstrip("/")
        previous_scope_rows.setdefault(scope, []).append(row)
    for scope, matches in previous_scope_rows.items():
        if len(matches) < 2:
            continue
        baseline = replace(matches[0], number="")
        ambiguous = (
            any(replace(row, number="") != baseline for row in matches[1:])
            or any(row.status != "未覆盖" or row.findings.strip() != "0" for row in matches)
        )
        if ambiguous:
            raise ValueError(
                "上一生命周期清单含重复内部 scope，且存在覆盖/发现状态或字段冲突，"
                f"拒绝合并歧义证据: {scope}"
            )

    units = discovery_units(root, symbols=use_symbols, strict=True)
    unit_errors = [unit for unit in units if unit.get("error")]
    if unit_errors:
        raise symbol_index.SourceDiscoveryError(
            "源码解析不完整，拒绝合并生命周期清单: "
            + "; ".join(f"{u['path']}: {u['error']}" for u in unit_errors[:5])
        )
    previous_internal = {
        row.path.strip().strip("`").rstrip("/"): row
        for row in previous_rows if row.phase == "内部"
    }
    previous_paths = set(previous_internal)
    current_by_path = {str(unit["scope"]): unit for unit in units}
    current_paths = set(current_by_path)
    changed_paths = {
        path for path in current_paths & previous_paths
        if row_hash(previous_internal[path]) != current_by_path[path].get("content_hash")
    }
    added = sorted(current_paths - previous_paths)
    removed = sorted(previous_paths - current_paths)
    renamed: list[dict[str, str]] = []
    unmatched_added = set(added)
    unmatched_removed = set(removed)
    for old_path in sorted(removed):
        old_hash = row_hash(previous_internal[old_path])
        if not old_hash:
            continue
        candidates = [
            path for path in sorted(unmatched_added)
            if current_by_path[path].get("content_hash") == old_hash
        ]
        if len(candidates) == 1:
            new_path = candidates[0]
            renamed.append({"from": old_path, "to": new_path, "sha256": old_hash})
            unmatched_added.remove(new_path)
            unmatched_removed.remove(old_path)

    rename_sources = {item["to"]: item["from"] for item in renamed}
    used_numbers = {
        int(row.number) for row in previous_rows
        if row.number.isdigit()
    }
    next_number = max(used_numbers or {0}) + 1
    rendered: list[ModuleRow] = []
    risk_cache: dict[tuple[str, str, str], float | None] = {}
    for unit in units:
        scope = str(unit["scope"])
        old_scope = rename_sources.get(scope, scope)
        old = previous_internal.get(old_scope)
        if old is not None:
            row = discovery_row(old.number, unit, reset=scope in changed_paths,
                                  previous=old, root=root, risk_cache=risk_cache)
            if scope in changed_paths:
                row = replace(row, note=row.note + "；增量重扫：内容已变化，旧证据失效")
        else:
            row = discovery_row(str(next_number), unit, root=root, risk_cache=risk_cache)
            next_number += 1
        rendered.append(row)

    def _number_order(unit_id: str) -> tuple[int, str]:
        """按 ``业务:16:...`` 的数字段排序，稳定且可读。"""
        _prefix, _sep, rest = unit_id.partition(":")
        head, _sep2, _tail = rest.partition(":")
        return (int(head) if head.isdigit() else 1 << 30, unit_id)

    current_ids = {row.number for row in rendered}
    for row in previous_rows:
        if row.phase != "业务":
            continue
        participants = participant_ids(row.participants)
        missing = participants - current_ids
        if missing:
            # 参与单元被删除（或按排除规则移出审计范围）时，把死编号从
            # participants 里剪掉并退回「未覆盖」：保留死编号只能让该链路
            # 永久 挂起，人必须手改表格才能重新开工；剪掉后链路在剩余单元
            # 上自然可再次排期，缺失事实写进备注留痕。
            order = _number_order
            kept = sorted(participants & current_ids, key=order)
            status = "未覆盖" if len(kept) >= 2 else "挂起"
            note = (
                f"{row.note}；增量重扫：参与内部单元已移出审计范围 "
                f"{', '.join(sorted(missing, key=order))}，"
                + ("链路已缩窄，需重新验证" if status == "未覆盖" else "剩余参与单元不足两条，需重新规划")
            )[:500]
            rendered.append(replace(
                row, participants=", ".join(kept) or "-",
                status=status, findings="0", evidence="待补", note=note,
            ))
        elif participants & {
            previous_internal[path].number for path in changed_paths
            if path in previous_internal
        }:
            rendered.append(replace(
                row, status="未覆盖", findings="0", evidence="待补",
                note=(row.note + "；增量重扫：参与单元内容变化，需重新复现")[:500],
            ))
        else:
            rendered.append(row)

    changes = {
        "added": sorted(unmatched_added),
        "changed": sorted(changed_paths),
        "removed": sorted(unmatched_removed),
        "renamed": renamed,
        "current_units": len(units),
    }
    return [row_text(row) for row in rendered], changes


def manifest_requires_symbols(path: Path) -> bool:
    try:
        return SYMBOL_INDEX_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def manifest_requires_graph(path: Path) -> bool:
    try:
        return PROJECT_GRAPH_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def manifest_gate_errors(path: Path) -> list[str]:
    """门禁始终开启；清单 marker 只能声明，不能关闭完整性检查。"""
    if not path.is_file():
        return []
    errors = []
    if not manifest_requires_symbols(path):
        errors.append(
            f"覆盖清单缺少必需符号索引标记: {SYMBOL_INDEX_MARKER}"
        )
    if not manifest_requires_graph(path):
        errors.append(
            f"覆盖清单缺少必需项目关系图标记: {PROJECT_GRAPH_MARKER}"
        )
    return errors


def startup_gate_errors(
    root: Path, manifest: Path, *, allow_business_partial: bool = False,
) -> list[str]:
    """Return strict errors for the automatic Supervisor start gate.

    ``coverage_bootstrap.inspect`` remains a fast, user-facing readiness
    probe.  The automatic control plane needs the stronger contract used by
    ``check``: generated markers, real source/asset discovery, symbol
    completeness and project-graph completeness.  An empty temporary
    workspace has no auditable units and is allowed for protocol fixtures;
    real projects with units are always checked strictly.  Unit discovery
    runs BEFORE the empty-rows shortcut: a project with real units and an
    empty manifest must fail the gate instead of silently skipping it.
    """
    rows, errors = parse_manifest(manifest)
    errors = list(errors) + manifest_gate_errors(manifest)
    # 「发现单元」与随后的 validate_rows 属于同一轮门禁：共享上下文让全仓枚举
    # 与证据探测缓存只计算一次，避免同一份文件系统快照被遍历两遍。
    ctx = PassContext(root)
    try:
        discovered_units = ctx.audit_unit_paths()
    except (OSError, ValueError, RuntimeError) as exc:
        return errors + [f"无法发现项目最小单元: {exc}"]
    if not discovered_units:
        # 空临时工作区没有任何可审计单元：协议夹具放行。符号索引/关系图
        # marker 是真实项目的防伪要求，空工作区无防伪对象，一并跳过。
        return []
    if not rows:
        return errors + ["清单漏列全部最小单元"]
    discovery_mode = manifest_discovery_mode(manifest)
    errors.extend(validate_rows(
        rows, root, require_symbols=True, require_graph=True,
        require_plan_fields=False, discovery_mode=discovery_mode,
        allow_business_partial=allow_business_partial, context=ctx,
    ))
    return list(dict.fromkeys(errors))


def status_manifest_errors(rows: list[ModuleRow]) -> list[str]:
    """Perform only bounded, row-local checks for the interactive status view.

    ``status`` is a progress readout and must remain responsive even when a
    legacy project has a multi-megabyte manifest.  Symbol indexing, source
    enumeration, evidence I/O, and graph discovery belong to ``check`` and
    ``final-check``; repeating them here made a harmless status probe time out.
    """
    if not rows:
        return ["覆盖清单没有真实最小单元"]
    errors: list[str] = []
    ids = [row.number for row in rows]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        errors.append("清单 # 标识不能为空且不能重复")
    for row in rows:
        if row.phase not in VALID_PHASES:
            errors.append(f"#{row.number} 阶段无效: {row.phase!r}")
        if row.status not in VALID_STATUS:
            errors.append(f"#{row.number} 状态无效: {row.status!r}")
    return errors
