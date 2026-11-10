#!/usr/bin/env python3
"""module_coverage.py — 最小审计单元与跨模块业务链路覆盖门禁。

这是「全覆盖宪法」的外部检查器，不相信 agent 口头报告：
  discover    递归发现每个源码文件，生成最小单元清单骨架
  check       校验最小单元、业务链路、阶段顺序、工具与证据
  final-check 要求内部最小单元和跨模块业务链路全部「已覆盖」

机械可验证的最小下限是源码文件；一个文件含多个可独立验证的函数、类、路由、
命令或状态机时，用 ``path/to/file.py::symbol`` 继续拆分。必须先把全部「内部」
最小单元标为已覆盖，才能开始「业务」阶段的模块间数据流/状态流审计。
"""

# DES-07 二期拆分：命令行入口簇（check / final-check / main /
# suggest_hits / status / ingest_test_coverage_report / _apply_auto_backfill /
# _row_from_line）下沉为本模块；模块 docstring 即拆分前原文——argparse 以
# ``description=__doc__`` 生成 ``--help``，任何改写都会改变 CLI 输出。

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

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import core.secure_atomic as secure_atomic
from core.retrieval import symbol_index

from core.coverage.coverage_backfill import auto_backfill_rows
from core.coverage.coverage_business import business_discovery, business_discovery_rows, coverage_summary
from core.coverage.coverage_discovery import participant_ids, row_text, coverage_unit_id, discovery_rows
from core.coverage.coverage_gate import (
    PROJECT_GRAPH_MARKER,
    SYMBOL_INDEX_MARKER,
    manifest_discovery_mode,
    manifest_gate_errors,
    reconcile_discovery_rows,
    status_manifest_errors,
)
from core.coverage.coverage_manifest import (
    MANIFEST,
    REPO_ROOT,
    ModuleRow,
    default_manifest_for_root,
    parse_cells,
    parse_manifest,
)
from core.coverage.coverage_platform import AUTO_OWNER, missing_plan_field
from core.coverage.coverage_validate import validate_rows


