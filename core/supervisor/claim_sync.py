#!/usr/bin/env python3
"""Supervisor claim 选择、租约创建与 claim 事件编排边界。

输入归一化、锁和持久化仍由 ``audit_supervisor.Supervisor`` 负责；本模块
只组合已校验的 claim 参数并复用 Supervisor 的状态机窄边界。
"""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any

from .supervisor_models import (
    COVERAGE_PENDING,
    DEFAULT_MAX_RENEWALS,
    GLOBAL_BLOCKED,
    MAX_WORKER_ATTEMPTS,
    UNIT_LEVEL_DEBT,
    BudgetExhausted,
    ClaimRejected,
    Status,
    WorkerClaim,
    worker_artifact_root,
    process_start_token,
    unit_source_path,
    worker_attempts_exhausted,
)

_UNSCOPED_NUMERIC_WORKER_ID = re.compile(r"worker-\d+")

# 终态 claim：只有这些状态允许被 recover_expired 自接手（缺陷 B）。
_TERMINAL_RECOVERABLE_STATES = frozenset({"expired", "released"})


def _readonly_baseline(supervisor, write_access) -> dict[str, object] | None:
    """只读 claim 的源码基线；写 claim / 未声明 一律 None（不产生校验成本）。"""

    if write_access is not False:
        return None
    try:
        from core.governance import readonly_enforcer

        return readonly_enforcer.snapshot_source_state(supervisor.workspace_root)
    except (ImportError, OSError, ValueError, RuntimeError):
        return None


