#!/usr/bin/env python3
"""core/supervisor/budget_governor.py - Supervisor 预算管控与并发协调服务 (DOC-070)。

负责轮次截止期、软硬预算监控、并发状态对齐、僵尸检测及预算耗尽状态转换。

恢复会话结算语义（2026-11-17 用户裁决，三维度）：

* 维度 1【继承豁免】：expired claim 的单元若已 100% 沿链继承覆盖（清单行带
  ``inherited_from`` 备注且 ``coverage_discovery.resolve_inherited_evidence``
  溯源可达祖先 passed 证据），该 claim 标记 ``superseded_by_inheritance``
  并从并行门的 expired 重试债集合移除，不再重派；未覆盖单元才进入重派路径。
  标记以 hash-chain 事件 ``expired_claim_superseded_by_inheritance`` 落账
  （不改动 WorkerClaim 持久化 schema）；豁免按当前清单状态**确定性重算**，
  清单继承引用被内容失效撤回时重试债如实回归。
* 维度 2【恢复场景豁免】：并行门（require_parallel 的并发证明债）对恢复
  会话豁免，三条件缺一不可（fail-closed）：① owner_cancelled 死亡证明在
  Supervisor 权威链（operation=lifecycle, gate=owner_cancelled）；② 本 Life
  covered 100% 来自继承+历史证据（判定口径：本 round 零新增证据记录，含
  failed/error——OQ-2 加强，不只 passed）；③ 本会话无新增 findings（owner
  死亡锚点之后 findings 增量为 0）。豁免动作落专项留痕事件
  ``parallel_gate_inherited_closeout``（含三条件逐项快照）；非 ``--final``
  收口场景由 post 侧拒绝消费该豁免（D5）。
* 维度 3【合账语义（2026-11-17 用户裁决"新证据为准 + chain 保留"）】：单元
  证据分三层——L1 本轮重派本地记录 / L2 本 Life 历史本地记录 / L3 沿链继承
  （清单行 ``inherited_from`` 备注 + ``resolve_inherited_evidence`` 溯源）。
  判定读取优先级 L1/L2 本地最新 > L3 沿链（R-1，实现在
  ``coverage_discovery.inherited_unit_coverage_verdicts`` /
  ``recovery_closeout_coverage_census``）：本函数消费 verdict 的 ``backing``
  归属字段（supersede 快照可区分凭继承/凭本地证据豁免），维度 1 决策逻辑
  不变——字面只有沿链继承豁免（OQ-1 有意的 fail-closed 浪费）；清单行遗留
  备注永不改写（R-3 chain 保留），本地新证据推翻继承时 divergence 随 census
  快照显性化（R-5）。
"""

from __future__ import annotations

import dataclasses
import re
import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


ROUND_HEARTBEAT_TIMEOUT_SECONDS = 450.0
LONG_CALL_MAX_SILENCE_SECONDS = 600.0

# 系统级熔断与预算耗尽写入已下沉到 systemic_failure / claim_events（2026-10-26
# 架构收敛）：budget_governor 与 lease_manager 的双向依赖由此消除。这里 re-export，
# 既有 `from .budget_governor import ...` 调用点保持不变。
try:
    from .claim_events import append_event
    from .systemic_failure import (
        SYSTEMIC_FAILURE_MARKERS,
        check_and_record_systemic_failure,
        set_budget_exhausted,
    )
except ImportError:  # pragma: no cover
    from core.supervisor.claim_events import append_event
    from core.supervisor.systemic_failure import (
        SYSTEMIC_FAILURE_MARKERS,
        check_and_record_systemic_failure,
        set_budget_exhausted,
    )

from .supervisor_models import (
    BudgetExhausted,
    InvalidCheckpoint,
    InvalidTransition,
    MAX_LEASE_SECONDS,
    MAX_WORKER_ATTEMPTS,
    TERMINAL_COVERED,
    RunBudget,
    RunState,
    Status,
    WorkerClaim,
    workspace_fingerprint,
)

from core.supervisor.lease_manager import (
    eligible_units,
    mark_claim_expired,
    mark_worker_attempts_exhausted,
    record_expiry_accounting,
)


# ---------------------------------------------------------------------------
# 第四批缺陷修复（假并发门禁）：并发证据只认**真实 worker**。
#
# 实证（2026-11-16 zcode 会话 supervisor.json）：judge 判读取证电池的记账式
# claim（provider=judge-collect/judge-flow，core/launch_phases/judge_phase.py）
# process_pid=None、tokens_consumed=0、心跳零推进、同波 execution_finished_at
# 逐秒相同——它们是引擎判读取证的记账，不是 LLM worker；但重叠执行窗口曾被
# 并行门禁当作真实并发证据（parallel_history max_active=6/overlap 787s）。
#
# 真实性判据（三选二，与 provider 解耦——记账方未来换名也不失效）：
#   1. process_pid 非空（真实进程被启动）；
#   2. tokens_consumed > 0（LLM 用量被实测）；
#   3. 心跳推进：last_heartbeat_at 晚于 execution_started_at
#      （记账 claim 的 last_heartbeat_at 在 start_worker 时一次性写入，
#      与 started_at 同值，永不推进——实测字段特征）。
# ---------------------------------------------------------------------------
WORKER_AUTHENTICITY_MIN_SIGNALS = 2


def worker_authenticity_signals(claim: WorkerClaim) -> int:
    """统计单条 claim 的真实 worker 证据数（0..3）。"""
    signals = 0
    if getattr(claim, "process_pid", None) is not None:
        signals += 1
    try:
        if int(getattr(claim, "tokens_consumed", 0) or 0) > 0:
            signals += 1
    except (TypeError, ValueError):
        pass
    heartbeat_at = getattr(claim, "last_heartbeat_at", None)
    started_at = getattr(claim, "execution_started_at", None)
    if heartbeat_at is not None and (
        started_at is None or float(heartbeat_at) > float(started_at)
    ):
        signals += 1
    return signals


def is_authentic_worker_claim(claim: WorkerClaim) -> bool:
    """三选二：满足至少两项真实证据才算 LLM worker 的执行证据。"""
    return worker_authenticity_signals(claim) >= WORKER_AUTHENTICITY_MIN_SIGNALS


