#!/usr/bin/env python3
"""launch_phases/common.py - 共享基础常量、环境探测与底层子进程调用。"""

from __future__ import annotations

import contextvars
import datetime as dt
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from core.host_runtime import host_registry
import core.hosts as host_plugins

# Execution paths & configuration
AGENT_DIR = Path(__file__).resolve().parent.parent
VERIFY = AGENT_DIR / "ledger" / "verify_life.py"
MODULE_COVERAGE = AGENT_DIR / "coverage" / "module_coverage.py"
COVERAGE_EVIDENCE = AGENT_DIR / "coverage-evidence.json"
# 扁平候选（``core/tools_kb.py`` / ``core/prep_validate.py``）已被迁移删除，
# 保留它们只会让运行期拿到一个永不存在的路径并把错误报成另一件事——L7-7
# 「引擎资产引用有效」正是为此设的闸门。这里只登记归档后的唯一真源。
TOOLS_KB = AGENT_DIR / "skills" / "tools_kb.py"
PREP_VALIDATE = AGENT_DIR / "runtime" / "prep_validate.py"
# DES-13：探针脚本路径不再在此手写。启动前探针的执行真源是
# ``probe_registry.preflight_probes()``（含 ``script`` 字段），新增/下线探针
# 只需改注册表，避免路径常量与注册表漂移。
SUPERVISOR_SCRIPT = AGENT_DIR / "supervisor" / "audit_supervisor.py"
SUPERVISOR_STATE = AGENT_DIR / "audit-supervisor.json"
# 覆盖清单路径单独可绑定：``coverage_manifest_path()`` 必须遵循沙箱绑定，
# 不能让调用方重新从 AGENT_DIR 推导（见该函数说明）。
COVERAGE_MANIFEST = AGENT_DIR / "module-coverage.md"
# 寿命状态文件同样可绑定：``session_runtime.baseline_json`` 必须读这个绑定，
# 不能自己从 AGENT_DIR / cwd 重新推导（见第三处推导缺陷说明）。
LIFE_FILE = AGENT_DIR / "bugx-life.json"
# 准备记录同样是可绑定运行态：``supervisor_bridge.run_prep_validate`` 旧实现
# 直接拼 ``AGENT_DIR / "prep-record.md"``，把可执行引擎目录当成状态目录，
# post 于是到 ``core/prep-record.md`` 找记录并报「缺少准备记录」，
# 而 pre 早已把它写在项目根 —— 又一处「同一事实、两份推导」。
PREP_RECORD = AGENT_DIR / "prep-record.md"
LESSON_LEDGER_SCRIPT = AGENT_DIR / "ledger" / "lesson_ledger.py"
LESSON_LEDGER_FILE = AGENT_DIR / "lesson-ledger.json"
GLOBAL_LESSON_LEDGER_FILE = Path.home() / ".bugx" / "knowledge" / "global.json"
LESSON_CONTEXT_MAX_CARDS = 6
LESSON_CONTEXT_TOKEN_BUDGET = 1200
LESSON_CONTEXT_BYTE_BUDGET = 9000
DEEP_CARD_LIMIT = 1
DEEP_CARD_TOKEN_BUDGET = 2400
DEEP_CARD_BYTE_BUDGET = 16000
RUNTIME_FILE = AGENT_DIR / "bugx-runtime.json"
RUNTIME_LOCK_FILE = AGENT_DIR / "bugx-runtime.lock"
RUNTIME_STARTED_THIS_PROCESS = False

# DOC-204：进程内 launcher 复用当前阶段模块直接导入的路径（例如
# ``post_phase.AGENT_DIR`` 和 ``session_runtime.RUNTIME_FILE``）。这些别名
# 不能在多个线程中同时指向不同沙箱，因此由统一的可重入锁保护整个调用
# 作用域。它比让调用方自行加锁更可靠：所有 future 的库入口都会共享同一
# 边界，而外部 subprocess 仍可并行运行。
_ISOLATED_RUNTIME_LOCK = threading.RLock()
#: 当前是否运行在 ``run_in_process`` 的隔离作用域内（进程内车道）。
#: 进程内车道**禁止**调用 ``os.execv``/``os._exit`` 之类替换或终止宿主进程的
#: 原语：那会把调用方（测试进程、被嵌入的宿主会话）整块替换掉。
IN_PROCESS_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "bugx_in_process_scope", default=False,
)
_ACTIVE_PROJECT_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "bugx_active_project_root", default=None,
)
ISOLATED_REMOTE_VERSION_RESULT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "bugx_isolated_remote_version_result", default=None,
)

# ``from common import NAME`` 是当前阶段模块的实际依赖。作用域进入
# 时批量更新这些别名，退出时逐项恢复；新增阶段模块只要复用这些名字即可
# 自动纳入边界。状态路径在 Layout 2 中来自 bugx_paths，非项目布局则落在
# 测试/调用方显式提供的沙箱根目录。
_ISOLATED_PATH_NAMES = (
    "AGENT_DIR", "VERIFY", "MODULE_COVERAGE", "COVERAGE_EVIDENCE",
    "COVERAGE_MANIFEST", "LIFE_FILE", "PREP_RECORD",
    "TOOLS_KB", "PREP_VALIDATE", "SUPERVISOR_SCRIPT", "SUPERVISOR_STATE",
    "LESSON_LEDGER_SCRIPT", "LESSON_LEDGER_FILE", "GLOBAL_LESSON_LEDGER_FILE",
    "RUNTIME_FILE", "RUNTIME_LOCK_FILE",
)


def is_runtime_started_this_process() -> bool:
    return RUNTIME_STARTED_THIS_PROCESS


def set_runtime_started_this_process(value: bool = True) -> None:
    _set_runtime_started_for_loaded_modules(value)


