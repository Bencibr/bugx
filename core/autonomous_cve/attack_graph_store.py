#!/usr/bin/env python3
"""core/attack_graph_store.py - LivingAttackGraph 的会话级持久化与事件写入契约 (DOC-125)。

DOC-125 把 :mod:`attack_graph` 定义成"审计期间真实演化的活图谱"，但库里只有结构与
算法，没有任何生产者：图谱从不被构建、演化、落盘或读取（核验报告 C125-1）。本模块
就是那个**生产者与唯一存储契约**：

  - 位置：``bugx_paths.require(root).polling_root`` —— **Session（polling）层**，
    不是 Round 级的 ``state_root``。图谱的价值在于跨轮累积（已发现的端点、被反驳的
    假设、回溯深度），放在 Round 层会让每一轮都从零开始。
  - 写入：一律经 :func:`coverage_evidence.attach_attack_graph_snapshot` 原子落盘，
    并发用与证据存储同一套 ``_exclusive_lock(path.with_name(path.name + ".lock"))``
    约定，符号链接由 ``assert_no_symlink_components`` 与 ``O_NOFOLLOW`` 双重防护。
  - 演化：一切状态变化都通过 :func:`record_event` 走事件类型表，事件同时进入
    ``StreamingEventBus``（进程内即刻广播）和 ``graph.events``（落盘日志），
    因此"谁在什么时候把图谱改成这样"是可审计的。

失败语义（关键契约）：:class:`AttackGraphStoreError` **故意不继承 ``ValueError``**。
``launch_phases/post_phase.py`` 的收尾 try/except 捕获 ``(OSError, ValueError, ...)``
并把它转成"运行计时结算失败"的中断路径；图谱故障必须是**降级**（挖矿业继续），
绝不能被那个 except 顺手吞成一次 abort。因此调用方要么显式捕获本类型，要么在最外层
用 ``except Exception`` 降级——两种都不会误判成结算失败。

消费者（worker 提示注入、任务调度、MCP 面）只需依赖本模块的公开面，不要直接读快照
文件路径，也不要自己拼 ``attack-graph-snapshot.json``。
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from core.autonomous_cve import attack_graph as _ag
import core.bugx_paths as bugx_paths
from core.coverage import coverage_evidence
from core.autonomous_cve.attack_graph import LivingAttackGraph, ReflexiveBacktrackingEngine

#: 快照文件名由 ``coverage_evidence.attach_attack_graph_snapshot`` 固定。
SNAPSHOT_NAME = "attack-graph-snapshot.json"

#: 图谱节点上用于"哪个覆盖单元推进了它"的归属字段（调度亲和性依赖这两个键）。
UNIT_ATTR = "unit_id"
ADVANCED_BY_ATTR = "advanced_by"


class AttackGraphStoreError(Exception):
    """攻击图谱存储层的失败。

    显式继承 ``Exception`` 而非 ``ValueError``：见模块文档串的"失败语义"。
    """


# --------------------------------------------------------------------------
# 事件类型表（唯一权威；消费者按常量引用，不要写字面量）
# --------------------------------------------------------------------------

EVENT_ENDPOINT_DISCOVERED = "endpoint_discovered"
EVENT_ENDPOINTS_READY = "endpoints_ready"
EVENT_SINK_FOUND = "sink_found"
EVENT_HYPOTHESIS_VERIFIED = "hypothesis_verified"
EVENT_HYPOTHESIS_REJECTED = "hypothesis_rejected"
EVENT_ROLLBACK = "rollback"
# 以下为四大深度优化机制运转所必需的补充事件：
EVENT_HYPOTHESIS_PROPOSED = "hypothesis_proposed"      # 假设入图，供 DAG 求值排期
EVENT_ROLE_OBSERVED = "role_observed"                  # 身份矩阵；跨租户判定的前提
EVENT_UNIT_STAMPED = "unit_stamped"                    # 单元归属（亲和性/断点续跑）
EVENT_UNIT_COVERED = "unit_covered"                    # 单元覆盖态推进 + tested_stages
EVENT_EVIDENCE_CHECKPOINTED = "evidence_checkpointed"  # Life 已检查点证据吸收
EVENT_GRAPH_SEEDED = "graph_seeded"                    # pre 阶段冷启动/播种
EVENT_GRAPH_ABSORBED = "graph_absorbed"                # post 阶段整轮吸收汇总

ALL_EVENTS = (
    EVENT_ENDPOINT_DISCOVERED,
    EVENT_ENDPOINTS_READY,
    EVENT_SINK_FOUND,
    EVENT_HYPOTHESIS_VERIFIED,
    EVENT_HYPOTHESIS_REJECTED,
    EVENT_ROLLBACK,
    EVENT_HYPOTHESIS_PROPOSED,
    EVENT_ROLE_OBSERVED,
    EVENT_UNIT_STAMPED,
    EVENT_UNIT_COVERED,
    EVENT_EVIDENCE_CHECKPOINTED,
    EVENT_GRAPH_SEEDED,
    EVENT_GRAPH_ABSORBED,
)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "required", "auth", "protected"}:
            return True
        if text in {"0", "false", "no", "n", "public", "anonymous"}:
            return False
    return bool(value)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _texts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        return [text for text in (_text(item) for item in value) if text]
    return []


def _route_id_of(payload: Mapping[str, Any]) -> str:
    """端点/路由节点 id：``route:<path>``（CLI 面为 ``cli:<name>``）。

    以 *路径* 而不是"文件:行"为 key，是为了让同一个端点在多轮、多 worker 提交下
    收敛成同一个节点——否则图谱会被重复端点灌满，越权链求解失去意义。
    """
    explicit = _text(payload.get("route_id") or payload.get("id"))
    if explicit:
        return explicit if ":" in explicit else f"route:{explicit}"
    path = _text(payload.get("path") or payload.get("url") or payload.get("endpoint"))
    if path.startswith(("http://", "https://")):
        path = "/" + path.split("://", 1)[1].split("/", 1)[-1]
    if not path and _text(payload.get("name")).startswith("endpoint:"):
        parts = _text(payload.get("name")).split(":", 2)
        path = parts[2] if len(parts) == 3 else ""
    name = _text(payload.get("name"))
    if path:
        if _text(payload.get("method")).upper() == "CLI" or name.startswith("cli:"):
            return f"cli:{path}"
        return f"route:{path}" if path.startswith("/") else f"route:/{path}"
    if not name:
        return ""
    return name if ":" in name else f"route:{name}"


def _sink_id_of(payload: Mapping[str, Any]) -> str:
    explicit = _text(payload.get("sink_id"))
    if explicit:
        return explicit if explicit.startswith("sink:") else f"sink:{explicit}"
    kind = _text(payload.get("kind") or payload.get("sink") or "unknown")
    location = _text(
        payload.get("location") or payload.get("name")
        or payload.get("target") or payload.get("source_file")
    )
    line = payload.get("line")
    if location:
        tail = f":{line}" if isinstance(line, int) else ""
        return f"sink:{location}{tail}"
    return f"sink:{kind}:{len(str(line))}"


def _hypothesis_id_of(payload: Mapping[str, Any]) -> str:
    explicit = _text(payload.get("hypothesis_id") or payload.get("hypo_id"))
    if explicit:
        return explicit if explicit.startswith("hypo:") else f"hypo:{explicit}"
    target = _route_id_of(payload) or _text(payload.get("target"))
    kind = _text(payload.get("type") or payload.get("vulnerability_type") or "unknown")
    finding = _text(payload.get("finding_id") or payload.get("fingerprint"))
    if finding:
        return f"hypo:{finding}"
    return f"hypo:{kind}:{target}" if target else f"hypo:{kind}"


# --------------------------------------------------------------------------
# 路径与并发
# --------------------------------------------------------------------------

def graph_dir(project_root: str | Path) -> Path:
    """图谱所在目录 = Session（polling）层根目录。

    放在 ``polling_root`` 而不是 ``state_root``：``state_root`` 随 Round 切换，
    图谱必须随 Session 存活，否则第二 Round 看不到第一 Round 的攻击面。
    """
    root = Path(project_root).expanduser()
    try:
        scope = bugx_paths.require(root)
    except bugx_paths.LayoutError as exc:
        raise AttackGraphStoreError(f"攻击图谱需要已初始化的项目布局: {exc}") from exc
    except OSError as exc:
        raise AttackGraphStoreError(f"攻击图谱布局不可读: {exc}") from exc
    directory = scope.polling_root
    bugx_paths.assert_no_symlink_components(directory, error_cls=AttackGraphStoreError)
    return directory


def snapshot_path(project_root: str | Path) -> Path:
    """持久化快照文件路径（供只读消费者展示；写入请走 :func:`save_graph`）。"""
    return graph_dir(project_root) / SNAPSHOT_NAME


def _lock_path(root: Path) -> Path:
    snapshot = root / SNAPSHOT_NAME
    return snapshot.with_name(snapshot.name + ".lock")


@contextmanager
def _store_lock(directory: Path) -> Iterator[None]:
    """复用证据存储的进程内 + 跨进程独占锁约定。

    注意：``fcntl.flock`` 对同一进程内**第二个 fd** 也会阻塞，所以持锁期间只能调用
    ``_*_locked`` 内部函数，绝不可重入公共 :func:`save_graph`。
    """
    directory.mkdir(parents=True, exist_ok=True)
    with coverage_evidence._exclusive_lock(_lock_path(directory)):  # noqa: SLF001
        yield


def _schema_validation_mode() -> dict[str, Any]:
    """把 C125-13 的降级事实写进 ``meta``，使其可被审计而不是只 warn 一次。"""
    available = _ag.jsonschema is not None
    schema_present = _ag.SCHEMA_PATH.is_file()
    return {
        "mode": "jsonschema" if (available and schema_present) else "builtin_degraded",
        "strict_json_schema": bool(available and schema_present),
        "jsonschema_available": available,
        "schema_file_present": schema_present,
        "schema_path": str(_ag.SCHEMA_PATH),
    }


def _new_graph(root: Path) -> LivingAttackGraph:
    graph = LivingAttackGraph()
    graph.meta.update({
        "project_root": str(root),
        "session_layer": "polling_root",
        "seeded_at": time.time(),
        "units": {},
        "event_counts": {},
        "schema_validation": _schema_validation_mode(),
    })
    return graph


# --------------------------------------------------------------------------
# 读 / 写
# --------------------------------------------------------------------------

def load_graph(project_root: str | Path) -> LivingAttackGraph | None:
    """读取当前 Session 的图谱；**缺失或损坏一律返回 None，绝不抛异常**。

    "损坏"包括：JSON 解析失败、顶层形状不合法、节点字段缺失导致反序列化抛错。
    消费者据此把"无图谱"当成正常的冷启动分支，而不是让整条流水线因一个坏文件中断。
    """
    try:
        directory = graph_dir(project_root)
    except (AttackGraphStoreError, bugx_paths.LayoutError, OSError, ValueError):
        return None
    try:
        payload = coverage_evidence.load_attack_graph_snapshot(directory)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    try:
        graph = LivingAttackGraph.from_dict(payload)
    except Exception:
        # from_dict 面对人为篡改/半写文件会抛 KeyError/TypeError/AttributeError 等，
        # 这里必须兜住：契约是"永不 raise"。
        return None
    if not isinstance(graph.meta, dict):
        graph.meta = {}
    return graph


def _write_graph_locked(directory: Path, graph: LivingAttackGraph) -> Path:
    graph.meta["updated_at"] = time.time()
    graph.meta["schema_validation"] = _schema_validation_mode()
    try:
        graph.validate()
    except Exception as exc:
        # 结构不合法就拒绝落盘：宁可留旧快照，也不要污染消费者的图谱。
        raise AttackGraphStoreError(f"攻击图谱结构校验未通过，拒绝写入: {exc}") from exc
    try:
        bugx_paths.assert_no_symlink_components(directory, error_cls=AttackGraphStoreError)
        return coverage_evidence.attach_attack_graph_snapshot(directory, graph)
    except AttackGraphStoreError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise AttackGraphStoreError(f"攻击图谱快照写入失败: {exc}") from exc


def save_graph(project_root: str | Path, graph: LivingAttackGraph) -> Path:
    """原子、并发安全地持久化图谱；返回快照路径。"""
    if not isinstance(graph, LivingAttackGraph):
        raise AttackGraphStoreError("save_graph 只接受 LivingAttackGraph 实例")
    directory = graph_dir(project_root)
    with _store_lock(directory):
        return _write_graph_locked(directory, graph)


# --------------------------------------------------------------------------
# 单元归属（调度亲和性的数据来源）
# --------------------------------------------------------------------------

_NODE_KINDS = ("routes", "sinks", "hypotheses", "roles", "parameters")


def stamp_unit(
    graph: LivingAttackGraph,
    unit_id: str,
    *,
    routes: Iterable[str] | None = None,
    sinks: Iterable[str] | None = None,
    hypotheses: Iterable[str] | None = None,
    roles: Iterable[str] | None = None,
    parameters: Iterable[str] | None = None,
    advanced_by: Any = None,
    round_no: int | None = None,
    stage_id: str | None = None,
    tested_stages: Iterable[str] | None = None,
    **attrs: Any,
) -> dict[str, Any]:
    """把一次推进归因到覆盖单元：既写节点属性，也写 ``meta["units"][unit_id]``。

    两个层次都要写，因为消费者读法不同：
      - 调度器（亲和性/断点续跑）按单元聚合 → 读 ``meta["units"]``；
      - 图谱切片与越权链按节点取值 → 读节点上的 ``unit_id`` / ``advanced_by``。
    """
    unit = _text(unit_id)
    if not unit:
        raise AttackGraphStoreError("stamp_unit 需要非空 unit_id")
    if not isinstance(graph.meta, dict):
        graph.meta = {}
    units = graph.meta.setdefault("units", {})
    if not isinstance(units, dict):
        units = {}
        graph.meta["units"] = units
    record = units.get(unit)
    if not isinstance(record, dict):
        record = {"unit_id": unit, "nodes": {}, "advanced_by": []}
        units[unit] = record
    record["updated_at"] = time.time()

    given = {
        "routes": _texts(routes),
        "sinks": _texts(sinks),
        "hypotheses": _texts(hypotheses),
        "roles": _texts(roles),
        "parameters": _texts(parameters),
    }
    index = {
        "routes": graph.routes,
        "sinks": graph.sinks,
        "hypotheses": graph.hypotheses,
        "roles": graph.roles,
        "parameters": graph.parameters,
    }
    nodes = record.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        nodes = {}
        record["nodes"] = nodes
    people = sorted(set(_texts(record.get("advanced_by")) + _texts(advanced_by)))
    for kind, ids in given.items():
        merged = sorted(set(_texts(nodes.get(kind)) + ids))
        if merged:
            nodes[kind] = merged
        store = index[kind]
        for node_id in ids:
            node = store.get(node_id)
            if not isinstance(node, dict):
                continue  # 允许引用尚未入图的 id，但不因此失败
            node[UNIT_ATTR] = unit
            if people:
                # 亲和性判定的两个键必须同时落在节点上：只留 unit_id 的话，
                # "这个端点是哪个 worker 推进的"仍然只能反查 meta。
                node[ADVANCED_BY_ATTR] = people[0] if len(people) == 1 else people
            if stage_id:
                node["last_stage_id"] = stage_id
            for key, value in attrs.items():
                node[key] = value
            if kind == "routes" and tested_stages:
                existing = set(_texts(node.get("tested_stages")))
                node["tested_stages"] = sorted(existing | set(_texts(tested_stages)))

    if people:
        record["advanced_by"] = people
    for key, value in attrs.items():
        record[key] = value
    if round_no is not None:
        record["last_round"] = round_no
    record["node_total"] = sum(len(_texts(value)) for value in nodes.values())
    return record


# --------------------------------------------------------------------------
# 事件处理器
# --------------------------------------------------------------------------

def _merge_node(graph: LivingAttackGraph, collection: str, node_id: str, **attrs: Any) -> dict[str, Any]:
    """把 ``attrs`` 并入已存在节点（保留既有键），或新建节点。"""
    # ``kind`` is a valid node attribute (notably for sinks).  Do not use it
    # as the Python parameter name: ``_merge_node(..., **{"kind": ...})``
    # would otherwise collide with the collection argument before the
    # function can merge the existing node.
    store = getattr(graph, collection)
    existing = store.get(node_id)
    if isinstance(existing, dict):
        merged = {**existing, **attrs}
        merged["id"] = node_id
        store[node_id] = merged
        return merged
    raise KeyError(node_id)


def _on_endpoint(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    route_id = _route_id_of(payload)
    if not route_id:
        raise AttackGraphStoreError("endpoint_discovered 需要 route_id/path/name 之一")
    observed = "auth_required" in payload and payload.get("auth_required") is not None
    auth_required = _as_bool(payload.get("auth_required"), False)
    attrs: dict[str, Any] = {
        "method": (_text(payload.get("method")) or "GET").upper(),
        "auth_required": auth_required,
        "auth_required_source": "observed" if observed else "public_unauthenticated",
        "exposure": "protected" if auth_required else "public_unauthenticated",
        "path": _text(payload.get("path") or payload.get("url") or route_id.split(":", 1)[-1]),
        "tested_stages": [],
    }
    for key in ("source_file", "line", "framework", "name", "stage_id"):
        if payload.get(key) is not None:
            attrs[key] = payload[key]
    if route_id in graph.routes:
        _merge_node(graph, "routes", route_id, **attrs)
        node = graph.routes[route_id]
        created = False
    else:
        node = graph.add_route(route_id, attrs["method"], auth_required, **{
            k: v for k, v in attrs.items() if k not in ("method", "auth_required")
        })
        created = True
    unit = _text(payload.get(UNIT_ATTR) or payload.get("unit_id"))
    if unit:
        stamp_unit(graph, unit, routes=[route_id], advanced_by=payload.get(ADVANCED_BY_ATTR),
                   round_no=ctx.get("round_no"))
    for role_id in _texts(payload.get("legit_roles")):
        graph.add_edge(role_id, route_id, "HAS_LEGITIMATE_ACCESS")
    return {"route_id": route_id, "created": created, "auth_required": auth_required}


def _on_endpoints_ready(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    surfaces = payload.get("endpoints") or payload.get("surfaces") or []
    if not isinstance(surfaces, list):
        surfaces = []
    ready: list[str] = []
    for item in surfaces:
        row = dict(item) if isinstance(item, Mapping) else {}
        row.setdefault("unit_id", _text(payload.get("unit_id")))
        row.setdefault(ADVANCED_BY_ATTR, payload.get(ADVANCED_BY_ATTR))
        try:
            result = _on_endpoint(graph, row, ctx)
        except AttackGraphStoreError:
            continue  # 单条畸形面不影响其余端点入图
        ready.append(result["route_id"])
    count = payload.get("count")
    graph.meta["endpoints_ready"] = True
    graph.meta["endpoints_ready_at"] = time.time()
    graph.meta["endpoints_ready_count"] = len(graph.routes)
    if isinstance(count, int) and not isinstance(count, bool):
        graph.meta["surface_reported_count"] = count
    unit = _text(payload.get("unit_id"))
    if unit:
        stamp_unit(graph, unit, routes=ready, advanced_by=payload.get(ADVANCED_BY_ATTR),
                   round_no=ctx.get("round_no"))
    return {"ready_routes": len(ready), "route_total": len(graph.routes)}


def _on_sink(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    sink_id = _sink_id_of(payload)
    kind = _text(payload.get("kind") or payload.get("sink") or "unknown")
    attrs: dict[str, Any] = {"kind": kind}
    for key in ("source_file", "line", "location", "severity", "name", "description"):
        if payload.get(key) is not None:
            attrs[key] = payload[key]
    if sink_id in graph.sinks:
        _merge_node(graph, "sinks", sink_id, **attrs)
        created = False
    else:
        graph.add_sink(sink_id, kind, **{k: v for k, v in attrs.items() if k != "kind"})
        created = True
    route_id = _text(payload.get("route_id")) or _route_id_of(payload)
    if route_id and route_id in graph.routes:
        graph.add_edge(route_id, sink_id, "REACHES_SINK")
    elif route_id:
        graph.add_edge(route_id, sink_id, "REACHES_SINK")
    param_id = _text(payload.get("param_id"))
    if param_id:
        graph.add_edge(param_id, sink_id, "TAINTS")
    unit = _text(payload.get(UNIT_ATTR) or payload.get("unit_id"))
    if unit:
        stamp_unit(graph, unit, sinks=[sink_id], advanced_by=payload.get(ADVANCED_BY_ATTR),
                   round_no=ctx.get("round_no"))
    if payload.get("speculative"):
        graph.sinks[sink_id]["speculative"] = True
    return {"sink_id": sink_id, "created": created, "kind": kind}


def _on_hypothesis_proposed(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    hyp_id = _hypothesis_id_of(payload)
    target = _text(payload.get("target")) or _route_id_of(payload)
    kind = _text(payload.get("type") or payload.get("vulnerability_type") or "unknown")
    status = _text(payload.get("status")) or "proposed"
    extra = {
        key: value
        for key, value in payload.items()
        if key in {"title", "root_cause", "severity", "confidence", "finding_id",
                   "description", "status_code", "route_id", "sink_id", "stage_id",
                   "reproduction", "source_file", "line", "unit_id", "confirmed_by_ai"}
        and value is not None
    }
    # DOC-241 假说融合与去重：AI 确认的精准假说平滑替代/升级粗粒度静态初筛假说 (消除双轨冗余)
    if extra.get("confirmed_by_ai"):
        src_file = str(payload.get("source_file") or "").replace("\\", "/")
        src_line = int(payload.get("line") or 0)
        p_sink = str(payload.get("sink_id") or "")
        p_type = str(payload.get("type") or payload.get("vulnerability_type") or "")
        superseded_ids = []
        for exist_id, exist_hypo in graph.hypotheses.items():
            if exist_id == hyp_id:
                continue
            e_src = str(exist_hypo.get("source_file") or "").replace("\\", "/")
            e_line = int(exist_hypo.get("line") or 0)
            e_sink = str(exist_hypo.get("sink_id") or "")
            e_type = str(exist_hypo.get("type") or exist_hypo.get("vulnerability_type") or "")
            if not e_src:
                raw_ref = e_sink if e_sink.startswith("sink:") else str(exist_hypo.get("target") or "")
                if raw_ref.startswith("sink:"):
                    parts = raw_ref[5:].rsplit(":", 1)
                    e_src = parts[0].replace("\\", "/")
                    if len(parts) > 1 and parts[1].isdigit():
                        e_line = int(parts[1])
                elif exist_hypo.get("unit_id") and "::" in str(exist_hypo.get("unit_id")):
                    u_parts = str(exist_hypo["unit_id"]).split("::")[0].split(":")[-1]
                    if "/" in u_parts or "." in u_parts:
                        e_src = u_parts.replace("\\", "/")

            if e_src and (e_src == src_file or e_src.endswith(src_file) or src_file.endswith(e_src)):
                line_match = src_line and e_line and abs(src_line - e_line) <= 3
                sink_match = p_sink and e_sink and (p_sink in e_sink or e_sink in p_sink)
                if line_match or sink_match:
                    if not exist_hypo.get("confirmed_by_ai"):
                        superseded_ids.append(exist_id)
                    elif p_type and e_type and (p_type in e_type or e_type in p_type):
                        superseded_ids.append(exist_id)
        for s_id in superseded_ids:
            graph.hypotheses.pop(s_id, None)
            graph.edges = [e for e in graph.edges if e.get("from") != s_id and e.get("to") != s_id and e.get("hypothesis_id") != s_id]

    if hyp_id in graph.hypotheses:
        _merge_node(graph, "hypotheses", hyp_id, type=kind, target=target, **extra)
        created = False
    else:
        graph.add_hypothesis(hyp_id, kind, target, status, **extra)
        created = True
    if target:
        graph.add_edge(hyp_id, target, "HYPOTHESIZES", hypothesis_id=hyp_id)
    sink_id = _text(payload.get("sink_id"))
    if sink_id:
        graph.add_edge(hyp_id, sink_id, "HYPOTHESIZES_SINK", hypothesis_id=hyp_id)
    unit = _text(payload.get(UNIT_ATTR) or payload.get("unit_id"))
    if unit:
        stamp_unit(graph, unit, hypotheses=[hyp_id], advanced_by=payload.get(ADVANCED_BY_ATTR),
                   round_no=ctx.get("round_no"))
    return {"hypothesis_id": hyp_id, "created": created, "status": graph.hypotheses[hyp_id]["status"]}


def _on_hypothesis_verified(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    detail = _on_hypothesis_proposed(graph, {**payload, "status": payload.get("status") or "testing"}, ctx)
    hyp_id = detail["hypothesis_id"]
    graph.update_hypothesis_status(hyp_id, "verified", _text(payload.get("reason")) or "证据实锤")
    node = graph.hypotheses[hyp_id]
    node["verified_at"] = time.time()
    if payload.get("evidence_path") is not None:
        node["evidence_path"] = _text(payload.get("evidence_path"))
    if payload.get("artifact_sha256") is not None:
        node["artifact_sha256"] = _text(payload.get("artifact_sha256"))
    relation = _text(payload.get("relation")) or "EXPLOITED_ACCESS"
    target = _text(payload.get("target")) or _route_id_of(payload) or node.get("target", "")
    # 实锤的端点/身份就是既成事实：它们可能只在 finding 文本里出现过（worker 直接
    # 提交结论），此时必须补进图谱，否则越权链与消费者切片都会缺一截。
    if target and target not in graph.routes and target.startswith("route:"):
        graph.add_route(target, _text(payload.get("method")) or "GET", True,
                        auth_required_source="inferred_from_verified_finding",
                        **{k: v for k, v in
                           {UNIT_ATTR: _text(payload.get(UNIT_ATTR) or payload.get("unit_id")),
                            ADVANCED_BY_ATTR: payload.get(ADVANCED_BY_ATTR)}.items() if v})
    role_id = _text(payload.get("role_id"))
    if role_id and role_id not in graph.roles:
        graph.add_role(role_id, _text(payload.get("role_type")) or "unknown",
                       source="inferred_from_verified_finding")
    if role_id and target:
        graph.add_edge(role_id, target, relation, hypothesis_id=hyp_id)
    elif target and target in graph.sinks:
        graph.add_edge(node.get("target", target), target, relation, hypothesis_id=hyp_id)
    node.pop("speculative", None)
    graph.meta["last_verified_hypothesis"] = hyp_id
    counts = graph.meta.setdefault("verified_count", 0)
    graph.meta["verified_count"] = (counts if isinstance(counts, int) else 0) + 1
    return {"hypothesis_id": hyp_id, "status": "verified", "illegal_edges": len(graph.get_illegal_access_edges())}


def _on_hypothesis_rejected(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    """反驳假设 → 快照 → 推测拓扑入图 → ``record_attempt`` 回溯 → 回滚。

    语义：被明确拒绝的假设不应把它在探测期间声称的端点/汇点永久留在图里误导调度；
    所以先打快照，把这些**推测节点**加进去（真实发生过的事），驱动回溯判定，再把
    拓扑回滚，只留下假设节点本身 + 它的最终裁决 + 可跨 Round 存活的回溯历史。
    实锤路径（``hypothesis_verified``）不走这里，其拓扑永久保留。
    """
    hyp_id = _hypothesis_id_of(payload)
    target = _text(payload.get("target")) or _route_id_of(payload)
    kind = _text(payload.get("type") or payload.get("vulnerability_type") or "unknown")
    # 1. 假设节点先入图（快照之前）：回滚后它必须仍然存在并承载裁决。
    _on_hypothesis_proposed(
        graph, {**payload, "target": target, "type": kind, "status": "testing"}, ctx
    )
    # 2. 快照：此刻拓扑里还没有本次推测节点。
    snapshot_id = graph.create_snapshot(tag=f"pre-reflex:{hyp_id}")
    # 3. 推测拓扑入图（worker 在失败尝试中声称的端点/汇点）。
    speculative_routes = _texts(payload.get("speculative_routes"))
    speculative_sinks = _texts(payload.get("speculative_sinks"))
    for row in payload.get("speculative") or []:
        if isinstance(row, str):
            # 允许只给 id：sink: 前缀归汇点，其余按端点处理。
            if row.startswith("sink:"):
                speculative_sinks.append(row)
            else:
                speculative_routes.append(row)
            continue
        if not isinstance(row, Mapping):
            continue
        try:
            if row.get("kind"):
                result = _on_sink(graph, {**row, "speculative": True}, ctx)
                speculative_sinks.append(result.get("sink_id", ""))
            else:
                result = _on_endpoint(graph, row, ctx)
                node = graph.routes.get(result.get("route_id"))
                if isinstance(node, dict):
                    node["speculative"] = True
                speculative_routes.append(result.get("route_id", ""))
        except AttackGraphStoreError:
            continue
    for route_id in speculative_routes:
        if route_id not in graph.routes:
            graph.add_route(route_id, "GET", True, speculative=True, unit_id=_text(payload.get("unit_id")))
    for sink_id in speculative_sinks:
        if sink_id not in graph.sinks:
            graph.add_sink(sink_id, _text(payload.get("kind")) or "unknown",
                           speculative=True, unit_id=_text(payload.get("unit_id")))
    # 4. 反思回溯：历史直接别名在 graph.reflex 上，因此天然跨 Round 存活。
    engine = ReflexiveBacktrackingEngine(graph)
    history_before = len(engine.backtrack_history.get(hyp_id, []))
    decision = engine.record_attempt(
        hyp_id,
        status_code=int(payload.get("status_code") or 0),
        response_body=str(payload.get("response_body") or payload.get("body_snippet") or ""),
        is_exploit_successful=bool(payload.get("is_exploit_successful", False)),
        has_negative_control=bool(payload.get("has_negative_control", False)),
        request_context=payload.get("request_context") if isinstance(payload.get("request_context"), Mapping) else None,
    )
    attempts = len(engine.backtrack_history.get(hyp_id, [])) - history_before
    # 5. 自动修包产物**只作为图谱属性/reflex 记录**，绝不回写证据文件。
    #    节点级属性留到回滚之后再写，否则会被自己刚恢复的快照抹掉。
    fix = decision.get("heuristic_fix")
    if isinstance(fix, Mapping):
        suggestions = graph.reflex.setdefault("auto_fix_suggestions", [])
        suggestions.append({"hypothesis_id": hyp_id, **dict(fix), "recorded_at": time.time()})
        del suggestions[:-_ag.MAX_ROLLBACK_LOG]
        # 有可尝试的补丁头 → 记录就地重放计划，交给有网络能力的消费者执行。
        headers = fix.get("patch_headers")
        if isinstance(headers, Mapping) and headers:
            pending = graph.reflex.setdefault("replay_pending", [])
            pending.append({
                "hypothesis_id": hyp_id,
                "patch_headers": dict(headers),
                "unit_id": _text(payload.get("unit_id")),
                # A replay plan is only useful when a later network-capable
                # caller can prove it is still bound to the same target.
                "target_fingerprint": _text(payload.get("target_fingerprint")) or None,
                "queued_at": time.time(),
                "attempts": attempts,
            })
            del pending[:-_ag.MAX_ROLLBACK_LOG]
    # 6. 终结性拒绝 → 回滚推测拓扑，然后重新落裁决（回滚会把假设状态一并带回
    #    snapshot 时刻的 testing，所以终态必须在回滚之后再写一次）。
    #
    #    ``terminal`` 由结算门禁裁定，而不是只信引擎的 HTTP 启发式：finding 被
    #    finding_confirmation 明确 rejected 时，即使响应里没有 403/404 签名特征，
    #    这个假设也已经死了；否则回溯历史会永远停在 backtracking，回滚分支也从不触发。
    final_status = _text(decision.get("status"))
    terminal = decision.get("action") in {"close_hypothesis", "abort_exhausted"} or _as_bool(
        payload.get("terminal"), False
    )
    if terminal and final_status not in {"refuted", "not_applicable"}:
        final_status = "refuted"
    rolled_back = False
    if terminal:
        rolled_back = graph.rollback_to_snapshot(snapshot_id)
        if rolled_back:
            node = graph.hypotheses.get(hyp_id)
            if node is None:
                graph.add_hypothesis(
                    hyp_id, kind, target, final_status or "refuted",
                    status_reason="假设被反驳，推测拓扑已回滚",
                )
            else:
                graph.update_hypothesis_status(
                    hyp_id, final_status or "refuted",
                    _text(payload.get("reason")) or "假设被反驳，推测拓扑已回滚",
                )
            survivor = graph.hypotheses.get(hyp_id)
            if isinstance(survivor, dict):
                survivor["reflex_backtrack_depth"] = attempts
                survivor["reflex_snapshot_id"] = snapshot_id
        rollbacks = graph.reflex.setdefault("rollbacks", [])
        rollbacks.append({
            "snapshot_id": snapshot_id,
            "hypothesis_id": hyp_id,
            "reason": _text(decision.get("action")) or "terminal_rejection",
            "rolled_back": rolled_back,
            "routes_dropped": max(0, len(speculative_routes)),
            "sinks_dropped": max(0, len(speculative_sinks)),
            "at": time.time(),
        })
        del rollbacks[:-_ag.MAX_ROLLBACK_LOG]
    survivor = graph.hypotheses.get(hyp_id)
    if isinstance(survivor, dict) and isinstance(fix, Mapping):
        survivor["auto_fix_action"] = _text(fix.get("action"))
    unit = _text(payload.get(UNIT_ATTR) or payload.get("unit_id"))
    if unit:
        stamp_unit(graph, unit, hypotheses=[hyp_id], advanced_by=payload.get(ADVANCED_BY_ATTR),
                   round_no=ctx.get("round_no"))
    counts = graph.meta.get("rejected_count")
    graph.meta["rejected_count"] = (counts if isinstance(counts, int) else 0) + 1
    return {
        "hypothesis_id": hyp_id,
        "status": graph.hypotheses.get(hyp_id, {}).get("status"),
        "action": decision.get("action"),
        "blocked_reason": decision.get("blocked_reason"),
        "backtrack_depth": attempts,
        "terminal": terminal,
        "rolled_back": rolled_back,
        "snapshot_id": snapshot_id,
        "history_total": len(engine.backtrack_history.get(hyp_id, [])),
    }


def _on_rollback(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    snapshot_id = _text(payload.get("snapshot_id"))
    if not snapshot_id:
        snapshots = graph.list_snapshots()
        if not snapshots:
            raise AttackGraphStoreError("rollback 需要 snapshot_id，且当前图谱没有可用快照")
        snapshot_id = snapshots[-1]["snapshot_id"]
    restored = graph.rollback_to_snapshot(snapshot_id)
    if not restored:
        raise AttackGraphStoreError(f"快照不存在，无法回滚: {snapshot_id}")
    rollbacks = graph.reflex.setdefault("rollbacks", [])
    rollbacks.append({
        "snapshot_id": snapshot_id,
        "reason": _text(payload.get("reason")) or "explicit",
        "rolled_back": True,
        "at": time.time(),
    })
    del rollbacks[:-_ag.MAX_ROLLBACK_LOG]
    return {"snapshot_id": snapshot_id, "restored": True}


def _on_role(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    role_id = _text(payload.get("role_id") or payload.get("id")) or "role:anonymous"
    role_type = _text(payload.get("type") or payload.get("role_type") or "unknown")
    attrs = {
        key: value
        for key, value in payload.items()
        if key in {"tenant_id", "privilege", "credential_source", "worker_id", "description"}
        and value is not None
    }
    if role_id in graph.roles:
        _merge_node(graph, "roles", role_id, type=role_type, **attrs)
    else:
        graph.add_role(role_id, role_type, **attrs)
    unit = _text(payload.get(UNIT_ATTR) or payload.get("unit_id"))
    if unit:
        stamp_unit(graph, unit, roles=[role_id], advanced_by=payload.get(ADVANCED_BY_ATTR),
                   round_no=ctx.get("round_no"))
    return {"role_id": role_id, "role_total": len(graph.roles)}


def _on_unit_stamped(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    unit = _text(payload.get("unit_id")) or _text(payload.get("id"))
    if not unit:
        raise AttackGraphStoreError("unit_stamped 需要 unit_id")
    extra = {
        key: value
        for key, value in payload.items()
        if key in {"phase", "parent", "name", "path", "risk", "severity", "tool", "source"}
        and value is not None
    }
    record = stamp_unit(
        graph, unit,
        routes=_texts(payload.get("routes")),
        sinks=_texts(payload.get("sinks")),
        hypotheses=_texts(payload.get("hypotheses")),
        advanced_by=payload.get("worker_id") or payload.get(ADVANCED_BY_ATTR),
        round_no=payload.get("round_no") if isinstance(payload.get("round_no"), int) else ctx.get("round_no"),
        stage_id=_text(payload.get("stage_id")) or None,
        tested_stages=_texts(payload.get("tested_stages")),
        **extra,
    )
    return {"unit_id": unit, "nodes": record.get("node_total", 0)}


def _on_unit_covered(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    unit = _text(payload.get("unit_id"))
    if not unit:
        raise AttackGraphStoreError("unit_covered 需要 unit_id")
    status = _text(payload.get("status")) or "covered"
    known: list[str] = []
    units = graph.meta.get("units")
    if isinstance(units, dict):
        entry = units.get(unit)
        nodes = entry.get("nodes") if isinstance(entry, Mapping) else None
        if isinstance(nodes, Mapping):
            known = _texts(nodes.get("routes"))
    route_ids = _texts(payload.get("routes")) or known
    stage = _text(payload.get("stage_id"))
    touched = 0
    for route_id in route_ids:
        node = graph.routes.get(route_id)
        if not isinstance(node, dict):
            continue
        node["coverage_status"] = status
        if stage:
            node["tested_stages"] = sorted(set(_texts(node.get("tested_stages")) + [stage]))
        touched += 1
    record = stamp_unit(
        graph, unit,
        routes=route_ids,
        advanced_by=payload.get("worker_id"),
        round_no=payload.get("round_no") if isinstance(payload.get("round_no"), int) else ctx.get("round_no"),
        stage_id=stage or None,
        tested_stages=[stage] if stage else None,
        coverage_status=status,
    )
    record["covered"] = True
    return {"unit_id": unit, "routes_touched": touched, "status": status}


def _on_evidence_checkpointed(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    unit = _text(payload.get("unit_id"))
    if not unit:
        raise AttackGraphStoreError("evidence_checkpointed 需要 unit_id")
    record = stamp_unit(
        graph, unit,
        routes=_texts(payload.get("routes")),
        sinks=_texts(payload.get("sink_ids")),
        hypotheses=_texts(payload.get("hypothesis_ids")),
        advanced_by=payload.get("worker_id"),
        round_no=payload.get("round_no") if isinstance(payload.get("round_no"), int) else ctx.get("round_no"),
        evidence_status=_text(payload.get("status")) or "unknown",
        artifact_path=_text(payload.get("artifact_path")),
        target_fingerprint=_text(payload.get("target_fingerprint")),
    )
    return {"unit_id": unit, "evidence_status": record.get("evidence_status")}


def _on_graph_seeded(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    """pre 阶段播种汇总事件：只在 ``meta`` 上打水位，节点由具体事件负责。"""
    summary = payload.get("summary") if isinstance(payload.get("summary"), Mapping) else payload
    graph.meta["seed_summary"] = {
        key: value for key, value in dict(summary or {}).items()
        if isinstance(value, (int, float, str, bool))
    }
    graph.meta["seeded_round"] = ctx.get("round_no")
    return {"seeded_round": ctx.get("round_no"), "unit_total": len(graph.meta.get("units") or {})}


def _on_graph_absorbed(graph: LivingAttackGraph, payload: Mapping[str, Any], ctx: dict) -> dict:
    """post 阶段整轮吸收汇总：留一份逐轮水位，供消费者判断图谱是否在真实演化。"""
    history = graph.meta.setdefault("absorb_history", [])
    if not isinstance(history, list):
        history = []
        graph.meta["absorb_history"] = history
    history.append({
        "round": ctx.get("round_no"),
        "at": time.time(),
        **{
            key: value for key, value in payload.items()
            if isinstance(value, (int, float, str, bool))
        },
    })
    del history[-200:]
    graph.meta["absorb_count"] = len(history)
    graph.meta["verified_count"] = sum(1 for h in history if h.get("verified"))
    return {"round": ctx.get("round_no"), "absorb_total": len(history)}


EVENT_HANDLERS: dict[str, Callable[[LivingAttackGraph, Mapping[str, Any], dict], dict[str, Any]]] = {
    EVENT_ENDPOINT_DISCOVERED: _on_endpoint,
    EVENT_ENDPOINTS_READY: _on_endpoints_ready,
    EVENT_SINK_FOUND: _on_sink,
    EVENT_HYPOTHESIS_PROPOSED: _on_hypothesis_proposed,
    EVENT_HYPOTHESIS_VERIFIED: _on_hypothesis_verified,
    EVENT_HYPOTHESIS_REJECTED: _on_hypothesis_rejected,
    EVENT_ROLLBACK: _on_rollback,
    EVENT_ROLE_OBSERVED: _on_role,
    EVENT_UNIT_STAMPED: _on_unit_stamped,
    EVENT_UNIT_COVERED: _on_unit_covered,
    EVENT_EVIDENCE_CHECKPOINTED: _on_evidence_checkpointed,
    EVENT_GRAPH_SEEDED: _on_graph_seeded,
    EVENT_GRAPH_ABSORBED: _on_graph_absorbed,
}


# --------------------------------------------------------------------------
# 事件写入主入口
# --------------------------------------------------------------------------

def _journal(
    graph: LivingAttackGraph, event_type: str, body: Mapping[str, Any],
    result: Mapping[str, Any], ctx: Mapping[str, Any],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "seq": len(graph.events) + 1,
        "event_type": event_type,
        "timestamp": time.time(),
        # 批量端点列表只进图谱节点，不进日志：否则 events 上限会被一次
        # endpoints_ready 全部吃掉，后面的假设裁决一条都留不下。
        "payload": {k: v for k, v in body.items() if k not in {"endpoints", "surfaces"}},
        "result": dict(result),
    }
    if body.get("unit_id") is not None:
        record["unit_id"] = _text(body.get("unit_id"))
    if isinstance(ctx.get("round_no"), int):
        record["round"] = ctx["round_no"]
    graph.events.append(record)
    graph.events = graph.events[-_ag.MAX_EVENT_JOURNAL:]
    counts = graph.meta.setdefault("event_counts", {})
    if isinstance(counts, dict):
        counts[event_type] = counts.get(event_type, 0) + 1
    graph.meta["events_recorded"] = len(graph.events)
    if isinstance(ctx.get("round_no"), int):
        graph.meta["last_round"] = ctx["round_no"]
    return record


def record_events(
    project_root: str | Path,
    operations: Iterable[tuple[str, Mapping[str, Any] | None] | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """批量事件写入：一次 load → N 次变更 → **一次** 原子落盘。

    必须存在批量入口，否则 ``absorb_findings`` 之类的整轮吸收会退化成"每条事件重写
    一次全图"，几百个端点就是几百次快照 fsync——图谱生产会在 pre/post 里变成流水线
    最慢的一环。
    """
    prepared: list[tuple[str, dict[str, Any], Callable]] = []
    for item in operations or []:
        if isinstance(item, Mapping):
            event_type, raw_payload = _text(item.get("event_type")), item
        else:
            event_type, raw_payload = _text(item[0]), item[1]
        handler = EVENT_HANDLERS.get(event_type)
        if handler is None:
            raise AttackGraphStoreError(
                f"未知的攻击图谱事件类型: {event_type!r}；已注册: {sorted(EVENT_HANDLERS)}"
            )
        body = dict(raw_payload or {})
        prepared.append((event_type, body, handler))
    if not prepared:
        return []
    directory = graph_dir(project_root)
    with _store_lock(directory):
        graph = load_graph(project_root)
        cold_start = graph is None
        if cold_start:
            graph = _new_graph(Path(project_root).expanduser())
        graph.meta["cold_start"] = cold_start
        records: list[dict[str, Any]] = []
        for event_type, body, handler in prepared:
            ctx = {
                "cold_start": cold_start,
                "round_no": body.get("round_no") if isinstance(body.get("round_no"), int)
                else graph.meta.get("last_round"),
            }
            # 总线先行：DOC-125 的"资产发现即刻可被订阅者消费"语义，不等落盘。
            graph.event_bus.publish(event_type, body)
            result = handler(graph, body, ctx) or {}
            records.append(_journal(graph, event_type, body, result, ctx))
        _write_graph_locked(directory, graph)
    return records


def record_event(
    project_root: str | Path,
    event_type: str,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """落盘一次图谱演化：load（或冷启动）→ 发布总线 → 变更图谱 → save → 返回事件记录。

    未注册的 ``event_type`` 一律 :class:`AttackGraphStoreError`（拼错事件名必须响，
    而不是静默产生一个"看起来成功"的空事件）。
    """
    records = record_events(project_root, [(event_type, payload)])
    if not records:
        raise AttackGraphStoreError(f"事件未被记录: {event_type!r}")
    return records[0]


# --------------------------------------------------------------------------
# 流水线胶水：pre 播种 / post 吸收
# --------------------------------------------------------------------------

def seed_from_units(
    project_root: str | Path,
    units: Iterable[Any],
    *,
    round_no: int | None = None,
    source: str = "coverage-manifest",
) -> dict[str, Any]:
    """pre 阶段播种：把可调度的覆盖单元登记进图谱（幂等，冷启动也可用）。"""
    operations: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    for item in units or []:
        if isinstance(item, Mapping):
            unit_id = _text(item.get("unit_id") or item.get("id"))
            attrs = {
                key: value
                for key, value in item.items()
                if key in {"phase", "parent", "name", "path", "risk", "tool", "status"}
                and value is not None
            }
        else:
            unit_id = _text(item)
            attrs = {}
        if not unit_id:
            skipped += 1
            continue
        operations.append((EVENT_UNIT_STAMPED, {
            "unit_id": unit_id, "round_no": round_no, "source": source, **attrs,
        }))
    seeded = len(operations)
    summary = {
        "seeded_units": seeded,
        "skipped_units": skipped,
        "round_no": round_no,
        "surface_ready": False,
    }
    operations.append((EVENT_GRAPH_SEEDED, {
        "round_no": round_no, "summary": summary, "source": source,
    }))
    record_events(project_root, operations)
    return summary


def absorb_surface(
    project_root: str | Path,
    surfaces: Iterable[Mapping[str, Any]],
    *,
    unit_id: str | None = None,
    round_no: int | None = None,
) -> dict[str, Any]:
    """把 :func:`module_coverage.extract_feature_surface` 的切面灌成端点节点。

    逐条 ``endpoint_discovered`` + 一次 ``endpoints_ready``：前者让调度器可以增量
    排期，后者是"本轮面已完整"的栅栏信号。已在图里的端点不再重复记日志——pre 每个
    Round 都会跑，否则事件日志会被同一批静态端点反复灌满，把真正的假设裁决挤出上限。
    """
    rows = [dict(item) for item in surfaces or [] if isinstance(item, Mapping)]
    known = set()
    existing = load_graph(project_root)
    if existing is not None:
        known = set(existing.routes)
    operations: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        payload = {
            "name": row.get("name"),
            "method": row.get("method"),
            "path": row.get("path"),
            "source_file": row.get("source_file"),
            "line": row.get("line"),
            "framework": row.get("framework"),
            "round_no": round_no,
        }
        if unit_id:
            payload["unit_id"] = unit_id
        if _route_id_of(payload) in known:
            continue
        operations.append((EVENT_ENDPOINT_DISCOVERED, payload))
    operations.append((EVENT_ENDPOINTS_READY, {
        "endpoints": [], "count": len(rows), "unit_id": unit_id, "round_no": round_no,
    }))
    record_events(project_root, operations)
    return {"endpoints_emitted": len(operations) - 1, "surface_total": len(rows)}


def absorb_sinks(
    project_root: str | Path,
    sinks: Iterable[Mapping[str, Any]],
    *,
    round_no: int | None = None,
) -> dict[str, Any]:
    """把静态分析（SAST/CodeAnalyzer）发现的敏感 Sinks 灌成汇点节点。

    如果提供 unit_id 或 route_id，则建立相应的关联边。
    已在图里的汇点合并属性，不重复发送相同日志。
    """
    rows = [dict(item) for item in sinks or [] if isinstance(item, Mapping)]
    known = set()
    existing = load_graph(project_root)
    if existing is not None:
        known = set(existing.sinks)
    operations: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        payload = {
            "kind": _text(row.get("kind") or row.get("sink_type") or "unknown"),
            "sink": _text(row.get("sink") or row.get("name") or row.get("rule_id")),
            "location": _text(row.get("location") or row.get("source_file") or row.get("file")),
            "source_file": _text(row.get("source_file") or row.get("file")),
            "line": row.get("line") if isinstance(row.get("line"), int) else None,
            "severity": _text(row.get("severity")) or "high",
            "round_no": round_no,
            "unit_id": _text(row.get("unit_id")) or None,
            "route_id": _text(row.get("route_id")) or None,
        }
        sink_id = _sink_id_of(payload)
        if sink_id in known:
            continue
        operations.append((EVENT_SINK_FOUND, payload))
        known.add(sink_id)
    if operations:
        record_events(project_root, operations)
    return {"sinks_emitted": len(operations), "sinks_total": len(rows)}


def _finding_is_verified(finding: Mapping[str, Any]) -> bool:
    confirmation = finding.get("confirmation")
    state = _text(confirmation.get("state")) if isinstance(confirmation, Mapping) else ""
    if state == "confirmed":
        return True
    if state == "rejected":
        return False
    status = _text(finding.get("status") or finding.get("state") or finding.get("verdict"))
    if status in {"confirmed", "verified", "reproduced", "real"}:
        return True
    if status in {"rejected", "refuted", "false_positive", "invalid"}:
        return False
    # 未确认且没有明确拒绝：按"待验证"处理，既不实锤也不反驳。
    return False


def _finding_is_rejected(finding: Mapping[str, Any]) -> bool:
    confirmation = finding.get("confirmation")
    state = _text(confirmation.get("state")) if isinstance(confirmation, Mapping) else ""
    if state == "rejected":
        return True
    return _text(finding.get("status") or finding.get("state") or finding.get("verdict")) in {
        "rejected", "refuted", "false_positive", "invalid",
    }


# 只登记**真正的危险汇聚点**（污点最终被消费的位置）。IDOR / 越权 / 凭证提升属于访问
# 控制缺陷，没有 sink；把它们塞进汇点表会让 ``find_entrypoints_for_sink`` 的反向污点
# 推演凭空产出假链路，并给 ``evaluate_runnable_tasks`` 打上假的 priority=100。
_SINK_HINTS = {
    "sql": "sql", "sqli": "sql", "injection": "injection", "inject": "injection",
    "rce": "cmd", "command inject": "cmd", "cmd inject": "cmd", "exec": "cmd",
    "ssti": "template", "xss": "html", "deserialization": "deserialize",
    "file upload": "file", "path traversal": "file", "arbitrary file": "file",
    "ssrf": "network", "xxe": "xml", "ldap": "ldap", "xpath": "xpath",
}


def _sink_kind_of(finding: Mapping[str, Any]) -> str:
    blob = " ".join(
        _text(finding.get(key))
        for key in ("vulnerability_type", "type", "category", "root_cause", "title", "trigger_condition")
    ).lower()
    for hint, kind in _SINK_HINTS.items():
        if hint in blob:
            return kind
    return ""


def absorb_findings(
    project_root: str | Path,
    findings: Iterable[Mapping[str, Any]],
    *,
    round_no: int | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """post 阶段吸收结算后的 findings：实锤 → verified，拒绝 → 反思回溯 + 回滚。

    待确认（candidate）的 finding 只登记为假设，不裁决——把"没验证过"伪装成
    "已反驳"会污染回溯历史与 DPO 语料。
    """
    verified = 0
    rejected = 0
    proposed = 0
    sinks = 0
    operations: list[tuple[str, dict[str, Any]]] = []
    existing = load_graph(project_root)
    known_routes = set(existing.routes) if existing is not None else set()
    for finding in findings or []:
        if not isinstance(finding, Mapping):
            continue
        unit_id = _text(finding.get("unit_id") or finding.get("coverage_unit")) or None
        payload: dict[str, Any] = {
            "finding_id": _text(finding.get("id") or finding.get("fingerprint")),
            "target": _text(finding.get("target") or finding.get("endpoint") or finding.get("route_id")),
            "type": _text(finding.get("vulnerability_type") or finding.get("category")) or "business",
            "title": _text(finding.get("title")) or None,
            "root_cause": _text(finding.get("root_cause")) or None,
            "severity": _text(finding.get("severity")) or None,
            "confidence": finding.get("confidence") if isinstance(finding.get("confidence"), (int, float)) else None,
            "unit_id": unit_id,
            "advanced_by": _text(finding.get("worker_id")) or (worker_id or None),
            "round_no": round_no,
            "status_code": finding.get("status_code") if isinstance(finding.get("status_code"), int) else None,
            "response_body": _text(finding.get("response_body") or finding.get("reproduction"))[:2000],
        }
        evidence = finding.get("evidence")
        if isinstance(evidence, list) and evidence and isinstance(evidence[0], Mapping):
            payload["source_file"] = _text(evidence[0].get("path")) or None
            payload["line"] = evidence[0].get("line") if isinstance(evidence[0].get("line"), int) else None
            if not payload["target"]:
                payload["target"] = _text(evidence[0].get("url") or evidence[0].get("endpoint"))
        kind = _sink_kind_of(finding)
        sink_row = {
            "kind": kind,
            "location": payload.get("source_file") or payload.get("finding_id") or kind,
            "line": payload.get("line"),
            "source_file": payload.get("source_file"),
            "severity": payload.get("severity"),
            "unit_id": unit_id,
            "advanced_by": payload.get("advanced_by"),
            "round_no": round_no,
        }
        if _finding_is_verified(finding):
            outcome = "verified"
        elif _finding_is_rejected(finding):
            outcome = "rejected"
        else:
            outcome = "proposed"
        if kind and outcome != "rejected":
            # 实锤/待验证的汇点作为独立事件入图；被反驳的交给下面的 speculative，
            # 由回溯环在回滚时一起撤销——否则图里会留着一条"死假设"的汇点误导调度。
            operations.append((EVENT_SINK_FOUND, dict(sink_row)))
            sinks += 1
        if outcome == "verified":
            operations.append((EVENT_HYPOTHESIS_VERIFIED, dict(payload)))
            verified += 1
        elif outcome == "rejected":
            speculative = [dict(sink_row, speculative=True)] if kind else []
            target = _text(payload.get("target"))
            target_route = _route_id_of({"route_id": target}) if target else ""
            if target_route and target_route not in known_routes:
                speculative.append({"route_id": target, "method": "GET"})
            operations.append((EVENT_HYPOTHESIS_REJECTED, {
                **payload, "terminal": True, "speculative": speculative,
            }))
            rejected += 1
        else:
            operations.append((EVENT_HYPOTHESIS_PROPOSED, {**payload, "status": "proposed"}))
            proposed += 1
    record_events(project_root, operations)
    return {
        "verified": verified,
        "rejected": rejected,
        "proposed": proposed,
        "sinks": sinks,
        "finding_total": verified + rejected + proposed,
    }


def absorb_evidence(
    project_root: str | Path,
    records: Iterable[Mapping[str, Any]],
    *,
    round_no: int | None = None,
    stage_id: str | None = None,
) -> dict[str, Any]:
    """吸收 Life 已检查点的覆盖证据：把单元→端点链路补全，供 DAG 求值消费。"""
    operations: list[tuple[str, dict[str, Any]]] = []
    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        unit_id = _text(record.get("unit_id"))
        if not unit_id:
            continue
        worker = _text(record.get("worker_id")) or None
        operations.append((EVENT_EVIDENCE_CHECKPOINTED, {
            "unit_id": unit_id,
            "worker_id": worker,
            "status": _text(record.get("status")) or None,
            "artifact_path": _text(record.get("artifact_path")) or None,
            "target_fingerprint": _text(record.get("target_fingerprint")) or None,
            "round_no": round_no,
        }))
        operations.append((EVENT_UNIT_COVERED, {
            "unit_id": unit_id,
            "status": _text(record.get("status")) or "covered",
            "stage_id": stage_id,
            "worker_id": worker,
            "round_no": round_no,
        }))
    absorbed = len(operations) // 2
    record_events(project_root, operations)
    return {"units_absorbed": absorbed}


def finalize_round(
    project_root: str | Path,
    summary: Mapping[str, Any],
    *,
    round_no: int | None = None,
) -> dict[str, Any]:
    """把本轮吸收结果作为一条汇总事件写进日志（观测与断点续跑用）。"""
    return record_event(project_root, EVENT_GRAPH_ABSORBED, {
        "round_no": round_no, **dict(summary),
    })


# --------------------------------------------------------------------------
# 反射回溯 / 就地重放的可达入口
# --------------------------------------------------------------------------

def reflex_state(project_root: str | Path) -> dict[str, Any]:
    """回溯状态快照：历史、回滚审计、待重放计划与自动修包建议。"""
    graph = load_graph(project_root)
    if graph is None:
        return {"present": False}
    history = graph.reflex.get("backtrack_history") if isinstance(graph.reflex, Mapping) else {}
    return {
        "present": True,
        "backtrack_history": {
            hyp_id: len(records) for hyp_id, records in (history or {}).items()
            if isinstance(records, list)
        },
        "rollbacks": list(graph.reflex.get("rollbacks") or [])[-20:],
        "replay_pending": list(graph.reflex.get("replay_pending") or []),
        "auto_fix_suggestions": len(graph.reflex.get("auto_fix_suggestions") or []),
        "snapshots": graph.list_snapshots(),
        "schema_validation": dict(graph.meta.get("schema_validation") or {}),
    }


def replay_reflex_fix(
    project_root: str | Path,
    hypothesis_id: str,
    sender_fn: Callable[[dict[str, Any]], Mapping[str, Any]],
    *,
    patch_headers: Mapping[str, Any] | None = None,
    target_fingerprint: str | None = None,
) -> dict[str, Any]:
    """执行一次就地免 LLM 快速重放（优化项 10），并把结果记进图谱。

    本模块自己**不发包**：``sender_fn`` 由持有沙箱网络栈的调用方（worker / MCP 面）
    注入。post 路径只负责离线地产出 ``reflex["replay_pending"]`` 计划。调用方还必须
    在 callback 上设置由当前 Supervisor/沙箱产生的 ``target_fingerprint``；缺失或不匹配
    时 fail-closed，并保留待重放计划供正确目标重试。
    """
    if not callable(sender_fn):
        raise AttackGraphStoreError("replay_reflex_fix 需要可调用的 sender_fn")
    root = graph_dir(project_root)
    with _store_lock(root):
        graph = load_graph(project_root)
        if graph is None:
            raise AttackGraphStoreError("图谱尚未建立，无法执行就地重放")
        hyp_id = _text(hypothesis_id)
        headers = dict(patch_headers or {})
        pending_item: Mapping[str, Any] | None = None
        for item in reversed(list(graph.reflex.get("replay_pending") or [])):
            if isinstance(item, Mapping) and _text(item.get("hypothesis_id")) == hyp_id:
                pending_item = item
                if not headers:
                    headers = dict(item.get("patch_headers") or {})
                break
        requested_target = _text(target_fingerprint)
        plan_target = _text(pending_item.get("target_fingerprint")) if pending_item else ""
        expected_target = plan_target or ("" if pending_item is not None else requested_target)
        binding_error = ""
        if pending_item is not None and requested_target and requested_target != plan_target:
            binding_error = "调用方 target_fingerprint 与待重放计划不匹配"
        try:
            bound_target = _text(getattr(sender_fn, "target_fingerprint", ""))
        except Exception:
            bound_target = ""
        if binding_error:
            outcome = {
                "fast_replay_attempted": False,
                "success": False,
                "action": "blocked_target_binding",
                "error": binding_error,
            }
        elif not expected_target:
            outcome = {
                "fast_replay_attempted": False,
                "success": False,
                "action": "blocked_target_binding",
                "error": "重放计划缺少 target_fingerprint",
            }
        elif bound_target != expected_target:
            outcome = {
                "fast_replay_attempted": False,
                "success": False,
                "action": "blocked_target_binding",
                "error": "sender_fn 的 target_fingerprint 与重放目标不匹配",
            }
        else:
            engine = ReflexiveBacktrackingEngine(graph)
            outcome = engine.execute_in_situ_fast_replay(
                hyp_id,
                headers,
                sender_fn,
                target_fingerprint=expected_target,
            )
        node = graph.hypotheses.get(hyp_id)
        if isinstance(node, dict):
            node["fast_replay"] = {
                key: value for key, value in dict(outcome).items() if key != "advice"
            }
            node["fast_replay"]["at"] = time.time()
        pending = graph.reflex.setdefault("replay_pending", [])
        if outcome.get("action") != "blocked_target_binding":
            graph.reflex["replay_pending"] = [
                item for item in pending
                if not (isinstance(item, Mapping) and _text(item.get("hypothesis_id")) == hyp_id)
            ]
        record = {
            "seq": len(graph.events) + 1,
            "event_type": "fast_replay",
            "timestamp": time.time(),
            "payload": {
                "hypothesis_id": hyp_id,
                "patch_headers": headers,
                "target_fingerprint": expected_target or None,
            },
            "result": dict(outcome),
        }
        graph.events.append(record)
        graph.events = graph.events[-_ag.MAX_EVENT_JOURNAL:]
        _write_graph_locked(root, graph)
    return {**dict(outcome), "event": record}


def runnable_tasks(
    project_root: str | Path,
    completed_stages: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """按图谱当前拓扑求值流式 DAG 可执行任务（优化项 1）。"""
    graph = load_graph(project_root)
    if graph is None:
        return []
    return _ag.evaluate_runnable_tasks(graph, set(_texts(completed_stages)))


def stats(project_root: str | Path) -> dict[str, Any]:
    """图谱可观测概况（供 ``bugx skill status`` / MCP 面展示）。"""
    graph = load_graph(project_root)
    if graph is None:
        return {"present": False, "snapshot": str(snapshot_path(project_root))}
    illegal = graph.get_illegal_access_edges()
    return {
        "present": True,
        "snapshot": str(snapshot_path(project_root)),
        "version": graph.version,
        "nodes": {
            "roles": len(graph.roles),
            "routes": len(graph.routes),
            "parameters": len(graph.parameters),
            "sinks": len(graph.sinks),
            "hypotheses": len(graph.hypotheses),
        },
        "edges": len(graph.edges),
        "illegal_access_edges": len(illegal),
        "events": len(graph.events),
        "last_event": graph.events[-1]["event_type"] if graph.events else None,
        "units": len(graph.meta.get("units") or {}),
        "last_round": graph.meta.get("last_round"),
        "endpoints_ready": bool(graph.meta.get("endpoints_ready")),
        "schema_validation": dict(graph.meta.get("schema_validation") or {}),
        "backtrack_hypotheses": len(graph.reflex.get("backtrack_history") or {}),
        "rollbacks": len(graph.reflex.get("rollbacks") or []),
    }


def _extract_text_from_jsonl_stream(lines: list[str]) -> str | None:
    """从多行 JSON Lines (NDJSON) 事件流中智能聚合 AI Agent 输出的正文文本。

    支持 Cline (run_result / agent_event), OpenCode (text part streaming), Codex 等。
    """
    # 策略 A: 检查是否存在运行终结结果 (Cline run_result / done, Codex final)
    for line in reversed(lines):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                # Cline run_result
                if obj.get("type") == "run_result" and isinstance(obj.get("text"), str):
                    return obj["text"]
                # Cline agent_event done
                if obj.get("type") == "agent_event":
                    ev = obj.get("event")
                    if isinstance(ev, dict) and ev.get("type") == "done" and isinstance(ev.get("text"), str):
                        return ev["text"]
                # Codex or other run result
                if "result" in obj and isinstance(obj["result"], str):
                    return obj["result"]
        except Exception:
            continue

    # 策略 B: 汇聚 OpenCode 或流式增量文本 (opencode text parts)
    opencode_parts: list[str] = []
    has_opencode = False
    for line in lines:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("type") == "text":
                part = obj.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    opencode_parts.append(part["text"])
                    has_opencode = True
        except Exception:
            continue
    if has_opencode and opencode_parts:
        return "".join(opencode_parts)

    # 策略 C: Codex item/message stream
    codex_parts: list[str] = []
    has_codex = False
    for line in lines:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("type") in ("message", "item.created", "item.updated"):
                item = obj.get("item") if "item" in obj else obj
                if isinstance(item, dict):
                    content = item.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                                codex_parts.append(c["text"])
                                has_codex = True
                    elif isinstance(content, str):
                        codex_parts.append(content)
                        has_codex = True
        except Exception:
            continue
    if has_codex and codex_parts:
        return "".join(codex_parts)

    return None


def _repair_and_load_json(s: str) -> dict | list | None:
    """词法栈括号自愈与尾随逗号消除器。"""
    # 针对模型漏掉最外层大括号的情况（如开头就是 "key": ...）
    if ":" in s and not s.lstrip().startswith("{") and not s.lstrip().startswith("["):
        wrapped = "{\n" + s.strip().rstrip("}") + "\n}"
        cleaned = re.sub(r",\s*([\]\}])", r"\1", wrapped)
        try:
            val = json.loads(cleaned)
            if isinstance(val, (dict, list)):
                return val
        except Exception:
            pass

    start_idx = -1
    open_char = ""
    for idx, char in enumerate(s):
        if char in "{[":
            start_idx = idx
            open_char = char
            break
    if start_idx == -1:
        return None

    candidate_raw = s[start_idx:]
    close_char = "}" if open_char == "{" else "]"
    last_idx = candidate_raw.rfind(close_char)
    if last_idx != -1:
        balanced_candidate = candidate_raw[: last_idx + 1]
        try:
            val = json.loads(balanced_candidate)
            if isinstance(val, (dict, list)):
                return val
        except Exception:
            cleaned = re.sub(r",\s*([\]\}])", r"\1", balanced_candidate)
            try:
                val = json.loads(cleaned)
                if isinstance(val, (dict, list)):
                    return val
            except Exception:
                pass

    stack = []
    in_string = False
    escape = False
    for char in candidate_raw:
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string:
            if char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]":
                if stack and stack[-1] == char:
                    stack.pop()

    candidate = candidate_raw + "".join(reversed(stack)) if stack else candidate_raw
    cleaned = re.sub(r",\s*([\]\}])", r"\1", candidate)
    try:
        val = json.loads(cleaned)
        if isinstance(val, (dict, list)):
            return val
    except Exception:
        pass
    return None


def _parse_single_json(raw: str) -> dict | list | None:
    """从单体文本（可能含 Markdown 围栏或前后自然语言）中提取并修复 JSON。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    try:
        val = json.loads(s)
        if isinstance(val, (dict, list)):
            return val
    except Exception:
        pass

    code_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", s)
    for block in code_blocks:
        b_clean = block.strip()
        try:
            val = json.loads(b_clean)
            if isinstance(val, (dict, list)):
                return val
        except Exception:
            repaired = _repair_and_load_json(b_clean)
            if repaired is not None:
                return repaired

    return _repair_and_load_json(s)


