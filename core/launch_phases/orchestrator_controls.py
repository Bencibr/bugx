#!/usr/bin/env python3
"""Operator CLI controls and supervisor state commands for launch phases."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import core.i18n as i18n
except ImportError:
    try:
        from core import i18n
    except ImportError:
        i18n = None  # type: ignore[assignment]

try:
    from core.coverage import coverage_bootstrap
except ImportError:
    coverage_bootstrap = None

from core.launch_phases.common import (
    LESSON_LEDGER_FILE,
    POLLING_DECISION_REQUIRED,
    POLLING_POLICIES,
    SUPERVISOR_STATE,
    VERIFY,
    child_environment,
    format_duration,
    resolve_project_root,
    run_verify_life,
    uses_project_layout,
    require_project_layout,
)
_require_project_layout = require_project_layout
from core.launch_phases.candidate_revalidation import (
    ci_detection_report,
)
_ci_detection_report = ci_detection_report

from core.launch_phases.finding_settlement import settle_findings_path
from core.launch_phases.session_runtime import (
    audit_todo_path,
    baseline_json,
    close_runtime,
    heartbeat_runtime,
    load_runtime,
    pause_runtime,
    print_runtime,
    refresh_audit_todo,
    resume_runtime,
    runtime_lock,
    runtime_summary,
    save_runtime,
    set_polling_policy,
)
from core.launch_phases.pre_phase import (
    attach_coverage,
    coverage_status,
    print_coverage,
)
from core.launch_phases.post_phase import (
    close_session,
    continue_after_death,
    life_summary,
)
from core.launch_phases.startup_gates import (
    detect_startup_environment,
    refresh_startup_todo,
    startup_todo_path,
)
from core.launch_phases.supervisor_bridge import (
    get_supervisor,
)

def _option_value(args: list[str], option: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == option:
            return args[i + 1] if i + 1 < len(args) else None
        if arg.startswith(f"{option}="):
            return arg.split("=", 1)[1]
    return None


#: 公开别名：同包延迟导入方（candidate_revalidation）必须用公开名——架构棘轮
#: 禁止跨模块导入下划线私有符号（private_leaks 棘轮基线 0）。
option_value = _option_value

def _ro():
    return sys.modules.get("core.launch_phases.round_orchestrator") or sys.modules.get("launch_phases.round_orchestrator")


def _get_ro_attr(name: str, fallback=None):
    ro = _ro()
    if ro and hasattr(ro, name):
        return getattr(ro, name)
    return fallback


def lesson_module() -> object:
    """Load the external lesson ledger without coupling startup to its internals."""
    try:
        from core.ledger import lesson_ledger
    except (ImportError, OSError) as exc:
        raise ValueError(f"复盘账本无法加载: {exc}") from exc
    return lesson_ledger

# _prepare_project_profile is decoupled to launch_phases.pre_phase (DOC-072)

from .lesson_context import (
    lesson_context,
    derive_lesson_query,
    set_lesson_context,
    lesson_review_matches,
    lesson_status,
)

def fix_review(args: list[str]) -> int:
    """修复转绿后立即沉淀 incident：修前/修后、决策链、commit，不等结算。"""
    if "--ci-detect" in args and "--input" not in args and "--signature" not in args:
        report = _ci_detection_report(resolve_project_root())
        print(json.dumps(report, ensure_ascii=False, indent=2) if "--json" in args else report)
        return 0
    duration_value = _option_value(args, "--duration-seconds")
    round_value = _option_value(args, "--round")
    input_path = (
        Path(_option_value(args, "--input"))
        if _option_value(args, "--input") else None
    )
    if input_path is not None and not input_path.is_file():
        print(f"✗ incident JSON 不存在: {input_path}")
        return 1
    try:
        payload = (
            json.loads(input_path.read_text(encoding="utf-8"))
            if input_path is not None else {}
        )
        overrides = {
            "signature": _option_value(args, "--signature"),
            "summary": _option_value(args, "--summary"),
            "context": _option_value(args, "--context"),
            "before_snippet": _option_value(args, "--before"),
            "after_snippet": _option_value(args, "--after"),
            "diff_ref": _option_value(args, "--diff-ref"),
            "commit_ref": _option_value(args, "--commit-ref"),
            "decision": _option_value(args, "--decision"),
            "duration_seconds": float(duration_value) if duration_value is not None else None,
            "lifecycle_id": _option_value(args, "--lifecycle-id"),
            "round": int(round_value) if round_value is not None else None,
            "pattern_id": _option_value(args, "--pattern-id"),
            "finding_fingerprint": _option_value(args, "--finding-fingerprint"),
        }
        payload = {
            **payload,
            **{key: value for key, value in overrides.items() if value is not None},
        }
        ledger_fn = _get_ro_attr("_lesson_module", _get_ro_attr("lesson_module", lesson_module))
        ledger = ledger_fn()
        result = ledger.add_incident(LESSON_LEDGER_FILE, payload)
        incident = result["incident"]
        if "--ci-detect" in args:
            result["ci_proposal"] = _ci_detection_report(resolve_project_root(), payload)
            remediation = payload.get("remediation")
            if isinstance(remediation, dict) and remediation.get("verification_plan"):
                result["ci_proposal"]["remediation_verification_plan"] = remediation["verification_plan"]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"✗ fix-review 失败: {exc}")
        return 2
    lock_fn = _get_ro_attr("_runtime_lock", _get_ro_attr("runtime_lock", runtime_lock))
    load_fn = _get_ro_attr("_load_runtime", _get_ro_attr("load_runtime", load_runtime))
    save_fn = _get_ro_attr("_save_runtime", _get_ro_attr("save_runtime", save_runtime))
    with lock_fn():
        state = load_fn()
        incident_ids = list(state.get("lifecycle_incident_ids", []))
        if incident["incident_id"] not in incident_ids:
            incident_ids.append(incident["incident_id"])
        state["lifecycle_incident_ids"] = incident_ids[-100:]
        realized = list(state.get("realized_fix_incidents", []))
        if incident["incident_id"] not in realized:
            realized.append(incident["incident_id"])
        state["realized_fix_incidents"] = realized[-100:]
        recent = [str(x) for x in state.get("lesson_recent_signatures", [])]
        signature = str(incident["signature"])
        if signature not in recent:
            recent.insert(0, signature)
        state["lesson_recent_signatures"] = recent[:20]
        save_fn(state)
    print(f"✓ incident 已沉淀: {incident['incident_id']}（{incident['summary']}）")
    print(f"  签名: {incident['signature']}")
    if incident.get("commit_ref"):
        print(f"  commit: {incident['commit_ref']}")
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("ci_proposal"):
        print("CI 检测提案（仅生成 diff，不自动写入）：")
        print(json.dumps(result["ci_proposal"], ensure_ascii=False, indent=2))
    return 0

# DES-06: run_module_coverage now lives in launch_phases.common (shared layer);
# its only caller is post_phase. It is NOT imported here any more.

# _coverage_status is decoupled to launch_phases.pre_phase (DOC-072)

# _attach_coverage is decoupled to launch_phases.pre_phase (DOC-072)

# _print_coverage is decoupled to launch_phases.pre_phase (DOC-072)

def cmd_prompt(stage_name: str | None = None) -> int:
    """输出指定阶段或当前会话阶段的 Harness 提示词。"""
    try:
        from core.planning import prompt_harness
    except ImportError:
        err = "[launch_bugx] ✗ 提示词装配模块缺失"
        if i18n is not None:
            err = i18n.exception_text("orchestrator.prompt_module_missing", fallback=err)
        print(err)
        return 1


    stage = (stage_name or "").strip().lower()
    if not stage or stage == "current":
        if not SUPERVISOR_STATE.is_file():
            stage = "gate"
        else:
            try:
                supervisor = get_supervisor()
                state = supervisor.status()
                if state.status.value in {"COMPLETE", "FAILED"}:
                    stage = "repair"
                elif any(claim.state == "active" for claim in state.claims.values()):
                    stage = "mutate"
                elif state.coverage:
                    covered = sum(1 for v in state.coverage.values() if v == "covered")
                    if covered == 0:
                        stage = "explore"
                    else:
                        stage = "mutate"
                else:
                    stage = "gate"
            except Exception:
                stage = "gate"

    try:
        content = prompt_harness.load_stage(stage, root=resolve_project_root())
        print(content)
        return 0
    except (ValueError, FileNotFoundError) as exc:
        err = f"[launch_bugx] ✗ 获取提示词失败: {exc}"
        if i18n is not None:
            err = i18n.exception_text("orchestrator.prompt_fetch_failed", fallback=err, exc=exc)
        print(err)
        return 1



def polling(policy: str | None = None) -> int:
    if not _require_project_layout():
        return 1
    if policy is None or policy == "status":
        try:
            state = load_runtime()
        except (OSError, ValueError) as exc:
            print(f"[launch_bugx] ✗ 无法读取轮询策略: {exc}")
            return 1
        print(f"Round 续轮策略: {state.get('polling_policy') or '待询问'}")
        return 0
    if policy not in POLLING_POLICIES:
        print("[launch_bugx] 轮询策略只能是 auto 或 manual")
        return 2
    try:
        state, changed = set_polling_policy(policy)
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 设置轮询策略失败: {exc}")
        return 1
    print(f"✓ 已将 Round 报告后的续轮策略切换为 {policy}。")
    if state.get("active"):
        print("  当前轮继续执行；策略在本轮死亡后的安全边界生效。")
    elif (state.get("lifecycle_terminal")
          and state.get("lifecycle_last_life", 1) <= 0):
        transition = continue_after_death()
        if transition == "continued":
            print("↻ 已获自动续轮授权，正在开始下一 Round。")
        elif transition == "stopped-zero-findings":
            print("✝ 自动续轮已终止：自动开启的新 Round 累计未发现任何有效 Bug 即死亡。")
        elif transition == "start-failed":
            print("✗ 自动下一轮启动失败，已停止轮询；请检查基线与准备门禁。")
        else:
            print("  当前死亡状态已停止；如需新会话请先按流程 reset。")
    elif not changed:
        print("  策略未变化。")
    return 0

# pre is decoupled to launch_phases.pre_phase (DOC-072)

from core.launch_phases.finding_settlement import (
    findings_payload_path,
)

# post is decoupled to launch_phases.post_phase (DOC-072)

# _life_summary is decoupled to launch_phases.post_phase (DOC-072)

# _print_new_findings is decoupled to launch_phases.post_phase (DOC-072)




def legacy_supervisor_state(root: Path | None = None) -> dict[str, Any] | None:
    """读取 legacy audit-supervisor.json 的只读摘要。

    当 engine ledger 为空或不存在时，用于 status 命令的 fallback，
    使操作者能看到 legacy 运行态的实际进度（covered/findings/status）。
    """
    project_root = root or resolve_project_root()
    legacy_path = project_root / ".bugx" / "audit-supervisor.json"
    if not legacy_path.is_file():
        return None
    try:
        raw = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    coverage = raw.get("coverage", {})
    if not isinstance(coverage, dict):
        coverage = {}
    covered_count = sum(
        1 for v in coverage.values()
        if isinstance(v, dict) and v.get("status") == "covered"
    )
    return {
        "source": "legacy_audit_supervisor",
        "status": raw.get("status", "unknown"),
        "covered": covered_count,
        "total_units": len(coverage),
        "findings": len(raw.get("findings", [])),
        "round_no": raw.get("round_no", 0),
    }


def status(as_json: bool = False) -> int:
    if os.environ.get("BUGX_PROJECT_ROOT"):
        from core.runtime import project_init

        root = resolve_project_root()
        if project_init.is_uninitialized_project(root):
            payload = {
                "status": "UNINITIALIZED",
                "project_root": str(root),
                "next_action": {"command": "pre", "project_root": str(root)},
            }
            if as_json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"[BugX] 项目尚未初始化：{root}")
                print("[BugX] 启动 pre 时会自动准备项目状态。")
            return 0
    if not _require_project_layout():
        return 1
    try:
        coverage = coverage_status()
        runtime = attach_coverage(runtime_summary(), coverage)
    except (OSError, ValueError) as exc:
        if as_json:
            print(json.dumps({"runtime_error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[launch_bugx] ✗ {exc}")
        return 1
    legacy = legacy_supervisor_state()
    try:
        todo_path = audit_todo_path()
    except (OSError, ValueError, RuntimeError):
        todo_path = None
    try:
        startup_todo = startup_todo_path()
    except (OSError, ValueError, RuntimeError):
        startup_todo = None
    if as_json:
        try:
            result = subprocess.run(
                [sys.executable, str(VERIFY), "check"],
                capture_output=True, text=True, check=False, timeout=30,
                cwd=str(resolve_project_root()), env=child_environment(),
            )
        except subprocess.TimeoutExpired:
            print(json.dumps({
                "runtime": runtime,
                "coverage": coverage,
                "legacy_supervisor": legacy,
                "life": life_summary(),
                "integrity_ok": False,
                "integrity_output": "寿命校验超时（30 秒）",
            }, ensure_ascii=False, indent=2))
            return 124
        print(json.dumps({
            "runtime": runtime,
            "coverage": coverage,
            "legacy_supervisor": legacy,
            "life": life_summary(),
            "lessons": lesson_status(),
            "todo": {
                "path": str(todo_path) if todo_path else None,
                "exists": bool(todo_path and todo_path.is_file()),
                "derived": True,
            },
            "startup_todo": {
                "path": str(startup_todo) if startup_todo else None,
                "exists": bool(startup_todo and startup_todo.is_file()),
                "derived": True,
            },
            "integrity_ok": result.returncode == 0,
            "integrity_output": result.stdout.strip(),
        }, ensure_ascii=False, indent=2))
        return result.returncode
    print("=" * 56)
    print("bugx 当前状态")
    print("=" * 56)
    print_runtime(runtime)
    print("-" * 56)
    print_coverage(coverage, runtime)
    if legacy:
        print("-" * 56)
        print(f"Legacy Supervisor 状态: {legacy['status']}")
        print(f"  覆盖: {legacy['covered']}/{legacy['total_units']} 单元")
        print(f"  Findings: {legacy['findings']}")
        if legacy["covered"] > 0:
            print("  ℹ 以上为 legacy 运行态进度（engine ledger 可能尚未同步）")
    print("-" * 56)
    print("复盘账本")
    print(json.dumps(lesson_status(), ensure_ascii=False, indent=2))
    if todo_path:
        print("-" * 56)
        print(f"当前 Round TODO 清单：{todo_path}")
    if startup_todo:
        print(f"启动总览 TODO 清单：{startup_todo}")
    print("-" * 56)
    sys.stdout.flush()
    return run_verify_life("check")

def heartbeat() -> int:
    """Agent/编排器在安全边界更新心跳；暂停或中止时用非零码要求停止推进。"""
    try:
        state, updated = heartbeat_runtime()
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 心跳更新失败: {exc}")
        return 1
    if updated:
        print(f"[launch_bugx] 心跳已更新: run_id={state['run_id']}")
        return 0
    print(f"[launch_bugx] CONTROL_{state['state'].upper()}: 不得开始下一个审计单元")
    return 2

def pause() -> int:
    try:
        state, changed = pause_runtime()
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 暂停失败: {exc}")
        return 1
    if not changed:
        print(f"[launch_bugx] 当前状态为 {state['state']}，无需暂停")
        return 0
    print("[launch_bugx] 已请求在安全边界暂停，暂停期间不累计运行时长")
    return 0

def resume() -> int:
    try:
        state, changed = resume_runtime()
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 继续失败: {exc}")
        return 1
    if not changed:
        print(f"[launch_bugx] 当前状态为 {state['state']}，无需继续")
        return 0
    print(f"[launch_bugx] 已继续运行: run_id={state['run_id']}")
    return 0

def abort(reason: str | None = None) -> int:
    try:
        runtime, changed = close_runtime("aborted", reason=reason or "用户请求")
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 中止失败: {exc}")
        return 1
    if not changed:
        print(f"[launch_bugx] 当前状态为 {runtime['state']}，没有活动运行可中止")
        return 0
    try:
        runtime = close_session(reason="aborted", detail=reason or "用户请求")
    except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"[launch_bugx] ✗ Session 收尾失败: {exc}")
        return 1
    refresh_audit_todo(runtime, "aborted", reason or "用户请求")
    refresh_startup_todo(
        "end",
        reason=f"Session 已中止并完成资源收尾：{reason or '用户请求'}",
        environment=detect_startup_environment(),
    )
    print(f"[launch_bugx] 已请求安全中止，累计计时: "
          f"{format_duration(runtime['effective_total_seconds'])}")
    return 0

def bootstrap_coverage(*, confirmed: bool = False, fresh_coverage: bool = False) -> int:
    """受控创建覆盖清单；这是宿主菜单确认后的唯一 CLI 引导入口。"""
    if coverage_bootstrap is None:
        print("[launch_bugx] ✗ 覆盖清单引导模块不可用")
        return 1
    if not confirmed:
        print("[launch_bugx] 覆盖清单引导需要用户先选择‘运行引导创建’")
        return POLLING_DECISION_REQUIRED
    try:
        result = coverage_bootstrap.bootstrap(
            resolve_project_root(), confirmed=True, inherit_coverage=not fresh_coverage,
        )
    except (OSError, ValueError, RuntimeError, coverage_bootstrap.CoverageBootstrapError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(f"[launch_bugx] ✗ 覆盖清单引导创建失败（{code}）: {exc}")
        return 1
    if result.get("created"):
        inheritance = result.get("inherited_coverage")
        inherited_units = (
            inheritance.get("applied", 0)
            if isinstance(inheritance, dict) else 0
        )
        inherit_note = (
            f"，继承历史已覆盖 {inherited_units} 个单元"
            if inherited_units
            else ("（--fresh-coverage：忽略历史结论，全量重审）" if fresh_coverage else "")
        )
        print(
            f"[launch_bugx] ✓ 已创建覆盖清单：{result.get('units', 0)} 个最小单元"
            f"（{result.get('source', 'module_coverage')}）{inherit_note}"
        )
    else:
        print("[launch_bugx] ✓ 覆盖清单已存在，无需重复创建")
    print("[launch_bugx] 下一步：宿主自动重试 launch_bugx.py pre")
    return 0


# Compatibility aliases
_lesson_module = lesson_module
_legacy_supervisor_state = legacy_supervisor_state


def print_help() -> int:
    """Print the caller-facing launch protocol without touching project state."""
    # Stable 构建没有开发模式参数，帮助行由下方 DEV_ONLY 块注入。
    dev_flag_help = ""
    # [BEGIN DEV_ONLY: DEV_MODE_ARG_HELP]
    dev_flag_help = (
        "       --dev                    开发者极速免交互调试模式（等价于 --autonomous 的确定性缺省："
        "审查=mcp-auto 轮询=auto 提交=round 恢复=resume，并默认本地云同步；全程不发起交互提问）\n"
    )
    # [END DEV_ONLY: DEV_MODE_ARG_HELP]
    print(
        """BugX launch_bugx 启动协议（面向宿主 AI）

