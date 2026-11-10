#!/usr/bin/env python3
"""Supervisor 数据模型、异常体系与状态契约定义 (DOC-036)。"""

from __future__ import annotations
from typing import Any

import copy
from core.planning import unit_kind
import hashlib
import json
import math
import os
import re
import core.secure_atomic as secure_atomic
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION = 2
DEFAULT_STATE_RELATIVE_PATH = Path(".bugx") / "state" / "supervisor.json"
LEGACY_STATE_RELATIVE_PATH = Path(".audit_supervisor") / "run-state.json"
FINDING_SEVERITY_ALIASES = {
    "p0": "critical",
    "p1": "high",
    "p2": "medium",
    "p3": "low",
}


def normalize_finding_severity(value: object) -> object:
    """Normalize common priority aliases to the confirmation schema levels."""
    if not isinstance(value, str):
        return value
    normalized = value.strip().lower()
    return FINDING_SEVERITY_ALIASES.get(normalized, normalized)


def finding_severity_levels() -> tuple[str, ...]:
    """finding 严重度词表的**唯一访问器**（BX-ENGINE-18 / DOC-230 §3.8 第 1 项）。

    真源是 ``agent_eval.FINDING_SEVERITY_LEVELS``（由 ``SEVERITY_ORDER`` 派生）。
    写侧归一化、MCP schema 的 ``enum``、修复通道的白名单**都从这里取**，避免
    再次出现「schema 无 enum / 门禁有词表」的三源漂移。导入不可用时退回同一
    集合的内联快照，保证引擎目录不完整时仍自洽（守卫测试比对两者一致）。
    """
    try:
        from core.agent import agent_eval

        return tuple(agent_eval.FINDING_SEVERITY_LEVELS)
    except (ImportError, AttributeError):  # pragma: no cover - 引擎目录不完整时
        return ("info", "low", "medium", "high", "critical")


COVERAGE_PENDING = "pending"
COVERAGE_COVERED = "covered"
COVERAGE_BLOCKED = "blocked"
COVERAGE_UNVERIFIED = "unverified"
COVERAGE_PARTIAL = "partial"
COVERAGE_NOT_APPLICABLE = "not_applicable"
COVERAGE_STALE = "stale"
COVERAGE_REMOVED = "removed"
COVERAGE_SUPERSEDED = "superseded"
COVERAGE_CORRUPTED = "corrupted"
VALID_COVERAGE_STATES = {
    COVERAGE_PENDING,
    COVERAGE_COVERED,
    COVERAGE_BLOCKED,
    COVERAGE_UNVERIFIED,
    COVERAGE_PARTIAL,
    COVERAGE_NOT_APPLICABLE,
    COVERAGE_STALE,
    COVERAGE_REMOVED,
    COVERAGE_SUPERSEDED,
    COVERAGE_CORRUPTED,
}
# Evidence status is separate from coverage status: ``covered`` describes the
# unit, while ``passed`` describes the verification evidence.
VALID_EVIDENCE_STATES = {"passed", "failed", "error", "skipped", "blocked"}
# 覆盖债务分类：单元级债务（可重试/需资源/需证据） vs 全局阻塞
UNIT_LEVEL_DEBT = {COVERAGE_PENDING, COVERAGE_PARTIAL, COVERAGE_UNVERIFIED, COVERAGE_STALE, COVERAGE_BLOCKED}
GLOBAL_BLOCKED = {COVERAGE_CORRUPTED}
TERMINAL_COVERED = {COVERAGE_COVERED, COVERAGE_NOT_APPLICABLE, COVERAGE_REMOVED, COVERAGE_SUPERSEDED}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# checkpoint 单次最多携带 10 个错误签名；状态里滚动保留最近 20 条。
MAX_SIGNATURES_PER_CHECKPOINT = 10
MAX_RECENT_SIGNATURES = 20
# Keep state reads bounded even when a long-running supervisor emits an event
# for every heartbeat/expiry.  The hash-chain anchor below preserves audit
# continuity after old events are compacted.
MAX_HISTORY_EVENTS = 1000
MAX_SIGNATURE_LENGTH = 512
# unit-notes：每个覆盖单元保留最新一条手记，总量封顶防膨胀。
MAX_UNIT_NOTES = 200
MAX_UNIT_NOTE_LENGTH = 2000

GRAPH_GOALS = {"find_bugs", "repair_preview"}
GRAPH_STAGES = {"recon", "repro", "review", "patch", "check", "report"}
GRAPH_TASK_STATES = {
    "queued", "reserved", "running", "artifact_ready", "submitted", "completed",
}
GRAPH_ASSESSMENTS = {"candidate", "confirmed", "rejected", "needs_context", "static_supported"}
GRAPH_EVIDENCE_LEVELS = {"source_review", "reproduced", "regression_verified"}
GRAPH_REMEDIATIONS = {
    "not_requested", "proposed", "fix_verified", "fix_failed", "regression_incomplete",
}


def unit_source_path(unit: object) -> str:
    """Extract only the source-file portion of a coverage unit identifier.

    Coverage IDs may carry a kind prefix, an optional line number, and a
    symbol (for example ``内部:12:core/foo.py::handler``).  A symbol or a
    finding ID is not a filesystem path and must never be manufactured as
    evidence.

    这里（``supervisor_models``）是唯一实现：claim 期的同文件互斥键派生与
    checkpoint 期的证据路径回填都必须用同一套解析，否则两处会各自漂移。
    """
    text = str(unit or "").strip()
    if not text:
        return ""
    if "::" in text:
        text = text.split("::", 1)[0]
    parts = text.split(":", 2)
    if len(parts) == 3 and parts[1].strip().isdigit():
        text = parts[2]
    elif len(parts) >= 2 and parts[0].strip() in {"内部", "业务"}:
        text = parts[1]
    return text.strip()


def _graph_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PersistenceError(f"{name} 必须是数组")
    if any(not isinstance(item, str) for item in value):
        raise PersistenceError(f"{name} 必须只包含字符串")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result) or len(set(result)) != len(result):
        raise PersistenceError(f"{name} 不能为空或重复")
    return result


