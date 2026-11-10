#!/usr/bin/env python3
"""launch_phases/pre_state_builder.py - 选项归一化、预算门禁、会话状态准备与凭据授权。"""

from __future__ import annotations

import collections
import dataclasses
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping

try:
    import core.i18n as i18n
except ImportError:
    try:
        from core import i18n
    except ImportError:
        i18n = None  # type: ignore[assignment]

from core.launch_phases.common import (
    FIX_MODES,
    resolve_project_root,
    uses_project_layout,
    child_environment,
    run_child,
    POLLING_DECISION_REQUIRED,
    REVIEW_DECISION_LATER,
    REPORT_POLICY_DEFAULT,
    STRATEGY_PLAN_ACTIONS,
    COMMIT_POLICIES,
    REVIEW_MODES,
    STARTUP_INTERACTION_CHOICES,
    STARTUP_INTERACTION_MODES,
    host_serial_worker,
    run_verify_life,
    bugx_paths,
)
from core.launch_phases.session_runtime import (
    set_review_mode,
    set_polling_policy,
    set_commit_policy,
    load_runtime,
    save_runtime,
    iso_time,
    runtime_lock,
    start_runtime,
    refresh_runtime_paths,
)
from core.launch_phases.supervisor_bridge import (
    run_tools_kb,
    degraded_gate,
    supervisor_mcp_gate,
    auto_provision_accelerators,
    autonomous_degraded_choices,
    report_degraded_probes,
    run_preflight_probes,
)
from core.launch_phases.report_sharing import normalize_report_policy
from core.launch_phases.startup_gates import (
    compile_strategy_plan as _orig_compile_strategy_plan,
    strategy_plan_gate as _orig_strategy_plan_gate,
    report_policy_startup_gate as _orig_report_policy_startup_gate,
    aborted_session_gate,
    check_global_budget_gate,
    check_stage_prompts_integrity,
    coverage_startup_gate,
    mark_strategy_customized,
    refresh_startup_todo,
)

#: 运行态 ``report_policy_source`` 的「用户已决策」取值集合。
#: 命中即表示交互三选一门禁已有答案，不再重复询问；``default`` /
#: ``autonomous`` 是系统兜底，不算用户决策。
_REPORT_POLICY_DECIDED_SOURCES = frozenset({"cli", "profile", "config", "gate"})

def compile_strategy_plan(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "compile_strategy_plan"):
        fn = getattr(pp, "compile_strategy_plan")
        if fn is not compile_strategy_plan:
            return fn(*args, **kwargs)
    return _orig_compile_strategy_plan(*args, **kwargs)

def strategy_plan_gate(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "strategy_plan_gate"):
        fn = getattr(pp, "strategy_plan_gate")
        if fn is not strategy_plan_gate:
            return fn(*args, **kwargs)
    return _orig_strategy_plan_gate(*args, **kwargs)

def report_policy_startup_gate(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "report_policy_startup_gate"):
        fn = getattr(pp, "report_policy_startup_gate")
        if fn is not report_policy_startup_gate:
            return fn(*args, **kwargs)
    return _orig_report_policy_startup_gate(*args, **kwargs)

def startup_policy_explicit_keys(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "startup_policy_explicit_keys"):
        fn = getattr(pp, "startup_policy_explicit_keys")
        if fn is not startup_policy_explicit_keys:
            return fn(*args, **kwargs)
    return frozenset()


def baseline_json(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "baseline_json"):
        fn = getattr(pp, "baseline_json")
        if fn is not baseline_json:
            return fn(*args, **kwargs)
    from core.launch_phases.session_runtime import baseline_json as real
    return real(*args, **kwargs)

def _startup_policy_config(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "_startup_policy_config"):
        fn = getattr(pp, "_startup_policy_config")
        if fn is not _startup_policy_config:
            return fn(*args, **kwargs)
    raise RuntimeError("pre_phase._startup_policy_config not available")

def resolve_project_root(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "resolve_project_root"):
        fn = getattr(pp, "resolve_project_root")
        if fn is not resolve_project_root:
            return fn(*args, **kwargs)
    from core.launch_phases.common import resolve_project_root as real
    return real(*args, **kwargs)

def load_runtime(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "load_runtime"):
        fn = getattr(pp, "load_runtime")
        if fn is not load_runtime:
            return fn(*args, **kwargs)
    from core.launch_phases.session_runtime import load_runtime as real
    return real(*args, **kwargs)

def save_runtime(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "save_runtime"):
        fn = getattr(pp, "save_runtime")
        if fn is not save_runtime:
            return fn(*args, **kwargs)
    from core.launch_phases.session_runtime import save_runtime as real
    return real(*args, **kwargs)


def detect_startup_environment(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "detect_startup_environment"):
        fn = getattr(pp, "detect_startup_environment")
        if fn is not detect_startup_environment:
            return fn(*args, **kwargs)
    try:
        from core.launch_phases.startup_gates import detect_startup_environment as real
        return real(*args, **kwargs)
    except Exception:
        return "terminal"

def run_tools_kb(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "run_tools_kb"):
        fn = getattr(pp, "run_tools_kb")
        if fn is not run_tools_kb:
            return fn(*args, **kwargs)
    from core.launch_phases.supervisor_bridge import run_tools_kb as real
    return real(*args, **kwargs)

def run_preflight_probes(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "run_preflight_probes"):
        fn = getattr(pp, "run_preflight_probes")
        if fn is not run_preflight_probes:
            return fn(*args, **kwargs)
    from core.launch_phases.supervisor_bridge import run_preflight_probes as real
    return real(*args, **kwargs)

def report_degraded_probes(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "report_degraded_probes"):
        fn = getattr(pp, "report_degraded_probes")
        if fn is not report_degraded_probes:
            return fn(*args, **kwargs)
    from core.launch_phases.supervisor_bridge import report_degraded_probes as real
    return real(*args, **kwargs)

def auto_provision_accelerators(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "auto_provision_accelerators"):
        fn = getattr(pp, "auto_provision_accelerators")
        if fn is not auto_provision_accelerators:
            return fn(*args, **kwargs)
    from core.launch_phases.supervisor_bridge import auto_provision_accelerators as real
    return real(*args, **kwargs)

def degraded_gate(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "degraded_gate"):
        fn = getattr(pp, "degraded_gate")
        if fn is not degraded_gate:
            return fn(*args, **kwargs)
    from core.launch_phases.supervisor_bridge import degraded_gate as real
    return real(*args, **kwargs)