def apply_auto_backfill(
    manifest: Path,
    rows: list[ModuleRow],
    root: Path,
    *,
    evidence_manifest: Path | None = None,
) -> dict[str, int]:
    """对齐符号集、回填计划字段并补齐业务链路行，返回变更统计。

    auto 模式的完整顺序：
    1. reconcile：枚举器升级导致符号集漂移时，按字节哈希对齐内部单元
       （编号稳定、未变字节的证据保留），不伪造覆盖；
    2. 回填缺失的计划字段（主工具/负责人/证据指针，全部指向真实文件）；
    3. 若清单没有业务链路行，从自动关系图生成并同样回填。
    """
    stats = {"tool": 0, "owner": 0, "evidence": 0, "business": 0, "changed": 0}
    lines = manifest.read_text(encoding="utf-8").splitlines()

    reconciled_lines, changes = reconcile_discovery_rows(
        rows, root,
        symbols=manifest_discovery_mode(manifest) not in {"files", "files-only"},
    )
    reconciled_rows = [row_from_line(line) for line in reconciled_lines]
    # L1-ADV-D4：reconcile 确实会把内容变化的单元重置为「未覆盖」，post 需要机读
    # 真相才能把失效单元交回 Supervisor 重新派发。把同一份 changes 落盘为
    # ``inventory-diff.json``（与 bootstrap 同 schema、原子写），让"校验"不再
    # 偷偷改覆盖状态却对外宣称没改。
    invalidated = [
        str(scope) for scope in (changes or {}).get("changed") or ()
    ]
    if changes:
        write_inventory_diff(manifest, changes)

    todo_path = manifest.parent / "audit-todo.md"
    todo_reference = ""
    if todo_path.is_file():
        todo_reference = Path(os.path.relpath(todo_path, root)).as_posix()
    covered: dict[str, str] = {}
    if evidence_manifest is not None and evidence_manifest.is_file():
        try:
            payload = json.loads(evidence_manifest.read_text(encoding="utf-8"))
            for record in payload.get("records") or []:
                unit_id = str(record.get("unit_id") or "")
                artifact = str(record.get("artifact_path") or "")
                if unit_id and artifact:
                    covered[unit_id] = artifact
        except (OSError, ValueError):
            pass
    filled_rows, fill_stats = auto_backfill_rows(
        reconciled_rows, root,
        todo_reference=todo_reference, covered_evidence=covered,
    )
    stats["tool"] += fill_stats["tool"]
    stats["owner"] += fill_stats["owner"]
    stats["evidence"] += fill_stats["evidence"]

    has_business = any(row.phase == "业务" for row in filled_rows)
    business_lines: list[str] = []
    if not has_business:
        seen_participants: set[tuple[str, ...]] = set()
        for line in business_discovery_rows(root, filled_rows):
            if "UNRESOLVED" in line:
                continue
            cells = parse_cells(line)
            if len(cells) < 16:
                continue
            participants = tuple(participant_ids(cells[5]))
            if len(participants) < 2 or participants in seen_participants:
                continue
            seen_participants.add(participants)
            if "待补" in line and todo_reference:
                line = line.replace("待补", todo_reference)
            row = row_from_line(line)
            fill: dict[str, str] = {}
            if missing_plan_field(row.owner):
                fill["owner"] = AUTO_OWNER
            if missing_plan_field(row.tool):
                fill["tool"] = "business-flow-review"
            if missing_plan_field(row.evidence) and todo_reference:
                fill["evidence"] = todo_reference
            if fill:
                row = replace(row, **fill)
            business_lines.append(row_text(row))
        stats["business"] = len(business_lines)

    header_index = next(
        (i for i, line in enumerate(lines) if "路径/范围" in line and "状态" in line),
        None,
    )
    if header_index is not None:
        separator_index = next(
            (i for i in range(header_index + 1, len(lines)) if "---" in lines[i]),
            header_index + 1,
        )
        data_start = separator_index + 1
        data_end = data_start
        # 表体可能由**多个以空行分隔的连续表行块**组成：coverage bootstrap 会把
        # 自动业务链路/端点行写成紧跟空行之后的独立块。若只替换第一个块，
        # 而 filled_rows 已包含从后续块解析出的业务行，就会把同一批业务行
        # 写进第一个块、同时保留原块 ⇒ 业务行翻倍（实测 64 → 128），
        # 进而触发「清单 # 标识不能为空且不能重复」+「覆盖证据 duplicate」，
        # 使自动续轮永久失败。这里把所有相邻（允许空行分隔）的表行块
        # 一并纳入替换区间，保证 check 幂等。
        while data_end < len(lines):
            if lines[data_end].lstrip().startswith("|"):
                data_end += 1
                continue
            if (lines[data_end].strip() == ""
                    and data_end + 1 < len(lines)
                    and lines[data_end + 1].lstrip().startswith("|")):
                data_end += 1
                continue
            break
        lines[data_start:data_end] = (
            [row_text(row) for row in filled_rows] + business_lines
        )
        # 原子写：非原子 write_text 会让并发读者读到截断的清单，
        # ``_manifest_units_blob`` 落到「按原始字节缓存」分支后产出一次性假指纹。
        secure_atomic.atomic_write_text(
            manifest, "\n".join(lines) + "\n", mode=0o600, parent_mode=0o700,
        )
    if invalidated:
        stats["invalidated"] = invalidated  # type: ignore[assignment]
    if any(stats[key] for key in ("tool", "owner", "evidence", "business")):
        stats["changed"] = 1
    return stats


def write_inventory_diff(manifest: Path, changes: dict) -> Path:
    """Persist the rescan result beside the lifecycle manifest (0600, atomic).

    L1-ADV-D4：``check --auto-backfill`` 的 reconcile 会重置内容变化单元的状态。
    该副作用必须可机读，post 才能把失效单元交回 Supervisor 重新派发。
    """
    target = manifest.with_name("inventory-diff.json")
    secure_atomic.atomic_write_json(
        target, {"schema_version": 1, "changes": changes},
        mode=0o600, parent_mode=0o700,
    )
    return target


def row_from_line(line: str) -> ModuleRow:
    cells = parse_cells(line)
    names = ("#", "阶段", "父模块", "模块", "路径/范围", "参与单元", "最小性",
             "难度", "命中", "主工具", "负责任务", "依赖", "状态", "发现数",
             "证据/测试", "备注")
    index = {name: i for i, name in enumerate(names)}

    def get(key: str) -> str:
        return cells[index[key]] if key in index else ""

    return ModuleRow(
        number=get("#"), phase=get("阶段"), parent=get("父模块"),
        name=get("模块"), path=get("路径/范围"), participants=get("参与单元"),
        minimality=get("最小性"), difficulty=get("难度"), hit=get("命中"),
        tool=get("主工具"), owner=get("负责任务"), deps=get("依赖"),
        status=get("状态"), findings=get("发现数"), evidence=get("证据/测试"),
        note=get("备注"),
    )


