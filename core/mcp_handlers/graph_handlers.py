#!/usr/bin/env python3
"""Host and Worker MCP handlers for deterministic native-agent graphs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import core.bugx_paths as bugx_paths

from core.agent.agent_orchestrator import AgentOrchestrator, GraphError

from .base import McpProtocolError


def _private_root(service: Any) -> Path:
    """Keep run state under a per-project namespace in BugX's private cache."""
    project_key = hashlib.sha256(str(service.root.resolve()).encode("utf-8")).hexdigest()[:32]
    root = bugx_paths.global_cache_root() / "agent-graphs" / project_key
    try:
        bugx_paths.assert_no_symlink_components(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        bugx_paths.assert_no_symlink_components(root)
    except (OSError, bugx_paths.LayoutError) as exc:
        raise McpProtocolError(f"graph 私有根不可用: {exc}") from exc
    if root.is_symlink() or not root.is_dir():
        raise McpProtocolError("graph 私有根不是安全目录")
    return root


def _open(service: Any, arguments: Mapping[str, object], name: str, role: str) -> AgentOrchestrator:
    service._require_role(arguments, name, role)
    service._scope()
    graph_id = arguments.get("graph_id")
    if not isinstance(graph_id, str) or not graph_id:
        raise McpProtocolError("graph_id 必须是非空字符串")
    try:
        return AgentOrchestrator.open(_private_root(service), graph_id)
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc


def _worker_identity(service: Any, arguments: Mapping[str, object], name: str) -> str:
    service._require_role(arguments, name, "worker")
    bound = getattr(service, "native_agent_id", None)
    supplied = arguments.get("native_agent_id")
    if not isinstance(bound, str) or not bound:
        raise McpProtocolError(
            "Worker MCP 进程未由 Host 注入 BUGX_NATIVE_AGENT_ID，拒绝图任务调用"
        )
    if supplied != bound:
        raise McpProtocolError("native_agent_id 与当前 Worker MCP 通道绑定身份不匹配")
    return bound


def handle_graph_start(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "graph_start"
    service._require_role(arguments, name, "host")
    service._scope()
    try:
        task_input = {
            "target": arguments.get("target"),
            "goal": arguments.get("goal"),
            "modifications": arguments.get("modifications"),
            "invariants": arguments.get("invariants"),
            "ownership_boundaries": arguments.get("ownership_boundaries"),
            "acceptance_criteria": arguments.get("acceptance_criteria"),
            "target_files": arguments.get("target_files"),
            "patch_requested": arguments.get("include_patch", False),
            "max_concurrency": arguments.get("max_concurrency", 1),
        }
        orchestrator, task = AgentOrchestrator.start(
            service.root, _private_root(service), task_input,
        )
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc
    return {
        "graph_id": orchestrator.graph_id,
        "status": "running",
        "private_workspace": str(orchestrator.workspace),
        "snapshot": orchestrator.snapshot()["snapshot"],
        "first_task": task,
    }


def handle_graph_next(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    orchestrator = _open(service, arguments, "graph_next", "host")
    try:
        task = orchestrator.next_task()
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc
    if task is None:
        state = orchestrator.snapshot()
        return {
            "graph_id": orchestrator.graph_id,
            "status": state["status"],
            "task": None,
            "active_count": sum(
                item.get("status") in {"offered", "assigned", "running"}
                for item in state["tasks"]
            ),
            "capacity": state["max_concurrency"],
        }
    return {
        "graph_id": orchestrator.graph_id,
        "status": "ready",
        "task": task,
        "workspace": str(orchestrator.workspace),
    }


def handle_graph_bind_agent(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    orchestrator = _open(service, arguments, "graph_bind_agent", "host")
    task_id = arguments.get("task_id")
    native_agent_id = arguments.get("native_agent_id")
    if not isinstance(task_id, str) or not task_id:
        raise McpProtocolError("task_id 必须是非空字符串")
    if not isinstance(native_agent_id, str) or not native_agent_id:
        raise McpProtocolError("native_agent_id 必须是非空字符串")
    try:
        task = orchestrator.bind_agent(task_id, native_agent_id)
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc
    return {
        "graph_id": orchestrator.graph_id,
        "task_id": task_id,
        "stage": task["stage"],
        "status": task["status"],
        "native_agent_id": task["native_agent_id"],
        "fencing_token": task["fencing_token"],
        "task_spec": task["prompt"],
        "workspace": str(orchestrator.workspace),
        "binding_assertion": "host_native_agent_id",
    }


def handle_graph_cancel(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    orchestrator = _open(service, arguments, "graph_cancel", "host")
    reason = arguments.get("reason")
    task_id = arguments.get("task_id")
    try:
        return orchestrator.cancel(
            reason if isinstance(reason, str) else "",
            task_id if isinstance(task_id, str) else None,
        )
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc


def handle_graph_finalize(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    orchestrator = _open(service, arguments, "graph_finalize", "host")
    decision = arguments.get("decision")
    summary = arguments.get("summary")
    try:
        return orchestrator.finalize(
            decision if isinstance(decision, str) else "",
            summary if isinstance(summary, str) else "",
        )
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc


def handle_graph_context(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "graph_context"
    native_agent_id = _worker_identity(service, arguments, name)
    orchestrator = _open(service, arguments, name, "worker")
    task_id = arguments.get("task_id")
    fencing_token = arguments.get("fencing_token")
    if not isinstance(task_id, str) or isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
        raise McpProtocolError("task_id/fencing_token 无效")
    try:
        return orchestrator.context(task_id, native_agent_id, fencing_token)
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc


def handle_graph_run_check(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "graph_run_check"
    native_agent_id = _worker_identity(service, arguments, name)
    orchestrator = _open(service, arguments, name, "worker")
    task_id = arguments.get("task_id")
    fencing_token = arguments.get("fencing_token")
    if not isinstance(task_id, str) or isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
        raise McpProtocolError("task_id/fencing_token 无效")
    try:
        observation = orchestrator.run_check(
            task_id, native_agent_id, fencing_token, arguments.get("plan"),
        )
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc
    return observation


def handle_graph_checkpoint(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    name = "graph_checkpoint"
    native_agent_id = _worker_identity(service, arguments, name)
    orchestrator = _open(service, arguments, name, "worker")
    task_id = arguments.get("task_id")
    fencing_token = arguments.get("fencing_token")
    if not isinstance(task_id, str) or isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
        raise McpProtocolError("task_id/fencing_token 无效")
    try:
        receipt = orchestrator.checkpoint(
            task_id, native_agent_id, fencing_token,
            arguments.get("envelope"), arguments.get("attachments", []),
        )
    except GraphError as exc:
        raise McpProtocolError(str(exc)) from exc
    return receipt


GRAPH_HANDLERS: dict[str, Any] = {
    "graph_start": handle_graph_start,
    "graph_next": handle_graph_next,
    "graph_bind_agent": handle_graph_bind_agent,
    "graph_cancel": handle_graph_cancel,
    "graph_finalize": handle_graph_finalize,
    "graph_context": handle_graph_context,
    "graph_run_check": handle_graph_run_check,
    "graph_checkpoint": handle_graph_checkpoint,
}