def run_verify_life(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "run_verify_life"):
        fn = getattr(pp, "run_verify_life")
        if fn is not run_verify_life:
            return fn(*args, **kwargs)
    from core.launch_phases.common import run_verify_life as real
    return real(*args, **kwargs)

def refresh_startup_todo(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "refresh_startup_todo"):
        fn = getattr(pp, "refresh_startup_todo")
        if fn is not refresh_startup_todo:
            return fn(*args, **kwargs)
    from core.launch_phases.startup_gates import refresh_startup_todo as real
    return real(*args, **kwargs)

def uses_project_layout(*args, **kwargs):
    pp = _pp()
    if pp and hasattr(pp, "uses_project_layout"):
        fn = getattr(pp, "uses_project_layout")
        if fn is not uses_project_layout:
            return fn(*args, **kwargs)
    from core.launch_phases.common import uses_project_layout as real
    return real(*args, **kwargs)

def _pp():
    return sys.modules.get("core.launch_phases.pre_phase") or sys.modules.get("launch_phases.pre_phase")

def _get_pp_attr(name: str, fallback=None):
    pp = _pp()
    if pp and hasattr(pp, name):
        return getattr(pp, name)
    return fallback

def begin_new_session(
    bootstrap_coverage: bool = False, *, autonomous: bool = False,
) -> tuple[dict, bool, int | None]:
    """在已完成 Session 后创建新的 Session/首个 Round。

    新 Session 的覆盖清单门禁必须先于运行态创建。清单缺失时可以让新
    Session 停留在 ``created``/待引导状态，但不能先写入 running runtime
    再把它错误收尾为 ``aborted``。返回值中的第三项用于把结构化门禁码
    交回 ``pre``，避免把“等待用户选择”误报成创建失败。
    """
    if not uses_project_layout() or bugx_paths is None:
        runtime, started = start_runtime(new_lifecycle=True, new_session=True)
        return runtime, started, None
    previous = load_runtime()
    previous_mode = (baseline_json() or {}).get("mode")
    inherited = {
        key: previous.get(key)
        for key in (
            "polling_policy", "review_mode", "review_mode_deferred",
            "commit_policy", "report_policy", "sync_status",
        )
    }
    try:
        bugx_paths.create_session(resolve_project_root())
        refresh_runtime_paths()
        # create_session() has switched current.json to the new lifecycle, but
        # no runtime has been created yet.  Check the new lifecycle's coverage
        # gate at this safe boundary BEFORE resetting the life ledger: a
        # blocked/missing manifest must not consume the reset or clear the
        # previous runnable state, and a fresh Session must not be turned
        # into an aborted run by a missing manifest.
        coverage_gate = coverage_startup_gate(
            bootstrap_confirmed=bootstrap_coverage, autonomous=autonomous,
        )
        if coverage_gate != 0:
            return load_runtime(), False, coverage_gate
        if run_verify_life("reset", "--confirm-reset") != 0:
            return load_runtime(), False, 1
        if previous_mode in {"auto", "log-only"}:
            if run_verify_life("set-mode", "--mode", previous_mode) != 0:
                return load_runtime(), False, 1
        # create_session() has already switched current.json to the new
        # polling.  Explicitly pass new_session so runtime identity, reports,
        # and lifecycle history cannot accidentally remain attached to the
        # completed/aborted Session from the previous startup.
        runtime, started = start_runtime(new_lifecycle=True, new_session=True)
        if started:
            runtime.update({key: value for key, value in inherited.items() if value is not None})
            runtime["startup_observation"] = "completed"
            runtime["startup_intent"] = "new_session"
            save_runtime(runtime)
        return runtime, started, None
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[launch_bugx] ✗ 新 Session 创建失败: {exc}")
        return previous, False, 1


