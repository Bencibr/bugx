"""FunIDE/Copilot user-level Agent registration."""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..common import (
    CliError,
    _atomic_write_global_file,
    _backup_host_resource,
    _find_repo_root,
    _i18n_exc,
    _resolve_locale_name,
)
from ..host_adapters import (
    add_locale_marker,
    _funide_data_dir,
    _funide_profile_mcp_path,
    _funide_profile_settings_path,
    _read_funide_json_object,
    _funide_user_skills_dir,
    _funide_user_agents_dir,
    get_adapter,
    get_agent_description,
)


_USER_AGENT_OWNERSHIP_MARK = "## FunIDE 用户级智能体边界"
_NATIVE_AGENT_OWNERSHIP_MARK = "<!-- BugX managed FunIDE native agent -->"
_NATIVE_SKILL_OWNERSHIP_MARK = "<!-- BugX managed FunIDE native skill -->"
_FUNIDE_CLOUD_CONFIG_KIND = "agents"
_FUNIDE_DEFAULT_CLOUD_BASE_URL = "https://fun.quqimeng.com/ide/api/v1"
_FUNIDE_MCP_AUTOSTART_KEY = "chat.mcp.autostart"
_FUNIDE_MCP_AUTOSTART_VALUE = "newAndOutdated"


def _funide_native_mcp_names() -> tuple[str, ...]:
    return ("bugx-supervisor", "bugx-supervisor-worker", "bugx-docs")


