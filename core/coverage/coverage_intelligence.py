#!/usr/bin/env python3
"""归一真实覆盖报告，映射最小审计单元并生成行为覆盖缺口。"""

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
import tempfile
from pathlib import Path, PurePosixPath

_CORE = Path(__file__).resolve().parent.parent
_REPO = _CORE.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.infra import safe_xml
from core.retrieval import symbol_index

SCHEMA_VERSION = 1
FORMATS = {"coveragepy", "lcov", "jacoco", "generic", "mutation"}
METRICS = ("statement", "branch", "condition", "exception", "state", "mutation")
DEFAULT_THRESHOLDS = {
    "statement": 80.0,
    "branch": 70.0,
    "condition": 0.0,
    "exception": 0.0,
    "state": 0.0,
    "mutation": 60.0,
}


class CoverageError(ValueError):
    """覆盖报告格式或门禁错误。"""


def _percent(covered: object, total: object) -> float | None:
    if (
        isinstance(covered, bool)
        or isinstance(total, bool)
        or not isinstance(covered, (int, float))
        or not isinstance(total, (int, float))
    ):
        return None
    if total <= 0 or covered < 0 or covered > total:
        return None
    return round(float(covered) / float(total) * 100, 2)


def _count(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CoverageError(f"覆盖计数必须是对象: {value!r}")
    covered = value.get("covered")
    total = value.get("total")
    if (
        isinstance(covered, bool)
        or isinstance(total, bool)
        or not isinstance(covered, int)
        or not isinstance(total, int)
        or covered < 0
        or total < 0
        or covered > total
    ):
        raise CoverageError(f"覆盖计数无效: {value!r}")
    return {"covered": covered, "total": total}


def _counts_from_pair(covered: object, total: object) -> dict[str, int] | None:
    if covered is None or total is None:
        return None
    return _count({"covered": covered, "total": total})


def _normalize_path(path: object, root: Path | None = None) -> str:
    if not isinstance(path, str):
        raise CoverageError(f"覆盖记录 path 必须是字符串: {path!r}")
    raw = path.strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        raise CoverageError("覆盖记录缺 path")
    is_drive_path = len(raw) >= 3 and raw[1] == ":" and raw[2] == "/"
    if root is not None:
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise CoverageError(f"覆盖根目录不可访问 {root}: {exc}") from exc
        candidate = Path(raw) if PurePosixPath(raw).is_absolute() or is_drive_path else resolved_root / raw
        try:
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise CoverageError(f"覆盖路径逃出项目根目录: {path}") from exc
        normalized = relative.as_posix()
    else:
        if PurePosixPath(raw).is_absolute() or is_drive_path:
            raise CoverageError("绝对覆盖路径必须配合 --root")
        normalized = PurePosixPath(raw).as_posix()
        if normalized == ".." or normalized.startswith("../"):
            raise CoverageError(f"覆盖路径逃出项目根目录: {path}")
    if not normalized or normalized == "." or normalized.startswith("../"):
        raise CoverageError(f"覆盖路径无效: {path}")
    return normalized


def _metric(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CoverageError(f"覆盖指标必须是数值: {value!r}")
    number = float(value)
    if not 0 <= number <= 100:
        raise CoverageError(f"覆盖指标必须在 0..100: {number}")
    return round(number, 2)


def _locations(locations: object) -> list[dict] | None:
    if locations is None:
        return None
    if not isinstance(locations, list):
        raise CoverageError("覆盖 locations 必须是数组")
    result = []
    for item in locations:
        if not isinstance(item, dict):
            raise CoverageError("覆盖 location 记录无效")
        start_line = item.get("start_line", item.get("line"))
        end_line = item.get("end_line", start_line)
        if (
            isinstance(start_line, bool)
            or isinstance(end_line, bool)
            or not isinstance(start_line, int)
            or not isinstance(end_line, int)
            or start_line < 1
            or end_line < start_line
        ):
            raise CoverageError(f"覆盖 location 行号无效: {item!r}")
        raw_metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
        raw_counts = item.get("counts") if isinstance(item.get("counts"), dict) else {}
        normalized_counts = {name: _count(raw_counts.get(name)) for name in METRICS}
        normalized_metrics = {}
        for name in METRICS:
            count = normalized_counts[name]
            normalized_metrics[name] = (
                _percent(count["covered"], count["total"])
                if count is not None
                else _metric(raw_metrics.get(name))
            )
        result.append({
            "start_line": start_line,
            "end_line": end_line,
            "metrics": normalized_metrics,
            "counts": normalized_counts,
        })
    return result


def _record(path: str, source: str, root: Path | None = None,
            counts: object = None, locations: object = None,
            **metrics: object) -> dict:
    normalized = _normalize_path(path, root)
    raw_counts = counts if isinstance(counts, dict) else {}
    normalized_counts = {name: _count(raw_counts.get(name)) for name in METRICS}
    normalized_metrics = {}
    for name in METRICS:
        count = normalized_counts[name]
        normalized_metrics[name] = (
            _percent(count["covered"], count["total"])
            if count is not None
            else _metric(metrics.get(name))
        )
    payload = {
        "path": normalized,
        "source": source,
        "metrics": normalized_metrics,
        "counts": normalized_counts,
    }
    normalized_locations = _locations(locations)
    if normalized_locations:
        payload["locations"] = normalized_locations
    return payload


def _coveragepy(payload: dict, root: Path | None = None) -> list[dict]:
    files = payload.get("files")
    if not isinstance(files, dict):
        raise CoverageError("coverage.py JSON 缺 files 对象")
    result = []
    for path, item in files.items():
        summary = item.get("summary", {}) if isinstance(item, dict) else {}
        statement = summary.get("percent_covered")
        if statement is None:
            statement = _percent(summary.get("covered_lines"), summary.get("num_statements"))
        branch = _percent(summary.get("covered_branches"), summary.get("num_branches"))
        counts = {
            "statement": _counts_from_pair(
                summary.get("covered_lines"), summary.get("num_statements"),
            ),
            "branch": _counts_from_pair(
                summary.get("covered_branches"), summary.get("num_branches"),
            ),
        }
        locations = []
        if isinstance(item, dict):
            executed = item.get("executed_lines")
            missing = item.get("missing_lines")
            if isinstance(executed, list) or isinstance(missing, list):
                executed = executed if isinstance(executed, list) else []
                missing = missing if isinstance(missing, list) else []
                executed_lines = {
                    line for line in executed
                    if isinstance(line, int) and not isinstance(line, bool) and line >= 1
                }
                missing_lines = {
                    line for line in missing
                    if isinstance(line, int) and not isinstance(line, bool) and line >= 1
                }
                for line in sorted(executed_lines | missing_lines):
                    locations.append({
                        "line": line,
                        "counts": {
                            "statement": {
                                "covered": int(line in executed_lines), "total": 1,
                            },
                        },
                    })
        result.append(_record(
            path, "coveragepy", root=root, counts=counts, locations=locations,
            statement=statement, branch=branch,
        ))
    return result


def _lcov(text: str, root: Path | None = None) -> list[dict]:
    result = []
    current: dict[str, object] = {}
    for line in [*text.splitlines(), "end_of_record"]:
        if line.startswith("SF:"):
            current = {"path": line[3:].strip()}
        elif line.startswith("DA:") and current.get("path"):
            parts = line[3:].split(",")
            if len(parts) >= 2:
                try:
                    line_number = int(parts[0])
                    hits = int(parts[1])
                except ValueError as exc:
                    raise CoverageError(f"LCOV DA 记录无效: {line}") from exc
                current.setdefault("lines", []).append((line_number, hits))
        elif line.startswith("BRDA:") and current.get("path"):
            parts = line[5:].split(",")
            if len(parts) >= 4:
                try:
                    line_number = int(parts[0])
                except ValueError as exc:
                    raise CoverageError(f"LCOV BRDA 记录无效: {line}") from exc
                current.setdefault("branches", []).append((line_number, parts[3] != "-"))
        elif ":" in line and line.split(":", 1)[0] in {"LF", "LH", "BRF", "BRH"}:
            key, value = line.split(":", 1)
            current[key] = value.strip()
        elif line == "end_of_record" and current.get("path"):
            try:
                statement_counts = _counts_from_pair(
                    int(current.get("LH", 0)), int(current.get("LF", 0)),
                )
                branch_counts = _counts_from_pair(
                    int(current.get("BRH", 0)), int(current.get("BRF", 0)),
                )
            except (TypeError, ValueError) as exc:
                raise CoverageError("LCOV 计数记录无效") from exc
            locations_by_line: dict[int, dict] = {}
            for line_number, hits in current.get("lines", []):
                locations_by_line.setdefault(line_number, {"line": line_number, "counts": {}})
                locations_by_line[line_number]["counts"]["statement"] = {
                    "covered": int(hits > 0), "total": 1,
                }
            branch_lines: dict[int, list[bool]] = {}
            for line_number, taken in current.get("branches", []):
                branch_lines.setdefault(line_number, []).append(taken)
            for line_number, outcomes in branch_lines.items():
                locations_by_line.setdefault(line_number, {"line": line_number, "counts": {}})
                locations_by_line[line_number]["counts"]["branch"] = {
                    "covered": sum(outcomes), "total": len(outcomes),
                }
            result.append(_record(
                current["path"], "lcov", root=root,
                counts={"statement": statement_counts, "branch": branch_counts},
                locations=list(locations_by_line.values()),
            ))
            current = {}
    if not result:
        raise CoverageError("LCOV 没有 SF 记录")
    return result


def _jacoco(text: str, root: Path | None = None) -> list[dict]:
    try:
        # SEC-07：JaCoCo 报告是被审计项目的产物；拒绝 DTD/实体并限制
        # 体积、深度与元素数，避免实体膨胀 DoS 拖垮审计宿主。
        xml_root = safe_xml.parse_xml(text)
    except safe_xml.XmlSecurityError as exc:
        raise CoverageError(f"JaCoCo XML 无效: {exc}") from exc
    result = []
    for package in xml_root.findall(".//package"):
        package_name = package.attrib.get("name", "").strip("/")
        for source_file in package.findall("sourcefile"):
            counters = {item.attrib.get("type"): item for item in source_file.findall("counter")}
            counts = {}
            locations = []
            for metric, counter_name in (("statement", "LINE"), ("branch", "BRANCH")):
                counter = counters.get(counter_name)
                if counter is not None:
                    covered = int(counter.attrib.get("covered", 0))
                    missed = int(counter.attrib.get("missed", 0))
                    counts[metric] = _counts_from_pair(covered, covered + missed)
            for line in source_file.findall("line"):
                try:
                    line_number = int(line.attrib["nr"])
                    covered_instructions = int(line.attrib.get("ci", 0))
                    missed_instructions = int(line.attrib.get("mi", 0))
                    covered_branches = int(line.attrib.get("cb", 0))
                    missed_branches = int(line.attrib.get("mb", 0))
                except (KeyError, ValueError) as exc:
                    raise CoverageError("JaCoCo line 记录无效") from exc
                line_counts = {}
                if covered_instructions + missed_instructions:
                    line_counts["statement"] = _counts_from_pair(
                        covered_instructions,
                        covered_instructions + missed_instructions,
                    )
                if covered_branches + missed_branches:
                    line_counts["branch"] = _counts_from_pair(
                        covered_branches,
                        covered_branches + missed_branches,
                    )
                if line_counts:
                    locations.append({"line": line_number, "counts": line_counts})
            path = "/".join(part for part in (package_name, source_file.attrib.get("name")) if part)
            result.append(_record(path, "jacoco", root=root, counts=counts, locations=locations))
    if not result:
        raise CoverageError("JaCoCo XML 没有 sourcefile 记录")
    return result


def _generic(payload: dict, source: str = "generic", root: Path | None = None) -> list[dict]:
    files = payload.get("files")
    if not isinstance(files, list):
        raise CoverageError("generic coverage 缺 files 数组")
    result = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise CoverageError("generic coverage file 记录无效")
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
        result.append(_record(
            item["path"], source, root=root, counts=item.get("counts"),
            locations=item.get("locations"), **metrics,
        ))
    return result


def _mutation(payload: dict, root: Path | None = None) -> list[dict]:
    files = payload.get("files")
    if not isinstance(files, list):
        raise CoverageError("mutation report 缺 files 数组")
    result = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise CoverageError("mutation file 记录无效")
        score = item.get("mutation")
        if score is None:
            killed = item.get("killed")
            total = item.get("total")
            score = _percent(killed, total)
        counts = _counts_from_pair(item.get("killed"), item.get("total"))
        result.append(_record(
            item["path"], "mutation", root=root, counts={"mutation": counts},
            locations=item.get("locations"), mutation=score,
        ))
    return result


def import_report(path: Path, report_format: str, root: Path | None = None) -> dict:
    if report_format not in FORMATS:
        raise CoverageError(f"不支持覆盖格式: {report_format}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoverageError(f"无法读取覆盖报告 {path}: {exc}") from exc
    if report_format == "lcov":
        files = _lcov(text, root=root)
    elif report_format == "jacoco":
        files = _jacoco(text, root=root)
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CoverageError(f"覆盖 JSON 无效: {exc}") from exc
        if report_format == "coveragepy":
            files = _coveragepy(payload, root=root)
        elif report_format == "mutation":
            files = _mutation(payload, root=root)
        else:
            files = _generic(payload, root=root)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "coverage-intelligence",
        "source_format": report_format,
        "files": files,
    }


def _normalize_record(item: object, root: Path | None = None) -> dict:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        raise CoverageError("归一覆盖 file 记录无效")
    raw_metrics = item.get("metrics")
    if raw_metrics is None:
        raw_metrics = {}
    if not isinstance(raw_metrics, dict):
        raise CoverageError("归一覆盖 file 记录缺 metrics 对象")
    return _record(
        item["path"], str(item.get("source", "unknown")), root=root,
        counts=item.get("counts"), locations=item.get("locations"), **raw_metrics,
    )


def merge_reports(reports: list[dict], root: Path | None = None) -> dict:
    merged: dict[str, dict] = {}
    for report in reports:
        if report.get("schema_version") != SCHEMA_VERSION or not isinstance(report.get("files"), list):
            raise CoverageError("归一覆盖报告 schema 无效")
        for raw_item in report["files"]:
            item = _normalize_record(raw_item, root)
            path = item["path"]
            target = merged.setdefault(path, {
                "path": path,
                "sources": [],
                "metrics": {name: None for name in METRICS},
                "counts": {name: None for name in METRICS},
                "locations": [],
            })
            target["sources"] = list(dict.fromkeys([
                *target["sources"], item.get("source", "unknown"),
            ]))
            item_counts = item["counts"]
            for name in METRICS:
                value = item["metrics"][name]
                incoming_count = item_counts[name]
                current_count = target["counts"][name]
                current_value = target["metrics"][name]
                if incoming_count is not None:
                    if current_count is not None:
                        covered = current_count["covered"] + incoming_count["covered"]
                        total = current_count["total"] + incoming_count["total"]
                        target["counts"][name] = {"covered": covered, "total": total}
                        target["metrics"][name] = _percent(covered, total)
                    elif current_value is not None:
                        raise CoverageError(
                            f"同一路径指标重复但缺少可合并计数: {path} ({name})"
                        )
                    else:
                        target["counts"][name] = incoming_count
                        target["metrics"][name] = _percent(
                            incoming_count["covered"], incoming_count["total"],
                        )
                elif value is not None:
                    if current_count is not None or current_value is not None:
                        raise CoverageError(
                            f"同一路径指标重复但缺少可合并计数: {path} ({name})"
                        )
                    target["metrics"][name] = value
            target["locations"].extend(item.get("locations") or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "coverage-intelligence-merged",
        "files": [merged[path] for path in sorted(merged)],
    }


def _unit_ranges(root: Path) -> dict[str, tuple[int, int]]:
    try:
        index = symbol_index.index_repository(root, strict=True)
    except symbol_index.SourceDiscoveryError as exc:
        raise CoverageError(str(exc)) from exc
    errors = [item for item in index["files"] if item.get("error")]
    if errors:
        raise CoverageError(
            "源码单元解析不完整，不能判定覆盖率: "
            + "; ".join(f"{item['path']}: {item['error']}" for item in errors[:5])
        )
    return {
        symbol["scope"]: (symbol["start_line"], symbol["end_line"])
        for item in index["files"]
        for symbol in item["symbols"]
    }


def _symbol_metrics(record: dict, start_line: int, end_line: int) -> dict[str, float | None]:
    locations = record.get("locations")
    if not isinstance(locations, list):
        return {name: None for name in METRICS}
    totals = {name: {"covered": 0, "total": 0} for name in METRICS}
    values: dict[str, list[float]] = {name: [] for name in METRICS}
    for location in locations:
        if not isinstance(location, dict):
            continue
        location_start = location.get("start_line", location.get("line"))
        location_end = location.get("end_line", location_start)
        if (
            not isinstance(location_start, int)
            or not isinstance(location_end, int)
            or location_end < start_line
            or location_start > end_line
        ):
            continue
        location_counts = location.get("counts") if isinstance(location.get("counts"), dict) else {}
        location_metrics = location.get("metrics") if isinstance(location.get("metrics"), dict) else {}
        for name in METRICS:
            count = location_counts.get(name)
            if isinstance(count, dict) and isinstance(count.get("covered"), int) and isinstance(count.get("total"), int):
                totals[name]["covered"] += count["covered"]
                totals[name]["total"] += count["total"]
            else:
                value = location_metrics.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values[name].append(float(value))
    result: dict[str, float | None] = {}
    for name in METRICS:
        total = totals[name]["total"]
        if total:
            result[name] = _percent(totals[name]["covered"], total)
        elif values[name]:
            result[name] = round(sum(values[name]) / len(values[name]), 2)
        else:
            result[name] = None
    return result


def _project_fingerprint(root: Path) -> str:
    import hashlib
    try:
        # Coverage identity belongs to the project, not to a Session/Round.
        # Never use ``polling_id`` here: it changes on a new session and would
        # make unchanged coverage look like a new project.
        manifest = json.loads((root / ".bugx" / "manifest.json").read_text(encoding="utf-8"))
        pid = manifest.get("project_id")
        if not isinstance(pid, str) or not pid:
            current = json.loads((root / ".bugx" / "current.json").read_text(encoding="utf-8"))
            pid = current.get("project_id")
        if not isinstance(pid, str) or not pid:
            pid = str(root.resolve())
    except Exception:
        pid = str(root.resolve())
    return hashlib.sha256(str(pid).encode()).hexdigest()[:16]


def _classify_debt(gaps: list[dict]) -> str:
    if not gaps:
        return "covered"
    reasons = {g.get("reason") for g in gaps}
    if "not_measured" in reasons:
        return "uncovered"
    return "partial"


def coverage_gaps(root: Path, merged: dict,
                  thresholds: dict[str, float] | None = None) -> dict:
    root = root.resolve()
    configured = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    for name, value in configured.items():
        if name not in METRICS:
            raise CoverageError(f"未知覆盖指标: {name}")
        metric = _metric(value)
        if metric is None:
            raise CoverageError(f"覆盖阈值必须是数值: {name}")
        configured[name] = metric
    if not isinstance(merged.get("files"), list):
        raise CoverageError("归一覆盖报告缺 files 数组")
    by_path = {}
    for raw_item in merged["files"]:
        item = _normalize_record(raw_item, root)
        by_path[item["path"]] = item
    try:
        audit_units = symbol_index.audit_units(root, strict=True)
    except symbol_index.SourceDiscoveryError as exc:
        raise CoverageError(str(exc)) from exc
    unit_errors = [unit for unit in audit_units if unit.get("error")]
    if unit_errors:
        raise CoverageError(
            "源码单元解析不完整，不能判定覆盖率: "
            + "; ".join(
                f"{unit['path']}: {unit['error']}" for unit in unit_errors[:5]
            )
        )
    ranges = _unit_ranges(root)
    units = []
    for unit in audit_units:
        record = by_path.get(unit["path"])
        if not record:
            metrics = {name: None for name in METRICS}
        elif unit["scope"] == unit["path"]:
            metrics = record["metrics"]
        else:
            unit_range = ranges.get(unit["scope"])
            metrics = (
                _symbol_metrics(record, *unit_range)
                if unit_range is not None
                else {name: None for name in METRICS}
            )
        gaps = []
        for name, minimum in configured.items():
            if minimum <= 0:
                continue
            value = metrics.get(name)
            if value is None:
                gaps.append({"metric": name, "reason": "not_measured", "minimum": minimum})
            elif value < minimum:
                gaps.append({
                    "metric": name, "reason": "below_threshold",
                    "actual": value, "minimum": minimum,
                })
        debt_state = _classify_debt(gaps)
        units.append({
            "scope": unit["scope"], "path": unit["path"], "kind": unit["kind"],
            "metrics": metrics, "gaps": gaps, "covered": not gaps,
            "debt_state": debt_state,
            "unit_debt": bool(gaps),
        })
    covered = sum(item["covered"] for item in units)
    unit_debt = sum(1 for item in units if item["unit_debt"])
    # 按 DOC-011 §5.4 分层统计：单元债务 vs 已覆盖，预留全局阻塞位
    debt = {
        "total": len(units),
        "covered": covered,
        "unit_debt": unit_debt,
        "global_blocked": 0,
        "by_reason": {
            "not_measured": sum(1 for u in units for g in u["gaps"] if g["reason"] == "not_measured"),
            "below_threshold": sum(1 for u in units for g in u["gaps"] if g["reason"] == "below_threshold"),
        },
        "has_unit_debt": unit_debt > 0,
        "has_global_block": False,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "coverage-gap-report",
        "thresholds": configured,
        "total": len(units),
        "covered": covered,
        "percent": round(covered / len(units) * 100, 2) if units else 0.0,
        "ready": bool(units) and covered == len(units),
        "debt": debt,
        "project_fingerprint": _project_fingerprint(root),
        "units": units,
    }


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageError(f"无法读取归一覆盖报告 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CoverageError("覆盖报告根必须是对象")
    return value


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("import", "merge", "gaps", "gate"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--format", choices=sorted(FORMATS))
    parser.add_argument("--report", type=Path, action="append", default=[])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--thresholds", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            if not args.input or not args.format:
                raise CoverageError("import 必须提供 --input 和 --format")
            payload = import_report(args.input, args.format, root=args.root)
        else:
            if not args.report:
                raise CoverageError(f"{args.command} 必须至少提供一个 --report")
            merged = merge_reports([_load(path) for path in args.report], root=args.root)
            if args.command == "merge":
                payload = merged
            else:
                thresholds = _load(args.thresholds) if args.thresholds else None
                payload = coverage_gaps(args.root, merged, thresholds)
        if args.output:
            _write(args.output, payload)
        if args.json or not args.output:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.command == "gate" and not payload["ready"]:
            return 1
        return 0
    except CoverageError as exc:
        print(f"[coverage_intelligence] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