用途
  这是由宿主/主 Agent 控制的生命周期门禁与安全控制入口，不是审计 worker。
  --help/help/-h 只读，不读取或修改项目运行态。

宿主 AI 的顺序化用法
     1. 先识别当前宿主和交互能力（OpenCode/Claude Code/Cline/Pi、TUI/TTY/ACP/headless）。
     有原生 question 时，每次只发送一个 multiple=false 的单选问题；不要把后续问题
     一次性发出，也不要用普通文本替代单选。
     2. 普通启动：bugx engine --root . pre
     启动门禁会先处理 startup_action，再按顺序处理云端同步、审查方式、轮询策略、
     修复模式、提交策略和恢复；log-only 会自动跳过提交策略。
     进入项目后先生成并展示 .bugx/startup-todo.md，给人查看七个大步骤；正式 Round 建立后
     再展示当前生命周期 state/audit-todo.md 的细分进度。
     非 TTY 时读取唯一的 BUGX_INTERACTION_REQUEST JSON，并把当前问题转成原生单选。
     用户未作出有效选择、取消、超时或调用失败时，停留在当前 gate，不得进入下一步。
     如果已配置的 bugx-supervisor 在当前会话中尚未加载，startup_action gate 会在其它启动策略
      和恢复决策前处理：自动模式只能选择重启宿主后开始或暂不启动，不能静默降级为手动审查。
      自治启动：bugx engine --root . pre --autonomous
     或项目 profile 使用 active_profile=autonomous。自治启动不发起策略类交互；首次登录必须
     预先完成，MCP/版本/Git/证据/预算失败则 BLOCKED，宿主必须立即并发执行 workers 输出的
     原生 Task，禁止 workers --execute。
  3. 缺少 Supervisor MCP：明确提示「初始化项目 → 安装/配置 → 重启宿主 → 新会话重新启动 BugX」。
     选择安装后由宿主执行配置并停止当前会话；不能在 MCP 未加载时假装自动审查，也不能手动回退。
  4. 缺少覆盖清单：用户选择「运行引导创建」后，调用
     bugx engine --root . bootstrap-coverage --confirm
     新 Session 默认继承历史已覆盖单元（scope 完全一致才继承，备注含
     inherited_from 便于追溯）；需要全量重审时追加 --fresh-coverage。
     创建完成后自动重试 pre；hosts 只发现宿主适配包，不创建覆盖清单。
  5. pre 成功后保留输出的 BH_PRE_BASELINE，然后调用 workers 生成并发 worker 任务。
     在同一时间窗口启动 worker；每个 worker 按租约领取单元，并在单元切换/长命令结束后
     调用 heartbeat。心跳返回非零、CONTROL_PAUSED 或 CONTROL_ABORTED 时立刻停在安全边界。
  6. worker 完成后调用 post；需要结束当前 Round 时调用 post --final。先生成报告，
     再由 post 完成外部基线核对、证据/覆盖校验和结算。

