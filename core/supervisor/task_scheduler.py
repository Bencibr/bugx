#!/usr/bin/env python3
"""task_scheduler.py — 下一代智能任务调度与高并发弹性引擎 (DOC-044)。

核心能力：
1. UnitPriorityScheduler: 基于 DAG 拓扑前置、AST 变更波及面与历史缺陷密度的多因子加权优先级调度；
2. AffinityBundler: 同文件/强调用关系单元的拓扑亲和性聚合与批处理；
3. AdaptiveConcurrencyController: 以机器可承载并发为基线，仅在真实 CPU/RAM 压力下进行背压；
4. ComplexityLeaseCalculator: 复杂度自适应租约时长推算；
5. ZombieDetector: 基于本地子进程存活性与心跳断流的僵死 Worker 毫秒级抢占回收探针。
"""

from __future__ import annotations

import ast
import ctypes
import errno
import itertools
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, Iterator

from core.planning import unit_kind

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 进程内共享调度器（DOC-140 调度提速）
# ---------------------------------------------------------------------------
# 调度器构造 = 全仓建图 + 影响网络 + 多个 JSON 工件加载，而无人值守的一轮
# 里它会被构造 2+N 次（workers() 两次 + 每个 worker turn 一次 + claim 兜底）。
# 同一 (root, HEAD, manifest mtime) 只构建一次；目标HEAD或清单变更即重建，
# 保证不会拿过期图做派发决策。
_SHARED_SCHEDULER_CACHE: dict[str, tuple[Any, str, float]] = {}
# C3：scheduler_history 临时文件唯一性计数器（配合 PID 防并发互踩）。
_TMP_COUNTER = itertools.count()


def _shared_cache_key_parts(root: Path) -> tuple[str, str]:
    """(清单内容 hash, 清单 mtime 兜底)。

    A2：原键用 git HEAD——无人值守每轮 auto-fix 都会 commit，HEAD 必变，
    缓存每轮失效、重付一次全仓索引（实测 5-8s）。而调度决策的真实输入是
    覆盖清单的单元集合，与 HEAD 无关。改用清单内容 hash 做主键：内容不变
    即命中（跨 commit 稳定），内容变才失效。mtime 仅作 hash 计算失败时的
    兜底，不参与正常判定。
    """
    try:
        import hashlib as _hl
        from core.coverage import module_coverage as _mc
        manifest = _mc.default_manifest_for_root(root)
        if manifest.is_file():
            data = manifest.read_bytes()
            return _hl.sha256(data).hexdigest()[:16], str(manifest.stat().st_mtime)
    except Exception:
        pass
    return "", "0"