class RuntimeMemo:
    """PERF-05：``pre()`` 内单一 runtime 快照，仅在已知写操作后显式刷新。

    未刷新时返回同一对象，因此不会在写操作之后继续使用陈旧数据；
    拆分为步骤函数后以显式对象传递，保持与原闭包完全一致的语义。
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: dict | None = None

    def get(self, *, refresh: bool = False) -> dict:
        """返回当前快照；``refresh=True`` 时强制重新读盘。"""
        if refresh or self._state is None:
            self._state = load_runtime()
        return self._state

    def seed(self, state: dict) -> None:
        """在已知写操作之后接管新快照（等价于原 ``runtime_state = runtime``）。"""
        self._state = state


@dataclass
class PreState:
    """``pre()`` 门禁序列在步骤函数之间流转的可变状态载体。

    原先 600+ 行函数体里的启动决策变量集中到此对象，使每个门禁块
    可以独立成函数而不必以 8+ 个位置参数互相传递。字段语义与原始
    局部变量逐字对应，赋值顺序不变。
    """

    # 命令行显式参数（归一化后的值）
    host: str | None = None
    polling_policy: str | None = None
    review_mode: str | None = None
    review_mode_deferred: bool = False
    fix_mode: str | None = None
    commit_policy: str | None = None
    session_recovery: str | None = None
    startup_action: str | None = None
    report_policy: str | None = None
    # report_policy 的取值来源（cli/profile/config/gate/autonomous/default），
    # 持久化为 runtime["report_policy_source"]，用于判定三选一门禁是否已作答。
    report_policy_source: str = "default"
    # ``--replay-share-pending``：显式授权在 report_policy=local 下重放队列。
    replay_share_pending_force: bool = False
    strategy_plan: str | None = None
    life_kind: str = "mine"
    startup_interaction: str | None = None
    profile: str | None = None
    ignore_global_budget: bool = False
    refresh_probes: bool = False
    autonomous_mode: bool = False
    max_round_wall_seconds: str | float | None = None
    autonomous_policy: dict[str, Any] | None = None
    tooling_choices: dict[str, str] | None = None
    lesson_query: str = ""
    lesson_category: str | None = None
    allow_serial: bool = False
    serial_reason: str | None = None
    bootstrap_coverage: bool = False
    # 派生标志
    noninteractive_startup: bool = False
    # 运行态现值
    current_runtime: dict = field(default_factory=dict)
    current_review_mode: str | None = None
    current_review_deferred: bool = False
    current_policy: str | None = None
    current_fix_mode: str | None = None
    current_commit_policy: str | None = None
    life_state: dict = field(default_factory=dict)
    aborted_new_session: bool = False
    # 会话启动与就绪输出
    new_session_started: bool = False
    runtime: dict = field(default_factory=dict)
    started: bool = False
    supervisor_state: dict = field(default_factory=dict)
    lesson_context: dict = field(default_factory=dict)
    project_profile_prepared: bool = False
    project_profile_data: dict[str, Any] | None = None


def normalize_pre_options(st: "_PreState") -> int:
    """参数校验与归一化：profile 回填 + 逐项合法性门禁。

    原 ``pre()`` 393-474 逐字迁移；仅把原先的函数局部变量换成
    ``_PreState`` 字段，校验顺序、错误文案与退出码完全不变。"""
    profile = st.profile
    polling_policy = st.polling_policy
    review_mode = st.review_mode
    fix_mode = st.fix_mode
    commit_policy = st.commit_policy
    session_recovery = st.session_recovery
    startup_action = st.startup_action
    report_policy = st.report_policy
    strategy_plan = st.strategy_plan
    life_kind = st.life_kind
    startup_interaction = st.startup_interaction
    review_mode_deferred = st.review_mode_deferred
    cli_policy = {
        "polling_policy": st.polling_policy,
        "review_mode": st.review_mode,
        "fix_mode": st.fix_mode,
        "commit_policy": st.commit_policy,
        "session_recovery": st.session_recovery,
        "startup_action": st.startup_action,
        "report_policy": st.report_policy,
    }
    profile_cfg = {}
    if st.max_round_wall_seconds is not None:
        try:
            st.max_round_wall_seconds = float(st.max_round_wall_seconds)
        except (TypeError, ValueError):
            st.max_round_wall_seconds = None
        if (st.max_round_wall_seconds is None
                or not math.isfinite(st.max_round_wall_seconds)
                or st.max_round_wall_seconds <= 0):
            print("[launch_bugx] ✗ --max-round-wall-seconds 必须是正的有限秒数")
            return 2
    try:
        from core.runtime import session_profile
    except Exception:
        if st.autonomous_mode:
            print("[launch_bugx] ✗ 自治策略无效，拒绝猜测默认值")
            return 2
        profile_cfg = {}
    else:
        try:
            profile_cfg = session_profile.load_profile(profile, root=resolve_project_root()) or {}
            if st.autonomous_mode:
                # Explicit CLI opt-in uses the audited preset, while a project
                # profile may override only already-defined scalar fields.
                preset = session_profile.load_profile("autonomous", root=resolve_project_root()) or {}
                preset.update(profile_cfg)
                profile_cfg = preset
            st.autonomous_policy = session_profile.autonomous_policy(profile_cfg)
        except session_profile.LegacySharePolicyError as exc:
            print(f"[launch_bugx] ✗ 已移除的启动配置：{exc}")
            return 2
        except Exception:
            if st.autonomous_mode:
                print("[launch_bugx] ✗ 自治策略无效，拒绝猜测默认值")
                return 2
            profile_cfg = {}

    if st.autonomous_policy is not None:
        st.autonomous_mode = True

    if profile_cfg:
        if polling_policy is None and profile_cfg.get("polling_policy"):
            polling_policy = profile_cfg["polling_policy"]
        if review_mode is None and profile_cfg.get("review_mode"):
            review_mode = profile_cfg["review_mode"]
        if fix_mode is None and profile_cfg.get("fix_mode"):
            fix_mode = profile_cfg["fix_mode"]
        if commit_policy is None and profile_cfg.get("commit_policy"):
            commit_policy = profile_cfg["commit_policy"]
        if session_recovery is None and profile_cfg.get("session_recovery"):
            session_recovery = profile_cfg["session_recovery"]
        if startup_action is None and profile_cfg.get("startup_action"):
            startup_action = profile_cfg["startup_action"]
        if report_policy is None and profile_cfg.get("report_policy"):
            report_policy = profile_cfg["report_policy"]
    if st.autonomous_mode:
        # In autonomous mode every strategy gate has a deterministic value;
        # explicit CLI values remain honored, but no missing value reaches an
        # interactive resolver.
        autonomous_defaults = {
            "polling_policy": "auto",
            "review_mode": "mcp-auto",
            "fix_mode": "auto",
            "commit_policy": "round",
            "session_recovery": "resume",
            "startup_action": "start",
            # DOC-236 C6：无人值守默认只保留本地报告（不对外提交）。
            "report_policy": "local",
            "strategy_plan": "approve",
        }
        polling_policy = polling_policy or autonomous_defaults["polling_policy"]
        review_mode = review_mode or autonomous_defaults["review_mode"]
        fix_mode = fix_mode or autonomous_defaults["fix_mode"]
        commit_policy = commit_policy or autonomous_defaults["commit_policy"]
        session_recovery = session_recovery or autonomous_defaults["session_recovery"]
        startup_action = startup_action or autonomous_defaults["startup_action"]
        report_policy = report_policy or autonomous_defaults["report_policy"]
        strategy_plan = strategy_plan or autonomous_defaults["strategy_plan"]
        st.bootstrap_coverage = bool(
            (st.autonomous_policy or {}).get("coverage_bootstrap", True)
        )

    try:
        layered = _startup_policy_config(resolve_project_root(), cli_policy)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 启动报告提交策略配置无效：{exc}")
        return 2
    for key in cli_policy:
        if cli_policy[key] is not None:
            continue
        profile_value = profile_cfg.get(key) if profile is not None else None
        value = profile_value if isinstance(profile_value, str) else layered.get(key)
        if key == "polling_policy":
            polling_policy = value
        elif key == "review_mode":
            review_mode = value
        elif key == "fix_mode":
            fix_mode = value
        elif key == "commit_policy":
            commit_policy = value
        elif key == "session_recovery":
            session_recovery = value
        elif key == "startup_action":
            startup_action = value

    # report_policy has an explicit source precedence that must not be
    # accidentally changed by the generic startup-policy loop:
    # CLI > selected/active profile > user/project config > safe default.
    if st.report_policy is not None:
        report_policy = st.report_policy
    else:
        profile_report_policy = profile_cfg.get("report_policy")
        if isinstance(profile_report_policy, str) and profile_report_policy.strip():
            report_policy = profile_report_policy
        else:
            report_policy = layered.get("report_policy", REPORT_POLICY_DEFAULT)

    environment = detect_startup_environment()
    terminal_interactive = bool(
        environment is not None
        and getattr(environment, "stdin_tty", False)
        and getattr(environment, "stdout_tty", False)
    )
    # 无 TTY 不再单独触发非交互启动：探针已识别 native-question 宿主时，
    # 策略门禁应经 BUGX_INTERACTION_REQUEST 交给宿主单选（与 git/云端门禁
    # 同一通道），而不是把推荐预设静默当作用户批准。真正的 headless/ACP/
    # blocked 交互面仍走自动只读路径（下方 interface/interaction 白名单判定）。
    noninteractive_startup = st.autonomous_mode or environment is None or (
        not terminal_interactive
        and getattr(environment, "interaction", None)
        not in {"native-question", "tty-radio", "text-menu"}
    )
    if noninteractive_startup:
        # Headless/ACP launchers cannot answer a strategy questionnaire. Keep
        # the automatic path read-only unless explicitly requested via CLI with approve.
        if st.strategy_plan != "approve":
            fix_mode = "log-only"
            commit_policy = "manual"
            strategy_plan = "approve"
        else:
            if fix_mode is None:
                fix_mode = "log-only"
            if commit_policy is None:
                commit_policy = "manual"
            if strategy_plan is None:
                strategy_plan = "approve"
    st.noninteractive_startup = noninteractive_startup
    if startup_interaction is not None:
        action_val = startup_interaction.strip().lower()
        # 两套值域曾经被错误合并，导致 ``--startup-interaction auto``（help 里
        # 白纸黑字声明的合法值）被判非法并返回 2，非交互启动直接失败：
        #   * ``auto|prompt``——本 flag 的原生语义：声明调用方如何应对启动门禁，
        #     它不是「对某个门禁的作答」，因此只影响是否走自动决策，见下方
        #     ``noninteractive_startup`` 的推导，不在这里短路退出。
        #   * ``restart_host|run_tty|later``——MCP 门禁的**作答**，才需要短路。
        if action_val in STARTUP_INTERACTION_MODES:
            pass
        elif action_val in STARTUP_INTERACTION_CHOICES or action_val in {"1", "2", "3"}:
            if action_val in {"later", "3"}:
                print("[launch_bugx] 已选择下次再说，本轮启动停止。")
                return POLLING_DECISION_REQUIRED
            elif action_val in {"restart_host", "1"}:
                print("[launch_bugx] 已选择重启宿主，请重启当前宿主会话后重试。")
                return POLLING_DECISION_REQUIRED
            elif action_val in {"run_tty", "2"}:
                print("[launch_bugx] 已选择在真实 TTY 中运行，请在外部终端中执行启动命令。")
                return POLLING_DECISION_REQUIRED
        else:
            valid = "、".join(sorted(STARTUP_INTERACTION_MODES | STARTUP_INTERACTION_CHOICES))
            print(f"[launch_bugx] ✗ 交互方式选择无效：只能是 {valid}")
            return 2

    # DOC-236 C1：`--review-mode later` 是"本轮不启动"。此时**任何运行态都不得
    # 落盘**——旧实现会先走到下面的 `report_policy` 持久化（`save_runtime`），
    # 于是"下次再说"仍然在磁盘上留下 runtime 状态。故此处前置短路，优先于持久化。
    if review_mode == REVIEW_DECISION_LATER:
        print("[launch_bugx] 已选择下次再说，本轮启动停止。")
        return POLLING_DECISION_REQUIRED

    # DOC-236 C6 补齐（2026-11-09 复盘 4.3-1）：report_policy 此前只有
    # CLI/config 入口，交互态用户被钉死在默认 local，无「不上报/上报云端」
    # 入口。这里先只判定**显式来源**；历史决策回读与三选一门禁必须等到
    # 下方全部参数校验通过之后执行（非法参数不得读 runtime、不得弹请求）。
    report_policy_source = "default"
    if st.report_policy is not None:
        report_policy_source = "cli"
    elif isinstance(profile_cfg.get("report_policy"), str) \
            and profile_cfg["report_policy"].strip():
        report_policy_source = "profile"
    elif "report_policy" in startup_policy_explicit_keys(resolve_project_root()):
        report_policy_source = "config"
    elif st.autonomous_mode:
        report_policy_source = "autonomous"

    # DOC-236 C6：非法值 fail-closed（不得静默回退默认值）；缺省值也必须
    # 在 L1 这里落盘，L5 不再猜测或回退未声明的提交策略。
    try:
        report_val = normalize_report_policy(report_policy)
    except ValueError as exc:
        print(f"[launch_bugx] ✗ 报告提交策略无效：{exc}")
        return 2
    report_val = report_val or REPORT_POLICY_DEFAULT

    if strategy_plan is not None:
        normalized_strat = strategy_plan.strip().lower()
        if normalized_strat not in STRATEGY_PLAN_ACTIONS and normalized_strat not in {"1", "2", "3"}:
            print("[launch_bugx] ✗ 作战方案策略无效：只能是 approve、customize 或 later")
            return 2

    review_mode_deferred = False
    if review_mode is not None and review_mode not in REVIEW_MODES:
        print("[launch_bugx] ✗ 启动前审查方式无效：只能是 manual 或 mcp-auto")
        return 2
    if fix_mode is not None and fix_mode not in FIX_MODES:
        print(
            "[launch_bugx] ✗ 启动前修复方式无效：只能是 "
            + " 或 ".join(FIX_MODES)
        )
        return 2
    if life_kind not in {"mine", "fix"}:
        print("[launch_bugx] ✗ Life 类型无效：只能是 mine 或 fix")
        return 2
    is_serial_host = host_serial_worker(st.host)
    if st.allow_serial and life_kind != "fix" and not is_serial_host:
        print("[launch_bugx] ✗ --allow-serial 仅允许用于 fix Life")
        return 2
    if st.allow_serial and (
        not isinstance(st.serial_reason, str) or not st.serial_reason.strip()
    ):
        print("[launch_bugx] ✗ --allow-serial 必须同时提供非空 --serial-reason")
        return 2
    if commit_policy is not None and commit_policy.strip().lower() in {
        "none", "no-submit", "no_submit", "不提交", "不自动提交",
    }:
        commit_policy = "manual"
    if commit_policy is not None and commit_policy not in COMMIT_POLICIES - {"not_applicable"}:
        print("[launch_bugx] ✗ 启动前提交策略无效：只能是 life、round、none 或 manual")
        return 2
    # 全部参数校验通过后才允许回读历史决策 / 发射三选一门禁。
    if report_policy_source == "default" and not noninteractive_startup:
        try:
            previous_runtime = load_runtime()
        except (OSError, ValueError):
            previous_runtime = {}
        inherited = previous_runtime.get("report_policy")
        if str(previous_runtime.get("report_policy_source") or "") \
                in _REPORT_POLICY_DECIDED_SOURCES \
                and isinstance(inherited, str) and inherited.strip():
            try:
                inherited_val = normalize_report_policy(inherited)
            except ValueError:
                inherited_val = None
            if inherited_val is not None:
                report_policy = inherited_val
                report_policy_source = "gate"
        if report_policy_source == "default":
            gate_code, gate_policy = report_policy_startup_gate(
                environment, autonomous=st.autonomous_mode,
            )
            if gate_code != 0:
                return gate_code
            report_policy = gate_policy
            report_policy_source = "gate"
        report_val = normalize_report_policy(report_policy) or REPORT_POLICY_DEFAULT
    # Validate the complete option set before the first runtime write. An
    # invalid authorization must fail without touching any project lifecycle.
    try:
        with runtime_lock():
            rt = load_runtime()
            rt["report_policy"] = report_val
            rt["report_policy_source"] = report_policy_source
            save_runtime(rt)
    except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"[launch_bugx] ✗ 报告提交策略持久化失败：{exc}（硬阻断，拒绝继续启动）")
        return 1
    report_policy = report_val
    st.polling_policy = polling_policy
    st.review_mode = review_mode
    st.fix_mode = fix_mode
    st.commit_policy = commit_policy
    st.session_recovery = session_recovery
    st.startup_action = startup_action
    st.report_policy = report_policy
    st.report_policy_source = report_policy_source
    st.strategy_plan = strategy_plan
    st.life_kind = life_kind
    st.review_mode_deferred = review_mode_deferred
    return 0


def run_preflight_budget_gates(st: "_PreState") -> int:
    """阶段提示词完整性防偷懒门禁 + 全局每日预算熔断门禁。

    原 ``pre()`` 476-496 逐字迁移。"""
    ignore_global_budget = st.ignore_global_budget
    # DOC-034 阶段 3：阶段提示词加载完整性防偷懒门禁（缺失立即阻断，不静默继续）
    check_prompts_fn = _get_pp_attr("check_stage_prompts_integrity", check_stage_prompts_integrity)
    prompts_ok, prompts_err = check_prompts_fn()
    if not prompts_ok:
        err = f"[launch_bugx] ✗ 阶段提示词章节缺失/加载失败: {prompts_err}（防偷懒门禁阻断）"
        if i18n is not None:
            err = i18n.exception_text("orchestrator.stage_prompts_failed", fallback=err, error=prompts_err)
        print(err)
        return 1

    # DOC-049 全局每日预算与配额熔断门禁（超限立即阻断，防止 API 账单穿透）
    check_budget_fn = _get_pp_attr("check_global_budget_gate", check_global_budget_gate)
    budget_ok, budget_err = check_budget_fn(ignore_budget=ignore_global_budget)
    if not budget_ok:
        err = f"[launch_bugx] ✗ 全局预算熔断阻断: {budget_err}"
        if i18n is not None:
            err = i18n.exception_text("orchestrator.budget_blocked", fallback=err, error=budget_err)
        print(err)
        return 1
    return 0


def resolve_resume_state(st: "_PreState", memo: "_RuntimeMemo") -> int:
    """读取运行态现值并恢复中断会话、持久化轮询策略。

    原 ``pre()`` 498-551 逐字迁移；``_runtime()`` 换为显式 memo 访问器，
    以保持 PERF-05 的单快照语义。"""
    review_mode = st.review_mode
    startup_action = st.startup_action
    polling_policy = st.polling_policy
    session_recovery = st.session_recovery
    review_mode_deferred = st.review_mode_deferred
    current_runtime = st.current_runtime
    current_review_mode = st.current_review_mode
    current_review_deferred = st.current_review_deferred
    current_policy = st.current_policy
    current_commit_policy = st.current_commit_policy
    aborted_new_session = st.aborted_new_session
    try:
        current_runtime = memo.get()
        current_policy = current_runtime.get("polling_policy")
        current_review_mode = current_runtime.get("review_mode")
        current_review_deferred = bool(
            current_runtime.get("review_mode_deferred", False)
        )
        current_commit_policy = current_runtime.get("commit_policy")
        if current_review_deferred and not current_runtime.get("active", False):
            current_review_mode = None
    except (OSError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 无法读取启动决策: {exc}")
        return 1

    if polling_policy is not None:
        try:
            set_polling_policy(polling_policy)
        except (OSError, TimeoutError, ValueError) as exc:
            print(f"[launch_bugx] ✗ 启动前轮询策略无效: {exc}")
            return 2
    if session_recovery is not None and session_recovery not in {"resume", "new"}:
        print("[launch_bugx] ✗ --session-recovery 只能是 resume 或 new")
        return 2
    if st.autonomous_mode:
        aborted_code, aborted_choice = aborted_session_gate(
            current_runtime, explicit_choice=session_recovery, autonomous=True,
        )
    else:
        # Keep the legacy call shape for host adapters that patch this gate.
        aborted_code, aborted_choice = aborted_session_gate(
            current_runtime, explicit_choice=session_recovery,
        )
    if aborted_code != 0:
        return aborted_code
    aborted_new_session = (aborted_choice == "new")
    if aborted_choice == "resume":
        print("↻ 已选择恢复上一次会话，继续启动流程。")
    st.current_runtime = current_runtime
    st.current_review_mode = current_review_mode
    st.current_review_deferred = current_review_deferred
    st.current_policy = current_policy
    st.current_commit_policy = current_commit_policy
    st.startup_action = startup_action
    st.review_mode = review_mode
    st.review_mode_deferred = review_mode_deferred
    st.aborted_new_session = aborted_new_session
    return 0


def derive_and_persist_decisions(st: "_PreState", memo: "_RuntimeMemo") -> int:
    """推导有效决策、执行作战方案策略门禁并持久化审查/轮询/提交/修复方式。

    原 ``pre()`` 553-625 逐字迁移。"""
    review_mode = st.review_mode
    review_mode_deferred = st.review_mode_deferred
    current_review_mode = st.current_review_mode
    polling_policy = st.polling_policy
    current_policy = st.current_policy
    fix_mode = st.fix_mode
    current_fix_mode = st.current_fix_mode
    commit_policy = st.commit_policy
    current_commit_policy = st.current_commit_policy
    strategy_plan = st.strategy_plan
    life_state = st.life_state
    current_review_deferred = st.current_review_deferred
    life_state = baseline_json() or {}
    current_fix_mode = life_state.get("mode")
    if current_fix_mode not in {"auto", "log-only"}:
        current_fix_mode = None
    if current_fix_mode == "log-only":
        current_commit_policy = "not_applicable"

    eff_review = review_mode or current_review_mode
    eff_polling = polling_policy or current_policy
    eff_fix = fix_mode or current_fix_mode
    eff_commit = commit_policy or current_commit_policy
    has_all_startup_decisions = (
        eff_review is not None
        and eff_polling is not None
        and eff_fix is not None
        and (eff_commit is not None or eff_fix == "log-only")
    )
    if (
        not st.noninteractive_startup
        and st.autonomous_mode
        and has_all_startup_decisions
        and strategy_plan is None
    ):
        # 期间只有 ``set_polling_policy`` 可能写盘，且不改 ``strategy_plan``；
        # 复用快照与重新读盘等价，因此无需再次读盘。
        current_strat = (memo.get().get("strategy_plan") or {}).get("status")
        if current_strat != "approved":
            mark_strategy_customized(compile_strategy_plan())
    else:
        plan_data = None
        if st.noninteractive_startup:
            plan_data = compile_strategy_plan(resolve_project_root())
            preset = dict(plan_data.get("recommended_preset") or {})
            preset.update(fix_mode="log-only", commit_policy="manual")
            plan_data["recommended_preset"] = preset
        strat_code, strat_decision = strategy_plan_gate(
            strategy_choice="approve" if st.noninteractive_startup else strategy_plan,
            environment=detect_startup_environment(),
            plan_data=plan_data,
            cli_fix_mode=fix_mode,
        )
        if strat_code != 0:
            return strat_code
        if strat_decision == "approved":
            # ``strategy_plan_gate`` 可能经 ``_apply_approved_strategy`` 写盘。
            # current_* 必须播种为盘面真实值（preset 已落盘），下方比较才能
            # 发现 CLI 显式值与盘面的差异并正确持久化——旧实现把 current_*
            # 播种成 CLI 值，显式 --review-mode 等被 preset 静默吞掉。
            current_runtime = memo.get(refresh=True)
            current_review_mode = current_runtime.get("review_mode")
            current_policy = current_runtime.get("polling_policy")
            current_commit_policy = current_runtime.get("commit_policy")
            current_fix_mode = (baseline_json() or {}).get("mode")
            if review_mode is None:
                review_mode = current_review_mode or "manual"
            if polling_policy is None:
                polling_policy = current_policy or "auto"
            if commit_policy is None:
                commit_policy = current_commit_policy or "round"
            if fix_mode is None:
                fix_mode = current_fix_mode or "auto"

    try:
        if review_mode is not None and current_review_mode != review_mode:
            set_review_mode(review_mode, deferred=review_mode_deferred)
            current_review_mode = review_mode
            current_review_deferred = review_mode_deferred
        elif review_mode is not None:
            current_review_deferred = review_mode_deferred
        if polling_policy is not None and current_policy != polling_policy:
            set_polling_policy(polling_policy)
            current_policy = polling_policy
        if commit_policy is not None and current_commit_policy != commit_policy:
            set_commit_policy(commit_policy)
            current_commit_policy = commit_policy
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 启动前策略无效: {exc}")
        return 2
    if fix_mode is not None and current_fix_mode != fix_mode:
        if run_verify_life("set-mode", "--mode", fix_mode, "--confirm") != 0:
            if run_verify_life("check") != 0 and run_verify_life("repair") == 0 and run_verify_life("check") == 0:
                print("→ 基线修复后重试修复方式设置…")
                if run_verify_life("set-mode", "--mode", fix_mode, "--confirm") != 0:
                    print(f"[launch_bugx] ✗ 修复方式持久化失败: {fix_mode}（硬阻断，拒绝继续启动）")
                    return 1
            else:
                print(f"[launch_bugx] ✗ 修复方式持久化失败: {fix_mode}（硬阻断，拒绝继续启动）")
                return 1
        current_fix_mode = fix_mode
    st.life_state = life_state
    st.current_fix_mode = current_fix_mode
    st.review_mode = review_mode
    st.polling_policy = polling_policy
    st.commit_policy = commit_policy
    st.fix_mode = fix_mode
    st.current_review_mode = current_review_mode
    st.current_review_deferred = current_review_deferred
    st.current_policy = current_policy
    st.current_commit_policy = current_commit_policy
    return 0


def run_tools_and_preflight_gates(st: "_PreState", memo: "_RuntimeMemo") -> int:
    """工具知识库校验、预飞探针扇出、降级门禁与基线 check/repair。

    原 ``pre()`` 769-828 逐字迁移。末尾的 ``_replay_share_pending()`` 刻意
    留在 ``pre()`` 编排层内联——见 ``pre()`` 内的说明。"""
    refresh_probes = st.refresh_probes
    tooling_choices = st.tooling_choices
    review_mode = st.review_mode
    current_review_mode = st.current_review_mode
    current_review_deferred = st.current_review_deferred
    refresh_startup_todo(
        "tools",
        reason="启动配置和最终启动动作已确认，进入工具与 MCP 准备",
        environment=detect_startup_environment(),
    )
    try:
        # ``supervisor_mcp_gate`` 可能经 ``set_review_mode`` 写盘，必须刷新。
        refreshed_runtime = memo.get(refresh=True)
        current_review_mode = refreshed_runtime.get("review_mode") or current_review_mode
        current_review_deferred = bool(
            refreshed_runtime.get("review_mode_deferred", current_review_deferred)
        )
        if current_review_mode == "manual" and current_review_deferred:
            review_mode = "manual"
    except (OSError, ValueError):
        pass

    print("=" * 56)
    print("bugx 启动前协议：校验基线 → 建立快照 → 输出外部基线")
    print("=" * 56)

    if run_tools_kb() != 0:
        print("✗ tools-kb 存在过期/缺日期条目，先搜索更新后再启动")
        return 1
    # PERF-09 / DES-13：启动前探针由 probe_registry 按 lifecycle="preflight"
    # 统一驱动，并在同一个并发扇出里各执行一次；不再手写 5 个顺序包装
    # （原先 5 个探针各起一个解释器，随后环境画像又把它们重跑一遍）。
    probe_result = run_preflight_probes(
        refresh=refresh_probes,
        include_profile=True,
        project_profile_data=st.project_profile_data,
    )
    if len(probe_result) == 3:
        probe_code, degraded_probes, preflight_profile = probe_result
    else:  # Preserve patched/legacy test doubles that return the public pair.
        probe_code, degraded_probes = probe_result
        preflight_profile = {}
    if probe_code != 0:
        return probe_code
    report_degraded_probes(degraded=degraded_probes)
    runtime_state = memo.get(refresh=True)
    audit_strategy = runtime_state.get("audit_strategy")
    skill_plan = (
        audit_strategy.get("skill_plan")
        if isinstance(audit_strategy, dict)
        else None
    )
    try:
        from core.governance import runtime_tool_gate

        selected_tool_ids = runtime_tool_gate.required_tool_ids_for_active_stages(
            skill_plan if isinstance(skill_plan, dict) else None,
        )
    except Exception as exc:
        print(f"✗ required Skill Pack stage tools 无法解析：{type(exc).__name__}: {exc}")
        return 1
    # DOC-180 / 反官僚主义：无论是否自治模式，优先尝试自愈安装/装配加速器，杜绝能自愈非要降级
    if degraded_probes:
        degraded_probes, provisioned = auto_provision_accelerators(
            resolve_project_root(), degraded_probes, required_tools=selected_tool_ids,
        )
        if provisioned:
            print("[launch_bugx] ✓ 环境加速器已就地自愈装配：" + "; ".join(provisioned))

    try:
        readiness_receipt = runtime_tool_gate.enforce_required_runtime_tools(
            resolve_project_root(),
            skill_plan if isinstance(skill_plan, dict) else None,
            runtime_state.get("project_classification"),
            preflight_profile,
        )
    except Exception as exc:
        print(f"✗ required Skill Pack runtime tool gate 异常：{type(exc).__name__}: {exc}")
        return 1
    if readiness_receipt and readiness_receipt.get("status") != "READY":
        print("✗ required Skill Pack runtime tool gate: BLOCKED")
        for item in readiness_receipt.get("tools", []):
            if item.get("status") not in {"READY", "NOT_APPLICABLE", "OPTIONAL"}:
                print(f"  - {item.get('name')}: {item.get('status')}")
                for check in item.get("checks", [])[:4]:
                    if isinstance(check, dict):
                        print(f"    {check.get('name')}: {check.get('detail')}")
        if readiness_receipt.get("error"):
            print(f"  原因：{readiness_receipt['error']}")
        if readiness_receipt.get("receipt_path"):
            print(f"  收据：{readiness_receipt['receipt_path']}")
        return 1
    if readiness_receipt:
        print(
            "[launch_bugx] ✓ 当前 Skill Pack 的必需运行时工具已就绪 "
            f"({readiness_receipt.get('skill_id')})"
        )

    # DOC-236 C1：删除"命令行显式允许回退"分支——缺能力只能自愈，自愈不成即阻断；
    # 无自动安装器的专有工具走 config.json `tooling_exemptions` 显式豁免（见 degraded_gate）。
    if st.autonomous_mode:
        if degraded_probes:
            choices = autonomous_degraded_choices(degraded_probes)
            if not degraded_gate(degraded_probes, explicit_choices=choices):
                return 1
            print("[launch_bugx] ✓ 自治模式仅保留结构化自愈成功、非适用剪枝或显式审计豁免")
        else:
            print("[launch_bugx] ✓ 自治模式加速器自动装配完成，环境满血就绪！")
    elif not degraded_gate(degraded_probes, explicit_choices=tooling_choices):
        return POLLING_DECISION_REQUIRED
    if run_verify_life("check") != 0:
        print("→ 基线不一致，先 repair 恢复…")
        if run_verify_life("repair") != 0:
            print("✗ repair 无法自动修复（可能 history 被篡改），基线不可用。")
            print("  请人工复核 bugx-life.json，或 reset 后重建。")
            return 1
        if run_verify_life("check") != 0:
            print("✗ repair 后仍不一致，基线不可用。")
            return 1
        print("✓ 基线已修复")
    st.review_mode = review_mode
    st.current_review_mode = current_review_mode
    st.current_review_deferred = current_review_deferred
    return 0


def ensure_autonomous_prep_record(root: Path | None = None) -> int:
    """Replace only the untouched init template with observed facts.

    Autonomous runs cannot wait for a human to fill the preparation form, but
    they must not overwrite a user-authored record or weaken ``prep_validate``.
    Any non-template invalid record remains a hard post gate failure.
    """
    root = (root or resolve_project_root()).resolve()
    try:
        from core.runtime import prep_validate
        return prep_validate.ensure_prep_record(root)
    except Exception as exc:
        print(f"[launch_bugx] ✗ 准备记录生成失败: {exc}")
        return 1


def start_or_resume_session(st: "_PreState", memo: "_RuntimeMemo") -> int:
    """会话启动：中断/完成会话的新建引导。

    原 ``pre()`` 830-862 逐字迁移。其后的覆盖清单门禁与快照刻意留在
    ``pre()`` 编排层内联——见 ``pre()`` 内的说明。"""
    aborted_new_session = st.aborted_new_session
    bootstrap_coverage = st.bootstrap_coverage
    life_state = st.life_state
    new_session_started = st.new_session_started
    life_state = baseline_json()
    try:
        # 期间没有任何 runtime 写操作（子进程 verify_life 不写 runtime.json），
        # 直接复用快照即可。
        previous_runtime = memo.get()
    except (OSError, ValueError):
        previous_runtime = {}
    new_session_started = False
    if aborted_new_session:
        print("↻ 上一个 Session 已中断，按显式新建请求创建新的 Session 和首个 Round。")
        runtime, started, startup_code = _begin_new_session(
            autonomous=st.autonomous_mode,
        )
        if not started:
            if startup_code is not None:
                return startup_code
            print("✗ 新 Session 未能建立，保留原中断状态并停止启动。")
            return 1
        new_session_started = True
        life_state = baseline_json()
    elif (not previous_runtime.get("active")
            and previous_runtime.get("lifecycle_terminal")):
        print("↻ 上一个 Session 已完成，正在创建新的 Session 和首个 Round。")
        runtime, started, startup_code = _begin_new_session(
            bootstrap_coverage=bootstrap_coverage, autonomous=st.autonomous_mode,
        )
        if not started:
            if startup_code is not None:
                return startup_code
            print("✗ 新 Session 未能建立，保留原完成状态并停止启动。")
            return 1
        new_session_started = True
        life_state = baseline_json()
    elif isinstance(life_state, dict) and life_state.get("life", 1) <= 0:
        print("✝ 当前 Life 已耗尽，但上一 Session 尚未安全收尾；进入恢复检查。")
        print("  请先恢复或修复当前运行状态，禁止覆盖未完成 Session。")
        return 1
    st.life_state = life_state
    st.new_session_started = new_session_started
    return 0


def serial_runtime_identity(runtime: Mapping[str, object]) -> dict[str, object]:
    """Validate the runtime-owned identity before it can bind a Supervisor run."""
    life_no = runtime.get("life_no")
    if isinstance(life_no, bool) or not isinstance(life_no, int) or life_no < 1:
        raise ValueError("runtime life_no must be a positive integer")

    lifecycle_id = runtime.get("lifecycle_id")
    if not isinstance(lifecycle_id, str) or not lifecycle_id.strip():
        raise ValueError("runtime lifecycle_id must be a non-empty string")
    for field_name in ("round_id",):
        value = runtime.get(field_name)
        if value is not None and value != lifecycle_id:
            raise ValueError(f"runtime {field_name} does not match lifecycle_id")

    expected_life_id = f"life-{life_no}"
    runtime_life_id = runtime.get("life_id")
    if runtime_life_id is not None and runtime_life_id != expected_life_id:
        raise ValueError("runtime life_id does not match life_no")

    session_ids: list[str] = []
    for field_name in ("session_id", "polling_id"):
        value = runtime.get(field_name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"runtime {field_name} must be a non-empty string")
        session_ids.append(value)
    if not session_ids:
        raise ValueError("runtime session identity is missing")
    if any(value != session_ids[0] for value in session_ids[1:]):
        raise ValueError("runtime session_id and polling_id conflict")

    life_kind = runtime.get("life_kind")
    host = str(runtime.get("host") or "").strip().casefold()
    if life_kind == "fix":
        pass
    elif life_kind == "mine" and host_serial_worker(host):
        pass
    else:
        raise ValueError(
            f"serial authorization only supports fix Life or mine Life on a serial-worker host ({host})"
        )
    return {
        "session_id": session_ids[0],
        "lifecycle_id": lifecycle_id,
        "round_id": lifecycle_id,
        "life_id": expected_life_id,
        "life_no": life_no,
        "life_kind": life_kind,
    }


def serial_authorization_intent(
    runtime: dict, supervisor_state: object, serial_reason: str,
) -> dict[str, object]:
    """Build the bounded pending record from matching runtime and Supervisor identities."""
    from core.supervisor.serial_authorization import derive_serial_policy_hash

    def value(name: str) -> object:
        if isinstance(supervisor_state, Mapping):
            return supervisor_state.get(name)
        return getattr(supervisor_state, name, None)

    runtime_identity = _serial_runtime_identity(runtime)
    lifecycle_id = runtime_identity["lifecycle_id"]
    identity: dict[str, object] = {
        "runtime_run_uuid": runtime.get("run_id"),
        "supervisor_run_id": value("run_id"),
        "session_id": value("session_id"),
        "lifecycle_id": lifecycle_id,
        "round_id": value("round_id"),
        "life_id": value("life_id"),
        "life_kind": value("life_kind"),
        "round_no": value("round_no"),
    }
    for field_name in ("session_id", "round_id", "life_id"):
        if identity[field_name] != runtime_identity[field_name]:
            raise ValueError(
                f"Supervisor {field_name} does not match runtime identity"
            )
    if identity["life_kind"] != runtime_identity["life_kind"]:
        raise ValueError("Supervisor Life kind does not match runtime identity")
    reason_digest = hashlib.sha256(serial_reason.encode("utf-8")).hexdigest()
    base_policy_hash = runtime.get("autonomous_policy_hash")
    policy_hash = derive_serial_policy_hash(
        identity=identity,
        base_policy_hash=base_policy_hash,
        serial_reason_sha256=reason_digest,
    )
    return {
        "schema_version": 1,
        "status": "pending",
        "authorization_source": "pre_cli",
        "allow_serial": True,
        "identity": identity,
        "base_policy_hash": base_policy_hash,
        "serial_reason_sha256": reason_digest,
        "policy_hash": policy_hash,
    }


def persist_serial_authorization_record(
    runtime: dict, expected: dict[str, object], memo: "_RuntimeMemo", *,
    receipt: dict[str, object] | None = None,
) -> dict[str, object]:
    """Persist pending before issuing, then atomically bind the Supervisor receipt."""
    with runtime_lock():
        latest = load_runtime()
        if latest.get("run_id") != runtime.get("run_id"):
            raise ValueError("runtime identity changed during serial authorization")
        existing = latest.get("serial_authorization")
        # 回执是**运行级**凭据：`start_runtime` 每次调用都会铸造新的 run_id
        # （同一会话被再次 `pre` resume 时亦然），因此绑定旧 run 的回执属于
        # **陈旧**而非冲突。若按冲突处理，重复 `bugx pre --allow-serial`
        # 会永久失败（实测 test_auto_stop_uses_total_findings_of_big_lifecycle
        # 命中 `runtime serial authorization conflicts with explicit pre inputs`）。
        if isinstance(existing, Mapping):
            stored_identity = existing.get("identity")
            stored_run = (
                stored_identity.get("runtime_run_uuid")
                if isinstance(stored_identity, Mapping) else None
            )
            if stored_run != latest.get("run_id"):
                existing = None
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise ValueError("runtime serial authorization record is malformed")
            expected_fields = set(expected)
            allowed = (expected_fields, expected_fields | {"receipt"})
            if set(existing) not in allowed or any(
                existing.get(key) != value
                for key, value in expected.items() if key != "status"
            ):
                raise ValueError("runtime serial authorization conflicts with explicit pre inputs")
            status = existing.get("status")
            if status not in {"pending", "authorized"}:
                raise ValueError("runtime serial authorization has an invalid status")
            if status == "authorized":
                if receipt is not None and existing.get("receipt") != receipt:
                    raise ValueError("runtime serial authorization receipt changed")
                memo.seed(latest)
                return dict(existing)
            if "receipt" in existing:
                raise ValueError("pending serial authorization cannot contain a receipt")
        record = dict(expected)
        if receipt is not None:
            record["status"] = "authorized"
            record["receipt"] = receipt
        latest["serial_authorization"] = record
        save_runtime(latest)
        memo.seed(latest)
        return record




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