def reconcile_parallel(state: RunState, now: float) -> None:
    """从持久化执行窗口重建本轮真实重叠，跨进程 resume 也不丢失。

    只有真实 worker（:func:`is_authentic_worker_claim`）的执行窗口才构成
    并发证据；judge 记账式 claim 不再能把 ``round_max_active_workers`` /
    ``round_overlap_seconds`` 抬到假达标。
    """
    intervals: list[tuple[float, float]] = []
    for claim in state.claims.values():
        if claim.round_no != state.round_no or claim.execution_started_at is None:
            continue
        if not is_authentic_worker_claim(claim):
            continue
        start = float(claim.execution_started_at)
        if claim.execution_state == "running":
            # B5：开区间右端必须夹到真实租约边界，否则僵尸 running claim 的
            # 重叠时长会随等待时间线性增长（并发证据可被"等得够久"伪造）。
            boundaries = [now]
            if claim.lease_expires_at is not None:
                boundaries.append(float(claim.lease_expires_at))
            if claim.total_lease_deadline is not None:
                boundaries.append(float(claim.total_lease_deadline))
            end = min(boundaries)
        else:
            end = float(
                claim.execution_finished_at
                or claim.submitted_at
                or claim.lease_expires_at
            )
        if end > start:
            intervals.append((start, end))
    events: dict[float, int] = {}
    for start, end in intervals:
        events[start] = events.get(start, 0) + 1
        events[end] = events.get(end, 0) - 1
    active_count = 0
    peak = sum(
        claim.active and claim.execution_state == "running"
        and is_authentic_worker_claim(claim)
        for claim in state.claims.values()
        if claim.round_no == state.round_no
    )
    overlap = 0.0
    points = sorted(events)
    for index, point in enumerate(points):
        active_count += events[point]
        peak = max(peak, active_count)
        if index + 1 < len(points) and active_count >= 2:
            overlap += max(0.0, points[index + 1] - point)
    state.round_max_active_workers = peak
    state.round_overlap_seconds = round(overlap, 6)
    current_running = sum(
        claim.active and claim.execution_state == "running"
        and is_authentic_worker_claim(claim)
        and claim.round_no == state.round_no
        for claim in state.claims.values()
    )
    # LOG-10：``current_running`` 必须与 ``peak`` / ``round_max_active_workers``
    # 同口径（只统计当前轮）。历史实现跨轮统计，导致上一轮遗留的 running claim
    # 就能把本轮 ``round_parallel_active_since`` 置位，误报「本轮并发达标」。
    state.round_parallel_active_since = now if current_running >= 2 else None


def _record_adaptive_outcome(state: RunState, outcome: str) -> None:
    try:
        from . import task_scheduler
        task_scheduler.AdaptiveConcurrencyController(
            min_concurrency=state.budget.min_concurrency,
            max_concurrency=state.budget.max_concurrency_ceiling,
        ).record_outcome(state, outcome)
    except Exception:
        # Scheduling feedback is advisory; never hide the lease/claim state.
        pass


def _renew_expired_live_claim(
    state: RunState, claim: WorkerClaim, now: float, mono_now: float,
) -> bool:
    """到期时保留仍有进展证据的 Worker，并延长原租约时长。

    A7/B1：活性与上限判定只读单调时钟；``renewal_count`` 不再参与终止判定，
    单 claim 寿命只由 ``total_lease_deadline`` 决定。
    """
    if claim.execution_state != "running" or claim.last_heartbeat_mono is None:
        return False
    if mono_now - claim.last_heartbeat_mono > ROUND_HEARTBEAT_TIMEOUT_SECONDS:
        return False
    # 只保留总租约截止期这一个寿命上限，杜绝假活无限空转续期
    deadline_mono = claim.total_lease_deadline_mono
    if deadline_mono is not None and mono_now >= deadline_mono:
        claim.preempted_reason = "total_lease_deadline_exceeded"
        return False

    if claim.process_pid is not None:
        try:
            from . import task_scheduler
            if not task_scheduler.ZombieDetector.is_pid_alive(claim.process_pid):
                return False
        except Exception:
            return False
    previous_expires_at = claim.lease_expires_at
    renewal_seconds = max(1.0, claim.lease_seconds)
    next_expires_at = now + renewal_seconds
    next_expires_mono = mono_now + renewal_seconds
    if claim.total_lease_deadline is not None:
        next_expires_at = min(next_expires_at, claim.total_lease_deadline)
    if deadline_mono is not None:
        next_expires_mono = min(next_expires_mono, deadline_mono)
    if next_expires_mono <= mono_now:
        claim.preempted_reason = "total_lease_deadline_exceeded"
        return False
    claim.lease_expires_at = max(claim.lease_expires_at, next_expires_at)
    claim.lease_expires_mono = max(
        claim.lease_expires_mono or 0.0, next_expires_mono,
    )
    claim.renewal_count += 1
    claim.preempted_reason = None
    append_event(
        state,
        event_type="worker_lease_renewed_after_timeout",
        now=now,
        worker_id=claim.worker_id,
        lease_id=claim.lease_id,
        execution_id=claim.execution_id,
        previous_lease_expires_at=previous_expires_at,
        lease_expires_at=claim.lease_expires_at,
        reason=f"fresh_heartbeat_renewal_{claim.renewal_count}",
    )
    return True


def _serial_budget_is_legal(state: RunState) -> bool:
    """Recognize the bounded serial budget used by a single-task host."""
    budget = getattr(state, "budget", None)
    reason = getattr(budget, "serial_reason", None)
    return (
        getattr(budget, "require_parallel", True) is False
        and isinstance(reason, str)
        and bool(reason.strip())
    )


_UNIT_PREFIX_RE = re.compile(r"^(?:内部|业务):\d+:")


#: 租约自适应下限：与 ``ZombieDetector`` 的心跳断流窗口（450s）对齐。
#: 低于它没有意义——回收判定本来就以该窗口为最快触发点。
ADAPTIVE_LEASE_FLOOR_SECONDS = 450.0
#: 无历史观测时的默认租约（保持历史行为，避免首轮突变）。
DEFAULT_LEASE_SECONDS = 1800.0


def adaptive_lease_seconds(state: RunState) -> float:
    """按**观测到的执行耗时**推导租约长度，替代写死的 1800s。

    观测源：``SchedulingMetrics``（``total_execution_seconds`` / ``max_execution_seconds``，
    由 checkpoint 侧持续记账）。规则：``3 × max(平均耗时, 最长耗时)``，再夹到
    ``[ADAPTIVE_LEASE_FLOOR_SECONDS, MAX_LEASE_SECONDS]``。

    为什么是「3×」与「下界 450」：租约是 worker 对自身执行时长的申报；不超过 3 倍
    实测峰值就没有容错余量，而低于心跳断流窗口会让「正在干活但没打心跳」的 worker
    被误杀（实测宿主的 Task 子代理常规就不打心跳）。

    观测不足（已完成 < 2）时返回默认值——自适应只在有事实时生效，不猜。
    """

    if not state or state.scheduling_metrics is None:
        return DEFAULT_LEASE_SECONDS
    metrics = state.scheduling_metrics
    completed = int(getattr(metrics, "total_completed", 0) or 0)
    executed = float(getattr(metrics, "total_execution_seconds", 0.0) or 0.0)
    if completed < 2 or executed <= 0:
        return DEFAULT_LEASE_SECONDS
    average = executed / completed
    peak = max(average, float(getattr(metrics, "max_execution_seconds", 0.0) or 0.0))
    return max(ADAPTIVE_LEASE_FLOOR_SECONDS, min(MAX_LEASE_SECONDS, 3.0 * peak))