def _funide_skill_source(root: Path) -> Path | None:
    """Find the canonical BugX Skill payload without depending on a workspace copy."""
    candidates = (
        root / ".agents" / "skills" / "bugx" / "SKILL.md",
        root / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        root / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        _find_repo_root(Path(__file__)) / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
        _find_repo_root(Path(__file__)) / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _funide_fallback_skill(*, locale: str | None = None) -> str:
    """Keep installed wheels useful when the source tree is not present."""
    content = (
        "---\n"
        "name: bugx\n"
        f"description: {get_agent_description(locale)}\n"
        "---\n\n"
        "# BugX\n\n"
        "这是 BugX 的 FunIDE 技能入口。执行审查前先运行 "
        "`bugx engine path --name bugx.md` 读取全局协议，并通过当前项目的 "
        "BugX MCP 完成启动、Worker、checkpoint 与报告闭环。\n"
    )
    return add_locale_marker(content, locale)


def _markdown_body(rendered: str) -> str:
    """Extract the prompt body from a rendered Agent markdown entry."""
    lines = rendered.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "\n".join(lines[index + 1:]).strip()
    return rendered.strip()


def _funide_native_agent_record(
    rendered: str, *, locale: str | None = None,
) -> dict[str, object]:
    """Convert BugX's thin markdown entry to FunIDE's native agent schema."""
    prompt = _markdown_body(rendered)
    if _NATIVE_AGENT_OWNERSHIP_MARK not in prompt:
        prompt = f"{prompt}\n\n{_NATIVE_AGENT_OWNERSHIP_MARK}"
    return {
        "id": "bugx",
        "name": "BugX",
        "description": get_agent_description(locale),
        "systemPrompt": prompt,
        "toolAllowList": [
            "apply_change", "delete_file", "run_command", "list_pending_changes",
        ],
        "enabled": True,
    }


def _read_funide_native_agents(path: Path) -> dict[str, object]:
    """Read FunIDE's native agent registry without silently replacing it."""
    if not path.exists():
        return {"version": 2, "agents": [], "disabledBuiltins": []}
    if path.is_symlink() or not path.is_file():
        raise CliError(
            _i18n_exc(
                "plugin.funide_native_agents_not_regular_file",
                f"FunIDE 原生智能体注册表不是普通文件，拒绝覆盖: {path}",
                path=path,
            )
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(
            _i18n_exc(
                "plugin.funide_native_agents_invalid",
                f"无法解析 FunIDE 原生智能体注册表，拒绝覆盖: {path}: {exc}",
                path=path,
                exc=exc,
            )
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        raise CliError(
            _i18n_exc(
                "plugin.funide_native_agents_invalid_shape",
                f"FunIDE 原生智能体注册表格式异常，拒绝覆盖: {path}",
                path=path,
            )
        )
    return data


def _ensure_funide_native_agent(
    root: Path, rendered: str, *, locale: str | None = None,
) -> dict[str, object]:
    """Upsert BugX into FunIDE's Settings → 智能体 registry."""
    data_root = _funide_data_dir()
    target = data_root / "agents.json"
    data = _read_funide_native_agents(target)
    agents = data["agents"]
    assert isinstance(agents, list)
    record = _funide_native_agent_record(rendered, locale=locale)
    existing_index = next(
        (index for index, item in enumerate(agents)
         if isinstance(item, dict) and item.get("id") == "bugx"),
        None,
    )
    if existing_index is not None:
        existing = agents[existing_index]
        if not isinstance(existing, dict) or _NATIVE_AGENT_OWNERSHIP_MARK not in str(existing.get("systemPrompt", "")):
            raise CliError(
                _i18n_exc(
                    "plugin.funide_native_agent_collision",
                    "FunIDE 原生智能体 id=bugx 已被其他内容占用，拒绝覆盖",
                    path=target,
                )
            )
        if existing == record and data.get("version") == 2:
            return {
                "path": str(target), "status": "unchanged", "backup": None,
                "created": False, "data_dir": str(data_root),
            }
        agents[existing_index] = record
    else:
        agents.append(record)
    data["version"] = 2
    if not isinstance(data.get("disabledBuiltins"), list):
        data["disabledBuiltins"] = []
    payload = (json.dumps(data, ensure_ascii=False, indent="\t") + "\n").encode("utf-8")
    backup = _backup_host_resource(root, "funide", target) if target.exists() else None
    _atomic_write_global_file(target, payload)
    return {
        "path": str(target),
        "status": "created" if backup is None else "upgraded",
        "backup": str(backup) if backup is not None else None,
        "created": backup is None,
        "data_dir": str(data_root),
    }


def _ensure_funide_native_skill(
    root: Path, skill_source: Path | None, *, locale: str | None = None,
) -> dict[str, object]:
    """Install BugX into FunIDE Settings → 技能's native user directory."""
    data_root = _funide_data_dir()
    skills_root = _funide_user_skills_dir()
    skill_dir = skills_root / "bugx"
    target = skill_dir / "SKILL.md"
    if data_root.is_symlink() or skills_root.is_symlink() or skill_dir.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.funide_symlink_skill_dir",
                f"拒绝写入符号链接 FunIDE 用户级技能目录: {skill_dir}",
                target=skill_dir,
            )
        )
    if target.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.funide_symlink_skill_refused",
                f"拒绝覆盖符号链接 FunIDE 用户级技能: {target}",
                target=target,
            )
        )
    if skill_source is not None:
        try:
            content = skill_source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CliError(
                _i18n_exc(
                    "plugin.funide_skill_source_unreadable",
                    f"无法读取 FunIDE 技能源文件: {skill_source}: {exc}",
                    source=skill_source, exc=exc,
                )
            ) from exc
        content = add_locale_marker(content, locale)
    else:
        content = _funide_fallback_skill(locale=locale)
    if _NATIVE_SKILL_OWNERSHIP_MARK not in content:
        content = content.rstrip("\n") + f"\n\n{_NATIVE_SKILL_OWNERSHIP_MARK}\n"
    payload = content.encode("utf-8")
    if target.exists() and not target.is_file():
        raise CliError(
            _i18n_exc(
                "plugin.funide_native_skill_invalid",
                f"FunIDE 原生技能不是普通文件，拒绝覆盖: {target}",
                target=target,
            )
        )
    if target.is_file():
        current = target.read_text(encoding="utf-8")
        if _NATIVE_SKILL_OWNERSHIP_MARK not in current:
            raise CliError(
                _i18n_exc(
                    "plugin.funide_native_skill_collision",
                    "FunIDE 原生技能名 bugx 已被其他内容占用，拒绝覆盖",
                    path=target,
                )
            )
        if target.read_bytes() == payload:
            return {
                "path": str(target), "status": "unchanged", "backup": None,
                "created": False, "data_dir": str(data_root),
            }
    backup = _backup_host_resource(root, "funide", target) if target.exists() else None
    skill_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_global_file(target, payload)
    return {
        "path": str(target),
        "status": "created" if backup is None else "upgraded",
        "backup": str(backup) if backup is not None else None,
        "created": backup is None,
        "data_dir": str(data_root),
    }


