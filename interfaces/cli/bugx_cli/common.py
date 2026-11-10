#!/usr/bin/env python3
"""BugX 统一命令行入口。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from email.parser import Parser
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
try:
    import tomllib
except ImportError:
    tomllib = None
import zipfile
from pathlib import Path
from typing import Any

import bugx_cli
from . import __version__
from bugx_cli.infra import (
    CONFIG_NAME,
    CORE_API_RE,
    CliError,
    ENGINE_INTEGRITY_NAME,
    ENGINE_INTEGRITY_SCHEMA,
    LOCK_NAME,
    MCP_RUNTIME_COMMAND,
    OPENCODE_MCP_PACKAGES,
    OPENCODE_PINNED_MCP_COMMANDS,
    PLUGIN_VERSION_RE,
    PROJECT_OWNED_CORE_NAMES,
    REQUIRED_I18N_ASSETS,
    REQUIRED_MCP_RECOMMENDATIONS,
    RUNTIME_NAMES,
    VERSION_RE,
    _assert_no_symlink_components,
    _asset_candidates,
    _atomic_write_global_file,
    _backup_host_resource,
    _config_candidates,
    _controlled_core_resource,
    _copy_pi_extension,
    _copy_plugin,
    _detach_project_owned_core_links,
    _detach_project_runtime_core_entries,
    _engine_file_manifest,
    _engine_install_lock,
    _engine_integrity_status,
    _ensure_global_cli_symlinks,
    _find_current_cli_executable,
    _find_repo_root,
    _engine_manifest_payload,
    _file_record,
    _fsync_engine_tree,
    _guard_plugin_write_target,
    _host_bundle_checks,
    _installed_plugin_agent,
    _install_engine,
    _integration_dir,
    _is_reparse_point,
    _is_runtime_artifact_name,
    _legacy_project_core_present,
    _json_version_marker,
    _layout_module,
    _legacy_core_runtime_entries,
    _link_legacy_project_core,
    _lock_candidates,
    _lock_disabled_servers,
    _mcp_entry_state,
    _mcp_recommendations,
    _mcp_runtime_check,
    _mcp_runtime_doctor_payload,
    _project_mcp_processes,
    _abandoned_mcp_processes,
    _mcp_session_hygiene_check,
    installed_runtime_servers,
    provision_targeted_mcp_runtime,
    targeted_mcp_names,
    _plugin_manifest,
    _plugin_store,
    _print_mcp_recommendations,
    _records_for_paths,
    _recover_engine_swap,
    _relative_project_file,
    _require_global_only_layout,
    _remove_empty_parents,
    _remove_engine_path,
    _safe_record_target,
    _engine_basename_index,
    _script,
    _sha256_file,
    _shared_asset_records,
    _stage_engine,
    verify_builtin_mcp_manifest,
    _version_contract_status,
    _version_marker,
    _write_engine_integrity,
    _write_host_resource,
    _write_private_json,
    asset_root,
    bundled_config,
    bundled_lock,
    default_mcp_runtime_root,
    global_engine_root,
    pi_extension_root,
    pi_skill_root,
    project_core,
)



MIN_PYTHON = (3, 11)


def _require_supported_python() -> None:
    """Fail before importing feature modules on an unsupported interpreter.

    BugX deliberately targets Python 3.11+ (see ``pyproject.toml``).  Several
    core modules use 3.11-era dataclass/type features, so waiting until a
    command imports them turns an environment problem into an opaque traceback.
    Keep the guard at the CLI import boundary and return a stable, actionable
    message instead.
    """
    if sys.version_info < MIN_PYTHON:
        actual = ".".join(str(part) for part in sys.version_info[:3])
        required = ".".join(str(part) for part in MIN_PYTHON)
        print(
            f"[BugX] 当前 Python {actual} 不受支持；需要 Python >= {required}。"
            "请切换到受支持的 Python 后重试。",
            file=sys.stderr,
        )
        raise SystemExit(2)


_require_supported_python()

from .host_adapters import (
    add_locale_marker,
    BUGX_WORKER_DESCRIPTION,
    HOST_ADAPTERS,
    WORKBUDDY_PROJECT_PATHS,
    _workbuddy_config_roots,
    discover_hosts,
    get_adapter,
    render_zcode_worker_entry,
    strip_locale_marker,
    read_locale_marker,
    workbuddy_user_skill_root,
    ZCODE_WORKER_INSTALL_PATH,
)
from .hosts.registry import sweepable_skill_adapter_names

#: 按 Skill 语义清扫的适配器基线（仅**未迁移**宿主；已迁移宿主由插件
#: ``install.sweepable`` 声明）。逐宿主切片时删除对应项。
_LEGACY_SWEEPABLE_SKILL_ADAPTERS = ("claude", "zcode", "workbuddy")
_SWEEPABLE_SKILL_ADAPTERS = sweepable_skill_adapter_names(_LEGACY_SWEEPABLE_SKILL_ADAPTERS)

RUNTIME_NAMES = {
    "bugx-life.json", "bugx-life.json.snapshot", "bugx-life.json.lock",
    "bugx-runtime.json", "bugx-runtime.lock", "findings.json",
    "audit-supervisor.json", "audit-supervisor.lock", "coverage-evidence.json",
    "coverage-evidence.lock", "finding-ledger.json", "finding-ledger.lock",
    "audit-supervisor.json.lock", "coverage-evidence.json.lock",
    "finding-ledger.json.lock",
    "lesson-ledger.sqlite3", "lesson-ledger.sqlite3-shm", "lesson-ledger.sqlite3-wal",
    "lesson-ledger.db", "lesson-ledger.db-shm", "lesson-ledger.db-wal",
    "lesson-ledger.json.lock", ".lesson-ledger.json.lock",
    "audit-campaign.json", "finding-lifecycle.json", "repair-audit.log",
    "bugx-runtime.json.bak", "audit-supervisor.json.bak",
}




OPENCODE_REQUIRED_MCP = ("playwright", "postmcp", "dbhub", "redis", "semgrep", "mobile")
OPENCODE_LOCAL_MCP = ("bugx-supervisor", "bugx-supervisor-worker", "bugx-docs")
OPENCODE_ALL_REQUIRED_MCP = OPENCODE_REQUIRED_MCP + OPENCODE_LOCAL_MCP
OPENCODE_BUGX_MANAGED_MCP = OPENCODE_ALL_REQUIRED_MCP
# Native host MCP channels for local automatic review and knowledge lookup.
HOST_SUPERVISOR_MCP = ("bugx-supervisor", "bugx-supervisor-worker")
HOST_ALL_MCP = ("bugx-supervisor", "bugx-supervisor-worker", "bugx-docs")
HOST_SUPERVISOR_MCP_ROLES = {"bugx-supervisor": "host", "bugx-supervisor-worker": "worker"}
PROBE_SCRIPTS = {
    "efficiency": "efficiency_probe.py",
    "sast": "sast_probe.py",
    "mobile": "mobile_probe.py",
    "ios_binary": "ios_binary_probe.py",
    "codebase-memory": "codebase_memory_probe.py",
    "platform": "platform_probe.py",
    "memory": "memory_probe.py",
    "pattern": "pattern_probe.py",
    "differential": "differential_probe.py",
    "openwiki": "openwiki_probe.py",
}
PROBES_WITHOUT_ROOT_ARGUMENT = {"memory", "differential"}
CHATGPT_PLUGIN_NAME = "bugx"
CHATGPT_PLUGIN_SOURCE = "./plugins/bugx"
CHATGPT_GLOBAL_MARKETPLACE_NAME = "bugx-global"
CHATGPT_GLOBAL_PLUGIN_DIR_NAME = "chatgpt-plugin"
CHATGPT_LEGACY_MARKETPLACE_PREFIX = "bugx-local-"
_ACTIVE_TRACE_ID: str | None = None

RESET_PROJECT_DIRECTORIES = (
    "pollings", "state", "logs", "artifacts", "index", "backups", "locks",
    "evidence", "findings",
)
RESET_PROJECT_FILES = (
    "current.json", "audit-supervisor.json",
    "audit-supervisor.json.bak", "audit-supervisor.json.lock",
    "lifecycle.lock", "bugx-life.json",
    "bugx-life.json.snapshot", "bugx-life.json.lock", "bugx-runtime.json",
    "bugx-runtime.lock", "coverage-evidence.json", "coverage-evidence.lock",
    "finding-ledger.json", "finding-ledger.lock", ".finding-ledger.json.lock",
    "audit-campaign.json", "finding-lifecycle.json", "repair-audit.log",
    "startup-todo.md", "resume_ticket.json", "init.lock", ".init_progress.json",
    "prep-record.md", "module-coverage.md", "strategy.json",
    "attack-graph-snapshot.json", "coverage-ledger.json", "findings.json",
    "life.json", "cloud-onboard.json",
)








def _ensure_core_importable() -> None:
    """把核心引擎目录（全局引擎/asset_root）加入 sys.path，保证 core 模块可 import。"""
    asset_override = os.environ.get("BUGX_ASSET_ROOT")
    if asset_override is not None:
        candidate = Path(asset_override).expanduser()
        try:
            valid_asset = candidate.resolve(strict=True) in _asset_candidates()
        except (OSError, RuntimeError, ValueError):
            valid_asset = False
        if not valid_asset:
            raise CliError(f"BUGX_ASSET_ROOT 指向无效或不安全的资源目录: {asset_override}")
    engine_override = os.environ.get("BUGX_GLOBAL_ENGINE_ROOT")
    if engine_override is not None:
        candidate = Path(engine_override).expanduser()
        if not engine_override or not candidate.is_dir():
            raise CliError(f"BUGX_GLOBAL_ENGINE_ROOT 指向不存在的引擎目录: {engine_override}")
    asset = asset_root()
    global_engine = global_engine_root()
    if global_engine.is_dir() and not (global_engine / "launch_bugx.py").is_file():
        print(
            f"[bugx] 全局引擎目录 {global_engine} 缺失 launch_bugx.py，"
            f"回落至 asset_root: {asset}",
            file=sys.stderr,
        )
    candidates = []
    for cand in (asset, global_engine):
        if cand.is_dir() and (cand / "launch_bugx.py").is_file():
            candidates.append(cand)
    # 后插入者位于 sys.path[0]，故按 (asset, global_engine) 顺序插入 ⇒
    # 最终 global_engine 优先。契约见 `docs/03_ai/AI_REFERENCE.md`「副本升级门禁」：
    # Python 导入必须优先 `global_engine_root()`（共享引擎是运行时真源），
    # 仅在全局引擎缺失 launch_bugx.py 时才回落到 asset_root。
    for cand in candidates:
        s = str(cand)
        while s in sys.path:
            sys.path.remove(s)
        sys.path.insert(0, s)


# 宿主启动子进程时会通过环境变量指明工作区。MCP / 引擎入口若拿不到显式
# ``--root``，必须读它，而不是盲取 ``cwd``：桌面宿主的 cwd 往往是 ``/``，
# 盲取会把项目根解析成 ``/``、找不到 ``/.bugx``，于是 MCP 主通道每次在
# 0.2 秒内 ``exit=2``（日志还会落进 `sha256("/")[:16]` 那个目录）。
HOST_PROJECT_DIR_ENV_VARS: tuple[str, ...] = (
    "BUGX_PROJECT_ROOT",
    "CODEBUDDY_PROJECT_DIR",
    "CLAUDE_PROJECT_DIR",
)


def resolve_project_root(explicit: Path | None = None) -> Path:
    """解析项目根，绝不把"没给"静默等同于"就是 cwd"。

    优先级：显式 ``--root`` → ``BUGX_PROJECT_ROOT`` → 宿主工作区环境变量 → ``cwd``。
    环境变量仅在指向**真实存在的目录**时才被采纳，避免把幽灵路径当成项目根。
    """
    if explicit is not None:
        return Path(explicit)
    for name in HOST_PROJECT_DIR_ENV_VARS:
        if name not in os.environ:
            continue
        raw = os.environ[name]
        if not raw.strip():
            raise CliError(f"{name} 不能是空路径")
        try:
            candidate = Path(raw).expanduser()
        except (OSError, RuntimeError, ValueError) as exc:
            raise CliError(f"{name} 不是有效的项目目录路径: {raw}") from exc
        if not candidate.is_dir():
            raise CliError(f"{name} 指向不存在或非目录路径: {raw}")
        return candidate
    return Path.cwd()


def _i18n_exc(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        _ensure_core_importable()
        import core.i18n as i18n
        return i18n.exception_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def _i18n_prompt(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        _ensure_core_importable()
        import core.i18n as i18n
        return i18n.prompt_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def _resolve_locale_name(locale: str | None = None) -> str:
    if locale:
        return locale
    try:
        _ensure_core_importable()
        import core.i18n as i18n
        return i18n.resolve_locale().resolved_locale
    except Exception:
        return "zh"




def _rooted(root: Path, path: Path, *, allow_controlled_core: bool = False) -> Path:
    """Resolve and enforce that path is within the project root."""
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        if allow_controlled_core and _controlled_core_resource(root, candidate, resolved):
            return resolved
        raise CliError(_i18n_exc("common.path_must_be_inside_root", f"路径必须位于项目根目录内: {path}", path=path)) from exc
    return resolved
































































def _seed_asset_in(source: Path, name: str) -> Path | None:
    """在引擎载荷内按**文件名**定位种子模板，找不到返回 ``None``。

    种子模板已归档到 ``core/ledger/``（``bug-log.md`` / ``mistake-book.md``），
    而这里曾经写的是 ``source / name``：文件搬进子包后该路径恒不存在，
    ``is_file()`` 恒假于是**静默跳过**——新项目永远没有
    ``.bugx/knowledge/bug-log.md``，而 ``init`` 依旧退出 0。
    这是「文件归档进子包、消费方仍用扁平路径」缺陷族的又一例，改为按文件名
    在载荷树内唯一解析，后续再搬目录也不需要改这里。同名多份时 fail-closed。
    """
    direct = source / name
    if direct.is_file() and not direct.is_symlink():
        return direct
    matches = [
        path for path in _engine_basename_index(source).get(name, [])
        if not path.is_symlink()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listed = "、".join(str(path) for path in matches)
        raise CliError(_i18n_exc(
            "common.knowledge_seed_ambiguous",
            f"引擎载荷中存在多个同名知识种子，拒绝猜测：{listed}", listed=listed,
        ))
    return None


def _initialize_project_knowledge_snapshots(root: Path, source: Path) -> None:
    """Create project-owned human-readable knowledge snapshots once."""
    layout = _layout_module(root).require(root)
    for name, target in (
        ("bug-log.md", layout.bug_log),
        ("mistake-book.md", layout.mistake_book),
    ):
        if target.is_symlink():
            raise CliError(_i18n_exc("common.knowledge_snapshot_symlink_refused", f"项目知识快照不能是符号链接: {target}", target=target))
        source_snapshot = _seed_asset_in(source, name)
        if not target.exists() and source_snapshot is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_snapshot, target)
            target.chmod(0o600)


def _logging_module(root: Path):
    """Load the versioned logger from the same asset source as the runtime."""
    path = _script(root, "bugx_logging.py")
    spec = importlib.util.spec_from_file_location("bugx_runtime_logging", path)
    if spec is None or spec.loader is None:
        raise CliError(_i18n_exc("common.logging_module_load_failed", f"无法加载 BugX 日日志模块: {path}", path=path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _configure_logging(root: Path, command: str, level: str | None = None,
                       trace_id: str | None = None):
    try:
        return _logging_module(root).configure(
            root, command=command, level=level, trace_id=trace_id,
        )
    except (OSError, ValueError, CliError) as exc:
        # Logging must never hide the command's real error or block a read-only
        # discovery operation. The command still reports its normal result.
        if (level or os.environ.get("BUGX_LOG_LEVEL", "")).upper() == "DEBUG":
            print(f"[bugx] ! 诊断日志未启用: {exc}", file=sys.stderr)
        return None


def diagnostic_log_path(root: Path) -> Path:
    """Return the diagnostic log path that logging actually writes to.

    Writers and readers must agree: an initialized project keeps
    ``<root>/.bugx/logs/bugx.jsonl``, while an uninitialized directory is
    logged to the per-user fallback (see ``bugx_logging.log_path``).  Callers
    that package or display the log must not re-derive the project path.
    """
    return _logging_module(root).log_path(root)


def _log_update_event(root: Path, event: str, message: str = "", **fields: object) -> None:
    """Best-effort structured update diagnostics without logging full responses."""
    import logging

    logger = logging.getLogger("bugx")
    if not any(getattr(handler, "_bugx_handler", False) for handler in logger.handlers):
        return
    try:
        _logging_module(root).log_event(logger, event, message, **fields)
    except (OSError, ValueError, CliError):
        # Update diagnostics must never turn a network failure into a logging failure.
        return


def _update_failure_fields(result: object) -> dict[str, object]:
    """Extract bounded, non-secret version-check failure metadata."""
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        data = {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    error_code = (
        error.get("code") or data.get("error_code") or nested.get("error_code")
        or data.get("code")
    )
    request_id = (
        data.get("request_id") or data.get("requestId") or nested.get("request_id")
        or nested.get("requestId")
    )
    message = getattr(result, "error", None) or data.get("message") or error.get("message")
    return {
        "http_status": getattr(result, "status", None),
        "error_code": str(error_code)[:128] if error_code is not None else None,
        "request_id": str(request_id)[:128] if request_id is not None else None,
        "error_summary": str(message or "unknown error")[:512],
    }


# 引擎把 ``help``/``-h``/``--help`` 一律当只读帮助视图
# （core/launch_phases/round_orchestrator.py「--help/help/-h 只读」契约）。
# CLI 与引擎共用这一份枚举，避免「帮助可读但日志已落盘」的静默漂移。
ENGINE_HELP_TOKENS = frozenset({"help", "-h", "--help"})


def _defer_command_logging_until_authorized(args: argparse.Namespace) -> bool:
    """Keep preview and rejected confirmation paths free of file writes.

    The normal CLI logger writes ``command_started`` before dispatch.  That is
    useful for commands which are going to run, but it violates the contract
    for a dry-run or an operation rejected solely because the user has not
    confirmed it yet.  Those paths must be observationally read-only, so do
    not create/open the project log until authorization has succeeded.
    """
    command = getattr(args, "command", None)
    if command == "completion":
        # Shell setup must be observationally read-only: emitting a script must
        # not create a project log merely because it was run from a repository.
        return True
    if command == "reset":
        return bool(getattr(args, "dry_run", False) or not getattr(args, "yes", False))
    if command == "uninstall":
        if getattr(args, "dry_run", False):
            # Both project-level and --self previews are observational only;
            # do not append command_started/command_finished to .bugx/logs.
            return True
        if getattr(args, "self", False):
            # --self 处理的是用户级资源，不得因为 cwd 恰好是普通项目而
            # 顺手创建项目级 .bugx/logs。卸载结果由 stdout/JSON 和外部
            # 备份记录，不能把当前项目变成新的残留来源。
            return True
        if not getattr(args, "all", False) and not getattr(args, "agent"):
            # 裸 uninstall 是引导/清单路径：必须观测只读（日志初始化会创建 .bugx）。
            return True
        dangerous = bool(
            getattr(args, "purge_data", False)
            or getattr(args, "purge_global_data", False)
        )
        return dangerous and not getattr(args, "yes", False)
    if command == "plugin":
        return (
            getattr(args, "plugin_command", None) == "uninstall"
            and not getattr(args, "yes", False)
        )
    if command == "engine":
        forwarded = list(getattr(args, "engine_args", []) or [])
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        # 裸 ``bugx engine`` 会被 command_engine 默认成 status，是真实调度，
        # 必须留日志；只有转发里出现帮助词时引擎才只打印帮助、不碰运行态。
        return bool(forwarded) and any(
            token in ENGINE_HELP_TOKENS for token in forwarded
        )
    return False


def _close_bugx_logging() -> None:
    """Close the active diagnostic file before a reset moves ``.bugx/logs``."""
    import logging

    logger = logging.getLogger("bugx")
    for handler in list(logger.handlers):
        try:
            handler.close()
        finally:
            logger.removeHandler(handler)


def _ensure_project_readme(root: Path, source: Path) -> None:
    target = root / ".bugx" / "README.md"
    template = source / "project-layout.md"
    if target.exists() or not template.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, target)
    target.chmod(0o600)


def _run_script(root: Path, name: str, arguments: list[str], timeout: float | None = None) -> int:
    """运行 core 脚本并透传退出码。

    ``timeout``（P3a）：lesson/knowledge/probe/self-audit 类调用传 300 秒，
    防止失控脚本永久挂起；mcp serve 与 mcp-runtime start 需要长驻/直通
    stdio，保持 None（不设超时）。
    """
    _ensure_core_importable()
    try:
        import core.bugx_paths as bugx_paths
        for arg in arguments:
            if isinstance(arg, str):
                bugx_paths.assert_safe_command(arg, root=root)
    except Exception as exc:
        if "[FATAL_BLOCKED]" in str(exc):
            raise CliError(str(exc)) from exc
    command = [sys.executable, str(_script(root, name)), *arguments]
    logger = _configure_logging(root, name, trace_id=_ACTIVE_TRACE_ID)
    logging_module = _logging_module(root) if logger is not None else None
    if logger is not None and logging_module is not None:
        logging_module.log_event(logger, "core_script_started", script=name)
    try:
        environment = _python_environment()
        if logger is not None and logging_module is not None:
            context = logging_module.get_context(logger)
            environment.update({
                "BUGX_TRACE_ID": str(context["trace_id"]),
                **({"BUGX_RUN_ID": str(context["run_id"])}
                   if context.get("run_id") else {}),
                **({"BUGX_LIFECYCLE_ID": str(context["lifecycle_id"])}
                   if context.get("lifecycle_id") else {}),
                **({"BUGX_ROUND": str(context["round"])}
                   if context.get("round") is not None else {}),
                **({"BUGX_WORKER_ID": str(context["worker_id"])}
                   if context.get("worker_id") else {}),
            })
        result = subprocess.call(
            command, cwd=root.resolve(), env=environment, timeout=timeout,
        )
        if logger is not None and logging_module is not None:
            logging_module.log_event(logger, "core_script_finished", script=name,
                                      exit_code=result)
        return result
    except subprocess.TimeoutExpired as exc:
        if logger is not None and logging_module is not None:
            logging_module.log_event(logger, "core_script_failed", str(exc),
                                      level=40, script=name, exc_info=True)
        raise CliError(_i18n_exc("common.script_execution_timeout", f"脚本执行超时（>{timeout}s）: {name}", timeout=timeout, name=name)) from exc
    except OSError as exc:
        if logger is not None and logging_module is not None:
            logging_module.log_event(logger, "core_script_failed", str(exc),
                                      level=40, script=name, exc_info=True)
        raise CliError(_i18n_exc("common.script_run_failed", f"无法运行 {name}: {exc}", name=name, exc=exc)) from exc


def _run_script_json(
    root: Path, name: str, arguments: list[str], timeout: float | None = 300.0,
) -> dict | None:
    """运行 core 脚本并解析其 stdout 的 JSON（不流式输出）。

    与 ``_run_script`` 共用安全校验与 Python 环境；脚本崩溃、超时或输出非 JSON 时
    返回 ``None``（由调用方决定降级还是报错），保证子进程噪声不混进 UI。
    """
    _ensure_core_importable()
    try:
        import core.bugx_paths as bugx_paths
        for arg in arguments:
            if isinstance(arg, str):
                bugx_paths.assert_safe_command(arg, root=root)
    except Exception as exc:  # noqa: BLE001
        if "[FATAL_BLOCKED]" in str(exc):
            raise CliError(str(exc)) from exc
    command = [sys.executable, str(_script(root, name)), *arguments]
    try:
        result = subprocess.run(
            command, cwd=root.resolve(), env=_python_environment(),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    payload = (result.stdout or "").strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _python_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return the CLI environment for Python core scripts.

    Core scripts are executed from the global engine (or the bundled checkout
    asset during development). The project contains state only, so Python's
    bytecode cache must never be used as an implicit project code layer.

    ``overrides`` lets callers pin project-scoped env vars (e.g.
    ``BUGX_PROJECT_ROOT``) that the core scripts resolve relative to ``cwd``.
    """
    environment = os.environ.copy()
    entries = [item for item in environment.get("PATH", "").split(os.pathsep) if item]
    candidates = [environment.get("BUGX_TOOL_PATH", "")]
    if os.name != "nt":
        candidates.extend((
            "/opt/homebrew/bin", "/usr/local/bin",
            "/home/linuxbrew/.linuxbrew/bin",
            str(Path.home() / ".local" / "bin"),
            str(Path.home() / ".cargo" / "bin"),
        ))
    for candidate in candidates:
        if candidate and candidate not in entries:
            try:
                if Path(candidate).is_dir():
                    entries.append(candidate)
            except OSError:
                continue
    environment["PATH"] = os.pathsep.join(entries)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if "BUGX_PYTHON" not in environment and sys.executable:
        environment["BUGX_PYTHON"] = sys.executable
    if overrides:
        environment.update(overrides)
    return environment


