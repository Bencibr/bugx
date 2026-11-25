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

from .. import __version__
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
from ..infra.security import _assert_no_symlink_components

_PLUGIN_NAME = "bugx"
_SCHEMA_VERSION = 1
_WORKER_PATH = "agents/bugx-worker.md"
_LEGACY_MAIN_PATH = "agents/bugx.md"
_AGENT_OWNER = "<!-- Managed by BugX for MiniMax Code -->"
_AGENT_AVATAR_NAME = "bugx-avatar-512.png"
_AGENT_AVATAR_FIELD = f"  avatar: {_AGENT_AVATAR_NAME}"


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


def _minimax_custom_agent_path() -> Path:
    return _minimax_data_dir() / "agents" / _PLUGIN_NAME / "agent.md"


def _find_bugx_skill_source(root: Path, source: Path) -> Path | None:
    from ..infra.host_sync import find_bugx_skill_source

    return find_bugx_skill_source(root, source)


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
        "你是 BugX，负责在用户授权的工作区和范围内协助代码审查与测试。\n\n"
        "开始任务前，先运行下面两条命令，再读取它们返回的文件。\n\n"
        "- `bugx engine path --name bugx-runtime-contract.md`\n"
        "- `bugx engine path --name bugx-quick.md`\n\n"
        "按文档约定使用 BugX Supervisor MCP 记录进度、检查点和验证证据。"
        "如果命令、文档或所需工具不可用，请说明具体缺项。"
        "报告已验证的结果和尚待验证的范围。\n"
    ) if is_zh else (
        "You are BugX. Help review and test code within the workspace and scope authorized by the user.\n\n"
        "Before starting a task, run the two commands below and read the files at the returned paths.\n\n"
        "- `bugx engine path --name bugx-runtime-contract.md`\n"
        "- `bugx engine path --name bugx-quick.md`\n\n"
        "Follow the documented BugX Supervisor MCP workflow to record progress, checkpoints, and evidence. "
        "Identify any unavailable command, document, or required tool. "
        "Report verified results and any scope that still needs verification.\n"
    )
    return (
        "---\n"
        f"# {_AGENT_OWNER}\n"
        "name: bugx\n"
        f"description: {json.dumps(desc, ensure_ascii=False)}\n"
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
        f"{_AGENT_AVATAR_FIELD}\n"
        "---\n\n"
        + body
    )


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
        f"description: {json.dumps(desc, ensure_ascii=False)}\n"
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


def _build_plugin_manifest(locale: str | None = None) -> dict[str, Any]:
    loc = _resolve_locale_name(locale)
    desc = get_agent_description(loc)
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "name": _PLUGIN_NAME,
        "displayName": "BugX",
        "version": __version__,
        "description": desc,
        "author": "BugX",
        "icon": "icon.png",
        "category": "Code",
        "exampleQueries": [
            "对当前项目执行自主代码审查，找出深层 Bug",
            "为当前项目执行自动化测试并提交可复现证据",
        ],
        "apps": [],
        "mcpServers": ["servers.mcp.json"],
        "skills": ["skills/bugx/SKILL.md"],
        "agents": [_WORKER_PATH],
    }


def _server(command_args: list[str]) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": "bugx",
        "args": command_args,
    }


