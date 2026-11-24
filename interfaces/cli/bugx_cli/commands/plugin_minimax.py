#!/usr/bin/env python3
"""MiniMax Code Plugin V1 installer for BugX.

This module owns the MiniMax package contract. It deliberately does not reuse
Kimi's manifest, registry or icon URL: MiniMax discovers a package directory
under <DATA_DIR>/plugins and validates .minimax-plugin/plugin.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..common import (
    CliError,
    _resolve_locale_name,
    asset_root,
    global_engine_root,
)
from ..hosts.template_engine import (
    add_locale_marker,
    get_agent_description,
    get_worker_description,
)

_PLUGIN_NAME = "bugx"
_SCHEMA_VERSION = 1
_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def _minimax_data_dir() -> Path:
    """Return MiniMax's data directory; the env override is BugX's test hook."""
    configured = os.environ.get("MINIMAX_DATA_DIR")
    if configured is not None:
        if not configured.strip():
            raise CliError("MINIMAX_DATA_DIR 不能为空。")
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".minimax").resolve()


def _minimax_plugin_root() -> Path:
    return _minimax_data_dir() / "plugins" / _PLUGIN_NAME


def _find_bugx_skill_source(root: Path, source: Path) -> Path | None:
    candidates = (
        source / "skills" / "bugx" / "SKILL.md",
        source / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        source / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        source.parent / "skills" / "bugx" / "SKILL.md",
        source.parent / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        source.parent / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        root / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        root / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        global_engine_root() / "skills" / "bugx" / "SKILL.md",
    )
    for candidate in candidates:
        if not candidate.is_symlink() and candidate.is_file():
            return candidate
    return None


def _find_bugx_avatar_source() -> Path | None:
    candidates = (
        asset_root() / "bugx-avatar-512.png",
        asset_root() / "assets" / "bugx-avatar-512.png",
        global_engine_root() / "assets" / "bugx-avatar-512.png",
        Path(__file__).resolve().parents[4] / "core" / "assets" / "bugx-avatar-512.png",
    )
    for candidate in candidates:
        if not candidate.is_symlink() and candidate.is_file():
            return candidate
    return None


def _build_main_agent(locale: str | None = None) -> str:
    loc = _resolve_locale_name(locale)
    desc = get_agent_description(loc)
    is_zh = loc.startswith("zh")
    body = (
        "# BugX\n\n"
        "启动前请先阅读 `bugx engine path --name bugx-runtime-contract.md` 与\n"
        "`bugx engine path --name bugx-quick.md`。使用 BugX Supervisor\n"
        "MCP 工具管理生命周期、证据、检查点、发现问题与最终报告。\n\n"
        "本 Agent 为 MiniMax Code 宿主入口，通过已声明的 MCP 服务委托\n"
        "BugX 运行时执行；不得绕开 Supervisor 证据机制或捏造第二生命周期。\n"
    ) if is_zh else (
        "# BugX\n\n"
        "Read bugx engine path --name bugx-runtime-contract.md and\n"
        "bugx engine path --name bugx-quick.md before starting. Use the BugX supervisor\n"
        "MCP tools for lifecycle, evidence, checkpoints, findings and final reporting.\n\n"
        "This Agent is the MiniMax Code host entry. It delegates execution to the\n"
        "BugX runtime through the declared MCP servers; it does not invent a second\n"
        "lifecycle or bypass supervisor evidence.\n"
    )
    rendered = (
        "---\n"
        "name: bugx\n"
        f"description: {desc}\n"
        "tools:\n"
        "  - read\n"
        "  - write\n"
        "  - edit\n"
        "  - bash\n"
        "  - grep\n"
        "  - glob\n"
        "  - web_fetch\n"
        "mcpServers:\n"
        "  - bugx-supervisor\n"
        "  - bugx-docs\n"
        "skills:\n"
        "  - bugx\n"
        "x-mavis:\n"
        "  displayName: BugX\n"
        "---\n\n"
        + body
    )
    return add_locale_marker(rendered, loc)


def _build_worker_agent(locale: str | None = None) -> str:
    loc = _resolve_locale_name(locale)
    desc = get_worker_description(loc)
    is_zh = loc.startswith("zh")
    body = (
        "# BugX Worker\n\n"
        "严格遵守分配的任务包（claim）与租约。通过 bugx-supervisor-worker\n"
        "发送心跳与检查点，为确认的缺陷附加可复现证据，不得委托给其他 Agent。\n"
    ) if is_zh else (
        "# BugX Worker\n\n"
        "Obey the assigned claim and lease. Send heartbeats and checkpoints through\n"
        "bugx-supervisor-worker, attach reproducible evidence to confirmed findings,\n"
        "and do not delegate to another Agent.\n"
    )
    rendered = (
        "---\n"
        "name: bugx-worker\n"
        f"description: {desc}\n"
        "tools:\n"
        "  - read\n"
        "  - write\n"
        "  - edit\n"
        "  - bash\n"
        "  - grep\n"
        "  - glob\n"
        "mcpServers:\n"
        "  - bugx-supervisor-worker\n"
        "skills:\n"
        "  - bugx\n"
        "x-mavis:\n"
        "  displayName: BugX Worker\n"
        "---\n\n"
        + body
    )
    return add_locale_marker(rendered, loc)


def _build_plugin_manifest(version: str = "0.1.3", locale: str | None = None) -> dict[str, Any]:
    loc = _resolve_locale_name(locale)
    desc = get_agent_description(loc)
    return {
        "$schema": _PLUGIN_SCHEMA,
        "schemaVersion": _SCHEMA_VERSION,
        "name": _PLUGIN_NAME,
        "displayName": "BugX",
        "version": version,
        "description": desc,
        "author": {"name": "BugX"},
        "icon": "icon.png",
        "category": "Code",
        "exampleQueries": [
            "对当前项目执行自主代码审查，找出深层 Bug",
            "为当前项目执行自动化测试并提交可复现证据",
        ],
        "apps": [],
        "mcpServers": ["servers.mcp.json"],
        "skills": ["skills/bugx/SKILL.md"],
        "agents": ["agents/bugx.md", "agents/bugx-worker.md"],
    }


def _server(command_args: list[str]) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": "bugx",
        "args": command_args,
    }


def _build_mcp_manifest() -> dict[str, Any]:
    return {
        "$schema": _MCP_SCHEMA,
        "mcpServers": {
            "bugx-supervisor": _server(
                ["mcp", "serve", "--root", ".", "--role", "host"]
            ),
            "bugx-supervisor-worker": _server(
                ["mcp", "serve", "--root", ".", "--role", "worker"]
            ),
            "bugx-docs": _server(
                [
                    "mcp", "docs", "serve", "--root", ".", "--db",
                    ".bugx/knowledge/docs.sqlite3"
                ]
            ),
        },
    }


def _write_owned_file(
    path: Path,
    payload: bytes,
    written: list[dict[str, Any]],
) -> None:
    if path.is_symlink():
        raise CliError(f"MiniMax 插件路径禁止符号链接: {path}")
    if path.exists() and not path.is_file():
        raise CliError(f"MiniMax 插件目标不是普通文件: {path}")
    status = "created"
    if path.is_file():
        if path.read_bytes() == payload:
            status = "unchanged"
        else:
            status = "updated"
    if status != "unchanged":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    written.append({
        "path": str(path),
        "status": status,
        "backup": None,
        "created": status == "created",
    })


def _validate_existing_package(dest: Path) -> None:
    if dest.is_symlink():
        raise CliError(f"MiniMax 插件目录禁止符号链接: {dest}")
    if not dest.exists():
        return
    if not dest.is_dir():
        raise CliError(f"MiniMax 插件目标不是目录: {dest}")
    manifest = dest / ".minimax-plugin" / "plugin.json"
    if not manifest.exists():
        if any(dest.iterdir()):
            raise CliError(
                f"MiniMax 插件目录已有未知内容，拒绝覆盖: {dest}"
            )
        return
    if manifest.is_symlink() or not manifest.is_file():
        raise CliError(f"MiniMax 插件 manifest 无效: {manifest}")
    try:
        existing = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(f"MiniMax 插件 manifest 无法解析，已阻断覆盖: {manifest}") from exc
    if not isinstance(existing, dict) or existing.get("name") != _PLUGIN_NAME:
        raise CliError(f"MiniMax 插件目录属于其他插件，拒绝覆盖: {dest}")

    entries = list(dest.rglob("*"))
    if len(entries) > 2048:
        raise CliError(f"MiniMax 插件目录条目超过 V1 上限: {dest}")
    total_bytes = 0
    for entry in entries:
        if entry.is_symlink():
            raise CliError(f"MiniMax 插件包禁止符号链接: {entry}")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise CliError(f"MiniMax 插件包包含特殊文件: {entry}")
        stat_result = entry.stat()
        if stat_result.st_nlink > 1:
            raise CliError(f"MiniMax 插件包禁止硬链接文件: {entry}")
        if stat_result.st_size > 16 * 1024 * 1024:
            raise CliError(f"MiniMax 插件单文件超过 V1 上限: {entry}")
        total_bytes += stat_result.st_size
        relative = entry.relative_to(dest).as_posix()
        try:
            relative.encode("ascii")
        except UnicodeEncodeError as exc:
            raise CliError(f"MiniMax 插件路径必须是 ASCII: {entry}") from exc
        if len(relative.encode("ascii")) > 512 or len(relative.split("/")) > 16:
            raise CliError(f"MiniMax 插件路径超过 V1 上限: {entry}")
    if total_bytes > 64 * 1024 * 1024:
        raise CliError(f"MiniMax 插件包总大小超过 V1 上限: {dest}")


def _ensure_minimax_plugin(
    root: Path,
    source: Path,
    *,
    locale: str | None = None,
) -> dict[str, Any]:
    """Install the complete MiniMax Plugin V1 package and return an audit receipt."""
    loc = _resolve_locale_name(locale)
    data_dir = _minimax_data_dir()
    if not data_dir.is_dir():
        raise CliError(
            f"未找到 MiniMax Code 数据目录: {data_dir}。"
            "请先启动一次 MiniMax Code，或设置有效的 MINIMAX_DATA_DIR。"
        )

    skill_src = _find_bugx_skill_source(root, source)
    if skill_src is None:
        raise CliError("未找到 BugX SKILL.md，已阻断 MiniMax 插件安装。")
    avatar_src = _find_bugx_avatar_source()
    if avatar_src is None:
        raise CliError("未找到 BugX PNG 图标，已阻断 MiniMax 插件安装。")

    dest = _minimax_plugin_root()
    _validate_existing_package(dest)
    dest.mkdir(parents=True, exist_ok=True)

    manifest_bytes = (
        json.dumps(_build_plugin_manifest(locale=loc), indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    mcp_bytes = (
        json.dumps(_build_mcp_manifest(), indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    avatar_bytes = avatar_src.read_bytes()
    written: list[dict[str, Any]] = []

    _write_owned_file(dest / ".minimax-plugin" / "plugin.json", manifest_bytes, written)
    _write_owned_file(dest / "servers.mcp.json", mcp_bytes, written)
    _write_owned_file(dest / "icon.png", avatar_bytes, written)
    _write_owned_file(
        dest / "agents" / "bugx.md",
        _build_main_agent(locale=loc).encode("utf-8"),
        written,
    )
    _write_owned_file(
        dest / "agents" / "bugx-worker.md",
        _build_worker_agent(locale=loc).encode("utf-8"),
        written,
    )
    _write_owned_file(
        dest / "skills" / "bugx" / "SKILL.md",
        skill_src.read_bytes(),
        written,
    )
    _validate_existing_package(dest)

    changed = any(item["status"] != "unchanged" for item in written)
    return {
        "status": "updated" if changed else "unchanged",
        "dir": str(dest),
        "home": str(data_dir),
        "manifest_path": str(dest / ".minimax-plugin" / "plugin.json"),
        "mcp_path": str(dest / "servers.mcp.json"),
        "agent_path": str(dest / "agents" / "bugx.md"),
        "worker_agent_path": str(dest / "agents" / "bugx-worker.md"),
        "written": written,
        "backups": [],
    }
