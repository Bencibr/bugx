#!/usr/bin/env python3
"""core/supervisor/path_containment.py - 工作区路径沙箱限制与 Worker 产物安全隔离 (DOC-070)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import core.secure_atomic as secure_atomic
from core.supervisor_types.exceptions import WorkspacePathError


def confined_path(workspace_root: Path, candidate: Path) -> Path:
    raw_root = Path(os.path.abspath(str(workspace_root)))
    root = workspace_root.resolve()
    path = candidate if candidate.is_absolute() else root / candidate
    raw_candidate = Path(os.path.abspath(str(candidate))) if candidate.is_absolute() \
        else raw_root / candidate
    resolved = path.resolve(strict=False)
    try:
        canonical_relative = resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(
            f"路径必须位于 workspace_root 内: {resolved} not under {root}"
        ) from exc
    try:
        raw_relative = raw_candidate.relative_to(raw_root)
    except ValueError:
        raw_relative = None
    if raw_relative is not None:
        current = raw_root
        for part in raw_relative.parts:
            current /= part
            if current.is_symlink():
                raise WorkspacePathError(f"受控状态路径不能包含符号链接: {candidate}")
    current = root
    for part in canonical_relative.parts:
        current /= part
        if current.is_symlink():
            raise WorkspacePathError(f"受控状态路径不能包含符号链接: {candidate}")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(
            f"路径必须位于 workspace_root 内: {resolved} not under {root}"
        ) from exc
    if resolved == root:
        raise WorkspacePathError("路径不能直接指向 workspace_root")
    return resolved


def has_lexical_symlink(lexical_root: Path, lexical_path: Path) -> bool:
    """Check whether lexical_path (or any parent up to lexical_root) is a symlink."""
    try:
        rel = lexical_path.relative_to(lexical_root)
    except ValueError:
        return True
    current = lexical_root
    for part in rel.parts:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    try:
        if lexical_path.is_symlink():
            return True
    except OSError:
        return True
    return False


def confined_regular_file(workspace_root: Path, raw_path: str) -> Path:
    """Resolve an artifact without allowing symlink components."""
    root = workspace_root.resolve()
    raw = Path(raw_path)
    candidate = raw if raw.is_absolute() else workspace_root / raw
    resolved = candidate.resolve(strict=False)

    try:
        canonical_relative = resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(
            f"artifact_path 必须位于 workspace_root 内: {raw_path}"
        ) from exc

    raw_root = Path(os.path.abspath(str(workspace_root)))
    lexical = Path(os.path.abspath(str(candidate)))
    try:
        raw_relative = lexical.relative_to(raw_root)
    except ValueError:
        try:
            raw_relative = lexical.relative_to(root)
        except ValueError:
            raw_relative = None

    if raw_relative is not None:
        current = raw_root if lexical.is_relative_to(raw_root) else root
        for part in raw_relative.parts:
            current /= part
            if current.is_symlink():
                raise WorkspacePathError(
                    f"artifact_path 不能包含符号链接: {raw_path}"
                )

    current = root
    for part in canonical_relative.parts:
        current /= part
        if current.is_symlink():
            raise WorkspacePathError(
                f"artifact_path 不能包含符号链接: {raw_path}"
            )

    if not resolved.is_file():
        raise WorkspacePathError(f"artifact_path 不是普通文件: {raw_path}")
    return resolved


def worker_artifact_root(workspace_root: Path, state_path: Path,
                         worker_id: str, lease_id: str) -> tuple[Path, str]:
    """为每个 lease 创建不可共享的 Worker 产物目录。"""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", worker_id.strip())[:64] or "worker"
    target = confined_path(workspace_root, state_path.parent / "workers" / slug / lease_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists() and not target.is_dir():
        raise WorkspacePathError(f"Worker artifact 根不是安全目录: {target}")
    target.mkdir(exist_ok=True)
    return target, target.relative_to(workspace_root.resolve()).as_posix()


def confined_worker_artifact(workspace_root: Path, artifact_root: str,
                             raw_path: str) -> Path:
    """只允许读取当前 Worker lease 自己的普通文件产物。"""
    allowed = confined_path(workspace_root, workspace_root / artifact_root)
    artifact = confined_regular_file(workspace_root, raw_path)
    try:
        artifact.relative_to(allowed)
    except ValueError as exc:
        raise WorkspacePathError(
            f"artifact_path 必须位于当前 Worker artifact_root: {raw_path}"
        ) from exc
    return artifact


def atomic_write_bytes(path: Path, content: bytes) -> None:
    try:
        secure_atomic.atomic_write_bytes(path, content)
    except secure_atomic.AtomicWriteError as exc:
        raise WorkspacePathError(f"拒绝通过符号链接写入: {path}: {exc}") from exc
