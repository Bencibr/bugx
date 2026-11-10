#!/usr/bin/env python3
"""launch_phases/startup_interaction.py - 交互菜单与审计策略方案编译。"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.launch_phases.common import (
    AGENT_DIR,
    HOST_DISPLAY_NAMES,
    resolve_project_root,
    uses_project_layout,
    child_environment,
    run_child,
    run_verify_life,
    POLLING_DECISION_REQUIRED,
    REVIEW_DECISION_LATER,
    bugx_paths,
    bugx_logging,
)
import shutil
import subprocess
import core.secure_atomic as secure_atomic
from core.launch_phases.session_runtime import (
    iso_time,
    load_runtime,
    save_runtime,
    append_runtime_event,
    runtime_lock,
)




from core.launch_phases.common import resolve_project_root as _orig_resolve_project_root
from core.launch_phases.session_runtime import load_runtime as _orig_load_runtime, save_runtime as _orig_save_runtime

def _sg():
    return sys.modules.get("core.launch_phases.startup_gates") or sys.modules.get("launch_phases.startup_gates")

def _get_sg_attr(name: str, fallback=None):
    sg = _sg()
    if sg and hasattr(sg, name):
        return getattr(sg, name)
    return fallback

def _sg_dispatch(name, fallback_fn=None):
    def wrapper(*args, **kwargs):
        sg = _sg()
        if sg is not None and hasattr(sg, name):
            target = getattr(sg, name)
            if target is not wrapper:
                return target(*args, **kwargs)
        if fallback_fn is not None:
            return fallback_fn(*args, **kwargs)
        raise RuntimeError(f"{name} not available and no fallback provided")
    wrapper.__name__ = name
    return wrapper

def _orig_stdin_tty():
    try:
        return bool(sys.stdin.isatty())
    except (OSError, AttributeError):
        return False

def _orig_tty_menu_available():
    env = os.environ.get("BUGX_INTERACTION_ENVIRONMENT", "").lower()
    if env in ("native_question", "mcp", "headless", "pipe"):
        return False
    if os.environ.get("CI") or os.environ.get("BUGX_NON_INTERACTIVE"):
        return False
    return _orig_stdin_tty()

def _orig_get_state(root):
    try:
        from core.cloud_sync.cloud_onboard import get_state as fn
        return fn(root)
    except Exception:
        return None

def _orig_detect_env():
    mod = _sg()
    if mod and hasattr(mod, "detect_startup_environment"):
        return mod.detect_startup_environment()
    return "terminal"

resolve_project_root = _sg_dispatch("resolve_project_root", _orig_resolve_project_root)
load_runtime = _sg_dispatch("load_runtime", _orig_load_runtime)
save_runtime = _sg_dispatch("save_runtime", _orig_save_runtime)
emit_startup_gate = _sg_dispatch("emit_startup_gate")
_tty_menu_available = _sg_dispatch("_tty_menu_available", _orig_tty_menu_available)
tty_menu_available = _tty_menu_available
_stdin_tty = _sg_dispatch("_stdin_tty", _orig_stdin_tty)
stdin_tty = _stdin_tty
_run_verify_life_direct = _sg_dispatch("_run_verify_life_direct", lambda *args, **kwargs: run_verify_life(*args, **kwargs))
run_verify_life_direct = _run_verify_life_direct
detect_startup_environment = _sg_dispatch("detect_startup_environment", _orig_detect_env)
get_state = _sg_dispatch("get_state", _orig_get_state)

class _DynamicObjectProxy:
    def __init__(self, name, fallback_factory=None):
        self._name = name
        self._fallback_factory = fallback_factory
    def _target(self):
        target = _get_sg_attr(self._name)
        if target is not None and not isinstance(target, _DynamicObjectProxy):
            return target
        if self._fallback_factory:
            return self._fallback_factory()
        return None
    def __call__(self, *args, **kwargs):
        t = self._target()
        if t is not None:
            return t(*args, **kwargs)
        raise RuntimeError(f"{self._name} is not callable or None")
    def __getattr__(self, item):
        t = self._target()
        if t is not None:
            return getattr(t, item)
        raise AttributeError(f"{self._name} has no attribute {item} (target is None)")
    def __bool__(self):
        return self._target() is not None
    def __eq__(self, other):
        return self._target() == other

def _fb_cloud_client():
    try:
        from core.cloud_sync import cloud_client
        return cloud_client.CloudClient
    except Exception:
        return None

def _fb_project_profile():
    try:
        from core.runtime import project_profile
        return project_profile
    except Exception:
        return None

def _fb_lesson_ledger():
    try:
        from core.ledger import lesson_ledger
        return lesson_ledger
    except Exception:
        return None

def _fb_interaction():
    try:
        from core.runtime import interaction
        return interaction
    except Exception:
        return None

CloudClient = _DynamicObjectProxy("CloudClient", _fb_cloud_client)
project_profile = _DynamicObjectProxy("project_profile", _fb_project_profile)
lesson_ledger = _DynamicObjectProxy("lesson_ledger", _fb_lesson_ledger)
_interaction = _DynamicObjectProxy("_interaction", _fb_interaction)

def _discard_stale_draft(root: Path, *, reason: str) -> None:
    mod = (sys.modules.get("core.launch_phases.startup_gates")
           or sys.modules.get("launch_phases.startup_gates"))
    if mod and hasattr(mod, "_discard_stale_draft"):
        return mod._discard_stale_draft(root, reason=reason)
    draft = root / ".bugx" / "state" / "strategy-plan.json"
    if draft.is_file() or draft.is_symlink():
        try:
            draft.unlink()
        except OSError:
            pass

def compile_strategy_plan(root: Path | None = None) -> dict[str, Any]:
    """根据项目客观画像与知识库编译《审计策略作战方案》（受控 ≤500 字摘要卡）。"""
    root = root or resolve_project_root()
    # 优先复用当前活跃会话中已批准的作战方案，避免多轮或多小周期下巨量重复静态分析
    runtime_file = root / ".bugx" / "state" / "bugx-runtime.json"
    if not runtime_file.is_file():
        runtime_file = root / "bugx-runtime.json"
    if runtime_file.is_file():
        try:
            runtime_data = json.loads(runtime_file.read_text(encoding="utf-8"))
            existing_plan = runtime_data.get("strategy_plan")
            if isinstance(existing_plan, dict) and existing_plan.get("summary"):
                # DOC-236 C4：运行时已有已批准/微调方案时，磁盘上残留的草稿是
                # **过期半成品**（可能是"写 runtime 后、删草稿前"被强杀留下的），
                # 必须显式丢弃并记录，而不是静默留着让人误以为它还有效。
                _discard_stale_draft(root, reason="运行时已存在已批准方案")
                return existing_plan
        except Exception:
            pass

    draft_file = root / ".bugx" / "state" / "strategy-plan.json"
    persisted_profile: dict[str, Any] = {}
    if project_profile is not None:
        try:
            ledger_path = root / ".bugx" / "knowledge" / "lesson-ledger.json"
            # 方案摘要必须基于**新鲜**画像：画像刷新（ensure_project_profile）
            # 原本在 pre 的准备阶段才执行、晚于本门禁——引擎修复检测规则后
            # （DETECTOR_VERSION 变化），Gate 2 仍会把旧规则算出的 backend/queue
            # 摘要端给用户。此处先 ensure（内部指纹/版本失配才重算，命中即秒回）。
            ensured_profile: dict[str, Any] = {}
            try:
                # ensure 返回值就是当前画像（复用或重算），直接流入后续编译，
                # 避免再走一次全量静态扫描的回退 detect（双倍扫描）。分流依据
                # 是 SQLite 账本（ledger.db_path 语义：<stem>.sqlite3）是否已由
                # pre 初始化：已初始化 → ensure（保新鲜/落库）；未初始化（裸
                # 环境/单元夹具）→ 只检测一次，落库留给 pre 的账本初始化。
                ledger_db = Path(str(ledger_path)).with_suffix(".sqlite3")
                if ledger_db.is_file():
                    ensured_profile, _changed = (
                        project_profile.ensure_project_profile(ledger_path, root)
                    )
                else:
                    ensured_profile = project_profile.detect_project_profile(root)
            except Exception:  # noqa: BLE001 - 账本未迁移等场景按旧值继续
                ensured_profile = {}
            persisted_profile = ensured_profile or (
                project_profile.load_project_profile(ledger_path) or {}
            )
        except Exception:
            persisted_profile = {}
    if draft_file.is_file():
        try:
            saved = json.loads(draft_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 损坏草稿必须可观测地丢弃
            # DOC-236 C4：此前是静默 `except: pass`，导致"草稿写一半被强杀"后
            # 既不报错也不重建，用户只看到方案莫名消失。现在显式丢弃 + 重建。
            print(f"[launch_bugx] 作战方案草稿不可解析（{type(exc).__name__}），已丢弃并将重新生成")
            _discard_stale_draft(root, reason="草稿不可解析")
        else:
            if isinstance(saved, dict) and saved.get("summary"):
                saved_profile = saved.get("project_profile") if isinstance(saved.get("project_profile"), dict) else {}
                saved_hash = str(saved_profile.get("profile_hash") or "")
                current_hash = str(persisted_profile.get("profile_hash") or "")
                # Legacy custom drafts have no hash and remain readable.  A
                # versioned draft must match the persisted fact profile.
                if not current_hash or not saved_hash or saved_hash == current_hash:
                    return saved
                # DOC-236 C4：画像已变 ⇒ 草稿是基于旧画像的半成品，显式丢弃重建。
                print("[launch_bugx] 作战方案草稿与当前项目画像不一致，已丢弃并将重新生成")
                _discard_stale_draft(root, reason="画像失配")
            else:
                _discard_stale_draft(root, reason="草稿缺少有效摘要")

    profile: dict[str, Any] = {}
    strategy: dict[str, Any] = {}
    if project_profile is not None:
        try:
            # pre_phase 已经生成并持久化画像时，策略只能消费这份画像。
            # 只有旧项目尚未有 project_profile 行时才兼容性重建一次。
            profile = persisted_profile
            if not profile:
                profile = project_profile.detect_project_profile(root)
            strategy = project_profile.compile_audit_strategy(profile)
        except Exception:
            profile = {}
            strategy = {}

    classification = profile.get("classification") if isinstance(profile.get("classification"), dict) else {}
    primary_lang = profile.get("primary_language") or classification.get("primary_language") or "unknown"
    forms = profile.get("forms") or ["cli"]
    surfaces = profile.get("surfaces") or []
    frameworks = profile.get("frameworks") or []

    # 推荐靶向工具链推导
    recommended_tools: list[str] = []
    if any(f in forms for f in ("web", "ui")):
        recommended_tools.append("Playwright (Web UI/网络)")
    if any(s in surfaces for s in ("rest", "graphql", "grpc", "api")):
        recommended_tools.append("postmcp + API Fuzzer")
    if any(s in surfaces for s in ("queue", "auth", "state-machine")) or "go" in primary_lang:
        recommended_tools.append("concurrency_fuzzer (并发时序)")
    if "python" in primary_lang:
        recommended_tools.append("pytest/ast-grep")
    elif "javascript" in primary_lang or "typescript" in primary_lang:
        recommended_tools.append("vitest/jest/ast-grep")
    elif "rust" in primary_lang or "go" in primary_lang or "c" in primary_lang:
        recommended_tools.append("memory_probe (内存/泄露)")
    if not recommended_tools:
        recommended_tools = ["pytest (内置单测执行器)", "ast-grep (白盒扫描)"]

    # 预选同技术栈高危错题集
    preselected_lessons: list[str] = []
    if lesson_ledger is not None:
        ledger_file = root / ".bugx" / "knowledge" / "lesson-ledger.json"
        if ledger_file.is_file():
            try:
                tags = project_profile.routing_tags(profile) if project_profile else None
                matched = lesson_ledger.retrieve(ledger_file, limit=3, routing_tags=tags)
                for item in matched:
                    pid = str(item.get("pattern_id") or "")
                    title = str(item.get("title") or item.get("pattern") or "")
                    preselected_lessons.append(f"{pid}: {title}" if title else pid)
            except Exception:
                pass

    # 预热专项 L2 规则
    prewarmed_rules = ["anti_patterns.md"]
    if any(s in surfaces for s in ("queue", "worker", "native")) or "go" in primary_lang:
        prewarmed_rules.append("concurrency_rules.md")
    if any(s in surfaces for s in ("auth", "rest", "ui")) or any(f in forms for f in ("web", "backend")):
        prewarmed_rules.append("prompt_injection_defense.md")
    if any(t in ("c", "cpp", "rust", "embedded") for t in [primary_lang] + list(forms)):
        prewarmed_rules.append("memory_leak_rules.md")

    # 云端定向预拉（DOC-041 Phase 2: 若已配置云端且认证有效，且非本地模式）
    if CloudClient is not None:
        try:
            onboard_state = None
            if get_state is not None:
                try:
                    onboard_state = get_state(root)
                except Exception:
                    onboard_state = None
            if onboard_state != "local":
                client = CloudClient()
                if client.cred.is_authenticated():
                    tags = project_profile.routing_tags(profile) if project_profile else None
                    pid = None
                    try:
                        from core.cloud_sync.cloud_onboard import load_project_binding
                        pid = load_project_binding(root)
                    except Exception:
                        pass
                    res = client.prefetch_knowledge(
                        profile,
                        routing_tags=tags,
                        limit=5,
                        project_id=pid,
                    )
                    if res.ok and isinstance(res.data, dict):
                        data = res.data.get("data") if ("data" in res.data and isinstance(res.data["data"], dict)) else res.data
                        if isinstance(data, dict):
                            for item in data.get("lessons") or []:
                                if isinstance(item, dict):
                                    lid = str(item.get("pattern_id") or "")
                                    title = str(item.get("title") or "")
                                    entry = f"{lid}: {title}" if (lid and title) else (title or lid)
                                    if entry and entry not in preselected_lessons:
                                        preselected_lessons.append(entry)
                            for tool in data.get("recommended_tools") or []:
                                if tool and isinstance(tool, str) and tool not in recommended_tools:
                                    recommended_tools.append(tool)
                            for rule in data.get("warmup_rules") or []:
                                if rule and isinstance(rule, str) and rule not in prewarmed_rules:
                                    prewarmed_rules.append(rule)
        except Exception:
            pass

    lessons_summary = "、".join(preselected_lessons[:2]) if preselected_lessons else "暂无历史踩坑记录"
    tools_summary = "、".join(recommended_tools[:3])
    rules_summary = "、".join(prewarmed_rules[:4])

    risk_info = profile.get("risk_metrics") or {}
    health_grade = risk_info.get("health_grade") or "B"
    hotspots = risk_info.get("top_risk_hotspots") or []
    top_targets = "、".join(Path(h["file"]).name for h in hotspots[:2]) if hotspots else ""
    taints_cnt = risk_info.get("total_taint_flows", 0)

    radar_text = f"• 代码健康度：{health_grade} 级" + (f" (高危靶点: {top_targets}，污点流: {taints_cnt} 处)" if top_targets else "") + "\n"

    summary = (
        f"【BugX 审计作战方案】\n"
        f"• 技术栈/形态：{primary_lang} | 形态: {','.join(forms)} | 攻击面: {','.join(surfaces[:4]) or '基础逻辑'}\n"
        f"{radar_text}"
        f"• 推荐靶向工具：{tools_summary}\n"
        f"• 预选同栈错题：{lessons_summary}\n"
        f"• 预热专项规则：{rules_summary}\n"
        f"• 推荐执行组合：自动修复 (TDD) + 自动轮询 (auto) + Round 提交"
    )
    if len(summary) > 500:
        summary = summary[:497] + "..."


    return {
        "schema_version": 1,
        "compiled_at": iso_time(time.time()),
        "project_profile": {
            "primary_language": primary_lang,
            "forms": forms,
            "surfaces": surfaces,
            "frameworks": frameworks,
            "profile_hash": profile.get("profile_hash", ""),
            "detector_version": profile.get("detector_version", ""),
            "runtime_facts": list(profile.get("runtime_facts") or [])[:24],
            "components": list(profile.get("components") or [])[:48],
            "risk_signals": list(profile.get("risk_signals") or [])[:24],
        },
        "recommended_tools": recommended_tools,
        "preselected_lessons": preselected_lessons,
        "prewarmed_rules": prewarmed_rules,
        "recommended_preset": {
            "review_mode": "mcp-auto",
            "polling_policy": "auto",
            "fix_mode": "auto",
            "commit_policy": "round",
            # DOC-236 C6：报告提交策略随同一次"作战方案"审批一并采集。
            "report_policy": "local",
        },
        "compiled_strategy": strategy,
        "summary": summary,
    }

def run_verify_life_direct(*args: str) -> int:
    """启动门禁对 verify_life 寿命校验脚本的直接调用入口（优先进程内直调，失败回退子进程）。"""
    return run_verify_life(*args)


def apply_approved_strategy(plan: dict[str, Any], *, cli_fix_mode: str | None = None) -> None:
    """一键批准策略：写入运行时并应用推荐的最佳执行预设。"""
    root = resolve_project_root()
    # DOC-236 C4：**先**把批准结果原子写入 runtime，**再**删除草稿。
    # 原实现先 unlink 草稿再写 runtime，若在两步之间被强杀 ⇒ 草稿与批准双丢；
    # 现在的顺序保证最坏情况只是"已批准 + 残留草稿"，而残留草稿会被
    # ``_discard_stale_draft`` 显式清理（可恢复、可观测）。
    with runtime_lock():
        state = load_runtime()
        state["strategy_plan"] = {
            **plan,
            "status": "approved",
            "approved_at": iso_time(time.time()),
        }
        preset = plan.get("recommended_preset", {})
        if state.get("review_mode") is None:
            state["review_mode"] = preset.get("review_mode", "manual")
        if state.get("polling_policy") is None:
            state["polling_policy"] = preset.get("polling_policy", "auto")
        if state.get("commit_policy") is None:
            state["commit_policy"] = preset.get("commit_policy", "round")
        # DOC-236 C6：随审批一并采集；非法值 fail-closed（拒绝写入）。
        # 已由 L1 显式采集的 runtime 值优先于审批 preset；preset 仍必须
        # 自身合法，避免把非法方案藏在一个已存在的旧值后面。
        from core.launch_phases.report_sharing import normalize_report_policy
        try:
            preset_policy = normalize_report_policy(
                preset.get("report_policy") or "local"
            ) or "local"
            state_policy = normalize_report_policy(state.get("report_policy"))
        except ValueError as exc:
            raise RuntimeError(f"作战方案中的 report_policy 非法：{exc}") from exc
        state["report_policy"] = state_policy or preset_policy
        save_runtime(state)
    # 批准已落盘（原子）⇒ 草稿成为过期半成品，此时删除才安全。
    _discard_stale_draft(root, reason="方案已批准落盘")

    fix_mode = plan.get("recommended_preset", {}).get("fix_mode", "auto")
    if cli_fix_mode:
        # CLI 显式给过 --fix-mode 时不得用 preset 覆盖（选择持久化先于本门禁
        # 执行，此处再 set-mode 会把显式值静默吞掉）；交还启动选择的通用持久化。
        print(f"[launch_bugx] ✓ 作战方案已一键批准；修复方式尊重显式 CLI 值 {cli_fix_mode}，其余采用推荐预设：审查=mcp-auto 轮询=auto 提交=round")
    else:
        target = _get_sg_attr("_run_verify_life_direct") or _run_verify_life_direct
        target("set-mode", "--mode", fix_mode, "--confirm")
        print(f"[launch_bugx] ✓ 作战方案已一键批准，采用推荐预设：审查=mcp-auto 修复={fix_mode} 轮询=auto 提交=round")

def mark_strategy_customized(plan: dict[str, Any]) -> None:
    """微调策略方案：记录状态并允许顺序进入细分机械门禁。"""
    with runtime_lock():
        state = load_runtime()
        state["strategy_plan"] = {
            **plan,
            "status": "customized",
            "customized_at": iso_time(time.time()),
        }
        save_runtime(state)
    print("[launch_bugx] 已选择微调方案，将顺序展开细分门禁。")

def save_strategy_draft(plan: dict[str, Any]) -> None:
    """暂存作战方案草案至磁盘。"""
    root = resolve_project_root()
    state_dir = root / ".bugx" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    draft_file = state_dir / "strategy-plan.json"
    # DOC-236 C4：原子写（临时文件 + rename），杜绝"草稿写一半"留下截断 JSON。
    secure_atomic.atomic_write_json(draft_file, plan, mode=0o600, parent_mode=0o700)
    print(f"[launch_bugx] 作战方案已暂存至 {draft_file}，本轮启动停止。")

def strategy_plan_gate(
    strategy_choice: str | None = None,
    environment=None,
    plan_data: dict[str, Any] | None = None,
    cli_fix_mode: str | None = None,
) -> tuple[int, str | None]:
    """策略规划层与人机对齐门禁（Strategy Plan Gate，DOC-025 D25-11）。

    返回 (code, decision)。code==0 且 decision in {"approved","customized"} 表示放行，
    code==POLLING_DECISION_REQUIRED 表示需要决策或暂存停止。
    """
    root = resolve_project_root()
    plan = plan_data or compile_strategy_plan(root)

    choice = (strategy_choice or "").strip().lower()
    if choice in {"approve", "approve_and_start", "1"}:
        target = _get_sg_attr("_apply_approved_strategy") or _apply_approved_strategy
        target(plan, cli_fix_mode=cli_fix_mode)
        return 0, "approved"
    if choice in {"customize", "custom", "2"}:
        target = _get_sg_attr("mark_strategy_customized") or mark_strategy_customized
        target(plan)
        return 0, "customized"
    if choice in {"later", "save_and_later", "save", "3"}:
        target = _get_sg_attr("_save_strategy_draft") or _save_strategy_draft
        target(plan)
        return POLLING_DECISION_REQUIRED, "later"

    runtime = load_runtime()
    saved_status = (runtime.get("strategy_plan") or {}).get("status")
    if saved_status == "approved":
        return 0, "approved"
    if saved_status == "customized":
        return 0, "customized"

    environment = environment or detect_startup_environment()
    options = [
        {
            "label": "一键批准推荐策略并开工",
            "description": "采用推荐靶向工具链与最佳运行组合（自动修复+自动轮询+按轮提交），立即开始审计",
            "value": "approve_and_start",
            "action": "approve_strategy_plan",
        },
        {
            "label": "自定义微调",
            "description": "保持本作战方案，顺序微调审查方式、轮询策略、修复模式与提交策略细分门禁",
            "value": "customize",
            "action": "customize_strategy_plan",
        },
        {
            "label": "暂存方案稍后启动",
            "description": "将方案草案保存至 .bugx/state/strategy-plan.json，退出本次启动",
            "value": "save_and_later",
            "action": "save_strategy_plan_later",
        },
    ]

    if _tty_menu_available():
        tty_opts = [(opt["label"], opt["description"]) for opt in options]
        res = ask_interaction(
            "strategy_plan", plan["summary"], tty_opts,
            phase="startup", title="BugX 审计策略规划作战方案", default=0,
            detail="请审阅作战方案并确认启动策略", emit_request=False,
        )
        val = answered_value(res)
        if val == "1":
            target = _get_sg_attr("_apply_approved_strategy") or _apply_approved_strategy
            target(plan)
            return 0, "approved"
        elif val == "2":
            target = _get_sg_attr("mark_strategy_customized") or mark_strategy_customized
            target(plan)
            return 0, "customized"
        elif val == "3":
            _save_strategy_draft_dispatched(plan)
            return POLLING_DECISION_REQUIRED, "later"
        return POLLING_DECISION_REQUIRED, None

    emit_startup_gate(
        "strategy_plan",
        plan["summary"],
        options,
        environment=environment,
        detail="请审阅作战方案并选择：一键批准并启动、微调或暂存稍后",
    )
    return POLLING_DECISION_REQUIRED, None

def stdin_tty() -> bool:
    target = _get_sg_attr("_stdin_tty") or _get_sg_attr("stdin_tty")
    if target is not None and target is not stdin_tty:
        return target()
    try:
        return bool(sys.stdin.isatty())
    except (OSError, AttributeError):
        return False

def tty_menu_available() -> bool:
    """Return whether this process may own a TTY menu.

    A native-question host may itself have a TTY, but the launcher is still a
    child process and must return one structured gate to the host instead of
    stealing the interaction with ``input()``.

    同理，探针已判定 ``blocked`` 时也不得抢占 stdin —— 那意味着宿主**显式**声明
    了无交互能力（``BUGX_INTERACTION=none/blocked/headless``），或 stdout 不构成
    可用交互面（例如被重定向到文件）。这两种情况下强行走菜单只会把菜单写进
    文件或被丢弃，用户什么都看不到，进程却静默阻塞在 ``input()`` 上。
    因此这里只认探针已经选定的交互通道，采用白名单而非黑名单：将来新增通道
    取值时默认 fail-closed（不抢占 stdin），而不是默认抢占。
    """
    target = _get_sg_attr("_tty_menu_available") or _get_sg_attr("tty_menu_available")
    if target is not None and target is not tty_menu_available:
        return target()
    if os.environ.get("CI") or os.environ.get("BUGX_NON_INTERACTIVE"):
        return False
    if not _stdin_tty():
        return False
    environment = detect_startup_environment()
    if environment is None:
        return False
    return getattr(environment, "interaction", None) in {"tty-radio", "text-menu"}

def radio_menu(title: str, options: list[tuple[str, str]], *, default: int):
    """通过统一交互组件调用方向键绘制层。"""
    if _interaction is None:
        from core.runtime.interaction import RADIO_UNAVAILABLE

        return RADIO_UNAVAILABLE
    return _interaction.select_one(title, options, default=default)

def interaction_environment():
    """Return one environment snapshot for the current interaction attempt."""
    environment = detect_startup_environment()
    if environment is None:
        return {"host": "unknown", "interface": "unknown", "interaction": "blocked"}
    if hasattr(environment, "as_dict"):
        data = environment.as_dict()
    elif isinstance(environment, dict):
        data = dict(environment)
    else:
        data = {
            key: getattr(environment, key)
            for key in ("host", "interface", "interaction")
            if hasattr(environment, key)
        }
    # Keep the local test seam aligned with the already completed environment
    # probe: only a real, non-native TTY may be owned by the core process.
    if _tty_menu_available():
        data["interaction"] = "tty-radio"
    return data

def ask_interaction(
    question_id: str,
    question: str,
    options: list[tuple[str, str]] | None = None,
    *,
    phase: str = "startup",
    kind: str = "select",
    title: str = "",
    default: int = 0,
    detail: str = "",
    required: bool = False,
    multiple: bool = False,
    emit_request: bool = False,
) -> object:
    """声明一个问题并返回统一 InteractionResult。"""
    _interaction = _get_sg_attr("_interaction")
    if _interaction is None:
        return None
    try:
        models = tuple(
            _interaction.InteractionOption(
                value=str(index + 1), label=label, description=description,
            )
            for index, (label, description) in enumerate(options or [])
        )
        result = _interaction.ask(
            _interaction.InteractionQuestion(
                question_id=question_id,
                phase=phase,
                kind=kind,
                title=title,
                question=question,
                options=models,
                multiple=multiple,
                default=default,
                detail=detail,
                required=required,
            ),
            _interaction_environment(),
            output=print if emit_request else (lambda _line: None),
            input_fn=input,
            select_fn=_radio_menu,
        )
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        print(f"[launch_bugx] 交互组件失败，停留在当前问题：{exc}")
        return None
    return result

def answered_value(result: object) -> str | None:
    values = answered_values(result)
    return values[0] if values and len(values) == 1 else None

def answered_values(result: object) -> tuple[str, ...] | None:
    if _interaction is None or not isinstance(result, _interaction.InteractionResult):
        return None
    if result.status != _interaction.ANSWERED:
        return None
    if isinstance(result.value, str):
        return (result.value,)
    if isinstance(result.value, tuple) and all(
        isinstance(value, str) for value in result.value
    ):
        return result.value
    return None

def map_polling_answer(answer: str) -> str | None:
    low = (answer or "").strip().lower()
    if low in {"2", "manual", "man", "m"}:
        return "manual"
    if low in {"1", "auto", "a", "automation", "automatic"}:
        return "auto"
    return None

def ask_polling_policy() -> str | None:
    """通过统一组件询问 Round 报告后的续轮策略。"""
    result = ask_interaction(
        "polling_policy", "当前 Round 报告后是否自动开始下一 Round？", [
            ("auto：自动续轮", "新 Round 累计零有效 Bug 并死亡时立即终止循环"),
            ("manual：手动续轮", "出具报告后自动收尾 Session"),
        ],
        default=1,
    )
    return _map_polling_answer(answered_value(result))

def map_mode_answer(answer: str) -> str | None:
    """修复方式输入→ verify_life mode；无效返回 None（循环重问）。"""
    low = (answer or "").strip().lower()
    if low in {"1", "auto", "automatic", "自动", "fix"}:
        return "auto"
    if low in {"2", "log-only", "logonly", "log", "仅记录", "record"}:
        return "log-only"
    return None

def map_commit_policy_answer(answer: str) -> str | None:
    """提交策略菜单输入映射；无效输入返回 None 以便当前 gate 重问。"""
    low = (answer or "").strip().lower()
    if low in {"1", "life", "每个life", "每个 life"}:
        return "life"
    if low in {"2", "round", "每个round", "每个 round"}:
        return "round"
    if low in {"3", "manual", "手动", "none", "no-submit", "no_submit", "不提交", "不自动提交"}:
        return "manual"
    return None

def ask_commit_policy() -> str | None:
    """通过统一组件串行询问提交策略。"""
    result = ask_interaction(
        "commit_policy", "请选择提交策略", [
            ("Life 提交", "每个 Life 验收后自动提交"),
            ("Round 提交", "每个 Round 验收后统一提交"),
            ("不提交（手动）", "BugX 不自动提交，保留待提交状态"),
        ],
        default=1,
    )
    return _map_commit_policy_answer(answered_value(result))

def map_review_mode_answer(answer: str) -> str | None:
    """审查执行面输入→manual/mcp-auto/later；无效返回 None。"""
    low = (answer or "").strip().lower()
    if low in {"1", "manual", "man", "手动", "手动审查"}:
        return "manual"
    if low in {"2", "mcp", "mcp-auto", "mcp_auto", "自动", "mcp自动",
               "自动审查", "automatic"}:
        return "mcp-auto"
    if low in {"3", "later", "defer", "skip", "下次", "下次再说"}:
        return REVIEW_DECISION_LATER
    return None

def ask_review_mode() -> str | None:
    """通过统一组件询问审查执行面。"""
    result = ask_interaction(
        "review_mode", "请选择审查方式", [
            ("手动审查", "宿主/Agent 按 BugX 协议推进"),
            ("MCP 自动审查", "bugx-supervisor MCP 协调状态、租约、心跳与门禁"),
            ("下次再说", "保持待决定，停止本轮启动"),
        ],
        default=0,
    )
    return _map_review_mode_answer(answered_value(result))

def ask_fix_mode() -> str | None:
    """通过统一组件询问修复方式（verify_life mode auto/log-only）。"""
    result = ask_interaction(
        "fix_mode", "请选择修复模式", [
            ("自动修复", "发现即修：TDD 红黑 + Live 复验 + 每轮提交"),
            ("仅记录不修", "不改代码；写入 .bugx/knowledge/bug-log.md，标记未修复-待后续"),
        ],
        default=0,
    )
    return _map_mode_answer(answered_value(result))

def map_report_policy_answer(answer: str) -> str | None:
    """报告处置菜单输入→none/local/submit；无效返回 None（当前 gate 重问）。"""
    low = (answer or "").strip().lower()
    if low in {"1", "none", "off", "不共享", "不上报"}:
        return "none"
    if low in {"2", "local", "本地", "仅本地"}:
        return "local"
    if low in {"3", "submit", "cloud", "上报", "云端", "上报云端"}:
        return "submit"
    return None

def ask_report_policy() -> str | None:
    """通过统一组件询问报告处置策略（none/local/submit 三选一）。"""
    result = ask_interaction(
        "report_policy", "审计报告如何处置？", [
            ("不上报（none）", "不共享结论、不参与知识飞轮，只留本地报告"),
            ("仅本地（local）", "保留本地报告并登记待重放队列，不对外提交"),
            ("上报云端（submit）", "每轮结束按私有可见性匿名上报运行结论"),
        ],
        default=1,
    )
    return _map_report_policy_answer(answered_value(result))

def report_policy_startup_gate(
    environment=None, *, autonomous: bool = False,
) -> tuple[int, str | None]:
    """L1 报告处置策略门禁：交互态三选一采集，无人值守取默认 local。

    返回 (code, policy)。code==0 表示已有合法策略；code==
    ``POLLING_DECISION_REQUIRED`` 表示已发出结构化交互请求（或 TTY 输入无效），
    调用方必须原样返回该退出码，**不得**静默采用默认值。
    """
    if autonomous:
        return 0, "local"
    environment = environment or detect_startup_environment()
    # F-RSI-20261110-004：非交互环境（headless/pipe，宿主无法回灌答案）
    # 取安全默认 local 并显式留痕——report_policy 是隐私出口（submit 才
    # 外发），local 是最小权限默认；发结构化请求会让无人值守管线卡死
    # （E2E fresh pre 实测 rc=2）。交互环境（TTY/原生控件）保持三选一。
    env_interaction = (
        (environment or {}).get("interaction")
        if isinstance(environment, dict) else getattr(environment, "interaction", None)
    )
    if env_interaction == "blocked":
        print("[launch_bugx] ✓ 非交互环境（无法回灌答案）：report_policy 取"
              "安全默认 local（仅本地，不外发）；显式覆盖："
              "pre --report-policy <none|local|submit>")
        return 0, "local"
    if _tty_menu_available():
        policy = _ask_report_policy()
        if policy is None:
            return POLLING_DECISION_REQUIRED, None
        return 0, policy
    emit_startup_gate(
        "report_policy",
        "审计报告如何处置？",
        [
            {
                "label": "不上报",
                "description": "不共享结论、不参与知识飞轮，只留本地报告",
                "value": "none",
                "action": "set_report_policy",
            },
            {
                "label": "仅本地",
                "description": "保留本地报告并登记待重放队列，不对外提交",
                "value": "local",
                "action": "set_report_policy",
            },
            {
                "label": "上报云端",
                "description": "每轮结束按私有可见性匿名上报运行结论",
                "value": "submit",
                "action": "set_report_policy",
            },
        ],
        environment=environment,
        detail="选择后以 `--report-policy <none|local|submit>` 重新执行 pre",
    )
    return POLLING_DECISION_REQUIRED, None

def interactive_startup_decisions(
    current_review_mode: str | None = None,
    current_polling: str | None = None,
    current_fix_mode: str | None = None,
    current_review_deferred: bool = False,
    current_commit_policy: str | None = None,
) -> dict | None:
    """启动时的交互决策；只询问尚未确认的决策，减少重复操作。"""
    if not _tty_menu_available():
        return None
    review = current_review_mode
    review_was_asked = review is None
    review_deferred = current_review_deferred
    if review is None:
        review = _ask_review_mode()
    if review is None:
        return None
    if review == REVIEW_DECISION_LATER:
        print("[launch_bugx] 已选择下次再说，本轮启动停止。")
        return None
    elif review_was_asked:
        review_deferred = False
    polling = current_polling
    if polling is None:
        polling = _ask_polling_policy()
        if polling is None:
            return None
    mode = current_fix_mode
    if mode is None:
        mode = _ask_fix_mode()
        if mode is None:
            return None
    commit = current_commit_policy
    if mode == "log-only":
        commit = "not_applicable"
    elif commit is None:
        commit = _ask_commit_policy()
        if commit is None:
            return None
    return {
        "review_mode": review,
        "review_mode_deferred": review_deferred,
        "polling_policy": polling,
        "fix_mode": mode,
        "commit_policy": commit,
    }




_run_verify_life_direct = run_verify_life_direct
_apply_approved_strategy = apply_approved_strategy
_save_strategy_draft = save_strategy_draft
_stdin_tty = stdin_tty
_tty_menu_available = tty_menu_available
_radio_menu = radio_menu
_interaction_environment = interaction_environment
_map_polling_answer = map_polling_answer
_ask_polling_policy = ask_polling_policy
_map_mode_answer = map_mode_answer
_map_commit_policy_answer = map_commit_policy_answer
_ask_commit_policy = ask_commit_policy
_map_review_mode_answer = map_review_mode_answer
_ask_review_mode = ask_review_mode
_ask_fix_mode = ask_fix_mode
_map_report_policy_answer = map_report_policy_answer
_ask_report_policy = ask_report_policy
_interactive_startup_decisions = interactive_startup_decisions


def _apply_approved_strategy_dispatched(*args, **kwargs):
    fn = _get_sg_attr("_apply_approved_strategy") or apply_approved_strategy
    return fn(*args, **kwargs)
def _mark_strategy_customized_dispatched(*args, **kwargs):
    fn = _get_sg_attr("mark_strategy_customized") or mark_strategy_customized
    return fn(*args, **kwargs)
def _save_strategy_draft_dispatched(*args, **kwargs):
    fn = _get_sg_attr("_save_strategy_draft") or save_strategy_draft
    return fn(*args, **kwargs)
