#!/usr/bin/env python3
"""core/mcp_handlers/retrieval_handlers.py - 结构化切片、局部拓扑与记忆检索 MCP 工具处理器 (DOC-234)。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

try:
    from core.runtime.project_graph import build_graph
    from core.retrieval.graph_ego import extract_ego_subgraph
    from core.retrieval.memory_store import MemoryStore
    from core.retrieval.slice_extractor import SliceExtractor, SliceExtractionError
    from core.retrieval.slice_normalizer import SliceNormalizer
except ImportError:
    from core.runtime.project_graph import build_graph
    from core.retrieval.graph_ego import extract_ego_subgraph
    from core.retrieval.memory_store import MemoryStore
    from core.retrieval.slice_extractor import SliceExtractor, SliceExtractionError
    from core.retrieval.slice_normalizer import SliceNormalizer

from .base import (
    McpProtocolError,
    bool_argument,
    number_argument,
    optional_string,
)


def _resolve_target_path(service: Any, target: str) -> tuple[Path, str]:
    if not isinstance(target, str) or not target.strip():
        raise McpProtocolError("target 必须是非空路径字符串")
    target_clean = target.strip()
    root = getattr(service, "root", None)
    if root is None:
        path = Path(target_clean).resolve()
        if not path.is_file():
            raise McpProtocolError(f"目标文件不存在: {target_clean}")
        return path, path.name

    root_resolved = Path(root).resolve()
    target_path = (root_resolved / target_clean).resolve()
    try:
        rel = target_path.relative_to(root_resolved)
    except ValueError as exc:
        raise McpProtocolError(f"target 路径超出项目根目录: {target_clean}") from exc
    if not target_path.exists():
        raise McpProtocolError(f"目标文件不存在: {target_clean}")
    if target_path.is_dir():
        raise McpProtocolError(f"target 必须是文件而不是目录: {target_clean}")
    return target_path, str(rel)


def _get_memory_store_path(service: Any, db_path_arg: str | None) -> str:
    if db_path_arg and db_path_arg.strip():
        return db_path_arg.strip()
    root = getattr(service, "root", None)
    if root is not None:
        p = Path(root).resolve() / ".bugx" / "memory.sqlite3"
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    return ":memory:"


def handle_retrieve_slice(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """结构化检索代码块切片（支持按语言、符号名、种类有界提取与语法自愈）。"""
    target = arguments.get("target")
    if not isinstance(target, str):
        raise McpProtocolError("target 必须是字符串路径")

    target_path, rel_file = _resolve_target_path(service, target)

    symbol_filter = optional_string(arguments, "symbol")
    kind_filter = optional_string(arguments, "kind")
    to_executable = bool_argument(arguments, "to_executable", default=False)
    include_context = bool_argument(arguments, "include_context", default=False)
    max_slices = int(number_argument(arguments, "max_slices", default=5, integer=True, minimum=1))
    if max_slices > 20:
        max_slices = 20

    try:
        raw_bytes = target_path.read_bytes()
    except OSError as exc:
        raise McpProtocolError(f"读取文件失败: {exc}") from exc

    if len(raw_bytes) > 2 * 1024 * 1024:
        raise McpProtocolError("文件大小超出 2MB 限制")

    file_digest = hashlib.sha256(raw_bytes).hexdigest()
    project_id = getattr(service, "project_id", "bugx-project") or "bugx-project"

    extractor = SliceExtractor(
        project_id=project_id,
        snapshot_digest=file_digest,
        generation=1,
    )

    try:
        specs = extractor.extract(raw_bytes, file=rel_file)
    except SliceExtractionError as exc:
        raise McpProtocolError(f"切片提取失败: {exc}") from exc

    matched = []
    for spec in specs:
        if symbol_filter:
            sym = spec.symbol_name or ""
            qname = spec.qualified_name or ""
            if symbol_filter != sym and not qname.endswith("." + symbol_filter) and symbol_filter != qname:
                continue
        if kind_filter and spec.kind != kind_filter:
            continue
        matched.append(spec)

    slices_output = []
    for spec in matched[:max_slices]:
        item: dict[str, Any] = {
            "slice_id": spec.slice_id,
            "kind": spec.kind,
            "symbol_name": spec.symbol_name,
            "qualified_name": spec.qualified_name,
            "start_line": spec.start_line,
            "end_line": spec.end_line,
            "start_byte": spec.start_byte,
            "end_byte": spec.end_byte,
            "language": spec.language,
            "display_text": spec.display_text,
        }
        if to_executable:
            view = SliceNormalizer.to_executable(spec)
            item["executable_view"] = {
                "code": view.executable_code,
                "wrapper_type": view.wrapper_type,
                "line_offset": view.line_offset,
                "is_valid_ast": view.is_valid_ast,
            }
        if include_context:
            imports = SliceNormalizer.extract_imports(raw_bytes, language=spec.language)
            item["tiered_context"] = SliceNormalizer.assemble_context(spec, imports=imports)
        slices_output.append(item)

    return {
        "status": "ok",
        "target": rel_file,
        "total_extracted": len(specs),
        "total_matched": len(matched),
        "slices": slices_output,
    }


def handle_extract_ego_subgraph(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """提取指定中心节点的 k-hop 局部有界调用/依赖拓扑子图（防上下文爆炸）。"""
    center_node = arguments.get("center_node")
    if not isinstance(center_node, str) or not center_node.strip():
        raise McpProtocolError("center_node 必须是非空字符串")
    center_node = center_node.strip()

    depth = int(number_argument(arguments, "depth", default=1, integer=True, minimum=1))
    if depth > 2:
        depth = 2
    direction = str(arguments.get("direction", "both") or "both").strip().lower()
    if direction not in ("both", "inbound", "outbound"):
        direction = "both"

    graph_arg = arguments.get("graph")
    if isinstance(graph_arg, Mapping):
        graph_data: Mapping[str, Any] = graph_arg
    else:
        root = getattr(service, "root", None)
        if root is not None:
            try:
                graph_data = build_graph(Path(root))
            except Exception:
                graph_data = {"nodes": [{"id": center_node}], "edges": []}
        else:
            graph_data = {"nodes": [{"id": center_node}], "edges": []}

    try:
        response = extract_ego_subgraph(
            graph_data,
            center_node_id=center_node,
            depth=depth,
            direction=direction,
        )
    except Exception as exc:
        raise McpProtocolError(f"拓扑子图提取失败: {exc}") from exc

    return {
        "status": "ok",
        **response.to_dict(),
    }


def handle_memory_query(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """在隔离命名空间内执行确定性记忆检索与经验知识召回。"""
    namespace = arguments.get("namespace")
    if not isinstance(namespace, str) or not namespace.strip():
        raise McpProtocolError("namespace 必须是非空字符串")
    namespace = namespace.strip()

    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise McpProtocolError("query 必须是非空字符串")
    query = query.strip()

    top_k = int(number_argument(arguments, "top_k", default=5, integer=True, minimum=1))
    if top_k > 20:
        top_k = 20

    db_path_arg = optional_string(arguments, "db_path")
    db_path = _get_memory_store_path(service, db_path_arg)

    try:
        with MemoryStore(db_path) as store:
            hits = store.recall(namespace, query, top_k=top_k)
    except Exception as exc:
        raise McpProtocolError(f"记忆检索失败: {exc}") from exc

    return {
        "status": "ok",
        "namespace": namespace,
        "query": query,
        "count": len(hits),
        "results": hits,
    }


RETRIEVAL_HANDLERS = {
    "bugx_retrieve_slice": handle_retrieve_slice,
    "bugx_extract_ego_subgraph": handle_extract_ego_subgraph,
    "bugx_memory_query": handle_memory_query,
}

__all__ = [
    "RETRIEVAL_HANDLERS",
    "handle_extract_ego_subgraph",
    "handle_memory_query",
    "handle_retrieve_slice",
]
