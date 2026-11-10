#!/usr/bin/env python3
"""launch_phases/round_orchestrator.py - Round 推进调度、测试执行、发现标定与终局 post 报告结算。"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.launch_phases.worker_orchestrator import workers
# DES-06：本模块对 pre_phase 的依赖是**真实存在**的（不是死导入），见下方说明。
from core.launch_phases.pre_phase import (  # noqa: E402
    pre,
    coverage_status,
    attach_coverage,
    print_coverage,
)
# 修正说明（重要）：此前这里曾以「零使用」为由删掉整条 import，导致
# ``bugx pre`` / ``bugx audit`` 报 ``NameError: name 'pre' is not defined`` ——
# 这是一次由**错误的依赖核查**引入的回归（当时用 grep 判定"零使用"，
# 而该 grep 的 `\|` 交替在本机 shell 下静默失效，实际漏掉了 4 处真实调用）。
# 实测仍在使用：pre(:1360 分发 pre/audit)、coverage_status(:938)、
# attach_coverage(:939)、print_coverage(:994)。
# 只有 ``_prepare_project_profile`` 确实无人使用，故未回补。
# 方向说明：这条边是 round -> pre（与声明的 PRE -> ROUND 相反），但它不构成环
# （pre_phase 不反向 import round_orchestrator），且是分发所必需，故保留。
from core.launch_phases.post_phase import (
    post,
    get_last_post_failure,
    continue_after_death,
    close_session,
    life_summary,
    retry_pending_commit,
)

from core.launch_phases.common import (
    AGENT_DIR,
    IN_PROCESS_SCOPE,
    VERIFY,
    SUPERVISOR_STATE,
    LESSON_LEDGER_FILE,
    POLLING_POLICIES,
    POLLING_DECISION_REQUIRED,
    get_last_child_failure,
    resolve_project_root,
    uses_project_layout,
    child_environment,
    diagnostic_logger,
    run_verify_life,
    format_duration,
    isolated_runtime_scope,
    bugx_paths,
    bugx_logging,
    dependency_lock,
)

try:
    import core.i18n as i18n
except ImportError:
    try:
        from core import i18n
    except ImportError:
        i18n = None  # type: ignore[assignment]

from core.launch_phases.session_runtime import (
    refresh_runtime_paths,
    audit_todo_path,
    refresh_audit_todo,
    load_runtime,
    runtime_lock,
    save_runtime,
    diagnostic_context,
    append_runtime_event,
    runtime_summary,
    close_runtime,
    pause_runtime,
    resume_runtime,
    heartbeat_runtime,
    print_runtime,
    set_polling_policy,
)
from core.launch_phases.startup_gates import (
    startup_todo_path,
    startup_todo_content,
    refresh_startup_todo,
    detect_startup_environment,
    startup_environment_line,
    git_startup_gate,
    emit_startup_gate,
    pre_has_explicit_startup_choices,
    project_layout_present,
    startup_version_banner,
    startup_cloud_onboarding,
    startup_version_gate,
    schedule_startup_remote_version_check,
    emit_version_contract_gate,
    emit_project_layout_gate,
)
from core.launch_phases.supervisor_bridge import (
    get_supervisor,
    supervisor_mcp_snapshot,
    print_supervisor_mcp_snapshot,
)

from core.governance.token_budget import estimate_tokens
import core.secure_atomic as secure_atomic
import tempfile

from core.supervisor.supervisor_models import BudgetExhausted

try:
    from core.runtime import project_profile
except ImportError:
    project_profile = None

try:
    from core.ledger import lesson_ledger
except ImportError:
    lesson_ledger = None


_STATEFUL_COMMANDS = frozenset({
    "pre", "audit", "post", "polling", "heartbeat", "pause", "resume",
    "abort", "bootstrap-coverage", "fix-review", "revalidate-candidates",
    "control", "workers", "daemon",
})

@dataclass(frozen=True)
class RunResult:
    """Subprocess-compatible result returned by :func:`run_in_process`.

    ``stdout``, ``stderr`` and ``returncode`` intentionally mirror
    ``subprocess.CompletedProcess`` so existing protocol fixtures can switch
    execution lanes without changing their assertions.  ``exception`` is only
    populated when a library call raises outside the launcher's normal error
    handling; it is diagnostic metadata, never a second status channel.
    """

    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    exception: BaseException | None = None


def _normalise_in_process_argv(argv: list[str] | tuple[str, ...]) -> list[str]:
    """Accept both ``["pre", ...]`` and ``["launch_bugx.py", "pre", ...]``."""
    values = [str(value) for value in argv]
    if values and Path(values[0]).name == "launch_bugx.py":
        return values
    return ["launch_bugx.py", *values]


def _launcher_path(root: Path) -> Path:
    """返回给定项目根下真实存在的启动器路径（无布局假设）。

    两种受支持布局：``<root>/core/launch_bugx.py``（自包含引擎副本）与
    ``<root>/launch_bugx.py``（归档扁平副本）。都不存在时回退到
    ``common.AGENT_DIR``（全局引擎），仍不存在则返回扁平候选——让错误在
    调用方以"文件不存在"暴露，而不是在这里静默造一个假路径。
    """
    for candidate in (
        root / "core" / "launch_bugx.py",
        root / "launch_bugx.py",
        AGENT_DIR / "launch_bugx.py",
    ):
        if candidate.is_file():
            return candidate
    return root / "launch_bugx.py"


def run_in_process(
    argv: list[str] | tuple[str, ...],
    *,
    project_root: Path,
    stdin_content: str | None = None,
    capture_output: bool = True,
    env: dict[str, str] | None = None,
) -> RunResult:
    """Dispatch one ``launch_bugx`` command inside a recoverable sandbox.

    This is the library counterpart to ``python launch_bugx.py ...``.  It does
    not fake a command by printing a prompt: the real ``main`` dispatcher and
    all phase functions run, while :func:`isolated_runtime_scope` binds the
    sandbox paths and restores process state on every exit path.  Calls are
    serialized by the scope because the legacy phases still use process-global
    cwd/environment/module aliases; independent OS subprocesses remain the
    correct lane for signal, orphan-reaping and process-group tests.
    """
    command_argv = _normalise_in_process_argv(argv)
    if command_argv[1:2] == ["daemon"] and "--help" not in command_argv[2:]:
        message = "[launch_bugx] daemon 需要独立 OS 进程及信号生命周期，请通过 CLI/subprocess 执行。\n"
        if not capture_output:
            print(message, end="", file=sys.stderr)
        return RunResult(
            args=tuple(command_argv), returncode=2,
            stderr=message if capture_output else "",
        )
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    stdin = io.StringIO("" if stdin_content is None else str(stdin_content))
    returncode = 1
    captured_exception: BaseException | None = None

    # ``redirect_stdin`` is not present on every supported Python minor.  A
    # tiny local context manager keeps the harness compatible without changing
    # the production interaction layer.
    @contextlib.contextmanager
    def _redirect_stdin(stream):
        previous = sys.stdin
        sys.stdin = stream
        try:
            yield
        finally:
            sys.stdin = previous

    with isolated_runtime_scope(project_root, env=env) as root:
        previous_argv = list(sys.argv)
        # ``sys.argv[0]`` 必须是**真实启动器路径**，不能假设扁平的
        # ``<root>/launch_bugx.py``：自包含引擎副本布局是
        # ``<root>/core/launch_bugx.py``，硬编码扁平路径会让
        # ``bugx_paths.project_root()`` 与 ``resolve_project_root()``
        # 拿到一个不存在的脚本位置（并让下游以它为"仓库根"拼路径）。
        sys.argv = [str(_launcher_path(root)), *command_argv[1:]]
        try:
            output_context = (
                contextlib.ExitStack()
                if capture_output else contextlib.nullcontext()
            )
            with output_context as stack:
                if capture_output:
                    stack.enter_context(contextlib.redirect_stdout(stdout_buffer))
                    stack.enter_context(contextlib.redirect_stderr(stderr_buffer))
                with _redirect_stdin(stdin):
                    try:
                        value = main(command_argv)
                        returncode = int(value) if value is not None else 0
                    except SystemExit as exc:
                        captured_exception = exc
                        code = exc.code
                        returncode = int(code) if isinstance(code, int) else (
                            0 if code is None else 1
                        )
                        if code is not None and not isinstance(code, int):
                            print(code, file=sys.stderr)
                    except KeyboardInterrupt as exc:
                        captured_exception = exc
                        returncode = 130
                    except Exception as exc:  # pragma: no cover - defensive boundary
                        captured_exception = exc
                        returncode = 1
                        print(
                            f"[launch_bugx] ✗ 进程内分发异常: {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
        finally:
            sys.argv = previous_argv

    return RunResult(
        args=tuple(command_argv),
        returncode=returncode,
        stdout=stdout_buffer.getvalue() if capture_output else "",
        stderr=stderr_buffer.getvalue() if capture_output else "",
        exception=captured_exception,
    )


def _require_project_layout() -> bool:
    """Refuse stateful commands when the project runtime scope is unavailable."""
    if uses_project_layout():
        return True
    if os.environ.get("BUGX_AIR_GAP") == "1":
        return True
    print(
        "[launch_bugx] ✗ 缺少有效的 .bugx 项目运行布局；"
        "拒绝向全局引擎写入运行态。请先在项目根执行 `bugx init`。"
    )
    return False


def _resume_ticket_handoff(
    project_root: Path, resume_ticket_module, ticket: dict[str, Any],
) -> int | None:
    """Handle one resume ticket without duplicating host/CLI handoff logic."""
    startup_env = detect_startup_environment()
    gui_hosts = {"workbuddy", "qoder", "trea", "zcode"}
    if (getattr(startup_env, "interface", None) != "acp"
            and getattr(startup_env, "host", None) in gui_hosts):
        print("BUGX_RESUME_TICKET " + json.dumps(
            resume_ticket_module.resume_card(ticket), ensure_ascii=False,
        ))
        return POLLING_DECISION_REQUIRED
    if resume_ticket_module.consume_resume_ticket(project_root) is not None:
        # ``os.execv`` 只在**真实 OS 进程**车道成立：它把当前进程整块替换成
        # 新解释器。进程内车道（``run_in_process`` / 嵌入式宿主 / 测试进程）
        # 里执行它等于把宿主进程杀掉——实测整个 pytest 会话在一次 pre 之后
        # 静默退出（exit 0、零输出、后续用例全部消失），看起来像"全绿"。
        # 进程内车道改为就地继续：resume ticket 已经被消费，重启语义由
        # 调用方决定，绝不能劫持宿主进程。
        if not IN_PROCESS_SCOPE.get():
            os.execv(sys.executable, [sys.executable, *sys.argv])
    return None


def _peek_pending_resume_ticket(project_root: Path) -> dict[str, Any] | None:
    """Read-only peek used only for an incomplete GUI handoff.

    ``resume_ticket.consume_resume_ticket`` remains the authority for all
    mutation and claim semantics.  The peek exists because a GUI must display
    its card without deleting the ticket, and is limited to a layout already
    validated by ``prepare_project``.
    """
    target = project_root / ".bugx" / "resume_ticket.json"
    # 路径必须**归一化后**比较：macOS 上 ``tempfile`` 给出 ``/var/...`` 而
    # ``Path.resolve()`` 给出 ``/private/var/...``，直接用字符串比对会让同一目录
    # 判成不同项目 → GUI resume 卡片永远不显示（build 286 全量验证发现）。
    resolved_root = project_root.resolve()
    try:
        if target.is_symlink() or not target.is_file():
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    recorded_root = str(payload.get("project_root") or "")
    if not recorded_root or Path(recorded_root).resolve() != resolved_root:
        return None
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)) or float(expires_at) <= time.time():
        return None
    return payload


def _emit_post_attempt_event(
    event: str, *, level: int = 20, **fields: object,
) -> bool:
    """Write a post command event through the acknowledged local JSONL path."""
    try:
        from core.launch_phases.post_observability import emit_attempt_event
        return emit_attempt_event(event, level=level, **fields)
    except Exception:
        return False


def _log_early_post_failure(
    exit_code: int, reason: str, *, gate_name: str = "dispatch_runtime_paths",
    gate_status: str = "blocked",
) -> None:
    """Keep post failures structured even when startup gates return early."""
    try:
        from core.launch_phases.post_observability import (
            attempt_scope, current_attempt, log_attempt_finished,
            record_dispatch_failure,
        )
        if current_attempt() is None:
            with attempt_scope("dispatcher"):
                _emit_post_attempt_event("core_command_started")
                record_dispatch_failure(
                    gate_name, reason, exit_code=exit_code,
                    gate_status=gate_status,
                )
                _emit_post_attempt_event(
                    "core_command_finished", level=40,
                    exit_code=exit_code, status="failed",
                )
                log_attempt_finished(exit_code)
        else:
            _emit_post_attempt_event("core_command_started")
            record_dispatch_failure(
                gate_name, reason, exit_code=exit_code,
                gate_status=gate_status,
            )
            _emit_post_attempt_event(
                "core_command_finished", level=40,
                exit_code=exit_code, status="failed",
            )
    except Exception:
        # Telemetry cannot change the early gate's business verdict.
        pass


def _post_dry_run_cli() -> int:
    """Print a read-only frozen-snapshot report without entering post dispatch."""
    from core.launch_phases import post_dry_run

    try:
        snapshot = post_dry_run.capture_snapshot(resolve_project_root())
        report = post_dry_run.evaluate_snapshot(snapshot)
    except post_dry_run.SnapshotInvalid as exc:
        report = post_dry_run.blocked_report(exc.reason_code)
    except Exception:
        # Dry-run failures are structured and fail-closed. Never leak arbitrary
        # exception detail into the machine-readable report.
        report = post_dry_run.blocked_report("snapshot_capture_error")
    report = dict(report)
    report["mode"] = "post_dry_run"
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") == "passed" and report.get("coherence") == "proven" else 1

try:
    from core.coverage import coverage_bootstrap
except ImportError:
    coverage_bootstrap = None



from core.launch_phases.orchestrator_controls import (
    abort,
    bootstrap_coverage,
    cmd_prompt,
    fix_review,
    heartbeat,
    legacy_supervisor_state,
    lesson_module,
    pause,
    polling,
    print_help,
    resume,
    status,
)
from core.launch_phases.candidate_revalidation import (
    candidate_dependencies,
    candidate_inventory,
    changed_test_files,
    ci_detection_report,
    declared_dependencies,
    inside_project,
    revalidate_candidates,
    safe_reproduction_command,
    verify_candidate_assets,
)

# Compatibility aliases
_lesson_module = lesson_module
_inside_project = inside_project
_candidate_inventory = candidate_inventory
_candidate_dependencies = candidate_dependencies
_declared_dependencies = declared_dependencies
_verify_candidate_assets = verify_candidate_assets
_safe_reproduction_command = safe_reproduction_command
_changed_test_files = changed_test_files
_ci_detection_report = ci_detection_report
_legacy_supervisor_state = legacy_supervisor_state
_print_help = print_help

def _option_value(args: list[str], option: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == option:
            return args[i + 1] if i + 1 < len(args) else None
        if arg.startswith(f"{option}="):
            return arg.split("=", 1)[1]
    return None

def _reject_legacy_share_policy(argv: list[str]) -> int | None:
    """Reject the removed pre flag before any project/runtime side effect."""
    if any(
        arg == "--share-policy" or arg.startswith("--share-policy=")
        for arg in argv[1:]
    ):
        print(
            "[launch_bugx] ✗ --share-policy 已移除；请使用 "
            "`pre --report-policy none|local|submit`。"
        )
        return 2
    return None


def main(argv: list[str]) -> int:
    """Bind one context-local attempt around CLI post dispatch."""
    values = list(argv)
    known = {
        "pre", "post", "status", "polling", "heartbeat", "pause", "resume",
        "abort", "bootstrap-coverage", "fix-review", "audit", "control",
        "workers", "judge", "findings-confirm", "revalidate-candidates", "prompt", "daemon", "help", "--help", "-h",
        "coverage",
    }
    if values and (values[0] in known or values[0].startswith("-")):
        values = ["launch_bugx.py", *values]
    legacy_share_policy = _reject_legacy_share_policy(values)
    if legacy_share_policy is not None:
        return legacy_share_policy
    command = values[1] if len(values) > 1 else "status"
    if command != "post" or "--dry-run" in values[2:]:
        return _main_impl(values)
    from core.launch_phases.post_observability import (
        attempt_scope, current_attempt, log_attempt_finished,
        record_unhandled_exception,
    )
    with attempt_scope("dispatcher") as attempt:
        try:
            result = _main_impl(
                values, _post_attempt_token=attempt.attempt_token,
            )
        except BaseException as exc:
            exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            if isinstance(exc, SystemExit):
                code = exc.code
                exit_code = code if isinstance(code, int) and code != 0 else 1
            active = current_attempt()
            if active is not None and not active.failure_recorded:
                record_unhandled_exception(exc, exit_code=exit_code)
            _emit_post_attempt_event(
                "core_command_finished", level=40,
                exit_code=exit_code, status="failed",
            )
            log_attempt_finished(exit_code)
            raise
        log_attempt_finished(result)
        return result


def _main_impl(
    argv: list[str], *, _post_attempt_token: object | None = None,
) -> int:
    _KNOWN_CMDS = {
        "pre", "post", "status", "polling", "heartbeat", "pause", "resume",
        "abort", "bootstrap-coverage", "fix-review", "audit", "control",
        "workers", "judge", "findings-confirm", "revalidate-candidates", "prompt", "daemon", "help", "--help", "-h",
    }
    if argv and (argv[0] in _KNOWN_CMDS or argv[0].startswith("-")):
        argv = ["launch_bugx.py"] + list(argv)
    cmd = argv[1] if len(argv) > 1 else "status"
    legacy_share_policy = _reject_legacy_share_policy(argv)
    if legacy_share_policy is not None:
        return legacy_share_policy
    _HELP_TOKENS = {"help", "--help", "-h"}
    if cmd in _HELP_TOKENS or any(token in _HELP_TOKENS for token in argv[2:]):
        # 帮助契约要求三种写法都只读：`pre help` / `pre -h` 若不在这里截住，
        # 会落进真实 pre 相位并写启动态，违反文档承诺。
        return _print_help()
    if cmd == "status" and os.environ.get("BUGX_PROJECT_ROOT"):
        # Querying a fresh project must not require the layout that only pre
        # creates.  Keep this path read-only and recheck inside status().
        try:
            from core.runtime import project_init

            if project_init.is_uninitialized_project(resolve_project_root()):
                return status(as_json="--json" in argv[2:])
        except (OSError, ValueError, RuntimeError) as exc:
            if "--json" in argv[2:]:
                print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
            else:
                print(f"[launch_bugx] ✗ 项目状态检查失败：{exc}")
            return 1
    if cmd in {"pre", "audit"} and "--allow-degraded" in argv[2:]:
        # DOC-236 C1（A4）：降级放行已废除。守卫必须位于**所有** pre/audit 出口
        # 之前——放在尾部 dispatch 会被前置硬门禁（交互/版本/云/策略门禁）提前
        # 返回绕过，从而变成"静默忽略"而不是"显式拒绝"。
        print("[launch_bugx] ✗ --allow-degraded 已废除（DOC-236 C1 不允许降级）："
              "请先自愈安装/配置；确无自动安装器的专有工具，"
              "在 .bugx/config.json 的 tooling_exemptions 中显式豁免。")
        return 1
    # DOC-236 C5：只做轻量布局/config/fingerprint 准备；宿主、交互、Git、
    # 版本与云门禁全部通过后才进入完整画像/探针/coverage pipeline。
    # DOC-174 的 pending ticket 只做一次廉价恢复，不重新启动昂贵初始化。
    project_root: Path | None = None
    project_init_module = None
    resume_ticket_module = None
    if cmd in {"pre", "audit"} and os.environ.get("BUGX_PROJECT_ROOT"):
        try:
            from core.runtime import project_init as project_init_module
            from core.runtime import resume_ticket as resume_ticket_module

            project_root = Path(os.environ["BUGX_PROJECT_ROOT"]).expanduser().resolve()
            project_init_module.prepare_project(project_root, AGENT_DIR)
            # A headless/ACP restart can safely consume its one-use ticket and
            # re-exec before gates, preserving the historical CLI handoff.
            # GUI hosts must keep the ticket until the reload has passed all
            # hard gates; otherwise the card would point at a deleted ticket.
            startup_env = detect_startup_environment()
            gui_hosts = {"workbuddy", "qoder", "trea", "zcode"}
            is_gui_resume = (
                getattr(startup_env, "interface", None) != "acp"
                and getattr(startup_env, "host", None) in gui_hosts
            )
            if is_gui_resume:
                pending_ticket = _peek_pending_resume_ticket(project_root)
                config = project_init_module._read_config(
                    project_root / ".bugx" / "config.json",
                )
                if (isinstance(pending_ticket, dict)
                        and config.get("status") != "ready"):
                    print("BUGX_RESUME_TICKET " + json.dumps(
                        resume_ticket_module.resume_card(pending_ticket),
                        ensure_ascii=False,
                    ))
                    return POLLING_DECISION_REQUIRED
            else:
                pending_ticket = resume_ticket_module.consume_resume_ticket(project_root)
                if isinstance(pending_ticket, dict):
                    os.execv(sys.executable, [sys.executable, *sys.argv])
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"[launch_bugx] ✗ 项目初始化/续做失败：{exc}")
            return 1
    if cmd == "post" and "--dry-run" in argv[2:]:
        return _post_dry_run_cli()
    logger = None
    if cmd == "post":
        # Set up diagnostics before layout/version/runtime-path gates can reject.
        logger = diagnostic_logger(cmd)
        from core.launch_phases.common import set_diagnostic_logger
        set_diagnostic_logger(logger)
    if cmd in _STATEFUL_COMMANDS and not _require_project_layout():
        if cmd == "post":
            _log_early_post_failure(
                1, "project_layout_unavailable", gate_name="dispatch_layout",
            )
        return 1
    startup_version_banner(
        stream=sys.stderr if "--json" in argv[2:] else sys.stdout
    )
    if logger is None:
        logger = diagnostic_logger(cmd)
        from core.launch_phases.common import set_diagnostic_logger
        set_diagnostic_logger(logger)
    environment = detect_startup_environment()
    # Stable 剥离下方 DEV_ONLY 块后开发模式参数不存在，哨兵值让下游全部回落到生产缺省。
    dev_mode_requested = False
    # [BEGIN DEV_ONLY: DEV_MODE_ARG]
    dev_mode_requested = "--dev" in argv[2:]
    # [END DEV_ONLY: DEV_MODE_ARG]
    autonomous_requested = "--autonomous" in argv[2:] or dev_mode_requested
    if not autonomous_requested and cmd in {"pre", "audit"}:
        try:
            from core.runtime import session_profile
            early_profile = session_profile.load_profile(root=resolve_project_root()) or {}
            autonomous_requested = (
                str(early_profile.get("mode", "")).strip().lower() == "autonomous"
            )
        except Exception:
            autonomous_requested = False
    startup_todo_path = None
    if cmd in {"pre", "audit"} and project_layout_present():
        startup_todo_path = refresh_startup_todo(
            "environment",
            reason="启动入口已进入，正在收集并校验环境信息",
            environment=environment,
        )
        if startup_todo_path:
            print(f"[BugX] 启动总览 TODO：{startup_todo_path}")
            print(startup_todo_content(
                "environment",
                reason="启动入口已进入，正在收集并校验环境信息",
                environment=environment,
            ).rstrip())
    if cmd in {"pre", "audit"} and environment is not None:
        # 诊断行必须走 **stderr**：stdout 是机读协议通道（`BUGX_RESUME_TICKET <json>`
        # 等标记要求独占可解析）。此前打到 stdout，导致宿主侧按「第一行」解析时
        # 拿到 `[BugX] Startup environment: ...`（build 276 全量验证发现）。
        print(
            f"[BugX] Startup environment: {startup_environment_line(environment)}",
            file=sys.stderr,
        )
        if uses_project_layout():
            print_supervisor_mcp_snapshot(supervisor_mcp_snapshot())
        # 哨兵必须在 DEV_ONLY 块之外：Stable 剥离后下方条件仍会读取该名字。
        bypass_headless_gate = False
        # [BEGIN DEV_ONLY: FAST_VERIFICATION_HEADLESS_BYPASS]
        try:
            from core.runtime.interaction import try_get_preset_runtime_config
        except ImportError:
            try:
                from core.runtime.interaction import try_get_preset_runtime_config
            except ImportError:
                try_get_preset_runtime_config = None
        if try_get_preset_runtime_config is not None:
            if try_get_preset_runtime_config(resolve_project_root() if uses_project_layout() else None):
                bypass_headless_gate = True
        # [END DEV_ONLY: FAST_VERIFICATION_HEADLESS_BYPASS]
        if (uses_project_layout()
                # 缺字段（最小化 mock 环境）不得让启动流程崩：诊断/门禁按「未知」
                # 处理，不等于 blocked，因此不额外拦截。
                and getattr(environment, "interaction", None) == "blocked"
                and not autonomous_requested
                and not bypass_headless_gate
                and not pre_has_explicit_startup_choices(argv[2:])):
            emit_startup_gate(
                "startup_interaction",
                "当前宿主没有可用的原生单选控件或真实 TTY",
                [
                    {
                        "label": "重启宿主并使用原生单选",
                        "description": "重启当前已识别的交互宿主后继续",
                        "value": "restart_host",
                        "action": "restart_host",
                    },
                    {
                        "label": "在真实 TTY 中运行",
                        "description": "通过方向键单选菜单继续",
                        "value": "run_tty",
                        "action": "run_in_tty",
                    },
                    {
                        "label": "下次再说",
                        "description": "停止本轮启动",
                        "value": "later",
                        "action": "stop_startup",
                    },
                ],
                environment=environment,
                detail=environment.reason,
            )
            return POLLING_DECISION_REQUIRED
    # Git is an environment hard prerequisite, not a later tool preference.
    # Check it before version, cloud, MCP and coverage gates so no Session or
    # other runtime state can be created without a reproducible code baseline.
    if cmd in {"pre", "audit"} and project_layout_present():
        if not git_startup_gate(environment, auto_init=dev_mode_requested):
            return POLLING_DECISION_REQUIRED
    if project_layout_present():
        version_ok, version_detail, gate_kind = startup_version_gate()
        if not version_ok:
            if gate_kind == "project_layout":
                print(f"[launch_bugx] ✗ 项目状态检查失败：{version_detail}")
                emit_project_layout_gate(version_detail, environment)
            else:
                print(f"[launch_bugx] ✗ 启动版本门禁失败：{version_detail}")
                emit_version_contract_gate(version_detail, environment)
            if cmd == "post":
                _log_early_post_failure(
                    POLLING_DECISION_REQUIRED,
                    "startup_version_gate_failed",
                    gate_name="dispatch_version",
                )
            return POLLING_DECISION_REQUIRED
        if cmd in {"pre", "audit"}:
            refresh_startup_todo(
                "startup",
                reason="环境与版本门禁通过，准备执行串行启动配置",
                environment=environment,
            )
    # 非阻断版本检查：向服务端查询是否有新版本（air-gap / 网络不可达时静默跳过）。
    # PERF-07：调度到守护线程执行，绝不在启动关键路径上同步等待。
    if cmd in {"pre", "audit"}:
        schedule_startup_remote_version_check(
            stream=sys.stderr if "--json" in argv[2:] else sys.stdout,
        )
    if uses_project_layout():
        try:
            refresh_runtime_paths()
        except (OSError, ValueError, RuntimeError) as exc:
            if logger is not None and cmd != "post":
                bugx_logging.log_event(
                    logger, "core_command_failed", str(exc), level=40,
                    command=cmd, exception_type=type(exc).__name__, exc_info=True,
                )
            if cmd == "post":
                _log_early_post_failure(
                    1, "runtime_paths_unavailable",
                    gate_name="dispatch_runtime_paths",
                    gate_status="error",
                )
            print(f"[launch_bugx] ✗ .bugx 运行布局不可用: {exc}")
            return 1
    if cmd in {"pre", "audit"} and uses_project_layout():
        try:
            retry_pending_commit()
        except (OSError, ValueError, RuntimeError) as exc:
            if logger is not None:
                bugx_logging.log_event(
                    logger, "git_recovery_failed", str(exc), level=40,
                    command=cmd, exception_type=type(exc).__name__, exc_info=True,
                )
    if cmd in {"pre", "audit"}:
        profile_cfg = {}
        try:
            from core.runtime import session_profile
            profile_cfg = session_profile.load_profile(
                _option_value(argv[2:], "--profile"), root=resolve_project_root(),
            ) or {}
        except Exception:
            profile_cfg = {}
        autonomous_mode = (
            "--autonomous" in argv[2:]
            or dev_mode_requested
            or str(profile_cfg.get("mode", "")).strip().lower() == "autonomous"
        )
        cloud_policy = _option_value(argv[2:], "--cloud-sync") or profile_cfg.get("cloud_sync")
        # [BEGIN DEV_ONLY: DEV_MODE_CLOUD_LOCAL]
        if dev_mode_requested and not cloud_policy:
            # 开发模式默认本地：无登录态的新机器上强制 sync 会被云端门禁判成
            # BLOCKED（绝对阻断），"极速"反而起不来。显式 --cloud-sync sync
            # 或项目/全局配置仍是同步的唯一入口，与 --dev 帮助文案
            # 「使用本地/显式云同步」保持一致。
            cloud_policy = "local"
        # [END DEV_ONLY: DEV_MODE_CLOUD_LOCAL]
        onboarding_result = startup_cloud_onboarding(
            cloud_policy, autonomous=autonomous_mode,
        )
        if onboarding_result != 0:
            return onboarding_result
        refresh_startup_todo(
            "startup",
            reason="云端同步门禁已处理，等待其余启动配置和恢复决策",
            environment=environment,
        )
        # DOC-236 C5：完整项目画像、可选 MCP 探针、prep/coverage bootstrap
        # 必须位于宿主/交互/Git/版本硬门禁和云决策之后，且早于 pre() 的
        # 策略门禁与探针剪枝。保持 report_policy 的 C6 参数转发不变。
        if project_root is not None and project_init_module is not None:
            try:
                init_state = project_init_module.run_init_pipeline(
                    project_root, AGENT_DIR,
                )
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                print(f"[launch_bugx] ✗ 项目初始化/续做失败：{exc}")
                return 1
            ticket = init_state.get("resume_ticket")
            if isinstance(ticket, dict) and resume_ticket_module is not None:
                handoff = _resume_ticket_handoff(
                    project_root, resume_ticket_module, ticket,
                )
                if handoff is not None:
                    return handoff
            elif resume_ticket_module is not None:
                # A GUI host leaves the ticket on disk while it reloads the
                # workspace.  On re-entry the fast path has no new ticket;
                # consume the old one only after every startup gate passed.
                resume_ticket_module.consume_resume_ticket(project_root)
    diag_ctx = diagnostic_context()
    cmd_started = time.monotonic()
    if cmd == "post":
        _emit_post_attempt_event("core_command_started")
    elif logger is not None:
        bugx_logging.log_event(
            logger, "core_command_started", command=cmd,
            **diag_ctx,
        )
    try:
        result: int
        if cmd == "post":
            _share = "share" if "--share" in argv[2:] else (
                "no-share" if "--no-share" in argv[2:] else None)
            post_started = time.monotonic()
            phase_status = "failed"
            try:
                from core.launch_phases.post_observability import current_attempt
                attempt = current_attempt()
                with bugx_logging.timed_phase(
                    logger, "post_settlement", post_event_schema=1,
                    post_attempt_id=(attempt.post_attempt_id if attempt else None),
                ) as tracker:
                    try:
                        result = post(
                            final="--final" in argv[2:], share=_share,
                            allow_incomplete="--allow-incomplete" in argv[2:],
                            allow_serial="--allow-serial" in argv[2:],
                            _post_attempt_token=_post_attempt_token,
                        )
                        phase_status = "ok" if result == 0 else "failed"
                        if result != 0:
                            tracker.mark_failed(
                                get_last_post_failure() or "post_return_nonzero",
                            )
                    except BaseException:
                        tracker.mark_failed(
                            get_last_post_failure() or "post_exception",
                        )
                        raise
            finally:
                phase_duration = max(0.0, time.monotonic() - post_started)
                _emit_post_attempt_event(
                    "phase.post_settlement.finished",
                    level=20 if phase_status == "ok" else 40,
                    phase="post_settlement", status=phase_status,
                    duration_seconds=round(phase_duration, 3),
                )
                threshold = bugx_logging.DEFAULT_SLOW_THRESHOLD_SECONDS
                configured_threshold = os.environ.get("BUGX_SLOW_THRESHOLD_SECONDS")
                if configured_threshold:
                    try:
                        threshold = float(configured_threshold)
                    except ValueError:
                        pass
                if threshold > 0 and phase_duration >= threshold:
                    _emit_post_attempt_event(
                        "slow_operation_warning", level=30,
                        phase="post_settlement", status=phase_status,
                        duration_seconds=round(phase_duration, 3),
                    )
        elif cmd == "status":
            result = status(as_json="--json" in argv[2:])
        elif cmd == "polling":
            value = argv[2] if len(argv) > 2 else None
            result = polling(value)
        elif cmd == "heartbeat":
            result = heartbeat()
        elif cmd == "pause":
            result = pause()
        elif cmd == "resume":
            result = resume()
        elif cmd == "abort":
            result = abort(_option_value(argv[2:], "--reason"))
        elif cmd == "bootstrap-coverage":
            result = bootstrap_coverage(
                confirmed="--confirm" in argv[2:],
                fresh_coverage="--fresh-coverage" in argv[2:],
            )
        elif cmd in {"pre", "audit"}:
            # `--allow-degraded` 已在 `_main_impl` 入口统一拒绝（A4），此处不再重复判定。
            pre_args = argv[2:]
            is_dev = dev_mode_requested
            strategy_plan_val = None
            if "--approve-strategy" in pre_args or is_dev:
                strategy_plan_val = "approve"
            else:
                strategy_plan_val = _option_value(pre_args, "--strategy-plan")
            tooling_choices: dict[str, str] = {}
            for i, arg in enumerate(pre_args):
                if arg == "--tooling-choice" and i + 1 < len(pre_args):
                    val = pre_args[i + 1]
                    if "=" in val:
                        t, a = val.split("=", 1)
                        tooling_choices[t.strip()] = a.strip()
                elif arg.startswith("--tooling-choice="):
                    val = arg.split("=", 1)[1]
                    if "=" in val:
                        t, a = val.split("=", 1)
                        tooling_choices[t.strip()] = a.strip()
            with bugx_logging.timed_phase(logger, "pre_flight_gates"):
                result = pre(
                    _option_value(pre_args, "--polling") or ("auto" if is_dev else None),
                    allow_serial="--allow-serial" in pre_args,
                    serial_reason=_option_value(pre_args, "--serial-reason"),
                    lesson_query=_option_value(pre_args, "--lesson-query") or "",
                    lesson_category=_option_value(pre_args, "--lesson-category"),
                    review_mode=_option_value(pre_args, "--review-mode") or ("mcp-auto" if is_dev else None),
                    fix_mode=_option_value(pre_args, "--fix-mode") or ("auto" if is_dev else None),
                    life_kind=_option_value(pre_args, "--life-kind") or "mine",
                    commit_policy=_option_value(pre_args, "--commit-policy") or ("round" if is_dev else None),
                    session_recovery=_option_value(pre_args, "--session-recovery") or ("resume" if is_dev else None),
                    startup_action=_option_value(pre_args, "--startup-action") or ("start" if is_dev else None),
                    strategy_plan=strategy_plan_val,
                    tooling_choices=tooling_choices or None,
                    report_policy=_option_value(pre_args, "--report-policy"),
                    replay_share_pending_force="--replay-share-pending" in pre_args,
                    startup_interaction=_option_value(pre_args, "--startup-interaction"),
                    bootstrap_coverage="--bootstrap-coverage" in pre_args,
                    profile=_option_value(pre_args, "--profile"),
                    ignore_global_budget="--ignore-global-budget" in pre_args,
                    refresh_probes="--refresh-probes" in pre_args,
                    autonomous="--autonomous" in pre_args or is_dev,
                    max_round_wall_seconds=_option_value(
                        pre_args, "--max-round-wall-seconds",
                    ),
                    host=_option_value(pre_args, "--host"),
                )
        elif cmd == "control":
            sub = argv[2] if len(argv) > 2 else "status"
            if sub == "pause":
                result = pause()
            elif sub == "resume":
                result = resume()
            elif sub == "abort":
                result = abort(_option_value(argv[3:], "--reason"))
            else:
                result = status(as_json="--json" in argv[2:])
        elif cmd == "findings-confirm":
            from core.launch_phases import finding_confirm_phase
            result = finding_confirm_phase.main(argv[2:])
        elif cmd == "judge":
            from core.launch_phases import judge_phase
            result = judge_phase.main(argv[2:])
        elif cmd == "coverage":
            # F-RSI-20261110-020：coverage_cli 此前是孤儿模块——
            # AI_REFERENCE §六·五 记载的 `bugx engine coverage
            # review-packages/review-apply` 无任何接线（顶层/engine 均不可达）。
            from core.coverage import coverage_cli
            result = coverage_cli.main(argv[2:])
        elif cmd == "workers":
            raw_count = _option_value(argv[2:], "--count")
            strategy = _option_value(argv[2:], "--strategy") or "risk"
            execute = "--execute" in argv[2:]
            dry_run = "--dry-run" in argv[2:]
            brief = "--brief" in argv[2:]
            provider_arg = _option_value(argv[2:], "--provider")
            model_arg = _option_value(argv[2:], "--model")
            executable_arg = _option_value(argv[2:], "--executable")
            host_arg = _option_value(argv[2:], "--host")
            subagent_type_arg = _option_value(argv[2:], "--subagent-type")
            max_workers_raw = _option_value(argv[2:], "--max-workers")
            stagger_raw = _option_value(argv[2:], "--stagger-seconds")
            try:
                parsed_count = int(raw_count) if raw_count else None
            except ValueError:
                parsed_count = None
            try:
                parsed_max_workers = int(max_workers_raw) if max_workers_raw else None
            except ValueError:
                parsed_max_workers = None
            try:
                parsed_stagger = float(stagger_raw) if stagger_raw else None
            except ValueError:
                parsed_stagger = None
            with bugx_logging.timed_phase(logger, "worker_execution", worker_count=parsed_count):
                result = workers(
                    parsed_count,
                    strategy=strategy,
                    execute=execute,
                    host=host_arg,
                    max_workers=parsed_max_workers,
                    subagent_type=subagent_type_arg,
                    stagger_seconds=parsed_stagger,
                    model=model_arg,
                    provider=provider_arg,
                    executable=executable_arg,
                    dry_run=dry_run,
                    brief=brief,
                )
        elif cmd == "fix-review":
            result = fix_review(argv[2:])
        elif cmd == "revalidate-candidates":
            result = revalidate_candidates(argv[2:])
        elif cmd == "prompt":
            stage_val = _option_value(argv[2:], "--stage")
            result = cmd_prompt(stage_val)
        elif cmd == "daemon":
            from core.runtime import daemon_runner
            result = daemon_runner.main(argv[2:])
        else:
            print(
                f"[launch_bugx] 未知命令: {cmd}"
                "（可选 pre/post/status/polling/heartbeat/pause/resume/abort/"
                "bootstrap-coverage/fix-review/revalidate-candidates/audit/control/workers/prompt/daemon）"
            )
            result = 2
    except BudgetExhausted as exc:
        cmd_duration = round(max(0.0, time.monotonic() - cmd_started), 3)
        if cmd == "post":
            try:
                from core.launch_phases.post_observability import (
                    current_attempt, record_unhandled_exception,
                )
                active = current_attempt()
                if active is not None and not active.failure_recorded:
                    record_unhandled_exception(exc, exit_code=1)
            except Exception:
                pass
            _emit_post_attempt_event(
                "core_command_finished", level=40,
                exit_code=1, status="failed", duration_seconds=cmd_duration,
            )
        elif logger is not None:
            bugx_logging.log_event(
                logger, "budget_exhausted", str(exc), level=40,
                command=cmd, exception_type=type(exc).__name__,
                duration_seconds=cmd_duration,
                **diag_ctx,
            )
        print(json.dumps({
            "status": "BUDGET_EXHAUSTED",
            "error_type": type(exc).__name__,
            "reason": str(exc),
            "recovery": [
                "读取 bugx engine --root . status 确认 budget_reasons",
                "必要时执行 audit_supervisor.py reset-budget --force 开启新的预算 epoch",
            ],
        }, ensure_ascii=False))
        return 1
    except Exception as exc:  # pragma: no cover - exercised by integration failures.
        cmd_duration = round(max(0.0, time.monotonic() - cmd_started), 3)
        if cmd == "post":
            try:
                from core.launch_phases.post_observability import (
                    current_attempt, record_unhandled_exception,
                )
                active = current_attempt()
                if active is not None and not active.failure_recorded:
                    record_unhandled_exception(exc, exit_code=1)
            except Exception:
                pass
            _emit_post_attempt_event(
                "core_command_finished", level=40,
                exit_code=1, status="failed", duration_seconds=cmd_duration,
            )
        elif logger is not None:
            bugx_logging.log_event(
                logger, "core_command_crashed", str(exc), level=40,
                command=cmd, exception_type=type(exc).__name__,
                duration_seconds=cmd_duration,
                **diag_ctx, exc_info=True,
            )
        err = f"[launch_bugx] ✗ 未处理异常: {type(exc).__name__}: {exc}"
        if i18n is not None:
            err = i18n.exception_text("orchestrator.unhandled", fallback=err, exc_type=type(exc).__name__, exc=exc)
        print(err)
        return 1

    cmd_duration = round(max(0.0, time.monotonic() - cmd_started), 3)
    if cmd == "post":
        _emit_post_attempt_event(
            "core_command_finished", level=40 if result != 0 else 20,
            exit_code=result,
            status="failed" if result != 0 else "ok",
            duration_seconds=cmd_duration,
        )
    elif logger is not None:
        if cmd in {"pre", "audit"} and result != 0 and bugx_logging is not None:
            bugx_logging.log_event(
                logger, "pre_failed", "pre 门禁失败；已记录最近失败详情",
                level=40, exit_code=result,
                duration_seconds=cmd_duration,
                failure_detail=get_last_child_failure() or "pre 返回非零，但未产生子命令输出",
            )
        if result != 0:
            err_detail = (get_last_post_failure() if cmd == "post" else "") or (get_last_child_failure() if cmd in {"pre", "audit"} else "")
            err_msg = f"{cmd} 命令执行失败 (exit_code={result})" + (f": {err_detail}" if err_detail else "")
            bugx_logging.log_event(
                logger, "core_command_finished", err_msg,
                level=40, command=cmd, exit_code=result,
                duration_seconds=cmd_duration,
                error=err_detail or f"exit_code={result}",
                **diag_ctx,
            )
        else:
            bugx_logging.log_event(
                logger, "core_command_finished", command=cmd,
                exit_code=result, duration_seconds=cmd_duration, **diag_ctx,
            )
    return result
