#!/usr/bin/env python3
"""core/host_runtime/mcp_process_tracker.py - MCP 进程探活与会话观测追踪。"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from core.infra.client_metadata import client_version
from pathlib import Path
from collections.abc import Iterable, Mapping

from core.host_runtime.mcp_registry_gate import mcp_entry
from core.host_runtime.adapters import HostAdapter, host_adapter

logger = logging.getLogger(__name__)

BUGX_SUPERVISOR_MCP = "bugx-supervisor"
MCP_SESSION_PROOF_ENV = "BUGX_MCP_SESSION_PROOF"
MCP_SESSION_PROOF_MAX_BYTES = 64 * 1024
MCP_SESSION_PROOF_FILE_PREFIX = "mcp-session-proof-"

def _config_candidates(root: Path, adapter: HostAdapter) -> tuple[Path, ...]:
    return adapter.mcp_config_paths(root)


def _read_mcp_document(path: Path, *, require_nested_mcp: bool = False) -> dict:
    try:
        if not path.is_file():
            return {}
        if path.suffix == ".toml":
            import tomllib
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        if require_nested_mcp and {"plugin", "permission", "tools", "agent"}.intersection(data):
            return {}
        for key in ("mcp", "mcpServers", "mcp_servers"):
            entries = data.get(key)
            if isinstance(entries, dict):
                # Host server maps are read directly from their selected file.
                nested = entries.get("servers")
                if isinstance(nested, dict):
                    if key == "mcp" and require_nested_mcp and any(
                        isinstance(server, Mapping) and "enabled" in server
                        for server in nested.values()
                    ):
                        # OpenCode v2 uses `disabled`; retired v1 server flags
                        # must not satisfy the current-session preflight.
                        return {}
                    return nested
                if key == "mcp" and require_nested_mcp:
                    # OpenCode v2 requires mcp.servers. A flat mcp object is
                    # the retired v1 shape and must not satisfy host readiness.
                    continue
                return entries
        # Claude settings can list enabled .mcp.json servers without copying
        # their definitions.  This proves registration only, not loading.
        enabled = data.get("enabledMcpjsonServers")
        if isinstance(enabled, list):
            return {name: {"enabled": True} for name in enabled if isinstance(name, str)}
    except (OSError, UnicodeError, ValueError, TypeError):
        pass
    return {}


def _read_mcp_config(root: Path, adapter: HostAdapter) -> dict:
    """Read and merge host-native MCP registration formats."""
    merged: dict[str, dict] = {}
    for path in _config_candidates(root, adapter):
        for name, value in _read_mcp_document(
            path, require_nested_mcp=adapter.name == "opencode",
        ).items():
            if isinstance(value, dict):
                current = merged.setdefault(name, {})
                current.update(value)
            else:
                merged[name] = {"value": value}
    return merged


def _set_from(value: Iterable[str] | Mapping[str, object] | None, name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        result = value.get(name)
        return result if isinstance(result, bool) else bool(result) if result is not None else False
    return name in set(value)


def _pid_is_alive(pid: object) -> bool:
    """Return whether a proof-producing MCP process is still alive."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is a POSIX probe.  On Windows it raises
        # WinError 87 even for a live process, which made every real MCP
        # proof look stale to the ACP/headless gate.
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: process exists
            try:
                exit_code = ctypes.c_uint32()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) \
                    and exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # The process exists but this user cannot signal it; that is still a
        # valid liveness observation, not proof failure.
        return True
    except (OSError, ValueError):
        return False
    return True


def _mcp_process_matches_proof(record: Mapping[str, object]) -> bool:
    """Reject a reused PID when the platform exposes a start token."""
    token = record.get("process_start_token")
    if token in (None, ""):
        return True
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    try:
        from core.skills import mcp_session_proof
        current = mcp_session_proof.process_start_token(pid)
        # 跨格式比较：写入方（MCP 服务）与校验方可能是不同代际引擎，macOS 上
        # ``ps`` 被宿主沙箱禁用时会分别写下 ``ps:`` / ``libproc:``。只比字符串会把
        # 同一个进程误判为 PID 复用，反而让门禁永久要求重启宿主。
        return mcp_session_proof.start_tokens_match(token, current)
    except (ImportError, OSError, ValueError):
        return False


def _merge_mcp_observation(
    observations: dict[str, dict[str, object]],
    name: str,
    record: Mapping[str, object],
) -> None:
    """Merge one bounded, already-validated MCP session record."""
    for key in ("loaded", "callable", "healthy"):
        value = record.get(key)
        if isinstance(value, bool):
            observations[key][name] = value
    tools = record.get("tool_names", record.get("tools"))
    if isinstance(tools, (list, tuple, set, frozenset)):
        observations["tool_names"][name] = [
            str(item)[:256] for item in list(tools)[:256]
            if isinstance(item, str) and item
        ]