def _set_runtime_started_for_loaded_modules(value: bool) -> None:
    """Reset the process-local startup marker in every loaded compatibility module.

    A few older phases imported ``RUNTIME_STARTED_THIS_PROCESS`` by value.  The
    accessor in this module is the primary source, but resetting the aliases too
    keeps the legacy checks honest when a launcher is invoked repeatedly in one
    interpreter.
    """
    global RUNTIME_STARTED_THIS_PROCESS
    RUNTIME_STARTED_THIS_PROCESS = bool(value)
    for name, module in list(sys.modules.items()):
        if not _is_launch_module(name):
            continue
        if hasattr(module, "RUNTIME_STARTED_THIS_PROCESS"):
            try:
                setattr(module, "RUNTIME_STARTED_THIS_PROCESS", bool(value))
            except Exception:
                pass


def _is_launch_module(name: str) -> bool:
    """模块名是否属于启动器（点分 ``core.launch_phases.*`` 或归档的扁平别名）。

    旧判定只看 ``name.startswith("launch_phases")``：迁移到点分命名后真实键是
    ``core.launch_phases.common`` 等，于是快照/回填/重置**静默扫不到任何模块**——
    ``isolated_runtime_scope`` 里的路径别名注入形同虚设（实测注入后
    ``session_runtime.RUNTIME_FILE`` 仍是旧的引擎路径，沙箱账目写到了引擎目录）。
    """
    return (
        name.startswith("launch_phases")
        or name.startswith("core.launch_phases")
        or name in {"launch_bugx", "core.launch_bugx"}
    )


def _path_values_for_root(root: Path) -> dict[str, Path]:
    """Return executable and state paths for one isolated launcher root.

    Exactly two layouts exist and both keep the package level intact:

    * project layout (Layout 2) — ``root/.bugx`` carries the runtime state
      (``bugx_paths.require(root)``) while executables stay in the trusted
      global engine ``engine_root``;
    * self-contained engine copy — the whole ``core`` package sits at
      ``root/core`` (``root/core/launch_bugx.py``, ``root/core/skills/...``),
      which is how the protocol fixture and the packaged engine are laid out.
      Dotted imports (``core.skills.tools_kb``) work because ``root`` is then
      the package parent.

    Detecting the layout once at scope entry avoids a half-boundary where some
    modules write to the repository while others write to the temporary tree.

    A flat copy (``root/launch_bugx.py`` plus ``root/skills/...``) is NOT a
    supported layout: it makes ``AGENT_DIR`` resolve to ``root``, so
    ``skills/tools_kb.py`` and ``runtime/prep_validate.py`` are missing,
    ``run_tools_kb``/``run_prep_validate`` fail closed with "缺少 ...", and
    every launch protocol test dies for a fixture-layout reason instead of a
    real engine defect (60 tests in ``tests/cli/test_launch_bugx.py``).
    """
    root = Path(root).expanduser().resolve()
    engine_root = Path(__file__).resolve().parent.parent
    active = None
    if bugx_paths is not None:
        try:
            active = bugx_paths.require(root)
        except (OSError, ValueError, RuntimeError):
            active = None
    if active is not None:
        asset_root = engine_root
    elif (root / "core" / "launch_bugx.py").is_file():
        asset_root = root / "core"
    else:
        asset_root = engine_root
    if active is not None:
        # 每一项都必须出现：``isolated_runtime_scope`` 只重置它返回的名字，
        # 漏掉的项会继续指向**引擎目录**。实测漏掉 COVERAGE_MANIFEST /
        # LIFE_FILE / PREP_RECORD 时，项目布局下 status、寿命校验和 post 的
        # 准备门禁各自去 core/ 找文件，报出一串"文件不存在"。
        state = {
            "COVERAGE_EVIDENCE": active.coverage,
            "COVERAGE_MANIFEST": active.state_root / "module-coverage.md",
            "LIFE_FILE": active.life,
            "PREP_RECORD": active.prep_record,
            "SUPERVISOR_STATE": active.supervisor,
            "LESSON_LEDGER_FILE": active.knowledge_seed,
            "GLOBAL_LESSON_LEDGER_FILE": active.global_knowledge_seed,
            "RUNTIME_FILE": active.runtime,
            "RUNTIME_LOCK_FILE": active.runtime_lock,
        }
    else:
        state = {
            "COVERAGE_EVIDENCE": root / "coverage-evidence.json",
            "COVERAGE_MANIFEST": root / "module-coverage.md",
            "LIFE_FILE": root / "bugx-life.json",
            "PREP_RECORD": root / "prep-record.md",
            "SUPERVISOR_STATE": root / "audit-supervisor.json",
            "LESSON_LEDGER_FILE": root / "lesson-ledger.json",
            "GLOBAL_LESSON_LEDGER_FILE": root / "lesson-ledger.json",
            "RUNTIME_FILE": root / "bugx-runtime.json",
            "RUNTIME_LOCK_FILE": root / "bugx-runtime.lock",
        }
    values = {
        "AGENT_DIR": asset_root,
        "VERIFY": asset_root / "ledger" / "verify_life.py",
        "MODULE_COVERAGE": asset_root / "coverage" / "module_coverage.py",
        "TOOLS_KB": asset_root / "skills" / "tools_kb.py",
        "PREP_VALIDATE": asset_root / "runtime" / "prep_validate.py",
        "SUPERVISOR_SCRIPT": asset_root / "supervisor" / "audit_supervisor.py",
        "LESSON_LEDGER_SCRIPT": asset_root / "ledger" / "lesson_ledger.py",
        **state,
    }
    return {name: Path(path) for name, path in values.items()}


def _loaded_path_bindings() -> dict[object, dict[str, object]]:
    """Snapshot path aliases from loaded launch modules for later restoration."""
    snapshot: dict[object, dict[str, object]] = {}
    for name, module in list(sys.modules.items()):
        is_launch_module = _is_launch_module(name)
        is_verify_module = name in {"core.ledger.verify_life"}
        if not is_launch_module and not is_verify_module:
            continue
        names = _ISOLATED_PATH_NAMES if is_launch_module else (
            "LIFE_FILE", "SNAPSHOT_FILE", "AUDIT_LOG", "LIFE_LOCK_FILE", "_REPO_ROOT",
        )
        values = {
            key: getattr(module, key)
            for key in names
            if hasattr(module, key)
        }
        if values:
            snapshot[module] = values
    return snapshot