def check(final: bool = False, root: Path = REPO_ROOT,
          manifest: Path = MANIFEST,
          evidence_manifest: Path | None = None,
          require_evidence: bool = False,
          auto_backfill: bool = False,
          allow_business_partial: bool = False) -> int:
    rows, errors = parse_manifest(manifest)
    errors.extend(manifest_gate_errors(manifest))
    discovery_mode = manifest_discovery_mode(manifest)
    if discovery_mode in {"files", "files-only"}:
        print(
            "[module_coverage] 警告：当前清单是 files-mode/partial，"
            "跳过符号与关系图硬门禁；不可宣称符号级全覆盖。"
        )
    if auto_backfill and not errors:
        stats = apply_auto_backfill(
            manifest, rows, root, evidence_manifest=evidence_manifest,
        )
        if stats.get("changed"):
            print(
                "[module_coverage] auto-backfill（auto 模式，诚实记账）: "
                f"主工具 {stats['tool']} 行、负责任务 {stats['owner']} 行、"
                f"证据 {stats['evidence']} 行、业务链路 {stats['business']} 行"
            )
        invalidated = list(stats.get("invalidated") or ())
        if stats.get("changed") or invalidated:
            # 清单已被改写（回填字段 或 内容失效重置），必须重新解析后再判定，
            # 否则门禁读到的是失效前的旧状态（假通过）。
            rows, errors = parse_manifest(manifest)
            errors.extend(manifest_gate_errors(manifest))
        if invalidated:
            print(
                "[module_coverage] 内容已变化，旧证据失效（覆盖状态已重置为未覆盖）: "
                f"{len(invalidated)} 项 → " + ", ".join(invalidated[:20])
                + "；已写入 inventory-diff.json，需重新派发并重新覆盖"
            )
    errors.extend(validate_rows(
        rows, root, final=final,
        require_symbols=True,
        require_graph=True,
        evidence_manifest=evidence_manifest,
        require_evidence=require_evidence,
        discovery_mode=discovery_mode,
        allow_business_partial=bool(allow_business_partial and not final),
    ))
    internals = [row for row in rows if row.phase == "内部"]
    business = [row for row in rows if row.phase == "业务"]
    internal_covered = sum(row.status == "已覆盖" for row in internals)
    business_covered = sum(row.status == "已覆盖" for row in business)
    print(
        f"[module_coverage] 内部最小单元: {internal_covered}/{len(internals)} "
        f"({(internal_covered / len(internals) * 100) if internals else 0:.1f}%)"
    )
    print(
        f"[module_coverage] 模块间业务链路: {business_covered}/{len(business)} "
        f"({(business_covered / len(business) * 100) if business else 0:.1f}%)"
    )
    if errors:
        print(f"[module_coverage] 失败（{len(errors)} 项）:")
        for error in errors:
            print(f"  ✗ {error}")
        return 1
    print("[module_coverage] OK: 最小单元、阶段顺序、业务链路、工具与证据均有效")
    return 0


def status(root: Path = REPO_ROOT, manifest: Path = MANIFEST,
           as_json: bool = False,
           evidence_manifest: Path | None = None) -> int:
    """只读输出当前覆盖进度；清单无效或为空时失败。"""
    rows, errors = parse_manifest(manifest)
    if manifest.is_file():
        errors.extend(manifest_gate_errors(manifest))
        errors.extend(status_manifest_errors(rows))
    summary = coverage_summary(rows, errors)
    summary["available"] = manifest.is_file()
    summary["gates"] = {
        "symbol_index": True,
        "project_graph": True,
    }
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"[module_coverage] 当前阶段: {summary['phase']}")
        for key, label in (("internal", "内部最小单元"), ("business", "模块间业务链路")):
            item = summary[key]
            print(f"  {label}: {item['covered']}/{item['total']} ({item['percent']:.1f}%)")
        if summary["current_units"]:
            current = ", ".join(
                f"#{item['id']} {item['name']}[{item['status']}]"
                for item in summary["current_units"]
            )
            print(f"  当前单元: {current}")
        if errors:
            print(f"  清单提示: {len(errors)} 项（运行 check 查看详情）")
    return 0 if manifest.is_file() and rows and not errors else 1