def _resource_domains(state: RunState) -> int:
    """可领取单元能形成的**并发资源域**数量（同文件单元共享一个域）。

    claim 层给每个单元派生 ``file:<path>`` 互斥键，同一源文件的两个单元因此
    无法同时激活。若全部待领取单元都落在同一个文件里，并发在物理上不可行，
    「要求 peak>=2」就成了不可满足的门禁——实测 2 个 symbol 单元同属
    ``probe/app.py`` 时，parallel_gate 必然残留 ``parallel_peak_below_two``，
    整轮被判 FAILED（正确行为应是按串行完成）。
    """

    domains: set[str] = set()
    for unit in sorted(eligible_units(state)):
        status = (state.coverage or {}).get(unit)
        if status in TERMINAL_COVERED:
            continue
        # 单元 ID 形如 ``内部:1:probe/app.py::read_app``：先剥掉 ``阶段:N:`` 前缀，
        # 再去 ``::symbol``，剩下的才是 claim 层派生 ``file:`` 互斥键的那个文件路径。
        scope = _UNIT_PREFIX_RE.sub("", unit)
        derived = scope.split("::", 1)[0] if "::" in scope else scope
        domains.add(derived)
    return len(domains)


def parallel_required(state: RunState) -> bool:
    if getattr(state, "life_kind", "mine") == "fix":
        return False
    if _serial_budget_is_legal(state):
        return False
    if _resource_domains(state) < 2:
        # 并发在物理上不可行（单元共享同一资源域）：不把 peak>=2 当门禁。
        return False
    return (
        state.budget.require_parallel
        and len(eligible_units(state)) >= 2
    )


# ---------------------------------------------------------------------------
# 恢复会话结算语义（2026-11-17 用户裁决，三维度；语义总纲见模块 docstring）。
#
# 门禁侧只做两件事：维度 1 把「单元已 100% 沿链继承覆盖」的 expired 重试债
# 标记 superseded_by_inheritance 并移出 expired 集合（不重派）；维度 2 在
# 三条件全真时豁免 require_parallel 的并发证明债并落
# ``parallel_gate_inherited_closeout`` 留痕。fail-closed 红线：
#   * 豁免三条件缺一即拒绝；
#   * 伪造继承覆盖（清单行无 inherited_from 备注 / 沿链溯源失败）不得触发豁免；
#   * 探针/清单/证据任何基础设施故障 → 一律维持原门禁（legacy 行为），
#     只在快照里留诊断字段，绝不借故障放宽。
#
# 定位注记：``parallel_gate_result`` 是无 root 的纯函数，而 manifest/证据文件
# 与 supervisor.json 同目录（life state 目录）。Supervisor 每次查询/变更都会
# 先经 :func:`refresh`（携带 workspace_root），这里用**有界**的 run_id→root
# 提示表把 root 带给门禁（进程内、键为 run_id、容量 32 FIFO 覆盖）。定位
# 失败/布局不符 → 返回 None → 门禁维持 legacy 行为，绝不猜测路径。
# ---------------------------------------------------------------------------
_GATE_WORKSPACE_ROOT_HINTS: dict[str, str] = {}
_GATE_WORKSPACE_ROOT_HINTS_MAX = 32

#: 项目布局 run_id 形态（``lifecycle-<polling_id>-<lifecycle_id>``）。
_LIFECYCLE_RUN_ID_RE = re.compile(r"^lifecycle-(polling-\d+)-(lifecycle-\d+)$")

#: 维度 2 豁免只针对 require_parallel 的并发证明债；串行合法性、active
#: worker、expired 重试债（维度 1 管辖）不在豁免范围。
_RECOVERY_PARALLEL_PROOF_REASONS = frozenset({
    "parallel_peak_below_two",
    "parallel_overlap_missing",
    "parallel_workers_missing_checkpoint",
    "bookkeeping_only_no_real_parallel",
})

_EXPIRE_SUPERSEDE_EVENT = "expired_claim_superseded_by_inheritance"
_CLOSEOUT_EXEMPT_EVENT = "parallel_gate_inherited_closeout"


def _remember_gate_workspace_root(run_id: str, workspace_root: object) -> None:
    """记录 run_id → workspace_root 提示（有界 FIFO；仅供门禁定位 life 目录）。"""
    if not run_id or workspace_root is None:
        return
    try:
        resolved = str(Path(workspace_root).resolve())
    except (OSError, ValueError, RuntimeError):
        return
    _GATE_WORKSPACE_ROOT_HINTS.pop(str(run_id), None)
    _GATE_WORKSPACE_ROOT_HINTS[str(run_id)] = resolved
    while len(_GATE_WORKSPACE_ROOT_HINTS) > _GATE_WORKSPACE_ROOT_HINTS_MAX:
        oldest = next(iter(_GATE_WORKSPACE_ROOT_HINTS))
        _GATE_WORKSPACE_ROOT_HINTS.pop(oldest, None)


def _recovery_closeout_probe(state: RunState) -> dict[str, str] | None:
    """从 run_id + workspace_root 提示定位 life state 目录；失败返回 None。

    只做路径解析与存在性检查（廉价 stat），不做任何清单解析——解析由
    coverage_discovery 的只读 helper 在确有 expired 债/并行要求时才执行。
    """
    run_id = str(getattr(state, "run_id", "") or "")
    match = _LIFECYCLE_RUN_ID_RE.match(run_id)
    if match is None:
        return None
    root = _GATE_WORKSPACE_ROOT_HINTS.get(run_id)
    if not root:
        return None
    try:
        import core.bugx_paths as bugx_paths

        state_dir = (
            bugx_paths.bugx_root(Path(root)) / "pollings"
            / match.group(1) / "lifecycles" / match.group(2) / "state"
        )
    except Exception:  # noqa: BLE001 - 定位失败一律维持 legacy 门禁行为
        return None
    if not state_dir.is_dir():
        return None
    return {
        "project_root": root,
        "polling_id": match.group(1),
        "lifecycle_id": match.group(2),
        "manifest_path": str(state_dir / "module-coverage.md"),
        "evidence_path": str(state_dir / "coverage-evidence.json"),
    }


