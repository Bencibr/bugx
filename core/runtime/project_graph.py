#!/usr/bin/env python3
"""构建源码关系图、自动发现跨模块业务链路，并生成增量审计计划。"""
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
import sys
from pathlib import Path


import argparse
import ast
import json
import re
import subprocess
from collections import defaultdict, deque
from dataclasses import asdict, dataclass

from core.retrieval import symbol_index
from core.retrieval import codebase_memory_bridge

SCHEMA_VERSION = 1
ENTRY_WORDS = {"main", "handler", "handle", "route", "endpoint", "command", "execute"}
SINK_WORDS = {
    "save", "delete", "remove", "charge", "pay", "commit", "publish", "send",
    "emit", "enqueue", "write", "update", "create",
}
SOURCE_EXTENSIONS = (
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".go",
    ".rs", ".swift", ".c", ".h", ".cc", ".cpp", ".cxx", ".cs", ".rb",
    ".php", ".vue", ".svelte",
)


class GraphError(ValueError):
    """关系图或增量计划输入错误。"""


@dataclass(frozen=True)
class Edge:
    type: str
    source: str
    target: str
    confidence: float
    evidence: str


class GraphPassCache:
    """Per-pass cache shared by the graph, unit and inventory steps of one pass.

    ``content`` is a ``symbol_index.SourceContentCache`` so every source file is
    read once across the whole pass, and ``_graphs`` memoises graphs already
    built in this pass so asking for the same root twice does not pay the full
    index + parse cost again.

    Lifetime is one caller-owned pass, never process-global: the audited
    workspace can change between passes (edits, ``git checkout``, generated
    files), so a process-wide cache would serve stale content.  The memo hands
    back the *same* graph object, so callers must treat it as read-only.
    """

    __slots__ = ("content", "_graphs")

    def __init__(self) -> None:
        self.content = symbol_index.SourceContentCache()
        self._graphs: dict[tuple[str, str], dict] = {}

    def graph(self, root: Path, knowledge_graph: str) -> dict | None:
        return self._graphs.get((str(root), knowledge_graph))

    def remember(self, root: Path, knowledge_graph: str, graph: dict) -> None:
        self._graphs[(str(root), knowledge_graph)] = graph


def _module_aliases(path: str) -> set[str]:
    value = Path(path).with_suffix("").as_posix().replace("/", ".")
    aliases = {value}
    if value.endswith(".__init__"):
        aliases.add(value[:-9])
    parts = value.split(".")
    for index in range(1, len(parts)):
        aliases.add(".".join(parts[index:]))
    return {item for item in aliases if item}


def _symbol_id(path: str, qualified: str) -> str:
    return f"symbol:{path}::{qualified}"


def _file_id(path: str) -> str:
    return f"file:{path}"


