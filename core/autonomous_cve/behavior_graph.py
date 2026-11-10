#!/usr/bin/env python3
"""从路由、事件、数据、配置和权限事实建立跨模块系统行为图。"""
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
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from core.retrieval import symbol_index

SCHEMA_VERSION = 1
STATE_MATRIX_SCHEMA_VERSION = 1
STATE_KEYS = frozenset({
    "status", "state", "step", "role", "permission", "phase", "flow",
})
_SENSITIVE_STATE_KEY = re.compile(
    r"(?:token|secret|password|passwd|cookie|authorization|credential|api[_-]?key)",
    re.I,
)
_MAX_STATE_VALUE_LENGTH = 128
_MAX_STATE_FIELDS = 32


class BehaviorGraphError(ValueError):
    """行为图构建错误。"""


@dataclass(frozen=True)
class Surface:
    kind: str
    role: str
    name: str
    path: str
    line: int
    confidence: float
    evidence: str


PATTERNS: tuple[tuple[str, str, re.Pattern[str], float], ...] = (
    ("http-route", "exposes", re.compile(
        r"(?:@\w+\.(?:get|post|put|patch|delete|route)|\b(?:app|router)\.(?:get|post|put|patch|delete))"
        r"\s*\(\s*['\"](?P<name>/[^'\"]*)['\"]", re.I), 0.95),
    ("event", "produces", re.compile(
        r"\b(?:emit|publish|send_event|dispatch)\s*\(\s*['\"](?P<name>[\w.:-]+)['\"]", re.I), 0.85),
    ("event", "consumes", re.compile(
        r"\b(?:on|subscribe|consume|add_listener)\s*\(\s*['\"](?P<name>[\w.:-]+)['\"]", re.I), 0.85),
    ("database", "writes", re.compile(
        r"\b(?:insert\s+into|update|delete\s+from)\s+[`\"']?(?P<name>[A-Za-z_]\w*)", re.I), 0.9),
    ("database", "reads", re.compile(
        r"\bselect\b.+?\bfrom\s+[`\"']?(?P<name>[A-Za-z_]\w*)", re.I), 0.9),
    ("database", "model", re.compile(
        r"__tablename__\s*=\s*['\"](?P<name>[A-Za-z_]\w*)['\"]", re.I), 0.95),
    ("config", "reads", re.compile(
        r"(?:getenv|environ\.get|process\.env\.|getProperty\s*\()\s*['\"]?(?P<name>[A-Z][A-Z0-9_]{2,})", re.I), 0.8),
    ("permission", "checks", re.compile(
        r"\b(?P<name>is_admin|has_role|has_permission|authorize|authorise|can_access)\b", re.I), 0.8),
    ("external-service", "calls", re.compile(
        r"\b(?:fetch|requests\.(?:get|post|put|patch|delete)|http\.(?:get|post))\s*\(\s*['\"]"
        r"(?P<name>https?://[^/'\"]+)", re.I), 0.85),
    ("concurrency", "coordinates", re.compile(
        r"\b(?P<name>lock|mutex|semaphore|atomic|synchronized|transaction)\b", re.I), 0.7),
)


def discover_surfaces(root: Path) -> list[dict]:
    root = root.resolve()
    surfaces: dict[tuple[str, str, str, str, int], Surface] = {}
    try:
        paths = symbol_index.discover_source_paths(root, strict=True)
    except symbol_index.SourceDiscoveryError as exc:
        raise BehaviorGraphError(str(exc)) from exc
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise BehaviorGraphError(f"行为图无法读取 {relative}: {exc}") from exc
        for line_number, line in enumerate(lines, 1):
            for kind, role, pattern, confidence in PATTERNS:
                for match in pattern.finditer(line):
                    name = match.group("name").strip().lower()
                    surface = Surface(
                        kind, role, name, relative, line_number, confidence,
                        f"{relative}:{line_number}",
                    )
                    surfaces[(kind, role, name, relative, line_number)] = surface
    return [asdict(surfaces[key]) for key in sorted(surfaces)]