def _apply_isolated_path_bindings(values: dict[str, Path]) -> None:
    """Apply one path map to all loaded launcher compatibility modules."""
    for name, module in list(sys.modules.items()):
        if not _is_launch_module(name):
            continue
        for key, value in values.items():
            if hasattr(module, key):
                try:
                    setattr(module, key, value)
                except Exception:
                    pass


def _restore_isolated_path_bindings(snapshot: dict[object, dict[str, object]]) -> None:
    for module, values in snapshot.items():
        for key, value in values.items():
            try:
                setattr(module, key, value)
            except Exception:
                pass


def _clear_isolated_caches() -> None:
    """Drop caches whose keys or values can contain a previous sandbox root."""
    _PROJECT_ROOT_CACHE.clear()
    # 缓存分别挂在 common / session_runtime 上。键必须用**当前**点分名：扁平名在
    # 迁移后不再存在，查不到就静默跳过，"换沙箱必清缓存"的隔离保证随之失效。
    for module, cache_names in (
        (sys.modules.get("core.launch_phases.common")
         or sys.modules.get("launch_phases.common"), ("PROBE_JSON_CACHE",)),
        (sys.modules.get("core.launch_phases.session_runtime")
         or sys.modules.get("launch_phases.session_runtime"), ("_BASELINE_JSON_CACHE",)),
    ):
        if module is None:
            continue
        for cache_name in cache_names:
            cache = getattr(module, cache_name, None)
            if hasattr(cache, "clear"):
                try:
                    cache.clear()
                except Exception:
                    pass
    startup_module = (sys.modules.get("core.launch_phases.startup_gates")
                      or sys.modules.get("launch_phases.startup_gates"))
    if startup_module is not None:
        startup_module.clear_startup_environment_cache()


@contextmanager
def isolated_runtime_scope(root: Path, *, env: dict[str, str] | None = None):
    """Run one launcher invocation against an isolated, reusable sandbox.

    The scope is deliberately serialized inside one Python process because the
    legacy launcher still reads ``os.getcwd()``, ``os.environ`` and imported
    module aliases.  It therefore provides a stronger guarantee than merely
    changing ``BUGX_PROJECT_ROOT``: cwd, argv, environment, path aliases,
    caches, diagnostic logger and process-local startup flags are all restored
    even when the command raises or calls ``SystemExit``.
    """
    global _LAST_CHILD_FAILURE
    logger_scope = (
        bugx_logging.isolated_logger_scope()
        if bugx_logging is not None else nullcontext()
    )
    with _ISOLATED_RUNTIME_LOCK, logger_scope:
        # Resolve relative paths only after the previous invocation has restored
        # cwd. Otherwise another thread's temporary cwd can change their target.
        requested = Path(root).expanduser().resolve()
        if not requested.is_dir():
            raise ValueError(f"进程内执行根目录不是目录: {requested}")
        # The verifier computes default paths on import. Load it before binding
        # this sandbox so that its first invocation has a real outer snapshot.
        # Subsequent invocations reuse the module and pay no import overhead.
        try:
            importlib.import_module("core.ledger.verify_life")
        except ImportError:
            pass
        old_cwd = Path.cwd()
        old_argv = list(sys.argv)
        old_sys_path = list(sys.path)
        old_env = dict(os.environ)
        old_logger = DIAGNOSTIC_LOGGER
        old_started = RUNTIME_STARTED_THIS_PROCESS
        old_child_failure = _LAST_CHILD_FAILURE
        old_warned = set(LIFECYCLE_ROOT_WARNED)
        path_snapshot = _loaded_path_bindings()
        runtime_module = (sys.modules.get("core.launch_phases.session_runtime")
                          or sys.modules.get("launch_phases.session_runtime"))
        runtime_pin = getattr(runtime_module, "_RUNTIME_PATH_PIN", None)
        pin_token = runtime_pin.set(None) if runtime_pin is not None else None
        old_runtime_depth = (
            runtime_module._runtime_lock_depth() if runtime_module is not None else None
        )
        if runtime_module is not None:
            # A lock held for the outer runtime is not a lock on this sandbox.
            runtime_module._set_runtime_lock_depth(0)
        token = _ACTIVE_PROJECT_ROOT.set(requested)
        in_process_token = IN_PROCESS_SCOPE.set(True)
        remote_token = ISOLATED_REMOTE_VERSION_RESULT.set({})
        try:
            _clear_isolated_caches()
            _set_runtime_started_for_loaded_modules(False)
            _LAST_CHILD_FAILURE = ""
            LIFECYCLE_ROOT_WARNED.clear()
            merged_env = dict(os.environ if env is None else env)
            # The explicit function argument is authoritative.  A caller cannot
            # accidentally redirect state by carrying another project's env.
            # Keep the variable absent for the standalone test layout: startup
            # gates intentionally treat an explicit ``BUGX_PROJECT_ROOT`` as an
            # installed project and would otherwise run the Layout 2 version
            # contract against a deliberately bare protocol sandbox.
            project_layout = False
            if bugx_paths is not None:
                try:
                    bugx_paths.require(requested)
                    project_layout = True
                except (OSError, ValueError, RuntimeError):
                    project_layout = False
            if project_layout:
                merged_env["BUGX_PROJECT_ROOT"] = str(requested)
            else:
                merged_env.pop("BUGX_PROJECT_ROOT", None)
            os.environ.clear()
            os.environ.update({str(k): str(v) for k, v in merged_env.items()})
            os.chdir(requested)
            if bugx_paths is not None:
                # Scope also contains environment-derived global knowledge
                # paths; its file stamp alone does not track those overrides.
                bugx_paths.clear_require_cache(requested)
            _apply_isolated_path_bindings(_path_values_for_root(requested))
            yield requested
        finally:
            # Close handlers before a temporary root is removed.  Restricting
            # cleanup by root preserves a host process's unrelated BugX logs.
            try:
                if bugx_logging is not None:
                    bugx_logging.close_handlers_for_root(requested)
            except (OSError, ValueError, RuntimeError):
                pass
            set_diagnostic_logger(old_logger)
            _set_runtime_started_for_loaded_modules(old_started)
            _LAST_CHILD_FAILURE = old_child_failure
            LIFECYCLE_ROOT_WARNED.clear()
            LIFECYCLE_ROOT_WARNED.update(old_warned)
            _clear_isolated_caches()
            _restore_isolated_path_bindings(path_snapshot)
            if bugx_paths is not None:
                bugx_paths.clear_require_cache(requested)
            try:
                os.chdir(old_cwd)
            except OSError:
                # The caller may have removed its cwd while the scope ran.
                os.chdir(Path.home())
            sys.argv = old_argv
            sys.path[:] = old_sys_path
            os.environ.clear()
            os.environ.update(old_env)
            _ACTIVE_PROJECT_ROOT.reset(token)
            IN_PROCESS_SCOPE.reset(in_process_token)
            ISOLATED_REMOTE_VERSION_RESULT.reset(remote_token)
            if pin_token is not None:
                runtime_pin.reset(pin_token)
            if runtime_module is not None:
                runtime_module._set_runtime_lock_depth(old_runtime_depth)
