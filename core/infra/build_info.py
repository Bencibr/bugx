#!/usr/bin/env python3
"""BugX 构建标识与自增 Build ID 管理模块 (DOC-169).

依据 AGENTS.md 规范：全局语义版本号严格保持 0.1.0，
日常开发与提交通过单调自增、从 1 开始无上限递增的 Build ID 进行版本追踪。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

BASE_VERSION = "0.1.0"
_BUILD_ID_RE = re.compile(r"^build_id:\s*(\d+)", re.M)


def _resolve_metadata_path(root: Path | None = None) -> Path:
    if root is not None:
        p = root.resolve() / "core" / "metadata.txt"
        if p.exists():
            return p
        alt = root.resolve() / "metadata.txt"
        if alt.exists():
            return alt
    # Default to relative to this file
    here = Path(__file__).resolve().parent
    meta = here / "metadata.txt"
    if meta.exists():
        return meta
    if (here.parent / "metadata.txt").exists():
        return here.parent / "metadata.txt"
    return here.parent.parent / "core" / "metadata.txt"


def get_build_id(root: Path | None = None) -> int:
    """获取当前 Build ID（整型正整数，默认 1）。"""
    env_id = os.environ.get("BUGX_BUILD_ID")
    if env_id:
        try:
            val = int(env_id.strip())
            if val >= 1:
                return val
        except ValueError:
            pass

    meta_path = _resolve_metadata_path(root)
    if meta_path.is_file():
        try:
            content = meta_path.read_text(encoding="utf-8")
            match = _BUILD_ID_RE.search(content)
            if match:
                return int(match.group(1))
        except OSError:
            pass
    return 1


def bump_build_id(root: Path | None = None) -> int:
    """原子递增 Build ID 并写入 metadata.txt，返回递增后的新 ID。"""
    meta_path = _resolve_metadata_path(root)
    current = get_build_id(root)
    next_id = current + 1

    if meta_path.is_file():
        content = meta_path.read_text(encoding="utf-8")
        if _BUILD_ID_RE.search(content):
            new_content = _BUILD_ID_RE.sub(f"build_id: {next_id}", content)
        else:
            new_content = content.rstrip() + f"\nbuild_id: {next_id}\n"
        meta_path.write_text(new_content, encoding="utf-8")
    return next_id


def get_full_version_string(root: Path | None = None) -> str:
    """返回标准版本与 Build ID 组合字符串，例如 0.1.0+build.1。"""
    bid = get_build_id(root)
    return f"{BASE_VERSION}+build.{bid}"


__all__ = [
    "BASE_VERSION",
    "bump_build_id",
    "get_build_id",
    "get_full_version_string",
]
