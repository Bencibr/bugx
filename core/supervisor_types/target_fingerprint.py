#!/usr/bin/env python3
"""core/supervisor/target_fingerprint.py - 审计目标指纹、Git 状态缓存与进程活性 Token (DOC-070 / DOC-140)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import sys
from typing import Any

def _get_sm_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get('core.supervisor.supervisor_models')
    if mod and hasattr(mod, name):
        return getattr(mod, name)
    return default

from pathlib import Path
from typing import Iterable, Mapping

from core.supervisor_types.exceptions import InvalidCheckpoint

MAX_JSON_DEPTH = 32
MAX_JSON_KEYS = 5000
MAX_PROCESS_START_TOKEN_LENGTH = 1024

_MANIFEST_UNITS_CACHE: dict[str, tuple[float, bytes]] = {}
_GIT_HEAD_CACHE: dict[str, tuple[float, str, str | None]] = {}
_GIT_HEAD_TTL_SECONDS = 2.0


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _boot_time_token() -> str | None:
    """Return a stable boot identity, or None when it cannot be proven."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            get_tick_count64 = kernel32.GetTickCount64
            get_tick_count64.restype = ctypes.c_ulonglong
            return f"uptime_ms:{int(get_tick_count64())}"
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True, text=True, check=False, timeout=2,
            )
            match = re.search(r"\bsec\s*=\s*(\d+)", result.stdout)
            if result.returncode == 0 and match:
                return f"epoch:{int(match.group(1))}"
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if not line.startswith("btime "):
                continue
            value = int(line.split()[1])
            if value > 0:
                return f"epoch:{value}"
            break
    except (OSError, ValueError, IndexError):
        pass
    return None


def _process_start_timestamp(pid: int) -> str | None:
    """Return the platform process-start primitive without inventing one."""
    from core.skills import mcp_session_proof
    try:
        return mcp_session_proof.process_start_token(pid)
    except (OSError, ValueError):
        return None