def suggest_hits(root: Path = REPO_ROOT, manifest: Path = MANIFEST,
                 findings_payloads: list[dict] | None = None) -> dict:
    """按实测 findings 产出「命中」列建议值，替代手填优先级数据。

    统计每条清单路径（含 ``::symbol`` 前缀匹配）对应的非 rejected finding
    数并换算 1-5 档；与当前手填值对照输出漂移清单。只建议、不回写——
    应用与否由 Supervisor 决定，避免自动改写工作清单造成状态混乱。
    """
    rows, errors = parse_manifest(manifest)
    if errors:
        raise ValueError("; ".join(errors[:5]))
    counts: dict[str, int] = {}
    for payload in findings_payloads or []:
        for finding in (payload or {}).get("findings", []):
            if not isinstance(finding, dict):
                continue
            confirmation = finding.get("confirmation")
            if isinstance(confirmation, dict) and confirmation.get("state") == "rejected":
                continue
            for evidence in finding.get("evidence", []):
                if isinstance(evidence, dict) and isinstance(evidence.get("path"), str):
                    normalized = evidence["path"].replace("\\", "/").lstrip("./")
                    counts[normalized] = counts.get(normalized, 0) + 1

    def scale(count: int) -> int:
        if count <= 0:
            return 1
        if count <= 2:
            return 2
        if count <= 5:
            return 3
        if count <= 10:
            return 4
        return 5

    suggestions: list[dict] = []
    mismatches: list[dict] = []
    for row in rows:
        clean = row.path.strip().strip("`").rstrip("/")
        if "::" in clean:
            clean = clean.split("::", 1)[0]
        clean = clean.replace("\\", "/").lstrip("./")
        observed = 0
        if clean and clean not in {"-", "N/A", "待定"}:
            observed = sum(
                value for path, value in counts.items()
                if path == clean or path.startswith(clean + "/") or clean.startswith(path + "/")
            )
        suggested = scale(observed)
        current = None
        if row.hit.strip().isdigit():
            current = int(row.hit.strip())
        entry = {
            "number": row.number, "module": row.name,
            "path": clean or row.path,
            "observed_findings": observed, "suggested_hit": suggested,
            "current_hit": current,
        }
        suggestions.append(entry)
        if current is not None and current != suggested:
            mismatches.append(entry)
    return {
        "schema_version": 1,
        "rows_total": len(rows),
        "suggestions": suggestions,
        "mismatches": mismatches,
        "note": (
            "命中列应以实测为准更新 module-coverage.md；手填值只是冷启动默认"
            if mismatches else "手填命中与实测一致"
        ),
    }


