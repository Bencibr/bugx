from __future__ import annotations

import os
from pathlib import Path

from ..common import (
    CliError,
    _atomic_write_global_file,
    _backup_host_resource,
    _i18n_exc,
    _i18n_prompt,
    _resolve_locale_name,
)
from ..host_adapters import (
    render_cline_agent_entry,
    render_cline_worker_entry,
)
from ..hosts.registry import engine_host_plugin

#: 宿主名由**本模块的约定名**推导（``commands/plugin_<host>.py``），不写字面量。
#: 这样宿主改名只改一个模块文件名，通用层与实现体都不会留下第二份声明。
_HOST_NAME = __name__.rsplit(".", 1)[-1].removeprefix("plugin_")
_PLUGIN = engine_host_plugin(_HOST_NAME)

#: Worker 入口路径的唯一声明点是 ``core/hosts/<host>.py`` 的 ``install.worker_entry_path``。
CLINE_WORKER_INSTALL_PATH = _PLUGIN.install.worker_entry_path


def _cline_mcp_settings_path(root: Path | None = None) -> Path:
    """Cline 3.x 唯一会被加载的 MCP 注册位置（用户级数据目录内）。

    路径口径**只有一处**：``core/hosts/cline.py`` 的 ``engine_run.mcp_config_paths``
    （``<CLINE_DATA_DIR>/settings/cline_mcp_settings.json``，未设置时落在
    ``~/.cline/data/settings/cline_mcp_settings.json``）。此处只做委托，不复制逻辑。

    Cline 3.x **没有工作区级 MCP 作用域**：项目内 ``<root>/.cline/mcp.json`` 不在
    任何读取路径上，写进去是静默 no-op（2026-10-22 探针 ``--dry-run`` 反证：
    项目级文件存在但 ``mcp_configured=False``）。``CLINE_MCP_SETTINGS_PATH`` 同样
    是死开关（Hub Daemon 不继承客户端环境变量），因此不再支持任何覆盖入口。
    """

    return _PLUGIN.engine_run.mcp_config_paths(root or Path.cwd())[0]


_CLINE_USER_AGENT_FILENAMES = ("bugx.yaml", "bugx.md", "bugx-worker.yaml", "bugx-worker.md")


def validate_install(root: Path) -> tuple[bool, str]:
    """安装诊断（供 ``bugx doctor`` 的宿主专属钩子调用）。

    入口路径来自插件 ``install.path`` / ``install.required_paths``，MCP 校验走通用层
    的全局作用域实现（同一份绝对化口径），因此本模块不再持有第二份路径事实。
    """

    from .plugin import _validate_global_supervisor_mcp

    label = _PLUGIN.identity.label
    skill = root / _PLUGIN.install.path
    agent_entry = root / _PLUGIN.install.required_paths[0]
    ok, detail = _validate_global_supervisor_mcp(_PLUGIN.name, root)
    has_entry = skill.is_file() or agent_entry.is_file()
    if ok and has_entry:
        return True, detail + f"；{label} 入口已就绪"
    if not has_entry:
        detail += (
            f"；缺少 {_PLUGIN.install.path}"
            f" 或 {_PLUGIN.install.required_paths[0]}"
        )
    elif not ok:
        detail += f"；Skill 存在但不能替代 {label} MCP 配置"
    return False, detail + f"；请运行 bugx install --agent {_PLUGIN.name}"


def user_agent_surfaces() -> list[dict[str, object]]:
    """用户级 Cline / Roo Code 智能体目录（卸载计划与「保留」提示的唯一来源）。

    通用层（``commands/lifecycle.py``）按约定模块名 ``plugin_<host>`` 调用本钩子，
    因此不再需要 ``if name == "cline"`` 分支。目录名与展示名同样不写死：
    ``~/.<宿主 marker>/agents`` 来自插件 ``install.marker``，别名目录来自
    ``identity.aliases``（Roo Code 的 ``.roo`` / ``.roo-cline``）。
    """

    label = _PLUGIN.identity.label
    alias_dirs = tuple(f".{alias}" for alias in _PLUGIN.identity.aliases)
    surfaces: list[dict[str, object]] = []
    for path in _cline_user_agents_dirs():
        mirror = " / ".join(a.strip(".").title() for a in alias_dirs
                            if a in str(path)) or label
        surfaces.append({
            "path": path,
            "label": mirror,
            "entries": _CLINE_USER_AGENT_FILENAMES,
        })
    return surfaces


def _cline_user_agents_dirs() -> list[Path]:
    """Return all target user-level agent directories for Cline and compatible hosts (e.g. Roo Code)."""
    override = os.environ.get("BUGX_CLINE_USER_AGENTS_DIR")
    if override:
        return [Path(override).expanduser().resolve()]

    home = Path.home()
    marker = _PLUGIN.install.marker or _PLUGIN.identity.markers[0]
    dirs: list[Path] = [(home / marker / "agents").resolve()]
    # 兼容宿主（Roo Code / Roo Cline）的目录来自插件 ``identity.aliases``：
    # 别名只有在用户真的装过该宿主时才加入清扫面，避免凭空创建目录。
    for alias in _PLUGIN.identity.aliases:
        candidate = (home / f".{alias}" / "agents").resolve()
        if candidate.parent.is_dir() or candidate.is_dir():
            if candidate not in dirs:
                dirs.append(candidate)
    return dirs


def _cline_user_agents_dir() -> Path:
    """Return the primary user-level Cline agents directory (~/.cline/agents)."""
    return _cline_user_agents_dirs()[0]