def _funide_profile_mcp_entry_owned(name: str, entry: object) -> bool:
    """Recognize BugX's prior FunIDE entry before an idempotent upgrade/removal."""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    args = entry.get("args")
    if command != "bugx" or not isinstance(args, list) or any(not isinstance(v, str) for v in args):
        return False
    if name in {"bugx-supervisor", "bugx-supervisor-worker"}:
        return args[:3] == ["mcp", "serve", "--root"] and "--role" in args
    return name == "bugx-docs" and args[:3] == ["mcp", "docs", "serve"] and "--root" in args


def _funide_cloud_storage_path() -> Path:
    """Return FunIDE's SQLite profile store without following a symlink."""
    return _funide_data_dir().parent / "globalStorage" / "state.vscdb"


def _funide_cloud_values() -> dict[str, str]:
    """Read only the short-lived FunIDE cloud metadata needed for agent sync."""
    database = _funide_cloud_storage_path()
    if database.is_symlink() or not database.is_file():
        return {}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(database), timeout=2)
        rows = connection.execute(
                "SELECT key, value FROM ItemTable "
                "WHERE key IN (?, ?, ?, ?)",
                (
                    "funide.cloud.accessToken",
                    "funide.cloud.baseUrl",
                    "funide.cloud.revision.agents",
                    "funide.cloud.md5.agents",
                ),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if connection is not None:
            connection.close()
    values: dict[str, str] = {}
    for key, value in rows:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value is not None:
            values[str(key)] = str(value)
    return values


def _funide_cloud_request(
    method: str,
    url: str,
    token: str,
    *,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    """Call FunIDE's config endpoint without logging credentials or payloads."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Client-Id": "bugx-cli-funide",
        "X-Device-Id": "bugx-cli-funide",
        "X-Device-Name": "bugx-cli",
        "X-Platform": sys.platform,
        "X-Client-Version": "0.1.0",
    }
    encoded = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise CliError(
            _i18n_exc(
                "plugin.funide_cloud_sync_network",
                f"FunIDE Agent 云同步请求失败: {type(exc).__name__}",
                exc=exc,
            )
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(
            _i18n_exc(
                "plugin.funide_cloud_sync_invalid_response",
                "FunIDE Agent 云同步返回了无效响应",
            )
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise CliError(
            _i18n_exc(
                "plugin.funide_cloud_sync_rejected",
                "FunIDE Agent 云同步被服务端拒绝",
            )
        )
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise CliError(
            _i18n_exc(
                "plugin.funide_cloud_sync_invalid_data",
                "FunIDE Agent 云同步响应格式异常",
            )
        )
    return data


def _funide_sync_native_agents_to_cloud() -> dict[str, object]:
    """Merge BugX into FunIDE's cloud Agent config when the IDE is signed in.

    FunIDE applies the cloud ``agents`` payload on startup.  A local-only write
    is therefore lost for signed-in users when the server still contains the
    empty default payload.  This helper is deliberately best-effort: users who
    are signed out, offline, or running an older FunIDE keep the local files and
    receive an explicit status in the install result.
    """
    values = _funide_cloud_values()
    token = values.get("funide.cloud.accessToken", "").strip()
    if not token:
        return {"status": "skipped", "reason": "未检测到 FunIDE 云端登录态"}
    native_path = _funide_data_dir() / "agents.json"
    try:
        local = json.loads(native_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"status": "failed", "reason": f"无法读取本地 agents.json: {type(exc).__name__}"}
    if not isinstance(local, dict) or not isinstance(local.get("agents"), list):
        return {"status": "failed", "reason": "本地 agents.json 格式异常"}
    base = values.get("funide.cloud.baseUrl", _FUNIDE_DEFAULT_CLOUD_BASE_URL).strip().rstrip("/")
    if not base:
        base = _FUNIDE_DEFAULT_CLOUD_BASE_URL
    query = urllib.parse.urlencode({"since_revision": 0, "client_md5": "bugx-force"})
    try:
        remote = _funide_cloud_request(
            "GET", f"{base}/sync/get/{_FUNIDE_CLOUD_CONFIG_KIND}?{query}", token,
        )
        remote_payload = remote.get("payload")
        remote_agents = (
            list(remote_payload.get("agents", []))
            if isinstance(remote_payload, dict) and isinstance(remote_payload.get("agents"), list)
            else []
        )
        desired = next(
            (item for item in local["agents"]
             if isinstance(item, dict) and item.get("id") == "bugx"),
            None,
        )
        if not isinstance(desired, dict):
            return {"status": "failed", "reason": "本地 agents.json 缺少 BugX 记录"}
        remote_bugx = next(
            (item for item in remote_agents
             if isinstance(item, dict) and item.get("id") == "bugx"),
            None,
        )
        if remote_bugx == desired:
            return {
                "status": "unchanged",
                "revision": remote.get("revision"),
                "agent_ids": [
                    item.get("id") for item in remote_agents
                    if isinstance(item, dict) and item.get("id")
                ],
            }
        merged: list[object] = []
        replaced = False
        for item in remote_agents:
            if isinstance(item, dict) and item.get("id") == "bugx":
                merged.append(desired)
                replaced = True
            else:
                merged.append(item)
        if not replaced:
            merged.append(desired)
        payload: dict[str, object] = {
            "version": 2,
            "agents": merged,
            "disabledBuiltins": (
                remote_payload.get("disabledBuiltins", [])
                if isinstance(remote_payload, dict)
                and isinstance(remote_payload.get("disabledBuiltins", []), list)
                else []
            ),
        }
        revision = int(remote.get("revision", values.get("funide.cloud.revision.agents", 0)) or 0)
        result = _funide_cloud_request(
            "POST", f"{base}/sync/put/{_FUNIDE_CLOUD_CONFIG_KIND}", token,
            body={"payload": payload, "base_revision": revision},
        )
    except CliError as exc:
        return {"status": "failed", "reason": str(exc)}
    return {
        "status": "synced",
        "revision": result.get("revision"),
        "agent_ids": [
            item.get("id") for item in merged if isinstance(item, dict) and item.get("id")
        ],
    }


def _ensure_funide_profile_mcp(root: Path) -> dict[str, object]:
    """Register BugX MCP in FunIDE's user-level ``<User>/mcp.json``."""
    from .plugin import _host_supervisor_server_definitions, _host_mcp_entry_is_current

    target = _funide_profile_mcp_path()
    if target.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.funide_symlink_mcp_refused",
                f"拒绝写入符号链接 FunIDE 用户级 MCP 配置: {target}",
                target=target,
            )
        )
    payload: dict[str, object]
    if not target.exists():
        payload = {}
    else:
        if not target.is_file():
            raise CliError(
                _i18n_exc(
                    "plugin.funide_mcp_config_not_regular_file",
                    f"FunIDE 用户级 MCP 配置不是普通文件，拒绝覆盖: {target}",
                    target=target,
                )
            )
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError(
                _i18n_exc(
                    "plugin.funide_mcp_config_invalid",
                    f"无法解析 FunIDE 用户级 MCP 配置，拒绝覆盖: {target}: {exc}",
                    path=target, exc=exc,
                )
            ) from exc
        if not isinstance(loaded, dict):
            raise CliError(
                _i18n_exc(
                    "plugin.funide_mcp_config_invalid_shape",
                    f"FunIDE 用户级 MCP 配置顶层必须是对象，拒绝覆盖: {target}",
                    path=target,
                )
            )
        payload = loaded
    servers = payload.get("servers")
    if servers is None:
        servers = {}
        payload["servers"] = servers
    if not isinstance(servers, dict):
        raise CliError(
            _i18n_exc(
                "plugin.funide_servers_not_object",
                f"FunIDE 用户级 MCP 配置 servers 不是对象，拒绝写入: {target}",
                target=target,
            )
        )
    desired = _host_supervisor_server_definitions(shape="funide")
    changed = False
    for name in _funide_native_mcp_names():
        current = servers.get(name)
        if current is None:
            servers[name] = desired[name]
            changed = True
            continue
        if _host_mcp_entry_is_current(name, current):
            continue
        if not _funide_profile_mcp_entry_owned(name, current):
            raise CliError(
                _i18n_exc(
                    "plugin.funide_mcp_collision",
                    f"FunIDE 用户级 MCP 名称 {name} 已被其他内容占用，拒绝覆盖",
                    name=name, path=target,
                )
            )
        servers[name] = desired[name]
        changed = True
    if not changed:
        return {
            "path": str(target), "status": "unchanged", "backup": None,
            "created": False, "servers_key": "servers",
        }
    backup = _backup_host_resource(root, "funide", target) if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_global_file(
        target,
        (json.dumps(payload, ensure_ascii=False, indent="\t") + "\n").encode("utf-8"),
    )
    return {
        "path": str(target),
        "status": "created" if backup is None else "upgraded",
        "backup": str(backup) if backup is not None else None,
        "created": backup is None,
        "servers_key": "servers",
    }