命令
  pre [选项]                    启动门禁、快照外部基线并开始运行（--refresh-probes 强制刷新探针）
       --host <name>             显式绑定本次 Session 宿主（缺省读 runtime 或物理探查）
       --max-round-wall-seconds N
                                  覆盖本轮 Supervisor 墙上时间预算（正的有限秒数）
       --autonomous             使用已校验自治策略，跳过策略提问但不跳过硬门禁
"""
        + dev_flag_help
        + """       --startup-action start|restart|later
                                  处理 MCP_SESSION_STALE 的最终启动动作
       --ignore-global-budget     忽略全局每日 Token 与耗时预算熔断限制
       --refresh-probes          强制重新探测环境探针（跳过缓存）
       --approve-strategy        直接批准策略门禁方案（等价于 --strategy-plan approve）
       --strategy-plan <approve|reject|custom>
                                  回传策略门禁决策（宿主单选回传通道）
       --tooling-choice <tool=choice>
                                  回传工具门禁处置选择（可多次指定，如 platform=skip；
                                  也可通过环境变量 BUGX_TOOLING_CHOICES 回传）
       --bootstrap-coverage      启动时自动引导初始化缺失的覆盖清单
       --polling auto|manual     设置 Round 报告后的续轮策略
       --review-mode mcp-auto|manual
                                  设置代码审查方式
       --fix-mode auto|log-only
                                  设置待确认缺陷修复处理模式
       --commit-policy round|manual|life|none
                                  设置会话代码提交策略（none 归一为 manual）
       --session-recovery resume|new
                                  设置历史会话与断点恢复策略
       --report-policy none|local|submit
                                  设置报告留存与提交策略（仅 pre）
       --startup-interaction auto|prompt
                                  设置启动门禁人机交互策略
       --profile <name>          指定加载的项目配置 Profile
       --allow-serial            显式允许串行模式（需配合 --serial-reason）
       --serial-reason <reason>  声明串行调度原因
       --lesson-query <query>    经验库经验检索关键词
       --lesson-category <cat>   经验库检索类别
       --life-kind <mine|team>   生命周期归属类型（缺省 mine）
  status [--json]               查看当前运行态（--json 供宿主机器读取）
  workers [--count N] [--strategy risk|bfs|dfs|hybrid] [--host <name>]
          [--model <name>] [--provider <name>]
          [--max-workers N] [--subagent-type <name>] [--stagger-seconds N]
          [--execute] [--dry-run] [--brief]
                                生成并发 worker 提示词（默认风险优先）
  heartbeat                     更新安全心跳并检查控制门禁
      post [--final] [--allow-incomplete] [--allow-serial]
                                完成当前 Life；--final 封存 Round；显式允许不完整/修复型串行收尾
      post --dry-run             只读冻结运行快照并输出结构化预检结果；不执行结算
  polling auto|manual           设置 Round 报告后的续轮策略
  pause | resume                在安全边界暂停或继续
  abort --reason <原因>          请求安全中止并结算
  bootstrap-coverage --confirm  用户确认后创建缺失的覆盖清单
                                （默认继承历史 Session 的已覆盖单元；--fresh-coverage
                                忽略历史结论全量重审）
  fix-review [--ci-detect] [选项] 处理待确认的修复结果；可只读生成 CI 测试提案
  control status|pause|resume|abort  控制命令别名
  revalidate-candidates [--root DIR] [--dry-run] [--json]
                                复验候选 PoC，只生成 proposal，从不直接确认 Finding
       --auto-install-deps      允许安装缺失依赖；**必须**同时给出 --deps-lock
       --deps-lock <路径>       受信哈希锁，必须位于被审计项目之外；每行须为
                                `name==version --hash=sha256:<64hex>`。安装强制
                                --require-hashes --no-deps --only-binary=:all:，
                                在中性临时目录执行且清空 PIP_*/PYTHON* 环境变量
  help | --help | -h            显示本说明

保护边界
  pre/post/pause/resume/abort 的生命周期控制权属于宿主/主 Agent；AI 只能通过本入口
  调用，不能直接修改 runtime、外部基线、Supervisor 状态或覆盖清单。只有用户在当前 gate 明确选择
  「运行引导创建」后，才允许执行 bootstrap-coverage --confirm。
  版本契约失败只输出一个 version_contract gate；更新、重启宿主后最多重试一次，仍不一致
  就停止报告，禁止在同一会话或跨失败重试中递归循环。
  自动审查必须有已加载的 bugx-supervisor MCP；MCP 调用失败必须立即询问重试、
  手动回退或下次再说，不能静默吞错。

返回码
  0 成功   1 硬失败/门禁失败   2 需要用户决定或命令无效   124 超时

常用示例
  bugx engine --root . pre --review-mode mcp-auto --polling manual
  bugx engine --root . status --json
  bugx engine --root . workers
  bugx engine --root . heartbeat
  bugx engine --root . post --final
"""
    )
    return 0



_print_help = print_help