def _cline_user_agent_boundary(*, locale: str | None = None) -> str:
    """用户级智能体入口共用的跨项目边界段。"""
    loc = _resolve_locale_name(locale)
    boundary = _i18n_prompt(
        "plugin.cline_user_agent_boundary",
        "## 用户级智能体边界（用户级形态专用）\n\n"
        "本入口安装为 Cline / Roo Code 等兼容宿主用户级智能体（`~/.cline/agents/`、`~/.roo/agents/`，"
        "跨目录全局可用）。"
        "当前项目尚未完成 BugX 初始化时，使用 `bugx engine path --name bugx-runtime-contract.md` "
        "读取全局协议；"
        "先运行 `bugx install --root . --agent cline --non-interactive` 安装工作区配置与 MCP，"
        "再开始审计；不得在未初始化项目上凭空执行协议步骤，也不得把本入口当成 "
        "全局引擎载荷的替代实体。\n",
        locale=loc,
    )
    return "\n\n" + boundary.rstrip() + "\n"


def _cline_user_agent_content_main(source: Path, *, locale: str | None = None) -> str:
    """用户级主审计智能体入口：宿主模板渲染 + 用户级边界段。"""
    loc = _resolve_locale_name(locale)
    entry = render_cline_agent_entry(source, locale=loc)
    return entry.rstrip("\n") + _cline_user_agent_boundary(locale=loc)


def _cline_user_agent_content_worker(*, locale: str | None = None) -> str:
    """用户级并发 worker 子智能体入口：宿主模板渲染 + 用户级边界段。"""
    loc = _resolve_locale_name(locale)
    entry = render_cline_worker_entry(locale=loc)
    return entry.rstrip("\n") + _cline_user_agent_boundary(locale=loc)


def _ensure_cline_user_agents(
    root: Path, source: Path, *, locale: str | None = None,
) -> dict[str, object]:
    """把主/worker 智能体写入 Cline 及兼容宿主（如 Roo Code）用户级 agents 目录。"""
    target_dirs = _cline_user_agents_dirs()
    main_content = _cline_user_agent_content_main(source, locale=locale)
    worker_content = _cline_user_agent_content_worker(locale=locale)
    generated = {
        "bugx.yaml": main_content,
        "bugx.md": main_content,
        "bugx-worker.yaml": worker_content,
        "bugx-worker.md": worker_content,
    }
    written: list[dict[str, object]] = []
    backups: list[Path] = []

    for agents_dir in target_dirs:
        if agents_dir.is_symlink():
            raise CliError(
                _i18n_exc(
                    "plugin.cline_symlink_agents_dir",
                    f"拒绝写入符号链接 Cline 用户级 agents 目录: {agents_dir}",
                    agents_dir=agents_dir,
                )
            )
        agents_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in generated.items():
            target = agents_dir / filename
            payload = content.encode("utf-8")
            if target.is_symlink():
                raise CliError(
                    _i18n_exc(
                        "plugin.cline_symlink_agent_refused",
                        f"拒绝覆盖符号链接 Cline 用户级智能体: {target}",
                        target=target,
                    )
                )
            if target.is_file() and target.read_bytes() == payload:
                written.append({
                    "path": str(target),
                    "status": "unchanged",
                    "backup": None,
                    "created": False,
                })
                continue
            backup = _backup_host_resource(root, _PLUGIN.name, target) if target.exists() else None
            if backup is not None:
                backups.append(backup)
            _atomic_write_global_file(target, payload)
            written.append({
                "path": str(target),
                "status": "created" if backup is None else "upgraded",
                "backup": str(backup) if backup is not None else None,
                "created": backup is None,
            })

    return {
        "dir": str(target_dirs[0]),
        "dirs": [str(d) for d in target_dirs],
        "written": written,
        "backups": backups,
    }


#: 判定「这是 BugX 自己的用户级入口」的内容标记（卸载只删自有文件）。
USER_AGENT_OWNERSHIP_MARKS = (
    "## 用户级智能体边界（用户级形态专用）",
    "## User-level Agent Boundary",
    "bugx engine path --name bugx-runtime-contract.md",
    "BugX Worker — Cline",
    "BugX — Cline",
)


def self_remove_user_agents(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """清理本宿主及兼容宿主（如 Roo Code）的用户级智能体入口（yaml/md）。"""

    from .lifecycle import _backup_tree_external

    for surface in user_agent_surfaces():
        agents_dir = surface["path"]
        display_dir = f"{agents_dir}/"
        if agents_dir.is_symlink() or not agents_dir.is_dir():
            continue
        for filename in surface["entries"]:
            entry = agents_dir / filename
            if not entry.exists() and not entry.is_symlink():
                continue
            if entry.is_symlink() or not entry.is_file():
                preserved.append(f"{display_dir}{filename}（不是安全的普通文件，未清理）")
                continue
            try:
                current = entry.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                preserved.append(f"{display_dir}{filename}（无法读取：{exc}，未清理）")
                continue
            if not any(mark in current for mark in USER_AGENT_OWNERSHIP_MARKS):
                preserved.append(
                    f"{display_dir}{filename}（内容不是 BugX 用户级入口，保留）"
                )
                continue
            if backup_root is not None:
                _backup_tree_external(
                    entry, backup_root / f"{_PLUGIN.name}-user-agent-{filename}",
                )
            try:
                entry.unlink()
            except OSError as exc:
                preserved.append(f"{display_dir}{filename}（删除失败：{exc}）")
                continue
            removed.append(f"{display_dir}{filename}（BugX 用户级智能体已移除）")
        try:
            agents_dir.rmdir()
        except OSError:
            pass