DIAGNOSTIC_LOGGER = None


def set_diagnostic_logger(logger) -> None:
    """注册诊断日志器（0.6.109）。

    拆分后 round_orchestrator 的 global 声明只写自己的命名空间，
    common.run_child 读不到——跨模块可变状态必须走显式 setter。
    """
    global DIAGNOSTIC_LOGGER
    DIAGNOSTIC_LOGGER = logger
_LAST_CHILD_FAILURE = ""


def get_last_child_failure() -> str:
    """读取最近一次子进程失败详情（0.6.109：值导入副本不可见的修复）。"""
    return _LAST_CHILD_FAILURE


def set_last_child_failure(detail: str) -> None:
    """写入最近一次进程内失败详情（E6a，2026-11-09 opencode 复盘）。

    ``pre``/``audit`` 是进程内调用，不经过 ``run_child``，``_LAST_CHILD_FAILURE``
    恒空 → orchestrator 的 ``failure_detail`` 永远是兜底文案（实测 5 次
    pre_failed 全部「未产生子命令输出」，exit 2/3 语义只能读源码反查）。
    进程内路径在非零返回前调用本函数写入原因。
    """
    global _LAST_CHILD_FAILURE
    _LAST_CHILD_FAILURE = str(detail or "").strip()


PROBE_JSON_CACHE: dict[Path, dict | None] = {}
RUNTIME_SCHEMA_VERSION = 2
RUNTIME_STATES = {"idle", "running", "paused", "completed", "aborted"}
HEARTBEAT_TIMEOUT_SECONDS = 600
POLLING_POLICIES = {"auto", "manual"}
REVIEW_MODES = {"manual", "mcp-auto"}
COMMIT_POLICIES = {"life", "round", "manual", "not_applicable"}
REVIEW_DECISION_LATER = "later"
POLLING_DECISION_REQUIRED = 2
# DOC-236 C7：宿主侧入口（MCP/skill）配置已完成但**必须重启宿主**才能加载。
# 与"需要人决策"分离，使无人值守也能得到正确信号。
RESTART_REQUIRED = 3
STRATEGY_PLAN_ACTIONS = {"approve_and_start", "approve", "customize", "save_and_later", "later"}
#: MCP/启动门禁的**作答**值域（用户对「如何解决缺 MCP」的选择）。
STARTUP_INTERACTION_CHOICES = {"restart_host", "run_tty", "later"}
#: ``--startup-interaction`` 的**模式**值域：声明调用方如何应对启动门禁。
#: 与作答值域是两回事——帮助文本一直只声明 ``auto|prompt``，而校验曾只认作答
#: 值域，导致 ``--startup-interaction auto``（非交互启动的标准用法）被判非法。
STARTUP_INTERACTION_MODES = {"auto", "prompt"}
# DOC-236 C6：报告提交策略（L1 采集并持久化，L5 `post` 无交互执行）。
#   none   = 本轮不参与结论共享与知识飞轮（不写 share_pending、不重放）；
#   local  = 只保留本地报告，不提交（默认；保留事后共享通道）；
#   submit = 生成报告后**无交互**提交（私有多设备同步）。
REPORT_POLICY_CHOICES = ("none", "local", "submit")
REPORT_POLICY_DEFAULT = "local"
LIFECYCLE_REPORT_NAME = "report.md"
LIFECYCLE_MAX_SMALL_CYCLES = 50
LIFECYCLE_MAX_NO_PROGRESS_CYCLES = 3
LIFECYCLE_MAX_SECONDS = 4 * 60 * 60

AUDIT_TODO_ITEMS = (
    "环境检测与版本门禁",
    "启动配置与最终动作确认",
    "工具准备与 MCP 健康证明",
    "覆盖清单与风险排序",
    "并发 Worker 勘察与心跳",
    "候选 Finding 独立复现、负向对照、根因确认",
    "修复/仅记录与定向测试",
    "Live 复验与 Finding 固化",
    "Life 结算与项目级知识复盘",
    "Round 汇总、全局级知识复盘与正式报告",
    "封存当前 Round（report/conclusion/integrity）",
    "按已配置 polling policy 创建下一 Round，或进入 Session 收尾",
    "若终止，记录 final_round_id 并释放 Session 资源",
    "按 Round 结论共享选择处理（已共享/不共享/share_pending）",
)

