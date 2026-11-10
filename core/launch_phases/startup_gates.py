#!/usr/bin/env python3
"""launch_phases/startup_gates.py - 启动门禁判定：Git边界、版本一致性、云端状态、交互菜单。"""

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
    STARTUP_TODO_ITEMS,
    STARTUP_TODO_PHASE_INDEX,
    STARTUP_TODO_GATE_PHASE,
    HOST_DISPLAY_NAMES,
    ISOLATED_REMOTE_VERSION_RESULT,
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
    runtime_lock,
    append_runtime_event,
)

try:
    from core.runtime import startup_environment
except ImportError:
    startup_environment = None

try:
    from core.runtime import interaction as _interaction
    RADIO_UNAVAILABLE = _interaction.RADIO_UNAVAILABLE
except ImportError:
    _interaction = None
    RADIO_UNAVAILABLE = object()

try:
    from core.cloud_sync import cloud_client as _cloud_client_module
    from core.cloud_sync.cloud_client import CloudClient
except ImportError:
    _cloud_client_module = None
    CloudClient = None

try:
    from core.infra import client_metadata as _client_metadata_module
except ImportError:
    _client_metadata_module = None

try:
    from core.cloud_sync.cloud_onboard import get_state, sync_layers
    from core.cloud_sync.onboard_wizard import maybe_onboard
except ImportError:
    maybe_onboard = get_state = sync_layers = None


# A preset is a responsibility-governance convenience, never an authority to
# waive a release gate.  Version, integrity, and identity failures therefore
# bypass preset resolution and remain blocked until the gate itself passes.
CRITICAL_STARTUP_GATES = frozenset({
    "version", "version_contract", "project_layout", "integrity", "integrity_contract", "identity",
    "identity_contract", "版本声明一致性", "源码防外泄门禁", "目标端物理隔离门禁",
})

try:
    from core.coverage import coverage_bootstrap
except ImportError:
    coverage_bootstrap = None

try:
    from core.coverage import module_coverage
except ImportError:
    module_coverage = None

#: 受信清单文件名（与 ``coverage_bootstrap.MANIFEST_NAME`` 同源；该模块可能导入失败，
#: 这里保留字面量以免显式清单入口在缺模块时静默失效——缺模块会在解析阶段暴露）。
COVERAGE_MANIFEST_NAME = getattr(coverage_bootstrap, "MANIFEST_NAME", "module-coverage.md")

try:
    from core.runtime import project_profile
except ImportError:
    project_profile = None

try:
    from core.ledger import lesson_ledger
except ImportError:
    lesson_ledger = None


# DESIGN_SPEC（启动门禁反自旋）：任何启动门禁失败时都必须注入强停止词与停机指示，
# 遏制模型在无外部事实的情况下多轮重试、发散自旋。此处是**唯一**文案来源。
FATAL_BLOCKED_BANNER = (
    "[FATAL_BLOCKED] 阻断已确凿，严禁模型自行连续重试或发散自旋推理。"
    "【强制停机指示】请立即停止调用工具，直接向用户呈现上述阻断原因与修复建议。"
)


def _startup_todo_phase_for_gate(gate: str) -> str:
    """Map a structured startup gate to one of the seven overview stages."""
    if str(gate).startswith("tooling:"):
        return "tools"
    return STARTUP_TODO_GATE_PHASE.get(str(gate), "startup")

def startup_todo_path() -> Path | None:
    """Return the project-level startup overview path.

    This view intentionally lives outside Session/Round storage: it must be
    available before a formal runtime identity exists and must not overwrite a
    completed or interrupted lifecycle's derived Round checklist.
    """
    if not project_layout_present():
        return None
    return resolve_project_root() / ".bugx" / "startup-todo.md"

def startup_todo_content(phase: str = "environment", *, gate: str | None = None,
                          reason: str | None = None, environment=None) -> str:
    """Build the human-readable project-level launch overview."""
    phase = str(phase or "environment")
    active_index = STARTUP_TODO_PHASE_INDEX.get(phase, 0)
    env = environment
    host = getattr(env, "host", "unknown") if env is not None else "unknown"
    interface = getattr(env, "interface", "unknown") if env is not None else "unknown"
    interaction = getattr(env, "interaction", "unknown") if env is not None else "unknown"
    lines = [
        "# BugX 启动总览 TODO",
        "",
        f"- 项目：`{resolve_project_root()}`",
        f"- 宿主/界面：`{host}` / `{interface}` / `{interaction}`",
        f"- 当前阶段：`{phase}`",
        f"- 当前门禁：`{gate or 'none'}`",
        f"- 最后校准：`{iso_time(time.time())}`",
        "",
    ]
    for index, (title, detail) in enumerate(STARTUP_TODO_ITEMS):
        if index < active_index:
            marker = "x"
        elif index == active_index:
            marker = "~"
        else:
            marker = " "
        lines.append(f"- [{marker}] {index + 1}. {title}")
        lines.append(f"  - {detail}")
    if reason:
        lines.extend(["", f"- 当前说明：{reason}"])
    lines.extend([
        "",
        "> 本文件是给用户和 Agent 查看全局进度的派生视图，不是账本、覆盖清单、Finding 来源或完成判定器。",
        "> 正式 Round 开始后，详细进度位于当前生命周期 `state/audit-todo.md`。",
        "> 阶段专精提示词（Harness）：可通过 `bugx engine --root . prompt --stage <gate|explore|mutate|repair>` 或 MCP `bugx_get_stage_harness` 动态获取。",
    ])
    return "\n".join(lines) + "\n"

