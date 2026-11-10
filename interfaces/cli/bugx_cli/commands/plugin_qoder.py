from __future__ import annotations

import json
import os
from pathlib import Path

from .. import __version__
from ..common import (
    CliError,
    _assert_no_symlink_components,
    _atomic_write_global_file,
    _backup_host_resource,
    _i18n_exc,
    _i18n_prompt,
    _resolve_locale_name,
)
from ..host_adapters import (
    QODER_WORKER_INSTALL_PATH,
    get_adapter,
    render_qoder_agent_entry,
    render_qoder_worker_entry,
)


def _qoder_user_agents_dir() -> Path:
    """Return the user-level Qoder agents directory (~/.qoder/agents)."""
    override = os.environ.get("BUGX_QODER_USER_AGENTS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".qoder" / "agents").resolve()


def _qoder_user_skills_dir() -> Path:
    """Return the user-level Qoder skills directory (~/.qoder/skills)."""
    override = os.environ.get("BUGX_QODER_USER_SKILLS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".qoder" / "skills").resolve()


def _qoder_user_plugins_dir() -> Path:
    """Return the user-level Qoder plugins directory (~/.qoder/plugins)."""
    override = os.environ.get("BUGX_QODER_USER_PLUGINS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".qoder" / "plugins").resolve()


def _qoder_user_settings_path() -> Path:
    """Return the user-level Qoder settings path (~/.qoder/settings.json)."""
    override = os.environ.get("BUGX_QODER_USER_SETTINGS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".qoder" / "settings.json").resolve()


def _qoder_user_mcp_path() -> Path:
    """Return the user-level Qoder MCP path (~/.qoder/mcp.json)."""
    override = os.environ.get("BUGX_QODER_USER_MCP_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".qoder" / "mcp.json").resolve()


def _qoder_user_plugin_cache_dir() -> Path:
    """Return the user-level Qoder plugin cache directory (~/.qoder/plugins/cache/bugx-agent/bugx)."""
    override = os.environ.get("BUGX_QODER_USER_PLUGIN_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".qoder" / "plugins" / "cache" / "bugx-agent" / "bugx").resolve()


def _qoder_user_agent_boundary(*, locale: str | None = None) -> str:
    """用户级智能体与插件入口共用的跨项目边界段。"""
    loc = _resolve_locale_name(locale)
    boundary = _i18n_prompt(
        "plugin.qoder_user_agent_boundary",
        "## 用户级智能体与插件边界（跨项目形态专用）\n\n"
        "本入口安装为 Qoder 用户级资产（`~/.qoder/`，"
        "跨工作区全局可用）。"
        "当前项目尚未完成 BugX 初始化时，使用 `bugx engine path --name bugx-runtime-contract.md` "
        "读取全局协议；"
        "先运行 `bugx install --root . --agent qoder --non-interactive` 安装工作区配置与 MCP，"
        "再开始审计；不得在未初始化项目上凭空执行协议步骤，也不得把本入口当成 "
        "全局引擎载荷的替代实体。\n",
        locale=loc,
    )
    return "\n\n" + boundary.rstrip() + "\n"


def _qoder_user_agent_content_main(source: Path, *, locale: str | None = None) -> str:
    """用户级主审计智能体入口：宿主模板渲染 + 用户级边界段。"""
    loc = _resolve_locale_name(locale)
    entry = render_qoder_agent_entry(source, locale=loc)
    return entry.rstrip("\n") + _qoder_user_agent_boundary(locale=loc)


def _qoder_user_agent_content_worker(*, locale: str | None = None) -> str:
    """用户级并发 worker 子智能体入口：宿主模板渲染 + 用户级边界段。"""
    loc = _resolve_locale_name(locale)
    entry = render_qoder_worker_entry(locale=loc)
    return entry.rstrip("\n") + _qoder_user_agent_boundary(locale=loc)


def _clean_legacy_qoder_plugin_and_skills() -> None:
    """清理历史遗留的 Qoder 散装技能目录与插件注册（Option A 收敛保障）。"""
    import shutil

    # 1. 清理 ~/.qoder/skills/bugx
    try:
        user_skill = _qoder_user_skills_dir() / "bugx"
        if user_skill.is_dir() and not user_skill.is_symlink():
            shutil.rmtree(user_skill)
    except OSError:
        pass

    # 2. 清理 ~/.qoder/plugins/cache/bugx-agent
    try:
        cache_parent = _qoder_user_plugins_dir() / "cache" / "bugx-agent"
        if cache_parent.is_dir() and not cache_parent.is_symlink():
            shutil.rmtree(cache_parent)
    except OSError:
        pass

    # 3. 从 installed_plugins_v2.json 与 installed_plugins.json 中注销
    try:
        plugins_dir = _qoder_user_plugins_dir()
        for filename in ("installed_plugins_v2.json", "installed_plugins.json"):
            p = plugins_dir / filename
            if p.is_file() and not p.is_symlink():
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("plugins"), dict):
                    if "bugx@bugx-agent" in loaded["plugins"]:
                        del loaded["plugins"]["bugx@bugx-agent"]
                        p.write_text(json.dumps(loaded, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass

    # 4. 从 settings.json enabledPlugins 中注销
    try:
        settings_path = _qoder_user_settings_path()
        if settings_path.is_file() and not settings_path.is_symlink():
            settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(settings_data, dict):
                enabled = settings_data.get("enabledPlugins")
                if isinstance(enabled, dict) and "bugx@bugx-agent" in enabled:
                    del enabled["bugx@bugx-agent"]
                    settings_path.write_text(json.dumps(settings_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass

    # 5. 从用户级 ~/.qoder/mcp.json 中主动清理历史遗留的 BugX Supervisor 注册条目（保持项目级隔离）
    try:
        user_mcp = _qoder_user_mcp_path()
        if user_mcp.is_file() and not user_mcp.is_symlink():
            mcp_data = json.loads(user_mcp.read_text(encoding="utf-8"))
            if isinstance(mcp_data, dict) and isinstance(mcp_data.get("mcpServers"), dict):
                servers = mcp_data["mcpServers"]
                bugx_keys = [
                    k for k, v in servers.items()
                    if k in {"bugx-supervisor", "bugx-supervisor-worker", "bugx-docs"}
                    or (isinstance(v, dict) and any("bugx" in str(arg).lower() for arg in v.get("args", [])))
                ]
                if bugx_keys:
                    for k in bugx_keys:
                        del servers[k]
                    user_mcp.write_text(json.dumps(mcp_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def _ensure_qoder_user_agents(
    root: Path, source: Path, *, locale: str | None = None,
) -> dict[str, object]:
    """把主/worker 智能体写入 Qoder 用户级目录，并清理历史遗留插件/技能。"""
    agents_dir = _qoder_user_agents_dir()
    if agents_dir.is_symlink():
        raise CliError(_i18n_exc("plugin.qoder_symlink_agents_dir", f"拒绝写入符号链接 Qoder 用户级 agents 目录: {agents_dir}", agents_dir=agents_dir))
    agents_dir.mkdir(parents=True, exist_ok=True)

    generated = {
        agents_dir / "bugx.md": _qoder_user_agent_content_main(source, locale=locale),
        agents_dir / "bugx-worker.md": _qoder_user_agent_content_worker(locale=locale),
    }

    written: list[dict[str, object]] = []
    backups: list[Path] = []
    for target, content in generated.items():
        payload = content.encode("utf-8")
        if target.is_symlink():
            raise CliError(_i18n_exc("plugin.qoder_symlink_asset_refused", f"拒绝覆盖符号链接 Qoder 用户级文件: {target}", target=target))
        if target.is_file() and target.read_bytes() == payload:
            written.append({
                "path": str(target), "status": "unchanged",
                "backup": None, "created": False,
            })
            continue
        backup = _backup_host_resource(root, "qoder", target) if target.exists() else None
        if backup is not None:
            backups.append(backup)
        _atomic_write_global_file(target, payload)
        written.append({
            "path": str(target),
            "status": "created" if backup is None else "upgraded",
            "backup": str(backup) if backup is not None else None,
            "created": backup is None,
        })

    # 清理历史遗留的散装技能与插件容器（Option A 收敛保障）
    _clean_legacy_qoder_plugin_and_skills()

    return {
        "dir": str(agents_dir),
        "written": written,
        "backups": backups,
    }