STARTUP_TODO_ITEMS = (
    ("环境检测与版本门禁", "项目根目录、Git、Python、宿主界面、版本和云端状态"),
    ("启动配置、策略对齐与最终动作", "云端、策略规划作战方案对齐、审查/轮询/修复/提交及新建/恢复决策"),
    ("工具检测、MCP 健康与知识准备", "按项目类型选择工具，校验 MCP，并读取经验集和错题集"),
    ("漏洞挖掘（Round / Life / Worker）", "按覆盖单元分配并发任务，执行勘察、复现、修复和 Live 复验"),
    ("Life/Round 结算、知识复盘与分层同步", "固化 Finding、复盘经验和错题，并按边界同步项目层/全局层"),
    ("Round 报告与结论共享", "封存正式 Round 报告，展示结论，再按既定策略处理共享"),
    ("Session 结束与资源释放", "完成自动收尾、释放锁和租约；下一次启动再创建新的 Session"),
)

STARTUP_TODO_PHASE_INDEX = {
    "environment": 0,
    "startup": 1,
    "tools": 2,
    "mining": 3,
    "sync": 4,
    "report": 5,
    "end": 6,
}

# DES-11：启动门禁 question_id 全集（单一真源）。
# 与 ``core/bugx-runtime-contract.md`` 的「启动 gate 的 question_id 全集」
# 逐字对应（14 个，含 ``aborted_session`` 与 ``report_policy``）。Round 阶段
# **不再有**独立交互 id：DOC-236 C6 把报告处置收敛为 L1 一次采集的
# ``report_policy``，L5 ``post`` 无交互执行；L1 的采集入口就是
# ``report_policy`` gate（2026-11-09 补齐：此前仅 CLI/config 可设，交互态
# 用户无入口，默认值被钉死为 local）。适配层不得对清单之外的 id 做特殊
# 分支——批量工具门禁使用独立的 ``tooling:batch`` 命名空间，由
# ``_startup_todo_phase_for_gate`` 的前缀规则处理，因此**不**属于本集合。
STARTUP_GATE_QUESTION_IDS: frozenset[str] = frozenset({
    "startup_interaction",
    "git_environment",
    "version_contract",
    "cloud_sync",
    "supervisor_mcp",
    "review_mode",
    "polling_policy",
    "fix_mode",
    "commit_policy",
    "report_policy",
    "startup_action",
    "aborted_session",
    "coverage_bootstrap",
    "strategy_plan",
})

# 启动门禁 id → 七阶段总览映射。键必须**恰好**等于 question_id 全集：
# 历史缺陷（DES-11）曾混入 ``session_recovery``（实为 ``--session-recovery``
# 配置键）与 ``degraded_tools``（实为 ``tooling:batch`` 批量门禁），同时漏掉
# ``aborted_session``，导致两个映射键永不命中。下面的导入期守卫让漂移直接失败。
STARTUP_TODO_GATE_PHASE: dict[str, str] = {
    "startup_interaction": "environment",
    "git_environment": "environment",
    "version_contract": "environment",
    "cloud_sync": "startup",
    "strategy_plan": "startup",
    "aborted_session": "startup",
    "review_mode": "startup",
    "polling_policy": "startup",
    "fix_mode": "startup",
    "commit_policy": "startup",
    "report_policy": "startup",
    "supervisor_mcp": "tools",
    "startup_action": "startup",
    "coverage_bootstrap": "tools",
}

_missing_gate_phases = STARTUP_GATE_QUESTION_IDS - set(STARTUP_TODO_GATE_PHASE)
_extra_gate_phases = set(STARTUP_TODO_GATE_PHASE) - STARTUP_GATE_QUESTION_IDS
if _missing_gate_phases or _extra_gate_phases:
    raise RuntimeError(
        "启动门禁 id→阶段映射与契约 question_id 全集漂移（DES-11）："
        f"缺少={sorted(_missing_gate_phases)} 多余={sorted(_extra_gate_phases)}"
    )
del _missing_gate_phases, _extra_gate_phases

# DES-18：仅保留 ``host_registry.SUPPORTED_HOSTS`` 中真实声明且可被识别的宿主。
# 历史清单混入 ``roo`` / ``windsurf`` / ``cursor`` / ``kline``——四者既不在
# 宿主声明内，也没有任何适配实现，属于「幽灵宿主名」，会被误当作已支持宿主
# 展示。一致性由 ``tests/test_launch_bugx.py`` 的守护测试保证。
HOST_DISPLAY_NAMES = {
    "opencode": "OpenCode",
    "claude": "Claude Code",
    "pi": "Pi",
    "zcode": "ZCode",
    "dsh": "dsh",
    "chatgpt": "ChatGPT",
    "workbuddy": "WorkBuddy",
    "funide": "FunIDE",
}


def _apply_plugin_host_facts() -> None:
    """DOC-239：已迁移宿主的展示名/串行性由 ``core/hosts`` 插件拥有。

    本地表只为**尚未迁移**的宿主保留；插件声明优先，因此把 Cline 从本地表删掉后
    行为不变，也不会再出现「同一宿主两处名字」的漂移。
    """

    for plugin in host_plugins.all():
        HOST_DISPLAY_NAMES[plugin.name] = plugin.identity.label


def host_plugin(name: object):
    """Return the migrated host plugin for ``name`` (``None`` = 未迁移/未知)。"""

    return host_registry.host_plugin(name)


def host_display_name(host: object) -> str:
    """宿主展示名：插件 ``identity.label`` 优先，未迁移宿主回落到本地表。"""

    name = str(host or "").strip().casefold()
    if host_registry.host_plugin(name) is not None:
        return host_registry.host_label(name)
    return HOST_DISPLAY_NAMES.get(name, name or "unknown")