def refresh_startup_todo(phase: str = "environment", *, gate: str | None = None,
                          reason: str | None = None, environment=None) -> Path | None:
    """Refresh the project-level launch overview without blocking BugX."""
    path = startup_todo_path()
    if path is None:
        return None
    try:
        secure_atomic.atomic_write_text(
            path,
            startup_todo_content(phase, gate=gate, reason=reason, environment=environment),
            mode=0o600,
            parent_mode=0o700,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"  （启动总览 TODO 刷新失败，不影响启动门禁：{exc}）")
        return None
    return path

@functools.lru_cache(maxsize=None)
def _startup_environment_cached(module, root: str):
    """Memoised ``detect()`` call behind :func:`detect_startup_environment`."""
    return module.detect(Path(root))

def detect_startup_environment():
    """Detect the real host/UI surface before any startup gate is shown.

    结果按 (``startup_environment`` 模块, 项目根) 在**进程内**缓存：宿主进程树、
    TTY 能力与 Git 就绪状态在一次会话里不会改变，而本函数在启动链路上被调用
    20 余次，每次重探都要同步执行 7 次 git 子进程与最多 8 层进程祖先 ``ps``
    （实测单次 65-108 ms，PERF-01）。

    缓存键包含模块对象本身，因此测试替换 ``startup_environment`` 桩件时自然落到
    新键，旧缓存不会掩盖后到的可用模块；模块不可用（``None``）时直接返回
    ``None`` 且不写缓存；探测抛出的异常也不会被缓存，下一次调用会重新探测。

    需要强制重探时调用 :func:`clear_startup_environment_cache`（例如在同一进程内
    改动显式会话宿主或 ``BUGX_INTERACTION`` 等交互钩子之后）。
    """
    if startup_environment is None:
        return None
    try:
        return _startup_environment_cached(startup_environment, str(resolve_project_root()))
    except (OSError, RuntimeError, ValueError):
        return None

def clear_startup_environment_cache() -> None:
    """清空启动环境探测缓存，并连带清空底层 ``startup_environment`` 的缓存。"""
    _startup_environment_cached.cache_clear()
    if startup_environment is not None:
        clear = getattr(startup_environment, "clear_cache", None)
        if callable(clear):
            clear()

def startup_environment_line(environment) -> str:
    if environment is None:
        return "host=unknown interface=unknown interaction=blocked git=unknown"
    git = getattr(environment, "git", {}) or {}
    # 诊断行必须**永不因缺字段而崩**：它出现在 pre 的早期，任何 AttributeError
    # 都会把一次正常启动变成 traceback（实测：测试用 SimpleNamespace 构造最小
    # 环境时 `host_source`/`interaction` 缺失即崩溃）。缺字段降级为 unknown。
    def _field(name: str, default: str = "unknown") -> str:
        value = getattr(environment, name, None)
        if value is None:
            return default
        if isinstance(value, bool):
            # 布尔字段在摘要行里必须是小写 true/false（既有契约：宿主探针与
            # 测试都按该形态解析；直接 str() 会给出 "True"）。
            return "true" if value else "false"
        return str(value)

    return (
        f"host={_field('host')} source={_field('host_source')} "
        f"interface={_field('interface')} interaction={_field('interaction')} "
        f"stdin_tty={_field('stdin_tty', 'unknown')} "
        f"stdout_tty={_field('stdout_tty', 'unknown')} "
        f"git={git.get('status', 'unknown')}"
    )

def _startup_host_label(environment=None, *, fallback: str = "当前宿主") -> str:
    """Return the host name for user-facing actions, never a fixed IDE name."""
    environment = environment or detect_startup_environment()
    host = getattr(environment, "host", None) if environment is not None else None
    host = str(host or "").strip().casefold()
    return HOST_DISPLAY_NAMES.get(host, fallback)