def _unwrap_event_object(obj: dict) -> dict | list | None:
    """若对象为 CLI 外层事件包装（如 run_result / agent_event / item.created），解包提取内部 JSON。"""
    if not isinstance(obj, dict):
        return None
    # Cline run_result
    if obj.get("type") == "run_result" and isinstance(obj.get("text"), str):
        return _parse_single_json(obj["text"])
    # Cline agent_event done
    if obj.get("type") == "agent_event":
        ev = obj.get("event")
        if isinstance(ev, dict) and isinstance(ev.get("text"), str):
            return _parse_single_json(ev["text"])
    # Codex item / message
    if obj.get("type") in ("message", "item.created", "item.updated"):
        item = obj.get("item") if "item" in obj else obj
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        res = _parse_single_json(c["text"])
                        if res is not None:
                            return res
            elif isinstance(content, str):
                return _parse_single_json(content)
    # OpenCode text part
    if obj.get("type") == "text" and isinstance(obj.get("part"), dict):
        t = obj["part"].get("text")
        if isinstance(t, str):
            return _parse_single_json(t)
    # result string
    if "result" in obj and isinstance(obj["result"], str):
        return _parse_single_json(obj["result"])
    return None


def extract_resilient_json(text: str) -> dict | list | None:
    """终局加固补丁 21：容错级准结构化 JSON 自愈提取器 (Resilient Normalization Extractor)。

    支持主流 AI CLI 的 JSON Lines (NDJSON) 事件流提取（Cline, OpenCode, Codex），
    自动剥离 Markdown 围栏，消除尾随逗号，并通过词法栈自动闭合未匹配括号。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()

    # 1. 优先尝试作为直接的完整 JSON 或由 _parse_single_json 处理
    direct = _parse_single_json(raw)
    if direct is not None:
        if isinstance(direct, dict):
            unwrapped = _unwrap_event_object(direct)
            if unwrapped is not None:
                return unwrapped
        return direct

    # 2. 检测并处理 JSON Lines (NDJSON) 事件流（如 cline --json, opencode run --format json）
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 1:
        # 尝试从行式流中提取模型生成的文本内容
        extracted_text = _extract_text_from_jsonl_stream(lines)
        if extracted_text:
            parsed = _parse_single_json(extracted_text)
            if parsed is not None:
                return parsed

        # 若提取的文本未直接解析成功，检查各行自身是否为业务目标 JSON
        for line in reversed(lines):
            line_parsed = _parse_single_json(line)
            if isinstance(line_parsed, (dict, list)):
                if isinstance(line_parsed, dict) and any(
                    k in line_parsed for k in ("semantic_review", "hypotheses", "excluded_fixtures", "real_forms")
                ):
                    return line_parsed

    return None


def verify_hypothesis_anchor(
    project_root: str | Path,
    source_file: str,
    line: int,
    *,
    window: int = 5,
) -> bool:
    """终局加固补丁 11：滑窗语义模糊锚定门禁 (Fuzzy Window Anchor Gate)。

    以模型给出的行号为中心，在 [line - window, line + window] 范围内
    静态验证物理文件与代码行存在性，防止大模型行号微小漂移导致有效漏洞假设被误杀。
    """
    if not source_file:
        return False
    root = Path(project_root).resolve()
    target = (root / source_file).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    if not target.is_file():
        return False

    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False

    total_lines = len(lines)
    if total_lines == 0:
        return False

    # 若行号 <= 0，默认看文件头
    center = max(1, min(line, total_lines))
    start = max(1, center - window)
    end = min(total_lines, center + window)

    # 窗口内存在非空白代码行即视为锚定成功
    for idx in range(start - 1, end):
        line_content = lines[idx].strip()
        if line_content and not line_content.startswith("#") and not line_content.startswith("//"):
            return True
    return False


__all__ = [
    "ADVANCED_BY_ATTR",
    "ALL_EVENTS",
    "AttackGraphStoreError",
    "EVENT_ENDPOINTS_READY",
    "EVENT_ENDPOINT_DISCOVERED",
    "EVENT_EVIDENCE_CHECKPOINTED",
    "EVENT_GRAPH_ABSORBED",
    "EVENT_GRAPH_SEEDED",
    "EVENT_HYPOTHESIS_PROPOSED",
    "EVENT_HYPOTHESIS_REJECTED",
    "EVENT_HYPOTHESIS_VERIFIED",
    "EVENT_ROLE_OBSERVED",
    "EVENT_ROLLBACK",
    "EVENT_SINK_FOUND",
    "EVENT_UNIT_COVERED",
    "EVENT_UNIT_STAMPED",
    "SNAPSHOT_NAME",
    "UNIT_ATTR",
    "absorb_evidence",
    "absorb_findings",
    "absorb_sinks",
    "absorb_surface",
    "extract_resilient_json",
    "finalize_round",
    "graph_dir",
    "load_graph",
    "record_event",
    "record_events",
    "reflex_state",
    "replay_reflex_fix",
    "save_graph",
    "seed_from_units",
    "snapshot_path",
    "stamp_unit",
    "stats",
    "verify_hypothesis_anchor",
]
