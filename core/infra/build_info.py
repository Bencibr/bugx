#!/usr/bin/env python3
"""BugX 构建标识与自增 Build ID 管理模块 (DOC-169).

语义版本号从 ``BUGX_VERSION`` 解析；源码模板只在构建 staging 时物化。
日常开发与提交通过单调自增、从 1 开始无上限递增的 Build ID 进行版本追踪。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

VERSION_ENV = "BUGX_VERSION"
VERSION_TOKEN = "0.1.5"
_DEFAULT_VERSION = "0.1.5"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def resolve_base_version(value: str | None = None) -> str:
    """Resolve the base version from ``BUGX_VERSION`` with one source fallback."""
    candidate = (value or os.environ.get(VERSION_ENV) or _DEFAULT_VERSION).strip()
    if candidate == VERSION_TOKEN:
        candidate = os.environ.get(VERSION_ENV, _DEFAULT_VERSION).strip()
    if not _VERSION_RE.fullmatch(candidate):
        raise ValueError(f"invalid BugX version: {candidate!r}")
    return candidate


BASE_VERSION = resolve_base_version()
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


def is_dev_mode(root: Path | None = None) -> bool:
    """是否处于 dev / alpha 开发模式。正式版返回 False。"""
    if os.environ.get("BUGX_DEV") in ("1", "true", "yes", "on"):
        return True
    env = os.environ.get("BUGX_ENV", "").strip().lower()
    if env in ("dev", "development", "local", "test"):
        return True
    meta_path = _resolve_metadata_path(root)
    if meta_path.is_file():
        try:
            content = meta_path.read_text(encoding="utf-8")
            m_chan = re.search(r"^bugx_channel:\s*[\"']?([A-Za-z0-9_-]+)", content, re.M)
            m_mark = re.search(r"^bugx_marker:\s*[\"']?([A-Za-z0-9_-]+)", content, re.M)
            channel = m_chan.group(1).lower() if m_chan else "stable"
            marker = m_mark.group(1).lower() if m_mark else "stable"
            if channel != "stable" or marker in ("dev", "alpha"):
                return True
        except OSError:
            pass
    return False


def get_full_version_string(root: Path | None = None) -> str:
    """返回标准版本展示字符串。正式版（Stable）不带 build ID；仅 dev 模式携带。"""
    if is_dev_mode(root):
        bid = get_build_id(root)
        return f"{BASE_VERSION}+build.{bid}"
    return BASE_VERSION


__all__ = [
    "BASE_VERSION",
    "VERSION_ENV",
    "VERSION_TOKEN",
    "bump_build_id",
    "get_build_id",
    "get_full_version_string",
    "is_dev_mode",
    "resolve_base_version",
]