def git_startup_gate(environment=None, *, auto_init: bool = False) -> bool:
    """Require a readable Git baseline before creating a BugX Session.

    The core never installs Git or initializes a repository unless ``auto_init`` is
    requested (developer mode). In that mode it may create the repository, set a
    repository-local commit identity when none is configured, and record an empty
    baseline commit; it never stages or commits the user's working tree. Any Git
    problem that remains still goes through the structured gate so the host can act
    on an explicit user choice.
    """
    snapshot = getattr(environment, "git", None) if environment is not None else None
    if not isinstance(snapshot, dict) and startup_environment is not None:
        try:
            snapshot = startup_environment.detect_git(resolve_project_root())
        except (OSError, RuntimeError, ValueError):
            snapshot = None
    snapshot = snapshot if isinstance(snapshot, dict) else {
        "status": "unusable", "ready": False,
        "detail": "无法读取 Git 环境快照", "root": str(resolve_project_root()),
    }
    if snapshot.get("ready") is True:
        return True

    status = str(snapshot.get("status") or "unusable")
    if auto_init and status in {"not-repository", "unborn", "identity-missing"}:
        root = resolve_project_root()
        try:
            if status == "not-repository":
                subprocess.run(
                    ["git", "init"], cwd=root, check=True, capture_output=True,
                )
            # 仓库级身份只在缺失时补齐，绝不改动全局 git 配置。
            for key, value in (
                ("user.name", "BugX Dev"),
                ("user.email", "bugx-dev@localhost"),
            ):
                probe = subprocess.run(
                    ["git", "config", "--get", key],
                    cwd=root, capture_output=True, text=True,
                )
                if probe.returncode != 0 or not probe.stdout.strip():
                    subprocess.run(
                        ["git", "config", "--local", key, value],
                        cwd=root, check=True, capture_output=True,
                    )
                    print(f"[launch_bugx] ✓ 开发模式已补齐仓库级 Git 身份 {key}={value}")
            if status in {"not-repository", "unborn"}:
                staged = subprocess.run(
                    ["git", "diff", "--cached", "--quiet"], cwd=root, capture_output=True,
                )
                if staged.returncode == 0:
                    subprocess.run(
                        [
                            "git", "commit", "--allow-empty", "--no-verify",
                            "-m", "chore: bugx dev init baseline",
                        ],
                        cwd=root, check=True, capture_output=True,
                    )
                    print("[launch_bugx] ✓ 开发模式已建立空的 Git 基线提交（未暂存工作区文件）")
                else:
                    print(
                        "[launch_bugx] ⚠ 暂存区已有内容，跳过自动基线提交以免把工作区文件卷入"
                    )
        except (OSError, subprocess.SubprocessError) as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            print(f"[launch_bugx] ⚠ 自动初始化 Git 失败：{str(stderr).strip() or exc}")
        if startup_environment is not None:
            try:
                refreshed = startup_environment.detect_git(root)
            except (OSError, RuntimeError, ValueError):
                refreshed = None
            if isinstance(refreshed, dict):
                snapshot = refreshed
                if refreshed.get("ready") is True:
                    return True
                status = str(refreshed.get("status") or status)
    if status == "missing":
        question = "项目缺少 Git，是否安装 Git？"
        options = [
            {
                "label": "立即安装 Git",
                "description": "由宿主执行系统 Git 安装，完成后重新检测",
                "value": "install",
                "action": "install_git",
            },
            {
                "label": "退出",
                "description": "停止本次 BugX 启动，不创建 Session",
                "value": "exit",
                "action": "stop_startup",
            },
        ]
    elif status == "not-repository":
        question = "当前项目不是 Git 仓库，是否在项目根目录初始化？"
        options = [
            {
                "label": "初始化 Git 仓库",
                "description": "在确认的项目根目录执行 git init，完成后重新检测",
                "value": "init",
                "action": "git_init",
            },
            {
                "label": "退出",
                "description": "停止本次 BugX 启动，不创建 Session",
                "value": "exit",
                "action": "stop_startup",
            },
        ]
    else:
        question = "Git 环境未准备好，是否修复后重新检测？"
        options = [
            {
                "label": "修复 Git 环境",
                "description": "补齐 HEAD、提交身份或 Git 元数据写入能力后重新检测",
                "value": "repair",
                "action": "repair_git",
            },
            {
                "label": "退出",
                "description": "停止本次 BugX 启动，不创建 Session",
                "value": "exit",
                "action": "stop_startup",
            },
        ]
    detail = str(snapshot.get("detail") or "Git 环境检查失败")
    if snapshot.get("root"):
        detail = f"项目根目录：{snapshot['root']}；{detail}"
    if emit_startup_gate(
        "git_environment", question, options,
        environment=environment,
        detail=detail,
    ) is True:
        if startup_environment is not None:
            try:
                refreshed = startup_environment.detect_git(resolve_project_root())
                if isinstance(refreshed, dict) and refreshed.get("ready") is True:
                    return True
            except Exception:
                pass
    return False