def _owner_cancelled_chain_proof(state: RunState) -> tuple[bool, dict[str, object]]:
    """条件①：Supervisor 权威链在场 owner_cancelled 死亡证明（post 同口径）。

    与 ``post_phase._supervisor_owner_cancelled_proof`` 第 3 步同源：
    ``operation=lifecycle`` / ``gate=owner_cancelled`` 的 lifecycle_blocked
    事件。链身份绑定（lifecycle/run/session 代际）由 post 的接手门独立
    fail-closed 把关，这里只认本 run 自己链上的死亡证明。
    """
    last: dict[str, object] | None = None
    for event in getattr(state, "history", []) or []:
        if not isinstance(event, Mapping):
            continue
        if (
            event.get("type") in {"lifecycle_blocked", "lifecycle_blocked_throttled"}
            and event.get("operation") == "lifecycle"
            and event.get("gate") == "owner_cancelled"
        ):
            seq = event.get("seq")
            order = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
            last_seq = (
                last.get("seq") if isinstance(last, dict) else None
            )
            last_order = (
                last_seq if isinstance(last_seq, int) and not isinstance(last_seq, bool) else -1
            )
            if order >= last_order:
                last = event
    if last is None:
        return False, {
            "present": False,
            "reason": "no owner_cancelled death proof in supervisor chain",
        }
    return True, {
        "present": True,
        "last_seq": last.get("seq"),
        "last_at": last.get("at"),
        "event_type": last.get("type"),
    }


def _findings_increment_since(
    state: RunState, anchor_at: float | None,
) -> tuple[bool, dict[str, object]]:
    """条件③：owner 死亡锚点之后无新增 findings（found_total 增量为 0）。

    会话归属口径：owner_cancelled 证明事件之后落账的 finding 只能来自
    接手会话的新增挖掘（死亡进程不可能再产 finding），因此以「最后一条
    owner_cancelled 的 at」为会话分界锚点。fail-closed：finding 无
    ``origin_receipt.sealed_at``（无法证明早于死亡）视为不可归属，同样
    使条件③不成立——豁免宁可不做，不放行任何可能的新增挖掘。
    """
    findings = getattr(state, "findings", []) or []
    new_since_anchor = 0
    unattributable = 0
    new_worker_ids: set[str] = set()
    if anchor_at is None:
        return False, {
            "anchor_at": None,
            "total_findings": len(findings),
            "new_since_anchor": 0,
            "unattributable": len(findings),
            "new_worker_ids": [],
            "reason": "no owner_cancelled anchor (condition 1 failed)",
        }
    for item in findings:
        receipt = item.get("origin_receipt") if isinstance(item, Mapping) else None
        sealed_at = (
            receipt.get("sealed_at") if isinstance(receipt, Mapping) else None
        )
        if isinstance(sealed_at, bool) or not isinstance(sealed_at, (int, float)):
            unattributable += 1
            continue
        if float(sealed_at) > float(anchor_at):
            new_since_anchor += 1
            worker_id = receipt.get("worker_id") if isinstance(receipt, Mapping) else None
            if worker_id:
                new_worker_ids.add(str(worker_id))
    return (
        new_since_anchor == 0 and unattributable == 0,
        {
            "anchor_at": float(anchor_at),
            "total_findings": len(findings),
            "new_since_anchor": new_since_anchor,
            "unattributable": unattributable,
            "new_worker_ids": sorted(new_worker_ids),
        },
    )


def _has_gate_event(state: RunState, event_type: str, worker_id: str | None = None) -> bool:
    """幂等守卫：同类留痕事件在本 round 已在场则不重复落账。

    history 有界裁剪可能移除旧事件，重放只产生语义相同的重复留痕，不影响
    豁免/标记结论（结论按清单状态确定性重算，不依赖事件在场）。
    """
    for event in getattr(state, "history", []) or []:
        if not isinstance(event, Mapping) or event.get("type") != event_type:
            continue
        if event.get("round_no") != state.round_no:
            continue
        if worker_id is not None and event.get("worker_id") != worker_id:
            continue
        return True
    return False