def host_serial_worker(host: object) -> bool:
    """``host`` 是否声明为串行 Worker 宿主（插件 ``worker_runtime.mode``）。

    DOC-239：调用方**禁止**再写 ``host == "<宿主>"``——串行性、进程池逃逸口和并发硬顶
    都是宿主插件的 ``worker_runtime`` 面。实现只有 ``host_registry`` 一处。
    """

    return host_registry.host_serial_worker(host)


def host_workspace_dirs() -> frozenset[str]:
    """已迁移插件在工作区生成的顶层目录（宿主事实的唯一来源）。

    ``post_phase`` 的 Git 提交面与 ``symbol_index`` 的排除集都从这里取；未迁移宿主的
    目录仍留在各自调用点的「待迁移基线」里，逐宿主切片时删除。
    """

    return host_registry.host_workspace_dirs()


_apply_plugin_host_facts()

MANIFEST_TABLE_HEADER = (
    "| # | 阶段 | 父模块 | 模块 | 路径/范围 | 参与单元 | 最小性 | 难度 | 命中 | 主工具 | 负责任务 | 依赖 | 状态 | 发现数 | 证据/测试 | 备注 |\n"
    "|---|------|--------|------|-----------|----------|--------|------|------|--------|----------|------|------|--------|-----------|------|\n"
)

SUPERVISOR_MCP_HARD_CHANNELS = {
    "terminal", "subagent", "worktree", "comment", "file_reservation",
}

LIFECYCLE_ROOT_WARNED: set[Path] = set()
_INVALID = object()

# PERF-06：``resolve_project_root()`` 结果缓存。键包含所有会影响解析结果的外部输入
# （BUGX_PROJECT_ROOT / argv[0] / cwd / HOME），因此环境变化不会被钉住。
_PROJECT_ROOT_CACHE: dict[tuple[str, str, str, str], Path] = {}

try:
    import core.bugx_paths as bugx_paths
except ImportError:
    bugx_paths = None

try:
    from core.runtime import bugx_logging
except ImportError:
    bugx_logging = None

try:
    from core.governance import dependency_lock
except ImportError:
    dependency_lock = None

try:
    from bounded_subprocess import run_bounded
except ImportError:
    def run_bounded(cmd, *, cwd=None, env=None, timeout=None, capture_output=True, text=True):
        return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, capture_output=capture_output, text=text)



def resolve_project_root() -> Path:
    """解析正式 .bugx 状态布局；隔离测试目录仍可独立运行。

    DOC-004 Engine-State Separation：静态协议脚本位于全局引擎
    全局引擎位于用户目录，项目运行态位于 cwd（调用方已 cd 到项目根），与
    BUGX_PROJECT_ROOT 显式覆盖共同保证正确性。

    PERF-06：本函数在单次会话里被调用上百次，且每次都做
    ``resolve()``/``is_dir()``/``is_symlink()`` 链。结果按
    ``(BUGX_PROJECT_ROOT, sys.argv[0], cwd, HOME)`` 作为缓存键记忆，
    因此测试中 ``mock.patch.dict(os.environ, {"BUGX_PROJECT_ROOT": ...})``
    或切换 cwd 都会自然落到不同的键，不会读到被钉住的旧值。需要显式
    失效时调用 :func:`clear_project_root_cache`。
    """
    active = _ACTIVE_PROJECT_ROOT.get()
    if active is not None:
        return active
    key = (
        os.environ.get("BUGX_PROJECT_ROOT") or "",
        sys.argv[0] if sys.argv else "",
        str(Path.cwd()),
        os.environ.get("HOME") or "",
    )
    cached = _PROJECT_ROOT_CACHE.get(key)
    if cached is not None:
        return cached
    result = compute_project_root()
    _PROJECT_ROOT_CACHE[key] = result
    return result


def clear_project_root_cache() -> None:
    """清空 ``resolve_project_root()`` 的结果缓存（测试或显式失效时使用）。"""
    _PROJECT_ROOT_CACHE.clear()


def compute_project_root() -> Path:
    active = _ACTIVE_PROJECT_ROOT.get()
    if active is not None:
        return active
    if bugx_paths is None:
        return Path.cwd().resolve()
    if os.environ.get("BUGX_PROJECT_ROOT"):
        return bugx_paths.project_root(AGENT_DIR)
    # Layout 2 launches only from the global engine.  The project root is
    # therefore supplied explicitly by the CLI/bootstrap environment; when a
    # developer runs the bundled launcher directly, cwd remains the fallback.
    return bugx_paths.project_root(AGENT_DIR)

def uses_project_layout() -> bool:
    if bugx_paths is None:
        return False
    # DOC-004 Layout 2：项目 .bugx 不再包含可执行 core；判定标准是解析出的
    # 项目运行态是否真实存在，而非脚本位置。
    try:
        bugx_paths.require(resolve_project_root())
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def require_project_layout() -> bool:
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


_require_project_layout = require_project_layout


def supervisor_state_path() -> Path:
    """Return the Supervisor state for the active project layout."""
    if uses_project_layout():
        return bugx_paths.require(resolve_project_root()).supervisor
    return SUPERVISOR_STATE

def pollings_root(root: Path | None = None, *, layout: bool | None = None) -> Path:
    """Session/Round 账本根目录（唯一权威，布局无关）。

    项目布局（Layout 2）把它放在 ``<root>/.bugx/pollings``；自包含引擎副本
    （``<root>/core/...``，引擎自带包）把运行态放在项目根，因此账本在
    ``<root>/pollings``。此前 6 处各自硬编码 ``<root>/.bugx/pollings``，
    在副本布局下全部指向不存在的目录：生命周期落到 ``polling-unknown``、
    findings.json 写入端与读取端不一致、复盘门禁误判 finding_count。
    """
    base = Path(root) if root is not None else resolve_project_root()
    # 形状规则只有这一份实现。判定用**物理存在**而不是 ``uses_project_layout``：
    # 后者只认 ``.bugx/manifest.json``（initialize 才写），而账本目录可能先于
    # 清单存在；只看清单会把 <root>/.bugx/pollings 误判成 <root>/pollings。
    project_layout = layout
    if project_layout is None:
        project_layout = uses_project_layout() or (base / ".bugx").is_dir()
    if project_layout:
        return base / ".bugx" / "pollings"
    return base / "pollings"