def process_start_token(pid: int | None) -> str | None:
    """Return a composite PID identity, or None when any part is unproven."""
    if pid is None or pid <= 0:
        return None
    try:
        host = socket.gethostname().strip()
    except OSError:
        return None
    boot_time = _boot_time_token()
    timestamp = _process_start_timestamp(pid)
    if not host or boot_time is None or timestamp is None:
        return None
    token = json.dumps(
        {
            "boot_time": boot_time,
            "host": host,
            "pid": int(pid),
            "timestamp": timestamp,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(token) > MAX_PROCESS_START_TOKEN_LENGTH:
        return None
    return token


def default_coverage_manifest(root: Path) -> Path:
    """项目布局下清单的权威位置是 lifecycle state_root（与 launch/MCP 一致）。"""
    try:
        import core.bugx_paths as bugx_paths
        return bugx_paths.require(root).state_root / "module-coverage.md"
    except (ImportError, OSError, RuntimeError, ValueError, KeyError, TypeError):
        return root / ".bugx" / "module-coverage.md"


def _manifest_units_blob(manifest: Path) -> bytes:
    """覆盖清单的稳定 unit 集合字节（按 mtime 缓存；语义与原实现一致）。"""
    try:
        mtime = manifest.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = str(manifest)
    cached = _MANIFEST_UNITS_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        from core.coverage import module_coverage

        rows, errors = module_coverage.parse_manifest(manifest)
        if errors:
            raise ValueError("coverage manifest parse failed")
        units = sorted({module_coverage.coverage_unit_id(row) for row in rows})
        if not units:
            raise ValueError("coverage manifest has no units")
        blob = canonical_json(units)
    except (ImportError, OSError, ValueError, TypeError):
        if manifest.is_file() and not manifest.is_symlink():
            blob = manifest.read_bytes()
        else:
            blob = b"<missing>"
    _MANIFEST_UNITS_CACHE[key] = (mtime, blob)
    return blob


def invalidate_git_head_cache(root: Path | None = None) -> None:
    """清理 git HEAD 身份缓存（在 git 提交或生命周期切换后调用）。"""
    if root is None:
        _GIT_HEAD_CACHE.clear()
    else:
        _GIT_HEAD_CACHE.pop(str(root), None)
        _GIT_HEAD_CACHE.pop(str(root.resolve()), None)


def _git_ref_value(git_dir: Path, ref: str) -> str | None:
    """按 per-worktree gitdir → commondir 顺序读取 ref；都读不到返回 None。"""
    roots = [git_dir]
    try:
        common = (git_dir / "commondir").read_text(encoding="utf-8", errors="ignore").strip()
        if common:
            base = Path(common)
            roots.append(base if base.is_absolute() else git_dir / base)
    except OSError:
        pass
    for base in roots:
        try:
            return (base / ref).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
    return None


def _git_ref_token(root: Path) -> str | None:
    """HEAD 指向的 ref 内容（零子进程读取）；读不到返回 None。"""
    try:
        dot_git = root / ".git"
        git_dir = dot_git
        if dot_git.is_file():
            marker = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
            if not marker.startswith("gitdir:"):
                return None
            candidate = Path(marker.split(":", 1)[1].strip())
            git_dir = candidate if candidate.is_absolute() else (root / candidate)
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            value = _git_ref_value(git_dir, ref)
            if value is None:
                return None
            return f"{ref}={value}"
        return head
    except OSError:
        return None


def _git_head_cached(root: Path) -> str:
    """git HEAD 身份缓存：ref 内容不变即复用（子进程只在必要时启动）。"""
    now = time.monotonic()
    key = str(root)
    token = _git_ref_token(root)
    cached = _GIT_HEAD_CACHE.get(key)
    if cached is not None:
        if token is not None and cached[2] == token:
            return cached[1]
        if token is None and now - cached[0] < _GIT_HEAD_TTL_SECONDS:
            return cached[1]
    git_fn = _get_sm_attr("git_target_fingerprint", git_target_fingerprint)
    head = git_fn(root)
    _GIT_HEAD_CACHE[key] = (now, head, token)
    return head


def target_identity(root: Path) -> str:
    """跨 worktree 稳定的审计目标身份：git tree + 覆盖单元 ID。"""
    digest = hashlib.sha256()
    digest.update(b"target-identity-v1:")
    candidate = default_coverage_manifest(root)
    digest.update(_manifest_units_blob(candidate))
    digest.update(_git_head_cached(root).encode("utf-8"))
    return f"target:{digest.hexdigest()}"


def workspace_fingerprint(root: Path) -> str:
    """Bind evidence to this workspace, coverage plan and source revision."""
    digest = hashlib.sha256()
    digest.update(b"workspace-v2:")
    candidate = default_coverage_manifest(root)
    digest.update(_manifest_units_blob(candidate))
    digest.update(_git_head_cached(root).encode("utf-8"))
    return f"workspace:{digest.hexdigest()}"


def fingerprint_for_units(root: Path, units: Iterable[str]) -> str:
    """与 ``workspace_fingerprint`` 同一配方，但单元集由调用方显式给出。

    同一份「单元 ID + git HEAD」哈希，不含源码字节。需要判断内容是否变化的
    场景必须另有字节级信源（例如重扫产出的 ``inventory-diff.json``）。
    """
    digest = hashlib.sha256()
    digest.update(b"workspace-v2:")
    digest.update(canonical_json(sorted({str(unit) for unit in units})))
    digest.update(_git_head_cached(root).encode("utf-8"))
    return f"workspace:{digest.hexdigest()}"


def git_target_fingerprint(root: Path) -> str:
    """Return a bounded Git tree identity without making Git a runtime dependency."""
    parts: list[str] = []
    commands = (
        ("rev-parse", "--verify", "HEAD^{tree}"),
    )
    for command in commands:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *command],
                capture_output=True, text=False, check=False, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return "git:unavailable"
        if result.returncode != 0:
            return "git:not-a-repository"
        parts.append(hashlib.sha256(result.stdout).hexdigest())
    return "git:" + ":".join(parts)


def finite_number(value: object, name: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} 必须是有限数字")
    if integer and isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} 必须是整数")
    return int(value) if integer else float(value)


def check_depth_and_keys(value: object, depth: int = 0) -> int:
    """Bound JSON depth and total key count for checkpoint payloads."""
    if depth > MAX_JSON_DEPTH:
        raise InvalidCheckpoint(f"JSON 嵌套过深（>{MAX_JSON_DEPTH}）")
    if isinstance(value, Mapping):
        total = len(value)
        if total > MAX_JSON_KEYS:
            raise InvalidCheckpoint(f"JSON 对象键数过多（>{MAX_JSON_KEYS}）")
        for v in value.values():
            total += check_depth_and_keys(v, depth + 1)
            if total > MAX_JSON_KEYS:
                raise InvalidCheckpoint(f"JSON 总键数过多（>{MAX_JSON_KEYS}）")
        return total
    if isinstance(value, (list, tuple)):
        total = 0
        for item in value:
            total += check_depth_and_keys(item, depth + 1)
            if total > MAX_JSON_KEYS:
                raise InvalidCheckpoint(f"JSON 总键数过多（>{MAX_JSON_KEYS}）")
        return total
    return 0