def _build_mcp_manifest() -> dict[str, Any]:
    return {
        "schemaVersion": _SCHEMA_VERSION,
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
    from .agent import _atomic_write_global_file_tx

    _assert_no_symlink_components(path)
    if path.exists() and not path.is_file():
        raise CliError(f"MiniMax 插件目标不是普通文件: {path}")
    status = "created"
    if path.is_file():
        if path.read_bytes() == payload:
            status = "unchanged"
        else:
            status = "updated"
    if status != "unchanged":
        _atomic_write_global_file_tx(path, payload)
    written.append({
        "path": str(path),
        "status": status,
        "backup": None,
        "created": status == "created",
    })


def _validate_existing_package(dest: Path) -> dict[str, Any]:
    _assert_no_symlink_components(dest)
    if not dest.exists():
        return {}
    if not dest.is_dir():
        raise CliError(f"MiniMax 插件目标不是目录: {dest}")
    manifest = dest / ".minimax-plugin" / "plugin.json"
    _assert_no_symlink_components(manifest)
    if not manifest.exists():
        if any(dest.iterdir()):
            raise CliError(
                f"MiniMax 插件目录已有未知内容，拒绝覆盖: {dest}"
            )
        return {}
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
        _assert_no_symlink_components(entry)
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
    return existing


def _validate_custom_agent(path: Path) -> list[str]:
    _assert_no_symlink_components(path)
    if path.parent.exists() and not path.parent.is_dir():
        raise CliError(f"MiniMax Agent 目录无效: {path.parent}")
    if not path.exists():
        if path.parent.is_dir() and any(path.parent.iterdir()):
            raise CliError(f"MiniMax Agent 目录已有用户内容，拒绝覆盖: {path.parent}")
        return []
    if not path.is_file() or path.stat().st_nlink != 1:
        raise CliError(f"MiniMax Agent 入口不是安全的普通文件: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CliError(f"无法读取 MiniMax Agent 入口: {path}") from exc
    lines = content.splitlines()
    if not any(line.removeprefix("# ") == _AGENT_OWNER for line in lines):
        raise CliError(f"MiniMax 已有同名用户 Agent，拒绝覆盖: {path}")
    return lines


def _validate_avatar_file(path: Path) -> bool:
    """Return whether the avatar exists and reject unsafe file types."""
    _assert_no_symlink_components(path)
    if not path.exists():
        return False
    if not path.is_file() or path.stat().st_nlink != 1:
        raise CliError(f"MiniMax Agent 头像不是安全的普通文件: {path}")
    return True


def validate_install(_root: Path) -> tuple[bool, str]:
    dest = _minimax_plugin_root()
    agent = _minimax_custom_agent_path()
    avatar = agent.with_name(_AGENT_AVATAR_NAME)
    try:
        manifest = _validate_existing_package(dest)
        agent_lines = _validate_custom_agent(agent)
        avatar_ok = (
            _AGENT_AVATAR_FIELD in agent_lines
            and _validate_avatar_file(avatar)
            and avatar.read_bytes() == (dest / "icon.png").read_bytes()
        )
        expected = _build_plugin_manifest()
        mcp = json.loads((dest / "servers.mcp.json").read_text(encoding="utf-8"))
    except (CliError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"MiniMax Code 注册无效: {exc}"
    fields = ("schemaVersion", "author", "category", "agents", "mcpServers", "skills", "icon")
    if (
        not avatar_ok
        or any(manifest.get(field) != expected[field] for field in fields)
        or mcp != _build_mcp_manifest()
        or any(not (dest / path).is_file() for path in (
            _WORKER_PATH, "skills/bugx/SKILL.md", "icon.png",
        ))
    ):
        return False, "MiniMax Code 的 Agent 或 Plugin V1 注册不完整，请运行 bugx install --agent minimax"
    return True, f"MiniMax Code 自定义 Agent 和插件已注册: {agent}"


def user_agent_surfaces() -> list[dict[str, object]]:
    agent = _minimax_custom_agent_path()
    return [{"path": agent.parent, "entries": (agent.name,), "label": "MiniMax Code"}]


def self_remove_user_agents(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    from .lifecycle import _backup_tree_external

    agent = _minimax_custom_agent_path()
    if not agent.exists() and not agent.is_symlink():
        return
    avatar = agent.with_name(_AGENT_AVATAR_NAME)
    try:
        agent_lines = _validate_custom_agent(agent)
        remove_avatar = _AGENT_AVATAR_FIELD in agent_lines and _validate_avatar_file(avatar)
        if backup_root is not None:
            _backup_tree_external(agent, backup_root / "minimax-bugx-agent.md")
            if remove_avatar:
                _backup_tree_external(avatar, backup_root / _AGENT_AVATAR_NAME)
        agent.unlink()
    except (CliError, OSError) as exc:
        preserved.append(f"{agent}（未清理: {exc}）")
    else:
        removed.append(f"{agent}（BugX 自定义 Agent 已移除）")
        if remove_avatar:
            try:
                avatar.unlink()
            except OSError as exc:
                preserved.append(f"{avatar}（未清理: {exc}）")
            else:
                removed.append(f"{avatar}（BugX Agent 头像已移除）")


def _ensure_minimax_plugin(
    root: Path,
    source: Path,
    *,
    locale: str | None = None,
) -> dict[str, Any]:
    """Install the Custom Agent and Plugin V1 package with one receipt."""
    from .agent import _tx_stage_global

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
    agent = _minimax_custom_agent_path()
    avatar = agent.with_name(_AGENT_AVATAR_NAME)
    previous_manifest = _validate_existing_package(dest)
    agent_lines = _validate_custom_agent(agent)

    manifest_bytes = (
        json.dumps(_build_plugin_manifest(locale=loc), indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    mcp_bytes = (
        json.dumps(_build_mcp_manifest(), indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    avatar_bytes = avatar_src.read_bytes()
    if not avatar_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CliError(f"BugX 头像不是有效 PNG: {avatar_src}")
    if (
        _validate_avatar_file(avatar)
        and _AGENT_AVATAR_FIELD not in agent_lines
        and avatar.read_bytes() != avatar_bytes
    ):
        raise CliError(f"MiniMax Agent 头像路径已有用户文件，拒绝覆盖: {avatar}")
    written: list[dict[str, Any]] = []

    _write_owned_file(dest / "servers.mcp.json", mcp_bytes, written)
    _write_owned_file(dest / "icon.png", avatar_bytes, written)
    _write_owned_file(
        dest / _WORKER_PATH,
        _build_worker_agent(locale=loc).encode("utf-8"),
        written,
    )
    _write_owned_file(
        dest / "skills" / "bugx" / "SKILL.md",
        skill_src.read_bytes(),
        written,
    )
    _write_owned_file(dest / ".minimax-plugin" / "plugin.json", manifest_bytes, written)
    legacy_path = dest / _LEGACY_MAIN_PATH
    previous_agents = previous_manifest.get("agents")
    if isinstance(previous_agents, list) and _LEGACY_MAIN_PATH in previous_agents and legacy_path.is_file():
        _tx_stage_global(legacy_path)
        legacy_path.unlink()
        written.append({"path": str(legacy_path), "status": "removed", "backup": None, "created": False})
    _validate_custom_agent(agent)
    _write_owned_file(avatar, avatar_bytes, written)
    _write_owned_file(agent, _build_main_agent(locale=loc).encode("utf-8"), written)
    valid, detail = validate_install(root)
    if not valid:
        raise CliError(detail)

    changed = any(item["status"] != "unchanged" for item in written)
    return {
        "status": "updated" if changed else "unchanged",
        "dir": str(dest),
        "home": str(data_dir),
        "manifest_path": str(dest / ".minimax-plugin" / "plugin.json"),
        "mcp_path": str(dest / "servers.mcp.json"),
        "agent_path": str(agent),
        "avatar_path": str(avatar),
        "worker_agent_path": str(dest / _WORKER_PATH),
        "written": written,
        "backups": [],
    }


def validate_install(root: Path) -> tuple[bool, str]:
    """Verify MiniMax Code Plugin V1 assets and MCP servers at user-level plugin root."""
    del root
    try:
        dest = _minimax_plugin_root()
    except CliError as exc:
        return False, str(exc)
    manifest = dest / ".minimax-plugin" / "plugin.json"
    mcp_path = dest / "servers.mcp.json"
    agent_main = dest / "agents" / "bugx.md"
    agent_worker = dest / "agents" / "bugx-worker.md"
    skill_file = dest / "skills" / "bugx" / "SKILL.md"
    required_files = (
        ("plugin manifest", manifest),
        ("MCP 配置", mcp_path),
        ("主智能体", agent_main),
        ("Worker 智能体", agent_worker),
        ("Skill", skill_file),
    )
    missing = [
        label for label, path in required_files
        if path.is_symlink() or not path.is_file()
    ]
    if missing:
        return (
            False,
            f"MiniMax Code 插件不完整（{dest} 缺少 {'、'.join(missing)}）；"
            "请运行 `bugx install --agent minimax`",
        )
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"MiniMax Code plugin.json 无法解析（{manifest}）：{exc}"
    if not isinstance(manifest_data, dict) or manifest_data.get("name") != _PLUGIN_NAME:
        return False, f"MiniMax Code plugin.json 无效（{manifest}）；请运行 `bugx install --agent minimax`"
    try:
        mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"MiniMax Code servers.mcp.json 无法解析（{mcp_path}）：{exc}"
    servers = mcp_data.get("mcpServers") if isinstance(mcp_data, dict) else None
    if not isinstance(servers, dict):
        return False, f"MiniMax Code servers.mcp.json 缺少 mcpServers 对象（{mcp_path}）"
    required_channels = ("bugx-supervisor", "bugx-supervisor-worker", "bugx-docs")
    missing_channels = [
        ch for ch in required_channels if not isinstance(servers.get(ch), dict)
    ]
    if missing_channels:
        return (
            False,
            f"MiniMax Code servers.mcp.json 缺少 {', '.join(missing_channels)}（{mcp_path}）；"
            "请运行 `bugx install --agent minimax`",
        )
    return True, f"MiniMax Code Plugin V1 与三通道 MCP 已就绪（{dest}）"