def emit_startup_gate(
    gate: str,
    question: str,
    options: list[dict[str, object]],
    *,
    environment=None,
    detail: str = "",
) -> bool:
    """Emit exactly one machine-readable interaction request for the host Agent.

    The core process cannot invoke OpenCode/Claude UI tools itself. In a
    headless child process it must return one gate, not print a text survey;
    the host Agent renders this payload with its native single-select tool and
    then retries the command with the persisted choice.

    ``value`` is the stable return value consumed by the host; ``action`` is
    only an opaque business whitelist identifier. Requiring a value here
    prevents a future gate from falling back to label parsing in adapters.
    """
    # [BEGIN DEV_ONLY: STARTUP_GATE_PRESET]
    if (
        gate not in CRITICAL_STARTUP_GATES
        and _interaction is not None
        and hasattr(_interaction, "try_resolve_startup_gate_preset")
    ):
        try:
            resolved, _ = _interaction.try_resolve_startup_gate_preset(
                gate, options, root=resolve_project_root()
            )
            if resolved:
                return True
        except Exception:
            pass
    # [END DEV_ONLY: STARTUP_GATE_PRESET]

    environment = environment or detect_startup_environment()
    if _interaction is None:
        raise RuntimeError("统一交互组件不可用，拒绝降级为旧文本门禁")
    try:
        interaction_options = tuple(
            _interaction.InteractionOption.from_mapping(option) for option in options
        )
        interaction_question = _interaction.InteractionQuestion(
            question_id=gate,
            phase="startup",
            kind="select",
            title="BugX 启动需要你的选择",
            question=question,
            options=interaction_options,
            multiple=False,
            detail=detail,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"启动门禁 {gate} 定义非法: {exc}") from exc
    refresh_startup_todo(
        _startup_todo_phase_for_gate(gate),
        gate=gate,
        reason=detail or question,
        environment=environment,
    )
    result = _interaction.emit_request(interaction_question, environment)
    if result.status != _interaction.NEEDS_DECISION:
        raise RuntimeError(f"无法输出交互请求 {gate}: {result.reason or result.status}")
    return False

def pre_has_explicit_startup_choices(argv: list[str]) -> bool:
    """Whether a non-interactive caller supplied every user startup choice."""
    if "--approve-strategy" in argv or any(
        arg == "--strategy-plan" and i + 1 < len(argv) and argv[i + 1] in ("approve", "approve_and_start", "1")
        or arg.startswith(("--strategy-plan=approve", "--strategy-plan=1"))
        for i, arg in enumerate(argv)
    ):
        return True
    if "--startup-interaction" in argv or any(arg.startswith("--startup-interaction=") for arg in argv):
        return True
    return all(option in argv for option in (
        "--review-mode", "--polling", "--fix-mode", "--commit-policy",
    ))

def project_layout_present() -> bool:
    """Distinguish a standalone protocol test from an installed project.

    ``uses_project_layout`` intentionally returns false for a broken layout
    so legacy tests can run in a bare directory.  Startup version checks must
    still run for a present-but-invalid ``.bugx`` directory and fail closed.
    """
    try:
        marker = resolve_project_root() / ".bugx"
        if not (marker.exists() or marker.is_symlink()):
            return False
        # Legacy protocol tests may create .bugx/findings only.  An installed
        # BugX project is identified by its manifest or explicit project root;
        # Layout 2 has no project core reference.
        return (
            (marker / "manifest.json").exists()
            or bool(os.environ.get("BUGX_PROJECT_ROOT"))
        )
    except (OSError, ValueError, RuntimeError):
        return True



# ---------------------------------------------------------------------------
# DOC-256: 架构解耦导出：版本门禁与交互决策
# ---------------------------------------------------------------------------
from core.launch_phases.startup_version_gate import (
    normalize_cli_version,
    installed_cli_version,
    global_engine_root,
    version_marker,
    startup_release_version,
    startup_version_banner,
    startup_cloud_onboarding,
    run_cloud_boundary_sync,
    startup_version_gate,
    pending_remote_version_result,
    remote_version_check_result,
    clear_remote_version_check_cache,
    record_remote_version_result,
    schedule_startup_remote_version_check,
    startup_remote_version_check,
    emit_version_contract_gate,
    emit_project_layout_gate,
    REMOTE_VERSION_CHECK_TIMEOUT_SECONDS,
    REMOTE_VERSION_LOCK,
    REMOTE_VERSION_RESULT,
    REMOTE_VERSION_THREAD,
)
_normalize_cli_version = normalize_cli_version
_installed_cli_version = installed_cli_version
_global_engine_root = global_engine_root
_startup_release_version = startup_release_version
_pending_remote_version_result = pending_remote_version_result
_record_remote_version_result = record_remote_version_result
_startup_remote_version_check = startup_remote_version_check
_REMOTE_VERSION_LOCK = REMOTE_VERSION_LOCK
_REMOTE_VERSION_RESULT = REMOTE_VERSION_RESULT
_REMOTE_VERSION_THREAD = REMOTE_VERSION_THREAD

