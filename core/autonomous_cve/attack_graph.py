#!/usr/bin/env python3
"""core/attack_graph.py - BugX 统一动态攻击面图谱 (Living Attack Graph) (DOC-125)。

提供：
  - 动态攻击面图谱节点 (roles, routes, parameters, sinks, hypotheses) 与边 (edges) 维护
  - JSON Schema 严格校验 (attack-graph.schema.json)
  - 非法访问边 (Illegal Access Edge) 结构化检索与审计证明
  - 针对阶段 Harness 的有界上下文切片抽取 (Attack Graph Slicing, ≤200T)
  - 假设驱动的反思回溯状态推进 (Reflexive Backtracking)
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
import warnings
from pathlib import Path
from typing import Any, Mapping, Iterable

try:
    import jsonschema
except ImportError:
    jsonschema = None  # type: ignore[assignment]

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "attack-graph.schema.json"

ILLEGAL_ACCESS_RELATIONS = {
    "EXPLOITED_ACCESS",
    "UNAUTHORIZED_CROSS_TENANT",
    "IDOR_ACCESS",
    "PRIVILEGE_ESCALATION",
    "UNAUTHENTICATED_ACCESS",
}

# 与 attack-graph.schema.json 的 required / additionalProperties 约束一一对应，
# 用于 jsonschema 缺失（可选依赖，见 pyproject.toml）时的内置轻量校验。
_NODE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "roles": ("id", "type"),
    "routes": ("id", "method", "auth_required"),
    "parameters": ("id", "name"),
    "sinks": ("id", "kind"),
    "hypotheses": ("id", "type", "target", "status"),
}
_TOP_LEVEL_KEYS = frozenset({
    "version", "nodes", "edges", "meta", "events", "reflex",
})
# DOC-125 闭环：``meta`` / ``events`` / ``reflex`` 是后来追加的**可选**顶层持久化
# 字段（事件日志、反思回溯历史、快照）。历史快照里没有它们也必须能读回来，
# 因此「允许的键」与「必需的键」必须分开，否则旧快照会被内置校验判为损坏。
_REQUIRED_TOP_LEVEL_KEYS = frozenset({"version", "nodes", "edges"})
_OPTIONAL_TOP_LEVEL_KEYS = frozenset({"meta", "events", "reflex"})
# 事件日志与回溯记录的硬上限：图谱每轮都要整体原子重写，无界增长会把
# Session 级快照变成 MB 级热点文件。
MAX_EVENT_JOURNAL = 500
MAX_ROLLBACK_LOG = 50
MAX_PERSISTED_SNAPSHOTS = 5


def _validate_structure_builtin(payload: Mapping[str, Any]) -> None:
    """内置轻量结构校验（jsonschema 不可用时的 fail-closed 替代）。

    只覆盖 Schema 中真正带约束的部分：顶层 required / additionalProperties、
    节点与边的 required 字段及其基本类型。校验失败一律抛 ``ValueError``，
    使 ``validate()`` 在缺少可选依赖时仍然「失败即拒绝」，而不是静默放行。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("攻击图谱必须是 JSON 对象")
    unknown = set(payload) - _TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"攻击图谱存在 Schema 未允许的顶层字段: {sorted(unknown)}")
    missing = _REQUIRED_TOP_LEVEL_KEYS - set(payload)
    if missing:
        raise ValueError(f"攻击图谱缺少必需顶层字段: {sorted(missing)}")
    if not isinstance(payload.get("version"), str):
        raise ValueError("攻击图谱 version 必须是字符串")
    # 可选持久化字段：存在就必须是正确形状（缺失等价于「无历史」，不报错）。
    if "meta" in payload and not isinstance(payload["meta"], Mapping):
        raise ValueError("攻击图谱 meta 必须是对象")
    if "events" in payload and not isinstance(payload["events"], list):
        raise ValueError("攻击图谱 events 必须是数组")
    if "reflex" in payload and not isinstance(payload["reflex"], Mapping):
        raise ValueError("攻击图谱 reflex 必须是对象")
    nodes = payload.get("nodes")
    if not isinstance(nodes, Mapping):
        raise ValueError("攻击图谱 nodes 必须是对象")
    for key, required in _NODE_REQUIRED_FIELDS.items():
        entries = nodes.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"攻击图谱 nodes.{key} 必须是数组")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError(f"攻击图谱 nodes.{key} 的元素必须是对象")
            absent = [field for field in required if field not in entry]
            if absent:
                raise ValueError(
                    f"攻击图谱 nodes.{key} 元素缺少必需字段: {absent}"
                )
    edges = payload.get("edges")
    if not isinstance(edges, list):
        raise ValueError("攻击图谱 edges 必须是数组")
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise ValueError("攻击图谱 edges 的元素必须是对象")
        absent = [field for field in ("from", "to", "relation") if field not in edge]
        if absent:
            raise ValueError(f"攻击图谱 edges 元素缺少必需字段: {absent}")


