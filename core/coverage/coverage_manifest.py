#!/usr/bin/env python3
"""覆盖清单的**解析与证据探针**原语（DES-07 一期）。

本模块由 ``module_coverage.py`` 拆分而来，承载三个**叶子职责**（强连通分量分析
确认它们不依赖其余任何簇）：

* A 元数据 —— 清单发现元数据的读取原语（``bounded_errors`` /
  ``build_discovery_metadata`` / ``manifest_discovery_metadata``）
* B 清单解析 —— ``ModuleRow`` / ``parse_manifest`` / ``resolve_in_root`` 等
* C 证据探针 —— ``EvidenceProbe`` / ``evidence_is_real`` / ``evidence_is_real_uncached``

以及它们依赖的模块级常量（``MANIFEST`` / ``SOURCE_EXTS`` / ``EVIDENCE_EXTS`` /
``EVIDENCE_PATH_RE`` / ``DISCOVERY_METADATA_*``）。

``module_coverage`` 会**再导出**这里的全部公开名字，因此既有调用方
（含 ``coverage_bootstrap``、``check`` / ``status`` / ``suggest_hits``）无需改动。
依赖方向：``module_coverage`` → ``coverage_manifest``（单向，无环）。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from core.retrieval import symbol_index

# DES-02：以下符号原在 coverage_bootstrap；清单格式与发现元数据的解析
# 属于本模块的职责，下沉后 coverage_bootstrap 单向依赖本模块，双向环消除。
DISCOVERY_METADATA_START = "<!-- bugx-coverage-bootstrap-metadata -->"
DISCOVERY_METADATA_END = "<!-- /bugx-coverage-bootstrap-metadata -->"


def bounded_errors(errors: list[str] | tuple[str, ...] | None) -> list[str]:
    """Return bounded, single-line discovery diagnostics for result/manifest use."""
    result: list[str] = []
    for error in errors or ():
        detail = " ".join(str(error).split())
        if detail and detail not in result:
            result.append(detail[:1000])
        if len(result) >= 10:
            break
    return result



def build_discovery_metadata(
    *, status: str, mode: str, errors: list[str] | tuple[str, ...] | None = None,
    confidence: str | None = None,
) -> dict[str, object]:
    """Build the explicit discovery/coverage trust contract.

    A file-level fallback is usable for scheduling, but it is never reported as
    an equivalent symbol-complete discovery.  The metadata is returned to the
    host and persisted beside the rows so later inspection cannot lose that
    distinction.
    """
    diagnostics = bounded_errors(errors)
    partial = status in {"partial", "degraded"}
    return {
        "status": status,
        "discovery_status": status,
        "discovery_mode": mode,
        "degraded": partial,
        "partial": partial,
        "coverage_confidence": confidence or ("low" if partial else "high"),
        "discovery_errors": diagnostics,
        "error_details": diagnostics,
    }



def manifest_discovery_metadata(manifest: Path) -> dict[str, object]:
    """Read persisted discovery metadata without trusting it as coverage proof."""
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return build_discovery_metadata(
            status="unknown", mode="unreadable", confidence="unknown",
            errors=[f"无法读取覆盖发现元数据: {manifest}"],
        )
    if DISCOVERY_METADATA_START not in text:
        # Manifests created before this metadata existed retain their previous
        # readiness semantics, but their confidence is intentionally unknown.
        return build_discovery_metadata(
            status="complete", mode="legacy", confidence="unknown",
        )
    start = text.index(DISCOVERY_METADATA_START) + len(DISCOVERY_METADATA_START)
    end = text.find(DISCOVERY_METADATA_END, start)
    if end < 0:
        return build_discovery_metadata(
            status="unknown", mode="metadata-invalid", confidence="unknown",
            errors=["覆盖发现元数据缺少结束标记，可信度未知"],
        )
    try:
        payload = json.loads(text[start:end].strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return build_discovery_metadata(
            status="unknown", mode="metadata-invalid", confidence="unknown",
            errors=["覆盖发现元数据不是有效 JSON，可信度未知"],
        )
    if not isinstance(payload, dict):
        return build_discovery_metadata(
            status="unknown", mode="metadata-invalid", confidence="unknown",
            errors=["覆盖发现元数据结构无效，可信度未知"],
        )
    status = str(payload.get("status") or "unknown")
    mode = str(payload.get("discovery_mode") or payload.get("mode") or "unknown")
    confidence = str(payload.get("coverage_confidence") or "unknown")
    errors = payload.get("discovery_errors") or payload.get("errors") or []
    if not isinstance(errors, list):
        errors = [str(errors)]
    return build_discovery_metadata(
        status=status, mode=mode, confidence=confidence,
        errors=[str(item) for item in errors],
    )


try:
    import core.bugx_paths as bugx_paths
except ImportError:  # pragma: no cover - standalone source copy fallback
    bugx_paths = None  # type: ignore[assignment]

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent.parent if AGENT_DIR.parent.name == ".bugx" else AGENT_DIR.parent
MANIFEST = AGENT_DIR / "module-coverage.md"


SOURCE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".go",
    ".rs", ".swift", ".c", ".h", ".cc", ".cpp", ".cxx", ".cs", ".rb",
    ".php", ".vue", ".svelte",
}
EVIDENCE_EXTS = SOURCE_EXTS | symbol_index.AUDIT_ASSET_EXTS | {
    ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".md",
    ".txt", ".log", ".png", ".jpg", ".jpeg", ".webp",
}
EVIDENCE_PATH_RE = re.compile(
    r"`([^`]+)`|([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+(?:\:\:\w+|\:\d+)?)"
)


def default_manifest_for_root(root: Path) -> Path:
    """Return the active project manifest, never the shared engine template."""
    root = root.expanduser().resolve()
    if bugx_paths is not None:
        try:
            return bugx_paths.require(root).state_root / "module-coverage.md"
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass
    # Keep a project-local fallback for standalone/uninitialised workspaces.
    return root / ".bugx" / "module-coverage.md"


@dataclass(frozen=True)
class ModuleRow:
    number: str
    phase: str
    parent: str
    name: str
    path: str
    participants: str
    minimality: str
    difficulty: str
    hit: str
    tool: str
    owner: str
    deps: str
    status: str
    findings: str
    evidence: str
    note: str


def parse_cells(line: str) -> list[str]:
    return [x.strip() for x in line.strip().strip("|").split("|")]


def parse_manifest(path: Path = MANIFEST) -> tuple[list[ModuleRow], list[str]]:
    """解析模块 Markdown 表，返回 rows + 结构错误。"""
    if not path.is_file():
        return [], [f"缺少模块清单: {path}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        # 与其他结构错误同形态返回，供门禁/状态页统一呈现，绝不抛出。
        return [], [f"模块清单无法读取（损坏或非 UTF-8）: {path}: {exc}"]
    header_index = next(
        (i for i, line in enumerate(lines)
         if "路径/范围" in line and "状态" in line),
        None,
    )
    if header_index is None:
        return [], ["模块清单缺少包含『路径/范围』和『状态』的表头"]
    header = parse_cells(lines[header_index])
    errors: list[str] = []
    required = {
        "#", "阶段", "父模块", "模块", "路径/范围", "参与单元", "最小性",
        "状态", "发现数", "主工具", "负责任务", "证据/测试",
    }
    missing = required - set(header)
    if missing:
        errors.append(f"模块清单表头缺少字段: {sorted(missing)}")
    index = {name: i for i, name in enumerate(header)}
    rows: list[ModuleRow] = []
    for line_no, line in enumerate(lines[header_index + 2:], header_index + 3):
        if not line.strip().startswith("|") or "---" in line:
            continue
        cells = parse_cells(line)
        if len(cells) < len(header):
            errors.append(f"第 {line_no} 行列数不足: {len(cells)}/{len(header)}")
            continue
        def get(key: str) -> str:
            return cells[index[key]] if key in index else ""
        row = ModuleRow(
            number=get("#"), phase=get("阶段"), parent=get("父模块"),
            name=get("模块"), path=get("路径/范围"),
            participants=get("参与单元"), minimality=get("最小性"),
            difficulty=get("难度"), hit=get("命中"), tool=get("主工具"),
            owner=get("负责任务"), deps=get("依赖"), status=get("状态"), findings=get("发现数"),
            evidence=get("证据/测试"), note=get("备注"),
        )
        if "示例" in row.name or "示例" in row.path:
            continue
        rows.append(row)
    return rows, errors


def resolve_in_root(
    path: str, root: Path, resolved_root: Path | None = None,
) -> Path | None:
    """解析仓库内路径；拒绝绝对路径/符号链接逃出审计根目录。

    ``resolved_root`` 允许调用方复用已经 ``resolve(strict=True)`` 过的审计根，
    避免同一轮校验里对每个证据路径重复解析根目录；传入 ``None`` 时与旧实现
    完全一致（现算现用）。越界与符号链接检查始终逐个路径执行，不受缓存影响。
    """
    clean = path.strip().strip("`").rstrip("/")
    if "::" in clean:
        clean = clean.split("::", 1)[0]
    if not clean or clean in {"-", "N/A", "待定"}:
        return None
    p = Path(clean)
    candidate = p if p.is_absolute() else root / p
    try:
        if resolved_root is None:
            resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    return resolved


class EvidenceProbe:
    """单次校验遍历内的证据探测缓存（生命周期 = 一次 ``validate_rows``）。

    覆盖门禁是安全相关判定：证据文件可能在两次校验之间被删除、改写或替换成
    逃逸符号链接，因此这里刻意不使用进程级 ``lru_cache``——那会把上一轮会话
    的文件系统快照当成新结论，可能放行已经失效的证据。本缓存只消除「同一轮
    遍历内对同一路径重复 resolve/stat/read/parse」，所有越界、符号链接、空文件
    与 AST 解析失败判定仍逐个路径照常执行，只是结果在轮内复用。

    缓存项只能由 ``resolve_in_root`` 产生，而后者始终执行
    ``candidate.resolve(strict=True)`` + ``relative_to(resolved_root)``：逃逸根目录
    的路径被缓存为 ``None`` 并永远判为不可用，缓存不可能把「根目录之外」变成
    「根目录之内」。符号链接在解析时即被跟随并由 ``relative_to`` 复核，命中缓存
    不会跳过这两个检查，只是对同一路径不再重复执行。

    只读的 ``stat_info``（is_file/suffix/size）与昂贵的 ``py_ok``（read+parse）
    分开缓存，路径合法性校验不会误触发对每个源码文件的 AST 解析。
    """

    __slots__ = ("root", "_resolved_root", "_root_ready", "_resolve_cache",
                 "_stat_cache", "_py_cache", "_line_cache", "_verdict_cache")

    def __init__(self, root: Path):
        self.root = root
        self._resolved_root: Path | None = None
        self._root_ready = False
        self._resolve_cache: dict[str, Path | None] = {}
        self._stat_cache: dict[Path, dict[str, Any]] = {}
        self._py_cache: dict[Path, bool] = {}
        self._line_cache: dict[Path, int | None] = {}
        self._verdict_cache: dict[str, bool] = {}

    def resolved_root(self) -> Path | None:
        """审计根只解析一次；失败结果同样缓存（与逐次失败语义一致）。"""
        if not self._root_ready:
            try:
                self._resolved_root = self.root.resolve(strict=True)
            except OSError:
                self._resolved_root = None
            self._root_ready = True
        return self._resolved_root

    def resolve(self, path: str) -> Path | None:
        """按归一化路径缓存 ``resolve_in_root`` 结果（含逃逸检查）。"""
        key = path.strip().strip("`").rstrip("/")
        if "::" in key:
            key = key.split("::", 1)[0]
        if key not in self._resolve_cache:
            self._resolve_cache[key] = resolve_in_root(
                path, self.root, self.resolved_root(),
            )
        return self._resolve_cache[key]

    def stat_info(self, path: Path) -> dict[str, Any]:
        """每个路径只 stat 一次；不读取内容、不做 AST 解析。"""
        info = self._stat_cache.get(path)
        if info is None:
            info = {"is_file": False, "suffix": path.suffix.lower(), "size": None}
            try:
                if path.is_file():
                    info["is_file"] = True
                    info["size"] = path.stat().st_size
            except OSError:
                pass
            self._stat_cache[path] = info
        return info

    def py_ok(self, path: Path) -> bool:
        """每个 ``.py`` 证据文件只读取 + ``ast.parse`` 一次。

        与原实现一致：空内容、解析失败或 AST 无实际语句都视为不可用证据。
        """
        cached = self._py_cache.get(path)
        if cached is None:
            try:
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                # 排除完全只有注释且无任何实际代码语句的虚假测试文件
                cached = bool(content) and bool(ast.parse(content).body)
            except Exception:
                cached = False
            self._py_cache[path] = cached
        return cached

    def line_count(self, path: Path) -> int | None:
        """按行号校验用的行数；不可读时返回 ``None``（等价于旧实现的 continue）。"""
        if path not in self._line_cache:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    self._line_cache[path] = sum(1 for _ in handle)
            except OSError:
                self._line_cache[path] = None
        return self._line_cache[path]

    def evidence_is_real(self, evidence: str) -> bool:
        cached = self._verdict_cache.get(evidence)
        if cached is None:
            cached = evidence_is_real_uncached(evidence, self)
            self._verdict_cache[evidence] = cached
        return cached


def evidence_is_real(evidence: str, root: Path,
                     probe: EvidenceProbe | None = None) -> bool:
    """证据必须引用仓库内真实文件，可带 :行号 或 ::test_name。

    ``probe`` 是同一次校验共享的探测缓存；省略时退化为单条证据的临时缓存，
    判定语义与传入共享缓存时逐位一致。
    """
    if probe is None:
        probe = EvidenceProbe(root)
    return probe.evidence_is_real(evidence)


def evidence_is_real_uncached(evidence: str, probe: EvidenceProbe) -> bool:
    """在给定探测缓存上执行原始逐路径判定（无缓存命中时即旧语义）。"""
    for match in EVIDENCE_PATH_RE.finditer(evidence):
        raw = next((group for group in match.groups() if group), "")
        clean = raw.split("::", 1)[0]
        line = None
        line_match = re.search(r":(\d+)$", clean)
        if line_match:
            line = int(line_match.group(1))
            clean = clean[:line_match.start()]
        resolved = probe.resolve(clean)
        if resolved is None:
            continue
        info = probe.stat_info(resolved)
        if not info["is_file"] or info["suffix"] not in EVIDENCE_EXTS:
            continue
        if info["size"] is None or info["size"] == 0:
            continue
        if info["suffix"] == ".py" and not probe.py_ok(resolved):
            continue
        if line is not None:
            line_count = probe.line_count(resolved)
            if line_count is None or not 1 <= line <= line_count:
                continue
        return True
    return False