def build_behavior_graph(root: Path) -> dict:
    surfaces = discover_surfaces(root)
    nodes: dict[str, dict] = {}
    edges = []
    for item in surfaces:
        file_id = f"file:{item['path']}"
        behavior_id = f"behavior:{item['kind']}:{item['name']}"
        nodes[file_id] = {"id": file_id, "type": "file", "path": item["path"]}
        nodes[behavior_id] = {
            "id": behavior_id, "type": item["kind"], "name": item["name"],
        }
        if item["role"] in {"consumes", "reads", "checks"}:
            source, target = behavior_id, file_id
        else:
            source, target = file_id, behavior_id
        edges.append({
            "type": item["role"], "from": source, "to": target,
            "confidence": item["confidence"], "evidence": item["evidence"],
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "root": ".",
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": edges,
        "surfaces": surfaces,
        "summary": {
            "surfaces": len(surfaces),
            "kinds": dict(sorted(Counter(item["kind"] for item in surfaces).items())),
        },
    }


def discover_system_flows(graph: dict) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in graph.get("surfaces", []):
        grouped[(item["kind"], item["name"])].append(item)
    flows = []
    for (kind, name), items in sorted(grouped.items()):
        roles = {item["role"] for item in items}
        complete = (
            kind == "event" and {"produces", "consumes"} <= roles
            or kind == "database" and bool(roles & {"writes", "model"}) and "reads" in roles
            or kind == "config" and len({item["path"] for item in items}) >= 2
        )
        if not complete:
            continue
        flows.append({
            "id": f"S{len(flows) + 1:03d}",
            "kind": kind,
            "resource": name,
            "participants": sorted({item["path"] for item in items}),
            "roles": sorted(roles),
            "confidence": round(min(item["confidence"] for item in items), 2),
            "evidence": sorted({item["evidence"] for item in items}),
        })
    return flows


def detect_behavioral_anomalies(graph: dict) -> list[dict[str, Any]]:
    """从行为图谱推断跨模块潜在逻辑缺陷与架构盲区：
    1. unprotected_endpoint: 暴露了 HTTP 路由或数据库写操作，但在该文件中没有任何权限校验
    2. orphaned_event: 存在事件发布（produces）但在全仓无任何消费订阅（consumes）
    3. split_brain_config: 跨 3 个以上模块分散读取同一环境变量，存在默认值漂移隐患
    """
    surfaces = graph.get("surfaces", [])
    anomalies: list[dict[str, Any]] = []

    # 1. 按文件聚类分析未鉴权路由与写操作
    by_file: dict[str, list[dict]] = defaultdict(list)
    for s in surfaces:
        by_file[s["path"]].append(s)

    for path, items in sorted(by_file.items()):
        has_perm = any(item["kind"] == "permission" for item in items)
        sensitive_ops = [
            item for item in items
            if (item["kind"] == "http-route" and item["role"] == "exposes")
            or (item["kind"] == "database" and item["role"] == "writes")
        ]
        if sensitive_ops and not has_perm:
            anomalies.append({
                "kind": "unprotected_endpoint",
                "severity": "high",
                "path": path,
                "detail": f"文件 {path} 暴露了 {len(sensitive_ops)} 处路由或数据库写操作，但缺少任何权限检查调用",
                "evidence": [op["evidence"] for op in sensitive_ops[:5]],
            })

    # 2. 孤立事件推断
    event_producers: dict[str, list[dict]] = defaultdict(list)
    event_consumers: dict[str, list[dict]] = defaultdict(list)
    for s in surfaces:
        if s["kind"] == "event":
            if s["role"] == "produces":
                event_producers[s["name"]].append(s)
            elif s["role"] == "consumes":
                event_consumers[s["name"]].append(s)

    for name, producers in sorted(event_producers.items()):
        if name not in event_consumers:
            anomalies.append({
                "kind": "orphaned_event",
                "severity": "medium",
                "name": name,
                "detail": f"事件 '{name}' 由 {len(producers)} 处发布，但全仓未发现任何消费订阅者",
                "evidence": [p["evidence"] for p in producers[:5]],
            })

    # 3. 跨模块配置读取分析
    configs: dict[str, set[str]] = defaultdict(set)
    for s in surfaces:
        if s["kind"] == "config":
            configs[s["name"]].add(s["path"])

    for name, paths in sorted(configs.items()):
        if len(paths) >= 3:
            anomalies.append({
                "kind": "split_brain_config",
                "severity": "low",
                "name": name,
                "detail": f"配置项 '{name}' 被跨越 {len(paths)} 个不同模块分散独立读取，需防范默认值漂移",
                "paths": sorted(list(paths)),
            })

    return anomalies


def query_behavior_graph(
    root: Path | str = ".",
    *,
    file_filter: str | None = None,
    include_anomalies: bool = True,
) -> dict[str, Any]:
    """供 Python 模块或 MCP 工具调用的行为图谱与逻辑异常查询接口。"""
    project_root = Path(root).resolve()
    graph = build_behavior_graph(project_root)

    if file_filter:
        norm = file_filter.strip().lstrip("./").rstrip("/")

        def matches(path: str) -> bool:
            path = str(path).replace("\\", "/").lstrip("./")
            return path == norm or path.startswith(norm + "/")

        surfaces = [s for s in graph["surfaces"] if matches(s["path"])]
        allowed = {f"file:{s['path']}" for s in surfaces}
        allowed.update(f"behavior:{s['kind']}:{s['name']}" for s in surfaces)
        graph["surfaces"] = surfaces
        graph["nodes"] = [n for n in graph["nodes"] if n.get("id") in allowed]
        graph["edges"] = [
            e for e in graph["edges"]
            if e.get("from") in allowed and e.get("to") in allowed
        ]
        graph["summary"] = {
            "surfaces": len(surfaces),
            "kinds": dict(sorted(Counter(item["kind"] for item in surfaces).items())),
        }

    flows = discover_system_flows(graph)

    result: dict[str, Any] = {
        "status": "ok",
        "root": str(project_root),
        "summary": graph.get("summary", {}),
        "flows": flows,
        "nodes_count": len(graph.get("nodes", [])),
        "edges_count": len(graph.get("edges", [])),
    }
    if include_anomalies:
        anomalies = detect_behavioral_anomalies(graph)
        result["anomalies"] = anomalies
        result["anomalies_count"] = len(anomalies)

    return result


def _decode_response(response: object) -> Mapping[str, Any]:
    """把响应观察转换为有限对象；解析失败时返回空对象而不是猜状态。"""
    if isinstance(response, (bytes, bytearray)):
        try:
            response = json.loads(bytes(response).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    elif isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return {}
    return response if isinstance(response, Mapping) else {}


def _safe_state_value(value: object) -> str | None:
    """只允许短标量状态，避免把凭据、正文或大对象带入图谱。"""
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list, tuple)):
        return None
    text = str(value).strip()
    if not text or len(text) > _MAX_STATE_VALUE_LENGTH:
        return None
    if _SENSITIVE_STATE_KEY.search(text):
        return None
    return text