class StreamingEventBus:
    """DOC-125 优化项 1: 内存级真流式事件总线 (In-Memory Streaming Event Bus)。

    支持事件订阅与即刻发布，实现资产发现与漏洞验证零批次延迟流水线推进 (Pipelining)。
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Any]] = {}
        self.event_history: list[dict[str, Any]] = []

    def subscribe(self, event_type: str, callback: Any) -> None:
        """注册事件监听器。"""
        self._listeners.setdefault(event_type, []).append(callback)

    def publish(self, event_type: str, payload: Mapping[str, Any]) -> list[Any]:
        """发布事件并即刻调用所有已注册监听器。"""
        event_record = {
            "event_type": event_type,
            "payload": dict(payload),
            "timestamp": time.time(),
        }
        self.event_history.append(event_record)
        if len(self.event_history) > MAX_EVENT_JOURNAL:
            del self.event_history[:-MAX_EVENT_JOURNAL]
        results = []
        for listener in self._listeners.get(event_type, []):
            try:
                res = listener(dict(payload))
                results.append(res)
            except Exception as exc:
                results.append({"error": str(exc)})
        return results

    def clear(self) -> None:
        """清理所有监听器与历史。"""
        self._listeners.clear()
        self.event_history.clear()


class LivingAttackGraph:
    """BugX 统一动态攻击面图谱。"""

    def __init__(self, version: str = "1.0.0") -> None:
        self.version = version
        self.roles: dict[str, dict[str, Any]] = {}
        self.routes: dict[str, dict[str, Any]] = {}
        self.parameters: dict[str, dict[str, Any]] = {}
        self.sinks: dict[str, dict[str, Any]] = {}
        self.hypotheses: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.event_bus = StreamingEventBus()
        self._snapshots: dict[str, dict[str, Any]] = {}
        # --- DOC-125 持久化状态（随 to_dict/from_dict 往返，跨 Round 存活）---
        # meta：图谱级观测数据（schema 降级事实、单元归属、计数器、轮次水位）。
        self.meta: dict[str, Any] = {}
        # events：StreamingEventBus 的落盘事件日志（总线本身是内存态，不持久）。
        self.events: list[dict[str, Any]] = []
        # reflex：回溯历史 + 回滚审计 + 待重放计划；快照拓扑另存 _snapshots。
        self.reflex: dict[str, Any] = {
            "backtrack_history": {},
            "rollbacks": [],
            "replay_pending": [],
            "auto_fix_suggestions": [],
        }
        # 路径遍历搜索记忆化缓存 (DOC-137 2.0 / D137-03)
        self._paths_cache: dict[tuple[str, str | None, int], list[list[dict[str, Any]]]] = {}

    def validate(self) -> None:
        """执行 Schema 校验（DES-10：校验不可用时显式降级，绝不静默放行）。

        ``jsonschema`` 是可选依赖，未安装时旧实现直接 ``return``，导致 DOC-125
        的 schema 契约在生产环境从不被校验（死代码）。现在：

        - 依赖与 schema 文件齐备时，走完整 JSON Schema 严格校验；
        - 否则发出 ``RuntimeWarning`` 显式降级，并回落到内置轻量结构校验，
          对违反 Schema 必需字段的结构**失败即拒绝**（抛 ``ValueError``）。

        选择「告警 + 内置 fail-closed」而非直接抛错，是因为直接抛错会把可选
        依赖的缺失变成每一次合法图谱写入的硬失败（含本仓库无 jsonschema 的
        测试环境）；而内置校验已覆盖 Schema 的 required 约束，信任边界仍然
        被守住，同时不会把可选依赖变成事实上的强依赖。
        """
        payload = self.to_dict()
        if jsonschema is None or not SCHEMA_PATH.is_file():
            reason = (
                "未安装 jsonschema 依赖"
                if jsonschema is None
                else f"Schema 文件缺失: {SCHEMA_PATH}"
            )
            # DES-10/C125-13：只 warnings.warn 的话，降级在生产环境里无人可见——
            # Worker 的 stderr 不会进证据链。把事实同时写进 meta，使「严格校验是否
            # 真的跑过」可以从持久化快照里被审计出来。
            self.meta["schema_validation"] = {
                "mode": "builtin_degraded",
                "strict_json_schema": False,
                "jsonschema_available": jsonschema is not None,
                "schema_path": str(SCHEMA_PATH),
                "reason": reason,
                "validated_at": time.time(),
            }
            warnings.warn(
                f"攻击图谱 Schema 校验已降级（{reason}）：改用内置轻量结构校验，"
                "严格 JSON Schema 约束未被执行。",
                RuntimeWarning,
                stacklevel=2,
            )
            _validate_structure_builtin(payload)
            return
        with open(SCHEMA_PATH, "r", encoding="utf-8") as sf:
            schema = json.load(sf)
        jsonschema.validate(instance=payload, schema=schema)
        self.meta["schema_validation"] = {
            "mode": "jsonschema",
            "strict_json_schema": True,
            "jsonschema_available": True,
            "schema_path": str(SCHEMA_PATH),
            "validated_at": time.time(),
        }

    def add_role(self, role_id: str, role_type: str, **attrs: Any) -> dict[str, Any]:
        """添加或更新身份角色节点 (例如 role:admin, role:user_a)。"""
        node = {"id": role_id, "type": role_type, **attrs}
        self.roles[role_id] = node
        if hasattr(self, "event_bus"):
            self.event_bus.publish("node_added", node)
        return node

    def add_route(self, route_id: str, method: str, auth_required: bool, **attrs: Any) -> dict[str, Any]:
        """添加或更新 API 路由端点节点 (例如 route:/api/v1/orders/{id})。"""
        node = {"id": route_id, "method": method.upper(), "auth_required": bool(auth_required), **attrs}
        self.routes[route_id] = node
        if hasattr(self, "event_bus"):
            self.event_bus.publish("node_added", node)
        return node

    def add_parameter(self, param_id: str, name: str, **attrs: Any) -> dict[str, Any]:
        """添加或更新参数节点 (例如 param:order_id)。"""
        node = {"id": param_id, "name": name, **attrs}
        self.parameters[param_id] = node
        return node

    def add_sink(self, sink_id: str, kind: str, **attrs: Any) -> dict[str, Any]:
        """添加或更新危险汇聚点节点 (例如 sink:sql_query, sink:cmd_exec)。"""
        node = {"id": sink_id, "kind": kind, **attrs}
        self.sinks[sink_id] = node
        if hasattr(self, "_paths_cache"):
            self._paths_cache.clear()
        return node

    def add_hypothesis(
        self,
        hypothesis_id: str,
        hyp_type: str,
        target: str,
        status: str = "proposed",
        **attrs: Any,
    ) -> dict[str, Any]:
        """添加攻防假设节点 (例如 hypo:idor_orders)。"""
        node = {
            "id": hypothesis_id,
            "type": hyp_type,
            "target": target,
            "status": status,
            **attrs,
        }
        self.hypotheses[hypothesis_id] = node
        if hasattr(self, "event_bus"):
            self.event_bus.publish("hypothesis_added", node)
        return node

    def update_hypothesis_status(self, hypothesis_id: str, status: str, reason: str = "") -> None:
        """推进或回溯假设状态 (proposed -> testing -> verified / refuted / not_applicable)。"""
        if hypothesis_id in self.hypotheses:
            self.hypotheses[hypothesis_id]["status"] = status
            if reason:
                self.hypotheses[hypothesis_id]["status_reason"] = reason

    def add_edge(
        self,
        source: str,
        target: str,
        relation: str,
        hypothesis_id: str | None = None,
    ) -> dict[str, Any]:
        """添加实体间的拓扑关系边。"""
        edge = {"from": source, "to": target, "relation": relation}
        if hypothesis_id:
            edge["hypothesis_id"] = hypothesis_id
        # 避免重复边
        for existing in self.edges:
            if (
                existing.get("from") == source
                and existing.get("to") == target
                and existing.get("relation") == relation
                and existing.get("hypothesis_id") == hypothesis_id
            ):
                return existing
        self.edges.append(edge)
        if hasattr(self, "_paths_cache"):
            self._paths_cache.clear()
        if hasattr(self, "event_bus"):
            self.event_bus.publish("edge_added", edge)
        return edge

    def get_illegal_access_edges(self) -> list[dict[str, Any]]:
        """获取所有已实锤的非法越权边 (Illegal Access Edges)。"""
        return [e for e in self.edges if e.get("relation") in ILLEGAL_ACCESS_RELATIONS]

    def find_attack_paths(
        self,
        source_id: str,
        target_id: str | None = None,
        max_depth: int = 5,
    ) -> list[list[dict[str, Any]]]:
        """DOC-125 优化项 2: 多跳攻击路径拓扑求解器 (Multi-Hop Path Traversal)。

        求解从起点 (如 role:user_b) 出发，到达目标 (如 sink:sql 或 route:xxx)
        的所有可行攻击链路，自动剪枝防环路，深度限制 <= max_depth。
        使用 _paths_cache 进行路径遍历记忆化缓存 (DOC-137 2.0 / D137-03)。
        """
        cache_key = (source_id, target_id, max_depth)
        if hasattr(self, "_paths_cache") and cache_key in self._paths_cache:
            return copy.deepcopy(self._paths_cache[cache_key])

        paths: list[list[dict[str, Any]]] = []
        adj: dict[str, list[dict[str, Any]]] = {}
        for edge in self.edges:
            src = edge.get("from")
            if src:
                adj.setdefault(src, []).append(edge)

        def dfs(curr: str, visited: set[str], current_path: list[dict[str, Any]]) -> None:
            if len(current_path) >= max_depth:
                return
            for edge in adj.get(curr, []):
                dst = edge.get("to")
                if not dst or dst in visited:
                    continue
                next_path = current_path + [edge]
                is_hit = False
                if target_id is not None:
                    if dst == target_id or (target_id in ("sink:", "sink:*") and dst in self.sinks):
                        is_hit = True
                else:
                    if dst in self.sinks or edge.get("relation") in ILLEGAL_ACCESS_RELATIONS:
                        is_hit = True

                if is_hit:
                    paths.append(next_path)
                visited.add(dst)
                dfs(dst, visited, next_path)
                visited.remove(dst)

        dfs(source_id, {source_id}, [])
        if hasattr(self, "_paths_cache"):
            self._paths_cache[cache_key] = copy.deepcopy(paths)
        return copy.deepcopy(paths)

    def find_cross_tenant_paths(self, role_a: str, role_b: str) -> list[dict[str, Any]]:
        """求解角色 A 是否存在未经授权访问角色 B 所属私有资源的边。"""
        owned_by_b = {
            e["to"] for e in self.edges
            if e.get("from") == role_b and "LEGITIMATE" in e.get("relation", "")
        }
        cross = [
            e for e in self.edges
            if (
                e.get("from") == role_a
                and e.get("to") in owned_by_b
                and e.get("relation") in ILLEGAL_ACCESS_RELATIONS
            )
        ]
        return cross

    def create_snapshot(self, tag: str = "") -> str:
        """DOC-125 优化项 9: 创建图谱拓扑增量快照 (Incremental Snapshot)。"""
        ts = time.time()
        snapshot_id = f"snap_{int(ts * 1000)}_{len(self._snapshots) + 1}"
        if len(self._snapshots) >= 10:
            oldest_key = next(iter(self._snapshots))
            del self._snapshots[oldest_key]

        self._snapshots[snapshot_id] = {
            "snapshot_id": snapshot_id,
            "tag": tag or f"snapshot_{snapshot_id}",
            "timestamp": ts,
            "roles": copy.deepcopy(self.roles),
            "routes": copy.deepcopy(self.routes),
            "parameters": copy.deepcopy(self.parameters),
            "sinks": copy.deepcopy(self.sinks),
            "hypotheses": copy.deepcopy(self.hypotheses),
            "edges": copy.deepcopy(self.edges),
        }
        return snapshot_id

    def rollback_to_snapshot(self, snapshot_id: str) -> bool:
        """DOC-125 优化项 9: 时间旅行回滚至指定快照。"""
        if snapshot_id not in self._snapshots:
            return False
        snap = self._snapshots[snapshot_id]
        self.roles = copy.deepcopy(snap["roles"])
        self.routes = copy.deepcopy(snap["routes"])
        self.parameters = copy.deepcopy(snap["parameters"])
        self.sinks = copy.deepcopy(snap["sinks"])
        self.hypotheses = copy.deepcopy(snap["hypotheses"])
        self.edges = copy.deepcopy(snap["edges"])
        if hasattr(self, "_paths_cache"):
            self._paths_cache.clear()
        if hasattr(self, "event_bus"):
            self.event_bus.publish("snapshot_rollback", {"snapshot_id": snapshot_id, "tag": snap.get("tag", "")})
        return True

    def list_snapshots(self) -> list[dict[str, Any]]:
        """DOC-125 优化项 9: 列出当前保留的快照列表。"""
        return [
            {
                "snapshot_id": s["snapshot_id"],
                "tag": s.get("tag", ""),
                "timestamp": s["timestamp"],
                "roles_count": len(s["roles"]),
                "routes_count": len(s["routes"]),
                "edges_count": len(s["edges"]),
            }
            for s in self._snapshots.values()
        ]

    def find_entrypoints_for_sink(
        self,
        sink_id: str,
        max_depth: int = 5,
    ) -> list[dict[str, Any]]:
        """DOC-125 优化项 11: 高危汇点 (Sink) 反向污点入口推演。

        从危险汇聚点 (如 sink:sql_query, sink:cmd_exec) 出发逆向拓扑回溯，
        求解所有可达该汇点的候选入口端点 (route:*) 与参数 (param:*)。
        """
        in_adj: dict[str, list[dict[str, Any]]] = {}
        for edge in self.edges:
            dst = edge.get("to")
            if dst:
                in_adj.setdefault(dst, []).append(edge)

        entrypoints: list[dict[str, Any]] = []
        visited: set[str] = set()

        def backward_dfs(curr: str, depth: int, path: list[dict[str, Any]]) -> None:
            if depth >= max_depth:
                return
            for edge in in_adj.get(curr, []):
                src = edge.get("from")
                if not src or src in visited:
                    continue
                visited.add(src)
                new_path = [edge] + path
                if src.startswith("route:") or src in self.routes:
                    entrypoints.append({
                        "entrypoint": src,
                        "sink_id": sink_id,
                        "distance": depth + 1,
                        "path": new_path,
                    })
                backward_dfs(src, depth + 1, new_path)
                visited.remove(src)

        backward_dfs(sink_id, 0, [])
        return entrypoints

    def to_dict(self) -> dict[str, Any]:
        """导出为符合 Schema 的标准字典。

        DOC-125 闭环：``meta`` / ``events`` / ``reflex`` 让图谱从「一次性结构」变成
        「跨 Round 累积的会话级状态」——事件日志、单元归属与回溯历史必须一起落盘，
        否则消费者读到的永远是空图。``reflex.snapshots`` 承接 ``create_snapshot``
        的拓扑快照，使 ``rollback_to_snapshot`` 在重新 load 之后仍然可用。
        """
        return {
            "version": self.version,
            "nodes": {
                "roles": copy.deepcopy(list(self.roles.values())),
                "routes": copy.deepcopy(list(self.routes.values())),
                "parameters": copy.deepcopy(list(self.parameters.values())),
                "sinks": copy.deepcopy(list(self.sinks.values())),
                "hypotheses": copy.deepcopy(list(self.hypotheses.values())),
            },
            "edges": copy.deepcopy(self.edges),
            "meta": copy.deepcopy(self.meta),
            "events": copy.deepcopy(self.events[-MAX_EVENT_JOURNAL:]),
            "reflex": {
                **{
                    key: value
                    for key, value in self.reflex.items()
                    if key != "snapshots"
                },
                "snapshots": copy.deepcopy(
                    dict(list(self._snapshots.items())[-MAX_PERSISTED_SNAPSHOTS:])
                ),
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LivingAttackGraph:
        """从字典反序列化图谱。"""
        graph = cls(version=str(data.get("version") or "1.0.0"))
        nodes = data.get("nodes") or {}
        for r in nodes.get("roles") or []:
            graph.add_role(r["id"], r.get("type", "unknown"), **{k: v for k, v in r.items() if k not in ("id", "type")})
        for r in nodes.get("routes") or []:
            graph.add_route(r["id"], r.get("method", "GET"), r.get("auth_required", False), **{k: v for k, v in r.items() if k not in ("id", "method", "auth_required")})
        for p in nodes.get("parameters") or []:
            graph.add_parameter(p["id"], p.get("name", ""), **{k: v for k, v in p.items() if k not in ("id", "name")})
        for s in nodes.get("sinks") or []:
            graph.add_sink(s["id"], s.get("kind", ""), **{k: v for k, v in s.items() if k not in ("id", "kind")})
        for h in nodes.get("hypotheses") or []:
            graph.add_hypothesis(h["id"], h.get("type", ""), h.get("target", ""), h.get("status", "proposed"), **{k: v for k, v in h.items() if k not in ("id", "type", "target", "status")})
        for e in data.get("edges") or []:
            graph.add_edge(e["from"], e["to"], e["relation"], e.get("hypothesis_id"))
        meta = data.get("meta")
        if isinstance(meta, Mapping):
            graph.meta = copy.deepcopy(dict(meta))
        events = data.get("events")
        if isinstance(events, list):
            graph.events = copy.deepcopy(
                [item for item in events if isinstance(item, Mapping)]
            )
        reflex = data.get("reflex")
        if isinstance(reflex, Mapping):
            restored = copy.deepcopy(dict(reflex))
            snapshots = restored.pop("snapshots", None)
            if isinstance(snapshots, Mapping):
                # 只接受形状完整的快照，避免手工篡改的快照把回滚变成 KeyError。
                graph._snapshots = {
                    str(key): value
                    for key, value in snapshots.items()
                    if isinstance(value, Mapping)
                    and all(
                        field in value
                        for field in ("snapshot_id", "roles", "routes", "sinks",
                                      "hypotheses", "edges")
                    )
                }
            defaults = graph.reflex
            for key, value in defaults.items():
                if key not in restored:
                    restored[key] = value
            graph.reflex = restored
        return graph


def evaluate_runnable_tasks(
    graph: LivingAttackGraph,
    completed_stages: set[str] | None = None,
) -> list[dict[str, Any]]:
    """DOC-125: 流式 DAG 任务求值器 (Streaming DAG Evaluator)。

    根据图谱当前节点状态（routes, roles, parameters, hypotheses）以及已完成阶段，
    计算出所有前置依赖满足的最小可并行执行任务清单。

    调度逻辑：
    1. 若 00-scope 未就绪（completed_stages 为空且无 routes），产生 00-scope 资产发现任务；
    2. 对于公共端点 (auth_required == False)：
       - 若未完成 input-output 测试，立即生成 40-input-output 任务 (affinity="exploit")；
    3. 对于受保护鉴权端点 (auth_required == True)：
       - 若尚未获取至少 2 个有效角色凭证 (len(roles) < 2 且 20-auth-session 未完成)：生成 20-auth-session 任务 (affinity="auth")；
       - 若已有有效身份凭证：生成 30-authz-business 任务 (affinity="logic")；
    4. 对于含状态异步/并发特征的端点：生成 50-state-async 任务 (affinity="logic")；
    5. 全局收敛栅栏 (80-evidence-regression)：
       - 必须在所有已发现端点已测试，且存在已实锤/待验证假设时，放行最终红绿对照复现任务 (affinity="exploit")。
    """
    completed = set(completed_stages or [])
    tasks: list[dict[str, Any]] = []

    # 1. 资产侦察流
    if "00-scope" not in completed and not graph.routes:
        tasks.append({
            "task_id": "task:00-scope",
            "stage_id": "00-scope",
            "affinity": "recon",
            "tier": "FAST",
            "description": "界定审计范围与资产边界清单",
        })
        return tasks

    # 2. 流式端点分流派发
    has_credentials = len(graph.roles) >= 2 or "20-auth-session" in completed
    untested_routes = 0

    for route_id, route in graph.routes.items():
        tested_stages = set(route.get("tested_stages") or [])
        auth_req = route.get("auth_required", False)

        if not auth_req:
            # 公共端点流 -> 40-input-output
            if "40-input-output" not in tested_stages:
                untested_routes += 1
                tasks.append({
                    "task_id": f"task:input-output:{route_id}",
                    "stage_id": "40-input-output",
                    "target_route": route_id,
                    "affinity": "exploit",
                    "tier": "PRO",
                    "description": f"公共端点参数与注入挖掘: {route.get('method', 'GET')} {route_id}",
                })
        else:
            # 鉴权端点流
            if not has_credentials:
                if not any(t["stage_id"] == "20-auth-session" for t in tasks):
                    tasks.append({
                        "task_id": "task:20-auth-session",
                        "stage_id": "20-auth-session",
                        "affinity": "auth",
                        "tier": "BALANCED",
                        "description": "提取多角色凭证矩阵与 Session 状态机",
                    })
            else:
                if "30-authz-business" not in tested_stages:
                    untested_routes += 1
                    tasks.append({
                        "task_id": f"task:authz:{route_id}",
                        "stage_id": "30-authz-business",
                        "target_route": route_id,
                        "affinity": "logic",
                        "tier": "PRO",
                        "description": f"鉴权端点越权/IDOR/跨租户推演: {route.get('method', 'GET')} {route_id}",
                    })

        # 异步状态特征流
        if route.get("has_async_state") and "50-state-async" not in tested_stages and "30-authz-business" in tested_stages:
            tasks.append({
                "task_id": f"task:state-async:{route_id}",
                "stage_id": "50-state-async",
                "target_route": route_id,
                "affinity": "logic",
                "tier": "PRO",
                "description": f"异步并发条件竞争与状态机测试: {route.get('method', 'GET')} {route_id}",
            })

    # 3. 阶段栅栏：80-evidence-regression 全局硬收敛
    verified_hypotheses = [h for h in graph.hypotheses.values() if h.get("status") == "verified"]
    if untested_routes == 0 and verified_hypotheses and "80-evidence-regression" not in completed:
        tasks.append({
            "task_id": "task:80-evidence-regression",
            "stage_id": "80-evidence-regression",
            "affinity": "exploit",
            "tier": "PRO",
            "barrier": True,
            "description": f"收敛栅栏放行：对 {len(verified_hypotheses)} 个实锤漏洞执行确定性红绿回放与负向对照验证",
        })

    # 4. DOC-125 优化项 11: 高危汇点 (Sink) 反向污点推演与任务优先级动态加权
    high_risk_routes: set[str] = set()
    for s_id, s_node in graph.sinks.items():
        kind = str(s_node.get("kind", "")).lower()
        if any(k in kind for k in ("sql", "rce", "cmd", "exec", "file", "ssrf")):
            for ep in graph.find_entrypoints_for_sink(s_id):
                high_risk_routes.add(ep["entrypoint"])

    for t in tasks:
        r_id = t.get("target_route")
        pri = 10
        if t.get("barrier"):
            pri = 1000  # 收敛栅栏绝对优先
        elif r_id and r_id in high_risk_routes:
            pri = 100   # 可达高危汇点端点享有最高优先级
        elif t.get("stage_id") == "30-authz-business":
            pri = 50    # 鉴权/越权推演次优
        elif t.get("stage_id") == "20-auth-session":
            pri = 60    # 凭证矩阵优先获取
        elif t.get("stage_id") == "00-scope":
            pri = 20
        t["priority"] = pri

    # 按 priority 降序排列 (最高优先级任务排在队首)
    tasks.sort(key=lambda x: x.get("priority", 0), reverse=True)
    return tasks


class ReflexiveBacktrackingEngine:
    """DOC-125: 假设驱动的反思回溯验证引擎 (Reflexive Backtracking Engine)。

    当 Worker 沙箱验证遇到 403 签名错误、401 凭证过期或 429 限流等阻碍时，
    驱动假设状态机反思修正，定向反查代码，重新构造请求（最大深度 <= 2）。
    """

    MAX_BACKTRACK_DEPTH = 2

    STATUS_PROPOSED = "proposed"
    STATUS_TESTING = "testing"
    STATUS_BLOCKED = "blocked"
    STATUS_BACKTRACKING = "backtracking"
    STATUS_SUSPECTED = "suspected"
    STATUS_VERIFIED = "verified"
    STATUS_REFUTED = "refuted"
    STATUS_NOT_APPLICABLE = "not_applicable"

    BLOCKED_403_SIGNATURE = "HTTP_403_SIGNATURE_MISMATCH"
    BLOCKED_401_TOKEN = "HTTP_401_TOKEN_EXPIRED"
    BLOCKED_429_RATE_LIMIT = "HTTP_429_RATE_LIMITED"
    BLOCKED_WAF = "WAF_PAYLOAD_DETECTED"

    def __init__(
        self,
        graph: LivingAttackGraph,
        *,
        secret_key: str | None = None,
        available_tokens: Iterable[str] | None = None,
    ) -> None:
        self.graph = graph
        self.secret_key = secret_key.strip() if isinstance(secret_key, str) and secret_key.strip() else None
        self.available_tokens = tuple(
            token.strip() for token in (available_tokens or ())
            if isinstance(token, str) and token.strip()
        )
        # 记录各假设的回溯追踪: hypothesis_id -> list[attempt_record]
        # DOC-125 闭环：这里**直接别名**到 graph.reflex，而不是自建一份内存字典。
        # 否则 record_attempt 的历史随进程结束蒸发，回溯深度在第 2 次尝试时被重置，
        # MAX_BACKTRACK_DEPTH 门禁跨 Round 失效，消费者也永远读不到 backtrack_history。
        history = graph.reflex.setdefault("backtrack_history", {})
        if not isinstance(history, dict):
            history = {}
            graph.reflex["backtrack_history"] = history
        self.backtrack_history = history

    def start_testing(self, hypothesis_id: str) -> None:
        """开始测试假设。"""
        self.graph.update_hypothesis_status(hypothesis_id, self.STATUS_TESTING, "开始执行沙箱测试")

    def build_dpo_pair(self, hypothesis_id: str) -> dict[str, Any] | None:
        """DOC-126 DES-14: 合成反思回溯成功假设的 DPO 偏好对 (prompt, chosen, rejected)。"""
        history = self.backtrack_history.get(hypothesis_id, [])
        if len(history) < 2:
            return None
        if not history[-1].get("is_exploit_successful"):
            return None
        hypothesis = self.graph.hypotheses.get(hypothesis_id, {})
        prompt = hypothesis.get("description") or f"Audit exploit for {hypothesis_id}"
        chosen = [history[-1]]
        rejected = history[:-1]
        obstacle_types = []
        for r in rejected:
            reason = str(r.get("blocked_reason") or "")
            if reason and reason not in obstacle_types:
                obstacle_types.append(reason)
        has_neg = any(h.get("has_negative_control") for h in history)
        try:
            from core.cloud_sync.flywheel_client import build_dpo_preference_pair
            return build_dpo_preference_pair(
                prompt=prompt,
                chosen=chosen,
                rejected=rejected,
                metadata={
                    "hypothesis_id": hypothesis_id,
                    "target_route": hypothesis.get("target_route"),
                    "vulnerability_type": hypothesis.get("vulnerability_type"),
                    "backtrack_depth": len(history),
                    "has_negative_control": has_neg,
                    "obstacle_types": obstacle_types,
                },
            )
        except Exception:
            return {
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected,
                "metadata": {
                    "hypothesis_id": hypothesis_id,
                    "has_negative_control": has_neg,
                    "obstacle_types": obstacle_types,
                },
            }

    def record_attempt(
        self,
        hypothesis_id: str,
        *,
        status_code: int,
        response_body: str = "",
        is_exploit_successful: bool = False,
        has_negative_control: bool = False,
        request_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记录一次测试尝试结果，并判定是否进入反思回溯、实锤或阻断。"""
        history = self.backtrack_history.setdefault(hypothesis_id, [])
        depth = len(history)

        record = {
            "attempt_index": depth + 1,
            "status_code": status_code,
            "body_snippet": response_body[:200],
            "is_exploit_successful": is_exploit_successful,
            "has_negative_control": has_negative_control,
            "request_context": dict(request_context or {}),
        }
        history.append(record)

        # 1. 验证实锤分支：利用成功且具备负向对照。单独的 2xx
        # 响应只能说明请求成功，不能证明漏洞成立。
        if is_exploit_successful and has_negative_control:
            self.graph.update_hypothesis_status(
                hypothesis_id,
                self.STATUS_VERIFIED,
                reason="利用成功并完成对照验证",
            )
            dpo_pair = self.build_dpo_pair(hypothesis_id)
            res = {
                "status": self.STATUS_VERIFIED,
                "action": "commit_evidence",
                "current_depth": depth + 1,
                "advice": "漏洞实锤成立！请提交正向利用证据与负向对照报文。",
            }
            if dpo_pair:
                res["dpo_preference_pair"] = dpo_pair
            return res
        if is_exploit_successful:
            self.graph.update_hypothesis_status(
                hypothesis_id,
                self.STATUS_SUSPECTED,
                reason="正向请求成功，但尚未完成负向对照验证",
            )
            return {
                "status": self.STATUS_SUSPECTED,
                "action": "hold_for_negative_control",
                "current_depth": depth + 1,
                "advice": "保留为疑似结果；必须补充负向对照后才能确认为漏洞。",
            }

        # 2. 遇阻分支：判断是否可反思回溯
        blocked_reason = ""
        advice = ""

        if status_code == 403 and any(w in response_body.lower() for w in ("sign", "signature", "nonce", "timestamp")):
            blocked_reason = self.BLOCKED_403_SIGNATURE
            advice = "检测到签名/防重放拦截：请定向反查控制器/拦截器代码，提取签名生成算法与密钥，修正 Header 后重试。"
        elif status_code == 401:
            blocked_reason = self.BLOCKED_401_TOKEN
            advice = "检测到身份凭证失效：请调用 20-auth-session 刷新 Token 或更换测试身份。"
        elif status_code == 429:
            blocked_reason = self.BLOCKED_429_RATE_LIMIT
            advice = "触发频率限制拦截：实施指数退避并降低并发发包速率。"
        elif "waf" in response_body.lower() or "blocked by security" in response_body.lower():
            blocked_reason = self.BLOCKED_WAF
            advice = "触发安全过滤器/WAF硬拦截：对测试 Payload 实施混淆/多层编码变异。"
        else:
            # 业务明确拒绝或无漏洞特征
            if status_code in (403, 404):
                self.graph.update_hypothesis_status(
                    hypothesis_id,
                    self.STATUS_REFUTED,
                    reason=f"目标明确拒绝请求 (HTTP {status_code})，假设不成立",
                )
                return {
                    "status": self.STATUS_REFUTED,
                    "action": "close_hypothesis",
                    "current_depth": depth + 1,
                    "advice": "目标正常鉴权拦截，假设被反驳。",
                }

        record["blocked_reason"] = blocked_reason

        # 3. 反思深度判定 (硬上限 <= 2)
        if depth + 1 >= self.MAX_BACKTRACK_DEPTH:
            self.graph.update_hypothesis_status(
                hypothesis_id,
                self.STATUS_NOT_APPLICABLE,
                reason=f"反思回溯深度超限 ({self.MAX_BACKTRACK_DEPTH} 次)，放弃进一步尝试",
            )
            return {
                "status": self.STATUS_NOT_APPLICABLE,
                "action": "abort_exhausted",
                "current_depth": depth + 1,
                "advice": f"已达最大反思回溯深度 ({self.MAX_BACKTRACK_DEPTH})，标记 not_applicable 并释放。",
            }

        # 4. 触发反思回溯
        self.graph.update_hypothesis_status(
            hypothesis_id,
            self.STATUS_BACKTRACKING,
            reason=f"遇到阻碍 ({blocked_reason})，进入反思回溯环",
        )
        heuristic_fix = HeuristicAutoFixer.diagnose_and_suggest_fix(
            status_code=status_code,
            response_body=response_body,
            last_request=request_context,
            available_tokens=self.available_tokens,
            secret_key=self.secret_key,
        )
        auto_fixed = (
            heuristic_fix.get("patch_headers") if heuristic_fix
            else HeuristicAutoFixer.fix_request_headers(
                blocked_reason,
                {},
                available_tokens=self.available_tokens,
                secret_key=self.secret_key,
            )
        )
        return {
            "status": self.STATUS_BACKTRACKING,
            "action": "reflexive_retry",
            "blocked_reason": blocked_reason,
            "current_depth": depth + 1,
            "advice": advice,
            "heuristic_fix": heuristic_fix,
            "suggested_headers": auto_fixed,
        }

    def execute_in_situ_fast_replay(
        self,
        hypothesis_id: str,
        patch_headers: Mapping[str, str],
        sender_fn: Any,
        *,
        target_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """DOC-125 优化项 10: 启发式修包就地免 LLM 快速重放闭环 (In-Situ Fast Replay)。

        直接调用传入的网络发包回调在沙箱网络栈快速试探 1 次。
        若直接命中利用成功 (status 200/201/204 或 is_exploit_successful=True)，
        直接推进至 verified 状态并固化证据，免去一次昂贵的大模型推理往返。
        """
        expected_target = (
            target_fingerprint.strip()
            if isinstance(target_fingerprint, str)
            else ""
        )
        try:
            bound_target = str(getattr(sender_fn, "target_fingerprint", "")).strip()
        except Exception:
            bound_target = ""
        if not expected_target or bound_target != expected_target:
            return {
                "fast_replay_attempted": False,
                "success": False,
                "action": "blocked_target_binding",
                "error": (
                    "快速重放必须绑定 target_fingerprint，且必须与 sender_fn 声明一致"
                ),
            }
        try:
            resp = sender_fn(dict(patch_headers))
        except Exception as exc:
            return {
                "fast_replay_attempted": True,
                "success": False,
                "error": str(exc),
                "action": "fallback_to_llm",
            }

        status_code = int(resp.get("status_code", 0))
        is_success = bool(resp.get("is_exploit_successful", False))
        has_negative = bool(resp.get("has_negative_control", False))

        if is_success and has_negative:
            self.graph.update_hypothesis_status(
                hypothesis_id,
                self.STATUS_VERIFIED,
                reason="启发式修包就地快速重放成功实锤 (Zero-LLM Fast Path)",
            )
            return {
                "fast_replay_attempted": True,
                "success": True,
                "status": self.STATUS_VERIFIED,
                "action": "commit_evidence_fast_path",
                "status_code": status_code,
                "advice": "就地重放成功！已绕过大模型推理直接确权实锤。",
            }

        if is_success or status_code in (200, 201, 204):
            self.graph.update_hypothesis_status(
                hypothesis_id,
                self.STATUS_SUSPECTED,
                reason="快速重放成功但缺少负向对照",
            )
            return {
                "fast_replay_attempted": True,
                "success": False,
                "status": self.STATUS_SUSPECTED,
                "status_code": status_code,
                "action": "hold_for_negative_control",
                "advice": "快速重放仅形成疑似结果，补充负向对照后再确权。",
            }

        return {
            "fast_replay_attempted": True,
            "success": False,
            "status_code": status_code,
            "action": "fallback_to_llm",
            "advice": "就地快速重放未成功，继续由大模型进行深度反思。",
        }


class HeuristicAutoFixer:
    """DOC-125 优化项 3: 常见阻碍启发式自动修包引擎 (Heuristic Auto-Fixer)。

    针对 403 签名/防重放缺失、401 凭证过期或 Content-Type 畸变，
    在回溯层自动尝试生成修正 Header，先免模型重放一次。
    """

    @classmethod
    def diagnose_and_suggest_fix(
        cls,
        status_code: int,
        response_body: str,
        last_request: Mapping[str, Any] | None = None,
        available_tokens: Iterable[str] | None = None,
        secret_key: str | None = None,
    ) -> dict[str, Any] | None:
        """根据 HTTP 响应状态码及响应体智能诊断并生成修包补丁。"""
        req = dict(last_request or {})
        headers = dict(req.get("headers") or {})
        body_lower = response_body.lower()

        if status_code == 403 and any(k in body_lower for k in ("sign", "timestamp", "nonce", "expire")):
            if not secret_key:
                return None
            now_str = str(int(time.time()))
            nonce = hashlib.md5(now_str.encode("utf-8")).hexdigest()[:16]
            sig = hashlib.sha256(f"{now_str}_{nonce}_{secret_key}".encode("utf-8")).hexdigest()
            patch_headers = {
                "X-Timestamp": now_str,
                "X-Nonce": nonce,
                "X-Sign": sig,
                "X-Signature": sig,
            }
            return {
                "action": "fix_signature_timestamp",
                "patch_headers": patch_headers,
                "advice": "目标提示签名或时间戳失效，已启发式计算最新 X-Timestamp/X-Nonce/X-Sign",
            }

        elif status_code == 401 or "token" in body_lower or "jwt" in body_lower:
            tokens = [
                token.strip() for token in (available_tokens or ())
                if isinstance(token, str) and token.strip()
            ]
            if not tokens:
                return None
            return {
                "action": "refresh_auth_token",
                "patch_headers": {"Authorization": f"Bearer {tokens[0]}"},
                "advice": "目标鉴权 Token 失效，已调度凭证池刷新 Bearer Token",
            }

        elif status_code in (415, 400) and any(k in body_lower for k in ("media type", "content-type", "expected application/json", "json")):
            target_ct = "application/json" if "application/json" in body_lower else "application/x-www-form-urlencoded"
            return {
                "action": "switch_content_type",
                "patch_headers": {"Content-Type": target_ct},
                "advice": f"目标提示 Payload 格式不兼容，已自动纠偏 Content-Type 为 {target_ct}",
            }

        return None

    @staticmethod
    def fix_request_headers(
        blocked_reason: str,
        current_headers: Mapping[str, str],
        *,
        secret_key: str | None = None,
        available_tokens: Iterable[str] | None = None,
    ) -> dict[str, str] | None:
        """根据遇阻原因启发式修正请求头。返回修正后的字典，若无法自动修正则返回 None。"""
        headers = dict(current_headers)
        if blocked_reason == ReflexiveBacktrackingEngine.BLOCKED_403_SIGNATURE:
            if not secret_key:
                return None
            now_str = str(int(time.time()))
            nonce = hashlib.md5(now_str.encode("utf-8")).hexdigest()[:16]
            sig = hashlib.sha256(f"{now_str}_{nonce}_{secret_key}".encode("utf-8")).hexdigest()
            headers["X-Timestamp"] = now_str
            headers["X-Nonce"] = nonce
            headers["X-Signature"] = sig
            return headers

        elif blocked_reason == ReflexiveBacktrackingEngine.BLOCKED_401_TOKEN:
            tokens = list(available_tokens or [])
            if tokens:
                headers["Authorization"] = f"Bearer {tokens[0]}"
                return headers

        elif blocked_reason == "CONTENT_TYPE_MISMATCH":
            if "json" in headers.get("Content-Type", "").lower():
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                return headers

        return None
