#!/usr/bin/env python3
"""Project fingerprint calculation and cache path validation."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    import core.bugx_paths as bugx_paths
    import core.secure_atomic as secure_atomic
except ImportError:  # pragma: no cover
    from .. import bugx_paths, secure_atomic  # type: ignore

from .constants import ALIASES, BUILD_FINGERPRINT_FILES, MAX_MANIFEST_BYTES


def private_cache_directory(path: Path) -> bool:
    try:
        secure_atomic.reject_symlink_components(path)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return False
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return False
        return os.name != "posix" or stat.S_IMODE(info.st_mode) == 0o700
    except (OSError, ValueError):
        return False


def profile_cache_path(cache_token: str) -> Path | None:
    try:
        cache_root = secure_atomic.reject_symlink_components(
            bugx_paths.global_cache_root()
        )
        cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not private_cache_directory(cache_root):
            return None
        return cache_root / f"project-profile-{cache_token}.json"
    except (OSError, ValueError):
        return None


def private_profile_cache(path: Path) -> bool:
    try:
        secure_atomic.reject_symlink_components(path)
        if not private_cache_directory(path.parent):
            return False
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return False
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return False
        return os.name != "posix" or stat.S_IMODE(info.st_mode) == 0o600
    except (OSError, ValueError):
        return False


def compute_build_fingerprint(root: str | os.PathLike[str]) -> str:
    """Hash a bounded set of build manifests using metadata-only stat records."""
    project_root = Path(root).resolve()
    rows: list[tuple[str, int, int]] = []
    for relative in BUILD_FINGERPRINT_FILES:
        path = project_root / relative
        try:
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
        except OSError:
            continue
        rows.append((relative, info.st_size, info.st_mtime_ns))
    digest = hashlib.sha256()
    for relative, size, mtime_ns in rows:
        digest.update(f"{relative}\0{size}\0{mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical(value: str, allowed: Iterable[str]) -> str:
    raw = str(value or "").strip().casefold()
    raw = ALIASES.get(raw, raw)
    return raw if raw in set(allowed) else "unknown"


def safe_relative(root: Path, path: Path) -> str | None:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        return resolved.relative_to(root.resolve(strict=True)).as_posix()
    except (OSError, ValueError):
        return None


def read_bounded(path: Path, limit: int = MAX_MANIFEST_BYTES) -> str:
    try:
        raw = path.read_bytes()[:limit]
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


# Compatibility aliases
_private_cache_directory = private_cache_directory
_profile_cache_path = profile_cache_path
_private_profile_cache = private_profile_cache
_canon = canon
_canonical = canonical
_safe_relative = safe_relative
_read_bounded = read_bounded