def shared_scheduler(root: Path | str | None = None, **kwargs: Any) -> "UnitPriorityScheduler":
    """返回进程内共享调度器；策略输入变化时安全失效，避免重复全仓建图。"""
    key_root = str(Path(root).resolve()) if root else ""
    digest, mtime = _shared_cache_key_parts(Path(key_root)) if key_root else ("", "0")
    # unit_risks/surfaces/traversal strategy 都影响排序，必须进入 cache key。
    import hashlib as _hl, json as _json
    strategy_key = _hl.sha256(_json.dumps(kwargs, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]
    key = key_root + "|" + strategy_key
    cached = _SHARED_SCHEDULER_CACHE.get(key)
    if cached is not None:
        scheduler, cached_digest, cached_mtime = cached
        if cached_digest == digest and cached_mtime == mtime:
            return scheduler
    scheduler = UnitPriorityScheduler(root, **kwargs)
    if key_root:
        _SHARED_SCHEDULER_CACHE[key] = (scheduler, digest, mtime)
    return scheduler


def reset_shared_scheduler(root: Path | str | None = None) -> None:
    """清空共享缓存（测试或目标切换后用）。"""
    if root is None:
        _SHARED_SCHEDULER_CACHE.clear()
        return
    prefix = str(Path(root).resolve()) + "|"
    for cache_key in tuple(_SHARED_SCHEDULER_CACHE):
        if cache_key == str(Path(root).resolve()) or cache_key.startswith(prefix):
            _SHARED_SCHEDULER_CACHE.pop(cache_key, None)



# 命令行中出现的 BugX Worker 标志。用于在终止进程前独立证明「这确实是一个
# BugX Worker」——Worker 可以自报任意 PID，但无法让无关进程的命令行出现这些
# 标志。刻意使用小写比较（调用方先 casefold）。
WORKER_CMDLINE_MARKERS = (
    "bugx-worker", "bugx_worker", "--agent bugx", "bugx-worker.md",
)

TRAVERSAL_STRATEGIES = frozenset({"auto", "dfs", "bfs", "hybrid"})

#: 推荐租约的硬上限（LOG-06）。必须与以下两处逐字一致：
#:   * ``supervisor_models.MAX_LEASE_SECONDS``（核心侧接受上限）；
#:   * ``bugx_mcp_server`` 的 ``lease_seconds.inputSchema.maximum``。
#: 三处漂移会让「推荐值」与「接受上限」脱节：调用方按推荐值申请却拿到更长的
#: 租约，或反之。``tests/test_audit_supervisor.py`` 有一致性守护。
LEASE_SECONDS_CEILING = 3600.0
_SENSITIVE_SURFACES = {
    "auth": 3.0, "login": 3.0, "payment": 3.0, "billing": 3.0,
    "file": 2.0, "upload": 2.0, "download": 2.0,
    "rest": 2.0, "api": 2.0, "route": 2.0,
}


def extract_unit_scope(unit_id: str) -> tuple[str, str]:
    """从 coverage unit ID 提取 (file_path, symbol)。
    
    样例：
      - "内部:1:core/foo.py::func_a" -> ("core/foo.py", "func_a")
      - "内部:core/foo.py" -> ("core/foo.py", "")
      - "业务:CheckoutFlow" -> ("CheckoutFlow", "")
    """
    text = str(unit_id).strip()
    for prefix in unit_kind.PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    parts = text.split(":")
    if len(parts) > 1 and parts[0].isdigit():
        text = ":".join(parts[1:])
    if "::" in text:
        fp, sym = text.split("::", 1)
        return fp.strip(), sym.strip()
    return text.strip(), ""


# DOC-141：高危攻击面（命中即 deep）与高危路径词（语义等价信号）。
_HIGH_VALUE_SURFACES = {
    "auth", "login", "payment", "billing", "admin", "rbac",
    "token", "secret", "credential",
}
_HIGH_VALUE_PATH_TOKENS = (
    "security", "inject", "sql", "query", "traversal", "escape",
    "deserial", "exec", "upload", "download", "encrypt", "decrypt",
    "crypto", "cipher", "key", "aes", "pem", "rsa", "ssh", "database", "db",
)

_TRIVIAL_ACCESSOR_SYMBOLS = frozenset({
    "id", "name", "severity", "description", "reference", "fix_command", "fmt",
})


def _scope_tokens(value: str) -> set[str]:
    """Return exact path/symbol tokens, avoiding substring collisions."""
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def verification_tier(
    unit_id: str,
    risk_score: float = 0.0,
    surfaces: Iterable[str] | None = None,
) -> str:
    """Map a unit to a bounded verification depth without changing coverage truth.

    DOC-141 策略升级（面感感知）：原策略只对 auth/login/payment/billing 四个
    面给 deep，其余一律要 risk≥70。实测的漏报代价不对称——越权/注入/遍历类
    面的中度风险单元（risk≥50）同样值得 deep：这些面的缺陷单点代价高，
    浅验证的漏报成本远多于多花的验证 token。triage 侧语义不变。
    """
    path, symbol = extract_unit_scope(unit_id)
    normalized = path.casefold().replace("\\", "/")
    symbol_normalized = re.sub(r"@l[0-9]+$", "", symbol.casefold().strip())
    path_tokens = _scope_tokens(normalized)
    symbol_tokens = _scope_tokens(symbol_normalized)
    sensitive = {
        key for key in _SENSITIVE_SURFACES
        if key in path_tokens or key in symbol_tokens
    }
    # 高危面：命中即 deep（不限风险分）——这些面的存在本身就是强信号。
    high_value = sensitive.intersection(_HIGH_VALUE_SURFACES)
    # 语义面：路径里出现这些词同样按高危面处理（query/sql/inject/traversal…）。
    semantic_hit = bool(
        path_tokens.intersection(_HIGH_VALUE_PATH_TOKENS)
        or symbol_tokens.intersection(_HIGH_VALUE_PATH_TOKENS)
    )
    try:
        score = float(risk_score)
    except (TypeError, ValueError):
        score = 0.0
    explicit_surfaces = {str(item).casefold() for item in (surfaces or ())}
    if score >= 70.0 or explicit_surfaces.intersection(_HIGH_VALUE_SURFACES):
        return "deep"
    if symbol_normalized in _TRIVIAL_ACCESSOR_SYMBOLS:
        return "triage"
    if score >= 70.0 or high_value or semantic_hit:
        return "deep"
    if score >= 50.0 and sensitive:
        # 中度风险 + 任一敏感面：deep 的期望收益高于 token 成本。
        return "deep"
    if score < 30.0 and not sensitive:
        return "triage"
    return "standard"


class UnitPriorityScheduler:
    """DAG 拓扑感知、遍历策略与缺陷密度驱动的智能优先级调度器。"""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        project_root: Path | str | None = None,
        graph: dict[str, Any] | None = None,
        impact_network: dict[str, Any] | None = None,
        defect_modules: set[str] | None = None,
        unit_risks: Mapping[str, float] | None = None,
        traversal_strategy: str = "auto",
        surfaces: Iterable[str] | None = None,
        **kwargs: Any,
    ) -> None:
        target_root = project_root if project_root is not None else root
        self.root = Path(target_root).resolve() if target_root else None
        self.traversal_strategy = self._normalize_traversal_strategy(traversal_strategy)
        self.last_strategy_info: dict[str, Any] = {}
        self.in_degrees: dict[str, int] = defaultdict(int)
        self.adjacency: dict[str, set[str]] = defaultdict(set)
        self.reverse_adjacency: dict[str, set[str]] = defaultdict(set)
        self.graph_nodes: set[str] = set()
        self.graph_edge_count = 0
        self.changed_files: set[str] = set()
        self.impacted_files: set[str] = set()
        self.defect_modules: set[str] = set(defect_modules or ())
        self.unit_risks: dict[str, float] = dict(unit_risks or {})
        self.surfaces: set[str] = {str(item).casefold() for item in (surfaces or ())}
        self.dynamic_priorities: dict[str, float] = defaultdict(float)
        # 调度热路径缓存（DOC-140 调度提速）：score_unit 的 size_factor 只需
        # 行数分档，按 fp 缓存避免 rank_units 内 2N 次整文件读；历史批量写
        # 标记让一批 advance_cycle/record_audit_attempt 只落盘一次。
        self._line_counts: dict[str, int] = {}
        self._defer_save: int = 0
        self._history_dirty: bool = False

        if not self.unit_risks and self.root:
            self._try_load_unit_risks()

        if graph is not None:
            self._load_graph(graph)
        elif self.root:
            self._try_load_project_graph()

        if impact_network is not None:
            self.changed_files = set(impact_network.get("changed_files", []))
            self.impacted_files = set(impact_network.get("impacted_files", []))
        elif self.root:
            self._try_load_impact_network()

        if self.root:
            self._try_load_attack_graph()

        # DOC-137 2.0 / D137-08: 基于 UCB1 多臂老虎机的探索与深度利用自适应平衡
        self.enable_ucb1: bool = bool(kwargs.get("enable_ucb1", True))
        self.ucb1_c: float = float(kwargs.get("ucb1_c", 40.0))
        self.total_trials: int = 0
        self.unit_trials: dict[str, int] = defaultdict(int)
        self.unit_rewards: dict[str, float] = defaultdict(float)
        self.unit_unpicked_cycles: dict[str, int] = defaultdict(int)

        if self.root:
            self._try_load_scheduler_history()

    def _history_file_path(self) -> Path | None:
        if not self.root:
            return None
        return self.root / ".bugx" / "state" / "scheduler_history.json"

    def _try_load_scheduler_history(self) -> None:
        path = self._history_file_path()
        if not path or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.total_trials = int(data.get("total_trials", 0))
            for k, v in (data.get("unit_trials") or {}).items():
                self.unit_trials[k] = int(v)
            for k, v in (data.get("unit_rewards") or {}).items():
                self.unit_rewards[k] = float(v)
            for k, v in (data.get("unit_unpicked_cycles") or {}).items():
                self.unit_unpicked_cycles[k] = int(v)
        except Exception:
            pass

    def _source_line_count(self, fp: str) -> int | None:
        """源文件行数（按 fp 缓存；读失败返回 None 走默认分档）。"""
        if fp in self._line_counts:
            return self._line_counts[fp]
        if not self.root:
            return None
        try:
            lines = (self.root / fp).read_text(
                encoding="utf-8", errors="ignore").count("\n")
        except OSError:
            return None
        self._line_counts[fp] = lines
        return lines

    @contextmanager
    def batch_history(self) -> Iterator[None]:
        """批量更新调度历史的上下文：期间的 save_history 只标记脏，
        退出时统一落盘一次（把"每单元一次全量 JSON 写"降为"每批一次"）。"""
        self._defer_save += 1
        try:
            yield
        finally:
            self._defer_save = max(0, self._defer_save - 1)
            if self._defer_save == 0 and self._history_dirty:
                self._history_dirty = False
                self.save_history()

    def save_history(self) -> None:
        path = self._history_file_path()
        if not path:
            return
        if self._defer_save:
            self._history_dirty = True
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "total_trials": self.total_trials,
                "unit_trials": dict(self.unit_trials),
                "unit_rewards": dict(self.unit_rewards),
                "unit_unpicked_cycles": dict(self.unit_unpicked_cycles),
                "updated_at": time.time(),
            }
            # C3：唯一临时文件名。原固定 .tmp 在并发写下互相覆盖，可能把
            # 半截内容 replace 上去（且被下方 except 静默吞掉）。
            tmp = path.with_name(
                f"{path.name}.{os.getpid()}.{next(_TMP_COUNTER)}.tmp"
            )
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def advance_cycle(
        self,
        selected_unit_id: str | None = None,
        candidate_units: Sequence[str] | None = None,
    ) -> None:
        """推进调度轮次：重置入选单元饥饿度，增加未入选候选单元饥饿度。"""
        if candidate_units:
            for u in candidate_units:
                if u != selected_unit_id:
                    self.unit_unpicked_cycles[u] += 1
        if selected_unit_id:
            self.unit_unpicked_cycles[selected_unit_id] = 0
        self.save_history()

    def _attack_graph_snapshot_path(self) -> Path | None:
        """生产者（attack_graph_store）实际输出的攻击图快照路径。"""
        if not self.root:
            return None
        try:
            import core.bugx_paths as bugx_paths
            scope = bugx_paths.require(self.root)
            candidate = scope.polling_root / "attack-graph-snapshot.json"
            return candidate if candidate.is_file() else None
        except Exception:
            return None

    def _try_load_attack_graph(self) -> None:
        """DOC-137 / D137-03: 从动态攻击面图谱中提取利用链出度，动态衍生下游关联单元优先级。"""
        if not self.root:
            return
        try:
            # DOC-141 L2：读取路径必须对齐**生产者**的实际输出。生产者
            # （attack_graph_store.save_graph）写 ``polling_root/attack-graph-snapshot.json``，
            # 而原实现只找 .bugx/knowledge/attack-graph.json 与
            # .bugx/pollings/*/state/attack-graph.json——两条路都接不上，
            # 高危边动态加权（+25/边）从未生效。现在以生产者路径为主，
            # 旧路径保留为兼容回退。
            ag_path = self._attack_graph_snapshot_path()
            if ag_path is None or not ag_path.is_file():
                legacy = self.root / ".bugx" / "knowledge" / "attack-graph.json"
                if legacy.is_file():
                    ag_path = legacy
            if not ag_path.is_file():
                candidates = sorted(self.root.glob(".bugx/pollings/*/state/attack-graph.json"), reverse=True)
                if candidates:
                    ag_path = candidates[0]
            if ag_path.is_file():
                raw = json.loads(ag_path.read_text(encoding="utf-8"))
                edges = raw.get("edges", []) if isinstance(raw, dict) else []
                for edge in edges:
                    if not isinstance(edge, dict):
                        continue
                    rel = str(edge.get("relation", "")).upper()
                    if any(marker in rel for marker in ("EXPLOITED", "UNAUTHORIZED", "IDOR", "ESCALATION", "UNAUTHENTICATED")):
                        target = edge.get("to") or edge.get("target")
                        target_file = self._graph_file_scope(target)
                        if target_file:
                            self.dynamic_priorities[target_file] += 25.0
        except Exception:
            pass

    def _try_load_unit_risks(self) -> None:
        """从项目已持久化的 project-profile.json 与 AI 语义侦察成果自动提取风险评分映射 (DOC-060, DOC-241)。"""
        if not self.root:
            return
        try:
            profile_path = self.root / ".bugx" / "knowledge" / "project-profile.json"
            if profile_path.is_file():
                import json
                data = json.loads(profile_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    risk_metrics = data.get("risk_metrics") or {}
                    for hotspot in risk_metrics.get("top_risk_hotspots") or []:
                        if isinstance(hotspot, dict) and "file" in hotspot:
                            self.unit_risks[str(hotspot["file"])] = float(hotspot.get("risk_score", 50.0))
        except Exception:
            pass
        # DOC-241: 结合攻击图谱 AI 假说与语义审查重点焦点 (Priority Focus) 优先置顶
        try:
            from core.autonomous_cve import attack_graph_store
            for hypo in attack_graph_store.get_hypotheses(self.root):
                src = hypo.get("source_file") or hypo.get("target")
                if src:
                    self.unit_risks[str(src)] = max(self.unit_risks.get(str(src), 0.0), 90.0)
        except Exception:
            pass
        try:
            rev_file = self.root / ".bugx" / "state" / "l1-semantic-review.json"
            if rev_file.is_file():
                import json
                rev_data = json.loads(rev_file.read_text(encoding="utf-8"))
                p_focus = (rev_data.get("audit_strategy_verdict") or {}).get("priority_focus") or []
                for pf in p_focus:
                    if pf:
                        self.unit_risks[str(pf)] = max(self.unit_risks.get(str(pf), 0.0), 95.0)
        except Exception:
            pass

    @staticmethod
    def _normalize_traversal_strategy(value: str | None) -> str:
        strategy = str(value or "auto").strip().lower()
        return strategy if strategy in TRAVERSAL_STRATEGIES else "auto"

    @staticmethod
    def _graph_file_scope(value: object) -> str:
        clean = str(value or "").strip()
        for prefix in ("file:", "symbol:"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
                break
        return clean.split("::", 1)[0].strip()


    def _load_graph(self, graph: dict[str, Any]) -> None:
        edges = graph.get("edges", [])
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = edge.get("source") or edge.get("from")
            target = edge.get("target") or edge.get("to")
            if target:
                clean_target = str(target)
                for prefix in ("file:", "symbol:"):
                    if clean_target.startswith(prefix):
                        clean_target = clean_target[len(prefix):]
                        break
                fp = clean_target.split("::")[0]
                self.in_degrees[fp] += 1
                self.in_degrees[clean_target] += 1
            source_scope = self._graph_file_scope(source)
            target_scope = self._graph_file_scope(target)
            if not source_scope or not target_scope or source_scope == target_scope:
                continue
            self.graph_nodes.update((source_scope, target_scope))
            if target_scope not in self.adjacency[source_scope]:
                self.adjacency[source_scope].add(target_scope)
                self.reverse_adjacency[target_scope].add(source_scope)
                self.graph_edge_count += 1

    def _risk_for_unit(
        self,
        unit_id: str,
        risks: Mapping[str, float],
    ) -> float:
        # DOC-141 提速：O(1) 精确键优先； miss 时按长度降序找匹配——
        # 原实现对每个未命中单元线性扫描整张风险表（O(N·R)）。
        fp, _ = extract_unit_scope(unit_id)
        value = risks.get(unit_id, risks.get(fp))
        if value is None and risks:
            cached_sorted = getattr(self, "_cached_sorted_risks", None)
            if (
                cached_sorted is None
                or cached_sorted[0] != id(risks)
                or len(cached_sorted[1]) != len(risks)
            ):
                sorted_keys = [k for k in sorted(risks, key=len, reverse=True) if k]
                self._cached_sorted_risks = (id(risks), sorted_keys)
            else:
                sorted_keys = cached_sorted[1]
            for risk_path in sorted_keys:
                if risk_path == fp or fp.endswith(risk_path) or risk_path.endswith(fp):
                    value = risks[risk_path]
                    break
        try:
            return max(float(value or 0.0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _graph_traversal(self, risks: Mapping[str, float]) -> dict[str, Any]:
        """生成确定性的图遍历顺序及深度/层宽指标。"""
        if not self.graph_nodes:
            return {
                "order": [], "dfs_order": [], "depths": {}, "max_depth": 0,
                "max_frontier": 0, "avg_out_degree": 0.0,
            }

        def node_key(node: str) -> tuple[float, str]:
            return (-self._risk_for_unit(node, risks), node)

        roots = sorted(
            (node for node in self.graph_nodes if not self.reverse_adjacency.get(node)),
            key=node_key,
        )
        if not roots:
            roots = sorted(self.graph_nodes, key=node_key)

        order: list[str] = []
        depths: dict[str, int] = {}
        seen: set[str] = set()
        frontier_widths: dict[int, int] = defaultdict(int)

        queue: deque[tuple[str, int]] = deque((root, 0) for root in roots)
        while queue:
            node, depth = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            order.append(node)
            depths[node] = depth
            frontier_widths[depth] += 1
            for child in sorted(self.adjacency.get(node, ()), key=node_key):
                if child not in seen:
                    queue.append((child, depth + 1))

        for node in sorted(self.graph_nodes - seen, key=node_key):
            order.append(node)
            depths[node] = 0
            frontier_widths[0] += 1

        dfs_order: list[str] = []
        dfs_seen: set[str] = set()
        stack = list(reversed(roots))
        while stack:
            node = stack.pop()
            if node in dfs_seen:
                continue
            dfs_seen.add(node)
            dfs_order.append(node)
            children = sorted(self.adjacency.get(node, ()), key=node_key)
            stack.extend(reversed(children))
        stack.extend(sorted(self.graph_nodes - dfs_seen, key=node_key, reverse=True))
        while stack:
            node = stack.pop()
            if node in dfs_seen:
                continue
            dfs_seen.add(node)
            dfs_order.append(node)
            stack.extend(reversed(sorted(self.adjacency.get(node, ()), key=node_key)))

        return {
            "order": order,
            "dfs_order": dfs_order,
            "depths": depths,
            "max_depth": max(depths.values(), default=0),
            "max_frontier": max(frontier_widths.values(), default=0),
            "avg_out_degree": self.graph_edge_count / max(len(self.graph_nodes), 1),
        }

    def _select_traversal_strategy(
        self,
        units: Sequence[str],
        risks: Mapping[str, float],
    ) -> tuple[str, str, dict[str, Any]]:
        traversal = self._graph_traversal(risks)
        candidate_nodes = {
            extract_unit_scope(unit)[0]
            for unit in units
            if extract_unit_scope(unit)[0] in self.graph_nodes
        }
        risk_values = [self._risk_for_unit(unit, risks) for unit in units]
        risk_total = sum(risk_values)
        risk_concentration = max(risk_values, default=0.0) / risk_total if risk_total else 0.0
        metrics = {
            "graph_nodes": len(self.graph_nodes),
            "graph_edges": self.graph_edge_count,
            "candidate_graph_nodes": len(candidate_nodes),
            "max_depth": traversal["max_depth"],
            "max_frontier": traversal["max_frontier"],
            "avg_out_degree": traversal["avg_out_degree"],
            "risk_concentration": risk_concentration,
            "order": traversal["order"],
            "depths": traversal["depths"],
        }
        if self.traversal_strategy != "auto":
            strategy = self.traversal_strategy
            metrics["order"] = traversal["dfs_order"] if strategy == "dfs" else traversal["order"]
            return strategy, f"显式指定 {strategy}", metrics
        if len(candidate_nodes) < 3 or self.graph_edge_count < 2:
            return "hybrid", "图规模不足，保留风险与评分排序", metrics
        if (
            traversal["max_depth"] >= 4
            and (
                traversal["max_depth"] >= 2 * max(traversal["max_frontier"], 1)
                or risk_concentration >= 0.45
            )
        ):
            metrics["order"] = traversal["dfs_order"]
            return "dfs", "图较深或风险集中，优先沿依赖链深入", metrics
        if traversal["max_frontier"] >= 3 or traversal["avg_out_degree"] >= 1.75:
            return "bfs", "图层较宽，优先铺开同层单元", metrics
        return "hybrid", "深度与层宽均衡，混合风险和遍历顺序", metrics

    def strategy_info(
        self,
        units: Sequence[str],
        unit_risks: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        """返回本批次自动选择的遍历策略及可观测指标。"""
        risks = dict(self.unit_risks)
        if unit_risks:
            risks.update(unit_risks)
        strategy, reason, metrics = self._select_traversal_strategy(units, risks)
        info = {"strategy": strategy, "reason": reason, **metrics}
        self.last_strategy_info = info
        return info

    def _try_load_project_graph(self) -> None:
        if not self.root:
            return
        try:
            from core.runtime import project_graph
            g = project_graph.build_graph(self.root)
            self._load_graph(g)
        except Exception:
            pass

    def _try_load_impact_network(self) -> None:
        if not self.root:
            return
        try:
            from core.planning import impact_pruner
            net = impact_pruner.compute_impact_network(self.root, depth=2)
            self.changed_files = set(net.get("changed_files", []))
            self.impacted_files = set(net.get("impacted_files", []))
        except Exception:
            pass

    def record_defect(self, unit_id: str, severity: str = "HIGH") -> None:
        """记录缺陷反馈，动态提高该单元及其关联依赖的调度权重。"""
        fp, _ = extract_unit_scope(unit_id)
        self.defect_modules.add(fp)
        bonus = 150.0 if severity.upper() in {"CRITICAL", "HIGH"} else 60.0
        self.dynamic_priorities[unit_id] += bonus
        self.dynamic_priorities[fp] += bonus

        # 联动 UCB1 多臂老虎机回报累积
        reward = 3.0 if severity.upper() in {"CRITICAL", "HIGH"} else 1.0
        self.unit_rewards[unit_id] += reward
        if fp != unit_id:
            self.unit_rewards[fp] += reward

    def record_audit_attempt(
        self,
        unit_id: str,
        reward: float = 0.0,
        findings_count: int = 0,
    ) -> None:
        """DOC-137 2.0 / D137-08: 记录一次审计派发与探索尝试，更新 UCB1 臂拉动次数与回报，并重置 Aging 周期。"""
        self.total_trials += 1
        self.unit_trials[unit_id] += 1
        self.unit_unpicked_cycles[unit_id] = 0
        effective_reward = reward if reward > 0.0 else float(findings_count)
        fp, _ = extract_unit_scope(unit_id)
        if fp != unit_id:
            self.unit_trials[fp] += 1
            self.unit_unpicked_cycles[fp] = 0
        if effective_reward > 0.0:
            self.unit_rewards[unit_id] += effective_reward
            if fp != unit_id:
                self.unit_rewards[fp] += effective_reward
        if self.root:
            self.save_history()

    def score_unit(
        self,
        unit_id: str,
        coverage_status: str = "pending",
        risk_score: float | None = None,
    ) -> float:
        """计算单元综合优先级得分（得分越高越优先出队）。"""
        score = 0.0
        # 1. 阶段绝对硬边界：内部优先于业务
        if unit_kind.is_internal(unit_id):
            score += 1000.0
        elif not unit_kind.is_business(unit_id):
            score += 500.0

        # 2. 债务重试加权：未完成债务优先于全新单元
        if coverage_status in {"partial", "unverified", "stale", "blocked"}:
            score += 50.0

        fp, sym = extract_unit_scope(unit_id)

        # 3. 拓扑依赖加权：入度越高说明越是公共基础库/被依赖核心
        in_deg = self.in_degrees.get(fp, 0)
        if sym:
            in_deg = max(in_deg, self.in_degrees.get(f"{fp}::{sym}", 0))
        score += min(in_deg, 20) * 5.0

        # 4. AST 变更波及面加权
        if fp in self.changed_files:
            score += 200.0
        elif fp in self.impacted_files:
            score += 80.0

        # 5. 历史缺陷聚集加权
        if fp in self.defect_modules:
            score += 100.0

        # 6. 静态风险分数加权
        r_score = risk_score
        if r_score is None:
            r_score = self._risk_for_unit(unit_id, self.unit_risks)
        bounded_risk = min(max(float(r_score or 0.0), 0.0), 100.0)
        surface_weight = max(
            [_SENSITIVE_SURFACES[key] for key in _SENSITIVE_SURFACES
             if key in fp.casefold()],
            default=1.0,
        )
        size_factor = 1.0
        if self.root:
            # 行数按 fp 缓存：rank_units 对同一单元多次调用 score_unit，
            # 整文件读只需首次发生一次。
            lines = self._source_line_count(fp)
            if lines is not None:
                size_factor = 1.15 if lines > 800 else 1.05 if lines > 300 else 1.0
        score += bounded_risk * surface_weight * size_factor * 1.5

        # 7. 动态反馈加权 (自适应重规划抢跑)
        score += self.dynamic_priorities.get(unit_id, self.dynamic_priorities.get(fp, 0.0))

        # 7.1 DOC-138 / S2-11: 消费显式配置的目标 surfaces，对命中指定攻击面的单元加权
        if self.surfaces:
            fp_lower = fp.casefold()
            sym_lower = (sym or "").casefold()
            if any(s in fp_lower or s in sym_lower for s in self.surfaces):
                score += 150.0

        # 8. DOC-137 2.0 / D137-08: 基于 UCB1 多臂老虎机的探索与深度利用平衡加权
        if self.enable_ucb1:
            import math
            n_u = self.unit_trials.get(unit_id, self.unit_trials.get(fp, 0))
            r_u = self.unit_rewards.get(unit_id, self.unit_rewards.get(fp, 0.0))
            exploitation = (r_u / (n_u + 1)) * 50.0
            n_total = max(self.total_trials, 1)
            exploration = self.ucb1_c * math.sqrt(2.0 * math.log(n_total + 1) / (n_u + 1))
            score += exploitation + exploration

        # 9. Aging 饥饿防御：未被调度轮次线性补偿，防止中低风险单元长期死锁
        unpicked = self.unit_unpicked_cycles.get(unit_id, self.unit_unpicked_cycles.get(fp, 0))
        score += min(unpicked * 15.0, 300.0)

        return score

    def rank_units(
        self,
        units: Sequence[str],
        coverage: Mapping[str, str] | None = None,
        unit_risks: Mapping[str, float] | None = None,
        update_aging: bool = False,
    ) -> list[str]:
        """对候选单元进行稳定多因子与 DFS/BFS/Hybrid 优先级排序。"""
        cov = coverage or {}
        risks = dict(self.unit_risks)
        if unit_risks:
            risks.update(unit_risks)
        info = self.strategy_info(units, unit_risks=risks)
        traversal_order = {node: index for index, node in enumerate(info["order"])}
        depths = info["depths"]
        # DOC-141 提速：score_unit 每单元只算一次并复用——原实现在 scored
        # 列表、traversal_key、hybrid 排序键里对同一单元重复调用（每次含
        # 整文件行数读取），2N 次起。
        score_by_unit = {
            u: self.score_unit(
                u, cov.get(u, "pending"),
                risk_score=self._risk_for_unit(u, risks),
            )
            for u in units
        }
        scored = [(score_by_unit[u], u) for u in units]
        fallback_rank = len(traversal_order)

        def traversal_key(unit_id: str) -> tuple[int, int, float, str]:
            fp, _ = extract_unit_scope(unit_id)
            stage = 0 if unit_kind.is_internal(unit_id) else 1
            return (
                stage,
                traversal_order.get(fp, fallback_rank),
                -score_by_unit[unit_id],
                unit_id in mismatched,
                unit_id,
            )

        strategy = info["strategy"]
        # 平台亲和：宿主无法验证的跨平台专属单元沉底，避免空转调度。
        try:
            from core.coverage import module_coverage as _mc
            host = _mc.host_platform()
            mismatched = {
                u for u in units if (pu := _mc.unit_platform(u)) and pu != host
            }
        except Exception:
            mismatched = set()
        if strategy == "dfs":
            scored.sort(key=lambda item: traversal_key(item[1]))
        elif strategy == "bfs":
            scored.sort(key=lambda item: (
                0 if unit_kind.is_internal(item[1]) else 1,
                depths.get(extract_unit_scope(item[1])[0], fallback_rank),
                traversal_order.get(extract_unit_scope(item[1])[0], fallback_rank),
                -item[0],
                item[1],
            ))
        else:
            risk_values = [self._risk_for_unit(unit_id, risks) for _, unit_id in scored]
            priority_values = risk_values if any(risk_values) else [score for score, _ in scored]
            value_min = min(priority_values, default=0.0)
            value_span = max(priority_values, default=0.0) - value_min
            for index, (score, unit_id) in enumerate(scored):
                fp, _ = extract_unit_scope(unit_id)
                rank = traversal_order.get(fp, fallback_rank + index)
                traversal_value = (
                    1.0 - rank / max(fallback_rank + len(scored) - 1, 1)
                    if traversal_order else 0.0
                )
                risk_value = (
                    (priority_values[index] - value_min) / value_span
                    if value_span else 0.0
                )
                scored[index] = (
                    0.6 * risk_value + 0.4 * traversal_value,
                    unit_id,
                )
            scored.sort(key=lambda item: (
                0 if unit_kind.is_internal(item[1]) else 1,
                -item[0],
                -score_by_unit[item[1]],
                item[1],
            ))
        ranked = [u for _, u in scored]
        if update_aging and ranked:
            self.advance_cycle(selected_unit_id=ranked[0], candidate_units=ranked)
        return ranked



class AffinityBundler:
    """拓扑内聚单元亲和性调度与打包器 (DOC-044)。"""

    @staticmethod
    def bundle_units(
        units: Sequence[str],
        max_bundle_size: int = 3,
    ) -> list[list[str]]:
        """将高内聚单元（同源码文件）聚合成任务包。
        
        约束：
        - 内部单元与业务单元绝不混包；
        - 单包单元数量受限在 [1, max_bundle_size] (上限 5)；
        - 保持排序优先级：首个单元优先级最高的文件包先产出。
        """
        limit = max(1, min(max_bundle_size, 5))
        if limit == 1:
            return [[u] for u in units]

        # 区分阶段
        internal_units = [u for u in units if unit_kind.is_internal(u)]
        business_units = [u for u in units if not unit_kind.is_internal(u)]

        bundles: list[list[str]] = []

        for group in (internal_units, business_units):
            if not group:
                continue
            # 按 file_path 聚合，同时记录首次出现的顺序
            file_to_units: dict[str, list[str]] = defaultdict(list)
            file_order: list[str] = []
            for u in group:
                fp, _ = extract_unit_scope(u)
                if fp not in file_to_units:
                    file_order.append(fp)
                file_to_units[fp].append(u)

            # 组装 bundles
            for fp in file_order:
                u_list = file_to_units[fp]
                for i in range(0, len(u_list), limit):
                    bundles.append(u_list[i:i + limit])

        return bundles

    @staticmethod
    def rebalance_for_parallelism(
        bundles: Sequence[Sequence[str]], desired_workers: int,
    ) -> list[list[str]]:
        """在亲和包不足以填满槽位时，安全退化为单元级 Bundle。"""
        normalized = [list(bundle) for bundle in bundles if bundle]
        if desired_workers <= len(normalized):
            return normalized
        # ponytail: only split when affinity would leave slots idle; keep the
        # ordered units and existing resource isolation, no second packer.
        return [[unit] for bundle in normalized for unit in bundle]


class AdaptiveConcurrencyController:
    """自适应弹性并发与背压调节引擎 (DOC-044)。"""

    def __init__(
        self,
        min_concurrency: int = 2,
        max_concurrency: int | None = None,
        target_cpu_load: float = 0.85,
    ) -> None:
        self.min_concurrency = max(1, min_concurrency)
        if max_concurrency is None:
            try:
                from .supervisor_models import resolve_system_concurrency
                _, default_ceil = resolve_system_concurrency()
            except Exception:
                default_ceil = 8
            max_concurrency = default_ceil
        self.max_concurrency = max(self.min_concurrency, max_concurrency)
        self.target_cpu_load = target_cpu_load
        self.concurrency_window: float = float(self.min_concurrency)

    def get_system_pressure(self) -> float:
        """获取系统实时压力指数（0.0 ~ 2.0）。

        Windows 无 os.getloadavg：用最近 1 秒忙进程计数 / 逻辑核数近似
        （psutil 缺失时的轻量探针），避免恒定 0.5 伪值让 AIMD 永远按
        半载决策。
        """
        cores = os.cpu_count() or 1
        try:
            load1 = os.getloadavg()[0]
            return max(0.0, float(load1) / float(cores))
        except (AttributeError, OSError):
            pass
        try:
            import ctypes

            idle_time = ctypes.c_ulonglong()
            kernel_times = ctypes.c_ulonglong()
            user_times = ctypes.c_ulonglong()

            class _FT(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                            ("dwHighDateTime", ctypes.c_ulong)]

            idle, kernel, user = _FT(), _FT(), _FT()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user),
            ):
                return 0.5

            def _u64(ft: "_FT") -> int:
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            total = (_u64(kernel) - _u64(idle)) + _u64(user)
            busy = _u64(kernel) + _u64(user)
            if total <= 0:
                return 0.5
            cpu_percent = min(100.0, busy / total * 100.0)
            return max(0.0, cpu_percent / 100.0 / 1.0)
        except Exception:
            return 0.5

    def _get_system_load(self) -> tuple[float, float | None]:
        """兼容接口：返回 (cpu_pressure, ram_usage_percent)。"""
        return self.get_system_pressure(), self.get_memory_pressure()

    @staticmethod
    def get_memory_pressure() -> float | None:
        """读取当前系统内存占用；探针不可用时返回 None。"""
        if sys.platform.startswith("linux"):
            try:
                values: dict[str, float] = {}
                for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                    key, _, raw = line.partition(":")
                    if key in {"MemTotal", "MemAvailable"}:
                        values[key] = float(raw.strip().split()[0])
                total = values.get("MemTotal", 0.0)
                available = values.get("MemAvailable")
                if total > 0 and available is not None:
                    return max(0.0, min(100.0, (1.0 - available / total) * 100.0))
            except (OSError, ValueError, IndexError):
                return None
            return None
        if sys.platform == "darwin":
            try:
                total = int(subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, check=False, timeout=2,
                ).stdout.strip())
                output = subprocess.run(
                    ["vm_stat"], capture_output=True, text=True,
                    check=False, timeout=2,
                ).stdout
                page_match = re.search(r"page size of (\d+) bytes", output)
                if total <= 0 or not page_match:
                    return None
                available_pages = 0
                for name in (
                    "Pages free", "Pages inactive", "Pages speculative",
                    "Pages purgeable", "File-backed pages",
                ):
                    match = re.search(rf"^{re.escape(name)}:\\s+(\d+)", output, re.MULTILINE)
                    if match:
                        available_pages += int(match.group(1))
                available = available_pages * int(page_match.group(1))
                return max(0.0, min(100.0, (1.0 - available / total) * 100.0))
            except (OSError, ValueError, subprocess.SubprocessError):
                return None
        if os.name == "nt":
            try:
                class MemoryStatusEx(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                status = MemoryStatusEx()
                status.dwLength = ctypes.sizeof(MemoryStatusEx)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                    return float(status.dwMemoryLoad)
            except (AttributeError, OSError, TypeError, ValueError):
                return None
        return None

    def compute_capacity(
        self,
        current_active: int,
        budget: Any,
        run_state: Any = None,
        state: Any = None,
    ) -> int:
        """按机器承载能力计算容量，仅在真实 CPU/RAM 压力下背压。"""
        actual_state = run_state if run_state is not None else state
        adaptive = getattr(budget, "adaptive_concurrency", True)
        configured_max = getattr(budget, "max_concurrency", None)
        if configured_max is None:
            try:
                from .supervisor_models import resolve_system_concurrency
                configured_max = resolve_system_concurrency()[0]
            except Exception:
                configured_max = 4
        if not adaptive or not getattr(budget, "require_parallel", True) or configured_max <= 1:
            return max(0, configured_max - current_active)

        c_min = max(1, int(getattr(budget, "min_concurrency", self.min_concurrency)))
        c_max = getattr(budget, "max_concurrency_ceiling", None)
        if c_max is None:
            try:
                from .supervisor_models import resolve_system_concurrency
                c_max = resolve_system_concurrency()[1]
            except Exception:
                c_max = self.max_concurrency
        configured_max = max(c_min, int(configured_max))
        c_max = max(configured_max, int(c_max))
        # The persisted AIMD window is retained for old state compatibility and
        # telemetry, but must not silently under-fill a healthy machine.
        # ponytail: use one hardware limit; add per-provider quotas only when
        # an external API exposes a real capacity signal.
        effective_limit = float(configured_max)

        # 1. 主机系统负载调节
        try:
            pressure, memory_pressure = self._get_system_load()
        except Exception:
            pressure = self.get_system_pressure()
            memory_pressure = None

        if pressure > self.target_cpu_load or (
            memory_pressure is not None and memory_pressure > 85.0
        ):
            reduction = (pressure - self.target_cpu_load) * 4.0
            if memory_pressure is not None and memory_pressure > 85.0:
                reduction = max(reduction, effective_limit * 0.2)
            effective_limit = max(float(c_min), effective_limit - reduction)
            self.concurrency_window = effective_limit

        # DOC-137 2.0 / D137-06: 漏洞产出速率 (Yield Velocity) 与高回报动态提速
        if actual_state is not None and getattr(actual_state, "recent_high_yield", False) is True:
            if pressure <= self.target_cpu_load and (memory_pressure is None or memory_pressure <= 75.0):
                effective_limit = min(float(c_max), effective_limit * 1.5)

        self.concurrency_window = effective_limit
        if actual_state is not None and hasattr(actual_state, "adaptive_concurrency_window"):
            actual_state.adaptive_concurrency_window = self.concurrency_window

        return max(0, int(effective_limit) - current_active)

    def record_outcome(self, state: Any, outcome: str) -> None:
        """记录 Worker 结果；结果不再自动降低健康机器的并发。"""
        if not getattr(getattr(state, "budget", None), "adaptive_concurrency", True):
            return
        budget = state.budget
        if not getattr(budget, "require_parallel", True) or getattr(budget, "max_concurrency", 1) <= 1:
            return
        c_min = max(1, int(getattr(budget, "min_concurrency", self.min_concurrency)))
        c_max = int(getattr(budget, "max_concurrency_ceiling", self.max_concurrency))
        current = getattr(state, "adaptive_concurrency_window", None)
        if not isinstance(current, (int, float)) or isinstance(current, bool):
            current = float(getattr(budget, "max_concurrency", c_min))
        current = max(float(c_min), min(float(c_max), float(current)))
        if outcome in {"expired", "preempted", "failed"}:
            # Keep the window as telemetry only. Capacity is reduced only by
            # the live CPU/RAM pressure check in compute_capacity().
            state.adaptive_success_streak = 0
            state.adaptive_failure_streak = min(
                int(getattr(state, "adaptive_failure_streak", 0)) + 1, 1000,
            )
            return
        if outcome != "completed":
            return
        state.adaptive_failure_streak = 0
        streak = int(getattr(state, "adaptive_success_streak", 0)) + 1
        state.adaptive_concurrency_window = current
        state.adaptive_success_streak = streak


class ComplexityLeaseCalculator:
    """复杂度感知动态租约计算器。"""

    @staticmethod
    def calculate_lease(
        unit_ids: Sequence[str],
        root: Path | None = None,
        defect_modules: set[str] | None = None,
    ) -> float:
        """根据单元数量、代码行数与业务类型动态计算推荐租约 TTL。

        AI-agent worker 完成一个单元需要 10~25 分钟（全文读码 + 动态复现
        + 证据落盘 + schema 校验），因此下限为 1800 秒；每个追加单元
        +600 秒；上限 ``LEASE_SECONDS_CEILING``（3600 秒）对齐常用
        max_round_wall_seconds 与 Supervisor 的租约接受上限。
        """
        if not unit_ids:
            return 1800.0

        # 单单元基础 1800s，多单元聚合每个追加 600s
        base_lease = 1800.0 + max(0, len(unit_ids) - 1) * 600.0

        # 检查是否包含业务链路单元
        if any(unit_kind.is_business(u) for u in unit_ids):
            base_lease = max(base_lease, 2400.0)

        # 检查是否命中历史错题/高危缺陷
        if defect_modules:
            for u in unit_ids:
                fp, _ = extract_unit_scope(u)
                if fp in defect_modules or any(m in fp for m in defect_modules):
                    base_lease += 600.0
                    break

        # 检查源码文件行数复杂度
        if root:
            for u in unit_ids:
                fp, _ = extract_unit_scope(u)
                target = (root / fp).resolve()
                if target.is_file():
                    try:
                        lines = len(target.read_text(encoding="utf-8", errors="ignore").splitlines())
                        if lines > 500:
                            base_lease += 600.0
                        elif lines > 200:
                            base_lease += 300.0
                    except OSError:
                        pass

        return max(1800.0, min(base_lease, LEASE_SECONDS_CEILING))

    estimate_lease = calculate_lease


class ZombieDetector:
    """僵死 Worker 探活与秒级抢占探针。"""

    @staticmethod
    def is_pid_alive(pid: int | None) -> bool:
        """检测指定 PID 本地进程是否仍在活跃运行。"""
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError as exc:
            return exc.errno == errno.EPERM

    @staticmethod
    def pid_command_line(pid: int) -> str | None:
        """读取进程命令行；无法读取时返回 None。

        ``None`` 必须被调用方当作「无法证明归属」处理，绝不能当作「可以终止」。
        """
        if pid <= 0:
            return None
        if os.name == "posix":
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                raw = b""
            if raw:
                return raw.replace(b"\x00", b" ").decode("utf-8", "replace")
            try:
                result = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    capture_output=True, text=True, check=False, timeout=2,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        return None

    @staticmethod
    def is_worker_process(pid: int) -> bool:
        """判断 ``pid`` 的命令行是否明确指向 BugX Worker。

        ``process_pid`` 完全由 Worker 通过 MCP 参数自报（只校验 ``>= 1``）。
        若仅凭该 PID 发信号，Worker 就能让宿主终止任意进程——包括 PID 复用
        导致的无关进程，以及 ``os.killpg`` 波及的整组进程。因此终止前必须
        独立取得「这确实是一个 BugX Worker」的正向证据。
        """
        cmdline = ZombieDetector.pid_command_line(pid)
        if not cmdline:
            return False
        lowered = cmdline.casefold()
        return any(marker in lowered for marker in WORKER_CMDLINE_MARKERS)

    @staticmethod
    def _parent_pid(pid: int) -> int | None:
        """读取 ``pid`` 的父进程号；无法读取时返回 None。"""
        if pid <= 0 or os.name != "posix":
            return None
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        if raw:
            # ``comm`` 可能含空格/括号，取最后一个 ')' 之后再切分：
            # 之后依次是 state、ppid。
            tail = raw.rsplit(")", 1)[-1].split()
            if len(tail) >= 2:
                try:
                    return int(tail[1])
                except ValueError:
                    return None
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, check=False, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0 and result.stdout.strip():
            try:
                return int(result.stdout.split()[0])
            except (ValueError, IndexError):
                return None
        return None

    @staticmethod
    def is_ancestor_of_self(pid: int | None) -> bool:
        """判断 ``pid`` 是否是当前进程的祖先（父 / 祖父 / ...）。

        祖先链上是宿主自身、用户 shell、``launchd``，以及拉起宿主的
        ``opencode --agent bugx`` 主会话——后者的命令行恰好含有 BugX 标志，
        仅凭命令行证据无法排除。Worker 由宿主派生，绝不可能是宿主的祖先，
        因此命中即拒绝终止，杜绝「误伤用户主会话/终端」。
        """
        if pid is None or pid <= 0:
            return False
        current = os.getpid()
        seen: set[int] = set()
        while current > 1 and current not in seen:
            seen.add(current)
            parent = ZombieDetector._parent_pid(current)
            if parent is None:
                return False
            if parent == pid:
                return True
            current = parent
        return False

    @staticmethod
    def owns_process_group(pid: int) -> bool:
        """判断 ``pid`` 是否是「本 BugX 会话自行创建」的进程组组长。

        BugX 一律用 ``start_new_session=True`` 拉起 Worker
        （``core/worker_execution.py``），因此真实 Worker 必然是自己新建会话的
        组长：``getsid(pid) == getpgid(pid) == pid``。用户终端 / ``launchd``
        子会话即使恰好是组长，也不是本会话创建的，返回 False。只有为 True 时
        才允许 ``os.killpg``，否则退化为对单个 PID 发信号，避免一次终止波及
        整组无关进程。
        """
        try:
            return os.getpgid(pid) == pid and os.getsid(pid) == pid
        except OSError:
            return False

    @staticmethod
    def _signal_target(pid: int):
        """返回本次终止使用的信号投递函数。

        仅当目标确为「本会话创建的进程组组长」时才用 ``os.killpg``；否则退回
        单 PID 的 ``os.kill``。无法证明归属时一律不整组投递。
        """
        if ZombieDetector.owns_process_group(pid):
            return lambda sig: os.killpg(pid, sig)
        return lambda sig: os.kill(pid, sig)

    @staticmethod
    def terminate_claim(claim: Any, workspace_root: Path | str | None = None) -> tuple[bool, str]:
        """按 claim 记录的归属信息终止其 Worker，返回 ``(是否已终止, 原因)``。

        ``claim`` 同时携带 ``process_pid`` 与 ``process_start_token``，把归属
        决策收敛到单点，调用方不再各自复制守卫。原因为空串表示成功；否则是
        拒绝/失败原因码，供调用方写入审计事件，使「拒绝」不再静默。
        终止成功后自动触发 OrphanSandboxReaper 物理收割其临时沙箱 (DOC-242)。
        """
        terminated, refusal = ZombieDetector._terminate_pid_verdict(
            getattr(claim, "process_pid", None),
            expected_start_token=getattr(claim, "process_start_token", None),
        )
        if terminated:
            worker_id = getattr(claim, "worker_id", None)
            OrphanSandboxReaper.reap_worker_sandbox(workspace_root, worker_id)
        return terminated, refusal

    @staticmethod
    def is_pid_cpu_active(pid: int | None) -> bool:
        """检查本地进程是否有非零 CPU 时间或活跃运行 (DOC-242)。"""
        if pid is None or pid <= 0:
            return False
        if not ZombieDetector.is_pid_alive(pid):
            return False
        if os.name == "posix":
            stat_path = Path(f"/proc/{pid}/stat")
            if stat_path.is_file():
                try:
                    parts = stat_path.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()
                    if len(parts) >= 13:
                        utime = int(parts[11])
                        stime = int(parts[12])
                        return (utime + stime) > 0
                except Exception:
                    pass
            try:
                result = subprocess.run(
                    ["ps", "-o", "cputime=", "-p", str(pid)],
                    capture_output=True, text=True, check=False, timeout=2,
                )
                if result.returncode == 0 and result.stdout.strip():
                    val = result.stdout.strip()
                    return val not in ("0:00.00", "00:00.00", "0:00", "")
            except Exception:
                pass
        return True

    @staticmethod
    def _terminate_pid_verdict(
        pid: int | None, *, expected_start_token: str | None,
    ) -> tuple[bool, str]:
        """终止决策的单点实现，返回 ``(是否已终止, 原因)``。

        fail-closed 的五道校验，任一不成立即拒绝发信号：
        1. PID 合法、不是本进程、也不是本进程的祖先进程；
        2. 命令行独立证明这确实是 BugX Worker（``is_worker_process``）；
        3. claim 记录过启动令牌——未记录即无法证明 PID 未被复用，拒绝；
        4. 当前 PID 的启动令牌与记录一致；读不到令牌同样拒绝。
        整组投递（``killpg``）另有 :meth:`owns_process_group` 把关。
        """
        if pid is None or pid <= 0:
            return False, "refused_invalid_pid"
        if pid == os.getpid():
            return False, "refused_self_pid"
        if ZombieDetector.is_ancestor_of_self(pid):
            return False, "refused_ancestor_process"
        if not ZombieDetector.is_pid_alive(pid):
            return True, ""
        if not ZombieDetector.is_worker_process(pid):
            return False, "refused_no_worker_evidence"
        if expected_start_token is None:
            return False, "refused_missing_start_token"
        try:
            from . import supervisor_models
            live_token = supervisor_models.process_start_token(pid)
        except Exception:
            return False, "refused_start_token_unavailable"
        if live_token is None:
            return False, "refused_start_token_unavailable"
        if live_token != expected_start_token:
            return False, "refused_start_token_mismatch"
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, check=False, timeout=5,
                )
                terminated = result.returncode == 0 or not ZombieDetector.is_pid_alive(pid)
                return terminated, ("" if terminated else "signal_failed")

            signal_target = ZombieDetector._signal_target(pid)
            try:
                signal_target(signal.SIGTERM)
            except OSError:
                os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 0.5
            while ZombieDetector.is_pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            if ZombieDetector.is_pid_alive(pid):
                try:
                    signal_target(signal.SIGKILL)
                except OSError:
                    os.kill(pid, signal.SIGKILL)
            terminated = not ZombieDetector.is_pid_alive(pid)
            return terminated, ("" if terminated else "signal_failed")
        except (OSError, subprocess.SubprocessError):
            terminated = not ZombieDetector.is_pid_alive(pid)
            return terminated, ("" if terminated else "signal_failed")

    @staticmethod
    def terminate_pid(pid: int | None, *, expected_start_token: str | None = None) -> bool:
        """终止卡死的本地 Worker，成功返回时进程已不再存活。

        兼容入口，保留既有签名；归属判定走 :meth:`_terminate_pid_verdict`
        单点实现。``expected_start_token`` 缺失即拒绝：无法证明 PID 未被复用
        时宁可不终止，租约照常过期，调度不受影响。
        """
        terminated, _reason = ZombieDetector._terminate_pid_verdict(
            pid, expected_start_token=expected_start_token,
        )
        return terminated

    @staticmethod
    def check_zombie_claim(
        claim: Any,
        now: float,
        heartbeat_timeout_window: float = 450.0,
        now_mono: float | None = None,
    ) -> tuple[bool, str]:
        """检查指定 Claim 是否已处于僵死状态需要立即抢占 (DOC-242: 支持单调时钟)。"""
        # 1. 进程存活性检查
        pid = getattr(claim, "process_pid", None)
        if pid is not None and not ZombieDetector.is_pid_alive(pid):
            return True, f"process_{pid}_dead"

        # 2. 连续心跳断流检查 (两个时钟任一判定断流/到期即生效，防模拟时钟与单调时钟脱敏)
        execution_state = getattr(claim, "execution_state", "")
        last_hb = getattr(claim, "last_heartbeat_at", None)
        last_hb_mono = getattr(claim, "last_heartbeat_mono", None)
        if execution_state == "running" and last_hb is not None:
            silence_wall = now - last_hb
            silence_mono = (now_mono - last_hb_mono) if (now_mono is not None and last_hb_mono is not None) else silence_wall
            silence = max(silence_wall, silence_mono)
            if silence > heartbeat_timeout_window:
                # C1 宽限：租约是 worker 对自身执行时长的申报。进程存活且
                # 租约未到期时，心跳断流只说明"该保活了"，不构成僵死证据——
                # 否则无人值守 worker（模型+差分+补丁合成常超 450s）会被同批
                # 并发线程的 refresh 误判抢占，整批静默失败。仅当断流超过
                # 租约剩余量（连申报的时长都守不住）才判僵尸。
                # A7（Wave 1 交接 §4-1）：租约过期只看**单调时钟**。
                # 旧实现 ``wall_expired or mono_expired`` 把墙钟 OR 进来，墙钟前跳
                # 会错杀仍存活的 worker（长任务常超 450s）。
                # 缺 mono 锚点 ⇒ fail-closed 视为过期：不得靠墙钟猜（旧快照已由
                # ``from_dict`` 拒绝，此处为第二道防线）。
                lease_expires_at = getattr(claim, "lease_expires_at", None)
                lease_expires_mono = getattr(claim, "lease_expires_mono", None)
                if lease_expires_mono is None or now_mono is None:
                    return True, "heartbeat_timeout_missing_mono_anchor"
                if now_mono >= lease_expires_mono:
                    return True, "heartbeat_timeout"

                # 剩余租约的**夹紧**仍保留墙钟兜底：silence 计算本身还混用墙钟
                # （Wave 1 交接 §4-3 的 grace 窗/Wave 3 统一口径）。这里只把
                # 「是否过期」的判定收紧为 mono，不提前改动 silence 语义。
                rem_wall = max(0.0, (lease_expires_at or now) - now)
                rem_mono = max(0.0, lease_expires_mono - now_mono)
                remaining_lease = min(rem_wall, rem_mono)

                # 活性证据自适应（卡死木桶修复）：``process_pid`` 是「进程还活着」
                # 的独立证据；没有它时（宿主 Task 子代理、CLI 会话等）**心跳就是
                # 唯一活性证据**，此时不能用「断流超过租约剩余量」这把宽限尺子——
                # 那等于要求等完整个租约。实测：worker 心跳停滞 age=113s 而租约仍剩
                # 2290s，日志刷了 38 分钟「无法走孤儿快通道回收」，单元被白占。
                # 因此：无进程证据 → 断流超窗口即回收；有进程证据才保留 C1 宽限。
                # judge collect/settle 的宿主簿记 claim（provider="judge-*"）
                # 全程无独立进程、设计上不心跳：collect→判读→settle 单轮可达
                # 分钟级，心跳断流不代表死亡。这类 claim 的存活契约是租约本身
                # （collect 建约时 lease_seconds=1800），断流超窗不再构成僵尸
                # 证据，交给租约到期兜底——否则长判读波次的 claim 必被误杀。
                provider = str(getattr(claim, "provider", "") or "")
                if (
                    pid is None
                    and silence > heartbeat_timeout_window
                    and not provider.startswith("judge-")
                ):
                    return True, "heartbeat_timeout_no_process_evidence"
                if silence > remaining_lease + heartbeat_timeout_window:
                    return True, "heartbeat_timeout_beyond_lease"

        return False, ""

    is_zombie = check_zombie_claim


class OrphanSandboxReaper:
    """异常终止 Worker 的沙箱与临时锁物理收割器 (DOC-242)。"""

    @staticmethod
    def reap_worker_sandbox(workspace_root: Path | str | None, worker_id: str | None) -> bool:
        """物理清理指定 Worker 的临时沙箱目录与挂载点。"""
        if not worker_id or not isinstance(worker_id, str):
            return False
        clean_id = worker_id.strip()
        # 严格校验 worker_id 字符集，杜绝路径穿透
        if not re.match(r"^[a-zA-Z0-9_\-]+$", clean_id):
            return False
        if not workspace_root:
            return False
        try:
            root = Path(workspace_root).resolve()
            targets = [
                root / ".bugx" / "sandboxes" / clean_id,
                root / ".bugx" / "sandboxes" / f"w-{clean_id}",
                root / ".bugx" / "state" / "sandboxes" / clean_id,
            ]
            reaped = False
            for target in targets:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                    reaped = True
            return reaped
        except Exception:
            return False


MIN_ROUND_WALL_SECONDS = 180.0
MAX_ROUND_WALL_SECONDS = 7200.0
DEFAULT_ROUND_WALL_SECONDS = 900.0


class RoundBudgetCalculator:
    """自适应轮次预算计算器 (DOC-150)。

    根据当前 Round 内规划派发的单元集合、代码行数与 AST 复杂度、
    调用链依赖深度以及历史错题/高危模块密度，动态推算科学合理的轮次预算墙钟。
    避免对全轻量级任务赋予死板的 900s 冗余，也避免对重度复杂链路过早硬超时。
    """

    @staticmethod
    def calculate_round_budget(
        unit_ids: Sequence[str],
        *,
        root: Path | None = None,
        concurrency: int = 1,
        defect_modules: set[str] | None = None,
        base_seconds: float = DEFAULT_ROUND_WALL_SECONDS,
    ) -> float:
        if not unit_ids:
            return base_seconds

        concurrency = max(1, int(concurrency))
        # 1. 基础时间：按并发分摊基础工作量（单单元基准估算 450s 勘察与验证）
        parallel_batches = math.ceil(len(unit_ids) / concurrency)
        calculated = max(MIN_ROUND_WALL_SECONDS, parallel_batches * 450.0)

        # 2. 业务链路与深层调用加权
        biz_units = [u for u in unit_ids if unit_kind.is_business(u)]
        if biz_units:
            calculated += len(biz_units) * 150.0

        # 3. 历史缺陷与错题模块排雷加权
        if defect_modules:
            matched_defects = 0
            for u in unit_ids:
                fp, _ = extract_unit_scope(u)
                if fp in defect_modules or any(m in fp for m in defect_modules):
                    matched_defects += 1
            if matched_defects > 0:
                calculated += min(1200.0, matched_defects * 200.0)

        # 4. 代码规模与复杂度加权
        if root:
            total_lines = 0
            for u in unit_ids:
                fp, _ = extract_unit_scope(u)
                target = (root / fp).resolve()
                if target.is_file():
                    try:
                        total_lines += len(target.read_text(encoding="utf-8", errors="ignore").splitlines())
                    except OSError:
                        pass
            if total_lines > 2000:
                calculated += 600.0
            elif total_lines > 800:
                calculated += 300.0

        return max(MIN_ROUND_WALL_SECONDS, min(calculated, MAX_ROUND_WALL_SECONDS))
