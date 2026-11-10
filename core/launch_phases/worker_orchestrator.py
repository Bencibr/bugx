#!/usr/bin/env python3
"""launch_phases/worker_orchestrator.py - 并发 Worker 生成器与 Task 提示词装配 (DOC-072)。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import stat
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from core.planning.unit_kind import is_internal
except ImportError:  # pragma: no cover
    from core.unit_kind import is_internal

from core.host_runtime import host_registry

from core.launch_phases.common import (
    resolve_project_root,
    supervisor_state_path,
    uses_project_layout,
    bugx_paths,
    worker_subagent_type,
)
from core.launch_phases.supervisor_bridge import get_supervisor
from core.launch_phases.finding_settlement import findings_payload_path
from core.launch_phases.session_runtime import load_runtime

try:
    import core.i18n as i18n
except ImportError:
    try:
        from core import i18n
    except ImportError:
        i18n = None  # type: ignore[assignment]

try:
    from core.runtime import project_profile
except ImportError:
    try:
        from core.runtime import project_profile
    except ImportError:
        project_profile = None  # type: ignore[assignment]


try:
    from core.coverage import module_coverage
except ImportError:
    try:
        from core import module_coverage
    except ImportError:
        module_coverage = None

from core.supervisor.supervisor_models import (
    UNIT_LEVEL_DEBT,
    note_requests_deep_verification,
    worker_attempts_exhausted,
)




# ---------------------------------------------------------------------------
# DOC-256: 架构解耦导出：Worker 先验推荐与阶段屏障
# ---------------------------------------------------------------------------


VERIFICATION_TIER_INSTRUCTIONS = {
    "deep": "必须完成确定性双跑（≥2 次同指纹失败）+ 负对照通过 + 根因锚点核验；满足十项准则才转 confirmed。",
    "standard": "执行标准复现、根因定位和至少一次负向或边界验证；证据不足时标记 candidate。",
    "triage": "只做低成本初筛和证据收集；发现候选必须记录 upgrade_tier=deep，交由深度复验后才能确认。",
}
_VERIFICATION_TIER_INSTRUCTIONS = VERIFICATION_TIER_INSTRUCTIONS

AUTONOMOUS_MAX_EXTRA_TOOL_CALLS = 12
AUTONOMOUS_MAX_EXTRA_SECONDS = 600

from core.launch_phases.worker_prior import (
    unit_platform,
    worker_id_prefix,
    next_worker_id,
    safe_route_values,
    source_revision,
    unit_matches_path,
    prior_runtime_scan,
    cached_unit_prior,
    build_unit_prior,
    is_confirmed_finding,
    targeted_unit_context,
    recommend_blackbox_tools,
    render_unit_prior,
    knowledge_route_hint,
    lesson_guidance,
    filter_platform_mismatched,

    worker_host_plugin,
    worker_host_profile,
    order_jobs_longest_first,
    unit_execution_seconds,
    PRIOR_SCHEMA_VERSION,
    KNOWLEDGE_GUIDANCE_CHAR_BUDGET,
    PRIOR_CHAR_BUDGET,
    PRIOR_RUNTIME_CACHE,
    PRIOR_SCAN_BUDGET_SECONDS,
)
_PRIOR_SCHEMA_VERSION = PRIOR_SCHEMA_VERSION
_KNOWLEDGE_GUIDANCE_CHAR_BUDGET = KNOWLEDGE_GUIDANCE_CHAR_BUDGET
_PRIOR_CHAR_BUDGET = PRIOR_CHAR_BUDGET
_PRIOR_RUNTIME_CACHE = PRIOR_RUNTIME_CACHE
_PRIOR_SCAN_BUDGET_SECONDS = PRIOR_SCAN_BUDGET_SECONDS
_unit_platform = unit_platform
_worker_id_prefix = worker_id_prefix
_next_worker_id = next_worker_id
_safe_route_values = safe_route_values
_source_revision = source_revision
_unit_matches_path = unit_matches_path
_prior_runtime_scan = prior_runtime_scan
_cached_unit_prior = cached_unit_prior
_build_unit_prior = build_unit_prior
_is_confirmed_finding = is_confirmed_finding
_targeted_unit_context = targeted_unit_context
_recommend_blackbox_tools = recommend_blackbox_tools
_render_unit_prior = render_unit_prior
_knowledge_route_hint = knowledge_route_hint
_lesson_guidance = lesson_guidance

_worker_host_plugin = worker_host_plugin
_worker_host_profile = worker_host_profile
_order_jobs_longest_first = order_jobs_longest_first
_unit_execution_seconds = unit_execution_seconds

from core.launch_phases.worker_barriers import (
    looks_rate_limited,
    format_rate_limit_summary,
    sanitize_convention_line,
    conventions_block,
    barrier_probe,
    check_stage_barrier_satisfied,
    behavior_evidence_case_is_valid,
    reject_non_json_constant,
    has_current_behavior_evidence,
)
_looks_rate_limited = looks_rate_limited
_format_rate_limit_summary = format_rate_limit_summary
_sanitize_convention_line = sanitize_convention_line
_conventions_block = conventions_block
_barrier_probe = barrier_probe
_behavior_evidence_case_is_valid = behavior_evidence_case_is_valid
_reject_non_json_constant = reject_non_json_constant
_has_current_behavior_evidence = has_current_behavior_evidence



def _resolve_pool_capacity(
    *, host: str | None, provider: str | None,
    requested: int | None, pending: int,
) -> dict[str, object]:
    """调用容量规划器；导入失败时退回「请求值/待办数」的保守下限，不放大。"""

    try:
        from core.governance import capacity_planner

        return capacity_planner.effective_worker_concurrency(
            host=host, provider=provider, requested=requested, pending=pending,
        )
    except (ImportError, OSError, ValueError, RuntimeError):
        value = max(1, min(requested or pending, pending))
        return {
            "value": value, "binding": "fallback", "constraints": {},
            "sources": {}, "ceiling": value,
        }


def _readonly_concurrency_granted(runtime: dict | None) -> bool:
    """是否显式为**只读并发**授予写权限豁免。

    默认 False（强互斥）。开启条件必须同时成立：
      * 环境变量 ``BUGX_READONLY_CONCURRENCY=1``（运维显式开关）；
      * 运行时声明 ``readonly_enforced=True``（表示执行侧确有只读强制）。

    为什么这么严：``write_access=False`` 会让 claim 层**不派生** ``file:`` 互斥键，
    同文件多个 worker 可并发。一旦执行侧没有真正的只读强制，这个豁免就等于把
    「两个 worker 同改一个文件」的风险打开——比慢几个数量级更糟。
    """

    flag = (os.environ.get("BUGX_READONLY_CONCURRENCY") or "").strip()
    if flag != "1":
        return False
    if bool((runtime or {}).get("readonly_enforced")):
        return True
    # 没有执行侧只读视图时，退而要求**校验能力可用**（git 基线可取证）。
    # 这是 build 269 之后新增的门槛：只读声明之所以敢放开同文件并发，是因为
    # checkpoint 会用 claim 时的 git 基线复检越权（``readonly_enforcer``）。
    # 连基线都取不到（非 git 项目）就没有验证手段，必须 fail-closed。
    try:
        from core.governance import readonly_enforcer

        return readonly_enforcer.snapshot_source_state(Path.cwd()) is not None
    except (ImportError, OSError, ValueError, RuntimeError):
        return False


def workers(
    count: int | None = None,
    strategy: str = "risk",
    execute: bool = False,
    host: str | None = None,
    max_workers: int | None = None,
    subagent_type: str | None = None,
    stagger_seconds: float | None = None,
    model: str | None = None,
    provider: str | None = None,
    executable: str | None = None,
    *,
    dry_run: bool = False,
    brief: bool = False,
) -> int:
    """把 Supervisor 可领取单元打印成可直接执行的 Task worker 提示词，或在自治模式下直接执行。

    宿主执行面/控制面分离的机械入口：pre 之后运行本命令，把输出的 N 个
    worker 提示词**并发**交给宿主 Task/subagent 能力（subagent_type 默认
    bugx-worker；若被派类型缺少
    bugx-supervisor-worker_* MCP 工具，宿主必须改派具备 MCP 的通用类型），
    满足「同一 Life 至少 2 个 worker 真实重叠」的并发门禁。
    参数优先级为显式入参 > 当前 runtime context > 物理宿主探查；不读取全局宿主环境变量。
    """
    print("=" * 56)
    runtime = load_runtime()
    host_autonomous = bool(runtime.get("autonomous_mode"))
    runtime_host = runtime.get("host")
    selected_host = host if host is not None else runtime_host
    if selected_host is None:
        try:
            from core.supervisor.supervisor_host import detect_host
            selected_host = detect_host(resolve_project_root()).name
        except (ImportError, OSError, RuntimeError, ValueError):
            selected_host = "unknown"
    selected_host = str(selected_host).strip().casefold()
    if not selected_host:
        selected_host = "unknown"
    # 原生 Task 派发能力是插件事实（``engine_run.native_task``）；未迁移宿主走基线表。
    native_task_host = host_registry.host_native_task(selected_host)
    host_label, host_worker_facts = _worker_host_profile(selected_host)
    serial_worker_host = bool(host_worker_facts and host_worker_facts["mode"] == "serial")
    runtime_execute = bool(runtime.get("execute"))
    effective_execute = bool(execute or runtime_execute)
    selected_subagent_type = worker_subagent_type(
        subagent_type if subagent_type is not None else runtime.get("subagent_type")
    )
    if host_autonomous and execute and native_task_host:
        print("✗ 自治宿主模式禁止 --execute：Worker 必须由 Claude Code/Codex 原生 Task 执行。")
        return 2
    if not brief:
        print(
            f"bugx worker 生成器：{host_label} 主会话串行执行"
            if serial_worker_host
            else "bugx 并发 worker 生成器：把可领取单元变成 Task 提示词"
        )
        print("=" * 56)
    if not supervisor_state_path().is_file():
        print("✗ 未找到 Supervisor 状态；请先运行 bugx engine --root . pre")
        return 1
    try:
        from core.supervisor import audit_supervisor
        supervisor = get_supervisor()
        state = supervisor.status()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"✗ Supervisor 状态不可读: {exc}")
        return 1
    if state.status.value in {"COMPLETE", "FAILED"}:
        print(f"✗ Supervisor 状态为 {state.status.value}，请先开启新审计 run")
        return 1
    claimed = {
        unit
        for claim in state.claims.values()
        if claim.round_no == state.round_no and claim.state in {"active", "submitted"}
        for unit in claim.unit_ids
    }
    debt_states = UNIT_LEVEL_DEBT
    internal_debt = {
        unit for unit, item in state.coverage.items()
        if is_internal(unit) and item not in {
            "covered", "not_applicable", "removed", "superseded",
        } and not worker_attempts_exhausted(state, unit)
    }
    eligible = supervisor._eligible_units(state)
    pool = {
        unit for unit, item in state.coverage.items()
        if unit in eligible and item in debt_states
        and not worker_attempts_exhausted(state, unit)
    }
    remaining_units = list(pool - claimed)
    # 平台亲和：宿主无法验证其它平台的专属单元（如 Windows 宿主上的
    # darwin.go/linux.go），调度必须跳过并如实汇报，防止空转与假覆盖。
    host_platform = platform.system().lower()
    remaining_units, platform_mismatched = filter_platform_mismatched(
        remaining_units, host_platform,
    )
    if platform_mismatched:
        print(
            f"平台亲和过滤（本机 {host_platform}）：{len(platform_mismatched)} 个"
            "跨平台专属单元标记 BLOCKED/待匹配平台宿主，不参与本轮调度："
        )
        for unit in platform_mismatched[:8]:
            print(f"  - {unit}")
        if len(platform_mismatched) > 8:
            print(f"  ...（其余 {len(platform_mismatched) - 8} 个略）")
    if not remaining_units:
        if state.coverage_complete:
            print("✓ 全部覆盖单元均已完成覆盖。")
            print("  下一步：请执行 bugx engine --root . post --final 封存当前 Round 并生成测试报告。")
            return 0
        exhausted_units = sorted(
            unit for unit, status in state.coverage.items()
            if status in debt_states and worker_attempts_exhausted(state, unit)
        )
        if exhausted_units:
            status_val = getattr(state.status, "value", str(state.status))
            print(
                f"✗ 覆盖债务已触发 worker 尝试熔断：{len(exhausted_units)} 个单元已达"
                "尝试上限，本轮无可派发单元（不伪报完成）。"
            )
            print(
                "  说明：这些单元的 coverage 已标记为 blocked，"
                f"而 run 状态为 {status_val}；"
            )
            print(
                "  出路：调用 recover（推进 scheduler_epoch 解冻）后重跑 workers，"
                "或执行 finish 结束本 Round 并如实报告未覆盖债务。"
            )
            for unit in exhausted_units[:8]:
                print(f"  - {unit}")
            if len(exhausted_units) > 8:
                print(f"  ...（其余 {len(exhausted_units) - 8} 个略）")
            return 1
        # 可领集合为空时的分流提示：stale/debt 单元若被本轮终态 claim 占用，
        # 出路是 next-round（释放占用并重领），不是让宿主空转重跑 workers。
        stale_occupied = sorted(
            unit for unit, status in state.coverage.items()
            if status == "stale"
            and any(
                c.unit_ids and unit in c.unit_ids
                and c.state in {"submitted", "active"}
                for c in state.claims.values()
            )
        )
        if stale_occupied:
            print(
                "✗ 当前没有可领取的覆盖单元：stale 单元仍被本轮终态 claim 占用："
                + ", ".join(stale_occupied[:5])
            )
            print(
                "  出路：执行 supervisor next-round 推进到下一轮（占用随之释放、"
                "stale 单元重领），或执行 post 结算本 Round。"
            )
            return 1
        print("✗ 当前没有可领取的覆盖单元（全部已领取或已覆盖）")
        return 1

    # 1. 拓扑与 AST 多因子优先级调度（集成代码智能内在风险倾斜，DOC-060）
    strategy = strategy.strip().lower()
    if strategy not in {"risk", "bfs", "dfs", "hybrid", "auto"}:
        print(f"✗ 未知扫描策略: {strategy}（可选 risk、bfs、dfs、hybrid、auto）")
        return 2
    strategy_info = {"strategy": "legacy-fallback", "reason": "调度器异常，保持原有字典序"}
    unit_risks: dict[str, float] = {}
    surfaces: list[str] = []
    skill_plan: dict[str, Any] | None = None
    scheduler = None
    try:
        from core.supervisor import task_scheduler
        if project_profile is not None:
            try:
                active = bugx_paths.require(resolve_project_root())
                profile_data = project_profile.load_project_profile(active.knowledge_seed)
                if isinstance(profile_data, dict):
                    risk_metrics = profile_data.get("risk_metrics") or {}
                    classification = profile_data.get("classification") or {}
                    surfaces = [str(item) for item in classification.get("surfaces", [])]
                    for hotspot in risk_metrics.get("top_risk_hotspots") or []:
                        if isinstance(hotspot, dict) and "file" in hotspot:
                            unit_risks[str(hotspot["file"])] = float(hotspot.get("risk_score", 50.0))
                    skill_plan = profile_data.get("skill_plan")
                    if not skill_plan and "strategy" in profile_data:
                        skill_plan = profile_data["strategy"].get("skill_plan")
                    if not skill_plan:
                        strat = project_profile.compile_audit_strategy(profile_data)
                        skill_plan = strat.get("skill_plan")

                    # DOC-241: 将 AI 识别的优先级焦点与攻击图谱假说注入调度器风险加权
                    try:
                        from core.autonomous_cve import attack_graph_store
                        for hypo in attack_graph_store.get_hypotheses(resolve_project_root()):
                            src = hypo.get("source_file") or hypo.get("target")
                            if src:
                                unit_risks[str(src)] = max(unit_risks.get(str(src), 0.0), 90.0)
                    except Exception:
                        pass
                    try:
                        rev_file = resolve_project_root() / ".bugx" / "state" / "l1-semantic-review.json"
                        if rev_file.is_file():
                            rev_data = json.loads(rev_file.read_text(encoding="utf-8"))
                            p_focus = (rev_data.get("audit_strategy_verdict") or {}).get("priority_focus") or []
                            for pf in p_focus:
                                if pf:
                                    unit_risks[str(pf)] = max(unit_risks.get(str(pf), 0.0), 95.0)
                    except Exception:
                        pass
            except Exception:
                unit_risks = {}

        sched = task_scheduler.shared_scheduler(
            resolve_project_root(),
            unit_risks=unit_risks,
            traversal_strategy="auto" if strategy == "risk" else strategy,
            surfaces=surfaces,
        )
        scheduler = sched
        # DOC-065: 缺陷反馈闭环驱动任务调度动态加权 (record_defect)
        try:
            fpath = findings_payload_path()
            if fpath and fpath.is_file():
                with open(fpath, "r", encoding="utf-8") as f:
                    fdata = json.load(f)
                flist = fdata.get("findings") if isinstance(fdata, dict) else (fdata if isinstance(fdata, list) else [])
                for item in flist:
                    if isinstance(item, dict) and _is_confirmed_finding(item):
                        file_path = item.get("file") or item.get("path")
                        if not file_path and isinstance(item.get("evidence"), list) and item["evidence"]:
                            file_path = item["evidence"][0].get("path")
                        sev = item.get("severity", "HIGH")
                        if file_path:
                            sched.record_defect(str(file_path), str(sev))
        except Exception:
            pass
        candidates = sched.rank_units(remaining_units, state.coverage, unit_risks=unit_risks)
        strategy_info = sched.last_strategy_info
    except Exception:
        candidates = sorted(remaining_units)
    print(
        f"扫描策略：{strategy}（实际遍历={strategy_info.get('strategy', 'legacy-fallback')}）；"
        f"{strategy_info.get('reason', '')}"
    )
    if scheduler is not None:
        deep_upgrades = {
            unit for unit, note in state.unit_notes.items()
            if isinstance(note, dict)
            and note_requests_deep_verification(note.get("note"))
        }
        tier_by_unit = {
            unit: "deep" if unit in deep_upgrades else task_scheduler.verification_tier(
                unit, scheduler._risk_for_unit(unit, unit_risks), surfaces,
            )
            for unit in candidates
        }
        if strategy != "bfs":
            tier_order = {"deep": 0, "standard": 1, "triage": 2}
            original_order = {unit: index for index, unit in enumerate(candidates)}
            candidates = sorted(
                candidates,
                key=lambda unit: (tier_order[tier_by_unit[unit]], original_order[unit]),
            )
        counts = {tier: sum(tier_by_unit.get(unit) == tier for unit in candidates)
                  for tier in ("deep", "standard", "triage")}
        print(f"验证深度：deep={counts['deep']} standard={counts['standard']} triage={counts['triage']}（triage 发现候选必须升档复验）")
        print("风险 Top-10：")
        for unit in candidates[:10]:
            print(
                f"  - {unit}: score={scheduler.score_unit(unit, state.coverage.get(unit, 'pending'), scheduler._risk_for_unit(unit, unit_risks)):.2f} "
                f"tier={tier_by_unit.get(unit, 'standard')}"
            )
    else:
        tier_by_unit = {unit: "standard" for unit in candidates}

    # 2. 自适应并发容量动态计算
    runtime_count = runtime.get("workers")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        print("✗ worker count 必须是非负整数")
        return 2
    if runtime_count is not None and (
        not isinstance(runtime_count, int) or isinstance(runtime_count, bool) or runtime_count < 0
    ):
        print("✗ runtime workers 必须是非负整数")
        return 2
    requested_limit = (
        count if isinstance(count, int) and count > 0
        else runtime_count if isinstance(runtime_count, int) and runtime_count > 0
        else state.budget.max_concurrency_ceiling
    )
    # 宿主插件声明串行 Worker 且没有会话内原生 Task 时，进程池模式是唯一的显式
    # 并行逃逸口（并发硬顶也来自插件的 ``worker_runtime.max_concurrency``）。
    process_mode_cap = int(host_worker_facts["max_concurrency"] or 0) if host_worker_facts else 0
    is_process_mode = (
        serial_worker_host
        and bool(host_worker_facts and host_worker_facts["requires_process_mode"])
        and effective_execute
    )
    if serial_worker_host and not is_process_mode and requested_limit > 1:
        print(f"{host_label} Worker 串行模式：本批最多领取 1 个 claim，完成 checkpoint 后再领取下一条。")
        requested_limit = 1
    elif is_process_mode:
        # 宿主没有 in-session Task(subagent_type=...) 原语：进程池是显式逃逸口，
        # 上限取插件声明的并发硬顶。
        if process_mode_cap:
            requested_limit = min(requested_limit, process_mode_cap)
        print(
            f"{host_label} CLI 进程池模式：按自适应容量派发，单批并发上限 {requested_limit}"
            f"（2~{process_mode_cap or requested_limit} 路）。"
        )
    try:
        from core.supervisor import task_scheduler
        ctrl = task_scheduler.AdaptiveConcurrencyController(
            min_concurrency=state.budget.min_concurrency,
            max_concurrency=state.budget.max_concurrency_ceiling,
        )
        adaptive_cap = ctrl.compute_capacity(len(state.active_claims), state.budget, state)
        if state.budget.adaptive_concurrency and state.budget.require_parallel and state.budget.max_concurrency > 1:
            capacity = adaptive_cap
        else:
            capacity = max(0, state.budget.max_concurrency - len(state.active_claims))
    except Exception:
        capacity = max(0, state.budget.max_concurrency - len(state.active_claims))

    # 只读并发授权（P2）必须满足**两个**条件才声明，缺一不可：
    #   1) 显式开启 ``BUGX_READONLY_CONCURRENCY=1``；
    #   2) 该轮次被强制只读。
    # 审计发现：``fix_mode`` **不构成写授权强制**——它只被 post_phase 消费（决定 open
    # finding 是否放行结算），worker 执行侧（worker_runtime / host_runtime）从不读它，
    # 也没有只读沙箱/挂载。因此「fix_mode=log-only ⇒ worker 不改文件」是**假前提**：
    # 若据此声明只读，同文件并发就会在「模型自作主张改文件」时静默覆盖。
    # 在拿到执行侧强制（只读挂载或 checkpoint 文件指纹校验）之前，默认 ``None``
    # 走历史强互斥——宁可慢，不可覆盖。
    write_access = False if _readonly_concurrency_granted(runtime) else None

    limit = min(requested_limit, capacity)
    if limit <= 0:
        print("✗ 当前并发槽位已满；等待已有 worker 结算后再派发")
        return 1

    # 3. 亲和任务打包调度（同文件内聚聚合，最多 3 单元/包）
    try:
        from core.supervisor import task_scheduler
        # DOC-142 实测结论：tier 感知的窄打包（deep 独占 + triage 密集）在
        # slot 受限下把 units_mined 从 24 降到 16（广度 −33%），有效 recall
        # 反而 −14%——宽打包均摊前导段的收益大于专注度收益，故保留原亲和
        # 打包器；本批的增量在"包内每单元投入"（见上方 prior_by_unit）。
        # Bundle 上限随写权限自适应（P4）：只读轮次可以更大包（读并发安全、
        # 减少 claim/前导开销），写轮次保持 3（同文件冲突面可控）。上限 5 与
        # claim 层的 ``unit_ids`` 硬上限（1~5）一致，不制造越界。
        max_bundle = 5 if not write_access else 3
        bundles = task_scheduler.AffinityBundler.bundle_units(
            candidates, max_bundle_size=max_bundle,
        )
        desired_workers = min(limit, len(candidates))
        initial_bundle_count = len(bundles)
        if len(bundles) < desired_workers:
            bundles = task_scheduler.AffinityBundler.rebalance_for_parallelism(
                bundles, desired_workers,
            )
            print(
                f"亲和 Bundle 数不足（{initial_bundle_count} < {desired_workers}），"
                "已展开为单元级 Bundle 以填满动态并发槽位。"
            )
    except Exception:
        bundles = [[u] for u in candidates]

    # 尝试完整候选序列；冲突 bundle 跳过后继续补位，避免首个 file mutex
    # 拒绝让后续独立单元空槽等待下一次 workers 调用。
    selected_bundles = bundles
    if dry_run:
        selected_bundles = bundles[:limit]
    if dry_run:
        print(json.dumps({
            "schema_version": 1,
            "dry_run": True,
            "available_units": len(candidates),
            "selected_bundles": selected_bundles,
            "requested_count": requested_limit,
            "capacity": capacity,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    assigned = []
    worker_prefix = _worker_id_prefix(state)
    worker_ids = set(state.claims)
    next_worker_index = 1
    for bundle in selected_bundles:
        if len(assigned) >= limit:
            break
        worker_id, next_worker_index = _next_worker_id(
            worker_prefix, worker_ids, next_worker_index,
        )
        res_keys = [f"unit:{u}" for u in bundle]
        for u in bundle:
            fp = None
            try:
                from core.supervisor import task_scheduler
                fp, _ = task_scheduler.extract_unit_scope(u)
            except (ImportError, AttributeError):
                pass
            except Exception as exc:
                print(f"! [worker_orchestrator] 提取任务包单元作用域异常 ({u}): {exc}")
            if not fp:
                src = None
                failure: Exception | None = None
                try:
                    from core.supervisor.supervisor_models import unit_source_path

                    src = unit_source_path(u)
                except Exception as exc:  # noqa: BLE001 - 失败必须可观测，不得静默
                    failure = exc
                if src and "." in src.rsplit("/", 1)[-1]:
                    fp = src
                elif failure is not None and "." in u.rsplit("/", 1)[-1]:
                    # 审计 #18：文件型单元解析不出源文件 ⇒ 缺 ``file:<fp>`` 互斥
                    # 作用域，并发派发可能同时改同一个文件。旧实现 ``pass`` 静默
                    # 回退，等于把「同文件互斥」降级成「没人保证」。这里 fail-closed。
                    print(
                        f"! [worker_orchestrator] 无法解析文件型单元 {u} 的源文件作用域"
                        f"（缺少文件互斥保护），拒绝派发: {failure}"
                    )
                    raise ValueError(
                        f"无法确定文件型单元 {u} 的互斥作用域，拒绝在缺少 file:<fp> "
                        f"保护的情况下派发"
                    ) from failure
            if fp and f"file:{fp}" not in res_keys:
                res_keys.append(f"file:{fp}")
        lease_secs = 300.0
        try:
            from core.supervisor import task_scheduler
            lease_secs = float(task_scheduler.ComplexityLeaseCalculator.estimate_lease(bundle))
        except (ImportError, AttributeError):
            lease_secs = 300.0
        except Exception as exc:
            print(f"! [worker_orchestrator] 估算任务包复杂度租约异常 ({bundle}): {exc}")
            lease_secs = 300.0

        try:
            # 写权限声明（P2/P4）：只有真正获得源文件写授权的轮次才声明 write_access=True。
            # log-only / 只读审计轮次声明 False —— 这样同文件的多个只读 claim 可以并发
            # （claim 层不再派生 file: 互斥键），这是同文件串行木桶的根治点；
            # 未声明（None）时 claim 层按历史强互斥处理，绝不因为「没声明」放开写并发。
            claim = supervisor.claim_worker(
                worker_id, unit_ids=bundle, resource_keys=res_keys,
                lease_seconds=lease_secs, allow_bundle=True,
                write_access=write_access,
            )
        except audit_supervisor.ClaimRejected as exc:
            print(f"! 跳过无法预占的任务包 {bundle}: {exc}")
            continue
        assigned.append(claim)
    if not assigned:
        print("✗ 没有成功预占可执行单元；请检查租约、资源冲突或目标漂移")
        return 1
    try:
        # DOC-140 调度提速：复用它上面已经建好的调度器实例（行 555），
        # 不再第二次构造——每次构造都要全仓建图 + 影响网络 + git diff；
        # 并把整批 advance_cycle 包进 batch_history，只落盘一次。
        with sched.batch_history():
            for c in assigned:
                for cu in c.unit_ids:
                    sched.advance_cycle(selected_unit_id=cu, candidate_units=candidates)
    except Exception:
        pass
    try:
        supervisor_state = supervisor_state_path()
        state_display = str(supervisor_state.resolve().relative_to(resolve_project_root().resolve()))
    except (OSError, ValueError):
        state_display = str(supervisor_state_path())
    total_assigned_units = sum(len(c.unit_ids) for c in assigned)
    active_count = len(state.active_claims)
    if active_count > 0:
        print(
            f"[流水线动态补位] 已有活跃 Worker: {active_count} 个，本次填补并发槽位: {len(assigned)} 个；"
            f"剩余未申领单元: {max(0, len(candidates) - total_assigned_units)} 个。"
        )
    print(
        f"可领取单元 {len(candidates)} 个；已原子预占本批 {len(assigned)} 个 worker "
        f"（含 {total_assigned_units} 个单元，requested={requested_limit}，"
        f"capacity={capacity}，预算 max_concurrency={state.budget.max_concurrency}，"
        f"ceiling={state.budget.max_concurrency_ceiling}）。"
    )
    if serial_worker_host and not is_process_mode:
        print(f"执行纪律：{host_label} 主会话按序完成唯一 claim，通过 worker MCP 提交 checkpoint 后再继续；")
        print(f"  {host_label} 研究子 Agent 不承接 BugX claim，不调用 Task(subagent_type=bugx-worker)。")
    elif is_process_mode:
        sandbox = str(host_worker_facts.get("sandbox_dir") or "") if host_worker_facts else ""
        sandbox_note = f"独立 {sandbox} 沙箱" if sandbox else "独立 Worker 沙箱"
        print(f"执行纪律：每个 claim 在独立 {host_label} CLI 进程与{sandbox_note}中运行；")
        print("  仅允许 bugx-supervisor-worker 与 bugx-docs MCP，不加载主控 bugx-supervisor。")
    else:
        print("执行纪律：所有 worker 在同一时间窗口并发发起（Task 并行调用）；")
        print("  Supervisor 门禁要求同一 Life 至少 2 个 worker 真实重叠；")
        print("  只有 1 个可执行单元时才允许单 worker（串行需 --allow-serial + 理由）。")
    print("  Worker 必须使用 claim 返回的独立 artifact_root；不得把产物写入共享根目录。")
    if host_autonomous:
        if serial_worker_host and not is_process_mode:
            print(
                f"AUTONOMOUS_HOST_NEXT: 当前 {host_label} 主会话必须执行以上唯一 claim，"
                "通过 worker MCP 完成并验证 checkpoint 后再运行 workers --count 1；"
                "全部单元处理完成后串行处理 confirmed Finding 并调用 post。禁止等待用户确认。"
            )
        elif is_process_mode:
            print(
                f"AUTONOMOUS_HOST_NEXT: {host_label} CLI 进程池已进入滑动窗口；"
                "每个进程必须以持久化 Checkpoint 结束，异常/超时由 Supervisor 回收 claim。"
            )
        else:
            print(
                "AUTONOMOUS_HOST_NEXT: 宿主采用滑动窗口流水线（Sliding Window Pipeline）与错峰防限流模式！\n"
                "  1. 错峰启动：并发启动 Task 时，每个 Task 之间交错 1.5~3.0 秒（或每批 2~3 个分批放行），严禁瞬间同时突发打满上游模型 API；\n"
                "  2. 滑动补位：每当有任意一个 Worker 完成并提交 Checkpoint，若仍有待覆盖单元，立即重新调用 `bugx engine --root . workers` 补位并启动新 Task，"
                "将并发度平滑维持在上限（绝不等待同批长尾任务）。所有单元覆盖完毕后，串行处理 confirmed Finding，"
                "最后调用 post。禁止等待用户确认。"
            )
    state_rel = state_display
    header = "【共享指令】把下面这段粘进每个 worker 的 Task 提示词："
    if i18n is not None:
        header = i18n.prompt_text("workers.shared_instruction_header", fallback=header)
    print(header)
    print("```text")
    body_fallback = (
        "你是 BugX 并发 worker。宿主已经原子预占了下面给你的 coverage unit(s)；不要再次 claim。\n"
        "1. 先确认真实启动（返回的 execution_id 必须保持不变；只能逐字复制预占值，无法确认时省略，禁止生成/替换）：\n"
        "   python3 \"$(bugx engine path --name audit_supervisor.py)\" worker-start --root . \\\n"
        f"   --state {state_rel} --worker <预占 worker_id> --provider <provider>\n"
        "2. 对分配单元完成勘察 → 测试 → 证据复验；所有证据写入预占 claim 返回的 artifact_root；"
        "每个 coverage unit 使用独立、唯一的 artifact 文件，提交 checkpoint 后不得覆盖或修改。\n"
        "   【严禁全量测试】：Worker 仅对分配单元执行最小单测验证（python3 -m unittest tests.test_x），绝对严禁运行全量测试套件（如 run_tests.sh 或 unittest discover）！\n"
        "   【黑盒测试与变异验证】：针对输入解析/并发竞态/状态机单元，优先使用 Worker MCP 专属工具（bugx_fuzz_input / bugx_fuzz_concurrency / bugx_fuzz_stateful）进行变异探测；疑似漏洞提交前必须经 bugx_verify_differential_poc 差分沙盒双跑证实；\n"
        "   没有宿主提供的独立 git worktree 时禁止修改共享源码，修复由宿主串行完成；\n"
        "   不写共享运行态、不写 life、不跑 verify_life.py settle、不生成最终报告。\n"
        "3. 提交 checkpoint（JSON 中 life_delta 必须是数字 0，不能写成字符串 \"0\"；artifact_path 必须位于自己的 artifact_root）：\n"
        "   python3 \"$(bugx engine path --name audit_supervisor.py)\" checkpoint --root . \\\n"
        f"   --state {state_rel} --worker <预占 worker_id> --input <payload.json>\n"
        "   payload.json 必须包含本次提交唯一且可重试的 checkpoint_id，覆盖所有领取的 units。\n"
        "   expected_revision 只有在同一 MCP 响应刚返回该值时才能原样带上；否则省略，禁止猜测 0/1。\n"
        "   evidence 的 command/exit_code/status/artifact_sha256 必须来自真实执行；"
        "coverage 状态和 evidence.status 是两套不同枚举：coverage=covered 时 evidence.status=passed，"
        "其他 evidence.status 只能是 failed/error/skipped/blocked，不能把 covered 填进 evidence.status；"
        "每个 candidate finding 内的 evidence 必须是对象数组 [{\"path\": \"<相对路径>\", \"line\": <正整数行号>}]，"
        "禁止写成 \"path:line\" 字符串；"
        "artifact_path 必须是"
        "从仓库根开始且位于该 claim artifact_root 下的路径（例如 <artifact_root>/evidence.json），"
        "timestamp 必须是 time.time() 返回的正数 Unix 秒数；run_id、round_no、lease_id、worker_id、"
        "target_fingerprint 必须逐字复制宿主给出的 WORKER_CLAIM_FACTS，禁止自行推导。\n"
        "推荐使用下面的受约束收尾器生成并提交 payload；命令返回非零前不得结束 Task：\n"
        "   python3 \"$(bugx engine path --name worker_checkpoint.py)\" finalize --root ."
        f" --state {state_rel} --worker <预占 worker_id>"
        " --status <covered|unverified|partial|blocked>"
        " --artifact <artifact_root/证据文件> --command '<真实验证命令>'"
        " --exit-code <真实退出码> --note '<本次处置事实>'\n"
        "   没有可提交的真实证据时，省略 --artifact 并使用 unverified/partial/blocked；"
        "禁止用收尾器伪造 covered。\n"
        "4. worker-start 本身已被引擎记录为首次心跳（不要单独立即再跳一次，省一个回合）；"
        "此后每 ≤5 分钟无新工具调用时才补一次心跳；"
        "长命令（预计 >60s 的探针/测试）开始前调用 MCP bugx_supervisor_begin_long_call "
        f"（expected_seconds≤1800，声明保活窗口，防止租约在命令执行中被回收）；"
        "若宿主没有 MCP，\n"
        "   改用 `python3 \"$(bugx engine path --name audit_supervisor.py)\" heartbeat --root . "
        f"--state {state_rel} --worker <预占 worker_id>`；卡住由宿主回收租约。\n"
        "   不要把普通 claim 当成真实执行，也不要调用 launch_bugx.py heartbeat。\n"
        "4a. checkpoint 被拒（run_id/execution_receipt/跨纪元证据）时不要盲目重跑探针："
        "把探针输出重写到 artifact_root 下的新文件，再用步骤 3 的收尾器"
        "（worker_checkpoint.py finalize）按当前纪元重建合规 checkpoint。"
    )
    if i18n is not None:
        body = i18n.prompt_text("workers.shared_instruction_body", fallback=body_fallback, state_rel=state_rel)
    else:
        body = body_fallback
    body += (
        "\n\n【REAL_ACP_WORKER_MCP_GATE】当前任务由宿主 ACP 启动，安装回执已声明"
        " bugx-supervisor-worker 可用。worker-start、tool_call、heartbeat 和 checkpoint"
        " 必须通过受控协议通道驱动（caller_role=worker）：优先直接调用 `bugx-supervisor-worker_*`"
        " MCP 工具；若宿主会话确实未暴露 Worker MCP，允许使用 stdio JSON-RPC 直驱"
        "（`python3 -m core.bugx_mcp_server --role worker`）作为完全等效的控制通道，"
        "不能运行未经协议校验的脚本旁路这些控制操作。"
        "只有当前工具列表无 Worker MCP 且无法通过 stdio 连接服务时才报告 BLOCKED/RECOVERY_REQUIRED。"
        "完成最后一次源码/测试工具调用后，下一步必须立即调用 checkpoint；"
        "不要只输出‘正在提交’或在没有 checkpoint 调用的情况下结束 Task。"
    )
    body += (
        "\n5. 在最终 findings.json 中，若本次实际使用了工具，填写去重后的 `tools` 字符串数组（最多 8 个）；"
        "未使用工具则省略该字段。"
        "\n6. 若历史卡片确实适用于当前 finding，在最终 finding 中填写对应 `pattern_id`；不适用不要填写。"
        "\n7. 验证深度由任务包声明：deep 执行完整反事实复核，standard 执行标准复现，triage 只做初筛；"
        "triage 发现必须在 notes 中用单独一行精确标记 `upgrade_tier=deep`，不得把标记嵌入解释句，也不得直接写 confirmed。"
        "\n8. 任务中的机器先验只是待核实线索；按需调用 bugx_knowledge_route/lesson_retrieve，"
        "需要时调用 bugx_oracle_suggest/bugx_harness_status；记录命中、miss 或权限失败，"
        "不把历史卡片正文复制进提示词。triage 档默认跳过知识检索（除非任务文本给出了"
        "具体错误签名），省下的回合用于真实审查。"
        "\n9. 回合预算：triage 总工具调用 ≤6、standard ≤12、deep ≤20（读文件不计入）；"
        "达到预算立即提交 checkpoint 如实汇报现状，禁止继续展开。"
        "\n10. **reproduction 必须是可直接执行的命令**，不能把命令和说明拼在一起。"
        "实测踩过：`python3 p.py records the request (no network); u.json shows a=b`"
        " 满足「非空」但跑不起来（bash 语法错误），会被摄入侧判 "
        "`reproduction_not_executable` 直接拒收。说明写进 root_cause，不要粘在命令行里。"
        "\n11. **candidate 不等于完成**。finding 要转 confirmed 必须带 `confirmation` 块："
        "同指纹 ≥2 次独立运行（两个**不同** channel）+ negative_control outcome=pass + "
        "root_cause_verified + target_execution（target_platform 与 executed_platform 逐字相同）。"
        "只产出 candidate 不转 confirmed，整轮会停在 `finding_confirmation_pending` 并判 BLOCKED"
        "（实测 OctoPrintMCP：4 条高质量 finding 因此全部作废）。"
        "若确实无法完成确认，显式置 `reproducible: false` 并说明原因——"
        "不要留下 reproducible=true 却没有 confirmation 的半成品。"
    )
    lesson_guidance = _lesson_guidance()
    if lesson_guidance:
        body += f"\n\n{lesson_guidance}"
    # DOC-141：每单元上下文优先用定向装配（符号摘要 + 攻击面 + 规则 ID，
    # ~200 字符），通用先验包（~1,100 字符）降级为后备。定向上下文信息
    # 密度更高且 token 更省——同一预算下可覆盖更多单元；完整先验包仍落盘
    # 于 .bugx/state/priors/，worker 可按需自读。
    # DOC-142：定向上下文覆盖包内**每一个**单元。原实现只构 unit_ids[0]
    # ——一个 3 单元亲和包里 2 个单元拿不到任何定向上下文，打包在这里不是
    # 节省 token，而是静默的覆盖回退。
    prior_by_unit: dict[str, str] = {}
    for claim in assigned:
        for unit in claim.unit_ids:
            try:
                # 无论是否使用定向上下文，均可靠预构建机器先验并落盘 .bugx/state/priors/
                # 满足「完整先验包仍落盘于 .bugx/state/priors/，worker 可按需自读」契约
                prior_payload = _build_unit_prior(unit)
                targeted = _targeted_unit_context(
                    unit, tier=tier_by_unit.get(unit),
                )
                if targeted:
                    # 提取已发现的经验反模式与历史变异种子，以紧凑行形式直接增益定向上下文
                    extra_guidance = []
                    for item in prior_payload.get("items", []):
                        if not isinstance(item, dict):
                            continue
                        source = item.get("source")
                        if source == "evolved_lessons" and item.get("items"):
                            for l in item["items"][:2]:
                                pid = l.get("pattern_id") or ""
                                title = l.get("title") or ""
                                summary = l.get("summary") or ""
                                desc = f": {summary[:60]}" if summary else ""
                                extra_guidance.append(f"- 经验反模式提示：[{pid}] {title}{desc}")
                        elif source == "mutation_seeds" and item.get("items"):
                            for s in item["items"][:2]:
                                iid = s.get("incident_id") or ""
                                oracle = s.get("repro_oracle") or ""
                                sig = s.get("signature") or ""
                                extra_guidance.append(f"- 历史错题变异种子：[{iid}] {sig} (复现参考: {oracle[:80]})")
                    # DOC-242: 注入专属 MCP 黑盒工具推荐与场景化调用指引
                    tool_hints = _recommend_blackbox_tools(
                        unit, targeted_ctx=targeted, tier=tier_by_unit.get(unit),
                    )
                    if tool_hints:
                        extra_guidance.extend(tool_hints)
                    prior_text = f"【定向上下文】{targeted}"
                    if extra_guidance:
                        prior_text += "\n" + "\n".join(extra_guidance)
                    prior_by_unit[unit] = prior_text
                else:
                    rendered = _render_unit_prior(prior_payload)
                    tool_hints = _recommend_blackbox_tools(
                        unit, tier=tier_by_unit.get(unit),
                    )
                    if tool_hints:
                        rendered += "\n" + "\n".join(tool_hints)
                    prior_by_unit[unit] = rendered
            except Exception as exc:  # noqa: BLE001 - prior generation must never block dispatch
                prior_by_unit[unit] = f"【机器先验】degraded: {str(exc)[:160]}（继续标准流程）"
    print(body)
    print("```")
    # DOC-212 设计项 6：项目约定块在主围栏**之外**独立打印——不进 i18n body、
    # 不触碰宿主侧打印，conventions.json 缺失/损坏/消毒后为空时零输出。
    conventions_text = _conventions_block(resolve_project_root())
    if conventions_text:
        print(conventions_text)
    print("完整执行规则见全局引擎 bugx-worker.md（可用 bugx engine path --name bugx-worker.md 定位）。")
    # DOC-140 调度提速：一轮内不变量外提出 per-claim 循环——
    # ① 阶段栅栏判定按 consumes 签名记忆化（原为 claims×stages 次
    #    registry.json 全量读）；② 攻击图只加载一次（原为每 claim 一次
    #    JSON 解析 + from_dict）。
    expected_run_id = state.run_id if isinstance(state.run_id, str) else None
    barrier_probe = _barrier_probe(resolve_project_root(), expected_run_id=expected_run_id)
    preloaded_attack_graph = None
    try:
        from core.autonomous_cve import attack_graph_store
        preloaded_attack_graph = attack_graph_store.load_graph(resolve_project_root())
    except Exception:
        preloaded_attack_graph = None

    # DOC-140 调度提速：执行阶段从"串行 for"改为"有界并行"。
    # 预占阶段已按容量算出 limit（claims 数），但原实现把所有 claim 排成
    # 单线程队列逐个执行——并发门禁的 peak≥2 只靠 claim 时间戳重叠"被
    # 满足"，真实吞吐等于 1。这里保留逐 claim 的任务包装配（依赖打印顺
    # 序与人类可读日志），把重量级的模型执行段并行化。
    parallel_jobs: list[dict[str, Any]] = []

    for index, claim in enumerate(assigned, 1):
        units_str = ", ".join(claim.unit_ids)
        unit_args = " ".join(f'--unit "{u}"' for u in claim.unit_ids)
        if not brief:
            print("-" * 56)
            print(f"[{index}/{len(assigned)}] {claim.worker_id} → {units_str}")
        bundle_tiers = [
            tier_by_unit.get(u, "standard") for u in claim.unit_ids
        ]
        if brief:
            print(json.dumps({
                "worker_id": claim.worker_id,
                "unit_ids": list(claim.unit_ids),
                "lease_id": claim.lease_id,
                "execution_id": claim.execution_id,
                "artifact_root": claim.artifact_root,
                "attempt": claim.attempt,
            }, ensure_ascii=False, sort_keys=True))
        else:
            print(
                f"预占 claim：lease_id={claim.lease_id} lease_seconds={claim.lease_seconds:.0f}s "
                f"execution_id={claim.execution_id} artifact_root={claim.artifact_root} attempt={claim.attempt} "
                f"tier={'/'.join(bundle_tiers)}"
            )
        tier = tier_by_unit.get(claim.unit_ids[0], "standard")
        knowledge_category, knowledge_query = _knowledge_route_hint(claim.unit_ids[0])
        unit_prior_context = "\n".join(
            context
            for unit in claim.unit_ids
            if (context := prior_by_unit.get(unit))
        )
        claim_facts = (
            "【WORKER_CLAIM_FACTS：不可改写、不可省略】\n"
            f"worker_id={claim.worker_id}\n"
            f"unit_ids={json.dumps(list(claim.unit_ids), ensure_ascii=False)}\n"
            f"run_id={state.run_id}\n"
            f"round_no={claim.round_no}\n"
            f"lease_id={claim.lease_id}\n"
            f"execution_id={claim.execution_id}\n"
            f"artifact_root={claim.artifact_root}\n"
            f"target_fingerprint={state.target_fingerprint}\n"
            f"state_path={state_rel}\n"
            "evidence.timestamp 使用 time.time() 的正数 Unix 秒数；artifact_path 使用"
            "artifact_root/文件名（相对仓库根）；artifact_sha256 必须是该文件的 SHA-256。\n"
            "【执行动作协议（必须严格按此顺序）】：\n"
            f"① 必须首先调用 MCP 工具 `bugx_supervisor_worker_start(caller_role=\"worker\", worker_id=\"{claim.worker_id}\")` 登记真实开工（严禁省略！严禁传入 process_pid！未调用 worker_start 直接 checkpoint 会被门禁以 -32602 硬拦截并判死）；\n"
            "② 在受控边界内执行当前单元的分析、探查与命令验证；\n"
            f"③ 完成后必须调用 MCP 工具 `bugx_supervisor_checkpoint` 提交证据（caller_role=\"worker\", worker_id=\"{claim.worker_id}\", checkpoint_id=\"chk-{claim.worker_id}-001\"，coverage key 必须逐字来自 unit_ids；省略 expected_revision 防止并发冲突；无法完全验证时提交 coverage=unverified 并填写 notes，此时才免 execution_receipt；coverage=covered 一律必须携带 execution_receipt，契约见下节，禁止以「无需回执」为理由省略）。\n"
            "【执行回执契约】：coverage=covered / evidence.status=passed 的证据必须额外携带"
            " execution_receipt（schema_version=1，字段 execution_id、worker_id、lease_id、"
            "command、exit_code、artifact_path、artifact_sha256、status 必须与本条 evidence 逐字一致，"
            "禁止多加字段）。自建 payload 漏掉它会被 checkpoint 以 -32602「必须绑定受信 worker "
            "execution receipt」拒绝——这不是权限问题，而是缺回执。最稳妥：用 "
            "`python3 \"$(bugx engine path --name worker_checkpoint.py)\" finalize --root . "
            f"--state {state_rel} --worker {claim.worker_id} --status <covered|unverified|partial|blocked> "
            "--artifact <artifact_root/证据文件> --command '<真实验证命令>' --exit-code <真实退出码> "
            "--note '<本次处置事实>'` 生成并提交（它按 claim 回填回执）；非 covered 状态不需要回执。\n"
            "【活性与进程契约】：Task 子代理无常驻 OS 子进程，worker_start 时严禁传入 process_pid"
            "（省略该字段，仅依靠 heartbeat 与租约期限保活）；传入瞬态 Shell PID 会在退出后被直接判死抢占。\n"
            "coverage/evidence/notes 的 key 只能逐字来自上述 unit_ids；notes 只写单元手记，"
            "禁止 summary/finding/knowledge 等自由 key。\n"
            "只使用上述预占身份；不要重新 claim、生成新 ID、猜测字段、猜测 artifact 路径或把任务转交给别的 Worker。"
        )
        autonomous_completion = (
            "【AUTONOMOUS_COMPLETION_BARRIER】\n"
            "这是无人值守回合：worker-start 成功后立即做覆盖优先的有限探查。首个 worker 工具调用后，"
            f"最多再执行 {AUTONOMOUS_MAX_EXTRA_TOOL_CALLS} 次工具调用或 "
            f"{AUTONOMOUS_MAX_EXTRA_SECONDS} 秒，以先到者为准；读取技能、文档、源码和等待 MCP 都计入边界。"
            "不得继续无边界读码或等待宿主。边界到达时必须写入自己的"
            "artifact_root 并调用 bugx_supervisor_checkpoint：真实验证通过才提交 coverage=covered +"
            "结构化 passed evidence；无法在本边界完成则提交 coverage=unverified/partial/blocked 和"
            "notes（key 只能是本任务 unit_ids）说明已验证事实与重试原因，不能使用 summary/finding/knowledge"
            "等自由 key，不能空等、伪造 covered 或把半成品当 Finding。没有 Finding"
            "时也必须提交 checkpoint；checkpoint 成功后再返回宿主。候选 Finding 必须自带可裁决的复现原料："
            "file、target_code（失败代码锚点）、poc_cmd、poc_input（失败输入）与 poc_negative_input"
            "（同命令良性输入）；缺原料的候选写进 notes 说明缺什么，不要当 Finding 提交。宿主侧差分验证"
            "负责完成确定性双跑与负对照复验，提交后每条候选都会被裁决到 confirmed/rejected 终态，"
            "不会永久挂起阻塞结算。"
        )
        task_spec = (
            f'{claim_facts}\n{autonomous_completion}\n'
            f'共享指令 + `--worker {claim.worker_id} {unit_args}` 原样交给宿主并发执行；'
            f'本任务验证深度={tier}：{_VERIFICATION_TIER_INSTRUCTIONS[tier]}\n'
            f'知识检索建议：category={knowledge_category} query={knowledge_query}'
            f'；命中时填写 pattern_id，miss/权限失败照实记录。\n{unit_prior_context}'
        )
        if i18n is not None:
            task_spec = i18n.prompt_text(
                "workers.task_spec",
                fallback=task_spec,
                worker_id=claim.worker_id,
                unit_args=unit_args,
            )
        # Locale templates are intentionally allowed to replace the prose
        # task wrapper, but autonomous identity and completion barriers are
        # protocol data, not translatable copy.  Re-attach them after the
        # locale render so a host cannot lose them through i18n fallback.
        if host_autonomous:
            if "WORKER_CLAIM_FACTS" not in task_spec:
                task_spec = f"{claim_facts}\n{task_spec}"
            if "AUTONOMOUS_COMPLETION_BARRIER" not in task_spec:
                task_spec = f"{autonomous_completion}\n{task_spec}"
        if f"验证深度={tier}" not in task_spec:
            task_spec += f" 本任务验证深度={tier}：{_VERIFICATION_TIER_INSTRUCTIONS[tier]}"
        prior_block = unit_prior_context
        if prior_block and prior_block not in task_spec:
            task_spec += f"\n知识检索建议：category={knowledge_category} query={knowledge_query}；命中时填写 pattern_id，miss/权限失败照实记录。\n{prior_block}"
        if not brief:
            if serial_worker_host:
                sandbox = str(host_worker_facts.get("sandbox_dir") or "") if host_worker_facts else ""
                label = (
                    f"{host_label} CLI 进程执行（独立 {sandbox or 'Worker'} 沙箱）"
                    if is_process_mode
                    else f"{host_label} 主会话串行执行（无 Task 子 Agent handoff）"
                )
            else:
                label = f'Task(subagent_type="{worker_subagent_type(selected_subagent_type)}")'
            print(f"{label}：{task_spec}")
            if not serial_worker_host:
                print(
                    "  [派发提示] 两条等效路径，按宿主形态择快："
                    "①宿主会话自身暴露 bugx-supervisor-worker_* MCP 工具时，可直接以 "
                    "caller_role=worker 直驱协议通道（worker-start→探针→checkpoint，"
                    "单单元小任务包显著更快）；②被派代理类型没有这些 MCP 工具时，"
                    "改派具备 MCP 的通用类型（如 general-purpose）重发同一提示词。"
                    "两条路径都必须走受控协议（MCP 或 stdio JSON-RPC 均可），"
                    "禁止未受控脚本旁路；可在当前会话 runtime context 中指定类型。"
                )
        # Harness 的完整装饰文本以前只打印前 6 行而不进入 task_spec，付出
        # AST/规则/预算计算却不产生实际审计收益。Worker 通过 bugx-worker.md
        # 按需读取阶段协议；这里只保留轻量 stage 标签，避免无效冷路径。
        stage_name = "explore"
        print("  [主动注入 Stage Harness 任务包]: stage=explore (按需读取)")

        is_standalone_exec = effective_execute
        should_execute = is_standalone_exec and (
            not host_autonomous
            or not native_task_host
        )
        if should_execute:
            print(f"  [自治驱动] 已装配 Autonomous Worker 任务包: {claim.worker_id}")
            parallel_jobs.append({
                "claim": claim,
                "task_spec": task_spec,
                "stage_name": stage_name,
                "model": model,
                "provider": provider,
                "executable": executable,
            })

    if not parallel_jobs:
        return 0

    # 有界并行执行：默认并发度 = 本批 claim 数（即预占阶段按容量算出的
    # limit），可由显式 max_workers 或 runtime worker_capacity 收紧。单个 worker 的失败不
    # 影响同批其他 worker；结果按装配顺序汇总，保证日志确定性。
    runtime_capacity = runtime.get("worker_capacity")
    if runtime_capacity is None:
        runtime_capacity = runtime.get("workers")
    selected_capacity = max_workers if max_workers is not None else runtime_capacity
    if selected_capacity is not None and (
        not isinstance(selected_capacity, int)
        or isinstance(selected_capacity, bool)
        or selected_capacity < 0
    ):
        print("✗ max_workers/worker_capacity 必须是非负整数")
        return 2
    # 并发上限 = **多源约束取最小值**（系统资源 / 宿主容量 / provider 配额 / 请求值 /
    # 待办数）。任何单源决定都会越界：机器只扛 2 路时接口给 10 没用，宿主只支持 5
    # 时机器给 10 也只能跑 5。测算逻辑见 ``core/capacity_planner.py``，这里只消费裁决。
    capacity_decision = _resolve_pool_capacity(
        host=selected_host, provider=provider,
        requested=selected_capacity, pending=len(parallel_jobs),
    )
    pool_workers = int(capacity_decision["value"])
    if capacity_decision.get("binding") not in {"pending", "requested", None}:
        print(
            f"  [容量裁决] 并发 {pool_workers}（最弱环节 {capacity_decision['binding']}；"
            f"约束 {capacity_decision['constraints']}）"
        )

    # DOC-193: 派发侧错峰抗限流延迟来自显式参数或当前 runtime context。
    stagger_value = (
        stagger_seconds if stagger_seconds is not None
        else runtime.get("stagger_seconds", 0.0)
    )
    try:
        stagger_secs = float(stagger_value)
    except (TypeError, ValueError):
        print("✗ stagger_seconds 必须是非负有限数")
        return 2
    if not math.isfinite(stagger_secs) or stagger_secs < 0:
        print("✗ stagger_seconds 必须是非负有限数")
        return 2

    def _run_job(job: dict[str, Any]) -> dict[str, Any]:
        from core.supervisor import worker_runtime
        claim = job["claim"]
        res = worker_runtime.execute_autonomous_worker(
            workspace_root=resolve_project_root(),
            claim=claim,
            task_spec=job["task_spec"],
            stage_name=job["stage_name"],
            model=job.get("model") or model,
            provider=job.get("provider") or provider,
            executable=job.get("executable") or executable,
            supervisor=supervisor,
            scheduler=sched,
            host=selected_host,
            max_workers=pool_workers,
            execute=effective_execute,
        )
        return {"worker_id": claim.worker_id, "result": res}

    print(f"  [自治驱动] 启动滑动窗口流水线执行 {len(parallel_jobs)} 个 Worker 任务（并发度 {pool_workers}）...")
    outcomes: list[tuple[str, dict[str, Any] | None, str | None]] = []
    rate_limited_units: list[str] = []
    # 木桶修复（LPT 调度）：把**预计最慢**的单元排到队首，让短单元在尾部填缝。
    # 固定并发下轮次墙钟 ≈ 最慢单元的收尾时间；把所有长单元堆在队尾会让它们
    # 串行收尾（经典 makespan 木桶）。LPT（Longest Processing Time first）对
    # makespan 有 ≤4/3·OPT 的界，且只需一个排序，不改并发度、不改账本语义。
    # 预计耗时优先用**单元级历史**，缺失时退回本批平均值，再缺失则保持原序（稳定）。
    job_queue = _order_jobs_longest_first(parallel_jobs, supervisor=supervisor)
    with ThreadPoolExecutor(max_workers=pool_workers, thread_name_prefix="bugx-worker") as pool:
        futures: dict[Any, dict[str, Any]] = {}
        # 初始填充并发窗口至 max_workers（带错峰延迟）
        while job_queue and len(futures) < pool_workers:
            job = job_queue.pop(0)
            if futures and stagger_secs > 0:
                time.sleep(stagger_secs)
            futures[pool.submit(_run_job, job)] = job

        # 滑动窗口动态补位：任意任务完成立即结算并从队列补充新任务维持并发度
        while futures:
            # 等待最早完成的任意一个或多个任务
            done, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                job = futures.pop(future)
                worker_id = job["claim"].worker_id
                try:
                    payload = future.result()
                    outcomes.append((worker_id, payload["result"], None))
                    print(f"  ✓ Worker {worker_id} 完成并提交 Checkpoint: {payload['result'].get('checkpoint_id')}")
                except Exception as exec_err:  # noqa: BLE001 - 单 worker 失败不拖垮同批
                    err_text = str(exec_err)
                    outcomes.append((worker_id, None, err_text))
                    if _looks_rate_limited(err_text):
                        rate_limited_units.extend(job["claim"].unit_ids)
                    print(f"  ✗ Worker {worker_id} 自治执行失败: {exec_err}")

                # 流水线动态补位：只要队列中还有任务，立即补满并发窗口
                while job_queue and len(futures) < pool_workers:
                    next_job = job_queue.pop(0)
                    next_worker_id = next_job["claim"].worker_id
                    if stagger_secs > 0:
                        time.sleep(stagger_secs)
                    print(f"  [流水线补位] 动态发起新任务 Worker {next_worker_id} 维持并发度 {pool_workers}...")
                    futures[pool.submit(_run_job, next_job)] = next_job

    succeeded = sum(1 for _, _, err in outcomes if err is None)
    print(f"  [自治驱动] 流水线完成 {succeeded}/{len(parallel_jobs)}")
    if rate_limited_units:
        # DOC-212 设计项 5：限流失败的结构化回传——只在此汇总行追加，
        # 不触碰 dry-run（schema_version:1）与 brief claim 两处 JSON 键集。
        print("  [自治驱动] " + _format_rate_limit_summary(rate_limited_units))
    return 0