from core.launch_phases.startup_interaction import (
    compile_strategy_plan,
    run_verify_life_direct,
    apply_approved_strategy,
    mark_strategy_customized,
    save_strategy_draft,
    strategy_plan_gate,
    stdin_tty,
    tty_menu_available,
    radio_menu,
    interaction_environment,
    ask_interaction,
    answered_value,
    answered_values,
    map_polling_answer,
    ask_polling_policy,
    map_mode_answer,
    map_commit_policy_answer,
    ask_commit_policy,
    map_review_mode_answer,
    ask_review_mode,
    ask_fix_mode,
    map_report_policy_answer,
    ask_report_policy,
    report_policy_startup_gate,
    interactive_startup_decisions,
)
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
_report_policy_startup_gate = report_policy_startup_gate
_interactive_startup_decisions = interactive_startup_decisions


def _ask_coverage_bootstrap() -> bool | None:
    """通过统一组件询问是否创建覆盖清单。"""
    result = ask_interaction(
        "coverage_bootstrap", "覆盖清单尚未建立，是否运行引导创建？", [
        ("运行引导创建", "创建本生命周期的最小覆盖单元，完成后继续启动"),
        ("下次再说", "停止本轮启动，不创建或修改运行态清单"),
        ],
        default=0,
    )
    value = answered_value(result)
    if value == "1":
        return True
    if value == "2":
        return False
    return None

#: 显式审计范围入口（`autonomous --coverage-manifest` / 手工 ``BUGX_COVERAGE_MANIFEST``）。
#:
#: 为什么需要它：gate 的 reconcile 把清单 scope 与**全仓** discovery 结果比对，任何
#: 「只审一个子集」的清单都会被判成漂移并触发全量重扫。对大仓（引擎自审、monorepo、
#: 任何带 vendored 目录的项目）这等于宣告无法限定审计范围——实测 worktree 场景下
#: 458 个引擎文件全部进单元，`--max-turns 4` 必然 INCOMPLETE，而清单 bootstrap 的
#: 交互门禁在非 TTY 下静默阻塞。显式清单把「审哪些单元」交回调用方，gate 只做
#: **真实性校验**（文件在、可解析、单元路径真实存在），不再 reconcile 到全仓。
COVERAGE_MANIFEST_ENV = "BUGX_COVERAGE_MANIFEST"