def _current_mcp_server_version() -> str | None:
    """Return the version declared by this engine, or None if unavailable."""
    metadata = Path(__file__).resolve().parent / "metadata.txt"
    if not metadata.is_file():
        metadata = Path(__file__).resolve().parent.parent / "metadata.txt"
    if not metadata.is_file():
        return None
    version = client_version(metadata)
    return version if version and version != "unknown" else None


def _mcp_record_matches_current_engine(
    root: Path | None, record: Mapping[str, object],
) -> bool:
    expected_version = _current_mcp_server_version()
    if expected_version is None:
        return False
    if record.get("schema_version") == 2:
        if record.get("engine_build") != expected_version:
            return False
    elif (record.get("server") != BUGX_SUPERVISOR_MCP
          or record.get("server_version") != expected_version):
        return False
    return root is None or record.get("root") == str(root.resolve())


def _file_mcp_session_observations(root: Path) -> dict[str, object]:
    """Read proof emitted by the live local MCP stdio servers.

    The MCP server writes one atomic record per role after a successful
    ``tools/list``. The record is only an observation receipt; liveness is
    checked here so a stale file cannot masquerade as a loaded session.
    """
    try:
        import core.bugx_paths as bugx_paths

        state_root = bugx_paths.require(root).state_root
    except (ImportError, OSError, RuntimeError, ValueError):
        return {}
    observations: dict[str, dict[str, object]] = {
        "loaded": {}, "callable": {}, "healthy": {}, "tool_names": {},
    }
    for role, name in (("host", "bugx-supervisor"),
                       ("worker", "bugx-supervisor-worker")):
        path = state_root / f"{MCP_SESSION_PROOF_FILE_PREFIX}{role}.json"
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > MCP_SESSION_PROOF_MAX_BYTES:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        if not _mcp_record_matches_current_engine(root, payload):
            continue
        if (payload.get("role") != role
                or not _pid_is_alive(payload.get("pid"))
                or not _mcp_process_matches_proof(payload)):
            continue
        _merge_mcp_observation(observations, name, payload)
    # DOC-231 A9: schema-2 proofs are scoped by UUID, so concurrent host/worker
    # connections cannot overwrite one another.  Keep only bounded instance
    # records and let the target phase/liveness decide whether they are usable.
    sessions_root = state_root / "mcp-sessions"
    try:
        instance_dirs = [
            path for path in sessions_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        ][:32]
    except OSError:
        instance_dirs = []
    from core.skills import mcp_session_proof
    for instance_dir in instance_dirs:
        proof_path = instance_dir / "proof.json"
        try:
            if mcp_session_proof is None:
                continue
            payload = mcp_session_proof.read_proof(
                proof_path, expected_root=root, now=time.time(),
            )
        except (OSError, ValueError, RuntimeError):
            continue
        if not _mcp_record_matches_current_engine(root, payload):
            continue
        role = payload.get("role")
        name = "bugx-supervisor" if role == "host" else "bugx-supervisor-worker"
        if (role not in {"host", "worker"}
                or not _pid_is_alive(payload.get("pid"))
                or not _mcp_process_matches_proof(payload)):
            continue
        if payload.get("phase") == "CLOSED":
            continue
        _merge_mcp_observation(observations, name, {
            **payload,
            "loaded": True,
            "callable": payload.get("phase") == "ACTIVE",
            "healthy": payload.get("phase") == "ACTIVE",
        })
    return observations if any(observations[key] for key in observations) else {}


def mcp_session_observations(root: Path | None = None) -> dict[str, object]:
    """Read the host's current-session MCP receipt.

    Registration in ``opencode.json`` proves only that a server is configured.
    The host/agent must call the MCP and pass a bounded receipt for the current
    session before automatic review can be enabled. A live local MCP service
    may publish the same receipt after its real ``tools/list`` handshake so a
    CLI child launched by ACP can consume it without spawning a second server.
    This is an observation receipt, not an authentication mechanism; the MCP
    server remains the authority for role and project checks.
    """
    raw = os.environ.get(MCP_SESSION_PROOF_ENV, "")
    if not raw:
        return _file_mcp_session_observations(root) if root is not None else {}
    if len(raw.encode("utf-8", errors="replace")) > MCP_SESSION_PROOF_MAX_BYTES:
        return {"error": "MCP 会话证明超过大小限制"}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"error": "MCP 会话证明不是有效 JSON"}
    if not isinstance(payload, Mapping):
        return {"error": "MCP 会话证明必须是对象"}
    observations: dict[str, dict[str, object]] = {
        "loaded": {}, "callable": {}, "healthy": {}, "tool_names": {},
    }
    for name, record in payload.items():
        if not isinstance(name, str) or len(name) > 128 or not isinstance(record, Mapping):
            continue
        if not _mcp_record_matches_current_engine(root, record):
            continue
        for key in ("loaded", "callable", "healthy"):
            value = record.get(key)
            if isinstance(value, bool):
                observations[key][name] = value
        tools = record.get("tool_names", record.get("tools"))
        if isinstance(tools, (list, tuple, set, frozenset)):
            bounded = [str(item)[:256] for item in list(tools)[:256]
                       if isinstance(item, str) and item]
            observations["tool_names"][name] = bounded
    if observations == {"loaded": {}, "callable": {}, "healthy": {}, "tool_names": {}}:
        if root is not None:
            return _file_mcp_session_observations(root)
        return {}
    return observations