def _collect_state_fields(value: object, source: str, output: list[dict]) -> None:
    if len(output) >= _MAX_STATE_FIELDS or not isinstance(value, Mapping):
        return
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip().casefold()
        if key in STATE_KEYS and not _SENSITIVE_STATE_KEY.search(key):
            safe = _safe_state_value(raw_value)
            if safe is not None:
                output.append({"key": key, "value": safe, "source": f"{source}.{key}"})


def extract_response_states(response: object) -> dict:
    """从调用方提供的响应观察提取安全的状态反馈。

    该函数不发起网络请求，也不保存原始响应。支持的输入可以是 JSON 对象、
    JSON 字符串/字节，或形如 ``{"status_code": 200, "json": {...},
    "headers": {...}}`` 的观察记录。
    """
    record = _decode_response(response)
    status_code = None
    for key in ("status_code", "statusCode", "http_status"):
        candidate = record.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and 100 <= candidate <= 599:
            status_code = candidate
            break
    if status_code is None and isinstance(record.get("status"), int):
        candidate = record["status"]
        if 100 <= candidate <= 599:
            status_code = candidate

    fields: list[dict] = []
    _collect_state_fields(record, "response", fields)
    body = record.get("json")
    if isinstance(body, Mapping):
        _collect_state_fields(body, "json", fields)
    body = record.get("body")
    if isinstance(body, Mapping):
        _collect_state_fields(body, "body", fields)
    headers = record.get("headers")
    if isinstance(headers, Mapping):
        for raw_key, raw_value in headers.items():
            key = str(raw_key).strip().casefold()
            if key in STATE_KEYS or any(marker in key for marker in ("state", "step", "role", "permission", "phase")):
                if _SENSITIVE_STATE_KEY.search(key):
                    continue
                safe = _safe_state_value(raw_value)
                if safe is not None:
                    fields.append({"key": key, "value": safe, "source": f"headers.{key}"})
    unique = {(item["key"], item["value"], item["source"]): item for item in fields}
    states = [unique[key] for key in sorted(unique)]
    fingerprint_input = {"status_code": status_code, "states": states}
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "schema_version": STATE_MATRIX_SCHEMA_VERSION,
        "status_code": status_code,
        "states": states,
        "state_fingerprint": fingerprint,
    }