@contextmanager
def _cli_version_environment():
    """Expose this source CLI version to child processes without leaking it to callers."""
    previous = os.environ.get("BUGX_CLI_VERSION")
    os.environ.setdefault("BUGX_CLI_VERSION", __version__)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("BUGX_CLI_VERSION", None)
        else:
            os.environ["BUGX_CLI_VERSION"] = previous


















































def _is_bugx_source_repo(root: Path) -> bool:
    """Check if the given root directory is the BugX development source repository.

    委托给 ``core.bugx_paths`` 的权威实现：判定同时被全局项目注册表守卫使用，
    两处各写一份必然漂移（本仓反复出现的缺陷族）。
    """
    from core.bugx_paths import is_bugx_source_repo
    return is_bugx_source_repo(root)


def _resolve_plugin_version(root: Path, default_version: str = __version__) -> str:
    """Resolve plugin version: in BugX dev repo, use repo baseline to avoid declaration drift."""
    if _is_bugx_source_repo(root):
        version_file = root / "VERSION"
        if version_file.is_file():
            try:
                v = version_file.read_text(encoding="utf-8").strip()
                if v:
                    return v
            except OSError:
                pass
    return default_version


def get_build_id() -> int | None:
    """获取当前 Build ID（整型正整数，DOC-169）。"""
    env_id = os.environ.get("BUGX_BUILD_ID")
    if env_id is not None and re.fullmatch(r"[0-9]+", env_id) is None:
        raise CliError("BUGX_BUILD_ID 必须仅包含十进制数字")
    env_value: int | None = None
    if env_id is not None:
        try:
            env_value = int(env_id)
        except ValueError as exc:
            raise CliError("BUGX_BUILD_ID 超出支持的整数范围") from exc
        if env_value < 1:
            raise CliError("BUGX_BUILD_ID 必须是正整数")
    build_id = getattr(bugx_cli, "__build_id__", None)
    if isinstance(build_id, int) and not isinstance(build_id, bool) and build_id >= 1:
        return build_id
    if env_value is not None:
        return env_value
    try:
        from bugx_cli.infra.engine_installer import asset_root
        meta = asset_root() / "metadata.txt"
        if meta.is_file():
            content = meta.read_text(encoding="utf-8")
            match = re.search(r"^build_id:\s*(\d+)", content, re.M)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    try:
        from bugx_cli.infra.engine_installer import global_engine_root
        meta = global_engine_root() / "metadata.txt"
        if meta.is_file():
            content = meta.read_text(encoding="utf-8")
            match = re.search(r"^build_id:\s*(\d+)", content, re.M)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None


def get_full_version() -> str:
    """返回标准版本与 Build ID 组合字符串，例如 0.1.0+build.4。"""
    bid = get_build_id()
    if bid is not None:
        return f"{__version__}+build.{bid}"
    return __version__


# 显式导出全部公有与内部辅助符号，支持模块化子命令透明引用
__all__ = [name for name in list(globals().keys()) if not (name.startswith("__") and name.endswith("__"))]