def coverage_manifest_path() -> Path:
    """覆盖清单的唯一位置。

    必须读**模块级** ``COVERAGE_*`` 绑定点而不是重新从 ``AGENT_DIR`` 推导：
    ``isolated_runtime_scope`` / ``launch_bugx`` 只把路径写进模块属性，
    重新推导会绕过沙箱绑定，把清单指回引擎目录。实测后果不是"报错"而是
    **静默换源**——引擎目录里没有清单，``status`` 于是重新做一次全树发现，
    把 ``core/`` 的 2441 个符号当成被审单元（``progress.total`` 从 3 变 2444），
    覆盖进度、ETA、当前阶段全部失真。
    """
    if uses_project_layout():
        return bugx_paths.require(resolve_project_root()).state_root / "module-coverage.md"
    bounded = Path(COVERAGE_MANIFEST)
    if bounded.is_absolute() and bounded.parent != Path(AGENT_DIR):
        return bounded
    return Path(AGENT_DIR) / "module-coverage.md"

def _coverage_evidence_path() -> Path:
    if uses_project_layout():
        return bugx_paths.require(resolve_project_root()).state_root / "coverage-evidence.json"
    return COVERAGE_EVIDENCE

def child_environment() -> dict[str, str]:
    """Bind every child verifier/probe to this launcher's project root.

    The launcher itself can identify a project from the lexical
    the global launcher path, while a child such as ``verify_life`` resolves
    its own real path to the shared engine. Passing the explicit root
    prevents that child from falling back to the global engine's runtime files,
    especially when ``BUGX_GLOBAL_ENGINE_ROOT`` or an isolated HOME is used.
    """
    try:
        from core.probes.probe_registry import normalized_tool_environment
        environment = normalized_tool_environment()
    except ImportError:
        environment = os.environ.copy()
    try:
        environment["BUGX_PROJECT_ROOT"] = str(resolve_project_root())
    except (OSError, ValueError, RuntimeError):
        pass
    return environment

def diagnostic_logger(command: str):
    """Return the project logger when launch runs from the installed layout."""
    if not uses_project_layout() or bugx_logging is None:
        return None
    try:
        return bugx_logging.configure(resolve_project_root(), command=command)
    except (OSError, ValueError):
        return None