def _explicit_coverage_manifest() -> Path | None:
    raw = (os.environ.get(COVERAGE_MANIFEST_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _manifest_unit_path(row: object) -> str:
    """取单元路径（去反引号/``::symbol`` 后缀/尾斜杠），与 reconcile 的 scope 口径一致。"""

    path = str(getattr(row, "path", "") or "").strip().strip("`").strip()
    if "::" in path:
        path = path.split("::", 1)[0].strip()
    return path.rstrip("/")


def _manifest_has_progress(payload: bytes) -> bool:
    """清单里是否已有「已覆盖」的真实进度（模板/全 pending 不算）。"""

    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        return True
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 13:
            continue
        if "covered" in cells[12]:
            return True
    return False


def _install_explicit_coverage_manifest(root: Path) -> int | None:
    """把显式清单落位为受信清单；``None`` 表示本次没有显式清单。

    校验只做**真实性**（对齐 ``coverage_bootstrap`` 的 fail-closed 口径）：
    拒绝符号链接、拒绝解析失败、拒绝空清单、拒绝指向项目外或不存在路径的单元。
    校验通过后原样落位到 ``state_root/module-coverage.md``——后续 Supervisor /
    ``start --coverage-manifest`` / 校验器读的都是同一个文件，不存在两份真相。
    """

    manifest = _explicit_coverage_manifest()
    if manifest is None:
        return None
    if not uses_project_layout():
        print(f"[launch_bugx] ✗ {COVERAGE_MANIFEST_ENV} 只在项目级布局下可用")
        return 1
    if manifest.is_symlink() or not manifest.is_file():
        print(f"[launch_bugx] ✗ 显式覆盖清单不可用: {manifest}")
        return 1
    try:
        rows, errors = module_coverage.parse_manifest(manifest)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"[launch_bugx] ✗ 显式覆盖清单解析失败: {exc}")
        return 1
    if errors:
        print(f"[launch_bugx] ✗ 显式覆盖清单有解析错误: {errors[0]}")
        return 1
    units = [_manifest_unit_path(row) for row in rows]
    units = [unit for unit in units if unit and unit not in {"-", "待定", "N/A"}]
    if not units:
        print("[launch_bugx] ✗ 显式覆盖清单没有任何可审计单元")
        return 1
    resolved_root = root.resolve()
    outside: list[str] = []
    for unit in units:
        target = (resolved_root / unit).resolve()
        if resolved_root not in target.parents and target != resolved_root:
            outside.append(unit)
        elif not target.exists():
            outside.append(unit)
    if outside:
        print(
            "[launch_bugx] ✗ 显式覆盖清单包含项目外或不存在的单元: "
            + ", ".join(sorted(outside)[:5])
        )
        return 1
    # 落位前用**同一套**门禁规则预检：post 阶段会用 startup_gate_errors 的质量口径
    # （symbol 级单元 + 符号索引/关系图 marker + 完整 plan 字段）复核清单，
    # 只做轻校验的显式清单会在跑到后段才被拒（实测 anchored 轮次就是这样变成
    # FAILED）。这里提前 fail-fast，把原因在启动阶段说清楚。
    try:
        from core.coverage import coverage_gate
    except ImportError:
        coverage_gate = None
    if coverage_gate is not None:
        try:
            gate_errors = coverage_gate.startup_gate_errors(resolved_root, manifest)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[launch_bugx] ✗ 显式覆盖清单预检失败: {exc}")
            return 1
        if gate_errors:
            print(
                "[launch_bugx] ✗ 显式覆盖清单未通过覆盖门禁（需 symbol 级单元 + "
                "符号索引/关系图 marker + 完整计划字段）: " + "; ".join(gate_errors[:3])
            )
            return 1
    scope = bugx_paths.require(resolved_root)
    destination = scope.state_root / COVERAGE_MANIFEST_NAME
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = manifest.read_bytes()
        if destination.is_file():
            existing = destination.read_bytes()
            if existing != payload:
                # 只在**已有真实进度**时拒绝覆盖。清单是「哪些单元算审计完成」的唯一
                # 账本，抹掉 covered 行就是数据丢失；但全 pending 的清单（模板 / 刚
                # bootstrap 出来还没跑过）没有进度可丢，必须允许显式清单替换——
                # 否则 `run_init_pipeline` 先建的模板清单会把显式审计范围永久挡在门外。
                if _manifest_has_progress(existing):
                    print(
                        "[launch_bugx] ✗ 项目覆盖清单已有进度，拒绝用显式清单覆盖: "
                        f"{destination}（如需改用请先备份并移除该文件）"
                    )
                    return 1
                destination.write_bytes(payload)
        else:
            destination.write_bytes(payload)
    except OSError as exc:
        print(f"[launch_bugx] ✗ 显式覆盖清单落位失败: {exc}")
        return 1
    print(
        f"[launch_bugx] ✓ 使用显式覆盖清单（{len(units)} 个单元，审计范围由调用方声明，"
        "不 reconcile 到全仓）"
    )
    return 0


def coverage_startup_gate(
    bootstrap_confirmed: bool = False, *, autonomous: bool = False,
) -> int:
    """Run the host-mediated coverage bootstrap gate before starting a run."""
    explicit = _install_explicit_coverage_manifest(resolve_project_root())
    if explicit is not None:
        return explicit
    if coverage_bootstrap is None or not uses_project_layout():
        return 0
    try:
        state = coverage_bootstrap.inspect(resolve_project_root())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[launch_bugx] ✗ 覆盖清单状态无法读取: {exc}")
        return 1
    if state.get("ready"):
        return 0
    if state.get("needs_reconcile"):
        try:
            res = coverage_bootstrap.bootstrap(resolve_project_root(), confirmed=True, reconcile=True)
            # 宿主 AI 是覆盖范围的审核兜底：对账差异（新增/失效移除的具体单元）
            # 必须显性输出，否则引擎自产工件混入审计范围时只会看到一个数量，
            # 审核信号在 bootstrap 内算完即丢（实测 .bugx-project.json 污染
            # 以"1 个单元"静默通过）。
            diff = res.get("inventory_diff") or {}
            added = sorted(str(item) for item in (diff.get("added") or []))
            removed = sorted(str(item) for item in (diff.get("removed") or []))
            detail = ""
            if added:
                detail += "新增: " + ", ".join(added)
            if removed:
                if detail:
                    detail += "；"
                detail += "失效移除: " + ", ".join(removed)
            print(
                f"[launch_bugx] ✓ 覆盖清单完成增量对账自愈（清除失效幽灵行并同步新单元）：{res.get('units', 0)} 个单元"
                + (f"（{detail}）" if detail else "")
                + "；继续启动。"
            )
            return 0
        except coverage_bootstrap.CoverageBootstrapError as exc:
            print(f"[launch_bugx] ✗ 覆盖清单增量重扫对账失败: {exc}")
            return 1
    if state.get("blocked"):
        print(f"[launch_bugx] ✗ {state.get('detail') or '覆盖清单门禁失败'}")
        print("  活动生命周期不会被静默重建；请从备份恢复后再继续。")
        return 1
    if bootstrap_confirmed or autonomous:
        choice = True
    else:
        choice = _ask_coverage_bootstrap()
    if choice is None:
        emit_startup_gate(
            "coverage_bootstrap",
            "覆盖清单尚未建立，是否运行引导创建？",
            [
                {
                    "label": "运行引导创建",
                    "description": "由受控引导创建本生命周期最小覆盖单元，然后自动重试启动",
                    "value": "bootstrap",
                    "action": "bootstrap_coverage",
                },
                {
                    "label": "下次再说",
                    "description": "停止本轮启动，不创建或修改任何运行态清单",
                    "value": "later",
                    "action": "stop_startup",
                },
            ],
            detail=str(state.get("detail") or "覆盖清单缺失"),
        )
        return POLLING_DECISION_REQUIRED
    if not choice:
        print("[launch_bugx] 本轮未创建覆盖清单，启动已停止。")
        return POLLING_DECISION_REQUIRED
    try:
        result = coverage_bootstrap.bootstrap(resolve_project_root(), confirmed=True)
    except coverage_bootstrap.CoverageBootstrapError as exc:
        print(f"[launch_bugx] ✗ 覆盖清单引导创建失败（{exc.code}）: {exc}")
        return 1
    print(
        f"[launch_bugx] ✓ 覆盖清单引导创建完成：{result.get('units', 0)} 个最小单元；"
        "继续启动。"
    )
    return 0

def _ask_aborted_session_choice() -> str | None:
    """通过统一组件询问已中断会话的显式去向：恢复或新建。"""
    result = ask_interaction(
        "aborted_session", "检测到未完成的已中断会话，如何继续？", [
        ("恢复上一次会话", "继续未完成的 Session，保留已结算 Life 和覆盖进度"),
        ("新建本次会话", "保留旧 Session 为 abandoned，创建全新 Session/首个 Round"),
        ],
        default=0,
    )
    value = answered_value(result)
    return {"1": "resume", "2": "new"}.get(value or "")

def aborted_session_gate(
    previous_runtime: dict, explicit_choice: str | None = None, *,
    autonomous: bool = False,
) -> tuple[int, str | None]:
    """已中断 Session 的启动门禁：必须显式选择恢复或新建，不能静默复用。

    返回 (code, choice)。code==0 且 choice in {"resume","new"} 表示已通过，
    code==2 表示需要用户决策，code==1 表示硬失败。
    """
    if previous_runtime.get("state") != "aborted":
        return 0, None
    choice = explicit_choice
    if choice not in {"resume", "new"} and autonomous:
        choice = "resume"
    if choice not in {"resume", "new"}:
        choice = _ask_aborted_session_choice()
    if choice is None:
        emit_startup_gate(
            "aborted_session",
            "检测到未完成的已中断会话，选择恢复或新建",
            [
                {
                    "label": "恢复上一次会话",
                    "description": "继续未完成的 Session，保留已结算 Life 和覆盖进度",
                    "value": "resume",
                    "action": "resume_session",
                },
                {
                    "label": "新建本次会话",
                    "description": "保留旧 Session 为 abandoned，创建全新 Session/首个 Round",
                    "value": "new",
                    "action": "create_new_session",
                },
            ],
            detail="aborted 会话不能静默复用；必须显式选择恢复或新建",
        )
        return POLLING_DECISION_REQUIRED, None
    return 0, choice

def _discard_stale_draft(root: Path, *, reason: str) -> None:
    """DOC-236 C4：显式丢弃过期/损坏的作战方案草稿（可观测，绝不静默）。

    丢弃是**幂等**的：文件不存在即无操作；删除失败只记录，不阻断启动
    （草稿只是中间产物，正式真源是 runtime 内的 ``strategy_plan``）。
    """
    draft = root / ".bugx" / "state" / "strategy-plan.json"
    if not draft.is_file() and not draft.is_symlink():
        return
    try:
        draft.unlink()
    except OSError as exc:
        print(f"[launch_bugx] 作战方案草稿清理失败（{type(exc).__name__}）：{draft}")
        return
    print(f"[launch_bugx] 已丢弃作战方案草稿（{reason}）：{draft}")



def check_stage_prompts_integrity(root: Path | None = None) -> tuple[bool, str]:
    """校验阶段与规则提示词的加载完整性（DOC-034 阶段 3 防偷懒门禁）。

    必须确保：
    1. L0 路由入口 (prompts/l0_router.md) 存在且非空
    2. 全部 L1 阶段卡 (gate, explore, mutate, repair) 存在且可加载
    3. 全部 L2 专项规则 (anti_patterns, concurrency, memory_leak, prompt_injection) 存在且可加载
    """
    try:
        from core.planning import prompt_harness
    except ImportError as exc:
        return False, f"提示词装配模块缺失: {exc}"

    target_root = root if root is not None else resolve_project_root()
    prompts_dir = prompt_harness.get_prompts_dir(target_root)
    if not prompts_dir.is_dir():
        return False, f"提示词目录缺失: {prompts_dir}"

    l0_file = prompts_dir / "l0_router.md"
    if not l0_file.is_file():
        return False, f"L0 路由入口文件缺失: {l0_file}"
    try:
        if not l0_file.read_text(encoding="utf-8").strip():
            return False, f"L0 路由入口文件为空: {l0_file}"
    except Exception as exc:
        return False, f"L0 路由入口读取失败: {exc}"

    for stage_name in prompt_harness.list_available_stages():
        try:
            content = prompt_harness.load_stage(stage_name, root=target_root)
            if not content.strip():
                return False, f"阶段卡 {stage_name} 内容为空"
        except Exception as exc:
            return False, f"阶段卡 {stage_name} 加载失败: {exc}"

    for rule_name in sorted(prompt_harness._RULE_MAP.keys()):
        try:
            content = prompt_harness.load_rule(rule_name, root=target_root)
            if not content.strip():
                return False, f"专项规则 {rule_name} 内容为空"
        except Exception as exc:
            return False, f"专项规则 {rule_name} 加载失败: {exc}"

    return True, "ok"


def check_global_budget_gate(
    project_root: Path | None = None,
    *,
    ignore_budget: bool = False,
    db_path: Path | str | None = None,
) -> tuple[bool, str]:
    """检查全局每日 Token 与耗时预算配额门禁 (DOC-049)。

    支持环境变量配置：
    - BUGX_GLOBAL_DAILY_TOKEN_LIMIT: 单日全机跨项目 Token 上限（整数）
    - BUGX_GLOBAL_DAILY_DURATION_LIMIT: 单日全机跨项目运行耗时上限（秒，浮点或整数）

    当累计使用量超过任一限制时阻断启动，防止多项目并发或死循环失控刷穿 API 账单。
    可通过 ignore_budget=True 或将环境变量置 0 / 清空绕过。
    任何读取或计算异常均 fail-open 放行，确保不阻断正常业务。
    """
    if ignore_budget:
        return True, "ok"

    token_limit_raw = os.getenv("BUGX_GLOBAL_DAILY_TOKEN_LIMIT", "").strip()
    duration_limit_raw = os.getenv("BUGX_GLOBAL_DAILY_DURATION_LIMIT", "").strip()

    token_limit: int | None = None
    if token_limit_raw:
        try:
            val = int(token_limit_raw)
            if val > 0:
                token_limit = val
        except ValueError:
            pass

    duration_limit: float | None = None
    if duration_limit_raw:
        try:
            val = float(duration_limit_raw)
            if val > 0:
                duration_limit = val
        except ValueError:
            pass

    if token_limit is None and duration_limit is None:
        return True, "ok"

    try:
        from core.runtime import global_telemetry
    except ImportError:
        return True, "ok"

    try:
        if token_limit is not None:
            consumed_tokens = global_telemetry.get_daily_token_consumption(db_path=db_path)
            if consumed_tokens >= token_limit:
                msg = (
                    f"今日全局已消耗 {consumed_tokens} Tokens，超过每日预算上限 "
                    f"{token_limit} Tokens。可通过 --ignore-global-budget 忽略或调整 "
                    f"BUGX_GLOBAL_DAILY_TOKEN_LIMIT 环境变量。"
                )
                return False, msg

        if duration_limit is not None:
            consumed_duration = global_telemetry.get_daily_duration_consumption(db_path=db_path)
            if consumed_duration >= duration_limit:
                msg = (
                    f"今日全局已运行 {consumed_duration:.1f} 秒，超过每日运行耗时上限 "
                    f"{duration_limit:.1f} 秒。可通过 --ignore-global-budget 忽略或调整 "
                    f"BUGX_GLOBAL_DAILY_DURATION_LIMIT 环境变量。"
                )
                return False, msg
    except Exception as exc:
        strict = os.environ.get("BUGX_STRICT_BUDGET", "").strip().lower() in {"1", "true", "yes"}
        print(f"[launch_bugx] ⚠ 全局预算遥测读取异常: {exc}", file=sys.stderr)
        if strict:
            return False, f"全局预算门禁检查异常（严格模式阻断）: {exc}"
        return True, "ok"

    return True, "ok"


check_global_budget_gate = check_global_budget_gate
