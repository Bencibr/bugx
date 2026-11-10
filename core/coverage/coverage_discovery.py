#!/usr/bin/env python3
"""覆盖清单的**发现单元与业务切面**原语（DES-07 二期）。

本模块由 ``module_coverage.py`` 拆分而来，承载「发现」与「业务发现」两簇。它们
只依赖 ``coverage_manifest`` 的叶子原语与 ``symbol_index`` / ``project_graph``，
彼此单向（业务簇 → 发现簇），且**不依赖** ``module_coverage`` 里的对账门禁、
校验、自动回填、命令入口四簇。依赖方向因此是单向无环：

    module_coverage → coverage_discovery → coverage_manifest

``module_coverage`` 会**再导出**这里的全部公开名字，既有调用方
（``coverage_bootstrap``、``launch_phases.*``、``check`` / ``status`` 等）无需改动。

* D 发现单元 —— ``discover_minimum_unit_paths`` / ``discover_asset_unit_paths`` /
  ``discover_audit_unit_paths`` / ``discovery_units`` / ``discovery_rows`` /
  ``discovery_row`` / ``row_text`` / ``scope_path`` / ``coverage_unit_id`` /
  ``evidence_manifest_errors`` / ``participant_ids`` / ``asset_units``
* F 业务发现 —— ``extract_feature_surface`` / ``announce_feature_surface`` /
  ``business_discovery`` / ``business_discovery_rows`` / ``graph_scope`` /
  ``PassContext`` / ``coverage_summary``（含端点/CLI 模式常量）
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

from core.runtime import project_graph
from core.retrieval import symbol_index

from core.coverage.coverage_manifest import EvidenceProbe, ModuleRow, parse_manifest
# unit_platform 属 platform 簇（七簇层次 platform(0) <- discovery(1)）。
# 原先误写为从 coverage_manifest 导入，导致 ImportError：
# 18 个文件受影响，module_coverage 无法导入，pre/post 端到端全失败。
from core.coverage.coverage_platform import unit_platform


def discover_minimum_unit_paths(root: Path, strict: bool = False) -> set[str]:
    """递归发现产品源码文件，作为最小单元清单的机械下限。"""
    return {
        path.relative_to(root.resolve()).as_posix()
        for path in symbol_index.discover_source_paths(root, strict=strict)
    }


def discover_asset_unit_paths(
    root: Path, strict: bool = False, asset_paths: list[Path] | None = None,
) -> set[str]:
    """Return configuration/dependency/build/CI/deployment assets in scope.

    ``asset_paths`` 允许调用方复用同一次校验内已经枚举好的资产列表，避免在
    ``discover_audit_unit_paths`` 与 ``discovery_units`` 里各走一遍全仓遍历。
    """
    if asset_paths is None:
        asset_paths = symbol_index.discover_asset_paths(root, strict=strict)
    return {
        path.relative_to(root.resolve()).as_posix()
        for path in asset_paths
    }


def discover_audit_unit_paths(
    root: Path, strict: bool = False, asset_paths: list[Path] | None = None,
) -> set[str]:
    """Return the file-level lower bound for source and behavior assets."""
    return discover_minimum_unit_paths(root, strict=strict) | discover_asset_unit_paths(
        root, strict=strict, asset_paths=asset_paths,
    )


def scope_path(path: str) -> str:
    """把 ``file::symbol`` 归一到源码文件路径。"""
    return path.strip().strip("`").rstrip("/").split("::", 1)[0]


def coverage_unit_id(row: ModuleRow) -> str:
    """Return the stable unit id used by ``coverage_evidence`` submissions."""
    scope = row.path.strip().strip("`")
    if not scope or scope in {"-", "待定"}:
        scope = row.name.strip()
    return f"{row.phase}:{row.number}:{scope}"


def cached_unit_risk_score(
    file_path: str,
    root: Path,
    content_hash: str,
    cache: dict[tuple[str, str, str], float | None],
) -> float | None:
    """Analyze one source snapshot once per discovery/reconciliation operation."""
    key = (str(root.resolve()), file_path, content_hash)
    if key not in cache:
        try:
            from core.repair import test_strategy

            cache[key] = test_strategy.calculate_unit_risk_score(file_path, root)
        except Exception:
            # Risk is prioritization metadata, not coverage evidence. Preserve the
            # existing default-difficulty behavior and don't retry per symbol row.
            cache[key] = None
    return cache[key]


# F3'（对抗审计重设计）：跨 Session 继承覆盖的备注锚点，与
# ``coverage_bootstrap.apply_inherited_coverage`` 写入的
# ``inherited_from=<polling_id>/<lifecycle_id>`` 同形。
INHERITED_FROM_RE = re.compile(
    r"inherited_from=(polling-[0-9]{6,})/(lifecycle-[0-9]{6,})"
)


def inherited_evidence_reference(note: str) -> tuple[str, str] | None:
    """从清单行备注解析 ``inherited_from=<polling>/<lifecycle>`` 引用。"""
    match = INHERITED_FROM_RE.search(note or "")
    if match is None:
        return None
    return match.group(1), match.group(2)


def inheritable_scope(row: object) -> str:
    """跨 Session 稳定的内部单元身份（与 coverage_bootstrap 同一口径：scope）。"""
    scope = (getattr(row, "path", "") or "").strip().strip("`").rstrip("/")
    if not scope or scope in {"-", "待定", "N/A"}:
        return ""
    return scope


def resolve_inherited_evidence(
    root: Path, row: object, polling_id: str, lifecycle_id: str,
) -> tuple[bool, str]:
    """Existence validation of a bootstrap-inherited coverage reference.

    门禁对带 ``inherited_from`` 备注的已覆盖单元，只做**源 lifecycle 证据在案**
    校验：源清单确以 covered 判定同一 scope（单元编号跨 Session 重排，scope
    才是稳定身份），且源 ``coverage-evidence.json`` 确有该单元的 passed 记录
    （形状完整）。绝不复制证据文件、绝不改写任何字段——源记录的
    ``target_fingerprint`` 漂移与 ``observed_at`` 年龄信号原样保留，引擎也不为
    继承背书写任何新记录（语义审计 BLOCK 条款）。
    """
    import core.bugx_paths as bugx_paths

    source_state = (
        bugx_paths.bugx_root(root) / "pollings" / polling_id
        / "lifecycles" / lifecycle_id / "state"
    )
    source_manifest = source_state / "module-coverage.md"
    if source_manifest.is_symlink() or not source_manifest.is_file():
        return False, f"源清单缺失: {source_manifest}"
    try:
        source_rows, parse_errors = parse_manifest(source_manifest)
    except (OSError, UnicodeError, ValueError) as exc:
        return False, f"源清单无法解析: {source_manifest}: {exc}"
    if parse_errors:
        return False, f"源清单无效: {source_manifest}: {'; '.join(parse_errors[:3])}"
    scope = inheritable_scope(row)
    source_unit_id = ""
    for source_row in source_rows:
        if (
            getattr(source_row, "status", "") == "已覆盖"
            and inheritable_scope(source_row) == scope
        ):
            # 后写覆盖先写，与 historical_unit_verdicts 的「最近一次判定」一致。
            source_unit_id = coverage_unit_id(source_row)
    if not source_unit_id:
        return False, f"源清单没有该 scope 的 covered 判定: scope={scope or '-'}"

    source_evidence = source_state / "coverage-evidence.json"
    if source_evidence.is_symlink() or not source_evidence.is_file():
        return False, f"源覆盖证据缺失: {source_evidence}"
    try:
        payload = json.loads(source_evidence.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return False, f"源覆盖证据无法解析: {source_evidence}: {exc}"
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return False, f"源覆盖证据缺少 records 数组: {source_evidence}"
    from core.coverage import coverage_evidence

    for item in records:
        if not isinstance(item, dict) or item.get("unit_id") != source_unit_id:
            continue
        try:
            record = coverage_evidence.EvidenceRecord.from_dict(item)
        except ValueError as exc:
            return False, f"源证据记录损坏 unit={source_unit_id}: {exc}"
        if record.status != "passed" or record.exit_code != 0:
            return False, (
                f"源证据记录非 passed unit={source_unit_id} "
                f"status={record.status} exit_code={record.exit_code}"
            )
        return True, ""
    return False, f"源覆盖证据没有该单元记录: unit={source_unit_id} in {source_evidence}"


def accept_inherited_evidence(
    rows: list[ModuleRow], root: Path, missing: list[str],
) -> set[str]:
    """对缺失证据的单元尝试 bootstrap 继承引用，返回通过核实的 unit_id 集合。

    接受时在结算输出记录 ``covered via inherited evidence from`` 引用；拒绝时
    向 stderr 打印含源路径与原因的显式告警（R7 复盘第三节：继承失败不得静默）。
    """
    if not missing:
        return set()
    rows_by_id = {coverage_unit_id(row): row for row in rows}
    accepted: set[str] = set()
    for unit_id in missing:
        row = rows_by_id.get(unit_id)
        if row is None or getattr(row, "status", "") != "已覆盖":
            continue
        reference = inherited_evidence_reference(getattr(row, "note", "") or "")
        if reference is None:
            continue
        polling_id, lifecycle_id = reference
        ok, reason = resolve_inherited_evidence(root, row, polling_id, lifecycle_id)
        if ok:
            accepted.add(unit_id)
            print(
                f"[coverage] {unit_id} covered via inherited evidence "
                f"from {polling_id}/{lifecycle_id}"
            )
        else:
            print(
                f"[coverage] ⚠ 继承证据引用校验失败 unit={unit_id} "
                f"source={polling_id}/{lifecycle_id}: {reason}",
                file=sys.stderr,
            )
    return accepted


def evidence_manifest_errors(
    rows: list[ModuleRow], root: Path, manifest: Path | None, *, final: bool,
    require: bool = False,
) -> list[str]:
    """Validate worker evidence when an evidence manifest is supplied."""
    if manifest is None:
        if require:
            return ["覆盖证据 manifest 为必需项，不能只凭 Markdown 自报"]
        return []
    expected = {
        coverage_unit_id(row)
        for row in rows
        if row.status == "已覆盖" or final
    }
    if not manifest.is_file():
        # F3'：全新 Session 的首个 lifecycle 可能尚无本地证据文件。若全部期望
        # 单元都能经 bootstrap 继承引用在源 lifecycle 核实，则不因文件缺席直接
        # 拒绝；任何单元核实失败，维持原有拒绝（保留缺文件错误本身）。
        if expected and accept_inherited_evidence(rows, root, sorted(expected)) >= expected:
            return []
        return [f"覆盖证据 manifest 不存在: {manifest}"]
    try:
        from core.coverage import coverage_evidence

        report = coverage_evidence.validate_manifest(
            manifest, workspace_root=root, expected_units=expected,
        )
    except (OSError, ValueError) as exc:
        return [f"覆盖证据 manifest 无法验证: {exc}"]
    errors: list[str] = []
    if report.missing:
        accepted = accept_inherited_evidence(rows, root, list(report.missing))
        unresolved = [unit for unit in report.missing if unit not in accepted]
        if unresolved:
            errors.append("缺少覆盖证据: " + ", ".join(unresolved))
    for key, items in (
        ("invalid", report.invalid), ("stale", report.stale),
        ("duplicate", report.duplicate), ("conflict", report.conflicts),
    ):
        if items:
            errors.append(f"覆盖证据 {key}: {json.dumps(list(items), ensure_ascii=False)}")
    return errors


def participant_ids(value: str) -> set[str]:
    return set(re.findall(r"#?([A-Za-z0-9_.-]+)", value))


def asset_units(root: Path, strict: bool = True,
                 asset_paths: list[Path] | None = None) -> list[dict]:
    units: list[dict] = []
    if asset_paths is None:
        asset_paths = symbol_index.discover_asset_paths(root, strict=strict)
    for path in asset_paths:
        relative = path.relative_to(root.resolve()).as_posix()
        units.append({
            "scope": relative,
            "path": relative,
            "name": path.name,
            "kind": "asset",
            "line": 1,
            "parser": "asset-inventory",
            "error": None,
            "language": "asset",
            "content_hash": symbol_index.file_sha256(path),
        })
    return units


def discovery_units(root: Path, symbols: bool = True, strict: bool = True,
                    asset_paths: list[Path] | None = None) -> list[dict]:
    """Build the authoritative source + behavior-asset discovery list."""
    root = root.resolve()
    if symbols:
        units = symbol_index.audit_units(root, strict=strict)
    else:
        units = [
            {
                "scope": path,
                "path": path,
                "name": Path(path).stem,
                "kind": "file",
                "line": 1,
                "parser": "files-only",
                "error": None,
                "content_hash": symbol_index.file_sha256(root / path),
            }
            for path in sorted(discover_minimum_unit_paths(root, strict=strict))
        ]
    return units + asset_units(root, strict=strict, asset_paths=asset_paths)


def row_text(row: ModuleRow) -> str:
    path = row.path if row.path.startswith("`") else f"`{row.path}`"
    values = [
        row.number, row.phase, row.parent, row.name, path,
        row.participants, row.minimality, row.difficulty, row.hit, row.tool,
        row.owner, row.deps, row.status, row.findings, row.evidence, row.note,
    ]
    return "| " + " | ".join(values) + " |"


def discovery_row(number: str, unit: dict, *, reset: bool = True,
                   previous: ModuleRow | None = None,
                   root: Path | None = None,
                   risk_cache: dict[tuple[str, str, str], float | None] | None = None) -> ModuleRow:
    file_path = str(unit["path"])
    scope = str(unit["scope"])
    source = Path(file_path)
    parent = str(source.parent) if str(source.parent) != "." else "ROOT"
    kind = str(unit["kind"])
    minimality = (
        "源码文件（未发现可继续拆分的声明）" if kind == "file"
        else "配置/依赖/构建资产，作为独立行为边界"
        if kind == "asset"
        else f"{kind}，可独立构造输入与验证行为"
    )
    digest = str(unit.get("content_hash") or "")
    note = f"{unit.get('parser', 'unknown')} line {unit.get('line', 1)}"
    if digest:
        note += f" sha256:{digest}"
    unit_platform_name = unit_platform(scope)
    if unit_platform_name:
        note += f" platform:{unit_platform_name}"
    difficulty = "3"
    calc_risk: float | None = None
    if root is not None:
        try:
            operation_cache = risk_cache if risk_cache is not None else {}
            calc_risk = cached_unit_risk_score(file_path, root, digest, operation_cache)
            if calc_risk is not None:
                from core.repair import test_strategy

                difficulty = test_strategy.risk_score_to_difficulty(calc_risk)
                note += f" risk:{int(calc_risk)}"
        except Exception:
            pass
    if previous is not None and not reset:
        prev_note = previous.note or note
        if calc_risk is not None:
            if re.search(r"\brisk:\d+", prev_note):
                prev_note = re.sub(r"\brisk:\d+", f"risk:{int(calc_risk)}", prev_note)
            else:
                prev_note += f" risk:{int(calc_risk)}"
        return ModuleRow(
            number=number, phase="内部", parent=parent,
            name=str(unit["name"]), path=scope,
            participants=previous.participants, minimality=previous.minimality,
            difficulty=difficulty if previous.difficulty in {"1", "2", "3", "4", "5"} else previous.difficulty,
            hit=previous.hit,
            tool=previous.tool, owner=previous.owner, deps=previous.deps,
            status=previous.status, findings=previous.findings,
            evidence=previous.evidence, note=prev_note,
        )
    return ModuleRow(
        number=number, phase="内部", parent=parent,
        name=str(unit["name"]), path=scope, participants="-",
        minimality=minimality, difficulty=difficulty, hit="3", tool="待定",
        owner="待定", deps="无", status="未覆盖", findings="0",
        evidence="待补", note=note,
    )


def discovery_rows(root: Path, symbols: bool = True) -> list[str]:
    """生成源码、配置和依赖资产的内部最小单元 Markdown 行。"""
    units = discovery_units(root, symbols=symbols, strict=True)
    unit_errors = [unit for unit in units if unit.get("error")]
    if unit_errors:
        raise symbol_index.SourceDiscoveryError(
            "源码解析不完整，拒绝生成覆盖清单: "
            + "; ".join(
                f"{unit['path']}: {unit['error']}" for unit in unit_errors[:5]
            )
        )
    risk_cache: dict[tuple[str, str, str], float | None] = {}
    return [row_text(discovery_row(str(index), unit, root=root, risk_cache=risk_cache))
            for index, unit in enumerate(units, 1)]


WEB_ENDPOINT_PATTERNS = [
    # FastAPI / Flask / Bottle / Tornado: @app.get('/path'), @router.post("/path"), @bp.route("/path", methods=[...])
    (
        re.compile(
            r"""@(?:app|router|bp|blueprint|api)\.(get|post|put|delete|patch|options|head|route)\s*\(\s*["']([^"']+)["']""",
            re.IGNORECASE,
        ),
        "python_route",
    ),
    # Express / Koa / Router (JS/TS): app.get('/path', ...), router.post('/path', ...)
    (
        re.compile(
            r"""\b(?:app|router)\.(get|post|put|delete|patch|options|head)\s*\(\s*["']([^"']+)["']""",
            re.IGNORECASE,
        ),
        "js_route",
    ),
    # NestJS decorators (@Get('/path'), @Post('/path'))
    (
        re.compile(
            r"""@(Get|Post|Put|Delete|Patch|Options|Head)\s*\(\s*["']([^"']+)["']""",
        ),
        "nestjs_route",
    ),
    # Go Gin / Echo: r.GET("/path", ...), e.POST("/path", ...)
    (
        re.compile(
            r"""\.(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s*\(\s*["']([^"']+)["']""",
        ),
        "go_route",
    ),
    # Go net/http: http.HandleFunc("/path", ...)
    (
        re.compile(
            r"""http\.HandleFunc\s*\(\s*["']([^"']+)["']""",
        ),
        "go_http",
    ),
]

CLI_COMMAND_PATTERNS = [
    # Click: @click.command("name") or @click.group("name") or @cli.command("name")
    (
        re.compile(
            r"""@(?:click|cli|group|cmd)\.(?:command|group)\s*(?:\(\s*(?:name\s*=\s*)?["']([^"']+)["']|\(\s*\))?""",
        ),
        "click_cli",
    ),
    # Argparse add_parser: subparsers.add_parser("cmd", ...)
    (
        re.compile(
            r"""(?:add_parser|add_command)\s*\(\s*["']([^"']+)["']""",
        ),
        "argparse_cli",
    ),
]


def announce_feature_surface(root: Path, surfaces: list[dict[str, Any]]) -> None:
    """DOC-125 C125-2：端点切面一旦被抽取出来，就即刻喂给活图谱。

    这里是全仓**唯一**真正产出 endpoint 事实的地方（业务清单的 EP 行同源），
    所以 ``endpoint_discovered`` / ``endpoints_ready`` 从这根管线发出，图谱才不再是
    无人写的死库。清单生成绝不能因为图谱写不进去而失败，因此：未初始化的工作区
    （``bugx_paths.require`` 抛 LayoutError）、引擎副本缺 ``attack_graph*.py``、
    快照损坏等一律静默降级——图谱的可观测性由 pre/post 的 ``attack-graph-*``
    运行事件负责，那条路径在拥有完整布局的地方跑。
    """
    if not surfaces:
        return
    try:
        from core.autonomous_cve import attack_graph_store

        attack_graph_store.absorb_surface(Path(root), surfaces)
    except Exception:  # noqa: BLE001 - 图谱降级不得冒泡到清单生成
        pass


def extract_feature_surface(root: Path) -> list[dict[str, Any]]:
    """提取工程中的业务特性切面与端点（Web API 路由、CLI 命令端点等）。"""
    root = root.resolve()
    surfaces: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        source_paths = sorted(discover_audit_unit_paths(root, strict=False))
    except Exception:
        source_paths = []

    for rel_path in source_paths:
        file_path = root / rel_path
        if not file_path.is_file() or file_path.stat().st_size > 512 * 1024:
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        for line_no, line in enumerate(content.splitlines(), 1):
            line_str = line.strip()
            if not line_str or line_str.startswith(("#", "//", "/*", "*")):
                continue

            # 1. 匹配 Web API 路由
            for pattern, fw in WEB_ENDPOINT_PATTERNS:
                m = pattern.search(line_str)
                if m:
                    if fw == "go_http":
                        method = "ALL"
                        route_path = m.group(1)
                    else:
                        method = m.group(1).upper()
                        route_path = m.group(2)
                        if method == "ROUTE":
                            method = "ALL"
                    if not route_path.startswith("/"):
                        route_path = "/" + route_path
                    name = f"endpoint:{method}:{route_path}"
                    if name not in seen:
                        seen.add(name)
                        surfaces.append({
                            "type": "endpoint",
                            "name": name,
                            "method": method,
                            "path": route_path,
                            "source_file": rel_path,
                            "line": line_no,
                            "framework": fw,
                        })

            # 2. 匹配 CLI 命令
            for pattern, fw in CLI_COMMAND_PATTERNS:
                m = pattern.search(line_str)
                if m:
                    cmd_name = m.group(1) if m.groups() and m.group(1) else ""
                    if not cmd_name:
                        continue
                    name = f"cli:{cmd_name}"
                    if name not in seen:
                        seen.add(name)
                        surfaces.append({
                            "type": "cli",
                            "name": name,
                            "method": "CLI",
                            "path": cmd_name,
                            "source_file": rel_path,
                            "line": line_no,
                            "framework": fw,
                        })

    announce_feature_surface(root, surfaces)
    return surfaces


def graph_scope(node_id: str) -> str:
    if node_id.startswith("symbol:"):
        return node_id[len("symbol:"):]
    if node_id.startswith("file:"):
        return node_id[len("file:"):]
    return node_id


def business_discovery(root: Path, rows: list[ModuleRow]) -> dict:
    """把自动关系图中的 scope 映射到清单内部单元编号。"""
    graph = project_graph.build_graph(root)
    flows = project_graph.discover_business_flows(graph) if graph.get("complete") else []
    scope_to_id = {
        row.path.strip().strip("`").rstrip("/"): row.number
        for row in rows if row.phase == "内部"
    }

    def closest_scheduled_scope(scope: str) -> str | None:
        """Map parser-only nested callables to their scheduled owner."""
        candidate = scope
        while candidate:
            if candidate in scope_to_id:
                return candidate
            if "::" not in candidate:
                return None
            file_path, qualified = candidate.split("::", 1)
            if "." not in qualified:
                return None
            candidate = f"{file_path}::{qualified.rsplit('.', 1)[0]}"
        return None

    mapped = []
    for flow in flows:
        scopes = [graph_scope(node) for node in flow["participants"]]
        participants = list(dict.fromkeys(
            scope_to_id[scheduled]
            for scope in scopes
            if (scheduled := closest_scheduled_scope(scope)) is not None
        ))
        mapped.append({**flow, "scopes": scopes, "participant_ids": participants})
    return {
        "schema_version": 1,
        "complete": graph.get("complete", False),
        "errors": graph.get("parse_errors", []),
        "flows": mapped,
    }


class PassContext:
    """一次覆盖校验的共享上下文：证据探测缓存 + 全仓枚举结果。

    生命周期严格限定在单次 ``validate_rows`` 调用（以及调用方显式复用的同一轮
    门禁，如 ``startup_gate_errors`` 的「发现单元 + 校验」两步）。跨会话或跨进程
    复用会把旧的文件系统快照当成新结论，对安全门禁是正确性风险，因此这里只做
    轮内去重，不做全局 memo：``probe`` 缓存证据文件事实，其余槽位缓存
    ``discover_audit_unit_paths`` / ``discovery_units`` / ``business_discovery``
    的全仓遍历结果，三者在一轮内各自只计算一次。
    """

    __slots__ = ("root", "probe", "_asset_paths", "_audit_paths",
                 "_units", "_units_ready", "_business", "_business_ready")

    def __init__(self, root: Path):
        self.root = root
        self.probe = EvidenceProbe(root)
        self._asset_paths: list[Path] | None = None
        self._audit_paths: set[str] | None = None
        self._units: list[dict] | None = None
        self._units_ready = False
        self._business: dict | None = None
        self._business_ready = False

    def asset_paths(self) -> list[Path]:
        """全仓资产枚举只走一次，供审计单元路径与符号单元共用。"""
        if self._asset_paths is None:
            self._asset_paths = symbol_index.discover_asset_paths(
                self.root, strict=True,
            )
        return self._asset_paths

    def audit_unit_paths(self) -> set[str]:
        """全仓源码/资产路径枚举只跑一次；失败照旧抛给调用方处理。"""
        if self._audit_paths is None:
            self._audit_paths = discover_audit_unit_paths(
                self.root, strict=True, asset_paths=self.asset_paths(),
            )
        return self._audit_paths

    def units(self) -> list[dict]:
        """符号级最小单元枚举只跑一次，复用已枚举的资产列表。"""
        if not self._units_ready:
            self._units = discovery_units(
                self.root, symbols=True, strict=True,
                asset_paths=self.asset_paths(),
            )
            self._units_ready = True
        return self._units

    def business(self, rows: list[ModuleRow]) -> dict:
        """自动关系图只构建一次；``rows`` 在同一轮内是稳定入参。"""
        if not self._business_ready:
            self._business = business_discovery(self.root, rows)
            self._business_ready = True
        return self._business


def auto_business_cap() -> int:
    """DOC-203 D1-c：自动业务单元生成上限。

    环境变量 ``BUGX_MAX_AUTO_BUSINESS_UNITS`` 可调；<1、非整数或超上界
    回退默认 64 并告警（fail-closed 到保守值，不静默）。硬上界 4096 防
    env 注入把覆盖义务重新撑爆。
    """
    default_cap, max_cap = 64, 4096
    raw = os.environ.get("BUGX_MAX_AUTO_BUSINESS_UNITS")
    if not raw:
        return default_cap
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        print(
            f"[coverage] ⚠ BUGX_MAX_AUTO_BUSINESS_UNITS={raw!r} 非法，"
            f"回退默认 {default_cap}",
            file=sys.stderr,
        )
        return default_cap
    if value < 1:
        print(
            f"[coverage] ⚠ BUGX_MAX_AUTO_BUSINESS_UNITS={value} < 1，"
            f"回退默认 {default_cap}",
            file=sys.stderr,
        )
        return default_cap
    if value > max_cap:
        print(
            f"[coverage] ⚠ BUGX_MAX_AUTO_BUSINESS_UNITS={value} 超过硬上界 "
            f"{max_cap}，按 {max_cap} 执行",
            file=sys.stderr,
        )
        return max_cap
    return value


def business_discovery_rows(root: Path, rows: list[ModuleRow]) -> list[str]:
    """生成自动业务链路/端点面清单行（DOC-203 D1-c 加上限）。

    截断只发生在清单**生成**时刻（bootstrap），确定性排序为
    ``(tier, -confidence, id)``：flow 行按 confidence 降序（非有限/缺失
    视为最低档），EP 端点行无置信度语义，固定排在 flow 之后按 id 序。
    绝不对已绑定 target_fingerprint 的存量清单做事后截断/收缩/重编号。
    """
    # (tier, neg_confidence, id, row) —— tier 0=flow, 1=EP。
    entries: list[tuple[int, float, str, str]] = []
    # 1. 关系图自动发现的跨模块业务流
    for flow in business_discovery(root, rows)["flows"]:
        p_ids = flow.get("participant_ids") or []
        if len(p_ids) < 2:
            continue
        participants = ",".join(p_ids)
        evidence = flow["evidence"][0] if flow["evidence"] else "待补"
        try:
            confidence = float(flow["confidence"])
        except (KeyError, TypeError, ValueError):
            confidence = float("-inf")
        if not math.isfinite(confidence):
            confidence = float("-inf")
        entries.append((
            0,
            -confidence,
            str(flow["id"]),
            f"| {flow['id']} | 业务 | AUTO | 自动业务链路 {flow['id']} | - | "
            f"{participants} | project_graph 自动发现跨模块关系 | 3 | 4 | "
            f"project_graph | 待定 | {participants} | 未覆盖 | 0 | {evidence} | "
            f"flow:{flow['id']} confidence={flow['confidence']:.2f} |",
        ))

    # 2. 功能特性端点与 API 路由切面 (DOC-046)
    surfaces = extract_feature_surface(root)
    if surfaces:
        internal_file_to_id: dict[str, str] = {}
        for r in rows:
            if r.phase == "内部":
                clean_p = r.path.strip().strip("`").split("::")[0].rstrip("/")
                if clean_p not in internal_file_to_id:
                    internal_file_to_id[clean_p] = r.number
        all_internals = [r.number for r in rows if r.phase == "内部"]

        for idx, s in enumerate(surfaces, 1):
            s_id = f"EP{idx:02d}"
            s_name = s["name"]
            s_file = s["source_file"]
            p_ids = []
            if s_file in internal_file_to_id:
                p_ids.append(internal_file_to_id[s_file])
            for uid in all_internals:
                if uid not in p_ids:
                    p_ids.append(uid)
                    if len(p_ids) >= 2:
                        break
            participants = ",".join(p_ids) if len(p_ids) >= 2 else (",".join(all_internals[:2]) if len(all_internals) >= 2 else "UNRESOLVED")
            parent = "API" if s["type"] == "endpoint" else "CLI"
            
            diff = "3"
            try:
                from core.repair import test_strategy
                risk = test_strategy.calculate_unit_risk_score(s_file, root)
                diff = test_strategy.risk_score_to_difficulty(risk)
                r_note = f" risk:{int(risk)}"
            except Exception:
                r_note = ""

            entries.append((
                1,
                0.0,
                s_id,
                f"| {s_id} | 业务 | {parent} | {s_name} | - | "
                f"{participants} | 功能特性端点，驱动内部服务与状态迁移 | {diff} | 4 | "
                f"project_graph | 待定 | {participants} | 未覆盖 | 0 | 待补 | "
                f"feature:{s_name}{r_note} framework={s['framework']} |",
            ))

    cap = auto_business_cap()
    ordered = sorted(entries, key=lambda entry: (entry[0], entry[1], entry[2]))
    if len(ordered) > cap:
        print(
            f"[coverage] ⚠ 自动业务单元生成 {len(ordered)} 条，超过上限 "
            f"{cap}（BUGX_MAX_AUTO_BUSINESS_UNITS），已按确定性排序截断；"
            f"被截单元视同发现范围之外，不伪造覆盖",
            file=sys.stderr,
        )
        ordered = ordered[:cap]
    return [entry[3] for entry in ordered]


def coverage_summary(rows: list[ModuleRow], errors: list[str] | None = None) -> dict:
    """返回供状态页/自动化消费的两阶段进度，不把未完成当成命令失败。"""
    internals = [row for row in rows if row.phase == "内部"]
    business = [row for row in rows if row.phase == "业务"]
    internal_covered = sum(row.status == "已覆盖" for row in internals)
    business_covered = sum(row.status == "已覆盖" for row in business)
    active_rows = [row for row in rows if row.status in {"挖掘中", "未知"}]
    if not active_rows:
        active_rows = [row for row in rows if row.status == "挂起"]
    if internal_covered < len(internals):
        phase = "内部"
    elif business_covered < len(business):
        phase = "业务"
    elif rows:
        phase = "完成"
    else:
        phase = "未规划"
    total = len(internals) + len(business)
    covered = internal_covered + business_covered
    unknown_internal = sum(row.status == "未知" for row in internals)
    unknown_business = sum(row.status == "未知" for row in business)
    return {
        "phase": phase,
        "current_units": [
            {
                "id": row.number,
                "name": row.name,
                "scope": row.path if row.phase == "内部" else row.participants,
                "status": row.status,
            }
            for row in active_rows
        ],
        "internal": {
            "covered": internal_covered,
            "total": len(internals),
            "percent": round(internal_covered / len(internals) * 100, 1)
            if internals else 0.0,
        },
        "business": {
            "covered": business_covered,
            "total": len(business),
            "percent": round(business_covered / len(business) * 100, 1)
            if business else 0.0,
        },
        "unknown": {
            "internal": unknown_internal,
            "business": unknown_business,
            "total": unknown_internal + unknown_business,
        },
        "overall": {
            "covered": covered,
            "total": total,
            "percent": round(covered / total * 100, 1) if total else 0.0,
        },
        "structure_valid": not errors,
        "errors": list(errors or []),
    }
