#!/usr/bin/env python3
"""launch_phases/pre_phase.py - Round 启动前门禁、模块覆盖校验与基线快照 (DOC-072)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Mapping

import core.secure_atomic as secure_atomic

from core.launch_phases.common import (
    pollings_root,
    AGENT_DIR,
    COMMIT_POLICIES,
    COVERAGE_EVIDENCE,
    MODULE_COVERAGE,
    POLLING_DECISION_REQUIRED,
    RESTART_REQUIRED,
    set_last_child_failure,
    POLLING_POLICIES,
    REPORT_POLICY_CHOICES,
    REPORT_POLICY_DEFAULT,
    REVIEW_DECISION_LATER,
    REVIEW_MODES,
    STARTUP_INTERACTION_CHOICES,
    STARTUP_INTERACTION_MODES,
    STRATEGY_PLAN_ACTIONS,
    child_environment,
    coverage_manifest_path,
    format_duration,
    resolve_project_root,
    run_verify_life,
    run_child,
    supervisor_state_path,
    uses_project_layout,
    bugx_paths,
    host_display_name,
    host_serial_worker,
    worker_subagent_type,
)
from core.launch_phases.session_runtime import (
    append_runtime_event,
    baseline_json,
    close_runtime,
    iso_time,
    load_runtime,
    refresh_audit_todo,
    refresh_runtime_paths,
    reset_segment_timer,
    runtime_lock,
    save_runtime,
    set_commit_policy,
    set_polling_policy,
    set_review_mode,
    start_runtime,
)
from core.launch_phases.startup_gates import (
    aborted_session_gate,
    check_global_budget_gate,
    check_stage_prompts_integrity,
    compile_strategy_plan,
    coverage_startup_gate,
    mark_strategy_customized,
    refresh_startup_todo,
    detect_startup_environment,
    strategy_plan_gate,
    version_marker,
)
from core.launch_phases.supervisor_bridge import (
    degraded_gate,
    ensure_supervisor,
    report_degraded_probes,
    run_preflight_probes,
    autonomous_degraded_choices,
    auto_provision_accelerators,
    run_tools_kb,
    supervisor_mcp_gate,
    host_entry_gate,
    supervisor_units,
)
from core.launch_phases.report_sharing import replay_share_pending, normalize_report_policy
from core.launch_phases.lesson_context import (
    LESSON_CONTEXT_BYTE_BUDGET,
    LESSON_CONTEXT_TOKEN_BUDGET,
    derive_lesson_query,
    lesson_context,
    set_lesson_context,
)

try:
    from core.runtime import project_profile
except ImportError:
    try:
        from core.runtime import project_profile
    except ImportError:
        project_profile = None  # type: ignore[assignment]

try:
    import core.i18n as i18n
except ImportError:
    try:
        from core import i18n
    except ImportError:
        i18n = None  # type: ignore[assignment]


_STARTUP_DEFAULTS = {
    "review_mode": "mcp-auto",
    "fix_mode": "auto",
    "polling_policy": "auto",
    "commit_policy": "manual",
    "cloud_policy": "local",
    "session_recovery": "resume",
    "startup_action": "start",
    # DOC-236 C6：报告提交策略在 L1 采集并持久化，L5 `post` 无交互执行。
    # 默认 `local`（安全：只保留本地报告，不对外提交）。
    "report_policy": "local",
}

autonomous_defaults = {
    "polling_policy": "auto",
    "review_mode": "mcp-auto",
    "fix_mode": "auto",
    "commit_policy": "round",
    "session_recovery": "resume",
    "startup_action": "start",
    "report_policy": "local",
    "strategy_plan": "approve",
}

_LEGACY_SHARE_POLICY_MESSAGE = (
    "share_policy 已移除；请删除旧字段并使用 report_policy=none|local|submit 重建"
)


def _snapshot_startup_git_dirty_paths(root: Path) -> list[str] | None:
    """Capture paths already dirty before this session, or fail closed."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
            cwd=root, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    records = result.stdout.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            return None
        status = record[:2]
        paths.add(os.fsdecode(record[3:]))
        if b"R" in status or b"C" in status:
            if index >= len(records) or not records[index]:
                return None
            paths.add(os.fsdecode(records[index]))
            index += 1
    return sorted(paths)