def _observation_operation(observation: Mapping[str, Any]) -> str:
    raw = observation.get("operation") or observation.get("request") or observation.get("route")
    if isinstance(raw, Mapping):
        raw = raw.get("operation") or raw.get("path") or raw.get("route")
    text = str(raw or "unknown").strip()
    return text[:_MAX_STATE_VALUE_LENGTH] or "unknown"


def build_state_transition_matrix(observations: Sequence[object]) -> dict:
    """按 trace 顺序把响应状态转换为可审计的迁移候选矩阵。"""
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes, bytearray)):
        raise BehaviorGraphError("observations 必须是响应观察数组")
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    previous: dict[str, list[str]] = {}
    valid = 0
    for index, item in enumerate(observations):
        if not isinstance(item, Mapping):
            continue
        trace = str(item.get("trace_id") or item.get("trace") or "default")[:_MAX_STATE_VALUE_LENGTH]
        operation = _observation_operation(item)
        extracted = extract_response_states(item.get("response", item))
        states = extracted["states"]
        operation_id = f"operation:{operation}"
        nodes[operation_id] = {"id": operation_id, "type": "operation", "name": operation}
        current_ids = []
        for state in states:
            state_id = f"state:{state['key']}={state['value']}"
            nodes[state_id] = {
                "id": state_id, "type": "state", "key": state["key"], "value": state["value"],
            }
            current_ids.append(state_id)
        sources = previous.get(trace) or ["state:START"]
        nodes.setdefault("state:START", {"id": "state:START", "type": "state", "value": "START"})
        for source in sources:
            edge_key = (source, operation_id, "transition")
            edge = edges.setdefault(edge_key, {"from": source, "to": operation_id, "type": "transition", "count": 0, "evidence": []})
            edge["count"] += 1
            edge["evidence"].append(index)
        for target in current_ids:
            edge_key = (operation_id, target, "emits")
            edge = edges.setdefault(edge_key, {"from": operation_id, "to": target, "type": "emits", "count": 0, "evidence": []})
            edge["count"] += 1
            edge["evidence"].append(index)
        if current_ids:
            previous[trace] = current_ids
        valid += 1
    return {
        "schema_version": STATE_MATRIX_SCHEMA_VERSION,
        "observations": valid,
        "nodes": [nodes[key] for key in sorted(nodes)],
        "transitions": [edges[key] for key in sorted(edges)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "flows", "state-matrix"), nargs="?", default="scan")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--input", type=Path, help="state-matrix 使用的 JSON 观察数组")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "state-matrix":
            if args.input is None or not args.input.is_file():
                raise BehaviorGraphError("state-matrix 必须提供存在的 --input JSON 文件")
            observations = json.loads(args.input.read_text(encoding="utf-8"))
            payload = build_state_transition_matrix(observations)
        else:
            graph = build_behavior_graph(args.root)
            payload = graph if args.command == "scan" else {
                "schema_version": SCHEMA_VERSION,
                "flows": discover_system_flows(graph),
                "surface_summary": graph["summary"],
            }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            count = len(payload.get("flows", payload.get("surfaces", [])))
            print(f"[behavior_graph] {args.command}: {count}")
        return 0
    except (BehaviorGraphError, OSError) as exc:
        print(f"[behavior_graph] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