def _supersede_expired_claims_by_inheritance(
    state: RunState,
    expired_claims: list[WorkerClaim],
    probe: dict[str, str],
    now: float,
) -> tuple[list[WorkerClaim], dict[str, object]]:
    """维度 1：单元已 100% 沿链继承覆盖的 expired claim 移出重试债。

    判定粒度是 claim（重派以 claim 任务包为单位）：claim 的**全部**单元都
    沿链继承覆盖才标记 superseded_by_inheritance；任一单元未覆盖/伪造/
    溯源失败 → 整包保留在 expired 集合（进入重派路径）。标记 = hash-chain
    事件（幂等守卫：同 round 同 worker 不重复落账）。

    合账语义（R-1/R-6）：verdict 由 coverage_discovery 按「本地最新记录 >
    沿链继承」优先级解析并携带 ``backing`` 归属（local_round/local_historical/
    local_overturns/inherited/unbacked）；本函数决策逻辑不变——只有
    ``backing == "inherited"``（沿链继承）触发豁免，本地证据覆盖不触发
    （OQ-1 字面 fail-closed）；``backing``/``local_record`` 随 verdict 自然
    进入 supersede 事件快照（F-4：快照更富，schema 不变）。
    """
    scopes: list[str] = []
    for claim in expired_claims:
        for unit in claim.unit_ids:
            scope = _UNIT_PREFIX_RE.sub("", str(unit)).strip()
            if scope:
                scopes.append(scope)
    try:
        from core.coverage.coverage_discovery import inherited_unit_coverage_verdicts

        verdicts = inherited_unit_coverage_verdicts(
            Path(probe["project_root"]), Path(probe["manifest_path"]), scopes,
            evidence_path=Path(probe["evidence_path"]),
            round_started_epoch=float(
                getattr(state, "round_started_at", 0.0) or 0.0
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 探针故障 fail-closed：维持原债
        return expired_claims, {
            "available": False,
            "reason": f"inheritance verdict unavailable: {type(exc).__name__}: {exc}"[:200],
        }
    kept: list[WorkerClaim] = []
    superseded: dict[str, dict[str, object]] = {}
    for claim in expired_claims:
        unit_verdicts: list[dict[str, object]] = []
        fully_inherited = bool(claim.unit_ids)
        for unit in claim.unit_ids:
            scope = _UNIT_PREFIX_RE.sub("", str(unit)).strip()
            verdict = verdicts.get(scope) if scope else None
            if verdict is None:
                verdict = {
                    "inherited": False, "covered": False, "backing": "unbacked",
                    "reference": None, "chain": "",
                    "reason": f"unit scope 不可解析或不在请求集: {scope or unit}",
                }
            unit_verdicts.append({"unit": str(unit), **verdict})
            if not verdict.get("inherited"):
                fully_inherited = False
        if fully_inherited:
            superseded[claim.worker_id] = {
                "worker_id": claim.worker_id,
                "unit_ids": list(claim.unit_ids),
                "attempt": claim.attempt,
                "lease_id": claim.lease_id,
                "verdicts": unit_verdicts,
            }
        else:
            kept.append(claim)
    for worker_id, snapshot in superseded.items():
        if not _has_gate_event(state, _EXPIRE_SUPERSEDE_EVENT, worker_id):
            append_event(
                state, _EXPIRE_SUPERSEDE_EVENT, now,
                round_no=state.round_no,
                **snapshot,
            )
    return kept, {"available": True, "claims": superseded}


def _recovery_closeout_exemption(
    state: RunState, now: float, probe: dict[str, str],
) -> dict[str, object]:
    """维度 2 三条件判定（fail-closed，逐条件快照供留痕事件与门禁快照）。"""
    cancelled, cancelled_snapshot = _owner_cancelled_chain_proof(state)
    anchor = (
        cancelled_snapshot.get("last_at") if cancelled else None
    )
    anchor_at = anchor if isinstance(anchor, (int, float)) and not isinstance(anchor, bool) else None
    findings_ok, findings_snapshot = _findings_increment_since(state, anchor_at)
    try:
        from core.coverage.coverage_discovery import recovery_closeout_coverage_census

        census = recovery_closeout_coverage_census(
            Path(probe["project_root"]),
            Path(probe["manifest_path"]),
            Path(probe["evidence_path"]),
            float(getattr(state, "round_started_at", 0.0) or 0.0),
        )
    except Exception as exc:  # noqa: BLE001 - 基础设施故障 fail-closed
        census = {
            "available": False,
            "covered_all_inherited_or_historical": False,
            "no_new_evidence_records_this_round": False,
            "reason": f"{type(exc).__name__}: {exc}"[:200],
        }
    # 条件②为 OQ-2 加强口径：本轮零新增证据记录（含 failed/error）。
    # 纯 failed 的重派轮不再误获豁免——任何新证据都证明存在真实挖掘/重派。
    census_ok = bool(
        census.get("covered_all_inherited_or_historical")
        and census.get("no_new_evidence_records_this_round")
    )
    eligible = bool(cancelled and census_ok and findings_ok)
    return {
        "eligible": eligible,
        "checked_at": now,
        "conditions": {
            "owner_cancelled_proof": cancelled_snapshot,
            "coverage_census": census,
            "findings_increment": findings_snapshot,
        },
    }


def parallel_gate_result(
    state: RunState,
    *,
    now: float | None = None,
) -> dict[str, object]:
    # An expired claim is a retry debt, not a permanent round failure.
    # Once a later attempt for the same unit is active or submitted, the
    # old expired claim remains in history but must no longer block the
    # round forever. Looking at the latest attempt per unit also handles
    # a second expiry correctly: only that newest expiry remains a gate.
    # DOC-231 A2/D04: 先隔离代际，再在当前代内选最新 attempt，防止旧代的高 attempt 遮蔽当前代的过期债务。
    # 跨代 expired claim 已无法被接手，语义裁定：scheduler_epoch 是调度代际，上一代的过期债由新一代重新派发，
    # 不再作为本代的门禁理由。
    state_epoch = getattr(state, "scheduler_epoch", None)
    latest_by_unit: dict[str, WorkerClaim] = {}
    for claim in state.claims.values():
        if claim.round_no != state.round_no:
            continue
        claim_epoch = getattr(claim, "scheduler_epoch", None)
        if state_epoch is not None and claim_epoch not in (None, state_epoch):
            # 排除非本代的历史 claim，防止旧代的高 attempt 遮蔽当前代债务
            continue
        for unit_id in claim.unit_ids:
            previous = latest_by_unit.get(unit_id)
            if previous is None or (
                claim.attempt, claim.claimed_at
            ) > (previous.attempt, previous.claimed_at):
                latest_by_unit[unit_id] = claim

    # 恢复结算维度 1（继承豁免）：在把 expired 报成重试债之前，先检查每个
    # expired claim 的单元是否已 100% 沿链继承覆盖（manifest 行带
    # inherited_from 备注 + 溯源可达 passed）。全继承的 claim 标记
    # superseded_by_inheritance（hash-chain 留痕）并移出 expired 集合，不再
    # 重派；任一单元未覆盖/伪造/溯源失败 → 整包保留（进入重派路径）。
    # 探针不可用（非项目布局 run_id / root 提示缺失）→ 维持 legacy 行为。
    expired_by_worker: dict[str, WorkerClaim] = {}
    for claim in latest_by_unit.values():
        if claim.state == "expired":
            expired_by_worker.setdefault(claim.worker_id, claim)
    closeout_probe = _recovery_closeout_probe(state)
    closeout_event_now = float(now) if now is not None else float(time.time())
    superseded_block: dict[str, object] = {"available": False}
    if closeout_probe is not None and expired_by_worker:
        kept_claims, superseded_block = _supersede_expired_claims_by_inheritance(
            state, list(expired_by_worker.values()), closeout_probe, closeout_event_now,
        )
        expired_by_worker = {claim.worker_id: claim for claim in kept_claims}
    expired = sorted(expired_by_worker)
    superseded_workers = sorted(
        claim.get("worker_id")
        for claim in (
            superseded_block.get("claims", {}).values()
            if isinstance(superseded_block.get("claims"), dict) else []
        )
        if isinstance(claim, Mapping) and claim.get("worker_id")
    )
    active = sorted(state.active_claims)
    running = sorted(
        claim.worker_id for claim in state.claims.values()
        if claim.active and claim.execution_state == "running"
    )
    submitted = sorted(
        claim.worker_id for claim in state.claims.values()
        if claim.round_no == state.round_no
        and claim.state == "submitted"
        and claim.submitted_at is not None
    )
    # Do not trust the mutable summary counters alone.  A real parallel proof
    # needs two distinct workers that both completed a non-empty checkpoint,
    # plus overlapping persisted execution windows.
    # 第四批修复（假并发门禁）：执行窗口与 checkpoint 进度只统计真实 worker
    # （is_authentic_worker_claim 三选二）。judge 记账式 claim 的重叠窗口
    # 单独留档——若"仅记账 claim 达标"必须如实 FAIL 并注明，绝不放行。
    intervals: list[tuple[float, float]] = []
    bookkeeping_intervals: list[tuple[float, float]] = []
    bookkeeping_workers: set[str] = set()
    observed_peak = sum(
        1
        for claim in state.claims.values()
        if claim.round_no == state.round_no
        and claim.active
        and claim.execution_state == "running"
        and is_authentic_worker_claim(claim)
    )
    progress_workers: list[str] = []
    for claim in state.claims.values():
        if claim.round_no != state.round_no or claim.execution_started_at is None:
            continue
        authentic = is_authentic_worker_claim(claim)
        if claim.state == "submitted" and claim.submitted_at is not None:
            end = claim.execution_finished_at or claim.submitted_at
            if end > claim.execution_started_at:
                (intervals if authentic else bookkeeping_intervals).append(
                    (claim.execution_started_at, end)
                )
                if not authentic:
                    bookkeeping_workers.add(claim.worker_id)
            if claim.last_checkpoint_digest and authentic:
                progress_workers.append(claim.worker_id)
        elif claim.active and claim.execution_state == "running":
            # A lease expiry is a promise about the future, not evidence that
            # the worker actually ran until that point.  Use the observation
            # boundary after refresh when available; direct callers without a
            # clock only get the last persisted heartbeat boundary.
            end = (
                min(now, claim.lease_expires_at)
                if now is not None
                else (claim.last_heartbeat_at or claim.execution_started_at)
            )
            if end > claim.execution_started_at:
                (intervals if authentic else bookkeeping_intervals).append(
                    (claim.execution_started_at, end)
                )
                if not authentic:
                    bookkeeping_workers.add(claim.worker_id)
        elif claim.execution_finished_at is not None:
            (intervals if authentic else bookkeeping_intervals).append(
                (claim.execution_started_at, claim.execution_finished_at)
            )
            if not authentic:
                bookkeeping_workers.add(claim.worker_id)
    observed_overlap = 0.0
    points: dict[float, int] = {}
    for start, end in intervals:
        if end <= start:
            continue
        points[start] = points.get(start, 0) + 1
        points[end] = points.get(end, 0) - 1
    current = 0
    ordered_points = sorted(points)
    for index, point in enumerate(ordered_points):
        current += points[point]
        observed_peak = max(observed_peak, current)
        if index + 1 < len(ordered_points) and current >= 2:
            observed_overlap += ordered_points[index + 1] - point
    progress_workers = sorted(set(progress_workers))
    # 记账式 claim 的假并发峰值（仅用于如实标注 FAIL，不构成证据）。
    bookkeeping_peak = 0
    bk_points: dict[float, int] = {}
    for start, end in bookkeeping_intervals:
        if end <= start:
            continue
        bk_points[start] = bk_points.get(start, 0) + 1
        bk_points[end] = bk_points.get(end, 0) - 1
    bk_current = 0
    for point in sorted(bk_points):
        bk_current += bk_points[point]
        bookkeeping_peak = max(bookkeeping_peak, bk_current)
    required = parallel_required(state)
    reasons: list[str] = []
    if (
        getattr(state.budget, "require_parallel", True) is False
        and not _serial_budget_is_legal(state)
    ):
        reasons.append("serial_budget_invalid")
    if active:
        reasons.append("active_workers_remaining")
    if expired:
        reasons.append("expired_workers_require_retry")
    if required and observed_peak < 2:
        reasons.append("parallel_peak_below_two")
    if required and observed_peak >= 2 and observed_overlap <= 0:
        reasons.append("parallel_overlap_missing")
    if required and observed_peak >= 2 and len(progress_workers) < 2:
        reasons.append("parallel_workers_missing_checkpoint")
    if required and observed_peak < 2 and bookkeeping_peak >= 2:
        # 第四批修复（假并发门禁）：被真实性过滤后不足阈值，但记账式 claim
        # 的重叠窗口本可"达标"——如实 FAIL 并注明仅记账 claim、无真实并发，
        # 禁止假并发借记账窗口通过门禁（fail-closed 诚实化）。
        reasons.append("bookkeeping_only_no_real_parallel")
    # 恢复结算维度 2（恢复场景豁免）：三条件全真（owner_cancelled 死亡证明
    # 在权威链 + 本 Life covered 100% 继承/历史证据且本 round 零新增证据
    # 记录（含 failed/error，OQ-2）+ owner 死亡锚点后零新增 findings）时，
    # 豁免 require_parallel 的
    # 并发证明债并落 ``parallel_gate_inherited_closeout`` 留痕（含三条件
    # 逐项快照）。fail-closed：任一条件不成立/探针不可用 → 并行证明债原样
    # 保留。串行合法性、active worker、expired 重试债（维度 1 管辖）不在
    # 豁免范围；非 --final 收口由 post 侧拒绝消费该豁免（D5）。
    closeout_exemption: dict[str, object] | None = None
    if required and closeout_probe is not None:
        closeout_exemption = _recovery_closeout_exemption(
            state, closeout_event_now, closeout_probe,
        )
        suppressed = (
            [r for r in reasons if r in _RECOVERY_PARALLEL_PROOF_REASONS]
            if closeout_exemption.get("eligible") else []
        )
        closeout_exemption["suppressed_reasons"] = suppressed
        if suppressed:
            reasons = [r for r in reasons if r not in _RECOVERY_PARALLEL_PROOF_REASONS]
            if not _has_gate_event(state, _CLOSEOUT_EXEMPT_EVENT):
                append_event(
                    state, _CLOSEOUT_EXEMPT_EVENT, closeout_event_now,
                    round_no=state.round_no,
                    conditions=closeout_exemption.get("conditions"),
                    suppressed_reasons=list(suppressed),
                )
    return {
        "round": state.round_no,
        "required": required,
        "satisfied": not reasons,
        "max_active_workers": observed_peak,
        "overlap_seconds": round(observed_overlap, 3),
        "active_workers": active,
        "running_workers": running,
        "submitted_workers": submitted,
        "progress_workers": progress_workers,
        "expired_workers": sorted(expired),
        # 恢复结算语义：维度 1 superseded 标记与维度 2 豁免快照（消费方：
        # post 的 D5 守卫与结算留痕；无恢复语义时恒为 []/None）。
        "expired_workers_superseded_by_inheritance": superseded_workers,
        "recovery_closeout_exemption": closeout_exemption,
        "serial_reason": state.budget.serial_reason,
        "reasons": reasons,
        # 第四批修复：并发证据的真实性口径留档（取证与回归用）。
        "bookkeeping_claims_filtered": len(bookkeeping_workers),
        "bookkeeping_workers": sorted(bookkeeping_workers),
        "bookkeeping_only_peak": bookkeeping_peak,
    }


def target_gate_result(workspace_root: Path, state: RunState) -> dict[str, object]:
    """Return a read-only target-drift marker for status/gate consumers."""
    expected = state.target_fingerprint
    if not expected:
        return {"target_drift": False}
    actual = workspace_fingerprint(workspace_root)
    if actual == expected:
        return {"target_drift": False}
    return {
        "target_drift": True,
        "expected_target_fingerprint": expected,
        "actual_target_fingerprint": actual,
    }


def close_parallel_round(state: RunState, now: float) -> dict[str, object]:
    reconcile_parallel(state, now)
    result = parallel_gate_result(state, now=now)
    state.parallel_history = [
        *state.parallel_history,
        dict(result),
    ][-100:]
    append_event(
        state, "parallel_round_closed", now,
        **{key: value for key, value in result.items() if key != "satisfied"},
        satisfied=result["satisfied"],
    )
    return result


def observe_clock(state: RunState, now: float) -> float:
    """墙钟回拨防护：返回不早于已观测高水位的有效 now (D3)。

    预算的墙钟锚（``started_at`` / ``round_started_at``）是持久化字段，
    系统时钟回拨会让 ``now - 锚点`` 变小，等于凭空把已耗掉的墙钟预算
    还回来。这里把有效 now 钉在「本 run 曾观测到的最晚时刻」
    （``clock_high_water``，随快照持久化），使已计时长的记账只增不减。

    下界还包含全部已持久化的墙钟锚：旧快照没有水位字段（升级首读），
    此时至少保证 ``elapsed >= 0``，回拨不可能把计时变成负数再重新开始。

    这是下界而非权威时钟：``now`` 前跳时照常推进水位（前跳导致预算
    提前耗尽属于 fail-closed 方向，由租约的 mono 权威兜住 Worker 误杀）。
    """
    effective = float(now)
    high_water = float(getattr(state, "clock_high_water", 0.0) or 0.0)
    for name in ("started_at", "budget_started_at", "round_started_at"):
        anchor = float(getattr(state, name, 0.0) or 0.0)
        if anchor > high_water:
            high_water = anchor
    if effective < high_water:
        effective = high_water
    # 水位只增不减；被锚点抬高的下界同样落盘，供跨进程复核。
    state.clock_high_water = effective
    return effective


def deadline_reasons(state: RunState, now: float) -> list[str]:
    now = observe_clock(state, now)
    reasons: list[str] = []
    # max_wall_seconds is the run-wide hard cap.  budget_started_at advances
    # for each recoverable epoch, so using it here would let reset_budget
    # restart the global wall clock.
    if now - state.started_at >= state.budget.max_wall_seconds:
        reasons.append("max_wall_seconds")
    if now - state.round_started_at >= state.budget.max_round_wall_seconds:
        # DOC-196: 在标记超时前，尝试双重进展活性续期
        if not _renew_round_budget_if_live(state, now):
            reasons.append("max_round_wall_seconds")
    return reasons


def _round_has_live_activity(state: RunState, now: float, now_mono: float | None = None) -> bool:
    """Return whether the current Round has evidence it is progressing (DOC-196 / DOC-242).

    A fresh running-worker heartbeat is sufficient liveness evidence.  With no
    active claims, a submitted claim means the Round is ready for the host to
    advance, not hung.  Expired/claimed-only workers and empty rounds do not
    renew the deadline.
    """
    if now_mono is None:
        now_mono = time.monotonic()
    active = list(state.active_claims.values())
    if not active:
        return any(
            claim.round_no == state.round_no and claim.state == "submitted"
            for claim in state.claims.values()
        )
    for claim in active:
        if claim.round_no != state.round_no or claim.execution_state != "running":
            continue
        # DOC-203 D3 / L2-Timeout / DOC-242:
        # 若 worker 声明了长调用且租约未到期，长调用窗口在 LONG_CALL_MAX_SILENCE_SECONDS 内视为活跃；
        # 若静默超限，必须具有非零 CPU 活跃度，否则判定死锁不再续期。
        in_long_call = (
            getattr(claim, "long_call_count", 0) > 0
            and claim.lease_expires_mono is not None
            and now_mono < claim.lease_expires_mono
        )
        if in_long_call:
            last_comm_mono = getattr(claim, "last_heartbeat_mono", None) or getattr(claim, "claimed_mono", None)
            if last_comm_mono is not None:
                long_silence = now_mono - last_comm_mono
            else:
                last_comm = claim.last_heartbeat_at or claim.claimed_at
                long_silence = now - last_comm if last_comm else 0.0
            if long_silence > LONG_CALL_MAX_SILENCE_SECONDS:
                process_pid = claim.process_pid
                if process_pid is not None:
                    try:
                        from . import task_scheduler
                        if not task_scheduler.ZombieDetector.is_pid_cpu_active(process_pid):
                            continue
                    except Exception:
                        pass
        else:
            # A7：心跳新鲜度同样只读单调时钟，墙钟跳变不改变活性判定。
            last_heartbeat_mono = claim.last_heartbeat_mono
            is_fresh_heartbeat = (
                last_heartbeat_mono is not None
                and (now_mono - last_heartbeat_mono <= ROUND_HEARTBEAT_TIMEOUT_SECONDS)
            )
            if not is_fresh_heartbeat:
                continue
        process_pid = claim.process_pid
        if process_pid is not None:
            try:
                from . import task_scheduler
                if not task_scheduler.ZombieDetector.is_pid_alive(process_pid):
                    continue
            except Exception:
                continue
        # B1：只要总截止期未到即视为有效进展（续租次数不参与终止判定）。
        deadline_mono = claim.total_lease_deadline_mono
        if deadline_mono is None or now_mono < deadline_mono:
            return True
    return False


def _renew_round_budget_if_live(
    state: RunState, now: float, *, now_mono: float | None = None,
) -> bool:
    """Roll only the single-Round wall clock when progress is still live (DOC-196)."""
    now = observe_clock(state, now)
    if now - state.started_at >= state.budget.max_wall_seconds:
        return False
    if now - state.round_started_at < state.budget.max_round_wall_seconds:
        return True
    if not _round_has_live_activity(state, now, now_mono):
        return False
    reasons = set(state.budget_reasons)
    if reasons - {"max_round_wall_seconds"}:
        return False
    previous = state.round_started_at
    state.round_started_at = now
    state.budget_reasons = [
        reason for reason in state.budget_reasons
        if reason != "max_round_wall_seconds"
    ]
    state.budget_exhausted = bool(state.budget_reasons)
    if state.status == Status.BUDGET_EXHAUSTED and not state.budget_exhausted:
        state.status = Status.RUNNING
    append_event(
        state, "budget_round_renewed", now,
        round_no=state.round_no,
        previous_round_started_at=previous,
        next_round_started_at=now,
        reason="live_worker_or_submitted_progress",
    )
    return True


def soft_limit_reasons(state: RunState) -> list[str]:
    reasons: list[str] = []
    if state.budget_tool_calls >= state.budget.max_tool_calls:
        reasons.append("max_tool_calls")
    if state.budget_cost_units >= state.budget.max_cost_units:
        reasons.append("max_cost_units")
    return reasons


def validate_parallel_capacity(budget: RunBudget, unit_count: int) -> None:
    """Reject parallel runs that cannot schedule two eligible units."""
    if budget.require_parallel and unit_count >= 2 and budget.max_concurrency < 2:
        raise InvalidCheckpoint(
            "至少有两个覆盖单元时，max_concurrency 必须 >= 2；"
            "如确需串行，请提供 serial_reason"
        )





def refresh(
    state: RunState,
    now: float,
    workspace_root: Path | None = None,
    now_mono: float | None = None,
) -> None:
    # 恢复结算门禁的 root 提示（有界 FIFO，见 _GATE_WORKSPACE_ROOT_HINTS）：
    # parallel_gate_result 是无 root 纯函数，Supervisor 每次查询/变更都先经
    # 本函数（携带 workspace_root），在这里登记 run_id → root，门禁即可定位
    # life state 目录（manifest/证据文件与 supervisor.json 同目录）。
    _remember_gate_workspace_root(
        str(getattr(state, "run_id", "") or ""), workspace_root,
    )
    if now_mono is None:
        now_mono = time.monotonic()
    # D3：一次 refresh 内所有墙钟写入（事件时间戳、租约展示期、预算锚）
    # 都使用回拨防护后的有效 now，避免记账时间倒退。
    now = observe_clock(state, now)
    for worker_id, claim in list(state.claims.items()):
        if not claim.active:
            continue
        if claim.is_expired(now_mono=now_mono) and _renew_expired_live_claim(
            state, claim, now, now_mono,
        ):
            continue
        # 租约是否已实际失效，必须在任何状态突变**之前**取样：僵尸分支会把 claim
        # 标记成 ``expired``（此后 ``claim.active`` 为假，``mark_claim_expired`` 会被
        # 自身守卫直接吞掉），于是 running 形态的过期记账整条丢失（缺陷 #8）。
        # A7：过期判定只读单调时钟（mono 是唯一权威），墙钟跳变不改变结论。
        lease_lapsed = claim.is_expired(now_mono=now_mono)
        is_zombie = False
        z_reason = ""
        try:
            from . import task_scheduler
            is_zombie, z_reason = task_scheduler.ZombieDetector.check_zombie_claim(claim, now, now_mono=now_mono)
        except Exception as exc:
            # A failed liveness probe is not evidence of health.  Fail closed
            # and reclaim the claim so a broken probe cannot preserve capacity
            # forever.
            is_zombie = True
            z_reason = "zombie_probe_failed"
            append_event(
                state, "worker_zombie_probe_failed", now,
                worker_id=worker_id, lease_id=claim.lease_id,
                execution_id=claim.execution_id,
                error=type(exc).__name__,
            )


        if is_zombie:
            terminated = None
            if z_reason == "heartbeat_timeout" and claim.process_pid is not None:
                refusal = ""
                try:
                    from . import task_scheduler
                    # 归属判定收敛在 terminate_claim：命令行证据 + 启动令牌双重
                    # 校验，任一缺失/不匹配即拒绝发信号（进程组投递另有把关）。
                    terminated, refusal = task_scheduler.ZombieDetector.terminate_claim(
                        claim, workspace_root=workspace_root or getattr(state, "workspace_root", None),
                    )
                except Exception:
                    terminated, refusal = False, "terminate_error"
                z_reason = (
                    f"{z_reason}_killed" if terminated
                    else f"{z_reason}_kill_failed"
                )
                if not terminated:
                    # 拒绝终止必须可观测：Worker 自报的 PID 可能指向无关进程，
                    # 静默拒绝会掩盖「有人试图诱导宿主杀进程」这一事实。
                    append_event(
                        state, "worker_termination_refused", now,
                        worker_id=worker_id, lease_id=claim.lease_id,
                        execution_id=claim.execution_id,
                        process_pid=claim.process_pid,
                        reason=refusal or "unknown",
                    )
            claim.state = "expired"
            claim.execution_state = "expired"
            claim.execution_finished_at = now
            claim.preempted_reason = z_reason
            state.scheduling_metrics.total_preempted += 1
            _record_adaptive_outcome(state, "preempted")
            append_event(
                state, "worker_preempted", now, worker_id=worker_id,
                lease_id=claim.lease_id,
                execution_id=claim.execution_id,
                reason=z_reason,
                process_pid=claim.process_pid,
                process_terminated=terminated,
            )
            if lease_lapsed:
                # 「租约失效」与「被预占」是两个独立事实，必须同时记账（缺陷 #8：
                # running 形态此前只记预占，``total_expired`` 系统性少计）。
                record_expiry_accounting(
                    state, claim, now, include_execution_id=True,
                )
            if claim.attempt >= MAX_WORKER_ATTEMPTS:
                mark_worker_attempts_exhausted(
                    state, now, claim.unit_ids,
                    worker_id=claim.worker_id, attempt=claim.attempt,
                )
        elif claim.is_expired(now_mono=now_mono):
            # LOG-11：过期记账（状态/计数/事件）收敛到 lease_manager 单点。
            mark_claim_expired(state, claim, now, include_execution_id=True)
            _record_adaptive_outcome(state, "expired")
    reconcile_parallel(state, now)
    if _renew_round_budget_if_live(state, now, now_mono=now_mono):
        return
    deadline = deadline_reasons(state, now)
    # D2：INCOMPLETE 是 finish 已收尾终态，不得被预算刷新改写成 BUDGET_EXHAUSTED。
    if deadline and state.status not in {
        Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
    }:
        if not state.budget_exhausted:
            set_budget_exhausted(state, now, deadline)
        else:
            state.budget_reasons = list(dict.fromkeys(
                [*state.budget_reasons, *deadline]
            ))


def apply_status_budget(state: RunState, now: float) -> None:
    """Apply status-query budget gates after the caller refreshes leases."""
    now = observe_clock(state, now)
    if (
        state.status not in {
            Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
            Status.BUDGET_EXHAUSTED,
        }
        and not state.coverage_complete
        and (state.budget_exhausted or soft_limit_reasons(state))
    ):
        set_budget_exhausted(
            state, now, soft_limit_reasons(state),
            event_type="status_budget_exhausted",
        )


def refresh_status_budget(state: RunState, now: float) -> None:
    """Refresh worker leases and apply status-query budget gates once."""
    refresh(state, now)
    apply_status_budget(state, now)


def ensure_live_for_work(state: RunState) -> None:
    # D2：INCOMPLETE 与 COMPLETE/FAILED 同级冻结，claim/next_round 等一切
    # 工作入口必须拒绝（BUDGET_EXHAUSTED 不在内：它是可续耗尽态）。
    if state.status in {Status.COMPLETE, Status.FAILED, Status.INCOMPLETE}:
        raise InvalidTransition(f"run 已结束: {state.status.value}")
    if state.budget_exhausted:
        raise BudgetExhausted(
            "预算已耗尽: " + ", ".join(state.budget_reasons or ["unknown"])
        )


def recalibrate_round_budget(
    state: RunState,
    recommended_seconds: float,
    now: float,
    *,
    reason: str = "adaptive_round_complexity",
) -> float:
    """动态校准当前 Round 的单轮墙钟预算上限 (DOC-150)。

    受全局总预算 max_wall_seconds 约束，校准后立即检查是否满足活性并记录审计事件。
    """
    if recommended_seconds <= 0:
        return state.budget.max_round_wall_seconds
    previous = state.budget.max_round_wall_seconds
    bounded = min(recommended_seconds, state.budget.max_wall_seconds)
    state.budget = dataclasses.replace(state.budget, max_round_wall_seconds=bounded)
    append_event(
        state,
        "round_budget_recalibrated",
        now,
        previous_budget=previous,
        new_budget=bounded,
        reason=reason,
    )
    _renew_round_budget_if_live(state, now)
    return bounded
