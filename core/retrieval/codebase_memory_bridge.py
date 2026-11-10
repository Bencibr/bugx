#!/usr/bin/env python3
"""把 codebase-memory-mcp 的语义关系安全地接入本地项目图。

本模块只做可选增强，不把 MCP 当成全量覆盖事实源：文件、配置和符号仍由
``symbol_index`` 枚举；图谱只补充动态/框架关系，并且所有节点都必须能回到
当前工作树中的本地文件和符号，否则丢弃该关系并记录统计。
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any

from core.probes import codebase_memory_probe


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 45
MAX_CALL_ROWS = 20000
MIN_CALL_CONFIDENCE = 0.85
TRUSTED_CALL_STRATEGIES = {
    "lsp_direct", "lsp_method", "lsp_constructor", "lsp_generic_method",
    "lsp_builtin", "lsp_builtin_method", "lsp_builtin_constructor",
    "lsp_module_attr", "lsp_callable_alias", "lsp_method_union",
    "import_map", "qualified_suffix",
}


class KnowledgeGraphError(RuntimeError):
    """MCP 可用性、索引或结果格式不满足增强契约。"""


def _run_query(executable: str, project: str, query: str, *,
               timeout: float = DEFAULT_TIMEOUT) -> str:
    # query_graph 的文本表格比 --json 包装更稳定，列值不包含空格，便于保守解析。
    try:
        result = subprocess.run(
            [
                executable, "cli", "--quiet", "query_graph",
                "--project", project, "--query", query,
            ],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KnowledgeGraphError(f"调用 query_graph 失败: {exc}") from exc
    if result.returncode != 0:
        raise KnowledgeGraphError(
            (result.stderr or result.stdout).strip()[:1000]
            or f"调用 query_graph 返回 {result.returncode}"
        )
    return result.stdout


def _find_project(root: Path, executable: str) -> dict[str, Any]:
    try:
        result = codebase_memory_probe.list_projects_json(
            executable,
            timeout_seconds=min(DEFAULT_TIMEOUT, 30.0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KnowledgeGraphError(f"调用 list_projects 失败: {exc}") from exc
    if result.returncode != 0:
        raise KnowledgeGraphError(
            (result.stderr or result.stdout).strip()[:1000]
            or f"调用 list_projects 返回 {result.returncode}"
        )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise KnowledgeGraphError(
            "list_projects --format json 返回无效 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise KnowledgeGraphError("list_projects --format json 返回结构不是对象")
    projects = payload.get("projects")
    if not isinstance(projects, list):
        raise KnowledgeGraphError(
            "list_projects --format json --detail stats 缺少 projects 数组"
        )
    for project in projects:
        if not isinstance(project, dict):
            continue
        try:
            if Path(str(project.get("root_path", ""))).resolve() == root:
                return project
        except (OSError, RuntimeError):
            continue
    raise KnowledgeGraphError(f"当前仓库未建立 codebase-memory 索引: {root}")


def inspect(root: Path, binary: str | None = None) -> dict[str, Any]:
    """检查当前项目图谱是否可用于语义增强。"""
    root = root.expanduser().resolve()
    expected_version = codebase_memory_probe._pinned_cli_version()
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "DEGRADED",
        "usable": False,
        "quality": "unavailable",
        "root": str(root),
        "project": None,
        "version": None,
        "reason": "未安装 codebase-memory-mcp",
        "partial_files": [],
        "not_indexed_files": [],
    }
    if expected_version is None:
        base["reason"] = "codebase-memory 固定版本规格无效"
        return base
    executable = codebase_memory_probe.resolve_compatible_binary(
        expected_version=expected_version,
        binary=binary,
        timeout_seconds=10.0,
    )
    if not executable:
        base["reason"] = (
            f"未找到报告固定版本 {expected_version} 的 codebase-memory-mcp 可执行文件"
        )
        return base
    base["executable"] = executable
    try:
        try:
            version = subprocess.run(
                [executable, "--version"], capture_output=True, text=True,
                timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            base["reason"] = f"读取 codebase-memory 版本失败: {exc}"
            return base
        version_text = (version.stdout or version.stderr).strip()
        base["version"] = version_text or None
        reported_version = codebase_memory_probe._reported_cli_version(version_text)
        if version.returncode != 0 or reported_version != expected_version:
            base["reason"] = (
                f"codebase-memory 版本复核与固定版本 {expected_version} 不符: "
                f"{version_text or f'--version exit {version.returncode}'}"
            )
            return base
        project = _find_project(root, executable)
        name = str(project.get("name") or "")
        if not name:
            raise KnowledgeGraphError("索引项目缺少名称")
        base["project"] = name
        try:
            status_result = codebase_memory_probe.index_status_json(
                executable,
                name,
                timeout_seconds=min(DEFAULT_TIMEOUT, 30.0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KnowledgeGraphError(f"调用 index_status 失败: {exc}") from exc
        if status_result.returncode != 0:
            raise KnowledgeGraphError(
                (status_result.stderr or status_result.stdout).strip()[:1000]
                or f"调用 index_status 返回 {status_result.returncode}"
            )
        try:
            status = json.loads(status_result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KnowledgeGraphError("index_status --format json 返回无效 JSON") from exc
        if not isinstance(status, dict):
            raise KnowledgeGraphError("index_status --format json 返回结构不是对象")
        base["index_status"] = status
        partial = status.get("parse_partial") or {}
        not_indexed = status.get("not_indexed") or {}
        base["partial_files"] = partial.get("files", []) if isinstance(partial, dict) else []
        base["not_indexed_files"] = (
            not_indexed.get("files", []) if isinstance(not_indexed, dict) else []
        )
        indexed_status = str(status.get("status") or "").lower()
        nodes = int(status.get("nodes") or project.get("nodes") or 0)
        generation_matches = (status.get("metadata") or {}).get("generation_matches")
        usable = indexed_status == "ready" and nodes > 0
        if generation_matches is False:
            usable = False
            base["reason"] = "索引代际与当前项目不一致"
        else:
            base["reason"] = "索引可用"
        base["usable"] = usable
        base["status"] = "READY" if usable else "DEGRADED"
        base["quality"] = "partial" if base["partial_files"] else "complete"
        if base["quality"] == "partial":
            base["reason"] += "；存在解析不完整文件，本地索引仍是覆盖事实源"
    except KnowledgeGraphError as exc:
        base["reason"] = str(exc)
    return base


def _parse_call_rows(text: str) -> list[tuple[str, str, str, str, float, str]]:
    rows: list[tuple[str, str, str, str, float, str]] = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("rows:") or value.startswith("total:"):
            continue
        fields = value.split()
        if len(fields) != 6:
            continue
        try:
            confidence = float(fields[4].strip('"'))
        except ValueError:
            continue
        if not math.isfinite(confidence):
            # DOC-203 D1-c：外部图谱通道拒绝非有限置信度（NaN/inf 会传染
            # 下游排序/门禁比较，使确定性截断失效）。
            continue
        rows.append((fields[0], fields[1], fields[2], fields[3], confidence, fields[5]))
    return rows


def _local_symbol_lookup(graph: dict[str, Any]) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for node in graph.get("nodes", []):
        if not isinstance(node, dict) or node.get("type") != "symbol":
            continue
        path = str(node.get("path") or "")
        qualified = str(node.get("qualified_name") or "")
        node_id = str(node.get("id") or "")
        if path and qualified and node_id:
            lookup[(path, qualified)] = node_id
    return lookup


def _resolve_qualified(path: str, qualified: str,
                       lookup: dict[tuple[str, str], str]) -> str | None:
    exact = lookup.get((path, qualified))
    if exact:
        return exact
    candidates = [
        node_id for (node_path, local_name), node_id in lookup.items()
        if node_path == path and qualified.endswith("." + local_name)
    ]
    return candidates[0] if len(candidates) == 1 else None


def augment_graph(root: Path, graph: dict[str, Any], mode: str = "auto") -> dict[str, Any]:
    """用 codebase-memory 的 CALLS 关系增强本地图，失败时保持原图。

    ``auto`` 只有宿主在启动探针通过后设置 ``BUGX_USE_CODEBASE_MEMORY=1`` 才启用；
    ``on`` 用于显式命令，``required`` 用于需要图谱的专项验收，缺失时抛错。
    """
    normalized = str(mode or "auto").lower()
    if normalized not in {"auto", "off", "on", "required"}:
        raise KnowledgeGraphError(f"未知知识图谱模式: {mode}")
    if normalized == "off" or (
        normalized == "auto" and os.environ.get("BUGX_USE_CODEBASE_MEMORY") != "1"
    ):
        graph["knowledge_graph"] = {
            "schema_version": SCHEMA_VERSION, "status": "DISABLED",
            "usable": False, "reason": "未请求知识图谱增强",
            "added_edges": 0,
        }
        return graph

    context = inspect(root)
    if not context["usable"]:
        graph["knowledge_graph"] = {**context, "added_edges": 0}
        if normalized == "required":
            raise KnowledgeGraphError(str(context["reason"]))
        return graph

    project = str(context["project"])
    query = (
        "MATCH (a)-[r:CALLS]->(b) "
        f"WHERE r.confidence >= {MIN_CALL_CONFIDENCE} "
        "RETURN a.file_path, a.qualified_name, b.file_path, b.qualified_name, "
        "r.confidence, r.strategy "
        f"LIMIT {MAX_CALL_ROWS}"
    )
    try:
        rows = _parse_call_rows(_run_query(
            str(context["executable"]), project, query,
        ))
    except KnowledgeGraphError:
        if normalized == "required":
            raise
        graph["knowledge_graph"] = {
            **context, "status": "DEGRADED", "usable": False,
            "reason": "query_graph 调用失败，保留本地图", "added_edges": 0,
        }
        return graph

    lookup = _local_symbol_lookup(graph)
    local_paths = {path for path, _ in lookup}
    existing = {
        (edge.get("type"), edge.get("source"), edge.get("target"))
        for edge in graph.get("edges", [])
        if isinstance(edge, dict)
    }
    added = []
    skipped = 0
    prefix = project + "."
    for source_path, source_name, target_path, target_name, confidence, strategy in rows:
        if source_path not in local_paths or target_path not in local_paths:
            skipped += 1
            continue
        # Same-file helper calls are already represented by the local parser;
        # the bridge is for cross-file/framework edges where the local parser
        # has the largest blind spot.
        if (
            source_path == target_path
            or confidence < MIN_CALL_CONFIDENCE
            or strategy not in TRUSTED_CALL_STRATEGIES
        ):
            skipped += 1
            continue
        source = _resolve_qualified(
            source_path, source_name[len(prefix):] if source_name.startswith(prefix) else source_name,
            lookup,
        )
        target = _resolve_qualified(
            target_path, target_name[len(prefix):] if target_name.startswith(prefix) else target_name,
            lookup,
        )
        if not source or not target or source == target:
            skipped += 1
            continue
        key = ("calls", source, target)
        if key in existing:
            continue
        existing.add(key)
        added.append({
            "type": "calls", "source": source, "target": target,
            "confidence": confidence,
            "evidence": f"codebase-memory:{project}:{strategy}",
        })
    graph["edges"] = [*graph.get("edges", []), *added]
    graph["knowledge_graph"] = {
        **context, "status": "READY", "usable": True,
        "query_rows": len(rows), "skipped_rows": skipped,
        "added_edges": len(added),
        "reason": "已与本地符号索引交叉校验后合并 CALLS 关系",
    }
    return graph
