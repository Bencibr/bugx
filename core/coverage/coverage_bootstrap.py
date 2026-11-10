#!/usr/bin/env python3
"""受控创建 BugX 生命周期覆盖清单。

覆盖清单是启动门禁的输入，不是宿主发现器的输出。这个模块只负责一次性
的引导创建，并把写入边界与 Supervisor MCP、launch_bugx 共用，避免两个入口
出现不同的安全判定。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import core.bugx_paths as bugx_paths
from core.coverage import module_coverage
from core.retrieval import symbol_index
import core.secure_atomic as secure_atomic


# DES-02：清单发现元数据的读取原语属于「清单格式」的归属方 module_coverage，
# 已下沉到那里；本模块按需再导出，保持 coverage_bootstrap.<name> 对外可用。
from core.coverage.module_coverage import (  # noqa: F401  (re-export)
    DISCOVERY_METADATA_END,
    DISCOVERY_METADATA_START,
    bounded_errors,
    build_discovery_metadata,
    manifest_discovery_metadata,
)


MANIFEST_NAME = "module-coverage.md"
INHERIT_NOTE_MARK = "inherited_from="
_TABLE_HEADER = (
    "| # | 阶段 | 父模块 | 模块 | 路径/范围 | 参与单元 | 最小性 | 难度 | 命中 | "
    "主工具 | 负责任务 | 依赖 | 状态 | 发现数 | 证据/测试 | 备注 |\n"
    "|---|------|--------|------|-----------|----------|--------|------|------|"
    "--------|----------|------|------|--------|-----------|------|\n"
)


class CoverageBootstrapError(RuntimeError):
    """覆盖清单不能由当前受控引导安全创建。"""

    def __init__(self, message: str, *, code: str = "COVERAGE_BOOTSTRAP_FAILED") -> None:
        super().__init__(message)
        self.code = code


def _is_partial_parse_failure(exc: BaseException) -> bool:
    """Only parser-partial failures may use the file-level fallback.

    Enumeration, permission, and root-layout failures must remain fail-closed;
    silently turning those into a smaller file list would create false coverage.
    """
    detail = str(exc)
    return any(marker in detail for marker in (
        "源码解析不完整", "Tree-sitter", "语法树包含 ERROR", "语法解析",
    ))


def manifest_path(root: Path) -> Path:
    """Return the active lifecycle manifest and validate the project layout."""
    return bugx_paths.require(root).state_root / MANIFEST_NAME


def _read_json(path: Path) -> dict[str, object] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _plan_hash_for_manifest(manifest: Path) -> str | None:
    try:
        if manifest.is_symlink() or not manifest.is_file():
            return None
        return hashlib.sha256(manifest.read_bytes()).hexdigest()
    except OSError:
        return None


def _debt_from_rows(rows: list) -> dict[str, object]:
    """按 DOC-011 §5.2/§5.4 分类覆盖债务：单元级 vs 已终态 vs 阻塞。"""
    by_status: dict[str, int] = {}
    for row in rows:
        status = getattr(row, "status", "") or ""
        by_status[status] = by_status.get(status, 0) + 1
    total = len(rows)
    covered = by_status.get("已覆盖", 0)
    # 中文清单：未覆盖/挖掘中/挂起 均为单元级债务；已覆盖为终态
    unit_debt = total - covered
    return {
        "total": total,
        "covered": covered,
        "unit_debt": unit_debt,
        "by_status": by_status,
        "has_unit_debt": unit_debt > 0,
        "has_global_block": False,
    }


def inspect(root: Path) -> dict[str, object]:
    """Describe whether the current lifecycle needs an explicit bootstrap."""
    root = root.expanduser().resolve()
    scope = bugx_paths.require(root)
    manifest = scope.state_root / MANIFEST_NAME
    if manifest.is_symlink():
        return {
            "ready": False,
            "needs_bootstrap": False,
            "blocked": True,
            "code": "COVERAGE_MANIFEST_SYMLINK",
            "path": str(manifest),
            "detail": f"拒绝使用符号链接覆盖清单: {manifest}",
        }

    rows: list[dict[str, object]] = []
    errors: list[str] = []
    if manifest.is_file():
        try:
            rows, errors = module_coverage.parse_manifest(manifest)
        except (OSError, UnicodeError, ValueError) as exc:
            errors = [str(exc)]
    if rows and not errors:
        # 对账检查：当前清单中的内部 scope 集合 vs 当前工作区实际发现的 units
        manifest_internal_scopes = {
            getattr(r, "path", "").strip().strip("`").rstrip("/")
            for r in rows if getattr(r, "phase", "") == "内部"
        }
        manifest_internal_scopes = {s for s in manifest_internal_scopes if s and s not in {"-", "待定", "N/A"}}

        discovery_meta = manifest_discovery_metadata(manifest)
        use_symbols = discovery_meta.get("discovery_mode") not in {"files", "files-only"}
        try:
            current_units = module_coverage.discovery_units(root, symbols=use_symbols, strict=False)
            current_errors = [u for u in current_units if u.get("error")]
            if current_errors or not current_units:
                # 扫描报错或无单元（避免误把扫描异常当成幽灵行）
                current_scopes = None
            else:
                current_scopes = {str(u["scope"]).strip().strip("`").rstrip("/") for u in current_units}
        except Exception:
            current_scopes = None

        try:
            current = bugx_paths.read_current(root)
            polling_id = current.get("polling_id")
            lifecycle_id = current.get("lifecycle_id")
        except Exception:
            polling_id = lifecycle_id = None

        if current_scopes is not None and manifest_internal_scopes != current_scopes:
            added = sorted(current_scopes - manifest_internal_scopes)
            removed = sorted(manifest_internal_scopes - current_scopes)
            return {
                "ready": False,
                "needs_bootstrap": False,
                "needs_reconcile": True,
                "blocked": False,
                "code": "COVERAGE_MANIFEST_DRIFTED",
                "path": str(manifest),
                "units": len(rows),
                "debt": _debt_from_rows(rows),
                "plan_hash": _plan_hash_for_manifest(manifest),
                "drift_details": {
                    "added": added,
                    "removed": removed,
                },
                "detail": f"覆盖清单与工作区代码发现漂移：新增 {len(added)} 项，失效幽灵行 {len(removed)} 项，需执行增量对账",
                "session_id": polling_id,
                "lifecycle_id": lifecycle_id,
                **discovery_meta,
            }

        return {
            "ready": True,
            "needs_bootstrap": False,
            "blocked": False,
            "path": str(manifest),
            "units": len(rows),
            "debt": _debt_from_rows(rows),
            "plan_hash": _plan_hash_for_manifest(manifest),
            "session_id": polling_id,
            "lifecycle_id": lifecycle_id,
            **discovery_meta,
        }

    runtime_exists = scope.runtime.exists() or scope.runtime.is_symlink()
    runtime = _read_json(scope.runtime)
    if runtime_exists and runtime is None:
        return {
            "ready": False,
            "needs_bootstrap": False,
            "blocked": True,
            "code": "COVERAGE_BOOTSTRAP_RUNTIME_INVALID",
            "path": str(manifest),
            "detail": f"运行计时文件损坏或不安全，拒绝先写覆盖清单: {scope.runtime}",
        }
    runtime = runtime or {}
    active = bool(scope.supervisor.is_file() or runtime.get("state") in {"running", "paused"})
    detail = "覆盖清单缺失或没有可调度单元"
    if errors:
        detail = "覆盖清单无法解析：" + "; ".join(str(item) for item in errors[:3])
    if active:
        return {
            "ready": False,
            "needs_bootstrap": False,
            "blocked": True,
            "code": "COVERAGE_BOOTSTRAP_ACTIVE_LIFECYCLE",
            "path": str(manifest),
            "detail": detail + "；当前生命周期已有运行锚点，拒绝自动重建",
            "debt": _debt_from_rows(rows) if rows else {"total": 0, "covered": 0, "unit_debt": 0, "by_status": {}, "has_unit_debt": False, "has_global_block": False},
        }
    # 覆盖债务分类：即使需要引导，也返回待处理债务明细，便于 Round 计划
    debt = _debt_from_rows(rows) if rows else {"total": 0, "covered": 0, "unit_debt": 0, "by_status": {}, "has_unit_debt": False, "has_global_block": False}
    return {
        "ready": False,
        "needs_bootstrap": True,
        "blocked": False,
        "path": str(manifest),
        "detail": detail,
        "debt": debt,
    }


def _previous_manifest(manifest: Path) -> Path | None:
    """Find the newest valid sibling lifecycle manifest, if any."""
    lifecycles = manifest.parent.parent.parent
    if not lifecycles.is_dir() or lifecycles.is_symlink():
        return None
    current = manifest.parent.parent.name

    def number(path: Path) -> int:
        try:
            return int(path.name.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    for candidate in sorted(
        (item for item in lifecycles.iterdir() if item.is_dir() and not item.is_symlink()),
        key=number,
        reverse=True,
    ):
        if candidate.name == current:
            continue
        source = candidate / "state" / MANIFEST_NAME
        if source.is_symlink() or not source.is_file():
            continue
        try:
            rows, errors = module_coverage.parse_manifest(source)
        except (OSError, UnicodeError, ValueError):
            continue
        if rows and not errors:
            return source
    return None


def _find_project_root_from_manifest(manifest: Path) -> Path:
    p = manifest.resolve()
    while p != p.parent:
        if p.name == ".bugx":
            return p.parent
        p = p.parent
    return Path.cwd().resolve()


def _inheritable_unit_id(row: object) -> str:
    """Return the stable cross-Session identity of an internal unit.

    单元编号（#）每个 Session 重新分配，不能作为继承键；跨 Session 稳定的
    只有内部单元的 scope 字符串（``path`` 或 ``path::symbol``）。scope 缺失
    （业务链路行等）或为占位值时返回空串，绝不猜测映射。
    """
    unit_id = getattr(row, "path", "") or ""
    unit_id = unit_id.strip().strip("`").rstrip("/")
    if not unit_id or unit_id in {"-", "待定", "N/A"}:
        return ""
    return unit_id


def historical_unit_verdicts(
    root: Path, *, exclude_polling: str | None = None,
) -> dict[str, dict[str, object]]:
    """Collect the newest per-unit verdict from previous Sessions' manifests.

    只读扫描 ``.bugx/pollings/*/lifecycles/*/state/<MANIFEST_NAME>``（默认排除
    当前 polling），按 polling/lifecycle 编号升序遍历、后写覆盖先写，因此每个
    内部单元 id 保留的是「最近一次判定」（含后来被增量重扫重置回未覆盖的）。
    任何解析失败、符号链接或布局异常的历史清单都被跳过，绝不让历史扫描阻断
    引导创建。
    """
    verdicts: dict[str, dict[str, object]] = {}
    try:
        pollings = bugx_paths.bugx_root(root) / "pollings"
        if not pollings.is_dir() or pollings.is_symlink():
            return verdicts

        def number(name: str) -> int:
            try:
                return int(name.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                return 0

        for polling in sorted(
            (item for item in pollings.iterdir()
             if item.is_dir() and not item.is_symlink()
             and bugx_paths.POLLING_ID_RE.fullmatch(item.name)),
            key=lambda item: number(item.name),
        ):
            if exclude_polling and polling.name == exclude_polling:
                continue
            lifecycles = polling / "lifecycles"
            if not lifecycles.is_dir() or lifecycles.is_symlink():
                continue
            for lifecycle in sorted(
                (item for item in lifecycles.iterdir()
                 if item.is_dir() and not item.is_symlink()
                 and bugx_paths.LIFECYCLE_ID_RE.fullmatch(item.name)),
                key=lambda item: number(item.name),
            ):
                source = lifecycle / "state" / MANIFEST_NAME
                if source.is_symlink() or not source.is_file():
                    continue
                try:
                    rows, errors = module_coverage.parse_manifest(source)
                except (OSError, UnicodeError, ValueError):
                    continue
                if errors:
                    continue
                for row in rows:
                    if getattr(row, "phase", "") != "内部":
                        continue
                    unit_id = _inheritable_unit_id(row)
                    if not unit_id:
                        continue
                    verdicts[unit_id] = {
                        "polling_id": polling.name,
                        "lifecycle_id": lifecycle.name,
                        "status": getattr(row, "status", ""),
                        "row": row,
                    }
    except OSError:
        # 历史扫描是纯增益：读不到历史就全量 pending，不影响引导安全。
        return verdicts
    return verdicts


def apply_inherited_coverage(
    discovered: list[str], verdicts: dict[str, dict[str, object]],
) -> tuple[list[str], dict[str, object]]:
    """Flip freshly discovered units whose latest historical verdict is covered.

    继承只影响状态位：``状态→已覆盖``，``证据/测试``与``发现数``保留原
    lifecycle 的可追溯值（证据仍属原 lifecycle，可通过 ``inherited_from``
    备注追溯），其余计划字段（编号/难度/工具/内容哈希）全部来自本次发现。
    最近一次判定不是 covered（未覆盖/挖掘中/挂起/未知）的单元保持 pending。
    """
    summary: dict[str, object] = {"enabled": True, "applied": 0, "sources": {}}
    if not verdicts:
        return list(discovered), summary
    sources: dict[str, int] = {}
    applied = 0
    rendered: list[str] = []
    for line in discovered:
        stripped = line.lstrip()
        if not stripped.startswith("|") or stripped.startswith(("| #", "|---")):
            rendered.append(line)
            continue
        row = module_coverage.row_from_line(line)
        if getattr(row, "phase", "") != "内部" or row.status == "已覆盖":
            rendered.append(line)
            continue
        record = verdicts.get(_inheritable_unit_id(row))
        if not record or record.get("status") != "已覆盖":
            rendered.append(line)
            continue
        previous = record.get("row")
        source_polling = str(record.get("polling_id") or "unknown")
        source_lifecycle = str(record.get("lifecycle_id") or "unknown")
        row = replace(
            row,
            status="已覆盖",
            findings=str(getattr(previous, "findings", "0") or "0"),
            evidence=str(getattr(previous, "evidence", "待补") or "待补"),
            note=(
                f"{row.note}；跨Session继承覆盖: "
                f"{INHERIT_NOTE_MARK}{source_polling}/{source_lifecycle}"
            )[:500],
        )
        rendered.append(module_coverage.row_text(row))
        applied += 1
        sources[source_polling] = sources.get(source_polling, 0) + 1
    summary.update({"applied": applied, "sources": sources})
    return rendered, summary


def write_manifest(
    manifest: Path,
    rows: list[str],
    *,
    discovery_metadata: dict[str, object] | None = None,
) -> None:
    if manifest.is_symlink():
        raise CoverageBootstrapError(
            f"拒绝写入符号链接覆盖清单: {manifest}", code="COVERAGE_MANIFEST_SYMLINK"
        )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    final_rows = list(rows)
    if not any("| 业务 |" in r for r in final_rows):
        try:
            parsed_internals = [
                module_coverage.row_from_line(line)
                for line in final_rows
                if line.lstrip().startswith("|") and not line.lstrip().startswith("| #") and not line.lstrip().startswith("|---")
            ]
            if parsed_internals:
                root_candidate = _find_project_root_from_manifest(manifest)
                business_lines = module_coverage.business_discovery_rows(root_candidate, parsed_internals)
                if business_lines:
                    final_rows.extend(business_lines)
        except Exception as exc:
            import sys
            print(f"[bootstrap] business_discovery error: {exc}", file=sys.stderr)
    metadata = discovery_metadata or build_discovery_metadata(
        status="complete", mode="symbols",
    )
    metadata_payload = {
        "status": metadata.get("status", "unknown"),
        "discovery_mode": metadata.get("discovery_mode", "unknown"),
        "degraded": bool(metadata.get("degraded", False)),
        "partial": bool(metadata.get("partial", False)),
        "coverage_confidence": metadata.get("coverage_confidence", "unknown"),
        "discovery_errors": bounded_errors(
            metadata.get("discovery_errors")  # type: ignore[arg-type]
            if isinstance(metadata.get("discovery_errors"), list)
            else None
        ),
    }
    content = (
        "# 最小单元与业务链路覆盖清单（module-coverage）\n\n"
        "## 覆盖发现状态\n\n"
        f"{DISCOVERY_METADATA_START}\n"
        f"{json.dumps(metadata_payload, ensure_ascii=False, indent=2)}\n"
        f"{DISCOVERY_METADATA_END}\n\n"
        f"{module_coverage.SYMBOL_INDEX_MARKER}\n"
        f"{module_coverage.PROJECT_GRAPH_MARKER}\n\n"
        "## 覆盖清单\n\n"
        + _TABLE_HEADER
        + "\n".join(final_rows)
        + "\n"
    )
    temporary = manifest.with_name(f".{manifest.name}.bootstrap.tmp")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    try:
        temporary.write_text(content, encoding="utf-8")
        parsed_rows, parse_errors = module_coverage.parse_manifest(temporary)
        if parse_errors or not parsed_rows:
            raise CoverageBootstrapError(
                "覆盖清单候选内容无法安全复核："
                + "; ".join(parse_errors or ["没有有效最小单元"]),
                code="COVERAGE_DISCOVERY_FAILED",
            )
        # Validate through a temporary regular file, then perform the final
        # replacement with the shared fail-closed writer so a swapped parent
        # or target symlink cannot redirect the manifest.
        secure_atomic.atomic_write_text(manifest, content, mode=0o600, parent_mode=0o700)
    finally:
        # 临时文件只是**复核载体**，不是产物：成功路径同样必须清理，
        # 否则会与正式清单并存（且其默认权限比 0o600 更宽）。
        try:
            temporary.unlink()
        except OSError:
            pass


def write_inventory_diff(manifest: Path, changes: dict) -> None:
    """Persist the rescan result beside the lifecycle manifest."""
    target = manifest.with_name("inventory-diff.json")
    payload = {
        "schema_version": 1,
        "changes": changes,
    }
    secure_atomic.atomic_write_json(target, payload, mode=0o600, parent_mode=0o700)


def bootstrap(
    root: Path, *, confirmed: bool = False, inherit_coverage: bool = True,
    reconcile: bool = False,
) -> dict[str, object]:
    """Create the manifest after an explicit host/user confirmation.

    Existing valid manifests are returned unchanged unless reconcile is requested.
    An active Supervisor is never rewritten: changing its unit set would invalidate
    claims and the external baseline.

    ``inherit_coverage``（默认开启）在全新 Session 重建清单时继承历史
    「已覆盖」判定（见 ``historical_unit_verdicts``）；显式传 False（CLI
    ``--fresh-coverage``）可忽略历史结论全量重审。
    """
    if not confirmed:
        raise CoverageBootstrapError(
            "覆盖清单引导创建需要宿主先取得用户的‘运行引导创建’确认",
            code="COVERAGE_BOOTSTRAP_CONFIRMATION_REQUIRED",
        )
    root = root.expanduser().resolve()
    state = inspect(root)
    if state.get("ready") and not reconcile:
        return {**state, "created": False, "reused": True, "next_action": "RETRY_START"}
    if state.get("blocked"):
        raise CoverageBootstrapError(
            str(state.get("detail") or "覆盖清单引导被阻断"),
            code=str(state.get("code") or "COVERAGE_BOOTSTRAP_BLOCKED"),
        )

    manifest = Path(str(state["path"]))
    # 增量对账自愈分支：处理代码发现漂移或幽灵行清理
    if (reconcile or state.get("needs_reconcile")) and manifest.is_file():
        current_rows, errors = module_coverage.parse_manifest(manifest)
        if not errors and current_rows:
            discovery_meta = manifest_discovery_metadata(manifest)
            use_symbols = discovery_meta.get("discovery_mode") not in {"files", "files-only"}
            try:
                discovered, changes = module_coverage.reconcile_discovery_rows(
                    current_rows, root, symbols=use_symbols,
                )
            except symbol_index.SourceDiscoveryError as exc:
                raise CoverageBootstrapError(
                    f"覆盖清单增量重扫对账失败: {exc}",
                    code="COVERAGE_DISCOVERY_FAILED",
                ) from exc
            metadata = build_discovery_metadata(
                status="partial" if not use_symbols else "complete",
                mode="files" if not use_symbols else "symbols",
                confidence="low" if not use_symbols else "high",
            )
            write_manifest(manifest, discovered, discovery_metadata=metadata)
            write_inventory_diff(manifest, changes)
            new_rows, parse_errors = module_coverage.parse_manifest(manifest)
            if parse_errors or not new_rows:
                raise CoverageBootstrapError(
                    "增量重扫生成的覆盖清单无法复核",
                    code="COVERAGE_DISCOVERY_FAILED",
                )
            return {
                "ready": True,
                "created": False,
                "reconciled": True,
                "source": "reconciled_manifest",
                "path": str(manifest),
                "units": len(new_rows),
                "debt": _debt_from_rows(new_rows),
                "plan_hash": _plan_hash_for_manifest(manifest),
                "inventory_diff": changes,
                "next_action": "RETRY_START",
                **metadata,
            }
    # 当前 polling（manifest 位于 pollings/<polling_id>/lifecycles/<id>/state/），
    # 继承扫描时排除自身，只认更早 Session 的判定。
    current_polling = ""
    if bugx_paths.POLLING_ID_RE.fullmatch(manifest.parent.parent.parent.parent.name):
        current_polling = manifest.parent.parent.parent.parent.name
    inheritance: dict[str, object] = {
        "enabled": bool(inherit_coverage), "applied": 0, "sources": {},
    }
    previous = _previous_manifest(manifest)
    if previous is not None:
        previous_rows, errors = module_coverage.parse_manifest(previous)
        if errors or not previous_rows:
            raise CoverageBootstrapError(
                "上一生命周期覆盖清单无法安全沿用",
                code="COVERAGE_BOOTSTRAP_PREVIOUS_INVALID",
            )
        try:
            discovered, changes = module_coverage.reconcile_discovery_rows(
                previous_rows, root,
                symbols=manifest_discovery_metadata(previous).get("discovery_mode")
                not in {"files", "files-only"},
            )
        except symbol_index.SourceDiscoveryError as exc:
            raise CoverageBootstrapError(
                f"新生命周期重扫失败，拒绝沿用旧覆盖结论: {exc}",
                code="COVERAGE_DISCOVERY_FAILED",
            ) from exc
        previous_mode = manifest_discovery_metadata(previous).get("discovery_mode")
        metadata = build_discovery_metadata(
            status="partial" if previous_mode in {"files", "files-only"} else "complete",
            mode="files" if previous_mode in {"files", "files-only"} else "symbols",
            confidence="low" if previous_mode in {"files", "files-only"} else "high",
        )
        write_manifest(manifest, discovered, discovery_metadata=metadata)
        write_inventory_diff(manifest, changes)
        rows, parse_errors = module_coverage.parse_manifest(manifest)
        if parse_errors or not rows:
            raise CoverageBootstrapError(
                "增量重扫生成的覆盖清单无法复核",
                code="COVERAGE_DISCOVERY_FAILED",
            )
        return {
            "ready": True,
            "created": True,
            "source": "previous_lifecycle",
            "path": str(manifest),
            "units": len(rows),
            "debt": _debt_from_rows(rows),
            "plan_hash": _plan_hash_for_manifest(manifest),
            "inventory_diff": changes,
            "next_action": "RETRY_START",
            **metadata,
        }

    try:
        discovered = module_coverage.discovery_rows(root)
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        strict_detail = str(exc)
        if not _is_partial_parse_failure(exc):
            raise CoverageBootstrapError(
                f"覆盖清单自动发现失败: {strict_detail}",
                code="COVERAGE_DISCOVERY_FAILED",
            ) from exc
        try:
            # This path only enumerates real, in-root source files.  It does
            # not infer symbols from a damaged syntax tree and therefore keeps
            # the minimum scheduling unit conservative.
            degraded_rows = module_coverage.discovery_rows(root, symbols=False)
        except (OSError, UnicodeError, ValueError, RuntimeError) as fallback_exc:
            raise CoverageBootstrapError(
                "严格符号发现失败，文件级安全回退也失败："
                f"严格错误={strict_detail}; 回退错误={fallback_exc}",
                code="COVERAGE_DISCOVERY_FAILED",
            ) from fallback_exc
        if not degraded_rows:
            raise CoverageBootstrapError(
                "严格符号发现失败，且文件级安全回退没有发现任何可安全列出的源码："
                f"{strict_detail}",
                code="COVERAGE_DISCOVERY_EMPTY",
            ) from exc
        if inherit_coverage:
            # 文件级回退同样继承历史 covered：回退只影响发现粒度，不否定
            # 更早 lifecycle 的审计结论（scope 精确一致才继承）。
            degraded_rows, inheritance = apply_inherited_coverage(
                degraded_rows,
                historical_unit_verdicts(root, exclude_polling=current_polling),
            )
        discovery = build_discovery_metadata(
            status="partial",
            mode="files",
            errors=[strict_detail],
            confidence="low",
        )
        write_manifest(manifest, degraded_rows, discovery_metadata=discovery)
        rows, parse_errors = module_coverage.parse_manifest(manifest)
        if parse_errors or not rows:
            raise CoverageBootstrapError(
                "文件级安全回退生成的覆盖清单无法复核："
                + "; ".join(parse_errors or ["没有有效最小单元"]),
                code="COVERAGE_DISCOVERY_FAILED",
            )
        return {
            "ready": True,
            "created": True,
            "source": "module_coverage.discovery_rows(files_only)",
            "path": str(manifest),
            "units": len(degraded_rows),
            "debt": _debt_from_rows(rows),
            "plan_hash": _plan_hash_for_manifest(manifest),
            "inherited_coverage": inheritance,
            "next_action": "RETRY_START",
            **discovery,
        }
    if not discovered:
        raise CoverageBootstrapError(
            "项目没有可发现的源码最小单元，无法建立 Supervisor 队列",
            code="COVERAGE_DISCOVERY_EMPTY",
        )
    if inherit_coverage:
        # 全新 Session：把历史「已覆盖」审计结论继承到新清单（R6 复盘结构性
        # 候选 #1），避免每轮重复重审同批入口文件；--fresh-coverage 可关闭。
        discovered, inheritance = apply_inherited_coverage(
            discovered,
            historical_unit_verdicts(root, exclude_polling=current_polling),
        )
    discovery = build_discovery_metadata(status="complete", mode="symbols")
    write_manifest(manifest, discovered, discovery_metadata=discovery)
    try:
        rows, _ = module_coverage.parse_manifest(manifest)
        if not any(r.phase == "业务" for r in rows):
            business_lines = module_coverage.business_discovery_rows(root, rows)
            if business_lines:
                with open(manifest, "a", encoding="utf-8") as f:
                    for bl in business_lines:
                        f.write(bl + "\n")
    except Exception:
        pass
    rows, _ = module_coverage.parse_manifest(manifest)
    return {
        "ready": True,
        "created": True,
        "source": "module_coverage.discovery_rows",
        "path": str(manifest),
        "units": len(discovered),
        "debt": _debt_from_rows(rows) if rows else {"total": len(discovered), "covered": 0, "unit_debt": len(discovered), "by_status": {"未覆盖": len(discovered)}, "has_unit_debt": True, "has_global_block": False},
        "plan_hash": _plan_hash_for_manifest(manifest),
        "inherited_coverage": inheritance,
        "next_action": "RETRY_START",
        **discovery,
    }