def _graph_digest(value: object, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PersistenceError(f"{name} 必须是 SHA-256")
    return value.lower()


from core.supervisor_types.exceptions import (
    SupervisorError,
    NoRunError,
    RunAlreadyStarted,
    InvalidTransition,
    BudgetExhausted,
    ClaimRejected,
    LeaseExpired,
    RevisionConflict,
    InvalidCheckpoint,
    PersistenceError,
    WorkspacePathError,
    ConcurrencyNotSatisfied,
)
from core.supervisor_types.path_containment import (
    confined_path,
    has_lexical_symlink,
    confined_regular_file,
    worker_artifact_root,
    confined_worker_artifact,
    atomic_write_bytes,
)
import core.supervisor_types.target_fingerprint as _st_tf
import core.supervisor_types.graph_models as _st_gm

_boot_time_token = _st_tf._boot_time_token
_process_start_timestamp = _st_tf._process_start_timestamp
_manifest_units_blob = _st_tf._manifest_units_blob
_git_ref_token = _st_tf._git_ref_token
_git_head_cached = _st_tf._git_head_cached

_graph_strings = _st_gm._graph_strings
_graph_digest = _st_gm._graph_digest
from core.supervisor_types.target_fingerprint import (
    MAX_JSON_DEPTH,
    MAX_JSON_KEYS,
    MAX_PROCESS_START_TOKEN_LENGTH,
    canonical_json,
    process_start_token,
    default_coverage_manifest,
    invalidate_git_head_cache,
    target_identity,
    workspace_fingerprint,
    fingerprint_for_units,
    git_target_fingerprint,
    finite_number,
    check_depth_and_keys,
)
from core.supervisor_types.graph_models import (
    GRAPH_GOALS,
    GRAPH_STAGES,
    GRAPH_TASK_STATES,
    GRAPH_ASSESSMENTS,
    GRAPH_EVIDENCE_LEVELS,
    GRAPH_REMEDIATIONS,
    GraphTask,
    AgentBinding,
    BugRecord,
    GraphRun,
)
from core.supervisor_types.worker_claim import (
    WorkerClaim,
)
def note_requests_deep_verification(note: object) -> bool:
    """Recognize the explicit triage-upgrade sentinel, never prose mentions."""
    return isinstance(note, str) and any(
        line.strip().casefold() == "upgrade_tier=deep"
        for line in note.splitlines()
    )


# ── Worker checkpoint 容量与字段长度上限（MCP 与引擎一致，fail-closed） ──
MAX_FINDINGS_PER_CHECKPOINT = 50
MAX_COVERAGE_PER_CHECKPOINT = 5  # DOC-044 支持亲和任务包，单个 Worker 每次最多 5 个覆盖单元
MAX_EVIDENCE_PER_CHECKPOINT = 5
MAX_UNITS_PER_CLAIM = 5
MAX_COMMAND_LENGTH = 8192
MAX_ARTIFACT_PATH_LENGTH = 1024
MAX_FINDING_FINGERPRINT_LENGTH = 256
MAX_FINDING_FIELD_LENGTH = 4096
MAX_JSON_DEPTH = 32
MAX_JSON_KEYS = 5000
# checkpoint artifact 先 stat 后流式哈希：超过 8MB 直接拒绝，防止 Worker
# 用超大 artifact 把 Supervisor 内存打爆（P2-2）。
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
ARTIFACT_HASH_CHUNK_BYTES = 8 * 1024
# worker lease 上限 1 小时（LOG-06）。此处必须与两处声明逐字一致：
#   * ``core/bugx_mcp_server.py`` 的 ``lease_seconds.inputSchema.maximum``；
#   * ``core/task_scheduler.py`` 的租约时长推算上限。
# 历史漂移：本常量曾是 14400（4 小时）而注释写「1 小时」，schema 与推算器
# 都是 3600；绕过 inputSchema 校验的调用方因此能拿到 4 倍于防护目标的租约，
# 长期占用并发额度并与 max_round_wall_seconds 的轮次预算脱节。
MAX_LEASE_SECONDS = 3600
# 租约总时长乘数：单 claim 的寿命上限 = 初始租约 × (DEFAULT_MAX_RENEWALS + 1)。
# 它**不再**表示「续租次数上限」——续租次数只做审计计数，寿命只由
# ``total_lease_deadline`` 决定，故寿命与心跳节奏无关。
DEFAULT_MAX_RENEWALS = 10
MAX_WORKER_ATTEMPTS = 3
# DOC-205 S2: 全 Run 跨代际累计尝试上限，防止多轮次死循环永远重试无望单元
MAX_RUN_WORKER_ATTEMPTS = 6
# DOC-203 D3：worker 单次长调用声明的上限（秒）。inputSchema.maximum
# （core/bugx_mcp_server.py）与 lease_manager 的推进公式必须共用本常量，
# 避免重现 MAX_LEASE_SECONDS 的历史漂移（见上方注释）。
MAX_LONG_CALL_SECONDS = 1800
# DOC-204 L1：同 (operation, gate) 组合每 run 的 lifecycle_blocked 上限。
MAX_LIFECYCLE_BLOCKED_PER_GATE = 50
# DOC-203 D3：每个 claim 允许的长调用声明次数上限；总占用仍受
# total_lease_deadline 与 run 级 max_wall_seconds 双重封顶。
MAX_LONG_CALLS_PER_CLAIM = 5
# A hostname may legally be longer than the historical 256-byte token limit.
# Keep the persisted fingerprint bounded while leaving room for all four
# identity fields.
MAX_PROCESS_START_TOKEN_LENGTH = 1024


def worker_attempts_exhausted(state: object, unit_id: str) -> bool:
    """Return whether a unit has used all retryable worker attempts.

    Attempts belong to the current scheduler epoch.  A target rebind or
    explicit state recovery fences the old claims, so their retry history
    must not consume the new target generation's budget.  The newest claim
    for the unit is authoritative: an active attempt is still allowed to
    finish, ``superseded`` is only a historical marker, and a submitted
    attempt exhausts the unit only when the unit is still non-terminal.
    Bundle claims are evaluated per unit through the shared coverage map.

    DOC-205 S2: 若跨代际累计已尝试达 MAX_RUN_WORKER_ATTEMPTS 且未终态，
    作为全 Run 级硬顶判定熔断，防止跨轮死循环。
    """
    coverage = getattr(state, "coverage", {}) or {}
    if coverage.get(unit_id) in TERMINAL_COVERED:
        return False

    claims = getattr(state, "claims", {}) or {}
    # 全 Run 累计已终结尝试计数（不包含仍在 active 的进行中尝试与 superseded 标记）
    total_non_active_attempts = sum(
        1
        for claim in claims.values()
        if unit_id in getattr(claim, "unit_ids", ())
        and getattr(claim, "state", "") in {"expired", "released", "submitted"}
        and not getattr(claim, "fenced_from_attempts", False)
    )
    if total_non_active_attempts >= MAX_RUN_WORKER_ATTEMPTS:
        return True

    current_epoch = getattr(state, "scheduler_epoch", None)
    latest_claim = None
    latest_key: tuple[int, float, str] | None = None
    for claim in claims.values():
        if unit_id not in getattr(claim, "unit_ids", ()):
            continue
        claim_epoch = getattr(claim, "scheduler_epoch", current_epoch)
        if current_epoch is not None and claim_epoch != current_epoch:
            continue
        attempt = int(getattr(claim, "attempt", 0))
        claimed_at = getattr(claim, "claimed_at", 0.0)
        if not isinstance(claimed_at, (int, float)):
            claimed_at = 0.0
        key = (attempt, float(claimed_at), str(getattr(claim, "worker_id", "")))
        if latest_key is None or key > latest_key:
            latest_key = key
            latest_claim = claim

    if latest_claim is None or latest_key is None:
        return False
    if latest_key[0] < MAX_WORKER_ATTEMPTS:
        return False
    claim_state = getattr(latest_claim, "state", "")
    if claim_state in {"expired", "released"}:
        return True
    if claim_state == "submitted":
        return coverage.get(unit_id) not in TERMINAL_COVERED
    # In particular, an active third attempt is allowed to finish and a
    # superseded claim does not independently consume the current generation.
    return False


class Status(str, Enum):
    """Supervisor 对外暴露的明确状态。"""

    RUNNING = "RUNNING"
    CHECKPOINT = "CHECKPOINT"
    NEXT_ROUND = "NEXT_ROUND"
    STALLED = "STALLED"
    INCOMPLETE = "INCOMPLETE"
    COMPLETE = "COMPLETE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FAILED = "FAILED"




def resolve_system_concurrency() -> tuple[int, int]:
    """根据系统硬件（CPU 核心数）、系统配置与环境变量解析 (max_concurrency, max_concurrency_ceiling)。"""
    # 1. 环境变量显式覆盖
    env_max = os.environ.get("BUGX_MAX_CONCURRENCY") or os.environ.get("BUGX_WORKERS_MAX")
    env_ceiling = os.environ.get("BUGX_MAX_CONCURRENCY_CEILING")

    # 2. 系统配置文件覆盖（若存在 ~/.bugx/config.json 或 cloud-config.json）
    cfg_max: int | None = None
    cfg_ceiling: int | None = None
    try:
        cfg_paths = [
            Path.home() / ".bugx" / "config.json",
            Path.home() / ".bugx" / "cloud-config.json",
        ]
        for cfg_p in cfg_paths:
            if cfg_p.is_file():
                cfg_data = json.loads(cfg_p.read_text(encoding="utf-8"))
                if isinstance(cfg_data, dict):
                    if "max_concurrency" in cfg_data and isinstance(cfg_data["max_concurrency"], (int, float)):
                        cfg_max = int(cfg_data["max_concurrency"])
                    elif "max_workers" in cfg_data and isinstance(cfg_data["max_workers"], (int, float)):
                        cfg_max = int(cfg_data["max_workers"])
                    if "max_concurrency_ceiling" in cfg_data and isinstance(cfg_data["max_concurrency_ceiling"], (int, float)):
                        cfg_ceiling = int(cfg_data["max_concurrency_ceiling"])
                    if cfg_max is not None:
                        break
    except Exception:
        pass

    # 3. 硬件拓扑自适应决策
    cores = os.cpu_count() or 4
    if cores <= 2:
        base_concurrency = 2
        base_ceiling = 4
    elif cores <= 4:
        base_concurrency = cores
        base_ceiling = 8
    elif cores <= 8:
        base_concurrency = cores
        base_ceiling = max(12, cores * 2)
    else:
        # 逻辑核数就是默认可承载上限；不再把大机器截断到 16/32。
        base_concurrency = cores
        base_ceiling = cores * 2

    final_concurrency = cfg_max if (cfg_max is not None and cfg_max >= 1) else base_concurrency
    final_ceiling = cfg_ceiling if (cfg_ceiling is not None and cfg_ceiling >= final_concurrency) else base_ceiling

    if env_max and env_max.strip().isdigit() and int(env_max.strip()) >= 1:
        final_concurrency = int(env_max.strip())
        final_ceiling = max(final_ceiling, final_concurrency)
    if env_ceiling and env_ceiling.strip().isdigit() and int(env_ceiling.strip()) >= final_concurrency:
        final_ceiling = int(env_ceiling.strip())

    return final_concurrency, max(final_concurrency, final_ceiling)


def _default_max_concurrency() -> int:
    return resolve_system_concurrency()[0]


def _default_max_concurrency_ceiling() -> int:
    return resolve_system_concurrency()[1]


@dataclass(frozen=True)
class RunBudget:
    """独立于 life 的 run 预算。

    ``max_rounds`` 是 run 内的绝对轮次上限；``max_wall_seconds`` 是整个
    run 的不可续期硬上限；``max_round_wall_seconds``、``max_tool_calls``
    和 ``max_cost_units`` 按 budget epoch 计算。reset_budget 会保留审计
    历史并开启新 epoch，但不能扩大或重启全局墙钟。
    """

    max_rounds: int = 25
    max_wall_seconds: float = 14400.0
    max_round_wall_seconds: float = 1800.0
    max_tool_calls: int = 1000
    max_cost_units: float = 10000.0
    max_concurrency: int = field(default_factory=_default_max_concurrency)
    require_parallel: bool = True
    serial_reason: str | None = None
    # Generic callers may opt in; BugX's host integrations enable this by
    # default so an unverified finding cannot close an audit run.
    require_confirmation: bool = False
    adaptive_concurrency: bool = True
    min_concurrency: int = 2
    max_concurrency_ceiling: int = field(default_factory=_default_max_concurrency_ceiling)

    @classmethod
    def calculate_adaptive_round_budget(
        cls,
        pending_units_count: int,
        concurrency: int = 4,
        unit_baseline_seconds: float = 60.0,
    ) -> float:
        """根据待测单元与并发度自适应推算单轮墙钟预算 (DOC-196)。"""
        batches = max(1, math.ceil(max(1, pending_units_count) / max(1, concurrency)))
        raw = batches * unit_baseline_seconds
        return float(max(600.0, min(7200.0, raw)))

    def __post_init__(self) -> None:
        if self.max_concurrency is None:
            object.__setattr__(self, "max_concurrency", _default_max_concurrency())
        if self.max_concurrency_ceiling is None:
            object.__setattr__(
                self, "max_concurrency_ceiling",
                max(self.max_concurrency, _default_max_concurrency_ceiling()),
            )
        rounds = finite_number(self.max_rounds, "max_rounds", integer=True)
        calls = finite_number(self.max_tool_calls, "max_tool_calls", integer=True)
        concurrency = finite_number(
            self.max_concurrency, "max_concurrency", integer=True,
        )
        wall = finite_number(self.max_wall_seconds, "max_wall_seconds")
        round_wall = finite_number(
            self.max_round_wall_seconds, "max_round_wall_seconds",
        )
        cost = finite_number(self.max_cost_units, "max_cost_units")
        if rounds < 1:
            raise ValueError("max_rounds 必须 >= 1")
        if calls < 0:
            raise ValueError("max_tool_calls 必须 >= 0")
        if concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        if wall <= 0 or round_wall <= 0:
            raise ValueError("wall budget 必须 > 0")
        if cost < 0:
            raise ValueError("max_cost_units 必须 >= 0")
        if not isinstance(self.require_parallel, bool):
            raise ValueError("require_parallel 必须是布尔值")
        if not isinstance(self.require_confirmation, bool):
            raise ValueError("require_confirmation 必须是布尔值")
        if not isinstance(self.adaptive_concurrency, bool):
            raise ValueError("adaptive_concurrency 必须是布尔值")
        min_c = finite_number(self.min_concurrency, "min_concurrency", integer=True)
        max_c = finite_number(self.max_concurrency_ceiling, "max_concurrency_ceiling", integer=True)
        if min_c < 1:
            raise ValueError("min_concurrency 必须 >= 1")
        if max_c < concurrency:
            raise ValueError("max_concurrency_ceiling 必须 >= max_concurrency")
        if not self.require_parallel:
            if not isinstance(self.serial_reason, str) or not self.serial_reason.strip():
                raise ValueError("允许串行时必须提供 serial_reason")
        elif self.serial_reason is not None and not isinstance(self.serial_reason, str):
            raise ValueError("serial_reason 必须是字符串或 null")

    def to_dict(self) -> dict[str, int | float | bool | str | None]:
        return {
            "max_rounds": self.max_rounds,
            "max_wall_seconds": self.max_wall_seconds,
            "max_round_wall_seconds": self.max_round_wall_seconds,
            "max_tool_calls": self.max_tool_calls,
            "max_cost_units": self.max_cost_units,
            "max_concurrency": self.max_concurrency,
            "require_parallel": self.require_parallel,
            "serial_reason": self.serial_reason,
            "require_confirmation": self.require_confirmation,
            "adaptive_concurrency": self.adaptive_concurrency,
            "min_concurrency": self.min_concurrency,
            "max_concurrency_ceiling": self.max_concurrency_ceiling,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RunBudget":
        required = {
            "max_rounds", "max_wall_seconds", "max_round_wall_seconds",
            "max_tool_calls", "max_cost_units",
        }
        missing = required - set(value)
        if missing:
            raise PersistenceError(f"budget 缺字段: {sorted(missing)}")
        try:
            require_parallel = value.get("require_parallel", True)
            serial_reason = value.get("serial_reason")
            require_confirmation = value.get("require_confirmation", False)
            adaptive_concurrency = value.get("adaptive_concurrency", True)
            min_concurrency = value.get("min_concurrency", 2)
            max_c_val = value.get("max_concurrency")
            max_concurrency = int(max_c_val) if max_c_val is not None else _default_max_concurrency()
            max_ceiling_val = value.get("max_concurrency_ceiling")
            max_concurrency_ceiling = (
                int(max_ceiling_val) if max_ceiling_val is not None
                else max(max_concurrency, _default_max_concurrency_ceiling())
            )
            if not isinstance(require_parallel, bool):
                raise ValueError("require_parallel 必须是布尔值")
            if serial_reason is not None and not isinstance(serial_reason, str):
                raise ValueError("serial_reason 必须是字符串或 null")
            if not isinstance(require_confirmation, bool):
                raise ValueError("require_confirmation 必须是布尔值")
            if not isinstance(adaptive_concurrency, bool):
                raise ValueError("adaptive_concurrency 必须是布尔值")
            return cls(
                max_rounds=int(value["max_rounds"]),
                max_wall_seconds=float(value["max_wall_seconds"]),
                max_round_wall_seconds=float(value["max_round_wall_seconds"]),
                max_tool_calls=int(value["max_tool_calls"]),
                max_cost_units=float(value["max_cost_units"]),
                max_concurrency=max_concurrency,
                require_parallel=require_parallel,
                serial_reason=serial_reason,
                require_confirmation=require_confirmation,
                adaptive_concurrency=adaptive_concurrency,
                min_concurrency=int(min_concurrency),
                max_concurrency_ceiling=max_concurrency_ceiling,
            )
        except (TypeError, ValueError) as exc:
            raise PersistenceError(f"budget 无效: {exc}") from exc


def load_unit_notes(value: object) -> dict[str, dict[str, object]]:
    """从持久化状态恢复 unit-notes；无效条目静默丢弃（不阻断恢复）。"""
    if not isinstance(value, Mapping):
        return {}
    notes: dict[str, dict[str, object]] = {}
    for unit_id, item in value.items():
        if not isinstance(unit_id, str) or not isinstance(item, Mapping):
            continue
        note = item.get("note")
        if not isinstance(note, str) or not note.strip():
            continue
        notes[unit_id] = {
            "worker_id": str(item.get("worker_id", "unknown")),
            "note": note,
            "at": item.get("at", 0.0),
        }
    return dict(list(notes.items())[-MAX_UNIT_NOTES:])


@dataclass
class SchedulingMetrics:
    """调度层全链路性能度量与遥测快照 (DOC-044)。"""

    total_claims: int = 0
    total_completed: int = 0
    total_preempted: int = 0
    total_expired: int = 0
    total_queue_seconds: float = 0.0
    total_execution_seconds: float = 0.0
    max_queue_seconds: float = 0.0
    max_execution_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        avg_q = self.total_queue_seconds / max(1, self.total_claims)
        avg_e = self.total_execution_seconds / max(1, self.total_completed)
        return {
            "total_claims": self.total_claims,
            "total_completed": self.total_completed,
            "total_preempted": self.total_preempted,
            "total_expired": self.total_expired,
            "total_queue_seconds": round(self.total_queue_seconds, 3),
            "total_execution_seconds": round(self.total_execution_seconds, 3),
            "max_queue_seconds": round(self.max_queue_seconds, 3),
            "max_execution_seconds": round(self.max_execution_seconds, 3),
            "avg_queue_seconds": round(avg_q, 3),
            "avg_execution_seconds": round(avg_e, 3),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "SchedulingMetrics":
        if not isinstance(value, Mapping):
            return cls()
        return cls(
            total_claims=int(value.get("total_claims", 0)),
            total_completed=int(value.get("total_completed", 0)),
            total_preempted=int(value.get("total_preempted", 0)),
            total_expired=int(value.get("total_expired", 0)),
            total_queue_seconds=float(value.get("total_queue_seconds", 0.0)),
            total_execution_seconds=float(value.get("total_execution_seconds", 0.0)),
            max_queue_seconds=float(value.get("max_queue_seconds", 0.0)),
            max_execution_seconds=float(value.get("max_execution_seconds", 0.0)),
        )


@dataclass
class RunState:
    """可持久化的 Supervisor 状态快照。"""

    run_id: str
    status: Status
    round_no: int
    life_score: float
    budget: RunBudget
    started_at: float
    budget_started_at: float
    round_started_at: float
    # D3：墙钟高水位——本 run 已观测到的最晚墙钟时刻。预算判定用
    # ``max(now, clock_high_water)`` 作为有效 now，使「已计时长」的记账
    # 只增不减：系统时钟回拨不得把已耗掉的墙钟预算还回来（不得续命）。
    clock_high_water: float = 0.0
    life_kind: str = "mine"
    budget_epoch: int = 0
    scheduler_epoch: int = 0
    tool_calls: int = 0
    budget_tool_calls: int = 0
    cost_units: float = 0.0
    budget_cost_units: float = 0.0
    # 算力消耗与多轮版本控制 (DOC-196)
    tokens_consumed: int = 0
    token_accounting: dict[str, int] = field(default_factory=dict)
    round_version: int = 1
    budget_exhausted: bool = False
    budget_reasons: list[str] = field(default_factory=list)
    claims: dict[str, WorkerClaim] = field(default_factory=dict)
    findings: list[dict[str, object]] = field(default_factory=list)
    # A5: report publication cursor and seal are persisted with the same
    # Supervisor snapshot as findings, so readers never observe a newer report
    # paired with an older state revision.
    findings_revision: int = 0
    report_seal: dict[str, object] | None = None
    report_consumption: dict[str, object] = field(default_factory=lambda: {"phase": "OPEN"})
    verification_runs: list[dict[str, object]] = field(default_factory=list)
    verification_protocol_version: int = 2
    confirmation_gate: dict[str, object] = field(default_factory=lambda: {
        "ready": True, "total": 0, "confirmed": 0, "rejected": 0, "pending": 0,
    })
    scheduling_metrics: SchedulingMetrics = field(default_factory=SchedulingMetrics)
    coverage: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, object]] = field(default_factory=list)
    audit_head: str = ""
    # Hash-chain anchor for the first retained event after bounded compaction.
    history_base_seq: int = 1
    history_base_hash: str = ""
    revision: int = 0
    completed_at: float | None = None
    last_error: str | None = None
    round_max_active_workers: int = 0
    round_overlap_seconds: float = 0.0
    # 跨单元系统级故障级联熔断 (DOC-242)
    systemic_failures: dict[str, str] = field(default_factory=dict)
    systemic_failure_units: dict[str, str] = field(default_factory=dict)
    round_parallel_active_since: float | None = None
    parallel_history: list[dict[str, object]] = field(default_factory=list)
    # reset_budget 不会抹掉已经完成的并发证明；它们按 epoch 留在这里。
    parallel_proof_history: list[dict[str, object]] = field(default_factory=list)
    # Persisted AIMD state so each workers/claim process continues the same
    # adaptive window instead of resetting to min_concurrency.
    adaptive_concurrency_window: float | None = None
    adaptive_success_streak: int = 0
    adaptive_failure_streak: int = 0
    # worker 提交的可复现错误签名（有界）；供 incident hints 检索深案例。
    recent_signatures: list[str] = field(default_factory=list)
    # 断点手记：unit_id -> {worker_id, note, at}；恢复/接手 worker 的上下文。
    unit_notes: dict[str, dict[str, object]] = field(default_factory=dict)
    # BugX 层级绑定：Supervisor 只负责当前 Life 的 Worker 控制面，但必须
    # 能证明该控制面属于哪个项目、Session、Round 及冻结计划。
    project_id: str | None = None
    session_id: str | None = None
    round_id: str | None = None
    life_id: str | None = None
    plan_hash: str | None = None
    # Snapshot of the source/coverage target when this run was created.
    target_fingerprint: str | None = None
    business_admissions: dict[str, dict[str, object]] = field(default_factory=dict)
    # Optional DOC-232 graph state shares this snapshot, lock, revision, and
    # event chain with ordinary Supervisor state.
    agent_graph: GraphRun | None = None
    dirty: bool = field(default=False, repr=False, compare=False)

    @property
    def active_claims(self) -> dict[str, WorkerClaim]:
        return {
            worker_id: claim
            for worker_id, claim in self.claims.items()
            if claim.active
        }

    # A3：NA 比例硬上限。无上限时"把难挖的单元标成 not_applicable"是收敛
    # 捷径——覆盖率 100% 但实际啥也没挖。上限让覆盖率保持诚实；超限时
    # coverage_complete 仍为真（单元确实都有终态交代），但
    # coverage_na_over_limit 暴露该事实，供结算门禁裁决。
    MAX_NOT_APPLICABLE_RATIO = 0.20

    @property
    def coverage_complete(self) -> bool:
        return bool(self.coverage) and all(
            state in TERMINAL_COVERED for state in self.coverage.values()
        )

    @property
    def not_applicable_ratio(self) -> float:
        if not self.coverage:
            return 0.0
        na = sum(
            1 for s in self.coverage.values()
            if s == COVERAGE_NOT_APPLICABLE
        )
        return na / len(self.coverage)

    @property
    def coverage_na_over_limit(self) -> bool:
        """NA 比例超限——覆盖"完成"但成色不足，需人工/门禁复核。"""
        return self.not_applicable_ratio > self.MAX_NOT_APPLICABLE_RATIO

    @property
    def coverage_debt(self) -> dict[str, object]:
        """按 DOC-011 §5.4/§5.5 分类覆盖债务：单元级债务 vs 全局阻塞 vs 已终态。"""
        counts: dict[str, int] = {state: 0 for state in VALID_COVERAGE_STATES}
        for state in self.coverage.values():
            counts[state] = counts.get(state, 0) + 1
        unit_debt = sum(counts.get(k, 0) for k in UNIT_LEVEL_DEBT)
        global_blocked = sum(counts.get(k, 0) for k in GLOBAL_BLOCKED)
        covered = sum(counts.get(k, 0) for k in TERMINAL_COVERED)
        return {
            "total": len(self.coverage),
            "covered": covered,
            "unit_debt": unit_debt,
            "global_blocked": global_blocked,
            "by_state": counts,
            "has_unit_debt": unit_debt > 0,
            "has_global_block": global_blocked > 0,
            # A3：NA 成色——超限时覆盖虽"完成"但不可信，调度/结算应可见。
            "not_applicable_ratio": round(self.not_applicable_ratio, 4),
            "na_over_limit": self.coverage_na_over_limit,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": self.status.value,
            "round_no": self.round_no,
            "life_score": self.life_score,
            "life_kind": self.life_kind,
            "budget": self.budget.to_dict(),
            "started_at": self.started_at,
            "budget_started_at": self.budget_started_at,
            "round_started_at": self.round_started_at,
            "clock_high_water": self.clock_high_water,
            "budget_epoch": self.budget_epoch,
            "scheduler_epoch": self.scheduler_epoch,
            "tool_calls": self.tool_calls,
            "budget_tool_calls": self.budget_tool_calls,
            "cost_units": self.cost_units,
            "budget_cost_units": self.budget_cost_units,
            "budget_exhausted": self.budget_exhausted,
            "budget_reasons": list(self.budget_reasons),
            "claims": {
                worker_id: claim.to_dict()
                for worker_id, claim in sorted(self.claims.items())
            },
            "findings": copy.deepcopy(self.findings),
            "findings_revision": self.findings_revision,
            "report_seal": copy.deepcopy(self.report_seal),
            "report_consumption": copy.deepcopy(self.report_consumption),
            "verification_runs": copy.deepcopy(self.verification_runs),
            "verification_protocol_version": self.verification_protocol_version,
            "confirmation_gate": copy.deepcopy(self.confirmation_gate),
            "coverage": dict(sorted(self.coverage.items())),
            "business_admissions": copy.deepcopy(self.business_admissions),
            "history": copy.deepcopy(self.history),
            # A1：惰性校验锚点随状态持久化，使跨进程的每次加载都跳过
            # 已校验前缀的重算（history 无界增长下的二次瓶颈）。
            _HISTORY_ANCHOR_KEY: (
                dict(getattr(self, _HISTORY_ANCHOR_KEY, None) or {})
                if getattr(self, _HISTORY_ANCHOR_KEY, None) else None
            ),
            "audit_head": self.audit_head,
            "history_base_seq": self.history_base_seq,
            "history_base_hash": self.history_base_hash,
            "revision": self.revision,
            "completed_at": self.completed_at,
            "last_error": self.last_error,
            "round_max_active_workers": self.round_max_active_workers,
            "round_overlap_seconds": self.round_overlap_seconds,
            "round_parallel_active_since": self.round_parallel_active_since,
            "parallel_history": copy.deepcopy(self.parallel_history),
            "parallel_proof_history": copy.deepcopy(self.parallel_proof_history),
            "adaptive_concurrency_window": self.adaptive_concurrency_window,
            "adaptive_success_streak": self.adaptive_success_streak,
            "adaptive_failure_streak": self.adaptive_failure_streak,
            "recent_signatures": list(self.recent_signatures),
            "unit_notes": copy.deepcopy(self.unit_notes),
            "project_id": self.project_id,
            "session_id": self.session_id,
            "round_id": self.round_id,
            "life_id": self.life_id,
            "plan_hash": self.plan_hash,
            "target_fingerprint": self.target_fingerprint,
            "agent_graph": (
                None if self.agent_graph is None else self.agent_graph.to_dict()
            ),
            "scheduling_metrics": self.scheduling_metrics.to_dict(),
            "tokens_consumed": self.tokens_consumed,
            "token_accounting": copy.deepcopy(self.token_accounting),
            "round_version": self.round_version,
            "systemic_failures": copy.deepcopy(self.systemic_failures),
            "systemic_failure_units": copy.deepcopy(self.systemic_failure_units),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RunState":
        schema_version = value.get("schema_version")
        if schema_version not in {1, SCHEMA_VERSION}:
            raise PersistenceError("不支持的 Supervisor state schema")
        try:
            try:
                status = Status(str(value["status"]))
            except ValueError:
                status = Status.RUNNING
            budget = RunBudget.from_dict(value["budget"])  # type: ignore[arg-type]
            claims_raw = value.get("claims", {})
            if not isinstance(claims_raw, Mapping):
                raise TypeError("claims 必须是对象")
            claims = {}
            for raw_key, item in claims_raw.items():
                claim_obj = WorkerClaim.from_dict(item)  # type: ignore[arg-type]
                if str(raw_key) != claim_obj.worker_id:
                    raise PersistenceError(
                        f"claims key '{raw_key}' 与 claim.worker_id '{claim_obj.worker_id}' 不一致"
                    )
                claims[claim_obj.worker_id] = claim_obj
            findings_raw = value.get("findings", [])
            confirmation_raw = value.get("confirmation_gate", {
                "ready": True, "total": 0, "confirmed": 0, "rejected": 0, "pending": 0,
            })
            coverage_raw = value.get("coverage", {})
            history_raw = value.get("history", [])
            if not isinstance(findings_raw, list):
                raise TypeError("findings 必须是数组")
            findings = copy.deepcopy(findings_raw)
            for finding in findings:
                if isinstance(finding, dict) and "severity" in finding:
                    finding["severity"] = normalize_finding_severity(
                        finding["severity"]
                    )
            if not isinstance(confirmation_raw, Mapping):
                raise TypeError("confirmation_gate 必须是对象")
            if not isinstance(coverage_raw, Mapping):
                raise TypeError("coverage 必须是对象")
            if not isinstance(history_raw, list):
                raise TypeError("history 必须是数组")
            state = cls(
                run_id=str(value["run_id"]),
                status=status,
                round_no=int(value["round_no"]),
                life_score=float(value["life_score"]),
                life_kind=str(value.get("life_kind", "mine")),
                budget=budget,
                started_at=float(value["started_at"]),
                budget_started_at=float(value["budget_started_at"]),
                round_started_at=float(value["round_started_at"]),
                # D3：旧快照无此字段时回落 0.0（=「尚未观测」），首次预算
                # 判定即从当前墙钟重建水位；水位本身只是防回拨的下界。
                clock_high_water=float(value.get("clock_high_water", 0.0)),
                budget_epoch=int(value.get("budget_epoch", 0)),
                scheduler_epoch=int(value.get("scheduler_epoch", 0)),
                tool_calls=int(value.get("tool_calls", 0)),
                budget_tool_calls=int(value.get("budget_tool_calls", 0)),
                cost_units=float(value.get("cost_units", 0.0)),
                budget_cost_units=float(value.get("budget_cost_units", 0.0)),
                budget_exhausted=bool(value.get("budget_exhausted", False)),
                budget_reasons=[str(item) for item in value.get("budget_reasons", [])],
                claims=claims,
                findings=findings,  # type: ignore[arg-type]
                findings_revision=int(value.get("findings_revision", 0)),
                report_seal=(
                    None if value.get("report_seal") is None
                    else copy.deepcopy(dict(value.get("report_seal")))
                ),
                report_consumption=copy.deepcopy(dict(value.get("report_consumption", {"phase": "OPEN"}))),
                verification_runs=copy.deepcopy(value.get("verification_runs", [])),
                verification_protocol_version=int(value.get("verification_protocol_version", 1)),
                confirmation_gate=copy.deepcopy(dict(confirmation_raw)),
                coverage={str(key): str(item) for key, item in coverage_raw.items()},
                history=copy.deepcopy(history_raw),  # type: ignore[arg-type]
                audit_head=str(value.get("audit_head", "")),
                history_base_seq=int(value.get("history_base_seq", 1)),
                history_base_hash=str(value.get("history_base_hash", "")),
                revision=int(value.get("revision", 0)),
                completed_at=(
                    None if value.get("completed_at") is None
                    else float(value["completed_at"])
                ),
                last_error=(
                    None if value.get("last_error") is None
                    else str(value["last_error"])
                ),
                round_max_active_workers=int(
                    value.get("round_max_active_workers", 0)
                ),
                round_overlap_seconds=float(
                    value.get("round_overlap_seconds", 0.0)
                ),
                round_parallel_active_since=(
                    None if value.get("round_parallel_active_since") is None
                    else float(value["round_parallel_active_since"])
                ),
                parallel_history=copy.deepcopy(
                    value.get("parallel_history", [])
                ),
                parallel_proof_history=copy.deepcopy(
                    value.get("parallel_proof_history", [])
                ),
                adaptive_concurrency_window=(
                    None if value.get("adaptive_concurrency_window") is None
                    else float(value["adaptive_concurrency_window"])
                ),
                adaptive_success_streak=int(
                    value.get("adaptive_success_streak", 0)
                ),
                adaptive_failure_streak=int(
                    value.get("adaptive_failure_streak", 0)
                ),
                recent_signatures=[
                    str(item) for item in value.get("recent_signatures", [])
                    if isinstance(item, str) and item.strip()
                ],
                unit_notes=load_unit_notes(value.get("unit_notes")),
                project_id=(None if value.get("project_id") is None else str(value.get("project_id"))),
                session_id=(None if value.get("session_id") is None else str(value.get("session_id"))),
                round_id=(None if value.get("round_id") is None else str(value.get("round_id"))),
                life_id=(None if value.get("life_id") is None else str(value.get("life_id"))),
                plan_hash=(None if value.get("plan_hash") is None else str(value.get("plan_hash"))),
                target_fingerprint=(
                    None if value.get("target_fingerprint") is None
                    else str(value.get("target_fingerprint"))
                ),
                agent_graph=(
                    None if value.get("agent_graph") is None
                    else GraphRun.from_dict(value["agent_graph"])  # type: ignore[arg-type]
                ),
                scheduling_metrics=SchedulingMetrics.from_dict(
                    value.get("scheduling_metrics")  # type: ignore[arg-type]
                ),
                tokens_consumed=int(value.get("tokens_consumed", 0)),
                token_accounting=dict(value.get("token_accounting", {})),
                round_version=int(value.get("round_version", 1)),
                systemic_failures=dict(value.get("systemic_failures", {})),
                systemic_failure_units=dict(value.get("systemic_failure_units", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError(f"state 无效: {exc}") from exc
        admissions = value.get("business_admissions")
        if "business_admissions" not in value:
            admissions = {}
            for event in state.history:
                if event.get("type") == "target_rebound":
                    admissions.clear()
                elif event.get("type") == "business_admission":
                    admissions[event.get("unit")] = {
                        "reason": event.get("reason"), "risk_score": event.get("risk_score"),
                    }
        if not isinstance(admissions, dict):
            raise PersistenceError("business_admissions 必须是对象")
        for unit, record in admissions.items():
            if (not isinstance(unit, str) or not unit_kind.is_business(unit)
                    or unit not in state.coverage or not isinstance(record, dict)
                    or not isinstance(record.get("reason"), str) or not record["reason"].strip()
                    or isinstance(record.get("risk_score"), bool)
                    or not isinstance(record.get("risk_score"), (int, float))
                    or not 70 <= record["risk_score"] <= 100):
                raise PersistenceError("business_admissions 包含无效授权")
        state.business_admissions = copy.deepcopy(admissions)
        if not state.run_id or state.round_no < 1 or state.revision < 0:
            raise PersistenceError("state 基本字段无效")
        if state.history_base_seq < 1 or not isinstance(state.history_base_hash, str):
            raise PersistenceError("history anchor 无效")
        finite_fields = (
            "life_score", "started_at", "budget_started_at", "round_started_at",
            "cost_units", "budget_cost_units", "round_overlap_seconds",
            "clock_high_water",
        )
        for name in finite_fields:
            value = getattr(state, name)
            if not math.isfinite(float(value)):
                raise PersistenceError(f"state {name} 必须是有限数字")
        if state.clock_high_water < 0:
            raise PersistenceError("state clock_high_water 不能为负")
        for name in ("completed_at", "round_parallel_active_since"):
            value = getattr(state, name)
            if value is not None and not math.isfinite(float(value)):
                raise PersistenceError(f"state {name} 必须是有限数字或 null")
        if state.adaptive_concurrency_window is not None and (
            not math.isfinite(float(state.adaptive_concurrency_window))
            or state.adaptive_concurrency_window <= 0
        ):
            raise PersistenceError("state adaptive_concurrency_window 必须是正数或 null")
        integer_fields = (
            "budget_epoch", "scheduler_epoch", "tool_calls", "budget_tool_calls",
            "round_max_active_workers", "adaptive_success_streak",
            "adaptive_failure_streak", "findings_revision", "verification_protocol_version",
        )
        if any(isinstance(getattr(state, name), bool) or getattr(state, name) < 0
               for name in integer_fields):
            raise PersistenceError("state 计数器必须是非负整数")
        if state.cost_units < 0 or state.budget_cost_units < 0:
            raise PersistenceError("state cost_units 不能为负")
        if state.life_kind not in {"mine", "fix"}:
            raise PersistenceError("state life_kind 必须是 mine 或 fix")
        for name in ("project_id", "session_id", "round_id", "life_id"):
            value = getattr(state, name)
            if value is not None and not value.strip():
                raise PersistenceError(f"state {name} 不能是空字符串")
        if state.plan_hash is not None and not SHA256_RE.fullmatch(state.plan_hash):
            raise PersistenceError("state plan_hash 必须是 SHA-256")
        if state.target_fingerprint is not None and not state.target_fingerprint.strip():
            raise PersistenceError("state target_fingerprint 不能是空字符串")
        # completed_at is mandatory for every formally finished terminal
        # status; budget_exhausted alone does not imply completion.
        # D2：INCOMPLETE 是 finish 三分支写下的收尾终态，同受此不变量约束。
        if state.status in {
            Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
        } and state.completed_at is None:
            raise PersistenceError(
                "status=COMPLETE/INCOMPLETE/FAILED 时必须设置 completed_at"
            )
        if any(item not in VALID_COVERAGE_STATES for item in state.coverage.values()):
            raise PersistenceError("coverage 含未知状态")
        if state.round_max_active_workers < 0 or state.round_overlap_seconds < 0:
            raise PersistenceError("并发观测字段无效")
        if not isinstance(state.parallel_history, list):
            raise PersistenceError("parallel_history 必须是数组")
        if not isinstance(state.parallel_proof_history, list):
            raise PersistenceError("parallel_proof_history 必须是数组")
        if not isinstance(state.confirmation_gate, dict):
            raise PersistenceError("confirmation_gate 必须是对象")
        if state.report_seal is not None and not isinstance(state.report_seal, dict):
            raise PersistenceError("report_seal 必须是对象或 null")
        if not isinstance(state.report_consumption, dict):
            raise PersistenceError("report_consumption 必须是对象")
        if state.report_consumption.get("phase", "OPEN") not in {
            "OPEN", "POSTING", "SETTLED",
            "RECOVERY_REQUIRED",
        }:
            raise PersistenceError("report_consumption.phase 无效")
        if not isinstance(state.verification_runs, list):
            raise PersistenceError("verification_runs 必须是数组")
        if state.verification_protocol_version not in {1, 2}:
            raise PersistenceError("verification_protocol_version 不支持")
        validate_history(state)
        return state


def event_hash(event: Mapping[str, object]) -> str:
    body = {key: value for key, value in event.items() if key != "hash"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


# A1：history 无界增长使每次 _mutate 的全链校验成为二次瓶颈。锚点机制：
# state 持久化「已校验到第 N 条、该条 hash 为 H」后，加载时只需重算
# 第 N 条的 hash 与锚点比对（O(1) 证明前缀未被篡改），再校验其后的事件。
# 语义不变：任何对前缀的篡改都会打破锚点 hash 而被发现。
_HISTORY_ANCHOR_KEY = "history_anchor"


def validate_history(state: RunState) -> None:
    total = len(state.history)
    start_index = 0
    previous = state.history_base_hash
    expected_seq = state.history_base_seq
    anchor = getattr(state, _HISTORY_ANCHOR_KEY, None)
    if isinstance(anchor, Mapping) and total:
        try:
            anchor_seq = int(anchor.get("seq", 0))
            anchor_hash = str(anchor.get("hash", ""))
        except (TypeError, ValueError):
            anchor_seq, anchor_hash = 0, ""
        anchor_index = anchor_seq - state.history_base_seq
        if 0 <= anchor_index < total:
            event = state.history[anchor_index]
            if (isinstance(event, Mapping)
                    and event.get("seq") == anchor_seq
                    and event.get("hash") == anchor_hash
                    and event.get("hash") == event_hash(event)):
                # 前缀锚点自证通过：跳过前 anchor_seq 条的重算。
                start_index = anchor_index + 1
                previous = anchor_hash
                expected_seq = anchor_seq + 1
    for expected_seq, event in enumerate(
        state.history[start_index:], start=expected_seq,
    ):
        if not isinstance(event, Mapping):
            raise PersistenceError("history 事件必须是对象")
        if event.get("seq") != expected_seq:
            raise PersistenceError("history seq 不连续")
        if event.get("prev_hash", "") != previous:
            raise PersistenceError("history hash-chain 前置 hash 不一致")
        stored_hash = event.get("hash")
        if not isinstance(stored_hash, str) or stored_hash != event_hash(event):
            raise PersistenceError("history hash-chain 校验失败")
        previous = stored_hash
        expected_seq += 1
    if state.audit_head != previous:
        raise PersistenceError("audit_head 与 history 不一致")
    # 校验完成后推进锚点，使下次加载继续受益。
    if total:
        object.__setattr__(state, _HISTORY_ANCHOR_KEY, {
            "seq": state.history_base_seq + total - 1,
            "hash": previous,
        })