def _derived_file_mutex_keys(units: Any) -> tuple[str, ...]:
    """按单元 ID 派生 ``file:<源文件>`` 互斥键（同文件互斥的引擎侧兜底）。

    只对能解析出源文件路径的单元生成键；无法解析（纯符号/发现 ID）时不造键，
    以免把「没有路径」伪造成互斥资源。
    """
    keys: list[str] = []
    for unit in units or ():
        source = unit_source_path(unit)
        if not source:
            continue
        # 只有**看起来像源文件**的单元才参与互斥：状态占位式 ID（如
        # ``内部:pending``）解析出来的是状态词而非路径，给它造 ``file:`` 键
        # 只会制造假冲突。要求 basename 带扩展名（``foo.py`` / ``a.b.js``）。
        basename = source.rsplit("/", 1)[-1]
        if "." not in basename:
            continue
        key = f"file:{source}"
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def pid_is_alive(pid: object) -> bool:
    """Best-effort liveness probe for a caller-supplied ``process_pid``.

    与 ``supervisor_host._pid_is_alive`` 同源的小函数（POSIX ``os.kill(pid, 0)``
    + Windows ``OpenProcess`` 回退）：进程存在但不可信号（``PermissionError``）
    仍算存活。``pid <= 0`` 恒为不存活 —— ``os.kill(0, 0)`` 会命中整个进程组，
    绝不能用作探活。
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: process exists
            try:
                exit_code = ctypes.c_uint32()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) \
                    and exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # The process exists but this user cannot signal it; that is still a
        # valid liveness observation.
        return True
    except (OSError, ValueError):
        return False
    return True


def _worker_namespace_prefix(state) -> str:
    scope = (
        getattr(state, "life_id", None)
        or getattr(state, "round_id", None)
        or getattr(state, "run_id", None)
        or "run"
    )
    safe_scope = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(scope)).strip("-") or "run"
    return f"worker-{safe_scope}-round-{int(state.round_no)}-"

from core.supervisor.lease_manager import mark_worker_attempts_exhausted

def apply_claim_operation(
    supervisor,
    state,
    now,
    *,
    allow_bundle,
    continue_worker,
    expected_revision,
    lease,
    process_pid,
    requested_resources,
    requested_units,
    supersede_key,
    worker_key,
    write_access=None,
    worker_role=None,
    preferred_affinity=None,
) -> WorkerClaim:
    """``claim_worker`` 的状态变更体（DES-03）。

    原先这是一段 238 行的嵌套闭包 ``operation``，捕获了
    ``claim_worker`` 的 9 个外层名字（含仅关键字参数）。
    闭包形式让这段逻辑无法单独命名、阅读、测试，也使外层方法
    膨胀到 282 行。现提升为具名方法：捕获的名字
    改为**仅关键字参数**传入，行为逐字不变。
    """
    supervisor._check_revision(state, expected_revision)
    supervisor._refresh(state, now)
    supervisor._assert_target_fingerprint(state, now)
    if (
        _UNSCOPED_NUMERIC_WORKER_ID.fullmatch(worker_key)
        and not worker_key.startswith(_worker_namespace_prefix(state))
    ):
        raise ClaimRejected(
            "worker_id 必须包含 lifecycle/round 命名空间；"
            f"请使用 {_worker_namespace_prefix(state)}<index>"
        )
    # 缺陷 A 前移校验：登记 ``process_pid`` 之前先做存活探针。宿主把一次性
    # bash/Task 子代理 shell 的 PID 传进来时，若等到后续存活探针才回收，
    # claim 会直接终态 expired（6/6 worker 全灭根因之一）；必须在入口即拒。
    if process_pid is not None and not pid_is_alive(process_pid):
        raise ClaimRejected(
            f"process_pid {process_pid} 不存活：Task 子代理请省略 process_pid，"
            "以心跳作为活性证据"
        )
    if state.status == Status.BUDGET_EXHAUSTED or state.budget_exhausted:
        supervisor._append_event(
            state, "claim_rejected", now, worker_id=worker_key,
            reason="budget_exhausted",
        )
        raise BudgetExhausted(
            "预算已耗尽: " + ", ".join(state.budget_reasons or ["unknown"])
        )
    supervisor._ensure_live_for_work(state)
    existing_claim = state.claims.get(worker_key)
    continuation = bool(
        continue_worker
        and existing_claim is not None
        and existing_claim.state == "submitted"
        and existing_claim.round_no == state.round_no
    )
    # 缺陷 B（recover_expired）：同一 worker_id 的终态 claim 允许「自接手」
    # （supersede_key == worker_key），一次调用完成 supersede 换约 —— 新 claim
    # 继承原 unit_ids/resource_keys/artifact_root、attempt 严格 +1、租约重新计时。
    # MCP 层由 ``bugx_supervisor_claim`` 的 ``recover_expired=true`` 触发；
    # 非终态与跨身份 supersede 语义不变。
    self_supersede_recovery = (
        supersede_key == worker_key
        and existing_claim is not None
        and existing_claim.state in _TERMINAL_RECOVERABLE_STATES
    )
    if existing_claim is not None and not continuation and not self_supersede_recovery:
        supervisor._append_event(
            state, "claim_rejected", now, worker_id=worker_key,
            reason="worker_already_seen",
        )
        if existing_claim.state != "submitted":
            if existing_claim.state in {"expired", "released"}:
                raise ClaimRejected(
                    f"worker 当前状态为 {existing_claim.state}，不可直接 continue_worker；"
                    f"如需接手该过期任务，请使用新 worker 并指定 --supersede-worker-id {worker_key}，"
                    "或宿主用 recover_expired=true 以同一 worker_id 直接接手"
                )
            raise ClaimRejected(
                f"重复 worker {worker_key} 当前状态为 {existing_claim.state}（仅 submitted 终态允许 continue_worker）"
            )
        raise ClaimRejected(
            f"重复 worker 不允许重用: {worker_key}；"
            "如需由同一 Worker 在完成前序提交后继续领取下一单元，请显式传入 --continue-worker"
        )
    if continuation and supersede_key is not None:
        raise ClaimRejected("continue_worker 不能同时 supersede_worker_id")
    superseded_claim = None
    if supersede_key is not None:
        superseded_claim = state.claims.get(supersede_key)
        if superseded_claim is None:
            raise ClaimRejected(f"待接手 worker 不存在: {supersede_key}")
        if superseded_claim.round_no != state.round_no:
            raise ClaimRejected("只允许在同一 Round 接手过期 worker")
        if superseded_claim.scheduler_epoch != state.scheduler_epoch:
            raise ClaimRejected("只允许接手当前 scheduler epoch 的 worker")
        if superseded_claim.state not in {"expired", "released"}:
            raise ClaimRejected("只有 expired/released worker 才能被接手")
        if requested_units is not None and tuple(requested_units) != superseded_claim.unit_ids:
            raise ClaimRejected("接手 worker 的 unit_ids 必须与原 claim 完全一致")
    active = state.active_claims
    capacity_ok = True
    capacity_probe_failed = False
    if state.budget.adaptive_concurrency and state.budget.require_parallel and state.budget.max_concurrency > 1:
        try:
            from . import task_scheduler
            ctrl = task_scheduler.AdaptiveConcurrencyController(
                min_concurrency=state.budget.min_concurrency,
                max_concurrency=state.budget.max_concurrency_ceiling,
            )
            cap = ctrl.compute_capacity(len(active), state.budget, state)
            capacity_ok = cap > 0
        except Exception:
            capacity_ok = False
            capacity_probe_failed = True
    else:
        capacity_ok = len(active) < state.budget.max_concurrency

    if not capacity_ok:
        supervisor._append_event(
            state, "claim_rejected", now, worker_id=worker_key,
            reason=("concurrency_probe_failed"
                    if capacity_probe_failed else "max_concurrency"),
        )
        if capacity_probe_failed:
            raise ClaimRejected("并发容量探针失败，拒绝 fail-open claim")
        raise ClaimRejected("max_concurrency 已满")
    if state.budget_tool_calls >= state.budget.max_tool_calls:
        supervisor._set_budget_exhausted(
            state, now, ["max_tool_calls"], event_type="claim_rejected",
        )
        raise BudgetExhausted("max_tool_calls 已耗尽")
    if state.budget_cost_units >= state.budget.max_cost_units:
        supervisor._set_budget_exhausted(
            state, now, ["max_cost_units"], event_type="claim_rejected",
        )
        raise BudgetExhausted("max_cost_units 已耗尽")
    if state.round_no > state.budget.max_rounds:
        supervisor._set_budget_exhausted(
            state, now, ["max_rounds"], event_type="claim_rejected",
        )
        raise BudgetExhausted("max_rounds 已耗尽")
    claimed_this_round = supervisor._claimed_units_this_round(state)
    if superseded_claim is not None:
        selected_units = requested_units or superseded_claim.unit_ids
    elif requested_units is None:
        eligible_units = supervisor._eligible_units(state)
        available_units = [
                unit for unit, status in state.coverage.items()
                if unit not in claimed_this_round
                and unit in eligible_units
                and status == COVERAGE_PENDING
                and not worker_attempts_exhausted(state, unit)
            ]
        if not available_units:
            available_units = [
                unit for unit, status in state.coverage.items()
                if unit not in claimed_this_round
                and unit in eligible_units
                and status in UNIT_LEVEL_DEBT
                and not worker_attempts_exhausted(state, unit)
            ]
        if available_units:
            try:
                from . import task_scheduler
                # DOC-140 调度提速：claim 是无人值守路径上最高频的调度
                # 入口（宿主模式一单元一次 MCP claim）。原实现每次新建
                # 调度器 = 全仓建图 + 影响网络 + git diff；改走按
                # (root, HEAD, 清单 mtime) 失效的进程内共享实例。
                scheduler = task_scheduler.shared_scheduler(supervisor.workspace_root)
                ranked = scheduler.rank_units(available_units, state.coverage)
                selected_units = (ranked[0],)
            except Exception:
                selected_units = (sorted(available_units)[0],)
        else:
            selected_units = ()
    else:
        unknown = [unit for unit in requested_units if unit not in state.coverage]
        if unknown:
            raise ClaimRejected(
                "未知 coverage unit: " + ", ".join(sorted(unknown))
            )
        ineligible = sorted(
            set(requested_units) - supervisor._eligible_units(state)
        )
        if ineligible:
            raise ClaimRejected(
                "内部单元未完成前不能领取业务 unit: "
                + ", ".join(ineligible)
            )
        globally_blocked = sorted(
            unit for unit in requested_units
            if state.coverage.get(unit) in GLOBAL_BLOCKED
        )
        if globally_blocked:
            raise ClaimRejected(
                "coverage unit 处于全局阻塞，不能直接派发: "
                + ", ".join(globally_blocked)
            )
        duplicate = sorted(set(requested_units) & claimed_this_round)
        if duplicate:
            occupying_workers = sorted({
                claim.worker_id
                for claim in state.claims.values()
                if claim.round_no == state.round_no
                and claim.state in {"active", "submitted"}
                and set(claim.unit_ids) & set(duplicate)
            })
            raise ClaimRejected(
                "coverage unit 已在当前 Round (round_no="
                + str(state.round_no)
                + ") 领取: "
                + ", ".join(duplicate)
                + " (占用 worker: "
                + ", ".join(occupying_workers)
                + ")"
            )
        selected_units = requested_units
    if state.coverage and not selected_units:
        eligible_debt_units = sorted(
            unit for unit, status in state.coverage.items()
            if unit in supervisor._eligible_units(state)
            and status in UNIT_LEVEL_DEBT
        )
        if eligible_debt_units and all(
            worker_attempts_exhausted(state, unit)
            for unit in eligible_debt_units
        ):
            mark_worker_attempts_exhausted(state, now, eligible_debt_units)
            if state.status not in {
                Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
            }:
                state.status = Status.STALLED
            supervisor._append_event(
                state, "claim_rejected", now, worker_id=worker_key,
                reason="worker_attempts_exhausted",
                unit_ids=eligible_debt_units,
                max_attempts=MAX_WORKER_ATTEMPTS,
            )
            raise ClaimRejected(
                "所有可分配 coverage unit 均已达到 worker 最大尝试次数，已熔断: "
                + ", ".join(eligible_debt_units)
            )
        raise ClaimRejected("没有可分配的 coverage unit")
    historical_bundle = (
        superseded_claim is not None and len(selected_units) <= 3
    )
    if not (allow_bundle or historical_bundle) and len(selected_units) != 1:
        raise ClaimRejected(
            "每个 Worker 只能领取一个 coverage unit；请由调度器拆分任务后再次 claim"
        )
    if len(selected_units) < 1 or len(selected_units) > 5:
        raise ClaimRejected("单次 claim 单元数必须在 1 到 5 之间")
    exhausted_units = [
        unit for unit in selected_units if worker_attempts_exhausted(state, unit)
    ]
    if exhausted_units:
        mark_worker_attempts_exhausted(state, now, exhausted_units)
        supervisor._append_event(
            state, "claim_rejected", now, worker_id=worker_key,
            reason="worker_attempts_exhausted", unit_ids=sorted(exhausted_units),
            max_attempts=MAX_WORKER_ATTEMPTS,
        )
        raise ClaimRejected(
            "coverage unit 已达到 worker 最大尝试次数，已熔断: "
            + ", ".join(sorted(exhausted_units))
        )
    selected_resources = requested_resources or (
        superseded_claim.resource_keys if superseded_claim is not None
        else tuple(selected_units)
    )
    if superseded_claim is not None and requested_resources and tuple(requested_resources) != superseded_claim.resource_keys:
        raise ClaimRejected("接手 worker 的 resource_keys 必须与原 claim 完全一致")
    # 同文件互斥必须由**引擎**强制，不能依赖调用方自觉：历史实现只在派发侧注入
    # ``file:<fp>``，Worker 经 MCP/CLI 自行 claim 时省略 resource_keys 就会让同一
    # 源文件的两个单元同时激活（缺陷 #7，已在协议层物理复现）。这里按单元 ID
    # 派生的源文件路径补 `file:<path>` 互斥键，再与调用方给的键取并集。
    # 读写互斥矩阵（P2）：``write_access is False`` 的 claim 是**只读审计**，
    # 同文件的多个只读 claim 可以并发（读并发安全）；但它与同文件的**写** claim
    # 互斥。反之写 claim 与同文件任何 claim 互斥。未声明（None）按历史强互斥，
    # 绝不因为「没声明」就放开写并发。
    read_only = write_access is False
    own_file_keys = _derived_file_mutex_keys(selected_units)
    merged_resources = tuple(
        dict.fromkeys(
            (*selected_resources, *( () if read_only else own_file_keys ))
        )
    )
    # 老 claim 可能没有 file 键（修复前领取、仍在活跃），因此活跃侧也按单元派生
    # 一次键，避免「只修新 claim、旧 claim 仍可被绕过」的窗口。
    active_keys = set(supervisor._active_resource_keys(state))
    file_writers: set[str] = set()
    for active_claim in state.claims.values():
        if not active_claim.active:
            continue
        keys = _derived_file_mutex_keys(active_claim.unit_ids)
        active_keys.update(keys)
        if getattr(active_claim, "write_access", None) is not False:
            file_writers.update(keys)
    if read_only:
        # 只读只需避开**写者**（同文件其他只读 claim 可共存）。
        conflict = sorted(set(own_file_keys) & file_writers)
    else:
        conflict = sorted(set(merged_resources) & active_keys)
    if conflict:
        supervisor._append_event(
            state, "claim_rejected", now, worker_id=worker_key,
            reason="resource_conflict", resource_keys=conflict,
        )
        raise ClaimRejected(
            "资源组正在被其他 worker 使用: " + ", ".join(conflict)
        )
    attempt = 1 + max(
        (
            claim.attempt
            for claim in state.claims.values()
            if claim.scheduler_epoch == state.scheduler_epoch
            if set(claim.unit_ids) & set(selected_units)
        ),
        default=0,
    )
    if superseded_claim is not None and attempt <= superseded_claim.attempt:
        raise ClaimRejected("接手 worker 的 attempt 必须严格递增")
    if state.status in {Status.CHECKPOINT, Status.NEXT_ROUND, Status.STALLED}:
        state.status = Status.RUNNING
    lease_id = uuid.uuid4().hex
    artifact_dir, artifact_root = worker_artifact_root(
        supervisor.workspace_root, supervisor.state_path, worker_key, lease_id,
    )
    if superseded_claim is not None and superseded_claim.artifact_root:
        artifact_root = superseded_claim.artifact_root
    # A7 之后过期判定只读单调时钟，因此**创建时的锚点也必须来自同一可注入源**：
    # 此前硬编码 ``time.monotonic()``，注入假时钟的用例拿到真实锚点，
    # 过期永不到来（run_fault_matrix「心跳丢失与租约回收」reclaimed=False 的根因）。
    mono_clock = getattr(supervisor, "_mono_clock", None) or time.monotonic
    mono_now = float(mono_clock())
    # 单 claim 寿命上限 = 初始租约 × (DEFAULT_MAX_RENEWALS + 1)；该乘数只决定
    # 绝对围栏宽度，不再是"续租次数上限"（B1）。A7：创建时一次性写入全部
    # 单调时钟锚，避免首次租约期间判定退回墙钟。
    total_lease_deadline = now + float(lease) * (DEFAULT_MAX_RENEWALS + 1)

    # DOC-248: 三维正交领域专精 JIT 规格装配
    target_unit_str = selected_units[0] if selected_units else ""
    target_file = unit_source_path(target_unit_str) if target_unit_str else ""
    if not target_file and target_unit_str:
        target_file = str(target_unit_str)
    symbol_name = ""
    if "#" in target_unit_str:
        symbol_name = target_unit_str.split("#", 1)[1]
    elif "::" in target_unit_str:
        symbol_name = target_unit_str.split("::", 1)[1]

    project_profile = getattr(supervisor, "project_profile", None) or getattr(state, "project_profile", None)
    from core.planning.domain_router import DomainSpecializedRouter

    spec = DomainSpecializedRouter.resolve_worker_spec(
        file_path=target_file,
        symbol_name=symbol_name,
        project_profile=project_profile,
        preferred_affinity=preferred_affinity,
    )
    assigned_worker_role = worker_role or spec["worker_role"]
    assigned_domain_affinity = preferred_affinity or spec["domain_affinity"]
    assigned_model_tier = spec["model_tier"]
    assigned_target_form = spec["target_form"]
    assigned_tech_stack = spec["tech_stack"]
    assigned_fixtures = spec["assigned_fixtures"]
    assigned_active_probes = spec["active_probes"]

    if superseded_claim is not None and not worker_role and not preferred_affinity:
        if getattr(superseded_claim, "worker_role", None) and superseded_claim.worker_role != "generalist":
            assigned_worker_role = superseded_claim.worker_role
            assigned_domain_affinity = superseded_claim.domain_affinity
            assigned_model_tier = superseded_claim.model_tier
            assigned_target_form = superseded_claim.target_form
            assigned_tech_stack = superseded_claim.tech_stack
            assigned_fixtures = superseded_claim.assigned_fixtures
            assigned_active_probes = superseded_claim.active_probes

    claim = WorkerClaim(
        worker_id=worker_key,
        lease_id=lease_id,
        round_no=state.round_no,
        claimed_at=now,
        lease_expires_at=now + float(lease),
        unit_ids=tuple(selected_units),
        resource_keys=merged_resources,
        attempt=attempt,
        scheduler_epoch=state.scheduler_epoch,
        execution_id=f"exec-{lease_id}",
        artifact_root=artifact_root,
        process_pid=process_pid,
        process_start_token=process_start_token(process_pid),
        heartbeat_nonce=(uuid.uuid4().hex if process_pid is not None else None),
        claimed_mono=mono_now,
        lease_expires_mono=mono_now + float(lease),
        last_heartbeat_mono=mono_now,
        total_lease_deadline_mono=mono_now + float(lease) * (DEFAULT_MAX_RENEWALS + 1),
        total_lease_deadline=total_lease_deadline,
        write_access=(bool(write_access) if write_access is not None else None),
        # 只读 claim 记录源码基线：checkpoint 时据此复检「声明只读却新增改动」。
        source_baseline=_readonly_baseline(supervisor, write_access),
        worker_role=assigned_worker_role,
        domain_affinity=assigned_domain_affinity,
        model_tier=assigned_model_tier,
        target_form=assigned_target_form,
        tech_stack=assigned_tech_stack,
        assigned_fixtures=assigned_fixtures,
        active_probes=assigned_active_probes,
    )
    if superseded_claim is not None:
        superseded_claim.state = "superseded"
        superseded_claim.execution_state = "released"
        superseded_claim.execution_finished_at = now
        superseded_claim.superseded_by = claim.worker_id
        supervisor._append_event(
            state, "worker_superseded", now,
            worker_id=superseded_claim.worker_id,
            superseded_by=claim.worker_id,
            lease_id=superseded_claim.lease_id,
            artifact_root=superseded_claim.artifact_root,
        )
    if continuation and existing_claim is not None:
        supervisor._append_event(
            state, "worker_continued", now,
            worker_id=worker_key,
            previous_lease_id=existing_claim.lease_id,
            previous_unit_ids=list(existing_claim.unit_ids),
            previous_attempt=existing_claim.attempt,
        )
    state.claims[claim.worker_id] = claim
    state.scheduling_metrics.total_claims += 1
    queue_latency = max(0.0, now - state.round_started_at)
    state.scheduling_metrics.total_queue_seconds += queue_latency
    state.scheduling_metrics.max_queue_seconds = max(
        state.scheduling_metrics.max_queue_seconds, queue_latency
    )
    claim.unit_notes = {
        unit: dict(state.unit_notes[unit])
        for unit in claim.unit_ids if unit in state.unit_notes
    }
    supervisor._reconcile_parallel(state, now)
    supervisor._append_event(
        state, "worker_claimed", now, worker_id=claim.worker_id,
        lease_id=claim.lease_id, round_no=claim.round_no,
        lease_expires_at=claim.lease_expires_at,
        unit_ids=list(claim.unit_ids),
        resource_keys=list(claim.resource_keys), attempt=claim.attempt,
        scheduler_epoch=claim.scheduler_epoch,
        execution_id=claim.execution_id,
        artifact_root=claim.artifact_root,
        supersedes_worker_id=(superseded_claim.worker_id if superseded_claim else None),
        continues_lease_id=(existing_claim.lease_id if continuation and existing_claim else None),
    )
    return claim

__all__ = ["apply_claim_operation", "pid_is_alive"]