def run_child(command: list[str], *, cwd: Path | None = None,
               timeout: float | None = None) -> int:
    """Run a protocol child while preserving output and recording failures."""
    global _LAST_CHILD_FAILURE
    try:
        # Child commands are Workers too: use the same process-group and
        # file-backed stream boundary as host execution.  A default deadline
        # prevents one broken probe from hanging the whole startup forever.
        try:
            from core.supervisor.worker_execution import run_bounded
        except ImportError as exc:  # pragma: no cover - broken bundle
            raise OSError("缺少 worker_execution.py，无法安全启动子命令") from exc
        result = run_bounded(
            command,
            cwd=(cwd or resolve_project_root()),
            env=child_environment(),
            timeout=timeout or 120.0,
            capture=True,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
        if result.returncode != 0:
            _LAST_CHILD_FAILURE = (stderr or stdout).strip()[-4000:]
        if DIAGNOSTIC_LOGGER is not None and bugx_logging is not None:
            bugx_logging.log_event(
                DIAGNOSTIC_LOGGER,
                "child_command_finished",
                "BugX 子命令执行完成",
                level=40 if result.returncode else 10,
                child_command=command,
                exit_code=result.returncode,
                stdout=stdout[-4000:], stderr=stderr[-4000:],
            )
        return result.returncode
    except subprocess.TimeoutExpired as exc:
        _LAST_CHILD_FAILURE = f"子命令超时: {command!r} ({exc.timeout}s)"
        if DIAGNOSTIC_LOGGER is not None and bugx_logging is not None:
            bugx_logging.log_event(
                DIAGNOSTIC_LOGGER, "child_command_timeout", _LAST_CHILD_FAILURE,
                level=40, child_command=command, timeout_seconds=exc.timeout,
            )
        return 124
    except OSError as exc:
        _LAST_CHILD_FAILURE = f"子命令无法启动: {exc}"
        if DIAGNOSTIC_LOGGER is not None and bugx_logging is not None:
            bugx_logging.log_event(
                DIAGNOSTIC_LOGGER, "child_command_failed", str(exc), level=40,
                child_command=command, exception_type=type(exc).__name__,
                exc_info=True,
            )
        return 1

def _lifecycle_auto_mode() -> bool:
    """作战方案选择 auto 轮询时，覆盖门禁允许自动回填记账元数据。

    DES-06：与 ``run_module_coverage`` 一同从 ``pre_phase`` 下沉到本模块。
    两者的唯一调用方是 ``post_phase``（收尾阶段的最终覆盖门禁），而所依赖的
    全部符号（``MODULE_COVERAGE`` / ``COVERAGE_EVIDENCE`` / ``resolve_project_root`` /
    ``coverage_manifest_path`` / ``run_child``）本就定义在这里；留在
    ``pre_phase`` 会让 ``post_phase`` 反向依赖前置阶段，与
    ``docs/02_architecture/ARCHITECTURE.md`` 声明的单向流水线矛盾。
    """
    try:
        manifest = coverage_manifest_path()
        payload = json.loads(
            (manifest.parent / "runtime.json").read_text(encoding="utf-8"),
        )
        return payload.get("polling_policy") == "auto"
    except (OSError, ValueError):
        return False


def run_module_coverage(final: bool = False) -> int:
    """外部校验模块清单；覆盖门禁不能只由 agent 自报。

    DES-06：唯一的调用方是 ``post_phase``，故下沉到共享层。原实现位于
    ``pre_phase``，导致 ``post_phase`` 与 ``round_orchestrator`` 都要反向
    导入 ``pre_phase``；同时 ``round_orchestrator`` 还 import 了它却从不调用
    （死导入，已一并清除）。
    """
    if not MODULE_COVERAGE.is_file():
        print("[launch_bugx] ✗ 缺少 module_coverage.py，拒绝通过覆盖门禁")
        return 1
    command = "final-check" if final else "check"
    arguments = [
        sys.executable, str(MODULE_COVERAGE), command,
        "--root", str(resolve_project_root()),
        "--manifest", str(coverage_manifest_path()),
        "--evidence", str(_coverage_evidence_path()),
        "--require-evidence",
    ]
    # A validated checkpoint receipt is sufficient to fill missing plan
    # metadata during post, even when round continuation is manual.  The
    # backfill never changes coverage status; that remains receipt-driven.
    if _lifecycle_auto_mode() or _coverage_evidence_path().is_file():
        arguments.append("--auto-backfill")
    # DOC-114 M3: risk-admitted business chains may be scheduled before
    # internal coverage reaches 100%; coverage completion remains separate.
    arguments.append("--allow-business-partial")
    return run_child(arguments, cwd=resolve_project_root())


def coverage_ratio_failure() -> bool:
    """DOC-203 D1-b：区分覆盖门禁失败的两种形态。

    比例不足=清单可解析且存在「未覆盖」行（--allow-incomplete 的诚实
    适用场景）；结构无效（清单缺失/不可读）返回 False——该旗标治不了
    结构问题，提示只会误导宿主。
    """
    try:
        manifest = _coverage_manifest_path()
        text = manifest.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return "未覆盖" in text


def run_verify_life(*args: str) -> int:
    """运行 verify_life 寿命校验命令；优先进程内直接调用，失败或异常时回退到子进程。"""
    if not uses_project_layout() and os.environ.get("BUGX_AIR_GAP") != "1":
        print(
            "[launch_bugx] ✗ 缺少有效的 .bugx 项目运行布局；"
            "拒绝向全局引擎写入寿命状态。请先在项目根执行 `bugx init`。"
        )
        return 1
    if VERIFY.is_file():
        try:
            parent_dir = str(VERIFY.parent.resolve())
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
            from core.ledger import verify_life
            root = resolve_project_root()
            if uses_project_layout() and "BUGX_PROJECT_ROOT" not in os.environ:
                os.environ["BUGX_PROJECT_ROOT"] = str(root)
            bound_layout = False
            if hasattr(verify_life, "bugx_paths") and verify_life.bugx_paths is not None:
                try:
                    active = verify_life.bugx_paths.require(root)
                    verify_life.LIFE_FILE = active.life
                    verify_life.SNAPSHOT_FILE = active.life_snapshot
                    verify_life.AUDIT_LOG = active.state_root / "repair-audit.log"
                    verify_life.LIFE_LOCK_FILE = active.life_lock
                    bound_layout = True
                except (OSError, ValueError, RuntimeError):
                    bound_layout = False
            if not bound_layout:
                base = VERIFY.parent / "bugx-life.json"
                # In-process execution imports the trusted module once and then
                # swaps its active files per sandbox.  ``VERIFY.parent`` is the
                # copied protocol fixture root after the scope binds aliases;
                # the explicit project root is safer when the asset lives in
                # the global engine.
                if not uses_project_layout():
                    base = root / "bugx-life.json"
                verify_life.LIFE_FILE = base
                verify_life.SNAPSHOT_FILE = base.with_suffix(".json.snapshot")
                verify_life.AUDIT_LOG = base.parent / "repair-audit.log"
                verify_life.LIFE_LOCK_FILE = base.with_name(base.name + ".lock")
            if hasattr(verify_life, "_REPO_ROOT"):
                verify_life._REPO_ROOT = root

            func = getattr(verify_life, "_run_direct", None)
            if callable(func):
                return int(func(*args))
            if hasattr(verify_life, "main"):
                try:
                    return int(verify_life.main(["verify_life.py", *args]))
                except SystemExit as exc:
                    return int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        except Exception:
            pass
    return run_child([sys.executable, str(VERIFY), *args])

_run = run_verify_life

def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)

def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def worker_subagent_type(value: str | None = None) -> str:
    """Return a validated worker subagent type from explicit session data.

    Worker routing is session context, not process-global configuration.  The
    caller may pass a value loaded from ``bugx-runtime.json``; omitted values
    use the protocol default without consulting ``os.environ``.
    """
    import re as _re

    default = "bugx-worker"
    raw = value
    if raw is None:
        return default
    value = str(raw).strip()
    if not value or not _re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        print(
            f"[bugx] ⚠ subagent_type={raw!r} 非法"
            "（需匹配 ^[A-Za-z0-9._-]{1,64}$），回退默认 bugx-worker",
            file=sys.stderr,
        )
        return default
    return value

_coverage_manifest_path = coverage_manifest_path
_RUNTIME_STARTED_THIS_PROCESS = RUNTIME_STARTED_THIS_PROCESS

_coverage_ratio_failure = coverage_ratio_failure

def _coverage_ratio_failure(*args, **kwargs):
    return coverage_ratio_failure(*args, **kwargs)

#: 修复模式（``bugx ledger set-mode --mode``）的**唯一**事实源。
#: 此前 ``auto``/``log-only`` 字面量在 CLI help、``pre_state_builder`` 校验与
#: ``verify_life.VALID_MODES`` 三处各写一份，help 写的是 ``auto|manual`` 而校验
#: 只收 ``auto|log-only`` —— 用户照着 help 敲 ``--fix-mode manual`` 必然被拒。
FIX_MODES: tuple[str, ...] = ("auto", "log-only")
