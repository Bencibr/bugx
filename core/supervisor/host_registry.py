#!/usr/bin/env python3
"""Multi-host membership registry, protocol compatibility, and elastic quota."""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from core import secure_atomic
from core.supervisor.supervisor_models import (
    WorkspacePathError,
    has_lexical_symlink,
)
from core.supervisor_types.exceptions import SupervisorError
from core.supervisor_types.target_fingerprint import process_start_token

SUPERVISOR_PROTOCOL_VERSION = 1
DEFAULT_HOST_TTL_SECONDS = 30.0
MAX_MULTI_HOST_PULL_BURST = 2
HOSTS_FILENAME = "hosts.json"
SUPERVISOR_LOCK_FILENAME = "supervisor.json.lock"

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]


class HostProtocolMismatch(SupervisorError):
    """Raised when a joining host uses an incompatible supervisor protocol version."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def hosts_file_path(state_root: Path) -> Path:
    return state_root / HOSTS_FILENAME


@contextmanager
def _supervisor_state_lock(state_root: Path, timeout: float = 10.0) -> Iterator[None]:
    """Acquire the same supervisor.json.lock used by AuditSupervisor."""
    lock_path = state_root / SUPERVISOR_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise WorkspacePathError(f"拒绝使用符号链接 Supervisor 锁: {lock_path}")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    deadline = time.monotonic() + max(0.0, timeout)
    with os.fdopen(fd, "a+", encoding="utf-8") as handle:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Host registry lock acquisition exceeded {timeout:.3g}s: {lock_path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass


def load_hosts_unlocked(state_root: Path) -> dict[str, dict[str, Any]]:
    path = hosts_file_path(state_root)
    if path.is_symlink() or has_lexical_symlink(state_root, path):
        raise WorkspacePathError(f"拒绝读取符号链接宿主注册表: {path}")
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    hosts = raw.get("hosts")
    if not isinstance(hosts, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for host_id, entry in hosts.items():
        if isinstance(host_id, str) and host_id.strip() and isinstance(entry, Mapping):
            result[host_id] = dict(entry)
    return result


def save_hosts_unlocked(state_root: Path, hosts: Mapping[str, Mapping[str, Any]]) -> None:
    path = hosts_file_path(state_root)
    if path.is_symlink() or has_lexical_symlink(state_root, path):
        raise WorkspacePathError(f"拒绝写入符号链接宿主注册表: {path}")
    payload = {
        "schema_version": 1,
        "protocol_version": SUPERVISOR_PROTOCOL_VERSION,
        "updated_at": time.time(),
        "hosts": {k: dict(v) for k, v in sorted(hosts.items())},
    }
    secure_atomic.atomic_write_json(path, payload)


def scavenge_hosts_unlocked(
    hosts: dict[str, dict[str, Any]],
    now: float,
) -> list[str]:
    """Mark timed-out or dead-PID hosts as offline and return newly offlined host_ids."""
    newly_offline: list[str] = []
    for host_id, entry in list(hosts.items()):
        state = str(entry.get("state") or "offline")
        if state not in {"active", "draining"}:
            continue
        ttl = entry.get("lease_ttl_seconds")
        ttl_val = float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl > 0 else DEFAULT_HOST_TTL_SECONDS
        last_hb = entry.get("last_heartbeat_at")
        expired = (
            not isinstance(last_hb, (int, float))
            or isinstance(last_hb, bool)
            or (now - float(last_hb)) > ttl_val
        )
        pid = entry.get("pid")
        pid_dead = False
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            if not _pid_alive(pid):
                pid_dead = True
            else:
                recorded_token = entry.get("process_start_token")
                if isinstance(recorded_token, str) and recorded_token:
                    current_token = process_start_token(pid)
                    if current_token and current_token != recorded_token:
                        pid_dead = True
        if expired or pid_dead:
            entry["state"] = "offline"
            entry["offline_at"] = now
            entry["offline_reason"] = "pid_dead" if pid_dead else "heartbeat_timeout"
            newly_offline.append(host_id)
    return newly_offline


def active_hosts_list(hosts: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(entry, host_id=host_id)
        for host_id, entry in sorted(hosts.items())
        if str(entry.get("state") or "") == "active"
    ]


def register_or_heartbeat_host(
    state_root: Path,
    *,
    host_id: str,
    pid: int | None = None,
    start_token: str | None = None,
    protocol_version: int = SUPERVISOR_PROTOCOL_VERSION,
    ttl_seconds: float = DEFAULT_HOST_TTL_SECONDS,
    max_concurrency: int = 4,
    host_kind: str = "mcp-host",
    entrypoint: str | None = None,
    inflight_count: int | None = None,
    active_domain: str | None = None,
    active_module: str | None = None,
    now: float | None = None,
    use_lock: bool = True,
) -> dict[str, Any]:
    """Register or renew a host entry under supervisor.json.lock."""
    if not isinstance(host_id, str) or not host_id.strip():
        raise ValueError("host_id 必须是非空字符串")

    def _op() -> dict[str, Any]:
        ts = time.time() if now is None else float(now)
        hosts = load_hosts_unlocked(state_root)
        newly_offline = scavenge_hosts_unlocked(hosts, ts)

        for other_id, other in hosts.items():
            if other_id == host_id:
                continue
            if str(other.get("state") or "") != "active":
                continue
            other_proto = other.get("protocol_version")
            if isinstance(other_proto, int) and other_proto != protocol_version:
                raise HostProtocolMismatch(
                    f"[HOST_PROTOCOL_MISMATCH] 在线宿主 {other_id} 使用协议版本 "
                    f"{other_proto}，当前宿主 {host_id} 使用协议版本 {protocol_version}；"
                    "请升级对应宿主的 bugx 引擎后再并发加入"
                )

        existing = hosts.get(host_id, {})
        resolved_pid = os.getpid() if pid is None else int(pid)
        resolved_token = start_token if start_token is not None else process_start_token(resolved_pid)
        joined_at = existing.get("joined_at")
        if not isinstance(joined_at, (int, float)) or str(existing.get("state") or "") == "offline":
            joined_at = ts
        entry: dict[str, Any] = {
            "host_id": host_id,
            "pid": resolved_pid,
            "process_start_token": resolved_token,
            "protocol_version": int(protocol_version),
            "host_kind": str(host_kind or "mcp-host"),
            "entrypoint": entrypoint,
            "state": "active",
            "joined_at": float(joined_at),
            "last_heartbeat_at": ts,
            "lease_ttl_seconds": float(max(5.0, ttl_seconds)),
            "max_concurrency": int(max(1, max_concurrency)),
            "inflight_count": (
                int(max(0, inflight_count))
                if inflight_count is not None
                else int(existing.get("inflight_count", 0) or 0)
            ),
            "active_domain": (
                active_domain
                if active_domain is not None
                else existing.get("active_domain")
            ),
            "active_module": (
                active_module
                if active_module is not None
                else existing.get("active_module")
            ),
        }
        hosts[host_id] = entry
        save_hosts_unlocked(state_root, hosts)
        actives = active_hosts_list(hosts)
        return {
            "host": entry,
            "active_hosts": actives,
            "active_host_count": len(actives),
            "newly_offline": newly_offline,
        }

    if use_lock:
        with _supervisor_state_lock(state_root):
            return _op()
    return _op()


def detach_host(
    state_root: Path,
    *,
    host_id: str,
    reason: str = "detached",
    now: float | None = None,
    use_lock: bool = True,
) -> dict[str, Any]:
    """Mark a host offline cleanly."""
    def _op() -> dict[str, Any]:
        ts = time.time() if now is None else float(now)
        hosts = load_hosts_unlocked(state_root)
        newly_offline = scavenge_hosts_unlocked(hosts, ts)
        entry = hosts.get(host_id)
        if entry is not None:
            entry["state"] = "offline"
            entry["offline_at"] = ts
            entry["offline_reason"] = reason
            entry["inflight_count"] = 0
            hosts[host_id] = entry
            save_hosts_unlocked(state_root, hosts)
        elif newly_offline:
            save_hosts_unlocked(state_root, hosts)
        actives = active_hosts_list(hosts)
        return {
            "host_id": host_id,
            "detached": entry is not None,
            "active_hosts": actives,
            "active_host_count": len(actives),
            "newly_offline": newly_offline,
        }

    if use_lock:
        with _supervisor_state_lock(state_root):
            return _op()
    return _op()


def compute_pull_quota(
    *,
    remaining_units: int,
    active_host_count: int,
    host_inflight: int,
    host_max_concurrency: int,
    max_multi_host_burst: int = MAX_MULTI_HOST_PULL_BURST,
) -> int:
    """Compute how many units a host may atomically pull right now."""
    if remaining_units <= 0:
        return 0
    avail = max(0, int(host_max_concurrency) - max(0, int(host_inflight)))
    if avail <= 0:
        return 0
    n = max(1, int(active_host_count))
    if n == 1:
        return min(avail, remaining_units)
    fair = max(1, math.ceil(remaining_units / n))
    return min(avail, fair, max(1, int(max_multi_host_burst)), remaining_units)