def _name_from_node(source: str, node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_from_node(source, node.value)
        return f"{base}.{node.attr}" if base else node.attr
    try:
        raw = ast.get_source_segment(source, node) or ""
        # Evidence is later embedded in a Markdown table. Complex calls such
        # as ``{...}.get(...)`` can span lines; keep the evidence one cell and
        # prevent a source pipe from changing the table schema.
        return re.sub(r"\s+", " ", raw).replace("|", "/").strip()[:240]
    except (TypeError, ValueError):
        return ""


class _PythonRelations(ast.NodeVisitor):
    def __init__(self, path: str, source: str, module_paths: dict[str, str],
                 symbols_by_file: dict[str, list[dict]], global_names: dict[str, list[str]]) -> None:
        self.path = path
        self.source = source
        self.module_paths = module_paths
        self.symbols_by_file = symbols_by_file
        self.global_names = global_names
        self.parents: list[str] = []
        self.imported_symbols: dict[str, str] = {}
        self.imported_modules: dict[str, str] = {}
        self.edges: list[Edge] = []

    def _caller(self) -> str:
        if self.parents:
            return _symbol_id(self.path, ".".join(self.parents))
        return _file_id(self.path)

    def _target_symbol(self, path: str, name: str) -> str | None:
        candidates = [
            _symbol_id(path, item["qualified_name"])
            for item in self.symbols_by_file.get(path, [])
            if item["name"] == name
        ]
        return candidates[0] if len(candidates) == 1 else None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target_path = self.module_paths.get(alias.name)
            if not target_path:
                continue
            local = alias.asname or alias.name.split(".")[0]
            self.imported_modules[local] = target_path
            self.edges.append(Edge(
                "imports", _file_id(self.path), _file_id(target_path), 0.95,
                f"{self.path}:{node.lineno} import {alias.name}",
            ))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        target_path = self.module_paths.get(module)
        if target_path:
            self.edges.append(Edge(
                "imports", _file_id(self.path), _file_id(target_path), 0.95,
                f"{self.path}:{node.lineno} from {module}",
            ))
        for alias in node.names:
            local = alias.asname or alias.name
            sub_mod = f"{module}.{alias.name}" if module else alias.name
            sub_target_path = self.module_paths.get(sub_mod)
            if sub_target_path:
                self.imported_modules[local] = sub_target_path
                self.edges.append(Edge(
                    "imports", _file_id(self.path), _file_id(sub_target_path), 0.95,
                    f"{self.path}:{node.lineno} from {module} import {alias.name}",
                ))
            elif target_path:
                target = self._target_symbol(target_path, alias.name)
                if target:
                    self.imported_symbols[local] = target

    def _visit_callable(self, node: ast.AST, name: str) -> None:
        self.parents.append(name)
        self.generic_visit(node)
        self.parents.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_callable(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_callable(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_callable(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        raw_name = _name_from_node(self.source, node.func)
        target: str | None = None
        if isinstance(node.func, ast.Name):
            target = self.imported_symbols.get(node.func.id)
            if target is None:
                target = self._target_symbol(self.path, node.func.id)
            if target is None and len(self.global_names.get(node.func.id, [])) == 1:
                target = self.global_names[node.func.id][0]
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                module_path = self.imported_modules.get(node.func.value.id)
                if module_path:
                    target = self._target_symbol(module_path, node.func.attr)
            if target is None and len(self.global_names.get(node.func.attr, [])) == 1:
                target = self.global_names[node.func.attr][0]
        if target and target != self._caller():
            self.edges.append(Edge(
                "calls", self._caller(), target, 0.9,
                f"{self.path}:{node.lineno} call {raw_name}",
            ))
        self.generic_visit(node)


IMPORT_PATTERNS = (
    re.compile(r"\bfrom\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"\bimport\s+['\"]([^'\"]+)['\"]"),
    re.compile(r"#include\s*[\"<]([^\">]+)[\">]"),
)

# tree-sitter 调用边：非 Python 语言的 calls 关系。置信度固定 0.5——
# 名字解析只有「本文件唯一符号 / 全局唯一符号」两档，比 Python AST 弱。
TS_CALL_CONFIDENCE = 0.5
_CALL_NODE_TYPES = {
    "call_expression", "call", "method_invocation", "method_call",
    "function_call", "call_expr",
}
_MEMBER_CALL_TYPES = {
    "member_expression", "field_access", "scoped_identifier",
    "selector_expression", "attribute",
}
_SIMPLE_NAME_TYPES = {"identifier", "field_identifier", "property_identifier", "constant"}


def _ts_call_name(function_node: object) -> str | None:
    """从调用节点的 function 字段提取被调名；支持直接名与成员访问。"""
    if function_node is None:
        return None
    node_type = function_node.type
    if node_type in _SIMPLE_NAME_TYPES:
        return function_node.text.decode("utf-8", errors="replace")
    if node_type in _MEMBER_CALL_TYPES:
        for field in ("property", "field", "attribute", "name"):
            member = function_node.child_by_field_name(field)
            if member is not None:
                return member.text.decode("utf-8", errors="replace")
    return None


def _tree_sitter_call_edges(path: str, source: str, language: str,
                            symbols_by_file: dict[str, list[dict]],
                            global_names: dict[str, list[str]]) -> list[Edge]:
    """tree-sitter 提取非 Python 语言的 calls 边；后端缺失时静默降级。

    未安装 tree-sitter 时不产生 parse_errors（imports 边仍由
    _generic_import_edges 提供，图保持 complete）；只把统计写进 summary。
    """
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        return []
    try:
        tree = get_parser(language).parse(source.encode("utf-8"))
    except Exception:  # 语言包对个别语法抛出多种运行时异常
        return []
    local_symbols = symbols_by_file.get(path, [])
    edges: list[Edge] = []

    def enclosing_id(parents: list[str]) -> str:
        return _symbol_id(path, ".".join(parents)) if parents else _file_id(path)

    def resolve(name: str) -> str | None:
        candidates = [
            _symbol_id(path, item["qualified_name"])
            for item in local_symbols if item["name"] == name
        ]
        if len(candidates) == 1:
            return candidates[0]
        globals_for_name = global_names.get(name, [])
        if len(globals_for_name) == 1:
            return globals_for_name[0]
        return None

    def walk(node: object, parents: list[str]) -> None:
        kind = symbol_index.TREE_SITTER_NODE_KINDS.get(node.type)
        child_parents = parents
        if kind:
            direct = node.child_by_field_name("name")
            if direct is not None:
                name = direct.text.decode("utf-8", errors="replace")
                child_parents = [*parents, name]
        if node.type in _CALL_NODE_TYPES:
            callee = _ts_call_name(node.child_by_field_name("function"))
            if callee:
                target = resolve(callee)
                caller = enclosing_id(child_parents)
                if target and target != caller:
                    edges.append(Edge(
                        "calls", caller, target, TS_CALL_CONFIDENCE,
                        f"{path} call {callee}",
                    ))
        for child in node.named_children:
            walk(child, child_parents)

    walk(tree.root_node, [])
    return edges


def _generic_import_edges(path: str, source: str,
                          path_lookup: dict[str, str]) -> list[Edge]:
    result: list[Edge] = []
    for line_number, line in enumerate(source.splitlines(), 1):
        for pattern in IMPORT_PATTERNS:
            for match in pattern.finditer(line):
                raw = match.group(1)
                # Relative JS/TS imports are unambiguous. For C/C++/Go and
                # similar languages also try local-looking paths; bare package
                # names remain external dependencies and are not false edges.
                if not (
                    raw.startswith(".")
                    or "/" in raw
                    or Path(raw).suffix.lower() in SOURCE_EXTENSIONS
                ):
                    continue
                candidate = (Path(path).parent / raw).as_posix()
                target = None
                for normalized in _import_path_candidates(candidate):
                    target = path_lookup.get(normalized)
                    if target:
                        break
                if target:
                    result.append(Edge(
                        "imports", _file_id(path), _file_id(target), 0.8,
                        f"{path}:{line_number} import {raw}",
                    ))
    return result


def _import_path_candidates(candidate: str) -> list[str]:
    """Resolve extensionless and explicitly extended relative imports."""
    path = Path(candidate)
    normalized = path.as_posix()
    candidates = [normalized]
    if path.suffix:
        candidates.append(path.with_suffix("").as_posix())
    else:
        candidates.extend(f"{normalized}{extension}" for extension in SOURCE_EXTENSIONS)
    candidates.extend(
        f"{normalized}/index{extension}" for extension in SOURCE_EXTENSIONS
    )
    candidates.extend(
        f"{normalized}/__init__{extension}" for extension in SOURCE_EXTENSIONS
    )
    return list(dict.fromkeys(candidates))


def build_graph(root: Path, knowledge_graph: str = "auto", *,
                cache: GraphPassCache | None = None) -> dict:
    """Build the repository relationship graph.

    ``cache`` is an optional per-pass cache.  When supplied, source files are
    read once and shared with ``symbol_index``, and a second request for the
    same root within the pass returns the graph already built instead of
    re-indexing the whole repository.  Omitting it keeps the original
    build-everything-fresh behaviour.
    """
    root = root.resolve()
    if cache is None:
        cache = GraphPassCache()
    else:
        memoized = cache.graph(root, knowledge_graph)
        if memoized is not None:
            return memoized
    try:
        index = symbol_index.index_repository(root, strict=True, cache=cache.content)
    except symbol_index.SourceDiscoveryError as exc:
        raise GraphError(str(exc)) from exc
    symbols_by_file = {
        item["path"]: item["symbols"] for item in index["files"]
    }
    module_paths: dict[str, str] = {}
    path_lookup: dict[str, str] = {}
    global_names: dict[str, list[str]] = defaultdict(list)
    for item in index["files"]:
        path = item["path"]
        for alias in _module_aliases(path):
            module_paths.setdefault(alias, path)
        stem = str(Path(path).with_suffix(""))
        path_lookup.setdefault(path, path)
        for candidate in (stem, f"{stem}/index", f"{stem}/__init__"):
            path_lookup.setdefault(candidate, path)
        for symbol in item["symbols"]:
            global_names[symbol["name"]].append(
                _symbol_id(path, symbol["qualified_name"])
            )

    edges = [
        Edge(item["type"], item["from"], item["to"], 1.0, "symbol-index")
        for item in index["edges"]
    ]
    parse_errors: list[dict] = []
    for item in index["files"]:
        path = item["path"]
        if item.get("error"):
            parse_errors.append({"path": path, "error": item["error"]})
        if item["language"] == "python":
            try:
                source = cache.content.read_text(root / path)
                tree = ast.parse(source)
            except (OSError, SyntaxError) as exc:
                if not any(error["path"] == path for error in parse_errors):
                    parse_errors.append({"path": path, "error": str(exc)})
                continue
            visitor = _PythonRelations(
                path, source, module_paths, symbols_by_file, global_names,
            )
            visitor.visit(tree)
            edges.extend(visitor.edges)
        else:
            try:
                source = cache.content.read_text(root / path)
            except OSError as exc:
                raise GraphError(f"关系图无法读取 {path}: {exc}") from exc
            edges.extend(_generic_import_edges(path, source, path_lookup))
            language = symbol_index.TREE_SITTER_LANGUAGES.get(
                Path(path).suffix.lower(),
            )
            if language and source:
                edges.extend(_tree_sitter_call_edges(
                    path, source, language, symbols_by_file, global_names,
                ))

    unique: dict[tuple[str, str, str], Edge] = {}
    for edge in edges:
        key = (edge.type, edge.source, edge.target)
        previous = unique.get(key)
        if previous is None or edge.confidence > previous.confidence:
            unique[key] = edge
    relationship_edges = [edge for edge in unique.values() if edge.type != "contains"]
    graph = {
        "schema_version": SCHEMA_VERSION,
        "root": ".",
        "nodes": index["nodes"],
        "edges": [asdict(edge) for edge in unique.values()],
        "parse_errors": parse_errors,
        "complete": not parse_errors,
        "summary": {
            "files": index["summary"]["files"],
            "symbols": index["summary"]["symbols"],
            "relationships": len(relationship_edges),
            "calls": sum(edge.type == "calls" for edge in relationship_edges),
            "imports": sum(edge.type == "imports" for edge in relationship_edges),
            "parse_errors": len(parse_errors),
        },
    }
    try:
        graph = codebase_memory_bridge.augment_graph(
            root, graph, mode=knowledge_graph,
        )
    except codebase_memory_bridge.KnowledgeGraphError as exc:
        raise GraphError(str(exc)) from exc
    relationship_edges = [
        edge for edge in graph["edges"] if edge["type"] != "contains"
    ]
    graph["summary"].update({
        "relationships": len(relationship_edges),
        "calls": sum(edge["type"] == "calls" for edge in relationship_edges),
        "imports": sum(edge["type"] == "imports" for edge in relationship_edges),
    })
    cache.remember(root, knowledge_graph, graph)
    return graph


def _node_path(node_id: str) -> str:
    value = node_id.split(":", 1)[-1]
    return value.split("::", 1)[0]


def _node_label(node_id: str) -> str:
    """Return the callable/file label, excluding its directory path."""
    value = node_id.split("::", 1)[-1]
    if "::" not in node_id:
        value = Path(value).stem
    return value.rsplit(".", 1)[-1]


def _has_keyword(node_id: str, words: set[str]) -> bool:
    """Match semantic name tokens, not arbitrary substrings in file paths."""
    tokens = set(re.findall(r"[A-Za-z0-9]+", _node_label(node_id).lower()))
    return bool(tokens & words)


def _file_sequence(nodes: list[str]) -> tuple[str, ...]:
    """Collapse intra-file helper hops while preserving cross-file order."""
    sequence: list[str] = []
    for node in nodes:
        path = _node_path(node)
        if not sequence or sequence[-1] != path:
            sequence.append(path)
    return tuple(sequence)


def _strongly_connected_components(nodes: set[str], adjacency: dict[str, list[dict]]) \
        -> list[list[str]]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for edge in adjacency.get(node, []):
            target = edge["target"]
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(component)

    for node in sorted(nodes):
        if node not in indices:
            visit(node)
    return components


def _discover_business_flows_with_coverage(graph: dict, max_depth: int = 6) \
        -> tuple[list[dict], list[dict]]:
    if max_depth < 2:
        raise GraphError("flow max depth 必须至少为 2")
    # Imports describe module availability, not an executed business step.
    # Traversing them here multiplies otherwise valid call paths; imports stay
    # in the graph for dependency analysis and incremental impact planning.
    relations = [edge for edge in graph["edges"] if edge["type"] == "calls"]
    adjacency: dict[str, list[dict]] = defaultdict(list)
    indegree: dict[str, int] = defaultdict(int)
    for edge in relations:
        adjacency[edge["source"]].append(edge)
        indegree[edge["target"]] += 1
    all_nodes = set(adjacency)
    all_nodes.update(edge["target"] for edge in relations)
    components = _strongly_connected_components(all_nodes, adjacency)
    starts = [
        node for node in adjacency
        if indegree[node] == 0 or _has_keyword(node, ENTRY_WORDS)
    ]
    paths: list[tuple[list[str], list[dict]]] = []
    for start in sorted(set(starts)):
        queue: deque[tuple[list[str], list[dict]]] = deque([([start], [])])
        while queue:
            nodes, path_edges = queue.popleft()
            current = nodes[-1]
            outgoing = [edge for edge in adjacency.get(current, []) if edge["target"] not in nodes]
            is_sink = _has_keyword(current, SINK_WORDS)
            if path_edges and (is_sink or not outgoing or len(nodes) >= max_depth):
                if len({_node_path(node) for node in nodes}) >= 2:
                    paths.append((nodes, path_edges))
                continue
            for edge in outgoing:
                queue.append(([*nodes, edge["target"]], [*path_edges, edge]))

    covered_nodes = {node for nodes, _ in paths for node in nodes}
    fallback_components: set[int] = set()
    for component_index, component in enumerate(components):
        component_nodes = set(component)
        if len(component) < 2 or component_nodes.issubset(covered_nodes):
            continue
        start = sorted(component)[0]
        queue: deque[tuple[list[str], list[dict]]] = deque([([start], [])])
        while queue:
            nodes, path_edges = queue.popleft()
            current = nodes[-1]
            outgoing = [
                edge for edge in adjacency.get(current, [])
                if edge["target"] not in nodes and edge["target"] in component
            ]
            if path_edges and (not outgoing or len(nodes) >= max_depth):
                paths.append((nodes, path_edges))
                fallback_components.add(component_index)
                covered_nodes.update(component)
                break
            for edge in outgoing:
                queue.append(([*nodes, edge["target"]], [*path_edges, edge]))

    # Every path here is already a call path; import-only dependency chains
    # remain available through the graph's imports edges, not coverage tasks.
    # Business coverage is a cross-file concern.  Function-level differences
    # are already scheduled as internal units; making every call-path variant
    # a separate business task causes combinatorial growth in real projects.
    # Keep one representative for each file sequence, while the graph still
    # retains every symbol-level call edge for evidence and impact analysis.
    deduplicated: dict[tuple[str, ...], tuple[list[str], list[dict]]] = {}
    for nodes, path_edges in paths:
        file_sequence = _file_sequence(nodes)
        previous = deduplicated.get(file_sequence)
        rank = (
            sum(edge["type"] == "calls" for edge in path_edges),
            len(nodes),
            min(edge["confidence"] for edge in path_edges),
        )
        previous_rank = (-1, -1, -1.0) if previous is None else (
            sum(edge["type"] == "calls" for edge in previous[1]),
            len(previous[0]),
            min(edge["confidence"] for edge in previous[1]),
        )
        if rank > previous_rank:
            deduplicated[file_sequence] = (nodes, path_edges)
    result = []
    for index, (_, (nodes, path_edges)) in enumerate(sorted(deduplicated.items()), 1):
        result.append({
            "id": f"B{index:03d}",
            "participants": nodes,
            "files": list(_file_sequence(nodes)),
            "edge_types": [edge["type"] for edge in path_edges],
            "confidence": round(min(edge["confidence"] for edge in path_edges), 2),
            "evidence": [edge["evidence"] for edge in path_edges],
        })
    uncovered = []
    for component_index, component in enumerate(components):
        if component_index in fallback_components or set(component).issubset(covered_nodes):
            continue
        uncovered.append({
            "id": f"U{len(uncovered) + 1:03d}",
            "nodes": sorted(component),
            "files": list(dict.fromkeys(_node_path(node) for node in sorted(component))),
            "reason": "no-entry-or-flow-path",
        })
    return result, uncovered


def discover_business_flows(graph: dict, max_depth: int = 6) -> list[dict]:
    """Return business flows, preserving the historical list return type."""
    flows, _ = _discover_business_flows_with_coverage(graph, max_depth)
    return flows


def discover_business_flows_with_coverage(graph: dict, max_depth: int = 6) -> dict:
    flows, uncovered = _discover_business_flows_with_coverage(graph, max_depth)
    return {"flows": flows, "uncovered_components": uncovered}


def _git_change_details(root: Path, base: str) -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", "-M", base, "--"], cwd=root,
            capture_output=True, text=True, timeout=30, check=False,
        )
    except OSError as exc:
        raise GraphError(f"无法运行 git: {exc}") from exc
    if result.returncode != 0:
        raise GraphError(result.stderr.strip() or f"git diff {base} 失败")
    changes: dict[str, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) >= 3:
            old_path, new_path = fields[1].strip(), fields[2].strip()
            changes[new_path] = {
                "status": "renamed" if status.startswith("R") else "copied",
                "path": new_path,
                "old_path": old_path,
            }
        else:
            path = fields[-1].strip()
            status_name = {
                "A": "added", "M": "modified", "D": "deleted",
                "T": "type_changed", "U": "unmerged",
            }.get(status[:1], status[:1].lower())
            changes[path] = {"status": status_name, "path": path}
    try:
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=root,
            capture_output=True, text=True, timeout=30, check=False,
        )
    except OSError as exc:
        raise GraphError(f"无法运行 git: {exc}") from exc
    if untracked.returncode != 0:
        raise GraphError(untracked.stderr.strip() or "git ls-files 失败")
    for line in untracked.stdout.splitlines():
        path = line.strip()
        if path:
            changes.setdefault(path, {"status": "added", "path": path})
    return [changes[path] for path in sorted(changes)]


def changed_paths(root: Path, base: str) -> list[str]:
    """Return current and removed paths changed from ``base``."""
    details = _git_change_details(root, base)
    paths = {item["path"] for item in details}
    paths.update(item["old_path"] for item in details if item.get("old_path"))
    return sorted(paths)


def incremental_plan(root: Path, base: str, depth: int = 2) -> dict:
    root = root.resolve()
    # One pass cache for the whole plan: build_graph, audit_units and
    # inventory_snapshot all walk the same repository, so sharing the content
    # cache turns three full re-reads into a single read per file.
    cache = GraphPassCache()
    graph = build_graph(root, cache=cache)
    if not graph["complete"]:
        raise GraphError(
            "项目关系图不完整，拒绝生成增量计划: "
            + "; ".join(
                f"{item['path']}: {item['error']}" for item in graph["parse_errors"][:5]
            )
        )
    change_details = _git_change_details(root, base)
    changed = sorted({item["path"] for item in change_details} | {
        item["old_path"] for item in change_details if item.get("old_path")
    })
    changed_set = set(changed)
    relations = [edge for edge in graph["edges"] if edge["type"] in {"calls", "imports"}]
    neighbors: dict[str, set[str]] = defaultdict(set)
    for edge in relations:
        source_path = _node_path(edge["source"])
        target_path = _node_path(edge["target"])
        neighbors[source_path].add(target_path)
        neighbors[target_path].add(source_path)
    impacted = set(changed_set)
    frontier = set(changed_set)
    for _ in range(max(depth, 0)):
        frontier = {target for source in frontier for target in neighbors.get(source, set())} - impacted
        impacted.update(frontier)
    units = []
    try:
        audit_units = symbol_index.audit_units(root, strict=True, cache=cache.content)
    except symbol_index.SourceDiscoveryError as exc:
        raise GraphError(str(exc)) from exc
    for asset in symbol_index.discover_asset_paths(root, strict=True):
        relative = asset.relative_to(root).as_posix()
        audit_units.append({
            "scope": relative, "path": relative, "name": asset.name,
            "kind": "asset", "line": 1, "end_line": 1,
            "parser": "asset-inventory", "error": None,
            "content_hash": symbol_index.file_sha256(asset, cache=cache.content),
            "language": "asset",
        })
    for unit in audit_units:
        path = unit["path"]
        if path not in impacted:
            continue
        score = 100 if path in changed_set else 60
        score += 10 if any(word in unit["name"].lower() for word in ENTRY_WORDS | SINK_WORDS) else 0
        units.append({**unit, "priority": score, "reason": (
            "changed" if path in changed_set else "dependency-impact"
        )})
    units.sort(key=lambda item: (-item["priority"], item["scope"]))
    inventory = symbol_index.inventory_snapshot(root, cache=cache.content)
    return {
        "schema_version": SCHEMA_VERSION,
        "base": base,
        "changed_paths": changed,
        "changes": change_details,
        "impacted_paths": sorted(impacted),
        "units": units,
        "inventory_hash": inventory["inventory_hash"],
        "full_coverage_required": True,
        "note": "增量计划只改变优先顺序，不替代最终全量覆盖门禁",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "flows", "incremental"),
                        nargs="?", default="scan")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="HEAD~1")
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--knowledge-graph", choices=("auto", "off", "on", "required"),
        default="auto",
        help="用 codebase-memory 增强调用关系；auto 仅在启动探针通过后启用",
    )
    args = parser.parse_args(argv)
    try:
        graph = build_graph(args.root, knowledge_graph=args.knowledge_graph)
        if args.command == "flows":
            payload = {"schema_version": SCHEMA_VERSION,
                       **discover_business_flows_with_coverage(graph, args.max_depth)}
        elif args.command == "incremental":
            payload = incremental_plan(args.root, args.base, args.depth)
        else:
            payload = graph
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif args.command == "scan":
            print(
                f"[project_graph] 文件 {graph['summary']['files']}，符号 "
                f"{graph['summary']['symbols']}，关系 {graph['summary']['relationships']}"
            )
        else:
            items = payload.get("flows", payload.get("units", []))
            for item in items:
                print(json.dumps(item, ensure_ascii=False))
        if graph["summary"]["parse_errors"]:
            return 1
        return 0
    except GraphError as exc:
        print(f"[project_graph] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