def _startup_policy_config(
    root: Path, cli_overrides: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Resolve startup policy from system, user, project, then CLI scope."""
    if cli_overrides and "share_policy" in cli_overrides:
        raise ValueError(_LEGACY_SHARE_POLICY_MESSAGE)
    values = dict(_STARTUP_DEFAULTS)
    user_config = Path.home() / ".bugx" / "config.json"
    for path in (user_config, root / ".bugx" / "config.json"):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if "share_policy" in payload:
            raise ValueError(_LEGACY_SHARE_POLICY_MESSAGE)
        if "report_policy" in payload:
            raw_report_policy = payload.get("report_policy")
            if (
                raw_report_policy is not None
                and (
                    not isinstance(raw_report_policy, str)
                    or (
                        raw_report_policy.strip()
                        and raw_report_policy.strip().casefold()
                        not in REPORT_POLICY_CHOICES
                    )
                )
            ):
                raise ValueError(
                    "report_policy 只能是 "
                    f"{'/'.join(REPORT_POLICY_CHOICES)}，收到 {raw_report_policy!r}"
                )
        for key in _STARTUP_DEFAULTS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                values[key] = value.strip()
    for key, value in (cli_overrides or {}).items():
        if key in values and isinstance(value, str) and value.strip():
            values[key] = value.strip()
    if values["review_mode"] == "auto":
        values["review_mode"] = "mcp-auto"
    if values["commit_policy"] == "none":
        values["commit_policy"] = "manual"
    return values


def startup_policy_explicit_keys(root: Path) -> frozenset[str]:
    """返回用户/项目 ``config.json`` 中**显式**声明的启动策略键集合。

    ``_startup_policy_config`` 会用 ``_STARTUP_DEFAULTS`` 播种全部键，调用方
    因此无法区分「用户配置了 local」与「默认值就是 local」。需要区分二者的
    门禁（如 ``report_policy`` 三选一：未显式声明才询问）只能读原始配置文件。
    非法/不可读配置在 ``_startup_policy_config`` 已 fail-closed，这里静默跳过
    仅用于判定「是否显式」，不承担校验职责。
    """
    keys: set[str] = set()
    for path in (Path.home() / ".bugx" / "config.json", root / ".bugx" / "config.json"):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in _STARTUP_DEFAULTS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                keys.add(key)
    return frozenset(keys)


def _prepare_project_profile(*, force: bool = False) -> dict[str, Any] | None:
    """Create/reuse the project fact profile before knowledge/tool planning."""
    if project_profile is None or not uses_project_layout():
        return None
    active = bugx_paths.require(resolve_project_root())
    if not active.knowledge_db.is_file() or active.knowledge_db.is_symlink():
        raise ValueError(
            f"项目知识账本不存在或不安全: {active.knowledge_db}；"
            "请先完成项目初始化/显式迁移"
        )
    producer = version_marker(AGENT_DIR / "metadata.txt") or "unknown"
    profile, changed = project_profile.ensure_project_profile(
        active.knowledge_seed, resolve_project_root(), producer_version=producer, force=force,
    )
    strategy = project_profile.compile_audit_strategy(profile)
    project_profile.export_project_profile(
        profile, active.knowledge_root / "project-profile.json",
    )
    with runtime_lock():
        state = load_runtime()
        state["project_profile_hash"] = profile.get("profile_hash")
        state["project_profile_revision"] = profile.get("revision")
        state["project_profile_status"] = profile.get("status", "unknown")
        state["project_classification"] = profile.get("classification", {})
        state["project_routing_tags"] = project_profile.routing_tags(profile)
        state["audit_strategy"] = strategy
        if changed:
            append_runtime_event(
                state, "project-profile-updated", time.time(),
                f"hash={profile.get('profile_hash')} revision={profile.get('revision')}",
            )
        save_runtime(state)
    print(
        f"  项目画像：{','.join(profile.get('forms') or ['unknown'])} | "
        f"语言：{','.join(profile.get('languages') or ['unknown'])} | "
        f"状态：{profile.get('status', 'unknown')} | "
        f"profile_hash={profile.get('profile_hash', 'unknown')}"
    )
    return profile


def _prepare_business_specification(*, environment: Any = None, noninteractive: bool = False) -> None:
    """DOC-245: 抽取多源规约资产、领域原型断环分析与人机交互治理。"""
    try:
        from core.planning import spec_harvester
        from core.planning import domain_archetype
        from core.runtime import project_settings
        from core.runtime import interaction
    except ImportError:
        return

    root = resolve_project_root()
    try:
        spec = spec_harvester.harvest_specification(root)
        spec_harvester.save_business_spec(root, spec)

        scope_decisions = project_settings.get_scope_decisions(root)
        gaps_result = domain_archetype.analyze_domain_completeness(spec, root, scope_decisions)

        gaps = gaps_result.get("gaps", [])
        for gap in gaps:
            domain = gap.get("domain", "auth")
            if domain in scope_decisions:
                continue
            if noninteractive or not sys.stdin.isatty():
                decision = "spec_gap_continue"
                fixture = domain_archetype.provide_prerequisite_fixture(domain, root)
                detail = f"无人在场自愈降级：底座测试夹具注入 ({fixture.get('test_user')})"
            else:
                decision = interaction.ask_domain_gap_decision(gap, environment=environment)
                detail = f"用户交互选择: {decision}"

            project_settings.save_scope_decision(root, domain, decision, detail=detail)
            scope_decisions[domain] = {"decision": decision, "detail": detail}

        gaps_result = domain_archetype.analyze_domain_completeness(spec, root, scope_decisions)
        domain_archetype.save_spec_gaps(root, gaps_result)

        if gaps:
            print(
                f"  [DOC-245] 业务规约抽取就绪：{len(spec.endpoints)} 端点 / "
                f"{len(spec.explicit_invariants)} 显式不变量 | 规约断环={len(gaps)}"
            )
    except Exception as exc:
        print(f"  [DOC-245 规约警告] 规约治理降级为纯 AST 拓扑：{exc}")


def coverage_status() -> dict:
    """读取覆盖清单的只读摘要；运行中未覆盖不会让 status 命令失败。"""
    if not MODULE_COVERAGE.is_file():
        return {"available": False, "error": "缺少 module_coverage.py"}
    try:
        arguments = [
            sys.executable, str(MODULE_COVERAGE), "status", "--json",
            "--root", str(resolve_project_root()),
            "--manifest", str(coverage_manifest_path()),
        ]
        if COVERAGE_EVIDENCE.is_file():
            arguments.extend(["--evidence", str(COVERAGE_EVIDENCE)])
        result = subprocess.run(
            arguments,
            cwd=str(resolve_project_root()), capture_output=True, text=True, check=False,
            timeout=30, env=child_environment(),
        )
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "覆盖状态检查超时（30 秒）"}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "available": False,
            "error": result.stderr.strip() or result.stdout.strip()
            or "覆盖状态无法解析",
        }
    if result.returncode != 0:
        payload.setdefault("error", "覆盖清单不可用")
    return payload


def attach_coverage(runtime: dict, coverage: dict) -> dict:
    """把覆盖进度、当前阶段和基于已完成单元的粗略 ETA 合入运行状态。"""
    result = dict(runtime)
    result["phase"] = coverage.get("phase", "未知")
    result["current_units"] = coverage.get("current_units", [])
    progress = coverage.get("overall") or {"covered": 0, "total": 0, "percent": 0.0}
    result["progress"] = progress
    completed = progress.get("covered", 0)
    total = progress.get("total", 0)
    elapsed = result.get("current_elapsed_seconds", 0.0)
    eta = None
    if result.get("state") == "running" and completed > 0 and total > completed:
        eta = max(0.0, elapsed / completed * (total - completed))
    result["eta_seconds"] = round(eta, 3) if eta is not None else None
    return result


def print_coverage(coverage: dict, runtime: dict) -> None:
    if not coverage.get("available"):
        print(f"覆盖状态: 不可用（{coverage.get('error', '清单缺失')}）")
        return
    print(f"覆盖状态: {coverage.get('phase', '未知')}阶段")
    for key, label in (("internal", "内部最小单元"), ("business", "模块间业务链路")):
        item = coverage.get(key) or {}
        print(
            f"  {label}: {item.get('covered', 0)}/{item.get('total', 0)} "
            f"({item.get('percent', 0.0):.1f}%)"
        )
    current = runtime.get("current_units") or []
    if current:
        print("  当前单元: " + ", ".join(
            f"#{item['id']} {item['name']}[{item['status']}]" for item in current
        ))
    if runtime.get("eta_seconds") is not None:
        print(f"  预计剩余: {format_duration(runtime['eta_seconds'])}")


# DOC-125 闭环：LivingAttackGraph 的**生产者**接线（核验报告 C125-1/C125-2）。
# 图谱故障永远只是降级：挖掘业的输入是覆盖清单与 Worker 证据，不是图谱文件，
# 一个坏快照绝不能让 pre 退出。失败通过 attack-graph-degraded 运行事件留痕。




# ---------------------------------------------------------------------------
# DOC-256: 架构解耦导出：攻击面侦察与会话状态构建
# ---------------------------------------------------------------------------
from core.launch_phases.pre_recon_scout import (
    attack_graph_degraded,
    read_json_or_none,
    build_recon_scout_command,
    extract_sink_snippets,
    extract_project_readme_summary,
    collect_mcp_skills_inventory,
    collect_directory_tree,
    recon_context_key,
    run_recon_scout,
    match_unit_for_sink,
    match_route_for_sink,
    seed_attack_graph,
)
_attack_graph_degraded = attack_graph_degraded
_read_json_or_none = read_json_or_none
_extract_sink_snippets = extract_sink_snippets
_extract_project_readme_summary = extract_project_readme_summary
_collect_mcp_skills_inventory = collect_mcp_skills_inventory
_collect_directory_tree = collect_directory_tree
_recon_context_key = recon_context_key
_run_recon_scout = run_recon_scout
_match_unit_for_sink = match_unit_for_sink
_match_route_for_sink = match_route_for_sink
_seed_attack_graph = seed_attack_graph

from core.launch_phases.pre_state_builder import (
    begin_new_session,
    RuntimeMemo,
    PreState,
    normalize_pre_options,
    run_preflight_budget_gates,
    resolve_resume_state,
    derive_and_persist_decisions,
    run_tools_and_preflight_gates,
    ensure_autonomous_prep_record,
    start_or_resume_session,
    serial_runtime_identity,
    serial_authorization_intent,
    persist_serial_authorization_record,
)
_begin_new_session = begin_new_session
_RuntimeMemo = RuntimeMemo
_PreState = PreState
_normalize_pre_options = normalize_pre_options
_run_preflight_budget_gates = run_preflight_budget_gates
_resolve_resume_state = resolve_resume_state
_derive_and_persist_decisions = derive_and_persist_decisions
_run_tools_and_preflight_gates = run_tools_and_preflight_gates
_ensure_autonomous_prep_record = ensure_autonomous_prep_record
_start_or_resume_session = start_or_resume_session
_serial_runtime_identity = serial_runtime_identity
_serial_authorization_intent = serial_authorization_intent
_persist_serial_authorization_record = persist_serial_authorization_record

for _fn in (
    _normalize_pre_options,
    _run_preflight_budget_gates,
    _resolve_resume_state,
    _derive_and_persist_decisions,
    _run_tools_and_preflight_gates,
    _start_or_resume_session,
    normalize_pre_options,
    run_preflight_budget_gates,
    resolve_resume_state,
    derive_and_persist_decisions,
    run_tools_and_preflight_gates,
    start_or_resume_session,
):
    try:
        _fn.__module__ = __name__
    except (AttributeError, TypeError):
        pass

def _prepare_runtime_and_supervisor(st: "_PreState", memo: "_RuntimeMemo") -> int:
    """项目画像、复盘账本上下文、运行计时建立与并发调度门禁。

    原 ``pre()`` 876-917 逐字迁移。"""
    new_session_started = st.new_session_started
    lesson_query = st.lesson_query
    lesson_category = st.lesson_category
    life_kind = st.life_kind
    allow_serial = st.allow_serial
    serial_reason = st.serial_reason
    runtime = st.runtime
    started = st.started
    supervisor_state = st.supervisor_state
    lesson_ctx = st.lesson_context
    try:
        profile = (
            st.project_profile_data
            if st.project_profile_prepared
            else _prepare_project_profile()
        )
        st.project_profile_data = profile
        st.project_profile_prepared = True
        # 该阶段是从 ``pre()`` 拆出的独立函数，``environment`` 不在其作用域内。
        # 就地取一次（与 pre 同源、无副作用），否则 ``NameError`` 会让整条
        # pre 链路崩溃（既有缺陷，build 278 全量验证发现）。
        _prepare_business_specification(
            environment=detect_startup_environment(),
            noninteractive=st.noninteractive_startup,
        )
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"✗ 项目画像准备失败：{exc}")
        if new_session_started:
            close_runtime("aborted", reason="project profile failed")
        return 1
    derived_query, query_source = derive_lesson_query(lesson_query, lesson_category)
    try:
        lesson_ctx = lesson_context(derived_query, lesson_category, profile)
        lesson_ctx["query_source"] = query_source
    except (OSError, ValueError, TypeError) as exc:
        print(f"✗ 复盘账本门禁失败: {exc}")
        if new_session_started:
            close_runtime("aborted", reason="lesson context failed")
        return 1
    try:
        runtime, started = start_runtime(auto_started=False)
        with runtime_lock():
            runtime = load_runtime()
            # 宿主绑定优先级：显式 --host > 物理探针（进程树/交互面检测） >
            # 旧 runtime 遗留绑定。st.host 从未被赋值（恒 None），旧实现因此
            # 永远回退到 runtime 首次绑定值——遗留自治进程（如 --host opencode
            # 的 watchdog 拉起物）会把错误宿主永久固化，宿主 usage/串行语义
            # 全部走错插件（实测 2026-11-04：zcode 会话被固化成 opencode，
            # _zcode_usage 记账通道整轮静默失效）。恢复/新启动一律以本轮
            # 物理探针为准，绑定变化时留痕告警。
            detected_host = str(
                (getattr(detect_startup_environment(), "host", None) or "")
            ).strip() or None
            previous_host = str(runtime.get("host") or "").strip() or None
            resolved_host = st.host or detected_host or previous_host or "unknown"
            if previous_host and resolved_host != previous_host:
                runtime["host_rebound_from"] = previous_host
                print(
                    f"[launch_bugx] ⚠ 宿主绑定重订：runtime 遗留绑定 "
                    f"{previous_host!r} ≠ 本轮物理探针 {resolved_host!r}，"
                    "已按物理探针重订（宿主 usage/并发/串行语义随新宿主生效）"
                )
            runtime["host"] = resolved_host
            runtime["life_kind"] = life_kind
            runtime["startup_result"] = "passed"
            runtime["autonomous_mode"] = bool(st.autonomous_mode)
            runtime["autonomous_policy"] = st.autonomous_policy
            runtime["autonomous_policy_hash"] = (
                st.autonomous_policy or {}
            ).get("policy_hash")
            runtime["report_policy"] = normalize_report_policy(
                st.report_policy
            ) or REPORT_POLICY_DEFAULT
            if new_session_started:
                runtime["startup_git_dirty_paths"] = _snapshot_startup_git_dirty_paths(
                    resolve_project_root()
                )
            if not runtime.get("startup_observation"):
                runtime["startup_observation"] = "clean"
            if not runtime.get("startup_intent"):
                runtime["startup_intent"] = "new_session" if new_session_started else "resume"
            save_runtime(runtime)
        # 原实现在此处执行 nonlocal ``runtime_state = runtime``，让随后的
        # ``_runtime()`` 返回刚写入的新快照。抽帧后该赋值会退化成**死局部变量**
        # （仅写不读），必须显式接管 memo，否则下游仍读到 ``start_runtime``
        # 与 life_kind 写盘之前的陈旧快照（PERF-05 不变式）。
        memo.seed(runtime)
        set_lesson_context(lesson_ctx)
    except (OSError, ValueError) as exc:
        print(f"✗ 无法建立运行计时: {exc}")
        if new_session_started:
            close_runtime("aborted", reason="runtime start failed")
        return 1
    try:
        serial_identity = _serial_runtime_identity(runtime) if allow_serial else None
    except (TypeError, ValueError) as exc:
        close_runtime("aborted", reason="serial runtime identity invalid")
        print(f"✗ 串行授权运行身份无效: {exc}")
        return 1
    try:
        supervisor_state = ensure_supervisor(
            allow_serial, serial_reason, lifecycle_id=runtime.get("lifecycle_id"),
            session_id=(serial_identity["session_id"] if serial_identity
                        else runtime.get("session_id")),
            life_id=(serial_identity["life_id"] if serial_identity
                     else (f"life-{runtime.get('life_no')}"
                           if runtime.get("life_no") is not None else None)),
            life_kind=life_kind,
            budget_override=(
                {"max_round_wall_seconds": st.max_round_wall_seconds}
                if st.max_round_wall_seconds is not None else None
            ),
        )
    except (OSError, ValueError) as exc:
        close_runtime("aborted", reason="Supervisor setup failed")
        print(f"✗ 并发调度门禁未建立: {exc}")
        return 1
    if allow_serial:
        try:
            from core.supervisor.serial_authorization import (
                issue_serial_authorization,
                validate_serial_authorization,
            )
            from core.launch_phases.supervisor_bridge import get_supervisor

            supervisor = get_supervisor()
            locked_state = supervisor.status()
            expected = _serial_authorization_intent(
                runtime, locked_state, str(serial_reason),
            )
            record = _persist_serial_authorization_record(runtime, expected, memo)
            identity = expected["identity"]
            identity_args = {
                key: value for key, value in identity.items() if key != "life_kind"
            }
            if record.get("status") == "authorized":
                receipt = record.get("receipt")
                validate_serial_authorization(
                    locked_state, receipt,
                    **identity_args,
                    life_kind=identity["life_kind"],
                    host=runtime.get("host"),
                    policy_hash=expected["policy_hash"],
                )
            else:
                receipt = issue_serial_authorization(
                    supervisor,
                    **identity_args,
                    life_kind=identity["life_kind"],
                    host=runtime.get("host"),
                    policy_hash=expected["policy_hash"],
                    expected_revision=locked_state.revision,
                )
                validate_serial_authorization(
                    supervisor.status(), receipt,
                    **identity_args,
                    life_kind=identity["life_kind"],
                    host=runtime.get("host"),
                    policy_hash=expected["policy_hash"],
                )
                _persist_serial_authorization_record(
                    runtime, expected, memo, receipt=receipt,
                )
            runtime = memo.get()
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError) as exc:
            close_runtime("aborted", reason="serial authorization failed")
            print(f"✗ 串行授权未能绑定到当前 Supervisor: {exc}")
            return 1
    _seed_attack_graph(supervisor_state, runtime)
    st.runtime = runtime
    st.started = started
    st.supervisor_state = supervisor_state
    st.lesson_context = lesson_ctx
    return 0


def _capacity_forecast_warnings(supervisor_state: dict) -> list[str]:
    """DOC-203 D1-a：post 覆盖门禁的容量可行性预报（warn-only，不阻断）。

    估计 ``wall × concurrency × 有效吞吐折扣 ÷ 单元耗时`` 能否覆盖清单。
    单元耗时优先取本项目历史 Supervisor claims 的执行时长中位数
    （样本须有限正数且 ≤ 2×MAX_LEASE_SECONDS，<3 个样本回退 1200s 假设值）；
    历史数据是仓库内状态文件（不可信输入），所以结论只作 warn 输出、
    显式标注来源，绝不写入 gate 或预算。``max_concurrency`` 为 auto/缺失
    时无法估算，直接跳过。
    """
    coverage = supervisor_state.get("coverage") or {}
    units_total = len(coverage) if isinstance(coverage, Mapping) else 0
    budget = supervisor_state.get("budget") or {}
    wall = budget.get("max_wall_seconds")
    concurrency = budget.get("max_concurrency")
    try:
        wall_f = float(wall)
        conc_f = float(concurrency)
    except (TypeError, ValueError):
        return []
    if not (math.isfinite(wall_f) and wall_f > 0):
        return []
    if not (math.isfinite(conc_f) and conc_f > 0):
        return []
    per_unit_s, sample_note, sample_count = _median_unit_seconds()
    effective_ratio = 0.55  # 有效并发折扣：租约重叠/接手损耗的经验上界
    expected = int(wall_f * conc_f * effective_ratio / per_unit_s)
    if units_total <= expected:
        return []
    need_wall = units_total * per_unit_s / (conc_f * effective_ratio)
    need_conc = units_total * per_unit_s / (wall_f * effective_ratio)
    return [
        f"  [pre] ⚠ 容量预报：{units_total} 个覆盖单元 vs 预计本 run 可覆盖 ≈{expected} 单元"
        f"（单耗时中位数 {per_unit_s:.0f}s{sample_note} × 并发 {conc_f:.0f} × 有效吞吐 {effective_ratio:.2f}"
        f" / wall {wall_f:.0f}s；样本 {sample_count} 个）",
        f"  [pre] ⚠ 按当前预算本 run 大概率以 INCOMPLETE 收口；全覆盖需 wall≈{need_wall:.0f}s"
        f" 或 max_concurrency≈{need_conc:.1f}。接受诚实的不完整收口时，"
        "post 显式加 --allow-incomplete（详见 --help）。",
    ]


def _median_unit_seconds() -> tuple[float, str, int]:
    """历史单元执行耗时中位数；不可信输入全部过滤（DOC-203 D1-a 红队修订）。"""
    import re as _re

    default_seconds = 1200.0
    root = resolve_project_root()
    candidates = sorted(
        (pollings_root(root)).glob(
            "polling-*/lifecycles/*/state/supervisor.json",
        ),
        reverse=True,
    )
    samples: list[float] = []
    source_note = ""
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        claims = data.get("claims") if isinstance(data, Mapping) else None
        if not isinstance(claims, Mapping):
            continue
        for claim in claims.values():
            if not isinstance(claim, Mapping):
                continue
            started_at = claim.get("execution_started_at")
            finished_at = claim.get("execution_finished_at")
            try:
                duration = float(finished_at) - float(started_at)
            except (TypeError, ValueError):
                continue
            # 恶意仓库可预埋 supervisor.json（hash-chain 只护 events）；
            # 非有限/非正/超长样本一律丢弃，防预报被整批假样本操纵。
            if not math.isfinite(duration) or duration <= 0:
                continue
            if duration > 2 * 3600.0:
                continue
            samples.append(duration)
        if len(samples) >= 3:
            source_note = f"，来源 {path.parents[3].name if len(path.parents) > 3 else path.parent.name}"
            break
    if len(samples) < 3:
        return default_seconds, "（假设值，无足够历史样本）", len(samples)
    samples.sort()
    mid = len(samples) // 2
    median = (
        samples[mid] if len(samples) % 2 else (samples[mid - 1] + samples[mid]) / 2
    )
    return median, source_note, len(samples)


def _print_pre_ready_summary(st: "_PreState", memo: "_RuntimeMemo") -> int:
    """启动就绪输出：TODO 刷新、经验卡片提示与防篡改外部基线导出。

    原 ``pre()`` 918-989 逐字迁移；块尾自带 ``return 0``。"""
    supervisor_state = st.supervisor_state
    runtime = st.runtime
    started = st.started
    lesson_ctx = st.lesson_context
    try:
        # ``ensure_supervisor`` 不写 runtime.json，直接复用刚保存的快照。
        todo_runtime = memo.get()
    except (OSError, ValueError):
        todo_runtime = runtime
    todo_path = refresh_audit_todo(todo_runtime, "running")
    refresh_startup_todo(
        "mining",
        reason="工具、知识、覆盖和运行身份已准备完成，进入漏洞挖掘",
        environment=detect_startup_environment(),
    )
    if started:
        try:
            reset_segment_timer()
        except (OSError, ValueError) as exc:
            print(f"[launch_bugx] 计时重置失败: {exc}")
    else:
        print("  运行计时：保留原开始时间")
    print("-" * 56)
    print("✓ 基线就绪。下一步生成并发 worker 并同时发起 Task：")
    print("    bugx engine --root . workers")
    print(
        f"    → 把输出的 N 个提示词并发交给 Task（subagent_type: "
        f"{worker_subagent_type(todo_runtime.get('subagent_type'))}，"
    )
    print("      每个 worker 领取独立 coverage unit；至少 2 个真实重叠；")
    print("      若该类型缺少 bugx-supervisor-worker_* MCP 工具，宿主必须改派")
    print("      具备 MCP 的通用类型（如 general-purpose）重发，禁止降级 CLI；")
    print("      可在当前会话 runtime context 中指定类型）")
    print(
        f"  Supervisor 已就绪：{len(supervisor_state.get('coverage', {}))} 个覆盖单元；"
        f"max_concurrency={supervisor_state.get('budget', {}).get('max_concurrency', 'auto')}"
    )
    for warning in _capacity_forecast_warnings(supervisor_state):
        print(warning)
    if todo_path:
        print(f"  当前 Round TODO 清单：{todo_path}")
    lesson_data = lesson_ctx or {}
    print(
        f"  复盘账本已校验：revision={lesson_data.get('revision')}，"
        f"active_lessons={lesson_data.get('lessons_active', 0)}；"
        "本轮结束前必须提交结构化 cycle-review。"
    )
    if lesson_data.get("recommendations"):
        print("  优先经验排查点：" + "；".join(
            f"{item.get('pattern_id') or item.get('lesson_id')} {item['title']}"
            for item in lesson_data["recommendations"]
        ))
    if lesson_data.get("tool_cards"):
        print("  工具经验提示：" + "；".join(
            f"{item['tool_name']}({item.get('experience_level')}"
            + (f"，" + "、".join(
                f"{cat.get('category')}×{cat.get('hits')}"
                for cat in (item.get("top_categories") or [])[:2]
            ) if item.get("top_categories") else "")
            + f"，命中{item.get('hit_count', 0)})"
            for item in lesson_data["tool_cards"]
        ))
    budget = lesson_data.get("context_budget", {})
    print(
        f"  经验上下文预算：{budget.get('tokens', 0)}/"
        f"{budget.get('max_tokens', LESSON_CONTEXT_TOKEN_BUDGET)} tokens，"
        f"{budget.get('bytes', 0)}/{budget.get('max_bytes', LESSON_CONTEXT_BYTE_BUDGET)} bytes，"
        f"{len(lesson_data.get('recommendations', []))} 张卡片"
    )
    print("  必须并发 claim 多个无资源冲突 unit；每个 worker 只写隔离 artifact 并提交 checkpoint。")
    print("  Life 结束后执行：bugx engine --root . post")
    print()
    print("  防篡改外部基线（复制下面 export 行，agent 结束后在 post 前执行，")
    print("  或手动传给 post——agent 无法篡改外部基线）：")
    baseline = baseline_json()
    if baseline:
        baseline_file = None
        if uses_project_layout():
            try:
                state_dir = resolve_project_root() / ".bugx" / "state"
                state_dir.mkdir(parents=True, exist_ok=True)
                candidate = state_dir / "pre-baseline.json"
                # DOC-236 C4：baseline 导出同样必须原子写，避免中断留下半个 JSON。
                secure_atomic.atomic_write_json(candidate, baseline, mode=0o600, parent_mode=0o700, indent=1)
                baseline_file = candidate
            except Exception:
                baseline_file = None

        if baseline_file:
            print(f"  export BH_PRE_BASELINE='{baseline_file}'")
        else:
            escaped = json.dumps(baseline, ensure_ascii=False)
            print(f"  export BH_PRE_BASELINE='{escaped}'")
    if started:
        print(f"  计时已开始: {runtime['started_at']}")
        print(f"  Run ID: {runtime['run_id']}")
    else:
        state = "暂停" if runtime["state"] == "paused" else "运行"
        print(f"  检测到未结束的{state}状态，保留原开始时间继续累计。")
    return 0


def _persist_pre_failure_reason(detail: str) -> None:
    """E6a：进程内 pre 失败原因写入 common 存储（orchestrator 才有 detail）。

    ``pre``/``audit`` 不经过 ``run_child``，``_LAST_CHILD_FAILURE`` 恒空 →
    ``pre_failed.failure_detail`` 永远兜底文案。非零返回前调用本函数写入。
    """
    set_last_child_failure(detail)


def pre(polling_policy: str | None = None,
        allow_serial: bool = False,
        serial_reason: str | None = None,
        lesson_query: str = "",
        lesson_category: str | None = None,
        review_mode: str | None = None,
        fix_mode: str | None = None,
        life_kind: str = "mine",
        commit_policy: str | None = None,
        session_recovery: str | None = None,
        startup_action: str | None = None,
        strategy_plan: str | None = None,
        tooling_choices: dict[str, str] | None = None,
        report_policy: str | None = None, replay_share_pending_force: bool = False,
        startup_interaction: str | None = None,
        bootstrap_coverage: bool = False,
        profile: str | None = None,
        ignore_global_budget: bool = False,
        refresh_probes: bool = False,
        autonomous: bool = False,
        max_round_wall_seconds: str | float | None = None,
        host: str | None = None) -> int:
    """读取已分层的启动配置并执行无需交互的准备流程。

    参数校验和不可恢复的完整性错误仍会返回非零码；可选工具缺失会记录
    内置回退并继续，不生成会话内决策问卷。
    """
    runtime_context = load_runtime(reconcile=False)
    selected_host = host or runtime_context.get("host")
    if not selected_host:
        try:
            from core.supervisor.supervisor_host import detect_host
            selected_host = detect_host(resolve_project_root()).name
        except (ImportError, OSError, RuntimeError, ValueError):
            selected_host = "unknown"
    if host_serial_worker(selected_host):
        # DOC-239：串行性来自插件；理由正文用宿主展示名生成（不再硬编码宿主名）。
        allow_serial = True
        serial_host_reason = (
            f"{host_display_name(selected_host)} 未提供 BugX Task Worker handoff；"
            "由主会话通过 Worker MCP 串行提交 checkpoint"
        )
        serial_reason = "; ".join(
            reason for reason in (serial_reason, serial_host_reason)
            if isinstance(reason, str) and reason.strip()
        )
    st = _PreState(
        host=str(selected_host).strip().casefold(),
        polling_policy=polling_policy,
        review_mode=review_mode,
        fix_mode=fix_mode,
        commit_policy=commit_policy,
        session_recovery=session_recovery,
        startup_action=startup_action,
        strategy_plan=strategy_plan,
        life_kind=life_kind,
        startup_interaction=startup_interaction,
        report_policy=report_policy,
        replay_share_pending_force=replay_share_pending_force,
        profile=profile,
        ignore_global_budget=ignore_global_budget, refresh_probes=refresh_probes,
        autonomous_mode=autonomous,
        max_round_wall_seconds=max_round_wall_seconds,
        tooling_choices=tooling_choices,
        lesson_query=lesson_query,
        lesson_category=lesson_category,
        allow_serial=allow_serial,
        serial_reason=serial_reason,
        bootstrap_coverage=bootstrap_coverage,
    )
    memo = _RuntimeMemo()

    code = _normalize_pre_options(st)
    if code:
        return code
    code = _run_preflight_budget_gates(st)
    if code:
        return code
    code = _resolve_resume_state(st, memo)
    if code:
        return code
    code = _derive_and_persist_decisions(st, memo)
    if code:
        return code
    # DOC-236 C7：MCP 门禁改为三态——"需重启宿主"必须与"需人决策"分离。
    mcp_gate = supervisor_mcp_gate(
        review_mode=st.review_mode or st.current_review_mode,
        autonomous=st.autonomous_mode,
    )
    if mcp_gate == "restart_required":
        _persist_pre_failure_reason("RESTART_REQUIRED (exit 3)：宿主入口已配置但未重启加载")
        return RESTART_REQUIRED
    if mcp_gate != "ok":
        _persist_pre_failure_reason(
            "POLLING_DECISION_REQUIRED (exit 2)：轮询策略需要用户决策"
            "（auto/manual 二选一）",
        )
        return POLLING_DECISION_REQUIRED
    # 宿主侧入口（MCP + skill 入口文件）同周期就绪性检查：未就绪 ⇒ 独立终态。
    entry_gate = host_entry_gate()
    if entry_gate:
        _persist_pre_failure_reason(
            f"host_entry_not_ready (exit {entry_gate})：宿主入口未同周期就绪，"
            "重启宿主或重新 bugx install 后重试",
        )
        return entry_gate
    try:
        st.project_profile_data = _prepare_project_profile()
        st.project_profile_prepared = True
        # The tool gate must consume the current startup's skill plan, never a
        # cached snapshot from before this project-profile compilation.
        memo.get(refresh=True)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"✗ 项目画像准备失败：{exc}")
        return 1
    code = _run_tools_and_preflight_gates(st, memo)
    if code:
        return code
    # DOC-051 F8 顺序守卫：恢复性共享必须在下一个 Session 的覆盖清单门禁
    # **之前**重放。既有用例
    # ``test_authorized_share_replay_precedes_new_coverage_gate`` 直接解析
    # ``pre()`` 体内的调用序列来断言这一先后关系，因此这两步刻意保留在编排层
    # 内联（而不是下沉为步骤函数），以免顺序观测点消失。
    replay_share_pending(force=st.replay_share_pending_force)
    code = _start_or_resume_session(st, memo)
    if code:
        return code
    if st.bootstrap_coverage:
        coverage_gate = coverage_startup_gate(
            bootstrap_confirmed=True, autonomous=st.autonomous_mode,
        )
    else:
        coverage_gate = coverage_startup_gate(autonomous=st.autonomous_mode)
    if coverage_gate != 0:
        if st.new_session_started:
            close_runtime("aborted", reason="coverage startup gate failed")
        return coverage_gate
    if run_verify_life("snapshot") != 0:
        print("✗ 快照建立失败")
        if st.new_session_started:
            close_runtime("aborted", reason="snapshot failed")
        return 1
    code = _prepare_runtime_and_supervisor(st, memo)
    if code:
        return code
    if st.autonomous_mode and _ensure_autonomous_prep_record() != 0:
        if st.new_session_started:
            close_runtime("aborted", reason="autonomous prep record failed")
        return 1
    return _print_pre_ready_summary(st, memo)