def _process_cwd(pid: int) -> Path | None:
    """Read a process working directory without following user commands."""
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    if proc_cwd.is_symlink():
        try:
            return Path(os.readlink(proc_cwd)).resolve()
        except (OSError, RuntimeError):
            return None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=0.5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in (result.stdout or "").splitlines():
            if line.startswith("n") and line[1:]:
                try:
                    return Path(line[1:]).resolve()
                except (OSError, RuntimeError):
                    return None
    return None


def _process_list() -> list[tuple[int, str]]:
    """Return a bounded process list for the read-only MCP liveness probe."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=1, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    processes: list[tuple[int, str]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        processes.append((pid, parts[2]))
    return processes


def _configured_server_command(cfg: Mapping[str, object]) -> list[str]:
    command = cfg.get("command")
    if isinstance(command, (list, tuple)):
        argv = [item for item in command if isinstance(item, str) and item]
    elif isinstance(command, str) and command.strip():
        try:
            argv = shlex.split(command)
        except ValueError:
            return []
    else:
        return []

    args = cfg.get("args")
    if isinstance(args, (list, tuple)):
        argv.extend(item for item in args if isinstance(item, str) and item)
    elif isinstance(args, str):
        try:
            argv.extend(shlex.split(args))
        except ValueError:
            return []
    elif args is not None:
        return []
    return argv


def _same_mcp_command(process_command: str, configured: list[str]) -> list[str] | None:
    """Match the configured argv inside a process command line.

    The process may be prefixed by a Python/Node interpreter, so matching is
    anchored at the configured executable basename and then compares the
    remaining arguments exactly.
    """
    if not configured:
        return None
    try:
        actual = shlex.split(process_command)
    except ValueError:
        return None
    executable = Path(configured[0]).name.casefold()
    for index, token in enumerate(actual):
        if Path(token).name.casefold() != executable:
            continue
        candidate = actual[index:index + len(configured)]
        if (len(candidate) == len(configured)
                and index + len(configured) == len(actual)
                and candidate[1:] == configured[1:]):
            return candidate
    return None


def _process_matches_root(pid: int, command: list[str], root: Path) -> bool:
    try:
        root = root.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        root_index = command.index("--root")
        configured_root = command[root_index + 1]
    except (ValueError, IndexError):
        return False
    if configured_root == ".":
        cwd = _process_cwd(pid)
        try:
            return cwd is not None and cwd.resolve() == root
        except (OSError, RuntimeError):
            return False
    candidate = Path(configured_root).expanduser()
    if not candidate.is_absolute():
        cwd = _process_cwd(pid)
        if cwd is None:
            return False
        candidate = cwd / candidate
    try:
        return candidate.resolve() == root
    except (OSError, RuntimeError):
        return False


def active_mcp_process_probe(
    root: Path,
    mcp_config: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Probe configured BugX MCP server processes for this exact project.

    This is a weak, read-only diagnostic.  It avoids a false cross-project hit
    by requiring the configured command, host/worker role and process cwd to
    all match, but it is never used as the current host session proof.  It does
    not start a server or modify state.
    """
    required_names = ("bugx-supervisor", "bugx-supervisor-worker")
    if any(
        not isinstance(mcp_config.get(name), Mapping)
        or mcp_config[name].get("enabled", True) is False
        for name in required_names
    ):
        # There is no valid two-channel receipt to discover when either
        # registration is absent/disabled.  Avoid even spawning ``ps`` on the
        # missing-MCP installation path.
        return {}
    processes = _process_list()
    if not processes:
        return {}
    loaded: dict[str, bool] = {}
    pids: dict[str, int] = {}
    for name, role in (("bugx-supervisor", "host"),
                       ("bugx-supervisor-worker", "worker")):
        cfg = mcp_config.get(name)
        configured = _configured_server_command(cfg)
        for pid, process_command in processes:
            actual = _same_mcp_command(process_command, configured)
            if actual is None:
                continue
            try:
                role_index = actual.index("--role")
                if actual[role_index + 1] != role:
                    continue
            except (ValueError, IndexError):
                continue
            if not _process_matches_root(pid, actual, root):
                continue
            loaded[name] = True
            pids[name] = pid
            break
    # Automatic MCP review requires both host and worker channels.  Do not
    # feed a partial mapping into the normal capability reducer: its missing
    # keys intentionally mean ``False`` and would turn a partial probe into a
    # misleading degraded state.  A partial process set therefore remains a
    # stale session and keeps the existing gate.
    if set(loaded) != {"bugx-supervisor", "bugx-supervisor-worker"}:
        return {}
    return {
        "loaded": dict(loaded),
        "callable": dict(loaded),
        "healthy": dict(loaded),
        "source": "process-liveness",
        "pids": pids,
    }


