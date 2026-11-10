#!/usr/bin/env python3
"""BugX 本地 Supervisor MCP 服务。

这是一个无第三方依赖的 MCP stdio 适配层。它只把现有
``audit_supervisor.Supervisor`` 的状态机暴露给宿主 AI，不执行 worker 命令，
不执行 worker 命令，不访问网络，也不把 ``.bugx`` 的事实源迁移到 MCP 服务进程之外。

每个请求都是一次有限的状态操作；服务不会通过长轮询阻塞等待 worker。宿主应
根据 ``next_action`` 决定下一次调用，lease 超时则由 Supervisor 的 status/gate
操作回收并记录。
"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import atexit
import errno
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows hosts use msvcrt or fallback.
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

_CORE_DIR = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "skills" else Path(__file__).resolve().parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 这些是核心领域模块（core/），缺失即安装损坏：直接 import 让错误在
# 启动时显式失败，不再用 None 兜底掩盖（DOC-031）。
from core.supervisor import audit_supervisor
import core.bugx_paths as bugx_paths
# 双入口守卫（**不是**兼容别名）：本模块既被包内导入，也被宿主以
# ``python core/skills/<name>.py`` 直接当脚本启动（MCP stdio 子进程）。
# 脚本模式下 ``__package__`` 为空，相对导入会 ``ImportError: attempted
# relative import with no known parent package``；此时改用绝对点号名。
# 两条路径指向**同一个**文件，不存在第二套实现。
if __package__:
    from . import mcp_session_proof
else:
    from core.skills import mcp_session_proof
from core.runtime import bugx_logging
from core.coverage import coverage_bootstrap
from core.coverage import module_coverage
from core.retrieval import knowledge_sources
import core.secure_atomic as secure_atomic
from core.planning import unit_kind
from core.supervisor import checkpoint_pipeline
from core.supervisor.supervisor_models import VALID_COVERAGE_STATES, VALID_EVIDENCE_STATES

# LOG-06：租约上限的单一来源。此前 inputSchema 硬编码 3600，而
# supervisor_models.MAX_LEASE_SECONDS 是 14400（注释还写「1 小时」），
# task_scheduler 的推算上限又是 3600——三处漂移。直接引用常量，
# 让 test_consistency 之外也能靠符号本身保持同步。
LEASE_SECONDS_MAX = audit_supervisor.MAX_LEASE_SECONDS

# DES-05：业务单元前缀的单一来源。schema 里的正则此前硬编码 "^业务:.+"，
# 与 unit_kind.PREFIXES 分离——新增单元类型时会漏改协议层校验，导致
# 「协议层接受、领域层不认」或反之的静默不一致。
UNIT_BUSINESS_PATTERN = f"^{unit_kind.UnitKind.BUSINESS.value}:.+"

from core.mcp_handlers import (
    HANDLER_REGISTRY,
    MAX_ARTIFACT_PATH_LENGTH,
    MAX_COMMAND_LENGTH,
    MAX_COVERAGE_PER_CHECKPOINT,
    MAX_EVIDENCE_PER_CHECKPOINT,
    MAX_FINDING_FIELD_LENGTH,
    MAX_FINDING_FINGERPRINT_LENGTH,
    MAX_FINDINGS_PER_CHECKPOINT,
    MAX_MCP_JSON_DEPTH,
    MAX_MCP_JSON_KEYS,
    MAX_MCP_REQUEST_BYTES,
    MAX_UNITS_PER_CLAIM,
    McpProtocolError,
    TOKEN_BASE_BUDGET,
    TOKEN_HARD_CEILING,
    TOKEN_MAX_RENEWALS,
    TOKEN_NEEDED_MAX,
    TOKEN_NEEDED_MIN,
    TOKEN_REASON_MAX_LENGTH,
    TOKEN_RENEWAL_MAX_GRANT,
    TOKEN_RENEWAL_STATE_VERSION,
    bool_argument,
    check_json_depth_and_keys,
    compact_json,
    format_result,
    next_action,
    number_argument,
    optional_string,
    optional_string_array_argument,
    project_state,
    recovery_chain,
    serialize_value,
    string_array_argument,
    validate_checkpoint_sizes,
)


def _finding_severity_levels() -> tuple[str, ...]:
    """finding 严重度单一真源（BX-ENGINE-18）；委托给唯一访问器。

    仅为渲染 MCP schema 的 ``severity`` enum，避免「schema 无 enum / 门禁有词表」
    再次漂移。词表与 fallback 快照的唯一定义在
    ``supervisor_models.finding_severity_levels``，此处不再复制字面量。
    """
    try:
        from core.supervisor.supervisor_models import finding_severity_levels

        return finding_severity_levels()
    except ImportError:  # pragma: no cover - 引擎目录不完整时
        return ("info", "low", "medium", "high", "critical")


_FINDING_SEVERITY_LEVELS = _finding_severity_levels()

def _i18n_exc(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
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
        import core.i18n as i18n
        return i18n.prompt_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "bugx-supervisor"
SERVER_VERSION = "0.1.0"
JSONRPC_VERSION = "2.0"
# A CLI ``pre`` command is short-lived, while the host MCP stdio process owns
# the Session afterwards.  Keep the interval well below the time it takes for
# the next CLI phase to start so a new polling scope is claimed before the
# runtime orphan detector sees the pre process as dead.
HOST_SESSION_HEARTBEAT_INTERVAL_SECONDS = 5.0
MCP_ROLES = {"host", "worker"}
# DES-08：MCP 工具元数据的**单一真源**。
#
# 原先工具元数据分散在三处维护，任一处笔误都会静默产生「被宣告但无法分发」
# 或「能分发但未被宣告」的工具：
#   1. HOST_TOOLS / WORKER_TOOLS / READ_ONLY_TOOLS 三个角色集合（原在本处）；
#   2. ``_tool_definitions()`` 内的 41 条 schema 字面量；
#   3. ``mcp_handlers.HANDLER_REGISTRY``（工具名 -> 实现的分发表）。
# 现在：角色归属与 schema 在 ``_build_tool_table()`` 里**同处声明**，三个集合由它派生，
# 分发表由模块底部的 ``_assert_tool_registry_consistent()`` 强制校验。
#
# 角色语义：三个集合**两两不相交**，并集即全部工具。
#   _HOST_ONLY     仅宿主可调用；_WORKER_ONLY 仅 Worker 可调用；_SHARED_ROLES 双方均可。
_HOST_ONLY = frozenset({"host"})
_WORKER_ONLY = frozenset({"worker"})
_SHARED_ROLES = frozenset({"host", "worker"})

# (Protocol caps and McpProtocolError imported from mcp_handlers)


import core.mcp_handlers.channel_lifecycle as _cl

_pid_alive = _cl._pid_alive
_idle_self_exit_seconds = _cl._idle_self_exit_seconds
_ChannelActivity = _cl._ChannelActivity
_channel_active_claim_keys = _cl._channel_active_claim_keys
_channel_idle_exit_reason = _cl._channel_idle_exit_reason
_record_lease_event = _cl._record_lease_event
_channel_self_retire = _cl._channel_self_retire
_channel_idle_monitor_loop = _cl._channel_idle_monitor_loop
_read_lease_holder = _cl._read_lease_holder
_holder_idle_seconds = _cl._holder_idle_seconds
_takeover_holder = _cl._takeover_holder
_host_channel_state_path = _cl._host_channel_state_path
_clear_host_channel_unavailable = _cl._clear_host_channel_unavailable
_run_external = _cl._run_external
_holder_started_at = _cl._holder_started_at
_record_host_channel_unavailable = _cl._record_host_channel_unavailable
_ControllerLease = _cl._ControllerLease
_host_session_heartbeat_once = _cl._host_session_heartbeat_once
_host_session_heartbeat_loop = _cl._host_session_heartbeat_loop

def _error_payload(error: BaseException) -> dict[str, object]:
    return {
        "error_type": type(error).__name__,
        "message": str(error) or type(error).__name__,
        "error_code": "MCP_CALL_FAILED",
        "interactive_required": True,
        "next_action": "PROMPT_USER",
        "prompt": (
            "MCP 调用失败，请立即询问用户：1) 立即重试 2) 本轮切换手动/回退 "
            "3) 下次再说（停止本轮）"
        ),
        "choices": ["retry", "fallback", "later"],
    }


def _call_failure_data(operation: object, error: BaseException) -> dict[str, object]:
    """为 tools/call 协议错误提供可被宿主直接呈现的交互门禁。"""
    payload = _error_payload(error)
    payload["operation"] = str(operation or "unknown")
    return payload


# (next_action imported from mcp_handlers)


class BugxMcpService:
    """MCP facade bound to one project and active lifecycle.

    Construction is deliberately fail-closed: it never creates or repairs the
    ``.bugx`` layout on its own.  A missing or incomplete layout must be
    created through an explicit, host-confirmed entry (``bugx init`` /
    ``bugx_paths.initialize`` or a host-driven bootstrap), so an arbitrary
    directory opened by a host cannot silently become a BugX project.
    """

    # Controlled explicit initialization entry retained for hosts that need
    # to bootstrap a fresh workspace.  It requires an explicit host
    # confirmation and never runs implicitly from construction.
    @staticmethod
    def initialize_project(root: Path, *, bugx_version: str = SERVER_VERSION) -> object:
        """Explicit, host-confirmed project initialization entry.

        Callers must have obtained user/host confirmation before invoking.
        This is the only MCP-side path that may create ``.bugx``.
        """
        target = root.expanduser().resolve()
        if not target.is_dir():
            raise McpProtocolError(f"项目根目录不是目录: {target}")
        return bugx_paths.initialize(target, bugx_version=bugx_version)

    def __init__(self, root: Path, *, role: str = "host") -> None:
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise McpProtocolError(f"项目根目录不是目录: {self.root}")
        if role not in MCP_ROLES:
            raise McpProtocolError(f"MCP role 必须是 {sorted(MCP_ROLES)}")
        self.role = role
        # Host injects this identity only into the dedicated Worker stdio
        # process it launches. A caller-supplied MCP argument is never enough
        # to authenticate a graph Worker.
        self.native_agent_id = (
            os.environ.get("BUGX_NATIVE_AGENT_ID") if role == "worker" else None
        )
        # ``tools/list`` can happen before ``pre`` creates a new polling
        # scope.  Retain only the bounded names so a later request/heartbeat
        # can republish the same receipt into the current scope.
        self._session_tool_names: tuple[str, ...] = ()
        # DOC-230 3.1 层 2 / 3.2：由 ``serve()`` 注入的通道活动记录。
        # 非 stdio 场景（进程内服务、测试）保持 None，行为完全不变。
        self.activity: _ChannelActivity | None = None
        # A stdio process can outlive a BugX polling scope.  Bind its proof to
        # one UUID and refresh the scope fields on each publication so a stale
        # PID or a replacement connection cannot overwrite this receipt.
        self._session_instance_id = mcp_session_proof.new_instance_id()
        self._session_process_start_token = mcp_session_proof.process_start_token(os.getpid())
        self._session_last_request_at: float | None = None
        self._session_last_progress_at: float | None = None
        # Fail-closed: do not touch the filesystem here.  Layout validation
        # is deferred to _scope() so construction has no side effects.

    def _scope(self):
        try:
            return bugx_paths.require(self.root)
        except bugx_paths.LayoutError as exc:
            # Surface as a structured MCP protocol error so hosts can prompt
            # for explicit initialization instead of silently creating state.
            raise McpProtocolError(
                f"[INITIALIZATION_REQUIRED] 项目未初始化或布局不完整: {exc}；请先通过受控入口初始化（bugx install --root . --agent {self.role} --non-interactive）",
                code=-32602,
            ) from exc

    def is_initialized(self) -> bool:
        """Check whether the active workspace layout is complete."""
        try:
            bugx_paths.require(self.root)
            return True
        except bugx_paths.LayoutError:
            return False

    def acquire_controller_lease(self) -> _ControllerLease:
        """Acquire the single IDE control lease for the host channel."""
        if self.role != "host":
            raise McpProtocolError("worker MCP 通道不能持有主控租约")
        lease = _ControllerLease(self._scope().state_root / "controller.lock")
        lease.acquire()
        return lease

    def _session_proof_path(self) -> Path:
        return mcp_session_proof.proof_path(
            self._scope().state_root, self._session_instance_id,
        )

    def _session_scope_fields(self) -> tuple[str | None, str | None, int]:
        """Read the current lifecycle binding without making one up."""
        lifecycle_id: str | None = None
        scope: str | None = None
        generation = 0
        try:
            current = bugx_paths.read_current(self.root)
            lifecycle_id = str(
                current.get("lifecycle_id") or current.get("polling_id") or ""
            ).strip() or None
            scope = str(current.get("scope") or lifecycle_id or "").strip() or None
            raw_generation = current.get("scope_generation", 0)
            if isinstance(raw_generation, int) and not isinstance(raw_generation, bool):
                generation = max(0, raw_generation)
        except Exception:
            pass
        if scope is None:
            try:
                scope = self._scope().lifecycle_root.name
            except Exception:
                pass
        return lifecycle_id, scope, generation

    def _session_entrypoint_hash(self) -> str:
        try:
            return __import__("hashlib").sha256(
                Path(__file__).read_bytes()
            ).hexdigest()
        except OSError:
            return "0" * 64

    def _touch_session_request(self) -> None:
        self._session_last_request_at = time.time()
        try:
            mcp_session_proof.update_proof(
                self._scope().state_root, self._session_instance_id,
                last_request_at=self._session_last_request_at,
                inflight=1,
            )
        except (OSError, ValueError, RuntimeError):
            pass

    def _touch_session_progress(self) -> None:
        self._session_last_progress_at = time.time()
        try:
            mcp_session_proof.update_proof(
                self._scope().state_root, self._session_instance_id,
                last_progress_at=self._session_last_progress_at,
                inflight=0,
            )
        except (OSError, ValueError, RuntimeError):
            pass

    def write_session_proof(self, tool_names: Sequence[str]) -> Path:
        """Publish a bounded receipt after the real MCP tools/list handshake."""
        names = tuple(str(name)[:256] for name in list(tool_names)[:256])
        self._session_tool_names = names
        now = time.time()
        lifecycle_id, scope, generation = self._session_scope_fields()
        phase = mcp_session_proof.PHASE_ACTIVE
        try:
            existing = mcp_session_proof.read_proof(
                self._session_proof_path(),
                expected_instance_id=self._session_instance_id,
            )
            phase = str(existing.get("phase") or phase)
            self._session_last_request_at = existing.get("last_request_at")  # type: ignore[assignment]
            self._session_last_progress_at = existing.get("last_progress_at")  # type: ignore[assignment]
        except (OSError, ValueError, RuntimeError):
            pass
        return mcp_session_proof.write_proof(
            self._scope().state_root,
            {
                "schema_version": mcp_session_proof.SCHEMA_VERSION,
                "server": SERVER_NAME,
                "server_version": SERVER_VERSION,
                "root": str(self.root),
                "scope": scope,
                "lifecycle_id": lifecycle_id,
                "scope_generation": generation,
                "role": self.role,
                "instance_id": self._session_instance_id,
                "pid": os.getpid(),
                "process_start_token": self._session_process_start_token,
                "engine_build": SERVER_VERSION,
                "entrypoint_hash": self._session_entrypoint_hash(),
                "transport": "stdio",
                "phase": phase,
                "written_at": now,
                "proof_written_at": now,
                "last_request_at": self._session_last_request_at,
                "last_progress_at": self._session_last_progress_at,
                "inflight": 0,
                "loaded": True,
                "callable": True,
                "healthy": phase == mcp_session_proof.PHASE_ACTIVE,
                "tool_names": list(names),
            },
            instance=self._session_instance_id,
            now=now,
        )

    def refresh_session_proof(self) -> Path | None:
        """Republish the tools/list receipt after the current scope changes.

        OpenCode keeps MCP stdio processes alive across BugX Session creation.
        The service therefore cannot treat the scope captured at process start
        as authoritative.  A refresh is best-effort for MCP availability; the
        autonomous lifecycle gate remains the fail-closed authority.
        """
        if not self._session_tool_names:
            return None
        try:
            return self.write_session_proof(self._session_tool_names)
        except (OSError, ValueError, RuntimeError):
            return None

    def clear_session_proof(self) -> None:
        """Cooperatively close this connection without deleting its receipt."""
        try:
            mcp_session_proof.set_phase(
                self._scope().state_root, self._session_instance_id,
                mcp_session_proof.PHASE_RETIRING,
            )
            mcp_session_proof.set_phase(
                self._scope().state_root, self._session_instance_id,
                mcp_session_proof.PHASE_CLOSED,
            )
        except (OSError, UnicodeError, ValueError, RuntimeError):
            return

    def get_supervisor(self, *, budget: Any = None):
        active = self._scope()
        return audit_supervisor.Supervisor(
            self.root,
            budget=budget,
            state_path=active.supervisor,
        )

    def _coverage_units(self) -> list[str]:
        active = self._scope()
        manifest = active.state_root / "module-coverage.md"
        if not manifest.is_file() or manifest.is_symlink():
            raise McpProtocolError(f"覆盖清单缺失或不安全: {manifest}")
        return checkpoint_pipeline.load_manifest_units(self.root, manifest)

    @staticmethod
    def _coverage_bootstrap_gate(state: Mapping[str, object]) -> dict[str, object]:
        return {
            "status": "COVERAGE_BOOTSTRAP_REQUIRED",
            "interactive_required": True,
            "next_action": "PROMPT_USER",
            "gate": "coverage_bootstrap",
            "question": "覆盖清单尚未建立，是否运行引导创建？",
            "multiple": False,
            "options": [
                {
                    "label": "运行引导创建",
                    "description": "由 BugX Supervisor 创建本生命周期的最小覆盖单元，然后自动重试启动",
                    "value": "bootstrap",
                },
                {
                    "label": "下次再说",
                    "description": "停止本轮启动，不创建或修改任何运行态清单",
                    "value": "later",
                },
            ],
            "detail": str(state.get("detail") or "覆盖清单缺失"),
            "path": state.get("path"),
        }

    def _consumption_ids(self) -> tuple[str, str]:
        """Resolve the current Session/Round/Life/run for knowledge consumption.

        The ledger must be able to audit which lifecycle and run actually
        consumed a pattern/incident.  Using a fixed ``mcp`` placeholder hides
        the true Session/Round/Life/run and breaks the host's traceability
        gate.  We therefore bind to the authoritative project layout and, when
        a Supervisor run exists, to its live ``run_id``.
        """
        lifecycle_id: str | None = None
        run_id: str | None = None
        # Primary lifecycle is the authoritative project current (Life).
        try:
            cur = bugx_paths.read_current(self.root)
            lifecycle_id = str(cur.get("lifecycle_id") or cur.get("polling_id") or "")
            if not run_id:
                base_lifecycle = cur.get("lifecycle_id")
                base_polling = cur.get("polling_id")
                # PE-20261005-003: 诊断兜底与 Supervisor 生成侧同形（polling 限定）
                if base_lifecycle and base_polling:
                    run_id = f"lifecycle-{base_polling}-{base_lifecycle}"
                elif base_lifecycle or base_polling:
                    run_id = f"lifecycle-{base_lifecycle or base_polling}"
        except Exception:
            pass
        # Prefer the live Supervisor run_id when a run exists; keep lifecycle
        # as the current Life (not the polling Session) for ledger indexing.
        try:
            sup = self.get_supervisor()
            st = sup.status()
            rid = getattr(st, "run_id", None)
            if isinstance(rid, str) and rid.strip():
                run_id = rid.strip()
            # If Supervisor has an explicit life_id, it is authoritative for
            # the Life; otherwise keep the current lifecycle.
            lid = getattr(st, "life_id", None)
            if isinstance(lid, str) and lid.strip():
                lifecycle_id = lid.strip()
        except Exception:
            pass
        # Final fallback: use the scope's lifecycle when readable.
        if not lifecycle_id or not run_id:
            try:
                sc = self._scope()
                if not lifecycle_id:
                    lifecycle_id = sc.lifecycle_root.name
                if not run_id:
                    # PE-20261005-003: 与 Supervisor 生成侧同形（polling 限定）
                    _polling_name = sc.lifecycle_root.parent.parent.name
                    run_id = (
                        f"lifecycle-{_polling_name}-{sc.lifecycle_root.name}"
                        if _polling_name.startswith("polling-")
                        else f"lifecycle-{sc.lifecycle_root.name}"
                    )
            except Exception:
                pass
        lifecycle_id = (lifecycle_id or "unknown").strip() or "unknown"
        run_id = (run_id or "unknown").strip() or "unknown"
        # Never return the old placeholder; it would hide the real binding.
        if lifecycle_id == "mcp":
            lifecycle_id = "unknown"
        if run_id == "mcp":
            run_id = "unknown"
        return lifecycle_id, run_id

    def _request_token_renewal(self, *, reason: str, needed: int) -> dict[str, object]:
        """Atomically account for a bounded token renewal in the active lifecycle."""
        try:
            with bugx_paths._project_lock(self.root):
                scope = self._scope()
                state_path = scope.state_root / "mcp-token-renewal.json"
                try:
                    secure_atomic.reject_symlink_components(state_path)
                except secure_atomic.AtomicWriteError as exc:
                    raise McpProtocolError(
                        f"动态续命状态路径不安全: {state_path}", code=-32602,
                    ) from exc

                if state_path.exists():
                    if state_path.is_symlink() or not state_path.is_file():
                        raise McpProtocolError(
                            "动态续命状态损坏，拒绝重置", code=-32602,
                        )
                    try:
                        if state_path.stat().st_size > 65536:
                            raise ValueError("state too large")
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                        raise McpProtocolError(
                            "动态续命状态损坏，拒绝重置", code=-32602,
                        ) from exc
                else:
                    state = {
                        "schema_version": TOKEN_RENEWAL_STATE_VERSION,
                        "lifecycle_id": scope.lifecycle_root.name,
                        "renewals_used": 0,
                        "granted_tokens": 0,
                        "reason_digests": [],
                    }

                if not isinstance(state, dict):
                    raise McpProtocolError("动态续命状态损坏，拒绝重置", code=-32602)
                renewals_used = state.get("renewals_used")
                granted_tokens = state.get("granted_tokens")
                reason_digests = state.get("reason_digests")
                if (
                    state.get("schema_version") != TOKEN_RENEWAL_STATE_VERSION
                    or state.get("lifecycle_id") != scope.lifecycle_root.name
                    or isinstance(renewals_used, bool)
                    or not isinstance(renewals_used, int)
                    or not 0 <= renewals_used <= TOKEN_MAX_RENEWALS
                    or isinstance(granted_tokens, bool)
                    or not isinstance(granted_tokens, int)
                    or not 0 <= granted_tokens <= TOKEN_RENEWAL_MAX_GRANT * TOKEN_MAX_RENEWALS
                    or TOKEN_BASE_BUDGET + granted_tokens > TOKEN_HARD_CEILING
                    or granted_tokens < renewals_used * TOKEN_NEEDED_MIN
                    or granted_tokens > renewals_used * TOKEN_RENEWAL_MAX_GRANT
                    or not isinstance(reason_digests, list)
                    or len(reason_digests) != renewals_used
                    or any(
                        not isinstance(item, str) or len(item) != 64
                        or any(char not in "0123456789abcdef" for char in item)
                        for item in reason_digests
                    )
                ):
                    raise McpProtocolError("动态续命状态损坏，拒绝重置", code=-32602)

                current_budget = TOKEN_BASE_BUDGET + granted_tokens
                if renewals_used >= TOKEN_MAX_RENEWALS:
                    return {
                        "status": "ok",
                        "approved": False,
                        "granted_tokens": 0,
                        "renewals_used": renewals_used,
                        "renewals_remaining": 0,
                        "current_budget": current_budget,
                    }

                grant = min(
                    needed,
                    TOKEN_RENEWAL_MAX_GRANT,
                    TOKEN_HARD_CEILING - current_budget,
                )
                if grant <= 0:
                    return {
                        "status": "ok",
                        "approved": False,
                        "granted_tokens": 0,
                        "renewals_used": renewals_used,
                        "renewals_remaining": 0,
                        "current_budget": current_budget,
                    }

                state["renewals_used"] = renewals_used + 1
                state["granted_tokens"] = granted_tokens + grant
                state["reason_digests"] = [
                    *reason_digests,
                    hashlib.sha256(reason.encode("utf-8")).hexdigest(),
                ]
                secure_atomic.atomic_write_json(
                    state_path, state, mode=0o600, parent_mode=0o700,
                )
                return {
                    "status": "ok",
                    "approved": True,
                    "granted_tokens": grant,
                    "renewals_used": renewals_used + 1,
                    "renewals_remaining": TOKEN_MAX_RENEWALS - renewals_used - 1,
                    "current_budget": current_budget + grant,
                }
        except McpProtocolError:
            raise
        except (bugx_paths.LayoutError, OSError, secure_atomic.AtomicWriteError) as exc:
            raise McpProtocolError(f"动态续命状态不可用: {exc}", code=-32602) from exc

    def _host_channel_state(self) -> dict[str, object]:
        """DOC-230 3.3：返回 host 通道可用性，供 status 内联给 worker 通道。

        - 本进程就是 host → 必然可用；
        - 否则读取 host 启动失败时落盘的冲突记录（带 TTL，避免陈旧误报）；
        - 无记录/记录过期 → `available=None`（**未观察到冲突**，不等于可用）。
        """
        if self.role == "host":
            return {
                "available": True, "blocked_by": None,
                "idle_seconds": None, "how_to_unblock": None, "observed": True,
            }
        path = _host_channel_state_path(self._scope().state_root)
        payload: object = None
        try:
            if path.is_file() and not path.is_symlink():
                payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, Mapping):
            return {
                "available": None, "blocked_by": None,
                "idle_seconds": None, "how_to_unblock": None, "observed": False,
            }
        observed_at = payload.get("observed_at")
        ttl = payload.get("ttl_seconds")
        fresh = (
            isinstance(observed_at, (int, float)) and not isinstance(observed_at, bool)
            and isinstance(ttl, (int, float)) and not isinstance(ttl, bool)
            and (time.time() - float(observed_at)) <= float(ttl)
        )
        if not fresh:
            return {
                "available": None, "blocked_by": None,
                "idle_seconds": None, "how_to_unblock": None,
                "observed": False, "stale": True,
            }
        return {
            "available": False,
            "blocked_by": {
                "holder_pid": payload.get("holder_pid"),
                "holder_started_at": payload.get("holder_started_at"),
                "observed_at": observed_at,
            },
            "idle_seconds": payload.get("holder_idle_seconds"),
            "how_to_unblock": payload.get("how_to_unblock"),
            "observed": True,
        }

    def _status_payload(self, supervisor: Any) -> dict[str, object]:
        try:
            snapshot = supervisor.status_with_gate()
        except audit_supervisor.NoRunError:
            return {
                "status": "NO_RUN",
                "next_action": "START_RUN",
                # DOC-230 3.3：即使尚未 start，worker 通道也必须能判断自己
                # 是否具备恢复能力（否则「静默降级」在这一刻最易发生）。
                "host_channel": self._host_channel_state(),
                "root": str(self.root),
            }
        state_payload = snapshot["state"]
        gate = snapshot["parallel_gate"]
        if isinstance(state_payload, dict):
            state_payload = dict(state_payload)
            for field, limit in (("history", 50), ("findings", 100), ("parallel_proof_history", 20)):
                value = state_payload.get(field)
                if isinstance(value, list) and len(value) > limit:
                    state_payload[field] = value[-limit:]
                    state_payload[f"{field}_truncated"] = True
        # 控制面决策必须消费原始未压缩投影的完整 state 字典
        action_val = next_action(state_payload, gate)
        host_channel = self._host_channel_state()
        chain_val = recovery_chain(
            state_payload, gate, caller_role=self.role,
            host_channel=host_channel.get("blocked_by"),
        )
        payload = project_state(state_payload)
        payload["parallel_gate"] = gate
        payload["next_action"] = action_val
        payload["host_channel"] = host_channel
        payload["recovery_chain"] = chain_val
        payload["root"] = str(self.root)
        return payload

    def _budget(self, arguments: Mapping[str, object]):
        allow_serial = bool_argument(arguments, "allow_serial", False)
        serial_reason = arguments.get("serial_reason")
        if serial_reason is not None and not isinstance(serial_reason, str):
            raise McpProtocolError("serial_reason 必须是字符串或 null")
        require_confirmation = bool_argument(arguments, "require_confirmation", True)
        if not require_confirmation:
            raise McpProtocolError(
                "Supervisor MCP 强制启用 Finding confirmation；"
                "require_confirmation 只能为 true"
            )
        try:
            # Resolve the adaptive concurrency pair once.  RunBudget's default
            # factories otherwise resolve the host again while constructing the
            # ceiling, so a config/environment change between the two reads can
            # produce an inconsistent budget (and makes the protocol hard to
            # test deterministically).
            system_concurrency, system_ceiling = (
                audit_supervisor.resolve_system_concurrency()
            )
            return audit_supervisor.RunBudget(
                max_rounds=number_argument(arguments, "max_rounds", 10, integer=True),
                max_wall_seconds=number_argument(arguments, "max_wall_seconds", 3600.0),
                max_round_wall_seconds=number_argument(
                    arguments, "max_round_wall_seconds", 1800.0,
                ),
                max_tool_calls=number_argument(
                    arguments, "max_tool_calls", 1000, integer=True,
                ),
                max_cost_units=number_argument(
                    arguments, "max_cost_units", 10000.0,
                ),
                max_concurrency=number_argument(
                    arguments, "max_concurrency",
                    system_concurrency,
                    integer=True,
                ),
                max_concurrency_ceiling=system_ceiling,
                require_parallel=not allow_serial,
                serial_reason=serial_reason,
                require_confirmation=require_confirmation,
            )
        except (TypeError, ValueError) as exc:
            raise McpProtocolError(f"budget 参数无效: {exc}") from exc

    def _require_role(
        self, arguments: Mapping[str, object], tool_name: str, *allowed: str,
    ) -> str:
        """Enforce both the declared role and this MCP channel's role.

        A caller-supplied ``caller_role`` is not an identity proof.  The
        actual boundary is the separately configured host/worker stdio
        service, whose tools are filtered by role.  The declaration remains
        required as an explicit protocol assertion and audit field.

        DOC-230 3.6：``_TOOL_ROLES`` 是角色归属的**单一真源**。handler 声明的
        允许角色必须与工具表一致；否则会重现「worker 通道列出工具、调用却必拒」
        的虚假可用性（BX-ENGINE-16）。此处 fail-closed 报错，把漂移暴露在第一次
        调用而不是用户排障时。
        """
        declared_roles = _TOOL_ROLES.get(tool_name)
        if declared_roles is not None and set(allowed) != set(declared_roles):
            raise McpProtocolError(
                f"{tool_name} 角色声明与工具表不一致："
                f"handler={sorted(allowed)} table={sorted(declared_roles)}"
            )
        role = arguments.get("caller_role")
        if not role and self.role in set(allowed):
            role = self.role
        if not isinstance(role, str) or role not in set(allowed):
            raise McpProtocolError(
                f"{tool_name} 需要 caller_role={('/'.join(allowed))}"
            )
        if role != self.role:
            raise McpProtocolError(
                f"{tool_name} 不允许在 {self.role} MCP 通道中以 {role} 身份调用"
            )
        return role

    def call(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(arguments, Mapping):
            raise McpProtocolError("tools/call arguments 必须是对象", code=-32602)
        # Generic bounded check for all tool arguments (depth/keys/size)
        if len(arguments) > 200:
            raise McpProtocolError("arguments 键过多", code=-32602)
        check_json_depth_and_keys(arguments)
        # DOC-230 3.1 层 2 / 3.2：stdin 服务场景下把每次工具调用记为通道活动，
        # 并统计 inflight，使空闲自退既不误退"正在执行"的调用，也能在被遗弃时
        # 主动让位。非 stdio 场景（activity 为 None）行为完全不变。
        activity = self.activity
        if activity is not None:
            activity.enter()
            activity.touch(worker_id=arguments.get("worker_id"))
        try:
            return self._call_locked(name, arguments)
        finally:
            if activity is not None:
                activity.leave()

    def _call_locked(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        def _resolve_tool_name(tool_name: str) -> str:
            if tool_name in _TOOL_ROLES:
                return tool_name
            candidates = [
                f"bugx_supervisor_{tool_name}",
                f"bugx_supervisor_worker_{tool_name}",
            ]
            for candidate in candidates:
                if candidate in _TOOL_ROLES:
                    return candidate
            return tool_name

        resolved_name = _resolve_tool_name(name)
        allowed_roles = _TOOL_ROLES.get(resolved_name)
        if allowed_roles is None:
            raise McpProtocolError(_i18n_exc("mcp.unknown_tool", f"未知工具: {name}", name=name))
        name = resolved_name
        call_arguments = dict(arguments)
        if self.role not in allowed_roles:
            declared_role = call_arguments.get("caller_role") or self.role
            target_channel = "bugx-supervisor-worker" if "worker" in allowed_roles else "bugx-supervisor"
            required_role_desc = next(iter(allowed_roles)) if allowed_roles in (_HOST_ONLY, _WORKER_ONLY) else "/".join(sorted(allowed_roles))
            raise McpProtocolError(
                f"{name} 需要 caller_role={required_role_desc} 且不允许在 {self.role} MCP 通道中以 {declared_role!r} 身份调用；"
                f"请切换至 MCP 服务通道 '{target_channel}'"
            )
        if allowed_roles in (_HOST_ONLY, _WORKER_ONLY):
            required_role = next(iter(allowed_roles))
            # The stdio server itself is the role boundary.
            # If caller_role was omitted, directly infer it from required_role / self.role.
            if not call_arguments.get("caller_role"):
                call_arguments["caller_role"] = required_role
            elif call_arguments.get("caller_role") != required_role:
                raise McpProtocolError(f"{name} 需要 caller_role={required_role}")
        if self.role == "host":
            # The daemon may have started before CLI pre created runtime.json;
            # claim the live host on the first host request instead of waiting
            # for the next periodic tick.
            _host_session_heartbeat_once(self.root)
        # The request may be the first request after a new polling scope was
        # created.  Refresh the role-bound proof before dispatching the tool.
        self.refresh_session_proof()
        self._touch_session_request()
        try:
            return HANDLER_REGISTRY[name](self, call_arguments)
        finally:
            self._touch_session_progress()

    def _knowledge(self):
        """延迟加载错题账本模块；让 worker 一条调用拿到经验卡。"""
        module = getattr(self, "_knowledge_module", None)
        if module is not None:
            return module
        from core.ledger import lesson_ledger

        self._knowledge_module = lesson_ledger
        return lesson_ledger

    def _runtime_intelligence(self) -> list:
        """只读读取 runtime 的 intelligence_history（缺省为空列表）。"""
        try:
            state = json.loads(
                self._scope().runtime.read_text(encoding="utf-8"),
            )
        except (OSError, json.JSONDecodeError):
            return []
        history = state.get("intelligence_history")
        return history if isinstance(history, list) else []

    def read_resource(self, uri: str) -> dict[str, object]:
        """只读读取 BugX 核心事实态 MCP Resource。"""
        self.refresh_session_proof()
        MAX_RESOURCE_BYTES = 262144
        if uri == "bugx://state/status":
            mime_type = "application/json"
            try:
                payload = self._status_payload(self.get_supervisor())
            except audit_supervisor.NoRunError:
                payload = {"status": "NO_RUN", "detail": "无正在运行的 Supervisor", "root": str(self.root)}
            except McpProtocolError as exc:
                payload = {"status": "NOT_INITIALIZED", "detail": str(exc), "root": str(self.root)}
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        elif uri == "bugx://state/coverage" or uri.startswith("bugx://state/coverage?"):
            mime_type = "application/json"
            try:
                st = self.get_supervisor().status()
                query = parse_qs(urlparse(uri).query)
                offset = max(0, int(query.get("offset", [0])[0]))
                limit = min(500, max(1, int(query.get("limit", [200])[0])))
                items = sorted(st.coverage.items())
                page = dict(items[offset:offset + limit])
                payload = {
                    "coverage": page,
                    "debt": st.coverage_debt,
                    "complete": st.coverage_complete,
                    "total_units": len(st.coverage),
                    "offset": offset,
                    "limit": limit,
                    "next_offset": offset + limit if offset + limit < len(items) else None,
                }
            except audit_supervisor.NoRunError:
                payload = {"status": "NO_RUN", "detail": "无正在运行的 Supervisor"}
            except (McpProtocolError, ValueError) as exc:
                payload = {"status": "NOT_INITIALIZED", "detail": str(exc)}
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        elif uri == "bugx://findings/recent":
            mime_type = "application/json"
            try:
                st = self.get_supervisor().status()
                payload = {
                    "status": "ok",
                    "findings_count": len(st.findings),
                    "findings": list(st.findings),
                }
            except audit_supervisor.NoRunError:
                payload = {"status": "NO_RUN", "detail": "无正在运行的 Supervisor"}
            except McpProtocolError as exc:
                payload = {"status": "NOT_INITIALIZED", "detail": str(exc)}
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        elif uri == "bugx://knowledge/mistakes":
            mime_type = "text/markdown"
            try:
                scope = self._scope()
                shared = knowledge_sources.retrieve_patterns(
                    self._knowledge(), scope.knowledge_seed,
                    scope.global_knowledge_seed, limit=10, runtime_only=True,
                )
                if scope.mistake_book.is_file():
                    text = scope.mistake_book.read_text(encoding="utf-8")
                else:
                    text = "# BugX Mistakes & Anti-Patterns\n\n暂无错题记录。"
                global_lessons = [
                    item for item in shared
                    if item.get("knowledge_source") == "global"
                ]
                if global_lessons:
                    lines = [text.rstrip(), "", "# Shared Active Knowledge", ""]
                    for l in global_lessons:
                        title = l.get("title") or l.get("pattern_id") or "Lesson"
                        lines.append(f"## {title}")
                        if l.get("reflection"):
                            lines.append(f"{l.get('reflection')}\n")
                    text = "\n".join(lines)
                elif not scope.mistake_book.is_file() and shared:
                    lines = ["# BugX Mistakes & Anti-Patterns", ""]
                    for l in shared:
                        title = l.get("title") or l.get("pattern_id") or "Lesson"
                        lines.append(f"## {title}")
                        if l.get("reflection"):
                            lines.append(f"{l.get('reflection')}\n")
                    text = "\n".join(lines)
            except Exception:
                text = "# BugX Mistakes & Anti-Patterns\n\n暂无错题记录。"
        elif uri == "bugx://profile/target":
            mime_type = "application/json"
            try:
                from core.runtime import project_profile
                profile = None
                try:
                    profile = project_profile.load_project_profile(self._scope().knowledge_seed)
                except Exception:
                    profile = None
                if not profile:
                    profile = project_profile.detect_project_profile(self.root)
                text = json.dumps(profile, ensure_ascii=False, indent=2)
            except Exception as exc:
                text = json.dumps({"error": f"无法生成目标项目画像: {exc}"}, ensure_ascii=False, indent=2)
        else:
            raise McpProtocolError(f"未知或非法的 Resource URI: {uri}", code=-32602)

        encoded = text.encode("utf-8")
        if len(encoded) > MAX_RESOURCE_BYTES:
            text = encoded[:MAX_RESOURCE_BYTES].decode("utf-8", errors="ignore") + "\n... [truncated]"

        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": mime_type,
                    "text": text,
                }
            ]
        }

    def get_prompt(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """按阶段装配标准 MCP Prompt 工作流消息。"""
        prompt_map = {
            "bugx-audit-explore": ("explore", _i18n_prompt("mcp.explore_title", "BugX 勘察与代码单元初筛")),
            "bugx-fuzz-mutate": ("mutate", _i18n_prompt("mcp.mutate_title", "BugX 模糊测试与状态变异挖掘")),
            "bugx-repro-verify": ("repair", _i18n_prompt("mcp.repair_title", "BugX 最小复现与差分修复")),
            "bugx-gate-decision": ("gate", _i18n_prompt("mcp.gate_title", "BugX 会话启动与门禁裁决")),
        }
        if name not in prompt_map:
            raise McpProtocolError(_i18n_exc("mcp.unknown_prompt_template", f"未知 Prompt 模版: {name}", name=name), code=-32602)
        stage, desc = prompt_map[name]
        unit_id = arguments.get("unit_id")
        rule_name = arguments.get("rule")
        context = arguments.get("context")
        if context is not None and not isinstance(context, Mapping):
            raise McpProtocolError(_i18n_exc("mcp.context_must_be_object", "context 必须是对象"), code=-32602)
        from core.planning import prompt_harness
        try:
            content = prompt_harness.get_stage_prompt(stage, context=context, root=self.root)
        except (ValueError, FileNotFoundError) as exc:
            raise McpProtocolError(str(exc), code=-32602)

        rule_slice = None
        if unit_id and isinstance(unit_id, str):
            rule_slice = prompt_harness.detect_unit_rule_slice(unit_id, root=self.root)
        elif rule_name and isinstance(rule_name, str):
            try:
                rule_content = prompt_harness.load_rule(rule_name, root=self.root)
                rule_slice = f"【专项规则 - {rule_name}】\n" + rule_content
            except (ValueError, FileNotFoundError):
                rule_slice = None

        if rule_slice:
            content = f"{content}\n\n{rule_slice}"

        return {
            "description": desc,
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": content,
                    },
                }
            ],
        }




try:
    import core.mcp_handlers.resource_prompt_table as _rpt
    import core.mcp_handlers.tool_table as _tt
    import core.mcp_handlers.rpc_server as _rpc
except ImportError:
    import core.mcp_handlers.resource_prompt_table as _rpt
    import core.mcp_handlers.tool_table as _tt
    import core.mcp_handlers.rpc_server as _rpc

_resource_definitions = _rpt._resource_definitions
_prompt_definitions = _rpt._prompt_definitions

_build_tool_table = _tt._build_tool_table
_assert_tool_registry_consistent = _tt._assert_tool_registry_consistent
_tool_definitions = _tt._tool_definitions
_TOOL_TABLE = _tt._TOOL_TABLE
_TOOL_ROLES = _tt._TOOL_ROLES
_WORKER_CORE_TOOL_NAMES = _tt._WORKER_CORE_TOOL_NAMES
HOST_TOOLS = _tt.HOST_TOOLS
WORKER_TOOLS = _tt.WORKER_TOOLS
READ_ONLY_TOOLS = _tt.READ_ONLY_TOOLS

_response = _rpc._response
_rpc_error = _rpc._rpc_error
_line_is_complete = _rpc._line_is_complete
_safe_tool_call_summary = _rpc._safe_tool_call_summary
serve = _rpc.serve
_resolve_serve_root = _rpc._resolve_serve_root
main = _rpc.main

_assert_tool_registry_consistent()

if __name__ == "__main__":
    raise SystemExit(main())
