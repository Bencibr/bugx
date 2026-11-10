#!/usr/bin/env python3
"""宿主适配层 + MCP 环境检查 + RealHooks（宿主无关）。

- HostAdapter：抽象各宿主差异（headless agent 启动 / agent 配置路径 / 必需 MCP / MCP 探活）。
- detect_host：按当前环境选适配器；宿主清单与项目标记来自 core/host_registry.py
  （OpenCode、Claude、Codex、Pi、Cline、ZCode、DSH、ChatGPT、WorkBuddy）。
- check_mcp_env：读宿主 agent/MCP 配置，返回能力目录、可选降级和真正的硬缺失。
- RealHooks：launch_bugx pre / 宿主 agent 运行（cwd=项目根、继承 MCP auth/env、
  超时、TRUNCATED）/ post（带基线）；G-Provider：子进程非零/超时 → 报错。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logger = logging.getLogger(__name__)

from core.host_runtime import host_registry
import core.hosts as host_plugins
from core.infra.client_metadata import client_version

from core.governance.goal_governor import (
    FailureKind,
    GoalFailureClassifier,
    GoalOracle,
    GoalProgress,
    GoalSpec,
    QuarantineManager,
    SelfHealingController,
)
from core.supervisor.worker_execution import run_bounded, terminate_group

# ---- 1. MCP 注册表与门禁校验 ----
import core.host_runtime.mcp_registry_gate as _rg
_MCP_REGISTRY_VIOLATIONS = _rg._MCP_REGISTRY_VIOLATIONS
from core.host_runtime.mcp_registry_gate import (
    IMPLEMENTED_INSTALLERS,
    MCP_INSTALLERS,
    MCP_REGISTRY,
    MCP_TRUST_LEVELS,
    McpRegistryError,
    active_mcp_names,
    MCP_CATALOG,
    MCP_POLICY,
    host_entry_status,
    mcp_credentials,
    mcp_entry,
    mcp_installer,
    mcp_tier,
    planned_mcp_names,
    registry_violations,
    validate_mcp_registry,
)

# ---- 2. 宿主适配器与探测 ----
import core.host_runtime.adapters as _ad
_ADAPTERS = _ad._ADAPTERS
_MISSING_ADAPTERS = _ad._MISSING_ADAPTERS
_host_marker = _ad._host_marker
_probe_active_host = _ad._probe_active_host
from core.host_runtime.adapters import (
    AgentRun,
    ChatGptAdapter,
    FunIdeAdapter,
    HostAdapter,
    PluginAdapter,
    QoderAdapter,
    TreaAdapter,
    UnknownAdapter,
    WorkBuddyAdapter,
    detect_host,
    host_adapter,
    BUGX_SUPERVISOR_MCP,
    HOST_NAMES,
)

# ---- 3. 全局常量 ----
CAPABILITY_STATES = (
    "configured", "loaded", "callable", "healthy", "not_applicable", "blocked",
)
ENGINE_INTEGRITY_SCHEMA = 2
MCP_SESSION_PROOF_ENV = "BUGX_MCP_SESSION_PROOF"
MCP_SESSION_PROOF_MAX_BYTES = 64 * 1024
MCP_SESSION_PROOF_FILE_PREFIX = "mcp-session-proof-"
HOST_AGENT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
GOAL_STATUS_READ_ATTEMPTS = 3
GOAL_STATUS_READ_RETRY_DELAY = 5.0
GOAL_TURN_HEARTBEAT_STALE_AFTER = 300.0
GOAL_TURN_HARD_CAP_RATIO = 8

# ---- 4. MCP 进程探活与会话观测追踪 ----
from core.host_runtime.mcp_process_tracker import (
    active_mcp_process_probe,
    mcp_session_observations,
)
import core.host_runtime.mcp_process_tracker as _pt
_config_candidates = _pt._config_candidates
_configured_server_command = _pt._configured_server_command
_current_mcp_server_version = _pt._current_mcp_server_version
_file_mcp_session_observations = _pt._file_mcp_session_observations
_mcp_process_matches_proof = _pt._mcp_process_matches_proof
_mcp_record_matches_current_engine = _pt._mcp_record_matches_current_engine
_merge_mcp_observation = _pt._merge_mcp_observation
_pid_is_alive = _pt._pid_is_alive
_process_cwd = _pt._process_cwd
_process_list = _pt._process_list
_process_matches_root = _pt._process_matches_root
_read_mcp_config = _pt._read_mcp_config
_read_mcp_document = _pt._read_mcp_document
_same_mcp_command = _pt._same_mcp_command
_set_from = _pt._set_from

# ---- 5. 宿主环境能力检查（模块核心） ----
def _explicit_bool(cfg: Mapping[str, object], *names: str) -> bool | None:
    for name in names:
        value = cfg.get(name)
        if isinstance(value, bool):
            return value
    return None


def _tool_names(cfg: Mapping[str, object]) -> set[str]:
    for key in ("tools", "tool_names", "loaded_tools"):
        value = cfg.get(key)
        if isinstance(value, (list, tuple, set, frozenset)):
            return {str(item) for item in value}
    return set()


def _legacy_error(cfg: Mapping[str, object]) -> str:
    error = cfg.get("error") or cfg.get("failure_reason")
    if isinstance(error, str) and error.strip():
        return error.strip()
    state = cfg.get("status") or cfg.get("state")
    if isinstance(state, str) and state.casefold() in {
        "error", "failed", "unavailable", "unreachable", "stale",
    }:
        return state
    return ""


def _capability_record(
    name: str,
    cfg: Mapping[str, object] | None,
    *,
    applicable: bool,
    required: bool,
    adapter: HostAdapter,
    root: Path,
    loaded_mcp: Iterable[str] | Mapping[str, object] | None = None,
    callable_mcp: Iterable[str] | Mapping[str, object] | None = None,
    healthy_mcp: Iterable[str] | Mapping[str, object] | None = None,
    tool_names: Iterable[str] | Mapping[str, object] | None = None,
) -> dict[str, object]:
    configured = cfg is not None
    data = cfg if isinstance(cfg, Mapping) else {}
    enabled = _explicit_bool(data, "enabled", "active")
    if enabled is None:
        enabled = True
    static_healthy = False
    health_error = ""
    if configured and enabled:
        try:
            static_healthy = bool(adapter.mcp_health(name, dict(data), root))
        except Exception as exc:  # noqa: BLE001
            health_error = str(exc)

    loaded = _explicit_bool(data, "loaded", "session_loaded", "initialized")
    if loaded is None:
        loaded = _set_from(loaded_mcp, name)
    names = _tool_names(data)
    if isinstance(tool_names, Mapping):
        supplied_names = tool_names.get(name)
        names.update(str(item) for item in supplied_names) if isinstance(
            supplied_names, (list, tuple, set, frozenset)
        ) else None
    elif tool_names is not None:
        names.update(str(item) for item in tool_names)
    if names:
        loaded = True

    callable_state = _explicit_bool(data, "callable", "callable_status", "tool_call")
    if callable_state is None:
        callable_state = _set_from(callable_mcp, name)
    prefix = "bugx_supervisor_" if name == BUGX_SUPERVISOR_MCP else ""
    if callable_state is None and prefix:
        callable_state = any(item.startswith(prefix) for item in names)

    healthy = _explicit_bool(data, "healthy", "health")
    if healthy is None:
        healthy = _set_from(healthy_mcp, name)
    if healthy is None and configured and enabled:
        healthy = static_healthy

    reason = health_error or _legacy_error(data)
    blocked = False
    if not applicable:
        status = "not_applicable"
        reason = reason or "未选择或不适用"
    elif not configured:
        status = "blocked" if required else "not_applicable"
        blocked = required
        reason = reason or ("关键 MCP 未配置" if required else "可选 MCP 未配置")
    elif not enabled:
        status = "blocked"
        blocked = True
        reason = reason or "MCP 已配置但被禁用"
    elif healthy is False:
        status = "blocked"
        blocked = True
        reason = reason or "MCP 健康检查失败"
    elif callable_state is True and healthy is True:
        status = "healthy"
    elif callable_state is True:
        status = "callable"
    elif loaded is True:
        status = "loaded"
    else:
        status = "configured"
        reason = reason or "当前会话尚未证明 MCP 已加载或可调用"

    return {
        "name": name,
        "status": status,
        "state": status,
        "configured": configured,
        "loaded": loaded is True,
        "callable": callable_state is True,
        "healthy": healthy is True,
        "not_applicable": status == "not_applicable",
        "blocked": blocked,
        "failure_reason": reason or None,
    }


def _host_capability(root: Path, adapter: HostAdapter) -> dict[str, object]:
    import shutil
    configured = adapter.name != "unknown" and bool(_host_marker(root, adapter.name))
    executable = shutil.which(adapter.executable) if adapter.executable else None
    callable_state = adapter.is_callable(root) if adapter.name != "unknown" else False
    if adapter.name == "unknown":
        status = "blocked"
        reason = (
            "宿主主动探查失败：进程祖先链未发现已知宿主，且项目存在多个宿主"
            "标记无法安全推断；请从目标宿主 agent 的会话中启动 BugX"
        )
    elif not callable_state:
        status = "blocked"
        reason = "宿主已识别但可执行入口不可调用"
    elif configured:
        status = "callable"
        reason = None
    else:
        status = "loaded"
        reason = "仅确认宿主入口可调用，未发现项目级宿主配置"
    return {
        "name": adapter.name,
        "status": status,
        "configured": configured,
        "loaded": status in {"loaded", "callable"},
        "callable": callable_state,
        "healthy": callable_state,
        "not_applicable": False,
        "blocked": status == "blocked",
        "executable": executable,
        "failure_reason": reason,
    }


def _interface_capability(adapter: HostAdapter) -> dict[str, object]:
    interaction = os.environ.get("BUGX_INTERACTION", "").strip().casefold()
    if interaction in {"question", "native", "native-question", "native-single-select"}:
        interaction = "native-single-select"
    elif interaction in {"tty", "tty-radio", "radio", "tty-menu"}:
        interaction = "tty-menu"
    elif interaction in {"text", "text-menu"}:
        interaction = "text-menu"
    elif interaction in {"none", "blocked", "headless", "unavailable"}:
        interaction = "unavailable"
    else:
        try:
            has_tty = bool(sys.stdin.isatty() and sys.stdout.isatty())
        except (AttributeError, OSError):
            has_tty = False
        interaction = "tty-menu" if has_tty else "unavailable"

    transport = os.environ.get("BUGX_TRANSPORT", "").strip().casefold()
    if transport not in {"stdio", "tty", "acp", "pipe"}:
        transport = "acp" if os.environ.get("BUGX_ACP", "").strip() == "1" else "stdio"
    process_mode = os.environ.get("BUGX_PROCESS_MODE", "").strip().casefold()
    if process_mode not in {"interactive", "headless"}:
        process_mode = "headless" if os.environ.get("BUGX_HEADLESS", "").strip() == "1" else "interactive"
    surface = os.environ.get("BUGX_HOST_SURFACE", "").strip().casefold() or adapter.host_surface
    verified = os.environ.get("BUGX_INTERACTION_VERIFIED", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }
    if interaction == "tty-menu":
        verified = True
    blocked = interaction == "unavailable"
    callable_state = interaction != "unavailable" and verified
    return {
        "host_surface": surface,
        "transport": transport,
        "process_mode": process_mode,
        "interaction": interaction,
        "verified": verified,
        "status": "blocked" if blocked else "callable" if callable_state else "configured",
        "configured": True,
        "loaded": verified,
        "callable": callable_state,
        "healthy": callable_state,
        "not_applicable": False,
        "blocked": blocked,
        "failure_reason": "未确认可用的原生单选/TTY 交互能力" if not callable_state else None,
    }


def check_host_env(root: Path, adapter: HostAdapter | None = None) -> dict[str, object]:
    """Return the normalized host and interface capability snapshot."""
    adapter = adapter or detect_host(root)
    host = _host_capability(root, adapter)
    interface = _interface_capability(adapter)
    capabilities = {"host": host, "interface": interface}
    states = {state: [] for state in CAPABILITY_STATES}
    for name, record in capabilities.items():
        status = str(record["status"])
        if status in states:
            states[status].append(name)
    return {
        "host": adapter.name,
        "adapter": type(adapter).__name__,
        "host_capability": host,
        "interface": interface,
        "capabilities": capabilities,
        **states,
        "states": states,
        "capability_states": states,
    }


def check_mcp_env(
    root: Path,
    adapter: HostAdapter | None = None,
    *,
    loaded_mcp: Iterable[str] | Mapping[str, object] | None = None,
    callable_mcp: Iterable[str] | Mapping[str, object] | None = None,
    healthy_mcp: Iterable[str] | Mapping[str, object] | None = None,
    tool_names: Iterable[str] | Mapping[str, object] | None = None,
) -> dict:
    """Return backward-compatible MCP fields plus normalized capability states.

    Static registration is deliberately not treated as session loading.  A
    caller that has a host session receipt can pass loaded/callable/healthy
    observations to distinguish the later states without changing the config
    reader or starting an MCP process.  Process liveness is diagnostic only;
    it never proves that the current host session loaded the MCP.
    """
    adapter = adapter or detect_host(root)
    mcp_cfg = _read_mcp_config(root, adapter)
    # Explicit arguments are used by tests and host adapters; otherwise use
    # the receipt from the current MCP session.  Never infer loading from a
    # static registration file or a matching background process: only the
    # current host session can prove that its tools are available.
    observations = mcp_session_observations(root)
    observation_source = "session-proof" if observations and not observations.get("error") else ""
    if loaded_mcp is None and not observations.get("error"):
        loaded_mcp = observations.get("loaded")  # type: ignore[assignment]
    if callable_mcp is None and not observations.get("error"):
        callable_mcp = observations.get("callable")  # type: ignore[assignment]
    if healthy_mcp is None and not observations.get("error"):
        healthy_mcp = observations.get("healthy")  # type: ignore[assignment]
    if tool_names is None and not observations.get("error"):
        tool_names = observations.get("tool_names")  # type: ignore[assignment]
    mcp_cfg = _read_mcp_config(root, adapter)
    required = list(adapter.required_mcp(root))
    names = required
    policy = {name: MCP_POLICY.get(name, "optional") for name in required}
    capabilities: dict[str, dict[str, object]] = {}
    configured, degraded, missing = [], [], []
    for name in names:
        is_required = name in required
        hard_required = policy.get(name) == "hard_for_mcp_auto"
        cfg = mcp_cfg.get(name)
        applicable = hard_required or cfg is not None
        if cfg is None and is_required:
            missing.append(name)
        record = _capability_record(
            name, cfg, applicable=applicable, required=hard_required,
            adapter=adapter, root=root, loaded_mcp=loaded_mcp,
            callable_mcp=callable_mcp, healthy_mcp=healthy_mcp,
            tool_names=tool_names,
        )
        capabilities[name] = record
        if cfg is not None and is_required:
            configured.append(name)
            if not record["healthy"]:
                degraded.append(name)

    state_lists = {state: [] for state in CAPABILITY_STATES}
    for name, record in capabilities.items():
        state = str(record["status"])
        if state in state_lists:
            state_lists[state].append(name)
    host_capability = _host_capability(root, adapter)
    interface = _interface_capability(adapter)
    all_capabilities = {**capabilities, "host": host_capability, "interface": interface}
    return {
        "host": adapter.name,
        "has_config": bool(mcp_cfg),
        "configured": configured,
        "loaded": state_lists["loaded"],
        "callable": state_lists["callable"],
        "healthy": state_lists["healthy"],
        "not_applicable": state_lists["not_applicable"],
        "blocked": state_lists["blocked"],
        "degraded": degraded,
        "critical_missing": [n for n in missing if policy.get(n) == "hard_for_mcp_auto"],
        "missing": missing,
        "policy": policy,
        "optional_missing": [n for n in missing if policy.get(n) == "optional"],
        "optional_degraded": [n for n in degraded if policy.get(n) == "optional"],
        "capabilities": all_capabilities,
        "mcp": capabilities,
        "states": state_lists,
        "capability_states": state_lists,
        "host_capability": host_capability,
        "interface": interface,
        "session_proof": {
            "provided": observation_source == "session-proof",
            "active_probe": observation_source == "process-liveness",
            "source": observation_source or None,
            "error": observations.get("error"),
        },
    }




# ---- 6. 宿主 MCP 启动、探测与子进程环境守卫 ----
from core.host_runtime.host_bootstrap import (
    bootstrap_host_mcp,
    probe_builtin_mcp_session,
)
import core.host_runtime.host_bootstrap as _hb
_BadRun = _hb._BadRun
_builtin_mcp_servers = _hb._builtin_mcp_servers
_extract_json_object = _hb._extract_json_object
_extract_session_id = _hb._extract_session_id
_pinned_child_environment = _hb._pinned_child_environment
_post_summary = _hb._post_summary
_read_json = _hb._read_json
_retry_window_seconds = _hb._retry_window_seconds
_verify_launcher = _hb._verify_launcher

# ---- 7. 宿主无头 Agent 运行 Hooks 与自治循环编排（惰性转发） ----
#
# 这里**不能**在模块级 import ``autonomous_runner``。真实依赖环是：
#
#   autonomous_runner → core.governance.goal_governor
#     → governance/__init__ → capacity_planner
#     → supervisor/__init__（eager ``from . import supervisor_host``）
#     → supervisor_host → autonomous_runner   ← 回到起点
#
# 结果是「谁先被导入谁赢」：以 supervisor 为入口正常，以 autonomous_runner
# 为入口（例如 ``python3 -c 'from core.host_runtime import autonomous_runner'``）
# 必定 ``ImportError: cannot import name 'RealHooks' from partially initialized
# module``。pytest 里恰好有别的用例先导入了 supervisor，于是长期被掩盖成
# 顺序依赖型假绿。本模块对这些名字**只做转发、不做实现**，因此改成 PEP 562
# 惰性属性即可彻底断开环，同时 ``supervisor_host.RealHooks`` /
# ``mock.patch("core.supervisor.supervisor_host.RealHooks.agent")`` 写法照旧成立。
_AUTONOMOUS_EXPORTS = frozenset({
    "RealHooks",
    "run_host_autonomous",
    "AUTONOMOUS_HOST_PROMPT",
    "_autonomous_stop_reason",
    "_confirmation_gate_blocked",
    "_host_reported_blocked",
    "_runtime_has_lifecycle_identity",
    "_supervisor_recovery_needed",
    "_sync_quarantine_from_supervisor",
})


def __getattr__(name: str):
    """按需解析 ``autonomous_runner`` 转发符号（PEP 562）。"""
    if name in _AUTONOMOUS_EXPORTS:
        import core.host_runtime.autonomous_runner as _ar
        return getattr(_ar, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _AUTONOMOUS_EXPORTS)
