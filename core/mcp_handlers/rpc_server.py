"""JSON-RPC server, stdio event loop, and CLI entry for BugX MCP service."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from core.runtime import bugx_logging
import core.bugx_paths as bugx_paths
from core.mcp_handlers import (
    MAX_MCP_REQUEST_BYTES,
    McpProtocolError,
    check_json_depth_and_keys,
    compact_json,
    format_result,
)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "bugx-supervisor"
SERVER_VERSION = "0.1.0"
JSONRPC_VERSION = "2.0"
HOST_SESSION_HEARTBEAT_INTERVAL_SECONDS = 5.0
MCP_ROLES = {"host", "worker"}
try:
    import core.mcp_handlers.channel_lifecycle as _cl
    import core.mcp_handlers.resource_prompt_table as _rpt
    import core.mcp_handlers.tool_table as _tt
except ImportError:
    import core.mcp_handlers.channel_lifecycle as _cl
    import core.mcp_handlers.resource_prompt_table as _rpt
    import core.mcp_handlers.tool_table as _tt


def _get_bms_attr(name: str, fallback: Any) -> Any:
    """动态获取 bugx_mcp_server 的属性，确保测试 patch 100% 穿透。"""
    mod = sys.modules.get("core.skills.bugx_mcp_server")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def _response(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _rpc_error(request_id: object, code: int, message: str, data: object | None = None) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _line_is_complete(chunk: object) -> bool:
    """readline 返回块是否以换行结尾（bytes / str 双形态）。"""
    if isinstance(chunk, bytes):
        return chunk.endswith(b"\n")
    return isinstance(chunk, str) and chunk.endswith("\n")


def _safe_tool_call_summary(params: Mapping[str, object], result: object) -> dict[str, object]:
    """Record tool-call shape, and optionally payload when verbose logging is enabled."""
    arguments = params.get("arguments")
    argument_map = arguments if isinstance(arguments, Mapping) else {}
    summary: dict[str, object] = {
        "argument_count": len(argument_map),
        "argument_keys": sorted(str(key) for key in argument_map),
        "result_type": type(result).__name__,
    }
    if os.environ.get("BUGX_LOG_VERBOSE") in {"1", "true", "yes", "on"}:
        summary["arguments"] = dict(argument_map)
        summary["result"] = result
    return summary


def serve(
    root: Path,
    *,
    role: str = "host",
    takeover: bool = False,
    idle_interval: float | None = None,
    exit_hook: Callable[[int], None] | None = None,
) -> int:
    service_cls = _get_bms_attr("BugxMcpService", None)
    if service_cls is None:
        from core.skills import bugx_mcp_server as _bms
        service_cls = _bms.BugxMcpService
    service = service_cls(root, role=role)

    activity_cls = _get_bms_attr("_ChannelActivity", _cl._ChannelActivity)
    activity = activity_cls(role)
    service.activity = activity

    controller_lease = None
    if role == "host" and service.is_initialized():
        if takeover:
            takeover_func = _get_bms_attr("_takeover_holder", _cl._takeover_holder)
            should_retry, evidence = takeover_func(service._scope().state_root)
            if evidence.get("taken_over"):
                record_lease_func = _get_bms_attr("_record_lease_event", _cl._record_lease_event)
                record_lease_func(
                    service, "controller_lease_taken_over",
                    holder_pid=evidence.get("holder_pid"),
                    holder_idle_seconds=evidence.get("holder_idle_seconds"),
                    by_pid=os.getpid(),
                )
            elif not should_retry:
                raise McpProtocolError(
                    f"接管被拒（{evidence.get('reason')}）：持有者 PID "
                    f"{evidence.get('holder_pid')} 闲置 "
                    f"{evidence.get('holder_idle_seconds')}s 未达阈值 "
                    f"{evidence.get('threshold_seconds')}s；可等待其自退或 "
                    f"kill {evidence.get('holder_pid')}"
                )
        controller_lease = service.acquire_controller_lease()

    channel_idle_monitor_func = _get_bms_attr("_channel_idle_monitor_loop", _cl._channel_idle_monitor_loop)
    channel_stop = threading.Event()
    channel_monitor = threading.Thread(
        target=channel_idle_monitor_func,
        args=(service, activity, channel_stop),
        kwargs={
            "lease": controller_lease,
            "interval": idle_interval,
            "exit_hook": exit_hook,
        },
        name=f"bugx-{role}-channel-idle-monitor",
        daemon=True,
    )
    channel_monitor.start()
    atexit.register(channel_stop.set)

    host_session_heartbeat_func = _get_bms_attr("_host_session_heartbeat_loop", _cl._host_session_heartbeat_loop)
    host_heartbeat_stop = threading.Event()
    host_heartbeat_thread = None
    if role == "host":
        host_heartbeat_thread = threading.Thread(
            target=host_session_heartbeat_func,
            args=(service, host_heartbeat_stop),
            name="bugx-host-session-heartbeat",
            daemon=True,
        )
        host_heartbeat_thread.start()
        atexit.register(host_heartbeat_stop.set)

    logger = None
    try:
        logger = bugx_logging.configure(service.root, command="mcp-serve")
    except (OSError, ValueError):
        logger = None

    cached_tool_definitions: list[dict[str, object]] | None = None
    tools_list_batch = bugx_logging.McpRequestBatch("tools/list") if logger is not None else None

    def flush_tools_list_batch(reason: str = "request_boundary") -> None:
        if logger is not None and tools_list_batch is not None:
            tools_list_batch.flush(logger, flush_reason=reason)

    tool_defs_func = _get_bms_attr("_tool_definitions", _tt._tool_definitions)
    resource_defs_func = _get_bms_attr("_resource_definitions", _rpt._resource_definitions)
    prompt_defs_func = _get_bms_attr("_prompt_definitions", _rpt._prompt_definitions)
    i18n_exc_func = _get_bms_attr("_i18n_exc", lambda k, f, **kw: f)
    call_failure_func = _get_bms_attr("_call_failure_data", lambda op, exc: {"error": str(exc)})
    error_payload_func = _get_bms_attr("_error_payload", lambda exc: {"error": str(exc)})

    stdin_buffer = getattr(sys.stdin, "buffer", None)
    raw_stream = stdin_buffer if stdin_buffer is not None else sys.stdin

    while True:
        raw_line = raw_stream.readline(MAX_MCP_REQUEST_BYTES + 1)
        if not raw_line:
            break
        request_fields = (
            bugx_logging.request_context(logger)
            if logger is not None else {}
        )
        if len(raw_line) > MAX_MCP_REQUEST_BYTES:
            if not _line_is_complete(raw_line):
                while True:
                    rest = raw_stream.readline(MAX_MCP_REQUEST_BYTES + 1)
                    if not rest or _line_is_complete(rest):
                        break
            flush_tools_list_batch()
            if logger is not None:
                bugx_logging.log_event(
                    logger, "mcp_request_rejected", "line too long", level=30,
                    method="invalid", error_code=-32602, **request_fields,
                )
            sys.stdout.write(compact_json(_rpc_error(
                None, -32602, f"请求行过长（>{MAX_MCP_REQUEST_BYTES} 字节）",
            )) + "\n")
            sys.stdout.flush()
            continue
        if isinstance(raw_line, bytes):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                flush_tools_list_batch()
                if logger is not None:
                    bugx_logging.log_event(
                        logger, "mcp_parse_error", "invalid utf-8 bytes", level=30,
                        method="invalid", error_code=-32700, **request_fields,
                    )
                sys.stdout.write(compact_json(_rpc_error(None, -32700, "Parse error")) + "\n")
                sys.stdout.flush()
                continue
        else:
            line = raw_line
        if not line.strip():
            continue
        request: object = None
        request_id: object = None
        method: str | None = None
        tool_name: str | None = None
        tool_arguments_snapshot: Mapping[str, object] = {}
        tool_list_cache_hit = False
        started = time.monotonic()
        try:
            if len(line.encode("utf-8")) > MAX_MCP_REQUEST_BYTES:
                raise McpProtocolError(
                    f"请求过大（>{MAX_MCP_REQUEST_BYTES} 字节）", code=-32602,
                )
            request = json.loads(line)
            check_json_depth_and_keys(request)
            if isinstance(request, dict):
                request_id = request.get("id")
                if logger is not None:
                    safe_request_id = (
                        request_id[:128] if isinstance(request_id, str)
                        else request_id
                    )
                    request_fields = bugx_logging.request_context(
                        logger,
                        safe_request_id
                        if isinstance(safe_request_id, (str, int)) and not isinstance(safe_request_id, bool)
                        else None,
                    )
            if not isinstance(request, dict) or request.get("jsonrpc") != JSONRPC_VERSION:
                raise McpProtocolError("只支持 JSON-RPC 2.0 MCP 请求", -32600)
            method = request.get("method")
            if not isinstance(method, str):
                raise McpProtocolError("method/params 格式无效", -32600)
            raw_params = request.get("params", {})
            if raw_params is None:
                params = {}
            elif not isinstance(raw_params, dict):
                raise McpProtocolError("params 必须是对象", -32602)
            else:
                params = raw_params
            if method != "tools/list":
                flush_tools_list_batch()
            if method in {"notifications/initialized", "notifications/cancelled"}:
                if logger is not None:
                    bugx_logging.log_event(
                        logger, "mcp_request_finished", method=method,
                        rpc_id=request_id, **request_fields,
                    )
                continue
            if method == "initialize":
                result = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": "每个工具调用都立即返回；根据 next_action 继续，不要等待长连接结果。",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                if cached_tool_definitions is None:
                    cached_tool_definitions = tool_defs_func(service.role)
                else:
                    tool_list_cache_hit = True
                definitions = cached_tool_definitions
                result = {"tools": definitions}
                try:
                    service.write_session_proof(
                        [item["name"] for item in definitions
                         if isinstance(item, dict) and isinstance(item.get("name"), str)]
                    )
                except (OSError, ValueError, RuntimeError):
                    pass
            elif method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str):
                    raise McpProtocolError(i18n_exc_func("mcp.tools_call_missing_name", "tools/call 缺少 name"))
                tool_name = name
                raw_args = params.get("arguments")
                if raw_args is not None and not isinstance(raw_args, dict):
                    raise McpProtocolError(i18n_exc_func("mcp.arguments_must_be_object", "arguments 必须是对象"), -32602)
                tool_arguments_snapshot = dict(raw_args or {})
                result = format_result(service.call(name, dict(tool_arguments_snapshot)))
            elif method == "resources/list":
                result = {"resources": resource_defs_func()}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "resources/read":
                uri = params.get("uri")
                if not isinstance(uri, str) or not uri.strip():
                    raise McpProtocolError(i18n_exc_func("mcp.resources_read_uri_required", "resources/read 缺少有效的 uri 参数"), -32602)
                result = service.read_resource(uri)
            elif method == "prompts/list":
                result = {"prompts": prompt_defs_func()}
            elif method == "prompts/get":
                name = params.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise McpProtocolError(i18n_exc_func("mcp.prompt_name_required", "prompts/get 缺少有效的 name 参数"), -32602)
                raw_args = params.get("arguments")
                if raw_args is not None and not isinstance(raw_args, dict):
                    raise McpProtocolError(i18n_exc_func("mcp.arguments_must_be_object", "arguments 必须是对象"), -32602)
                result = service.get_prompt(name, raw_args if raw_args is not None else {})
            else:
                raise McpProtocolError(i18n_exc_func("mcp.unsupported_mcp_method", f"不支持 MCP 方法: {method}", method=method), -32601)
            if "id" in request:
                sys.stdout.write(compact_json(_response(request_id, result)) + "\n")
                sys.stdout.flush()
            duration_ms = round((time.monotonic() - started) * 1000, 2)
            if method == "tools/list":
                if tools_list_batch is not None:
                    if tools_list_batch.due():
                        tools_list_batch.flush(logger, flush_reason="batch_limit")
                    tools_list_batch.record(
                        request_fields,
                        duration_ms=duration_ms,
                        counters={
                            "cache_hits": int(tool_list_cache_hit),
                            "cache_misses": int(not tool_list_cache_hit),
                        },
                    )
            elif logger is not None:
                finished_fields = dict(request_fields)
                if tool_name is not None:
                    finished_fields["tool_name"] = tool_name
                    finished_fields["summary"] = _safe_tool_call_summary(
                        {"arguments": tool_arguments_snapshot}, result,
                    )
                bugx_logging.log_event(logger, "mcp_request_finished", method=method,
                                       rpc_id=request_id,
                                       duration_ms=duration_ms,
                                       **finished_fields)
        except json.JSONDecodeError as exc:
            flush_tools_list_batch("request_error")
            if logger is not None:
                bugx_logging.log_event(
                    logger, "mcp_parse_error", str(exc), level=30,
                    method="invalid", error_code=-32700, **request_fields,
                )
            sys.stdout.write(compact_json(_rpc_error(None, -32700, "Parse error")) + "\n")
            sys.stdout.flush()
        except McpProtocolError as exc:
            flush_tools_list_batch("request_error")
            if logger is not None:
                rejected_fields = dict(request_fields)
                if tool_name is not None:
                    rejected_fields["tool_name"] = tool_name
                bugx_logging.log_event(
                    logger, "mcp_request_rejected", str(exc), level=30,
                    method=(request.get("method") if isinstance(request, dict) else "invalid"),
                    rpc_id=request_id,
                    error_code=exc.code,
                    **rejected_fields,
                )
            if request is None or "id" in (request if isinstance(request, dict) else {}):
                operation = (
                    (request.get("params") or {}).get("name")
                    if isinstance(request, dict)
                    and isinstance(request.get("params"), dict) else "unknown"
                )
                data = (
                    call_failure_func(operation, exc)
                    if isinstance(request, dict)
                    and request.get("method") == "tools/call" else None
                )
                sys.stdout.write(
                    compact_json(_rpc_error(request_id, exc.code, str(exc), data)) + "\n"
                )
                sys.stdout.flush()
        except Exception as exc:  # pragma: no cover
            flush_tools_list_batch("request_error")
            if "id" in (request if isinstance(request, dict) else {}):
                operation = (
                    (request.get("params") or {}).get("name")
                    if isinstance(request, dict)
                    and isinstance(request.get("params"), dict) else "unknown"
                )
                payload = call_failure_func(operation, exc) if isinstance(request, dict) and request.get("method") == "tools/call" else error_payload_func(exc)
                sys.stdout.write(compact_json(_response(request_id, format_result(payload, is_error=True))) + "\n")
                sys.stdout.flush()
            if logger is not None:
                bugx_logging.log_event(
                    logger, "mcp_request_failed", str(exc), level=40,
                    method=(request.get("method") if isinstance(request, dict) else "invalid"),
                    rpc_id=request_id, tool_name=tool_name,
                    exception_type=type(exc).__name__, exc_info=True,
                    **request_fields,
                )
    flush_tools_list_batch("connection_closed")
    host_heartbeat_stop.set()
    if host_heartbeat_thread is not None:
        host_heartbeat_thread.join(timeout=1.0)
    service.clear_session_proof()
    if controller_lease is not None:
        controller_lease.release()
    return 0


def _resolve_serve_root(root: Path) -> Path:
    """启动前解析有效服务 root：显式参数优先，参数无合法布局时自愈回退。"""
    def _has_layout(candidate: Path) -> bool:
        try:
            bugx_paths.require(candidate)
            return True
        except (AttributeError, OSError, ValueError, RuntimeError):
            return False

    if _has_layout(root):
        return root
    for raw in (os.environ.get("BUGX_PROJECT_ROOT"), str(Path.cwd())):
        if not raw:
            continue
        candidate = Path(raw)
        try:
            same = candidate.resolve() == root.resolve()
        except (OSError, RuntimeError):
            same = False
        if not same and _has_layout(candidate):
            print(
                f"[bugx-mcp] ⚠ 注册 root 无有效布局（{root}），自愈回退到 {candidate}",
                file=sys.stderr,
            )
            return candidate
    return root


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("serve", nargs="?", default="serve")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--role", choices=sorted(MCP_ROLES), default="host")
    parser.add_argument(
        "--takeover", action="store_true",
        help=(
            "DOC-230 3.1 层 3：仅当 host 通道持有者**闲置超阈值且未自退**时，"
            "SIGTERM 接管其租约（默认关闭）"
        ),
    )
    args = parser.parse_args(argv)
    if args.serve != "serve":
        parser.error("只支持 `serve`")
    try:
        serve_func = _get_bms_attr("serve", serve)
        resolve_root_func = _get_bms_attr("_resolve_serve_root", _resolve_serve_root)
        return serve_func(resolve_root_func(args.root), role=args.role, takeover=args.takeover)
    except McpProtocolError as exc:
        print(f"bugx-supervisor 启动失败: {exc}", file=sys.stderr)
        return 2