def ingest_test_coverage_report(
    root: Path,
    report_path: Path | str,
    *,
    format_hint: str | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """读取真实测试覆盖率报告（lcov/cobertura/coveragepy等），反向回填覆盖清单。"""
    from core.coverage import coverage_intelligence
    root = root.resolve()
    rpath = Path(report_path)
    if not rpath.is_absolute():
        rpath = root / rpath
    if not rpath.is_file():
        raise FileNotFoundError(f"覆盖报告文件不存在: {rpath}")

    fmt = format_hint
    if not fmt:
        suf = rpath.suffix.lower()
        if suf in {".info", ".lcov"}:
            fmt = "lcov"
        elif suf == ".xml":
            fmt = "jacoco"
        elif suf == ".json":
            try:
                data = json.loads(rpath.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "files" in data:
                    fmt = "coveragepy"
                else:
                    fmt = "generic"
            except Exception:
                fmt = "generic"
        else:
            fmt = "lcov"

    report = coverage_intelligence.import_report(rpath, fmt, root=root)
    covered_files: dict[str, dict] = {}
    for f in report.get("files", []):
        p = f.get("path", "")
        try:
            rel = str(Path(p).resolve().relative_to(root)).replace("\\", "/")
        except Exception:
            rel = p.replace("\\", "/")
        covered_files[rel] = f

    mpath = manifest_path or default_manifest_for_root(root)
    if not mpath.is_file():
        return {
            "status": "NO_MANIFEST",
            "report_format": fmt,
            "covered_files_count": len(covered_files),
            "units_updated": 0,
            "detail": f"未找到覆盖清单: {mpath}",
        }

    rows, manifest_errors = parse_manifest(mpath)
    if manifest_errors:
        return {
            "status": "INVALID_MANIFEST",
            "report_format": fmt,
            "covered_files_count": len(covered_files),
            "units_updated": 0,
            "detail": manifest_errors,
        }
    try:
        lines = mpath.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "status": "INVALID_MANIFEST",
            "report_format": fmt,
            "covered_files_count": len(covered_files),
            "units_updated": 0,
            "detail": str(exc),
        }
    updated_units = []
    new_rows = []

    thresholds = coverage_intelligence.DEFAULT_THRESHOLDS

    def file_meets_thresholds(item: dict) -> bool:
        metrics = item.get("metrics")
        if not isinstance(metrics, dict):
            return False
        for metric, minimum in thresholds.items():
            if minimum <= 0:
                continue
            value = metrics.get(metric)
            if value is None and metric != "statement":
                # Branch/mutation dimensions are optional for execution-only
                # reports; enforce them whenever the producer supplied them.
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
                return False
        return True

    for row in rows:
        row_unit = coverage_unit_id(row)
        file_part = row.path.split("::")[0].strip().strip("`").strip()
        matching_file = covered_files.get(file_part)
        # A file report cannot prove an individual symbol or a business flow.
        # Only file-level rows may be promoted, and only when the shared
        # coverage-gaps thresholds are satisfied.
        if (
            matching_file
            and "::" not in row.path
            and row.status != "已覆盖"
            and file_meets_thresholds(matching_file)
        ):
            m = matching_file.get("metrics", {})
            stmt_pct = m.get("statement")
            pct_str = f"stmt={stmt_pct}%" if stmt_pct is not None else "covered"
            new_row = replace(
                row,
                status="已覆盖",
                tool="test-suite" if not row.tool or row.tool == "-" else f"{row.tool},test-suite",
                evidence=f"`{rpath.name}` ({pct_str})" if not row.evidence or row.evidence == "-" else row.evidence,
            )
            updated_units.append(row_unit)
            new_rows.append(new_row)
        else:
            new_rows.append(row)

    if updated_units:
        updated_lines = []
        for line in lines:
            if not line.strip().startswith("|") or "---" in line or "序号" in line or "phase" in line:
                updated_lines.append(line)
            else:
                cells = parse_cells(line)
                if cells and len(cells) >= 11:
                    num = cells[0].strip()
                    matched_r = next((r for r in new_rows if r.number == num), None)
                    if matched_r:
                        updated_lines.append(row_text(matched_r))
                    else:
                        updated_lines.append(line)
                else:
                    updated_lines.append(line)
        mpath.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")

    return {
        "status": "SUCCESS",
        "report_format": fmt,
        "covered_files_count": len(covered_files),
        "units_updated": len(updated_units),
        "updated_unit_ids": updated_units,
        "thresholds": dict(thresholds),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "discover", "discover-flows", "check", "final-check", "status", "hits",
        "review-packages", "review-apply",
    ), nargs="?",
                        default="check")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--manifest", type=Path, default=None,
        help="覆盖清单；省略时使用当前项目活动生命周期的 state/module-coverage.md",
    )
    parser.add_argument(
        "--findings", type=Path, action="append", default=[],
        help="hits 子命令使用：findings JSON（可重复），驱动命中列实测建议",
    )
    parser.add_argument(
        "--verdicts", type=Path, default=None,
        help="review-apply 子命令使用：宿主 AI 目录裁决 JSON",
    )
    parser.add_argument(
        "--reviewed-by", type=str, default=None,
        help="review-apply 子命令使用：宿主 AI 身份（必填，拒绝匿名裁决）",
    )
    parser.add_argument(
        "--evidence", type=Path,
        help="worker 覆盖证据 manifest；提供后启用 artifact/hash/target 严格门禁",
    )
    parser.add_argument(
        "--require-evidence", action="store_true",
        help="要求提供覆盖证据 manifest；正式 launch 门禁使用此选项",
    )
    parser.add_argument(
        "--auto-backfill", action="store_true",
        help="auto 模式：回填缺失的计划字段（主工具/负责人/证据指针）并补齐"
             "自动发现的业务链路行；只填记账元数据，不改写覆盖状态",
    )
    parser.add_argument(
        "--allow-business-partial", action="store_true",
        help="允许风险驱动业务链路在内部覆盖未完成时进入候选队列；不改变覆盖完成判定",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    args.root = (args.root or Path.cwd()).expanduser().resolve()
    args.manifest = (
        args.manifest.expanduser().resolve()
        if args.manifest is not None
        else default_manifest_for_root(args.root)
    )
    if args.command == "discover":
        try:
            rows = discovery_rows(args.root, symbols=True)
        except symbol_index.SourceDiscoveryError as exc:
            print(f"[module_coverage] ✗ {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({
                "schema_version": 1,
                "symbol_gate": True,
                "project_graph_gate": True,
                "rows": rows,
            }, ensure_ascii=False, indent=2))
        else:
            print(SYMBOL_INDEX_MARKER)
            print(PROJECT_GRAPH_MARKER)
            print("\n".join(rows))
        return 0 if rows else 1
    if args.command == "review-packages":
        from core.coverage import coverage_ai_review

        packages = coverage_ai_review.candidate_groups(
            args.root,
            source_paths=symbol_index.discover_source_paths(args.root),
            asset_paths=symbol_index.discover_asset_paths(args.root),
        )
        path = coverage_ai_review.write_review_packages(args.root, packages)
        if args.json:
            print(json.dumps({"packages": len(packages), "path": str(path)},
                             ensure_ascii=False))
        else:
            print(f"[coverage-review] 候选目录组 {len(packages)} 个，"
                  f"复核包已写入: {path}")
            for pkg in packages:
                print(f"  - {pkg['top_dir']}（files={pkg['file_count']}"
                      f" exts={pkg['extensions']}）")
            print("[coverage-review] 宿主 AI 必须逐目录基于证据判定 product/"
                  "exclude 并给出理由；裁决只允许移出。完成后执行：")
            print("  bugx coverage review-apply --verdicts <verdicts.json>"
                  " --reviewed-by <AI身份>")
        return 0
    if args.command == "review-apply":
        from core.coverage import coverage_ai_review

        if not args.verdicts:
            print("[coverage-review] ✗ review-apply 需要 --verdicts <file>",
                  file=sys.stderr)
            return 2
        try:
            result = coverage_ai_review.apply_verdicts(
                args.root, args.verdicts, args.reviewed_by or "",
            )
        except (OSError, ValueError) as exc:
            print(f"[coverage-review] ✗ {exc}", file=sys.stderr)
            return 2
        print(f"[coverage-review] ✓ 已持久化 {result['applied']} 条目录裁决"
              f"（reviewed_by={result['reviewed_by']}）→ {result['path']}")
        return 0
    if args.command == "discover-flows":
        manifest_rows, parse_errors = parse_manifest(args.manifest)
        if parse_errors:
            for error in parse_errors:
                print(f"[module_coverage] ✗ {error}", file=sys.stderr)
            return 2
        payload = business_discovery(args.root, manifest_rows)
        payload["rows"] = business_discovery_rows(args.root, manifest_rows)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("\n".join(payload["rows"]))
        return 0 if payload["complete"] else 2
    if args.command == "status":
        return status(
            args.root, args.manifest, as_json=args.json,
            evidence_manifest=args.evidence,
        )
    if args.command == "hits":
        import json as _json

        payloads: list[dict] = []
        for path in args.findings:
            try:
                payloads.append(_json.loads(path.read_text(encoding="utf-8")))
            except (OSError, _json.JSONDecodeError) as exc:
                print(f"[module_coverage] ✗ 无法读取 findings {path}: {exc}",
                      file=sys.stderr)
                return 2
        try:
            result = suggest_hits(args.root, args.manifest, payloads)
        except ValueError as exc:
            print(f"[module_coverage] ✗ {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(_json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[module_coverage] 清单 {result['rows_total']} 行，"
                  f"命中漂移 {len(result['mismatches'])} 行")
            for item in result["mismatches"]:
                print(f"  {item['number']} {item['module']}: "
                      f"手填={item['current_hit']} 实测建议={item['suggested_hit']}"
                      f"（{item['observed_findings']} 个 finding）<- {item['path']}")
            print(f"[module_coverage] {result['note']}")
        return 0
    return check(
        args.command == "final-check", args.root, args.manifest,
        evidence_manifest=args.evidence,
        require_evidence=args.require_evidence,
        auto_backfill=args.auto_backfill,
        allow_business_partial=args.allow_business_partial,
    )


if __name__ == "__main__":
    sys.exit(main())