def _ensure_funide_mcp_autostart(root: Path) -> dict[str, object]:
    """Enable FunIDE MCP autostart for new and outdated servers.

    FunIDE reads this setting from the VS Code-compatible profile settings
    file, not from ``mcp.json``.  Preserve every unrelated setting while
    making the installer deterministic across fresh and existing profiles.
    """
    target = _funide_profile_settings_path()
    if target.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.funide_symlink_settings_refused",
                f"拒绝写入符号链接 FunIDE 用户设置: {target}",
                target=target,
            )
        )
    data: dict[str, object]
    if not target.exists():
        data = {}
    else:
        if not target.is_file():
            raise CliError(
                _i18n_exc(
                    "plugin.funide_settings_not_regular_file",
                    f"FunIDE 用户设置不是普通文件，拒绝覆盖: {target}",
                    target=target,
                )
            )
        try:
            loaded = _read_funide_json_object(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError(
                _i18n_exc(
                    "plugin.funide_settings_invalid",
                    f"无法解析 FunIDE 用户设置，拒绝覆盖: {target}",
                    path=target, exc=exc,
                )
            ) from exc
        data = loaded
    if data.get(_FUNIDE_MCP_AUTOSTART_KEY) == _FUNIDE_MCP_AUTOSTART_VALUE:
        return {
            "path": str(target), "status": "unchanged", "backup": None,
            "created": False, "key": _FUNIDE_MCP_AUTOSTART_KEY,
            "value": _FUNIDE_MCP_AUTOSTART_VALUE,
        }
    data[_FUNIDE_MCP_AUTOSTART_KEY] = _FUNIDE_MCP_AUTOSTART_VALUE
    backup = _backup_host_resource(root, "funide", target) if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_global_file(
        target,
        (json.dumps(data, ensure_ascii=False, indent="\t") + "\n").encode("utf-8"),
    )
    return {
        "path": str(target),
        "status": "created" if backup is None else "upgraded",
        "backup": str(backup) if backup is not None else None,
        "created": backup is None,
        "key": _FUNIDE_MCP_AUTOSTART_KEY,
        "value": _FUNIDE_MCP_AUTOSTART_VALUE,
    }


def _funide_user_agent_content(source: Path, *, locale: str | None = None) -> str:
    loc = _resolve_locale_name(locale)
    entry = get_adapter("funide").render(source, locale=loc)
    boundary = (
        "\n\n"
        f"{_USER_AGENT_OWNERSHIP_MARK}\n\n"
        "本入口安装在 FunIDE/Copilot 的用户级 Agent 目录（`~/.copilot/agents/`），"
        "因此可在不同 FunIDE 工作区中使用。当前项目仍需通过项目级 `.mcp.json` 注册 BugX MCP；"
        "如需初始化或修复当前项目，请运行 `bugx install --root . --agent funide --non-interactive`。\n"
    )
    return entry.rstrip("\n") + boundary


def _ensure_funide_user_agents(
    root: Path, source: Path, *, locale: str | None = None,
) -> dict[str, object]:
    """Install FunIDE's user Agent, Skill, and MCP settings-page entries."""
    agents_dir = _funide_user_agents_dir()
    if agents_dir.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.funide_symlink_agents_dir",
                f"拒绝写入符号链接 FunIDE 用户级 agents 目录: {agents_dir}",
                agents_dir=agents_dir,
            )
        )
    agents_dir.mkdir(parents=True, exist_ok=True)
    target = agents_dir / "bugx.agent.md"
    if target.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.funide_symlink_agent_refused",
                f"拒绝覆盖符号链接 FunIDE 用户级智能体: {target}",
                target=target,
            )
        )

    rendered = get_adapter("funide").render(source, locale=_resolve_locale_name(locale))
    payload = _funide_user_agent_content(source, locale=locale).encode("utf-8")
    if target.is_file() and target.read_bytes() == payload:
        written = [{
            "path": str(target), "status": "unchanged",
            "backup": None, "created": False,
        }]
        backups: list[Path] = []
    else:
        backup = _backup_host_resource(root, "funide", target) if target.exists() else None
        backups = [backup] if backup is not None else []
        _atomic_write_global_file(target, payload)
        written = [{
            "path": str(target),
            "status": "created" if backup is None else "upgraded",
            "backup": str(backup) if backup is not None else None,
            "created": backup is None,
        }]
    changed_resources: list[dict[str, object]] = [item for item in written if item["status"] != "unchanged"]
    try:
        native = _ensure_funide_native_agent(root, rendered, locale=locale)
        written.append(native)
        if native["status"] != "unchanged":
            changed_resources.append(native)
        skill = _ensure_funide_native_skill(
            root, _funide_skill_source(root), locale=locale,
        )
        written.append(skill)
        if skill["status"] != "unchanged":
            changed_resources.append(skill)
        mcp = _ensure_funide_profile_mcp(root)
        written.append(mcp)
        if mcp["status"] != "unchanged":
            changed_resources.append(mcp)
        autostart = _ensure_funide_mcp_autostart(root)
        written.append(autostart)
        if autostart["status"] != "unchanged":
            changed_resources.append(autostart)
        cloud_sync = _funide_sync_native_agents_to_cloud()
    except BaseException:
        # Agent, Skill, and MCP are one user-level transaction.  A collision
        # or malformed native file must not leave only part of the IDE setup.
        for item in reversed(changed_resources):
            try:
                item_path = Path(str(item["path"]))
                backup_path = Path(str(item["backup"])) if item.get("backup") else None
                if backup_path is not None and backup_path.is_file():
                    _atomic_write_global_file(item_path, backup_path.read_bytes())
                elif bool(item.get("created")) and item_path.exists() and not item_path.is_symlink():
                    item_path.unlink()
            except OSError:
                pass
        raise
    for item in written:
        if item.get("backup"):
            backup_path = Path(str(item["backup"]))
            if backup_path not in backups:
                backups.append(backup_path)
    return {
        "dir": str(agents_dir),
        "path": str(target),
        "written": written,
        "backups": backups,
        "native": native,
        "skill": skill,
        "mcp": mcp,
        "autostart": autostart,
        "cloud_sync": cloud_sync,
    }
