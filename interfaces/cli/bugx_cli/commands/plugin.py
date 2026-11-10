from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..common import *
from ..host_adapters import HOST_ADAPTERS
from ..hosts.registry import (
    engine_host_label,
    engine_host_mcp_config_paths,
    engine_host_mcp_write,
    engine_host_plugins,
    sweepable_skill_adapter_names,
)
from ..hosts.template_engine import interaction_protocol
from .plugin_zcode import (
    _ensure_zcode_plugin,
    _ensure_zcode_user_agents,
    _sweep_legacy_zcode_project_entries,
    _zcode_config_path,
    _zcode_workspace_mcp_config_path,
    _zcode_plugin_agent_content,
    _zcode_plugin_boundary_section,
    _zcode_plugin_manifest,
    _zcode_plugin_worker_content,
    _zcode_storage_dir_override,
    _zcode_user_agent_boundary,
    _zcode_user_agent_content_main,
    _zcode_user_agent_content_worker,
    _zcode_user_agents_dir,
    command_plugin_zcode,
    _ensure_zcode_user_skill,
)


def _i18n_exc(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        _ensure_core_importable()
        import core.i18n as i18n
        return i18n.exception_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def _i18n_prompt(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        _ensure_core_importable()
        import core.i18n as i18n
        return i18n.prompt_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def _resolve_locale_name(locale: str | None = None) -> str:
    if locale:
        return locale
    try:
        _ensure_core_importable()
        import core.i18n as i18n
        return i18n.resolve_locale().resolved_locale
    except Exception:
        return "zh-CN"


def command_plugin(args: argparse.Namespace) -> int:
    """Discover or explicitly install a private BugX plugin."""
    root = args.root.resolve()
    if args.plugin_command == "zcode":
        return command_plugin_zcode(args)
    if args.plugin_command == "list":
        store = _plugin_store(root)
        payload = []
        for manifest_path in sorted(store.glob("*/plugin.json")):
            try:
                _, manifest = _plugin_manifest(manifest_path.parent, validate_payload=False)
            except CliError:
                continue
            installed_agent = _installed_plugin_agent(root, manifest)
            loaded = bool(installed_agent and installed_agent.is_file())
            payload.append({"name": manifest["name"], "version": manifest["version"],
                            "autoload": manifest["autoload"], "private": manifest["private"],
                            "loaded": loaded})
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
              "\n".join(f"{item['name']}\tloaded={item['loaded']}\tautoload={item['autoload']}"
                        for item in payload))
        return 0
    if args.plugin_command == "status":
        if not args.plugin_name:
            raise CliError(_i18n_exc("plugin.status_missing_name", "plugin status 必须提供插件名称"))
        plugin_path = _plugin_store(root) / args.plugin_name
        if not (plugin_path / "plugin.json").is_file():
            payload = {"name": args.plugin_name, "loaded": False, "root": str(root)}
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
                  f"{args.plugin_name}: NOT_LOADED")
            return 0
        _, manifest = _plugin_manifest(
            plugin_path, args.plugin_name, validate_payload=False,
        )
        installed_agent = _installed_plugin_agent(root, manifest)
        loaded = bool(installed_agent and installed_agent.is_file())
        payload = {
            "name": manifest["name"], "version": manifest["version"],
            "loaded": loaded, "autoload": manifest["autoload"],
            "private": manifest["private"], "root": str(root),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
              f"{manifest['name']}: {'LOADED' if loaded else 'INCOMPLETE'}")
        return 0
    if args.plugin_command == "uninstall":
        if not args.plugin_name:
            raise CliError(_i18n_exc("plugin.uninstall_missing_name", "plugin uninstall 必须提供插件名称"))
        if not args.yes:
            raise CliError(_i18n_exc("plugin.uninstall_requires_yes", "plugin uninstall 需要显式提供 --yes；卸载前会备份 BugX 文件"))
        plugin_path = _plugin_store(root) / args.plugin_name
        manifest_path = plugin_path / "plugin.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            payload = {"status": "NOT_FOUND", "name": args.plugin_name, "removed": []}
            not_found_msg = _i18n_prompt("plugin.not_found", f"[bugx] 未找到插件 {args.plugin_name}", name=args.plugin_name)
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else not_found_msg)
            return 0

        _, manifest = _plugin_manifest(plugin_path, args.plugin_name, validate_payload=False)
        removed: list[str] = []
        preserved: list[str] = []
        marker = plugin_path / ".bugx-install.json"
        marker_payload: dict[str, object] = {}
        if marker.is_file() and not marker.is_symlink():
            try:
                loaded = json.loads(marker.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    marker_payload = loaded
            except (OSError, json.JSONDecodeError):
                preserved.append(str(marker.relative_to(root)) + "（安装记录无法解析）")
        for record in marker_payload.get("files", []):
            _remove_owned_record(root, record, f"plugin-{args.plugin_name}", removed, preserved)
        manifest_record = marker_payload.get("manifest")
        if manifest_record is None:
            preserved.append(str(manifest_path.relative_to(root)) + "（旧版安装缺少哈希记录）")
        else:
            _remove_owned_record(
                root, manifest_record, f"plugin-{args.plugin_name}", removed, preserved,
            )
        if marker.is_file() and not marker.is_symlink():
            _remove_owned_record(
                root,
                {"path": _relative_project_file(root, marker), "sha256": _sha256_file(marker)},
                f"plugin-{args.plugin_name}", removed, preserved,
            )
        _remove_empty_parents(root, removed)
        payload = {
            "status": "UNINSTALLED", "name": manifest["name"],
            "removed": removed, "preserved": preserved,
        }
        msg = _i18n_prompt(
            "plugin.uninstalled",
            f"[bugx] 已卸载插件 {manifest['name']}；删除 {len(removed)} 个文件，保留 {len(preserved)} 个文件",
            name=manifest["name"], removed=len(removed), preserved=len(preserved),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else msg)
        return 0
    if args.plugin_command not in {"install", "load"}:
        raise CliError(_i18n_exc("plugin.unknown_command", f"未知插件命令: {args.plugin_command}", command=args.plugin_command))
    if not args.source:
        raise CliError(_i18n_exc("plugin.install_missing_source", "plugin install 必须提供 --source <插件目录>"))
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise CliError(_i18n_exc("plugin.source_not_found", f"插件目录不存在: {source}", source=source))
    _, manifest = _plugin_manifest(source, args.plugin_name)
    copied, skipped, manifest = _copy_plugin(
        source, root / ".bugx" / "plugins" / str(manifest["name"]) / "payload",
        args.force, project_root=root,
    )
    installed_files = _records_for_paths(
        root, [path for path in manifest.pop("_installed_targets", []) if isinstance(path, Path)]
    )
    stored_manifest = _plugin_store(root) / str(manifest["name"]) / "plugin.json"
    marker_path = stored_manifest.parent / ".bugx-install.json"
    marker_payload = {
        "schema_version": 1,
        "plugin": manifest["name"],
        "files": installed_files,
    }
    manifest_record = _file_record(root, stored_manifest)
    if manifest_record is not None:
        marker_payload["manifest"] = manifest_record
    _write_private_json(marker_path, marker_payload)
    payload = {
        "status": "LOADED", "name": manifest["name"],
        "version": manifest["version"], "copied": copied, "skipped": skipped,
        "autoload": False, "private": True,
    }
    msg = _i18n_prompt(
        "plugin.installed_on_demand",
        f"[bugx] 已按需安装插件 {manifest['name']}（写入 {copied}，保留 {skipped}）",
        name=manifest["name"], copied=copied, skipped=skipped,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else msg)
    return 0


def _prompt_for_hosts(hosts: list[dict[str, object]], root: Path) -> list[str]:
    available = [item for item in hosts if item["installed"]]
    if not available:
        raise CliError(
            _i18n_exc(
                "plugin.no_agent_detected",
                "没有检测到已安装的 AI Agent；请安装 OpenCode/Codex/Claude/ZCode/Pi/ChatGPT，"
                "或显式指定 --agent",
            )
        )
    _ensure_core_importable()
    from core.runtime import interaction
    from core.runtime import startup_environment

    project_configured = _i18n_prompt("plugin.host_project_configured", "项目已有配置")
    cmd_installed = _i18n_prompt("plugin.host_command_installed", "命令已安装")
    options = [
        (
            str(item["name"]),
            f"{item['label']}（{project_configured if item['project_marker'] else cmd_installed}）",
        )
        for item in available
    ]
    options.append(("all", _i18n_prompt("plugin.host_all_detected", "安装到全部已检测 Agent")))
    title = _i18n_prompt("plugin.choose_install_target_title", "选择 BugX Agent 安装目标")
    question_text = _i18n_prompt("plugin.choose_install_target_question", "可多选需要安装或更新的宿主 Agent")
    question = interaction.InteractionQuestion(
        question_id="agent_install_targets",
        phase="install",
        kind="select",
        title=title,
        question=question_text,
        options=tuple(
            interaction.InteractionOption(value=value, label=value, description=description)
            for value, description in options
        ),
        multiple=True,
        default=0,
    )
    environment = startup_environment.detect(root)
    result = interaction.ask(question, environment)
    if result.status != interaction.ANSWERED or not isinstance(result.value, tuple):
        raise CliError(_i18n_exc("plugin.agent_selection_not_completed", "未完成 Agent 安装目标选择；请重新运行并完成当前菜单"))
    selected = list(result.value)
    if "all" in selected:
        return [str(item["name"]) for item in available]
    return [name for name in selected if name in {str(item["name"]) for item in available}]


def _write_host_entry(
    root: Path, target: Path, content: str, force: bool, host_name: str,
) -> tuple[int, int, Path | None]:
    del force  # Existing content is updated through the recoverable backup path.
    return _write_host_resource(root, target, content.encode("utf-8"), host_name)




def _backup_uninstall_file(
    root: Path, host_name: str, target: Path, backup_dir: Path | None = None,
) -> Path:
    """Backup a file removed by uninstall; backups remain recoverable in .bugx."""
    return _backup_host_resource(root, f"uninstall-{host_name}", target, backup_dir=backup_dir)


def _remove_owned_record(
    root: Path,
    record: object,
    host_name: str,
    removed: list[str],
    preserved: list[str],
    backup_dir: Path | None = None,
    force: bool = False,
) -> None:
    """Remove a file only when its current bytes match BugX's recorded bytes (or force=True)."""
    if not isinstance(record, dict):
        return
    relative = record.get("path")
    expected = record.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        preserved.append(f"{relative or '<unknown>'}（清单缺少哈希）")
        return
    target = _safe_record_target(root, relative)
    if target.is_symlink():
        preserved.append(f"{relative}（符号链接）")
        return
    if not target.exists():
        # 入口文件已被外部删除：仍然修剪安装时产生的空目录，避免
        # .workbuddy/skills/bugx 这类空壳在卸载后残留（removed 为空、
        # status 却报 UNINSTALLED，看起来像卸干净了）。
        _remove_empty_parents(root, [relative])
        return
    if not target.is_file():
        preserved.append(f"{relative}（不是普通文件）")
        return
    try:
        current_hash = _sha256_file(target)
    except OSError as exc:
        preserved.append(f"{relative}（无法读取：{exc}）")
        return
    if current_hash != expected and not force:
        preserved.append(f"{relative}（内容已被用户修改）")
        return
    backup = _backup_uninstall_file(root, host_name, target, backup_dir)
    try:
        target.unlink()
    except OSError as exc:
        preserved.append(f"{relative}（删除失败：{exc}；备份：{backup}）")
        return
    if current_hash != expected:
        removed.append(f"{relative}（强制清理已备份）")
    else:
        removed.append(relative)


#: 按 Skill 语义清扫的适配器基线（仅**未迁移**宿主；已迁移宿主由插件
#: ``install.sweepable`` 声明）。逐宿主切片时删除对应项。
_LEGACY_SWEEPABLE_SKILL_ADAPTERS = ("claude", "workbuddy", "qoder", "trea")
_SWEEPABLE_SKILL_ADAPTERS = sweepable_skill_adapter_names(_LEGACY_SWEEPABLE_SKILL_ADAPTERS)


def _sweep_legacy_skill_entry(
    root: Path, relative: str, rendered: str, host_name: str,
) -> bool:
    """安装时清理宿主遗留安装路径（DOC-169，声明式 ``legacy_paths``）。

    仅当内容与当前模板渲染逐字一致（忽略 locale 标记）才认定为 BugX 自有：
    先备份到项目卸载备份目录再删除；用户改过的文件一律保留。
    """
    target = root / relative
    if target.is_symlink() or not target.is_file():
        return False
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if strip_locale_marker(current) != strip_locale_marker(rendered):
        return False
    _backup_uninstall_file(root, host_name, target, None)
    try:
        target.unlink()
    except OSError:
        return False
    _remove_empty_parents(root, [relative])
    return True


def _sweep_unrecorded_host_entry(
    root: Path, adapter, removed: list[str], preserved: list[str],
    backup_dir: Path | None = None,
    force: bool = False,
) -> None:
    """无安装记录的宿主入口：按当前渲染模板比对清理（0.6.79）。

    项目卸载的语义是"任何受支持的 IDE 进来都像新项目"。对 hosts.json
    没有记录的宿主（旧版本安装或登记丢失），入口文件内容与当前模板渲染
    逐字一致才认定为 BugX 自有并删除；有出入一律保留并报告，不冒充归属。
    若 force=True，则对已识别的目标进行强制备份并删除。
    """
    if adapter.name not in _SWEEPABLE_SKILL_ADAPTERS:
        return

    def _sweep_rendered(relative: str, rendered: str) -> None:
        target = root / relative
        if target.is_symlink() or not target.is_file():
            return
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            preserved.append(f"{relative}（比对失败：{exc}，保留）")
            return
        if strip_locale_marker(current) != strip_locale_marker(rendered) and not force:
            preserved.append(f"{relative}（内容与 BugX 模板不一致，保留）")
            return
        _backup_uninstall_file(root, adapter.name, target, backup_dir)
        try:
            target.unlink()
        except OSError as exc:
            preserved.append(f"{relative}（删除失败：{exc}）")
            return
        if strip_locale_marker(current) != strip_locale_marker(rendered):
            removed.append(f"{relative}（无登记遗留，强制清理已备份）")
        else:
            removed.append(f"{relative}（无登记遗留，按当前模板比对后删除）")
        _remove_empty_parents(root, [relative])

    source = global_engine_root() / "bugx.md"
    if not source.is_file():
        preserved.append(f"{adapter.install_path}（无法读取当前模板，保留）")
        return
    try:
        # 安装端用解析后的部署 locale 渲染入口（i18n.resolve_locale()
        # .resolved_locale，可被 --locale 覆盖）；无登记清扫必须用同一
        # locale 才能逐字比对，否则非英语环境下入口会被误判为“内容与
        # BugX 模板不一致”而永远无法清理（2026-09-28 修复）。
        sweep_locale = _resolve_locale_name(None)
        rendered = adapter.render(source, locale=sweep_locale)
        if adapter.name == "workbuddy":
            for relative in adapter.required_paths:
                _sweep_rendered(relative, rendered)
        else:
            _sweep_rendered(adapter.install_path, rendered)
            for req_rel in getattr(adapter, "required_paths", ()) or ():
                req_rendered = adapter.render_required_entry(req_rel, source, locale=sweep_locale)
                if req_rendered:
                    _sweep_rendered(req_rel, req_rendered)
        if adapter.name == "claude":
            from .plugin_claude import _remove_claude_project_skills_and_dist
            _remove_claude_project_skills_and_dist(root, removed, preserved, backup_dir=backup_dir)
            _remove_empty_parents(root, [adapter.install_path])
        # DOC-169：声明式遗留路径（如 codex 旧 .codex/skills）无登记遗留
        # 按对应入口模板比对清理。worker 等遗留路径必须先使用宿主提供的
        # 路径级 renderer，不能拿主 agent 模板误判为用户改写。
        for legacy_rel in getattr(adapter, "legacy_paths", ()) or ():
            legacy_rendered = adapter.render_required_entry(
                legacy_rel, source, locale=sweep_locale,
            ) or rendered
            _sweep_rendered(legacy_rel, legacy_rendered)
    except CliError as exc:
        preserved.append(f"{adapter.install_path}（比对失败：{exc}，保留）")


def _read_hosts_record(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        return {"schema_version": 1, "installed": [], "shared_files": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(_i18n_exc("plugin.hosts_manifest_unreadable", f"宿主清单无法读取，拒绝卸载: {path}: {exc}", path=path, exc=exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("installed"), list):
        raise CliError(_i18n_exc("plugin.hosts_manifest_missing_installed", f"宿主清单缺少 installed 数组，拒绝卸载: {path}", path=path))
    if not isinstance(payload.get("shared_files", []), list):
        raise CliError(_i18n_exc("plugin.hosts_manifest_shared_files_not_list", f"宿主清单的 shared_files 必须是数组: {path}", path=path))
    return payload


def _chatgpt_marketplace_name(root: Path) -> str:
    """Return a stable, collision-resistant name for this project's marketplace."""
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:10]
    return f"bugx-local-{digest}"


def _chatgpt_skill_content(
    source: Path, *, locale: str | None = None, version: str = __version__,
) -> str:
    """Mirror the canonical BugX protocol into the OpenAI Skill format.

    Uses the progressive L0 router prompt as the lightweight root definition,
    avoiding monolithic context bloat in host environments.
    """
    l0_file = source / "prompts" / "l0_router.md"
    if l0_file.is_file():
        body = l0_file.read_text(encoding="utf-8").strip()
    else:
        body = (source / "bugx-quick.md").read_text(encoding="utf-8").strip()

    loc = _resolve_locale_name(locale)
    banner = _i18n_prompt(
        "plugin.chatgpt_skill_banner",
        "这是 BugX 的 ChatGPT Skill 镜像。执行前通过 `bugx engine path --name "
        "AGENT_GUIDE.md` 和 `bugx engine path --name bugx.md` 读取全局协议；"
        "全局引擎是协议和运行时的唯一权威来源。\n\n"
        f"协议镜像版本：{version}\n",
        locale=loc,
        version=version,
    )
    from ..host_adapters import get_agent_description
    desc = get_agent_description(loc)

    content = (
        "---\n"
        "name: bugx\n"
        f"description: {desc}\n"
        f"metadata:\n  bugx_version: \"{version}\"\n"
        "---\n\n"
        "# BugX for ChatGPT\n\n"
        f"{banner.rstrip()}\n\n"
        f"{interaction_protocol('chatgpt', loc).lstrip()}\n\n"
        f"{body.rstrip()}\n"
    )
    return add_locale_marker(content, locale)


def _chatgpt_plugin_manifest(version: str = __version__) -> dict[str, object]:
    return {
        "name": CHATGPT_PLUGIN_NAME,
        "version": version,
        "description": "Autonomous code review and vulnerability hunting agent that uncovers, reproduces, and fixes deep bugs.",
        "skills": "./skills/",
        "homepage": "https://bugx.dev",
        "author": {
            "name": "BugX",
            "url": "https://bugx.dev",
        },
        "license": "BSL-1.1",
        "keywords": ["bugx", "audit", "security", "testing"],
        "interface": {
            "displayName": "BugX",
            "shortDescription": "Autonomous code review and vulnerability hunting agent that uncovers, reproduces, and fixes deep bugs.",
            "longDescription": "An autonomous agent dedicated to code review, vulnerability hunting, and automated bug fixing. Built for deep investigations, BugX steps in when you are wrapping up a feature, opening a pull request, or hunting down an elusive bug. It analyzes code logic, automatically writes test cases to uncover and reproduce hidden flaws, and delivers verified patches that resolve root causes. Always safeguarding your code quality and security.",
            "developerName": "BugX",
            "category": "Developer tools",
            # ChatGPT only exposes its native choice/confirmation surface to
            # plugins that explicitly opt into the Interactive capability.
            # The Skill text alone cannot enable that surface.
            "capabilities": ["Interactive", "Read", "Write"],
            "websiteURL": "https://bugx.dev",
            "privacyPolicyURL": "https://bugx.dev",
            "termsOfServiceURL": "https://bugx.dev",
            "composerIcon": "./assets/bugx-avatar-512.png",
            "logo": "./assets/bugx-avatar-512.png",
            "logoDark": "./assets/bugx-avatar-512.png",
            "defaultPrompt": [
                "Use BugX to find, reproduce, verify, and report bugs in this app or codebase.",
            ],
            "brandColor": "#0284c7",
            "screenshots": [],
        },
    }


def _chatgpt_plugin_icon_payload() -> bytes:
    """Load the bundled icon used by project and global ChatGPT plugins."""
    path = asset_root() / "assets" / "bugx-avatar-512.png"
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CliError(
            _i18n_exc(
                "plugin.chatgpt_icon_missing",
                f"BugX Plugin 图标资源缺失: {path}",
                path=path,
            )
        ) from exc


def _host_mcp_mirror_targets(host_name: str, root: Path) -> list[tuple[str, Path]]:
    """宿主声明的 MCP 镜像目标（插件 ``mcp_write.mirror_relpaths``）。

    只有镜像根目录（或目标文件）真实存在时才返回——不替用户凭空创建宿主目录。
    """

    facet = engine_host_mcp_write(host_name)
    if facet is None or not facet.mirror_relpaths:
        return []
    label = engine_host_label(host_name)
    targets: list[tuple[str, Path]] = []
    for relpath in facet.mirror_relpaths:
        target = root.resolve() / relpath
        marker_dir = target.parent
        if marker_dir.is_dir() or target.is_file():
            targets.append((label, target))
    return targets


def _sync_mcp_mirror(
    host_name: str,
    mirror_label: str,
    mirror_target: Path,
    desired: dict[str, dict[str, object]],
    *,
    optional_names: set[str] | None = None,
) -> None:
    """把宿主的 MCP 注册同步镜像到兼容宿主的配置文件（如 Roo Code 的 ``.roo/mcp.json``）。"""

    facet = engine_host_mcp_write(host_name)
    servers_key = facet.container_key if facet is not None else "mcpServers"
    if host_name == "opencode":
        payload = _load_json_object(mirror_target, mirror_label)
    else:
        try:
            payload = _load_json_object(mirror_target, mirror_label)
        except Exception:
            payload = {}
    container = payload
    keys = servers_key.split(".")
    for key in keys[:-1]:
        child = container.get(key)
        if child is None:
            child = {}
            container[key] = child
        elif not isinstance(child, dict):
            raise CliError(f"{mirror_label} 配置 {servers_key} 不是对象，拒绝写入: {mirror_target}")
        if host_name == "opencode" and key == "mcp" and child and "servers" not in child:
            raise CliError(
                f"OpenCode 配置使用旧的扁平 mcp 结构，拒绝镜像迁移: {mirror_target}"
            )
        container = child
    leaf_key = keys[-1]
    servers = container.get(leaf_key)
    if servers is None:
        servers = {}
        container[leaf_key] = servers
    elif not isinstance(servers, dict):
        raise CliError(f"{mirror_label} 配置 {servers_key} 不是对象，拒绝写入: {mirror_target}")

    for name in {"playwright", "postmcp"} - set(optional_names or set()):
        if _host_mcp_entry_is_current(name, servers.get(name)):
            servers.pop(name, None)

    for name, expected in desired.items():
        current = servers.get(name)
        if not isinstance(current, dict):
            servers[name] = copy.deepcopy(expected)
        elif not _host_mcp_entry_is_current(name, current):
            merged = copy.deepcopy(current)
            merged.update(copy.deepcopy(expected))
            servers[name] = merged

    mirror_target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_global_file(
        mirror_target,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )


def _sync_trae_mcp_mirror(
    root: Path,
    desired: dict[str, dict[str, object]],
    *,
    optional_names: set[str] | None = None,
) -> None:
    """当工作区存在 .trae 或 .trae-cn 目录时，将 Trea MCP 注册同步镜像写入相应目录的 mcp.json。"""
    for marker_name in (".trae", ".trae-cn"):
        marker_dir = root.resolve() / marker_name
        if not marker_dir.is_dir() and not (marker_dir / "mcp.json").is_file():
            continue
        marker_target = marker_dir / "mcp.json"
        try:
            payload = _load_json_object(marker_target, f"Trae ({marker_name})")
        except Exception:
            payload = {}
        servers_key = "mcpServers"
        servers = payload.get(servers_key)
        if servers is None or not isinstance(servers, dict):
            servers = {}
            payload[servers_key] = servers

        for name in {"playwright", "postmcp"} - set(optional_names or set()):
            if _host_mcp_entry_is_current(name, servers.get(name)):
                servers.pop(name, None)

        for name, expected in desired.items():
            current = servers.get(name)
            if not isinstance(current, dict):
                servers[name] = copy.deepcopy(expected)
            elif not _host_mcp_entry_is_current(name, current):
                merged = copy.deepcopy(current)
                merged.update(copy.deepcopy(expected))
                servers[name] = merged

        marker_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_global_file(
            marker_target,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )


#: **未迁移**宿主的 MCP 条目附加字段基线（逐宿主切片时删除对应项）。
#: 已迁移宿主由插件 ``mcp_write.entry_extra`` 声明。
_LEGACY_MCP_ENTRY_EXTRA: dict[str, dict[str, object]] = {
    "zcode": {"enabled": True},
}


def _mcp_shape_names() -> set[str]:
    """合法 MCP 配置形态：CLI 适配器宿主 + 插件宿主 + 基线别名（``codebuddy``）。"""

    names = {adapter.name for adapter in HOST_ADAPTERS}
    names.update(plugin.name for plugin in engine_host_plugins())
    names.add("codebuddy")
    return names


def _host_mcp_entry_extra(host_name: str) -> dict[str, object]:
    """宿主 server 条目的专有字段：插件 ``mcp_write.entry_extra`` 优先，否则走基线表。"""

    facet = engine_host_mcp_write(host_name)
    if facet is not None and facet.entry_extra:
        return dict(facet.entry_extra)
    return dict(_LEGACY_MCP_ENTRY_EXTRA.get(str(host_name).strip().casefold(), {}))


def _global_only_mcp_host(host_name: str) -> bool:
    """宿主是否只有**用户级/全局** MCP 作用域（插件 ``mcp_write.project_relpath is None``）。"""

    facet = engine_host_mcp_write(host_name)
    return bool(facet is not None and facet.project_relpath is None)


def _host_global_mcp_target(host_name: str, root: Path) -> Path | None:
    """全局作用域宿主的真实配置文件路径（来自插件 ``engine_run.mcp_config_paths``）。"""

    paths = engine_host_mcp_config_paths(host_name, root.resolve())
    return paths[0] if paths else None


def _host_supervisor_server_definitions(
    *, shape: str, optional_names: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    """构建 Cline / Claude Code / ZCode / Qoder / Trea / FunIDE 的最小 BugX MCP 注册。

    命令刻意使用 ``--root .``：宿主以当前工作区为 cwd 启动 stdio MCP
    server，``.`` 因此解析到用户当前打开的项目，而不是某个用户级进程目录。

    每个宿主的配置只写在当前项目中，server 命令统一以 ``--root .``
    绑定宿主打开的工作区。
    """
    if shape not in _mcp_shape_names():
        raise CliError(_i18n_exc("plugin.unknown_mcp_shape", f"未知宿主 MCP 配置形态: {shape}", shape=shape))
    entry_extra = _host_mcp_entry_extra(shape)
    result: dict[str, dict[str, object]] = {}
    for name in HOST_SUPERVISOR_MCP:
        role = HOST_SUPERVISOR_MCP_ROLES[name]
        entry: dict[str, object] = {
            # 显式声明传输类型：所有宿主注册的都是本地 stdio server。
            # 旧实现省略该字段，与 test_cli_hosts 的 MCP 契约断言
            # （type == "stdio"）不一致，属实现漏写而非测试过时。
            "type": "stdio",
            "command": "bugx",
            "args": ["mcp", "serve", "--root", ".", "--role", role],
        }
        if entry_extra:
            entry.update(copy.deepcopy(entry_extra))
        result[name] = entry
    docs_entry: dict[str, object] = {
        "type": "stdio",
        "command": "bugx",
        "args": [
            "mcp", "docs", "serve", "--root", ".",
            "--db", ".bugx/knowledge/docs.sqlite3",
        ],
    }
    if entry_extra:
        docs_entry.update(copy.deepcopy(entry_extra))
    result["bugx-docs"] = docs_entry
    for name in sorted(optional_names or set()):
        if name not in {"playwright", "postmcp"}:
            continue
        result[name] = {
            "type": "stdio",
            "command": "bugx",
            "args": ["mcp-runtime", "start", "--server", name],
        }
    return result


def _host_mcp_command_tokens(entry: object) -> list[str] | None:
    """Normalize a host MCP entry to argv without executing it."""
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    args = entry.get("args", [])
    if isinstance(command, list):
        tokens = [item for item in command if isinstance(item, str)]
        if len(tokens) != len(command):
            return None
        if args not in (None, []):
            if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
                return None
            tokens.extend(args)
        return tokens
    if not isinstance(command, str) or not command.strip():
        return None
    if args is None:
        args = []
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        return None
    return [command, *args]


_PYTHON_MODULE_LAUNCHER_PREFIX = ["-m", "bugx_cli.cli"]


def _absolute_bugx_launcher() -> tuple[str, list[str]]:
    """解析 bugx 启动入口，返回 ``(command, 前缀参数)``。

    候选按可信度排序，**绝不落盘伪造路径**（旧实现无条件取 ``sys.argv[0]``，
    在 ``python -m`` 下会写出 ``/abs/project/-m`` 这类无法启动的 command）：

    1. ``shutil.which("bugx")`` —— 标准安装；
    2. ``sys.argv[0]`` 是真实可执行文件 —— 直接调用探测脚本的场景；
    3. 与当前解释器同目录的 ``bugx`` —— venv / pip 安装；
    4. 兜底：``<当前解释器> -m bugx_cli.cli`` —— 绝对、可执行、不伪造路径。
    """

    override = shutil.which("bugx")
    if override:
        return str(Path(override).resolve()), []
    try:
        candidate = Path(sys.argv[0]).resolve()
    except OSError:
        candidate = None
    if (
        candidate is not None
        and candidate.is_file()
        and os.access(candidate, os.X_OK)
        and candidate.name not in {"-m", "-c", "-"}
    ):
        return str(candidate), []
    sibling = Path(sys.executable).parent / "bugx"
    if sibling.is_file():
        return str(sibling), []
    return str(Path(sys.executable).resolve()), list(_PYTHON_MODULE_LAUNCHER_PREFIX)


def _absolutize_server_definitions(
    desired: dict[str, dict[str, object]], root: Path,
) -> dict[str, dict[str, object]]:
    """把用户级注册的期望定义绝对化：command、``--root`` 与项目内 ``--db``。

    用户级配置（ZCode 全局、Cline 数据目录）不在工作区内，MCP server 由宿主进程
    spawn——常驻 Hub Daemon 的 cwd 不是工作区，``--root .`` 会解析到宿主 cwd
    而不是本项目，因此必须写绝对项目根。
    """
    command, prefix = _absolute_bugx_launcher()
    root_str = str(root)
    for entry in desired.values():
        args = entry.get("args")
        if entry.get("command") == "bugx":
            entry["command"] = command
            if prefix and isinstance(args, list):
                # 兜底形态：解释器 + 模块入口，前缀必须与 pinned 参数一起落盘。
                entry["args"] = args = [*prefix, *args]
        if not isinstance(args, list):
            continue
        for index, token in enumerate(args):
            if token == "--root" and index + 1 < len(args):
                args[index + 1] = root_str
            elif token == "--db" and index + 1 < len(args) \
                    and str(args[index + 1]).startswith(".bugx/"):
                args[index + 1] = str(root / str(args[index + 1]))
    return desired


def _zcode_mcp_container_key() -> str:
    """ZCode MCP 容器的点分键；来自插件 ``mcp_write.container_key``（唯一事实）。"""

    facet = engine_host_mcp_write("zcode")
    if facet is None:  # pragma: no cover - 插件缺失时 engine_host_mcp_write 已 fail-closed
        raise CliError(
            _i18n_exc("plugin.unknown_mcp_shape", "未知宿主 MCP 配置形态: zcode", shape="zcode")
        )
    return facet.container_key


def _zcode_global_server_definitions(
    root: Path, *, optional_names: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    """ZCode 用户级全局注册的期望定义：command/--root/--db 全部绝对化。"""
    desired = _host_supervisor_server_definitions(
        shape="zcode", optional_names=optional_names,
    )
    return _absolutize_server_definitions(desired, root)


def _global_server_definitions(
    host_name: str, root: Path, *, optional_names: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    """全局作用域宿主的期望定义：command/``--root``/``--db`` 全部绝对化。

    全局注册（如 ``<DATA_DIR>/settings/<host>_mcp_settings.json``）里的 stdio server
    可能由常驻守护进程（cwd 为 ``/``）spawn ⇒ ``--root .`` 不可用，必须绝对化。
    """
    desired = _host_supervisor_server_definitions(
        shape=str(host_name).strip().casefold(), optional_names=optional_names,
    )
    return _absolutize_server_definitions(desired, root)


def _same_mcp_arg(got: object, want: object, root: Path | None = None) -> bool:
    """单个参数是否等价：同字面，或同一条绝对路径的两种写法（支持相对 root 解析）。"""
    if got == want:
        return True
    if not isinstance(got, str) or not isinstance(want, str):
        return False
    try:
        got_path = (root / got) if (root and not Path(got).is_absolute()) else Path(got)
        want_path = (root / want) if (root and not Path(want).is_absolute()) else Path(want)
        return got_path.resolve() == want_path.resolve()
    except OSError:
        return False


def _launcher_kind(command: str) -> str:
    """启动器类别：``bugx``（原生启动器）/ ``python``（解释器）/ ``other:<name>``。"""
    name = Path(command).name
    if name in {"bugx", "bugx.js"}:
        return "bugx"
    if name.startswith("python") or name.startswith("pypy"):
        return "python"
    return f"other:{name}"


def _launchers_equivalent(got_cmd: str, got_args: list, want_cmd: str, want_args: list) -> bool:
    """两个启动器写法是否等价（原生启动器 ⇔ 解释器 + ``-m bugx_cli.cli``）。"""
    got_kind, want_kind = _launcher_kind(got_cmd), _launcher_kind(want_cmd)
    if got_kind == want_kind == "bugx":
        return True
    if got_kind == want_kind:
        return got_cmd == want_cmd
    if {got_kind, want_kind} != {"bugx", "python"}:
        return False
    prefix = _PYTHON_MODULE_LAUNCHER_PREFIX
    if got_kind == "python" and list(got_args[: len(prefix)]) != prefix:
        return False
    if want_kind == "python" and list(want_args[: len(prefix)]) != prefix:
        return False
    return True


def _strip_launcher_prefix(args: list) -> list:
    prefix = _PYTHON_MODULE_LAUNCHER_PREFIX
    if list(args[: len(prefix)]) == prefix:
        return list(args[len(prefix):])
    return list(args)


def _tokens_equivalent(actual: list[str], expected: list[str], root: Path | None = None) -> bool:
    """两条完整命令（含启动器）是否语义等价——**唯一**的等价判定实现。

    先比启动器类别（允许 ``python -m bugx_cli.cli`` 与原生 ``bugx`` 互为等价），
    再剥掉模块入口前缀后逐参比对，因此两种写法的 token 数量可以不同。
    """
    if not actual or not expected:
        return False
    if not _launchers_equivalent(actual[0], actual[1:], expected[0], expected[1:]):
        return False
    got_rest = _strip_launcher_prefix(actual[1:])
    want_rest = _strip_launcher_prefix(expected[1:])
    if len(got_rest) != len(want_rest):
        return False
    return all(_same_mcp_arg(got, want, root) for got, want in zip(got_rest, want_rest))


def _absolute_entry_is_current(
    name: str, entry: object, desired: dict[str, dict[str, object]],
) -> bool:
    """用户级条目的时效判定：与本次期望定义**语义**相等，禁用即过期。

    不在期望集内的名字（如未选装的 playwright/postmcp）回退到 pinned
    相对 token 判定，保留"旧 bugx 装的托管条目要被清理"的既有语义。

    ``command`` 按启动器类别比较：``/abs/bin/bugx``、PATH 名 ``bugx`` 与
    ``<python> -m bugx_cli.cli`` 是同一件事的三种写法。角色、子命令、``--root``、
    ``--db`` 仍逐项比对（路径按解析结果比较），因此「指向别的项目」依旧判过期。
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("disabled") is True or entry.get("enabled") is False:
        return False
    expected = desired.get(name)
    if expected is None:
        return _host_mcp_entry_is_current(name, entry)
    got_cmd = entry.get("command")
    want_cmd = expected.get("command")
    if not isinstance(got_cmd, str) or not isinstance(want_cmd, str):
        return False
    got_args = entry.get("args")
    want_args = expected.get("args")
    if not isinstance(got_args, list) or not isinstance(want_args, list):
        return False
    return _tokens_equivalent([got_cmd, *got_args], [want_cmd, *want_args])


def _prune_global_mcp_servers(servers: dict[str, object], current_root: Path, max_projects: int = 3) -> bool:
    """终局加固补丁 13：全局配置孤儿修剪与 LRU 上限控制 (LRU Pruning & Active Ping)。

    1. 物理存在性校验：扫描全局配置历史条目，若对应 --root 目录在磁盘上已不存在，物理删除该条目；
    2. LRU 数量硬上限：跨项目条目数限制为最多 max_projects(3) 个项目。
    """
    changed = False
    project_entries: dict[str, list[str]] = {}
    for server_name, server_cfg in list(servers.items()):
        if not str(server_name).startswith("bugx-supervisor") and not str(server_name).startswith("bugx-docs"):
            continue
        if not isinstance(server_cfg, dict):
            continue
        args = server_cfg.get("args")
        root_path = None
        if isinstance(args, list):
            for i, arg in enumerate(args):
                if arg == "--root" and i + 1 < len(args):
                    root_path = str(args[i + 1])
                    break
        if root_path:
            p = Path(root_path)
            # 物理存在性校验：若磁盘目录已不存在，立即物理剔除
            if not p.is_dir():
                servers.pop(server_name, None)
                changed = True
                continue
            norm_root = str(p.resolve())
            project_entries.setdefault(norm_root, []).append(server_name)

    current_root_norm = str(current_root.resolve())
    # 若项目数超出上限（保留当前项目 + 其他项目最多 3 个）
    if len(project_entries) > max_projects:
        removable_roots = [r for r in project_entries if r != current_root_norm]
        overflow_count = len(project_entries) - max_projects
        for root_to_drop in removable_roots[:overflow_count]:
            for entry_name in project_entries[root_to_drop]:
                servers.pop(entry_name, None)
                changed = True

    return changed


def _validate_global_supervisor_mcp(host_name: str, root: Path) -> tuple[bool, str]:
    """校验全局作用域宿主配置内的 BugX MCP 注册（指向本项目的绝对路径）。"""
    target = _host_global_mcp_target(host_name, root)
    if target is None:
        return False, _i18n_prompt(
            "plugin.global_mcp_path_unknown",
            f"宿主 {host_name} 未声明全局 MCP 配置路径，无法校验",
            host_name=host_name,
        )
    path = target
    servers = _read_host_json_servers(path, "flat")
    if not servers:
        return False, _i18n_prompt(
            "plugin.mcp_not_found",
            "未找到 bugx-supervisor host/worker 双通道 MCP 配置；请运行 bugx install --agent",
        )
    desired = _global_server_definitions(host_name, root)
    missing_or_stale = [
        name for name in HOST_ALL_MCP
        if not _absolute_entry_is_current(name, servers.get(name), desired)
    ]
    if not missing_or_stale:
        return True, _i18n_prompt(
            "plugin.mcp_registered",
            f"已在 {path} 注册并启用 bugx-supervisor host/worker 双通道与 docs 服务",
            path=path,
        )
    detail = _i18n_prompt(
        "plugin.mcp_missing_or_stale",
        f"{path}（缺失或过期：{', '.join(missing_or_stale)}）",
        path=path, items=", ".join(missing_or_stale),
    )
    return False, _i18n_prompt(
        "plugin.mcp_incomplete",
        f"MCP 配置不完整：{detail}；请运行 bugx install --agent",
        details=detail,
    )


def _validate_zcode_global_supervisor_mcp(root: Path) -> tuple[bool, str]:
    """校验 ZCode 用户级全局配置中的 BugX MCP 注册（指向本项目的绝对路径）。"""
    servers = _read_host_json_servers(_zcode_config_path(), _zcode_mcp_container_key())
    if not servers:
        return False, _i18n_prompt(
            "plugin.mcp_not_found",
            "未找到 bugx-supervisor host/worker 双通道 MCP 配置；请运行 bugx install --agent",
        )
    desired = _zcode_global_server_definitions(root)
    missing_or_stale = [
        name for name in HOST_ALL_MCP
        if not _absolute_entry_is_current(name, servers.get(name), desired)
    ]
    if not missing_or_stale:
        return True, _i18n_prompt(
            "plugin.mcp_registered",
            f"已在 {_zcode_config_path()} 注册并启用 bugx-supervisor host/worker 双通道与 docs 服务",
            path=_zcode_config_path(),
        )
    detail = _i18n_prompt(
        "plugin.mcp_missing_or_stale",
        f"{_zcode_config_path()}（缺失或过期：{', '.join(missing_or_stale)}）",
        path=_zcode_config_path(), items=", ".join(missing_or_stale),
    )
    return False, _i18n_prompt(
        "plugin.mcp_incomplete",
        f"MCP 配置不完整：{detail}；请运行 bugx install --agent",
        details=detail,
    )


def _zcode_bound_project_root() -> Path | None:
    """读取用户级全局配置中 BugX 三通道当前绑定的 ``--root``（归一化绝对路径）。

    ZCode 的 MCP 注册是用户级全局单例：三通道 ``--root`` 指向最后一次
    ``bugx install --agent zcode`` 的项目。install 需要在重写前拿到这个
    既有绑定，才能判定"切换了挖掘项目"并输出明确提示。返回 ``None``
    表示没有可比较的既有绑定（未注册、无法解析或各通道根不一致）。
    """
    servers = _read_host_json_servers(_zcode_config_path(), _zcode_mcp_container_key())
    roots: set[str] = set()
    for name in HOST_ALL_MCP:
        entry = servers.get(name)
        if not isinstance(entry, dict):
            continue
        args = entry.get("args")
        if not isinstance(args, list):
            continue
        for index, token in enumerate(args):
            if token == "--root" and index + 1 < len(args) \
                    and isinstance(args[index + 1], str):
                roots.add(args[index + 1])
                break
    if len(roots) != 1:
        return None
    (only,) = roots
    return Path(only).resolve()


def _expand_pinned_expected(expected: list[str], root: Path | None) -> list[str]:
    """把 pinned 模板里的 ``--root .`` / ``--db .bugx/...`` 展开为绝对路径。"""
    expanded = list(expected)
    if root is None:
        return expanded
    for index, token in enumerate(expanded):
        if index + 1 >= len(expanded):
            continue
        if token == "--root" and expanded[index + 1] == ".":
            expanded[index + 1] = str(root)
        elif token == "--db" and str(expanded[index + 1]).startswith(".bugx/"):
            expanded[index + 1] = str(root / str(expanded[index + 1]))
    return expanded


def _mcp_tokens_match(
    actual: list[str], expected: list[str], root: Path | None = None,
) -> bool:
    """比较宿主 MCP 条目 tokens 与 pinned 模板，按**语义**而非字面。

    合法安装有等价写法：项目级 ``bugx mcp serve --root . --role host``（pinned）与
    用户级 ``/abs/bin/bugx mcp serve --root /abs/project --role host``。旧实现做字面
    token 相等 ⇒ 任何绝对安装永远被判「missing or stale」（实测 cline/zcode 长期红项）。

    收敛后只有一条比较流水线：pinned 模板先按本次根展开，再交给
    ``_tokens_equivalent``（启动器类别 + 逐参路径等价）。角色/子命令/顺序仍严格
    相等，所以「指向别的项目」「角色写错」「db 写错」依旧判过期。
    """
    return _tokens_equivalent(actual, _expand_pinned_expected(expected, root), root)


def _host_mcp_entry_is_current(
    name: str, entry: object, root: Path | None = None,
) -> bool:
    """Check one BugX host/worker entry without treating mere key presence as ready."""
    if not isinstance(entry, dict):
        return False
    if entry.get("disabled") is True or entry.get("enabled") is False:
        return False
    expected = OPENCODE_PINNED_MCP_COMMANDS.get(name)
    if not expected:
        return False
    actual = _host_mcp_command_tokens(entry)
    return _mcp_tokens_match(actual, expected, root)


def _load_json_object(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink():
        raise CliError(_i18n_exc("plugin.mcp_symlink_config_refused", f"拒绝写入符号链接 {label} 配置: {path}", label=label, path=path))
    if not path.exists():
        return {}
    if not path.is_file():
        raise CliError(_i18n_exc("plugin.mcp_config_not_regular_file", f"{label} 配置不是普通文件，拒绝写入: {path}", label=label, path=path))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(_i18n_exc("plugin.mcp_config_unparseable", f"{label} 配置无法解析，拒绝写入: {path}: {exc}", label=label, path=path, exc=exc)) from exc
    if not isinstance(payload, dict):
        raise CliError(_i18n_exc("plugin.mcp_config_not_json_object", f"{label} 配置顶层必须是 JSON 对象，拒绝写入: {path}", label=label, path=path))
    return payload


def write_host_mcp_config(
    root: Path, host_name: str, *, optional_names: set[str] | None = None,
) -> tuple[str, Path, Path | None, bool]:
    """Idempotently register both BugX MCP channels in a host-native file.

    Return ``(status, path, backup, created)``.  Only BugX's two reserved
    names are changed; unrelated host settings and MCP servers remain intact.
    """
    root = root.resolve()
    if _global_only_mcp_host(host_name):
        # 只有用户级/全局作用域：项目内没有会被加载的 MCP 文件（插件
        # ``mcp_write.project_relpath is None`` 即此语义）。
        host_plugin = engine_host_mcp_write(host_name)
        if host_plugin is not None and host_plugin.bootstrap is not None:
            # 写入实现由插件 ``mcp_write.bootstrap`` 拥有：宿主配置不是
            # 「JSON 容器键」形态（dsh 的 cordis patch 是 YAML **list**，通用
            # 骨架既解析不了也会把 ``[]`` 判成「顶层不是对象」直接拒绝写入）。
            result = host_plugin.bootstrap(
                root,
                _global_server_definitions(
                    host_name, root, optional_names=optional_names,
                ),
            )
            written = result.get("path")
            return (
                "merged" if result.get("changed") else "unchanged",
                Path(str(written)) if written else Path(""),
                None,
                False,
            )
        target = _host_global_mcp_target(host_name, root)
        if target is None:
            raise CliError(
                _i18n_exc(
                    "plugin.global_mcp_path_unknown",
                    f"宿主 {host_name} 未声明全局 MCP 配置路径，拒绝写入",
                    host_name=host_name,
                )
            )
        payload = _load_json_object(target, engine_host_label(host_name))
        servers_key = host_plugin.container_key
        container = payload
        keys = servers_key.split(".")
        for k in keys[:-1]:
            child = container.get(k)
            if not isinstance(child, dict):
                child = {}
                container[k] = child
            container = child
        leaf_key = keys[-1]
        servers = container.get(leaf_key)
        if servers is None:
            servers = {}
            container[leaf_key] = servers
        if not isinstance(servers, dict):
            raise CliError(
                _i18n_exc(
                    "plugin.host_servers_not_object",
                    f"{engine_host_label(host_name)} 配置 {servers_key} 不是对象，拒绝写入: {target}",
                    servers_key=servers_key,
                    target=target,
                )
            )
        desired = _global_server_definitions(host_name, root, optional_names=optional_names)
    elif (
        (migrated := engine_host_mcp_write(host_name)) is not None
        and migrated.config_format == "toml"
        and migrated.bootstrap is not None
    ):
        # TOML 宿主：写入 / 项目信任 / 回执全部由插件拥有。通用 JSON 骨架在这里只会
        # 产出死配置（Codex 从不加载 ``.codex/config.json``），因此直接委托插件，
        # 不再复制一份宿主实现。
        desired = _host_supervisor_server_definitions(
            shape=str(host_name).strip().casefold(), optional_names=optional_names,
        )
        result = migrated.bootstrap(root, desired)
        relpath = migrated.project_relpath or ""
        target = Path(str(result.get("config_path") or (root / relpath)))
        raw_backup = result.get("backup")
        backup = Path(str(raw_backup)) if raw_backup else None
        changed = bool(
            result.get("config_changed")
            or result.get("trust_changed")
            or result.get("legacy_removed")
        )
        if not changed:
            return "unchanged", target, None, False
        return ("merged" if backup is not None else "created"), target, backup, bool(
            result.get("created")
        )
    elif (
        (migrated := engine_host_mcp_write(host_name)) is not None
        and migrated.project_relpath
        and not migrated.custom_writer
    ):
        # 已迁移宿主：目标路径/容器键/条目附加字段全部来自插件 ``mcp_write``。
        target = root / migrated.project_relpath
        payload = _load_json_object(target, engine_host_label(host_name))
        servers_key = migrated.container_key
        servers = payload.get(servers_key)
        if servers is None:
            servers = {}
            payload[servers_key] = servers
        if not isinstance(servers, dict):
            raise CliError(
                _i18n_exc(
                    "plugin.host_servers_not_object",
                    f"{engine_host_label(host_name)} 配置 {servers_key} 不是对象，拒绝写入: {target}",
                    servers_key=servers_key,
                    target=target,
                )
            )
        desired = _host_supervisor_server_definitions(
            shape=str(host_name).strip().casefold(), optional_names=optional_names,
        )
    elif host_name == "claude":
        target = root / ".mcp.json"
        payload = _load_json_object(target, "Claude Code")
        servers_key = "mcpServers"
        servers = payload.get(servers_key)
        if servers is None:
            servers = {}
            payload[servers_key] = servers
        if not isinstance(servers, dict):
            raise CliError(_i18n_exc("plugin.claude_servers_not_object", f"Claude Code 配置 {servers_key} 不是对象，拒绝写入: {target}", servers_key=servers_key, target=target))
        desired = _host_supervisor_server_definitions(shape="claude", optional_names=optional_names)
    elif host_name == "funide":
        target = root / ".mcp.json"
        payload = _load_json_object(target, "FunIDE")
        servers_key = "mcpServers"
        servers = payload.get(servers_key)
        if servers is None:
            servers = {}
            payload[servers_key] = servers
        if not isinstance(servers, dict):
            raise CliError(_i18n_exc("plugin.funide_servers_not_object", f"FunIDE 配置 {servers_key} 不是对象，拒绝写入: {target}", servers_key=servers_key, target=target))
        desired = _host_supervisor_server_definitions(shape="funide", optional_names=optional_names)
    elif host_name in {"pi", "dsh", "chatgpt", "workbuddy", "codebuddy"}:
        target = root / {
            "pi": ".pi/mcp.json",
            "dsh": ".dsh/mcp.json",
            "chatgpt": ".agents/mcp.json",
            "workbuddy": ".workbuddy/mcp.json",
            "codebuddy": ".codebuddy/mcp.json",
        }[host_name]
        payload = _load_json_object(target, host_name)
        servers_key = "mcpServers"
        servers = payload.setdefault(servers_key, {})
        if not isinstance(servers, dict):
            raise CliError(f"{host_name} 配置 {servers_key} 不是对象，拒绝写入: {target}")
        desired = _host_supervisor_server_definitions(
            shape="workbuddy" if host_name == "codebuddy" else host_name,
            optional_names=optional_names,
        )
    elif host_name == "qoder":
        target = root / ".qoder" / "mcp.json"
        payload = _load_json_object(target, "Qoder")
        servers_key = "mcpServers"
        servers = payload.get(servers_key)
        if servers is None:
            servers = {}
            payload[servers_key] = servers
        if not isinstance(servers, dict):
            raise CliError(_i18n_exc("plugin.qoder_servers_not_object", f"Qoder 配置 {servers_key} 不是对象，拒绝写入: {target}", servers_key=servers_key, target=target))
        desired = _host_supervisor_server_definitions(shape="qoder", optional_names=optional_names)
    elif host_name == "trea":
        # DOC-169：补全 Trea 的项目级 MCP 写入（flat 形态，与 qoder 同构）。
        target = root / ".trea" / "mcp.json"
        payload = _load_json_object(target, "Trea")
        servers_key = "mcpServers"
        servers = payload.get(servers_key)
        if servers is None:
            servers = {}
            payload[servers_key] = servers
        if not isinstance(servers, dict):
            raise CliError(_i18n_exc("plugin.trea_servers_not_object", f"Trea 配置 {servers_key} 不是对象，拒绝写入: {target}", servers_key=servers_key, target=target))
        desired = _host_supervisor_server_definitions(shape="trea", optional_names=optional_names)
    else:
        raise CliError(_i18n_exc("plugin.unsupported_mcp_host", f"不支持写入 MCP 的宿主: {host_name}", host_name=host_name))

    changed = False
    upgraded = False
    # ZCode 全局注册使用绝对 command/--root，不能拿 OPENCODE_PINNED_MCP_COMMANDS
    # 的相对 token 判定时效；按本次构造的期望定义逐字段相等比对。
    if host_name == "zcode" or _global_only_mcp_host(host_name):
        if _prune_global_mcp_servers(servers, root):
            changed = True
        # 全局作用域宿主按「与本次绝对化期望逐字段相等」判定时效，不能用相对 token。
        def _entry_is_current(name: str, current: object) -> bool:
            return _absolute_entry_is_current(name, current, desired)
    else:
        _entry_is_current = _host_mcp_entry_is_current
    optional_managed = {"playwright", "postmcp"}
    for name in optional_managed - set(optional_names or set()):
        current = servers.get(name)
        if _entry_is_current(name, current):
            servers.pop(name, None)
            changed = True
    for name, expected in desired.items():
        current = servers.get(name)
        if not isinstance(current, dict):
            servers[name] = copy.deepcopy(expected)
            changed = True
            continue
        if not _entry_is_current(name, current):
            merged = copy.deepcopy(current)
            merged.update(copy.deepcopy(expected))
            servers[name] = merged
            changed = True
            upgraded = True
    mirror_targets = _host_mcp_mirror_targets(host_name, root)
    for mirror_host, mirror_target in mirror_targets:
        _sync_mcp_mirror(host_name, mirror_host, mirror_target, desired,
                         optional_names=optional_names)
    if host_name == "trea":
        _sync_trae_mcp_mirror(root, desired, optional_names=optional_names)
    if not changed:
        return "unchanged", target, None, False

    backup = _backup_host_resource(root, host_name, target) if target.exists() else None
    _atomic_write_global_file(
        target,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    result = ("upgraded" if upgraded else "merged" if backup else "created"), target, backup, backup is None
    return result


def _restore_host_mcp_config_change(
    target: Path, backup: Path | None, created: bool,
) -> None:
    """Best-effort rollback for a project host MCP write in the install transaction."""
    try:
        if backup is not None and backup.is_file():
            _atomic_write_global_file(target, backup.read_bytes())
        elif created and target.is_file() and not target.is_symlink():
            target.unlink()
    except OSError:
        # The original install error is more useful than a secondary rollback
        # error; the backup remains available under the project .bugx tree.
        pass


def _read_host_json_servers(path: Path, shape: str) -> dict[str, object]:
    """Read one native host MCP map for doctor without treating key presence as ready."""
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if "." in shape:
        container = payload
        keys = shape.split(".")
        for key in keys[:-1]:
            child = container.get(key)
            if not isinstance(child, dict):
                container = {}
                break
            container = child
        servers = container.get(keys[-1])
    elif shape == "funide":
        # FunIDE's profile-level MCP resource is the VS Code-compatible
        # ``<User>/mcp.json`` shape: ``{"servers": {...}}``.
        servers = payload.get("servers")
    else:
        servers = payload.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _validate_host_supervisor_mcp(
    candidates: tuple[tuple[Path, str], ...],
    root: Path | None = None,
) -> tuple[bool, str]:
    """Require current, enabled host and worker entries in one native config.

    ``root`` 用于判定用户级绝对 ``--root`` 是否指向**本项目**（语义等价比对），
    缺省时只接受 pinned 相对形式。
    """
    partial: list[str] = []
    for path, shape in candidates:
        servers = _read_host_json_servers(path, shape)
        if not servers:
            continue
        missing_or_stale = [
            name for name in HOST_ALL_MCP
            if not _host_mcp_entry_is_current(name, servers.get(name), root)
        ]
        if not missing_or_stale:
            return True, _i18n_prompt("plugin.mcp_registered", f"已在 {path} 注册并启用 bugx-supervisor host/worker 双通道与 docs 服务", path=path)
        partial.append(_i18n_prompt("plugin.mcp_missing_or_stale", f"{path}（缺失或过期：{', '.join(missing_or_stale)}）", path=path, items=', '.join(missing_or_stale)))
    if partial:
        return False, _i18n_prompt("plugin.mcp_incomplete", "MCP 配置不完整：" + "；".join(partial) + "；请运行 bugx install --agent", details='；'.join(partial))
    return False, _i18n_prompt("plugin.mcp_not_found", "未找到 bugx-supervisor host/worker 双通道 MCP 配置；请运行 bugx install --agent")


def _chatgpt_global_plugin_root() -> Path:
    """Return the one BugX-owned ChatGPT Plugin source for this machine.

    ChatGPT/Codex marketplace registration is global, while the BugX project
    protocol is intentionally project-neutral and resolves ``.bugx`` from the
    active workspace.  A stable global source therefore avoids one identical
    ``bugx`` Skill being indexed once per project.
    """
    override = os.environ.get("BUGX_CHATGPT_PLUGIN_ROOT")
    if override:
        return Path(override).expanduser()
    # macOS exposes the home directory through aliases such as /var; resolve
    # only this trusted system-provided prefix, while keeping user overrides
    # lexical so their symlink boundary can still be rejected.
    return Path.home().resolve(strict=False) / ".bugx" / CHATGPT_GLOBAL_PLUGIN_DIR_NAME


def _codex_config_path() -> Path:
    """Return Codex's registry path, with an isolation override for tests."""
    override = os.environ.get("BUGX_CODEX_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".codex" / "config.toml").resolve()


def _is_bugx_marketplace_source(root: Path) -> bool:
    """Check whether a legacy local marketplace is a BugX project source.

    Missing roots are considered stale BugX registrations because the name is
    reserved by this installer.  Existing roots must still contain the
    expected BugX manifest before their registry entry may be migrated; this
    prevents a user-owned marketplace that merely resembles the name from
    being removed.
    """
    if not root.exists():
        return True
    manifest = root / "plugins" / CHATGPT_PLUGIN_NAME / ".codex-plugin" / "plugin.json"
    if not manifest.is_file() or manifest.is_symlink():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("name") == CHATGPT_PLUGIN_NAME


def _legacy_chatgpt_marketplaces() -> tuple[list[str], list[str]]:
    """Find BugX-generated marketplace registrations that can be migrated.

    The result contains registry names and non-fatal diagnostics.  Parsing is
    intentionally read-only; registry mutations are delegated to the
    supported ``codex plugin`` commands below.
    """
    path = _codex_config_path()
    if not path.is_file() or path.is_symlink():
        return [], []
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [_i18n_prompt("plugin.codex_registry_unreadable", f"无法读取 Codex 插件注册表，未迁移旧索引：{exc}", exc=exc)]
    marketplaces = payload.get("marketplaces", {})
    if not isinstance(marketplaces, dict):
        return [], []
    names: list[str] = []
    warnings: list[str] = []
    for name, metadata in marketplaces.items():
        if not isinstance(name, str) or not name.startswith(CHATGPT_LEGACY_MARKETPLACE_PREFIX):
            continue
        if not isinstance(metadata, dict) or metadata.get("source_type") != "local":
            continue
        source = metadata.get("source")
        if not isinstance(source, str) or not source.strip():
            warnings.append(_i18n_prompt("plugin.codex_migration_missing_source", f"{name}：缺少本地来源，未迁移", name=name))
            continue
        source_root = Path(source).expanduser()
        if not source_root.is_absolute():
            source_root = path.parent / source_root
        source_root = source_root.resolve(strict=False)
        if _is_bugx_marketplace_source(source_root):
            names.append(name)
        else:
            warnings.append(_i18n_prompt("plugin.codex_migration_foreign_source", f"{name}：来源不是可确认的 BugX Marketplace，保留", name=name))
    return names, warnings


def _write_chatgpt_global_plugin(
    source: Path, *, locale: str | None = None,
) -> tuple[Path, dict[str, object]]:
    """Publish the canonical global ChatGPT Plugin atomically.

    Returns ``(marketplace_path, shared_skill_status)``.  ``~/.agents/skills/
    bugx/SKILL.md`` is the shared global skill surface (Pi consumes the same
    path); its write is best-effort but must be **observable** — 2026-11-09
    审计 P3：旧实现 ``except Exception: pass`` 会把符号链接/权限拒绝静默吞掉。
    """
    root = _chatgpt_global_plugin_root()
    _assert_no_symlink_components(root)
    manifest_path = root / "plugins" / CHATGPT_PLUGIN_NAME / ".codex-plugin" / "plugin.json"
    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    existing_manifest = manifest_path
    if existing_manifest.exists():
        if existing_manifest.is_symlink() or not existing_manifest.is_file():
            raise CliError(_i18n_exc("plugin.chatgpt_manifest_not_regular_file", f"拒绝覆盖非普通的全局 ChatGPT Plugin 清单: {existing_manifest}", path=existing_manifest))
        try:
            current = json.loads(existing_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CliError(_i18n_exc("plugin.chatgpt_manifest_unparseable", f"全局 ChatGPT Plugin 清单无法解析: {existing_manifest}: {exc}", path=existing_manifest, exc=exc)) from exc
        if not isinstance(current, dict) or current.get("name") != CHATGPT_PLUGIN_NAME:
            raise CliError(_i18n_exc("plugin.chatgpt_manifest_not_bugx", f"全局 ChatGPT Plugin 清单不是 BugX，拒绝覆盖: {existing_manifest}", path=existing_manifest))
    canonical_entry = {
        "name": CHATGPT_PLUGIN_NAME,
        "source": {"source": "local", "path": "./plugins/bugx"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer tools",
    }
    marketplace = {
        "name": CHATGPT_GLOBAL_MARKETPLACE_NAME,
        "interface": {"displayName": "BugX"},
        "plugins": [canonical_entry],
    }
    if marketplace_path.exists():
        if marketplace_path.is_symlink() or not marketplace_path.is_file():
            raise CliError(_i18n_exc("plugin.chatgpt_marketplace_not_regular_file", f"拒绝覆盖非普通的全局 Marketplace 清单: {marketplace_path}", path=marketplace_path))
        try:
            current_marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CliError(
                _i18n_exc("plugin.chatgpt_marketplace_unparseable", f"全局 Marketplace 清单无法解析，拒绝覆盖: {marketplace_path}: {exc}", path=marketplace_path, exc=exc)
            ) from exc
        if not isinstance(current_marketplace, dict) \
                or current_marketplace.get("name") != CHATGPT_GLOBAL_MARKETPLACE_NAME \
                or not isinstance(current_marketplace.get("plugins"), list):
            raise CliError(
                _i18n_exc("plugin.chatgpt_marketplace_not_bugx", f"全局 Marketplace 清单不是 BugX 所有，拒绝覆盖: {marketplace_path}", path=marketplace_path)
            )
        preserved = [
            item for item in current_marketplace["plugins"]
            if not isinstance(item, dict) or item.get("name") != CHATGPT_PLUGIN_NAME
        ]
        marketplace = dict(current_marketplace)
        marketplace["plugins"] = [canonical_entry, *preserved]
    generated = {
        root / "plugins" / CHATGPT_PLUGIN_NAME / ".codex-plugin" / "plugin.json": (
            json.dumps(_chatgpt_plugin_manifest(), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8"),
        root / "plugins" / CHATGPT_PLUGIN_NAME / "assets" / "bugx-avatar-512.png": (
            _chatgpt_plugin_icon_payload()
        ),
        root / "plugins" / CHATGPT_PLUGIN_NAME / "skills" / "bugx" / "SKILL.md": (
            _chatgpt_skill_content(source, locale=locale).encode("utf-8")
        ),
        marketplace_path: (json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }
    for target, content in generated.items():
        _atomic_write_global_file(target, content)
    home = Path.home().resolve()
    agents_skill_parent = (home / ".agents" / "skills").resolve(strict=False)
    shared_skill: dict[str, object] = {
        "status": "skipped",
        "path": str((home / ".agents" / "skills" / "bugx" / "SKILL.md")),
        "error": "shared skill dir escapes user home",
    }
    if agents_skill_parent.is_relative_to(home):
        target_skill = agents_skill_parent / "bugx" / "SKILL.md"
        try:
            _assert_no_symlink_components(agents_skill_parent)
            user_agents_skill_dir = agents_skill_parent / "bugx"
            _assert_no_symlink_components(user_agents_skill_dir)
            user_agents_skill_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_global_file(
                target_skill,
                _chatgpt_skill_content(source, locale=locale).encode("utf-8"),
            )
            shared_skill = {"status": "written", "path": str(target_skill), "error": None}
        except Exception as exc:  # noqa: BLE001 - 共享面写入失败必须可观察，但不阻断主安装
            shared_skill = {
                "status": "failed",
                "path": str(target_skill),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
    return marketplace_path, shared_skill


def _run_codex_cli(codex: str, command: list[str]) -> tuple[bool, str]:
    """Run one bounded Codex registry command without exposing full output."""
    try:
        process = subprocess.run(
            [codex, *command], capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (process.stdout or process.stderr or "").strip()
    return process.returncode == 0, output[-500:]


def _migrate_legacy_chatgpt_registrations(codex: str) -> dict[str, object]:
    """Remove only BugX-owned legacy indexes before registering the singleton."""
    names, warnings = _legacy_chatgpt_marketplaces()
    removed: list[str] = []
    failures: list[str] = []
    for name in names:
        # Removing the marketplace is the essential operation.  Plugin cache
        # removal is best effort because a broken source can prevent the CLI
        # from resolving the old plugin, while the registry entry is still
        # removable.
        plugin_ok, plugin_output = _run_codex_cli(
            codex, ["plugin", "remove", f"{CHATGPT_PLUGIN_NAME}@{name}", "--json"],
        )
        marketplace_ok, marketplace_output = _run_codex_cli(
            codex, ["plugin", "marketplace", "remove", name, "--json"],
        )
        if marketplace_ok:
            removed.append(name)
        else:
            detail = marketplace_output or plugin_output or _i18n_prompt("plugin.no_output", "无输出")
            failures.append(_i18n_prompt("plugin.codex_remove_marketplace_failed", f"{name}：无法移除 Marketplace（{detail}）", name=name, detail=detail))
        if not plugin_ok and plugin_output:
            warnings.append(_i18n_prompt("plugin.codex_cache_not_cleaned", f"{name}：旧插件缓存未清理（不影响索引迁移）：{plugin_output}", name=name, output=plugin_output))
    return {"removed": removed, "warnings": warnings, "failures": failures}


def _chatgpt_marketplace_paths(root: Path) -> tuple[Path, Path]:
    """Return the logical and safe physical paths for the project catalog."""
    logical = root / ".agents" / "plugins" / "marketplace.json"
    agents_root = root / ".agents"
    if _is_reparse_point(agents_root):
        try:
            resolved_agents = agents_root.resolve(strict=True)
        except OSError as exc:
            raise CliError(_i18n_exc(
                "plugin.chatgpt_agents_target_unreadable",
                f"ChatGPT .agents 宿主目录链接无法解析，拒绝写入: {agents_root}: {exc}",
                path=agents_root, exc=exc,
            )) from exc
        if not resolved_agents.is_dir():
            raise CliError(_i18n_exc(
                "plugin.chatgpt_agents_target_not_directory",
                f"ChatGPT .agents 宿主目录链接目标不是目录，拒绝写入: {agents_root}",
                path=agents_root,
            ))
        physical = resolved_agents / "plugins" / "marketplace.json"
    else:
        physical = logical
    _assert_no_symlink_components(physical)
    return logical, physical


def _chatgpt_marketplace(root: Path) -> tuple[Path, Path, dict[str, object]]:
    """Load and validate the repo marketplace before any writes."""
    logical_path, path = _chatgpt_marketplace_paths(root)
    if path.is_symlink():
        raise CliError(_i18n_exc("plugin.chatgpt_marketplace_symlink_refused", f"拒绝写入符号链接 ChatGPT Marketplace: {logical_path}", path=logical_path))
    if not path.exists():
        payload: dict[str, object] = {
            "name": _chatgpt_marketplace_name(root),
            "interface": {"displayName": "BugX Local Plugins"},
            "plugins": [],
        }
        return logical_path, path, payload
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(_i18n_exc("plugin.chatgpt_local_marketplace_unreadable", f"ChatGPT Marketplace 无法读取，拒绝覆盖: {logical_path}: {exc}", path=logical_path, exc=exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("plugins"), list):
        raise CliError(_i18n_exc("plugin.chatgpt_local_marketplace_missing_plugins", f"ChatGPT Marketplace 缺少有效 plugins 数组，拒绝覆盖: {logical_path}", path=logical_path))
    if not isinstance(payload.get("name"), str) or not payload["name"]:
        payload["name"] = _chatgpt_marketplace_name(root)
    if not isinstance(payload.get("interface"), dict):
        payload["interface"] = {"displayName": "BugX Local Plugins"}
    for item in payload["plugins"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise CliError(_i18n_exc("plugin.chatgpt_local_marketplace_invalid_plugin", f"ChatGPT Marketplace 含有无效插件条目，拒绝覆盖: {logical_path}", path=logical_path))
        if item.get("name") == CHATGPT_PLUGIN_NAME:
            existing_source = item.get("source")
            if existing_source not in (None, {
                "source": "local", "path": CHATGPT_PLUGIN_SOURCE,
            }):
                raise CliError(
                    _i18n_exc("plugin.chatgpt_local_marketplace_foreign_bugx", "ChatGPT Marketplace 已存在非 BugX 本地来源的 bugx 插件，拒绝替换")
                )
    return logical_path, path, payload


def _ensure_chatgpt_plugin(
    root: Path, source: Path, *, locale: str | None = None,
) -> dict[str, object]:
    """Create a full repo Plugin and optionally install it through Codex CLI.

    The ChatGPT application does not expose a supported silent account-side
    install API.  We therefore prepare the project marker, publish one stable
    machine-wide BugX marketplace, and use the supported Codex plugin commands
    when available. Legacy per-project BugX marketplaces are migrated before
    registration so ChatGPT cannot index the same Skill multiple times.
    """
    root = root.resolve()
    marketplace_path, marketplace_target, marketplace = _chatgpt_marketplace(root)
    plugin_root = root / "plugins" / CHATGPT_PLUGIN_NAME
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    skill_path = plugin_root / "skills" / "bugx" / "SKILL.md"
    for path in (
        plugin_root, root / "plugins",
    ):
        if path.is_symlink():
            raise CliError(_i18n_exc("plugin.chatgpt_symlink_plugin_path", f"拒绝写入符号链接 ChatGPT Plugin 路径: {path}", path=path))

    entry = {
        "name": CHATGPT_PLUGIN_NAME,
        "source": {"source": "local", "path": CHATGPT_PLUGIN_SOURCE},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer tools",
        "isCustom": True,
        "isUserCreated": True,
    }
    entries = marketplace["plugins"]
    assert isinstance(entries, list)
    replaced = False
    merged_entries: list[object] = []
    for item in entries:
        if item.get("name") == CHATGPT_PLUGIN_NAME:
            if replaced:
                continue
            merged_entries.append(entry)
            replaced = True
        else:
            merged_entries.append(item)
    if not replaced:
        merged_entries.append(entry)
    marketplace["plugins"] = merged_entries

    plugin_version = _resolve_plugin_version(root, __version__)
    generated = {
        manifest_path: json.dumps(_chatgpt_plugin_manifest(version=plugin_version), ensure_ascii=False, indent=2) + "\n",
        plugin_root / "assets" / "bugx-avatar-512.png": _chatgpt_plugin_icon_payload(),
        skill_path: _chatgpt_skill_content(source, locale=locale, version=plugin_version),
        marketplace_target: json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n",
    }
    copied = skipped = 0
    backups: list[Path] = []
    for target, content in generated.items():
        copied_count, skipped_count, backup = _write_host_resource(
            root,
            target,
            content if isinstance(content, bytes) else content.encode("utf-8"),
            "chatgpt",
        )
        copied += copied_count
        skipped += skipped_count
        if backup is not None:
            backups.append(backup)

    # PATH 上找不到时不放弃：Codex Windows 安装器常不写 PATH，
    # 复用适配器探测的标准安装位置解析真实二进制。
    codex = shutil.which("codex")
    if not codex:
        try:
            codex = get_adapter("codex").detect(root)["executable"] or None
        except (OSError, ValueError):
            codex = None
    global_marketplace_path, shared_skill = _write_chatgpt_global_plugin(source, locale=locale)
    result: dict[str, object] = {
        "status": "prepared",
        "marketplace": str(marketplace["name"]),
        "global_marketplace": CHATGPT_GLOBAL_MARKETPLACE_NAME,
        "global_marketplace_path": str(global_marketplace_path),
        "plugin": CHATGPT_PLUGIN_NAME,
        "marketplace_path": str(marketplace_path.relative_to(root)),
        "plugin_path": str(plugin_root.relative_to(root)),
        "copied": copied,
        "skipped": skipped,
        "backups": [str(path.relative_to(root)) for path in backups],
        "shared_skill": shared_skill,
        "next_step": _i18n_prompt("plugin.chatgpt_next_step", "重启 ChatGPT 桌面应用，在 Plugins 中启用 BugX；新聊天中使用 @bugx"),
    }
    if isinstance(shared_skill, dict) and shared_skill.get("status") == "failed":
        result["shared_skill_warning"] = _i18n_prompt(
            "plugin.chatgpt_shared_skill_write_failed",
            f"共享全局 Skill 写入失败（不影响插件注册）：{shared_skill.get('error')}",
            error=shared_skill.get("error", ""),
        )
    if not codex:
        result["legacy_cleanup"] = {
            "status": "pending", "removed": [], "warnings": [], "failures": [],
        }
        return result

    legacy_cleanup = _migrate_legacy_chatgpt_registrations(codex)
    result["legacy_cleanup"] = legacy_cleanup

    commands = [
        [codex, "plugin", "marketplace", "add", str(_chatgpt_global_plugin_root()), "--json"],
        [codex, "plugin", "add",
         f"{CHATGPT_PLUGIN_NAME}@{CHATGPT_GLOBAL_MARKETPLACE_NAME}", "--json"],
    ]
    command_outputs: list[str] = []
    for command in commands:
        try:
            process = subprocess.run(
                command, cwd=root, capture_output=True, text=True,
                check=False, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["registration_warning"] = _i18n_prompt("plugin.codex_command_call_failed", f"无法调用 Codex Plugin 安装命令: {exc}", exc=exc)
            return result
        output = (process.stdout or process.stderr or "").strip()
        command_outputs.append(output[-500:])
        if process.returncode != 0:
            no_out = _i18n_prompt("plugin.no_output", "无输出")
            result["registration_warning"] = (
                _i18n_prompt("plugin.codex_command_failed", f"Codex Plugin 命令失败（{process.returncode}）：{output[-500:] or no_out}", code=process.returncode, output=output[-500:] or no_out)
            )
            result["command_outputs"] = command_outputs
            return result
    result.update({
        "status": "installed-via-codex",
        "method": "codex plugin marketplace add + codex plugin add",
        "command_outputs": command_outputs,
        "next_step": _i18n_prompt("plugin.chatgpt_next_step_codex", "重启 ChatGPT 桌面应用；如未显示，请在 Plugins → Personal 中确认 BugX"),
    })
    if legacy_cleanup.get("failures"):
        result["registration_warning"] = (
            _i18n_prompt("plugin.legacy_marketplace_partial_failure", "部分旧 BugX Marketplace 未能迁移；当前插件已注册，但仍可能存在重复索引。")
        )
    return result


def _remove_opencode_mcp_entry(
    root: Path, removed: list[str], preserved: list[str], *, backup_host: str = "opencode",
    backup_dir: Path | None = None,
) -> None:
    """Remove BugX-managed OpenCode registrations on uninstall.

    Rules:
      - The project entry .opencode/agents/bugx.md is removed when it is a
        symlink resolving to the global engine payload; foreign or user-owned
        links are preserved.
      - The global agent registration (link into ~/.config/opencode/agents)
        is removed only when it points into this project.
      - If opencode.json was fully created by BugX (its mcp.servers section contains
        only managed BugX entries and no user-owned custom keys), the whole
        file is removed after a backup.
      - If pre-existing user configuration is present, only the BugX-managed
        MCP entries are structurally removed; every user-owned MCP is preserved.
    """
    from .agent import _unlink_global_opencode_agent

    _unlink_global_opencode_agent(root)
    for filename, label in (
        ("bugx.md", ".opencode/agents/bugx.md（入口符号链接）"),
        ("bugx-worker.md", ".opencode/agents/bugx-worker.md（worker 入口符号链接）"),
    ):
        entry = root / ".opencode" / "agents" / filename
        payload = global_engine_root() / filename
        if not payload.is_file():
            payload = global_engine_root() / "contracts" / filename
        alt_payload = global_engine_root() / "contracts" / "bugx-opencode.md" if filename == "bugx.md" else payload
        if not alt_payload.is_file():
            alt_payload = global_engine_root() / "bugx-opencode.md" if filename == "bugx.md" else payload
        if entry.is_symlink():
            try:
                resolved = entry.resolve()
                if resolved in (payload.resolve(), alt_payload.resolve()):
                    entry.unlink()
                    removed.append(label)
            except OSError:
                pass
    path = root / CONFIG_NAME
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        preserved.append(f"{CONFIG_NAME}（不是安全的普通文件，未清理）")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        preserved.append(f"{CONFIG_NAME}（无法解析：{exc}）")
        return
    if not isinstance(payload, dict):
        preserved.append(f"{CONFIG_NAME}（顶层不是对象，未清理）")
        return

    mcp = payload.get("mcp")
    servers: dict[str, object] = {}
    if mcp is not None:
        if not isinstance(mcp, dict):
            preserved.append(f"{CONFIG_NAME}（mcp 不是对象，未清理）")
            return
        raw_servers = mcp.get("servers")
        if raw_servers is None and mcp:
            preserved.append(f"{CONFIG_NAME}（旧版扁平 mcp 配置未迁移、未清理）")
            return
        if raw_servers is not None and not isinstance(raw_servers, dict):
            preserved.append(f"{CONFIG_NAME}（mcp.servers 不是对象，未清理）")
            return
        if isinstance(raw_servers, dict):
            servers = raw_servers

    managed_servers: list[str] = []
    for name in OPENCODE_BUGX_MANAGED_MCP:
        entry = servers.get(name)
        expected = OPENCODE_PINNED_MCP_COMMANDS.get(name)
        actual = _host_mcp_command_tokens(entry)
        if expected and actual and _mcp_tokens_match(actual, expected, root):
            managed_servers.append(name)

    plugins = payload.get("plugins")
    managed_plugins = (
        [item for item in plugins if item == "@dietrichgebert/ponytail"]
        if isinstance(plugins, list) else []
    )
    if not managed_servers and not managed_plugins:
        return

    _backup_uninstall_file(root, backup_host, path, backup_dir)
    for name in managed_servers:
        servers.pop(name, None)
    if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
        if not servers:
            mcp.pop("servers", None)
        if not mcp:
            payload.pop("mcp", None)

    if managed_plugins:
        remaining_plugins = [
            item for item in plugins if item != "@dietrichgebert/ponytail"
        ]
        if remaining_plugins:
            payload["plugins"] = remaining_plugins
        else:
            payload.pop("plugins", None)

    user_keys = set(payload) - {"$schema"}
    if not user_keys:
        try:
            path.unlink()
            removed.append(f"{CONFIG_NAME}（已安全完全删除）")
        except OSError as exc:
            preserved.append(f"{CONFIG_NAME}（删除失败：{exc}）")
        return

    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _write_host_resource(root, path, content.encode("utf-8"), backup_host)
    details = []
    if managed_servers:
        details.append(f"MCP：{', '.join(sorted(managed_servers))}")
    if managed_plugins:
        details.append("plugins：@dietrichgebert/ponytail")
    removed.append(f"{CONFIG_NAME}（移除 BugX 添加的 {'；'.join(details)}）")


def _host_project_mcp_config_target(root: Path, host_name: str) -> tuple[Path, str] | None:
    """Return (config_path, servers_key) for the host's project-level MCP registration."""
    root = root.resolve()
    if _global_only_mcp_host(host_name):
        # 用户级/全局作用域：项目内没有会被加载的 MCP 文件（插件事实）。
        facet = engine_host_mcp_write(host_name)
        global_target = _host_global_mcp_target(host_name, root)
        if global_target is None or facet is None:
            return None
        return global_target, facet.container_key
    # 已迁移宿主：项目级路径与容器键都来自插件 ``mcp_write``（不再按名字分支）。
    # ``custom_writer`` 为真的宿主（opencode.json）由专用写入器负责，避免通用
    # JSON 骨架接管它的 permission/plugin 合并语义。
    migrated = engine_host_mcp_write(host_name)
    if migrated is not None and migrated.project_relpath and not migrated.custom_writer:
        return root / migrated.project_relpath, migrated.container_key
    if host_name == "claude":
        return root / ".mcp.json", "mcpServers"
    if host_name == "funide":
        return root / ".mcp.json", "mcpServers"
    if host_name in {"pi", "dsh", "chatgpt", "workbuddy", "codebuddy"}:
        rel_map = {
            "pi": ".pi/mcp.json",
            "dsh": ".dsh/mcp.json",
            "chatgpt": ".agents/mcp.json",
            "workbuddy": ".workbuddy/mcp.json",
            "codebuddy": ".codebuddy/mcp.json",
        }
        return root / rel_map[host_name], "mcpServers"
    if host_name == "qoder":
        return root / ".qoder" / "mcp.json", "mcpServers"
    if host_name == "trea":
        return root / ".trea" / "mcp.json", "mcpServers"
    if host_name == "opencode":
        return root / "opencode.json", "mcp.servers"
    return None


def _host_project_mcp_config_targets(root: Path, host_name: str) -> list[tuple[Path, str]]:
    """Return all candidate (config_path, servers_key) tuples for the host's project-level MCP registration."""
    root = root.resolve()
    targets: list[tuple[Path, str]] = []
    primary = _host_project_mcp_config_target(root, host_name)
    if primary:
        targets.append(primary)
    facet = engine_host_mcp_write(host_name)
    if facet is not None:
        for mirror_label, mirror_target in _host_mcp_mirror_targets(host_name, root):
            servers_key = facet.container_key
            targets.append((mirror_target, servers_key))
    return targets


def _remove_host_mcp_entry(
    root: Path,
    host_name: str,
    removed: list[str],
    preserved: list[str],
    *,
    backup_host: str | None = None,
    backup_dir: Path | None = None,
) -> None:
    """从宿主项目级配置文件中清理 BugX MCP 服务器注册（覆盖全部受支持宿主）。

    若清理后该配置文件再无其他用户配置，整文件安全删除并修剪空父目录；
    若包含用户其他配置，则仅剔除 BugX 注册条目并保留其余配置。
    """
    if host_name == "opencode":
        _remove_opencode_mcp_entry(
            root, removed, preserved,
            backup_host=backup_host or "opencode", backup_dir=backup_dir,
        )
        return

    facet = engine_host_mcp_write(host_name)
    if facet is not None and facet.remove is not None:
        # 非 JSON 形态（TOML）由插件按表块精准切除；通用 JSON 解析在这里会静默
        # 跳过整个文件，卸载报告却显示「已清理」——那是最贵的假绿。
        candidate = root / facet.project_relpath if facet.project_relpath else None
        if candidate is not None and candidate.is_file() and not candidate.is_symlink():
            _backup_uninstall_file(root, backup_host or host_name, candidate, backup_dir)
        outcome = facet.remove(root)
        removed.extend(str(item) for item in (outcome.get("removed") or ()))
        preserved.extend(str(item) for item in (outcome.get("preserved") or ()))
        return

    def _is_bugx_mcp_entry(name: str, entry: object) -> bool:
        if name in HOST_ALL_MCP or "bugx" in name.lower():
            return True
        tokens = _host_mcp_command_tokens(entry)
        if tokens and any("bugx" in str(tok).lower() for tok in tokens):
            return True
        return False

    targets = _host_project_mcp_config_targets(root, host_name)
    for path, servers_key in targets:
        if not path.exists():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)
        if path.is_symlink() or not path.is_file():
            preserved.append(f"{rel}（不是安全的普通文件，未清理）")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            preserved.append(f"{rel}（无法解析：{exc}）")
            continue
        if not isinstance(payload, dict):
            continue
        mcp_section: dict | None = None
        leaf_key = servers_key
        if "." in servers_key:
            # 嵌套容器键（如 ZCode 的 "mcp.servers"）
            keys = servers_key.split(".")
            container = payload
            for k in keys[:-1]:
                child = container.get(k)
                if not isinstance(child, dict):
                    container = None
                    break
                container = child
            leaf_key = keys[-1]
            servers = container.get(leaf_key) if isinstance(container, dict) else None
            if isinstance(container, dict):
                mcp_section = container
        else:
            servers = payload.get(servers_key)
        if not isinstance(servers, dict):
            continue

        bugx_keys = [k for k, v in servers.items() if _is_bugx_mcp_entry(k, v)]
        if not bugx_keys:
            continue

        _backup_uninstall_file(root, backup_host or host_name, path, backup_dir)
        try:
            # 用**词法**绝对路径判断，不做 symlink 解析：项目内宿主符号链接
            # （如 `.agents -> 托管目录`）仍属于项目作用域，按原有清理语义处理。
            outside_project = not Path(os.path.abspath(str(path))).is_relative_to(
                Path(os.path.abspath(str(root)))
            )
        except (OSError, ValueError):
            outside_project = True
        if outside_project:
            # 用户级全局配置永不整删（jenkins/gitea 等无关 server 与插件登记同文件，
            # Cline 的 settings 还存放非 MCP 设置）：只精准剔除 BugX 条目，
            # 空 servers 段顺手收掉。
            for k in bugx_keys:
                servers.pop(k, None)
            if mcp_section is not None and not mcp_section.get(leaf_key):
                mcp_section.pop(leaf_key, None)
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            _write_host_resource(root, path, content.encode("utf-8"), backup_host or host_name)
            removed.append(f"{rel}（移除 BugX MCP 服务器：{', '.join(sorted(bugx_keys))}）")
            continue
        user_keys = [k for k in servers if k not in bugx_keys]
        top_level_user_keys = set(payload.keys()) - {servers_key}
        if not user_keys and not top_level_user_keys:
            try:
                path.unlink()
                removed.append(f"{rel}（已安全完全删除）")
                _remove_empty_parents(root, [rel])
            except OSError as exc:
                preserved.append(f"{rel}（删除失败：{exc}）")
            continue

        for k in bugx_keys:
            servers.pop(k, None)
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _write_host_resource(root, path, content.encode("utf-8"), backup_host or host_name)
        removed.append(f"{rel}（移除 BugX MCP 服务器：{', '.join(sorted(bugx_keys))}）")


def _remove_claude_mcp_entry(
    root: Path, removed: list[str], preserved: list[str], *, backup_host: str = "claude",
    backup_dir: Path | None = None,
) -> None:
    """Remove BugX supervisor MCP channels from .mcp.json on claude uninstall."""
    _remove_host_mcp_entry(
        root, "claude", removed, preserved,
        backup_host=backup_host, backup_dir=backup_dir,
    )


def _remove_qoder_mcp_entry(
    root: Path, removed: list[str], preserved: list[str], *, backup_host: str = "qoder",
    backup_dir: Path | None = None,
) -> None:
    """Remove BugX supervisor MCP channels from .qoder/mcp.json on qoder uninstall."""
    _remove_host_mcp_entry(
        root, "qoder", removed, preserved,
        backup_host=backup_host, backup_dir=backup_dir,
    )


def _remove_dsh_project_files(
    root: Path, removed: list[str], preserved: list[str],
    records: list[object] | None = None, *, backup_host: str = "dsh",
    backup_dir: Path | None = None,
    force: bool = False,
) -> None:
    """清理 DSH 在项目根下的写入产物。

    P2-3（哈希归属检查）：
    - 有记录且哈希未变：正常清理并记入 removed；
    - 有记录但内容已被用户修改：保留文件并记入 preserved，不强行覆盖/删除；
    - 无记录：没有所有权证明，保留文件并记入 preserved；调用方若要清理必须
      先提供当前安装记录，禁止按文件名猜测归属。
    - force=True：强制清理已备份的文件。
    """
    if not records and not force:
        for candidate in (
            root / ".dsh" / "patch.yml",
            root / ".dsh" / "agents" / "bugx.md",
            root / ".dsh" / "skills" / "bugx" / "SKILL.md",
        ):
            if candidate.exists() and not candidate.is_symlink() and candidate.is_file():
                relative = candidate.relative_to(root).as_posix()
                preserved.append(f"{relative}（没有安装记录，无法证明文件归属；未删除）")
        return
    records_by_path: dict[str, str] = {}
    for record in records or []:
        if isinstance(record, dict) and isinstance(record.get("path"), str) \
                and isinstance(record.get("sha256"), str):
            records_by_path[record["path"]] = record["sha256"]
    candidates = [
        root / ".dsh" / "patch.yml",
        root / ".dsh" / "agents" / "bugx.md",
        root / ".dsh" / "skills" / "bugx" / "SKILL.md",
    ]
    for candidate in candidates:
        if not candidate.exists() or candidate.is_symlink() or not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        expected_hash = records_by_path.get(relative)
        if expected_hash is not None:
            try:
                current_hash = _sha256_file(candidate)
            except OSError as exc:
                preserved.append(f"{relative}（无法读取：{exc}）")
                continue
            if current_hash != expected_hash and not force:
                note = f"{relative}（内容已被用户修改）"
                if note not in preserved:
                    preserved.append(note)
                continue
        try:
            backup = _backup_uninstall_file(root, backup_host, candidate, backup_dir)
        except OSError as exc:
            preserved.append(f"{relative}（备份失败：{exc}）")
            continue
        try:
            candidate.unlink()
            if (expected_hash is not None and current_hash != expected_hash) or not records:
                removed.append(f"{relative}（强制清理已备份）")
            else:
                removed.append(
                    relative if expected_hash is not None
                    else f"{relative}（无记录遗留，按 BugX 渲染归属清理）"
                )
        except OSError as exc:
            preserved.append(f"{relative}（删除失败：{exc}；备份：{backup}）")


def _remove_dsh_user_preset(
    root: Path, removed: list[str], preserved: list[str], *, purge_global: bool = False,
    backup_host: str = "dsh", backup_dir: Path | None = None,
) -> None:
    """Remove the global user preset ~/.dsh/.agent-presets/bugx/ ONLY when purge_global is set.

    In a multi-project setup, other projects may rely on the shared user preset.
    Ordinary project uninstalls clean up only project-local .dsh files to prevent
    breaking other projects. Global user preset removal is performed only during
    explicit --purge-global-data --yes operations.
    """
    if not purge_global:
        preserved.append("~/.dsh/.agent-presets/bugx（全局预设保留，供其他项目复用；全局清理需 --purge-global-data）")
        return
    preset_dir = Path.home() / ".dsh" / ".agent-presets" / "bugx"
    if not preset_dir.exists():
        return
    if preset_dir.is_symlink() or not preset_dir.is_dir():
        preserved.append("~/.dsh/.agent-presets/bugx（不是安全的目录，未清理）")
        return
    try:
        # Backup user preset directory before unlinking
        for child in preset_dir.iterdir():
            if child.is_file() and not child.is_symlink():
                _backup_uninstall_file(root, backup_host, child, backup_dir)
        shutil.rmtree(preset_dir)
        removed.append("~/.dsh/.agent-presets/bugx（已显式清理全局 DSH 预设）")
    except OSError as exc:
        preserved.append(f"~/.dsh/.agent-presets/bugx（清理失败：{exc}）")


def _remove_chatgpt_marketplace_entry(
    root: Path, removed: list[str], preserved: list[str], *, backup_host: str = "chatgpt",
    backup_dir: Path | None = None,
) -> None:
    """Remove only BugX's local marketplace entry and retain every other entry."""
    try:
        logical_path, path = _chatgpt_marketplace_paths(root)
    except CliError as exc:
        preserved.append(f".agents/plugins/marketplace.json（{exc}）")
        return
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        preserved.append(f"{logical_path.relative_to(root)}（不是安全的普通文件）")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        preserved.append(f"{logical_path.relative_to(root)}（无法解析：{exc}）")
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("plugins"), list):
        preserved.append(f"{logical_path.relative_to(root)}（缺少有效 plugins 数组）")
        return
    bugx_entries = [
        item for item in payload["plugins"]
        if isinstance(item, dict) and item.get("name") == CHATGPT_PLUGIN_NAME
    ]
    owned = [
        item for item in bugx_entries
        if item.get("source") in (None, {"source": "local", "path": CHATGPT_PLUGIN_SOURCE})
    ]
    if not bugx_entries:
        return
    if len(owned) != len(bugx_entries):
        preserved.append(f"{logical_path.relative_to(root)}（存在非 BugX 的同名插件，未修改）")
        return
    updated = dict(payload)
    updated["plugins"] = [item for item in payload["plugins"] if item not in owned]
    if not updated["plugins"]:
        # BugX 是唯一条目：marketplace 只为 BugX 存在，按"新项目"语义
        # 备份后删除文件并修剪 .agents 空壳目录。
        _backup_uninstall_file(root, backup_host, path, backup_dir)
        path.unlink()
        removed.append(str(logical_path.relative_to(root)) + "（BugX 为唯一条目，文件已删除）")
        for shell in (path.parent, path.parent.parent):
            try:
                shell.rmdir()
            except OSError:
                break
        return
    content = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
    _write_host_resource(root, path, content.encode("utf-8"), backup_host)
    removed.append(str(logical_path.relative_to(root)) + "（移除 BugX 条目）")


def _external_uninstall_backup_root(root: Path) -> Path:
    override = os.environ.get("BUGX_UNINSTALL_BACKUP_ROOT")
    base = Path(override).expanduser() if override else Path.home() / ".bugx" / "backups" / "uninstall"
    project_id = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return base.resolve() / project_id / str(time.time_ns())


def _reset_forbidden_targets(root: Path, scope: str) -> list[Path]:
    """Return every path the requested reset may actually clear.

    The backup archive only has to stay outside *these* targets.  Treating
    the whole reset root as forbidden was over-conservative: on Windows the
    system tempdir lives inside the user's home, so a global reset invoked
    from the home directory could never find a valid archive location.
    """
    root = root.resolve()
    if scope == "project":
        # A project reset only moves entries under <root>/.bugx.
        return [(root / ".bugx").resolve()]
    bugx_home = (Path.home() / ".bugx").resolve()
    knowledge_root = bugx_home / "knowledge"
    knowledge_override = os.environ.get("BUGX_GLOBAL_KNOWLEDGE_ROOT")
    if knowledge_override:
        knowledge_root = Path(knowledge_override).expanduser().resolve()
    forbidden = [
        knowledge_root,
        (bugx_home / "cloud-sync").resolve(),
        (bugx_home / "identity").resolve(),
        (bugx_home / "backups").resolve(),
    ]
    try:
        forbidden.append(default_mcp_runtime_root().resolve())
    except Exception:
        pass
    # File targets (cloud-device-id, cloud-credentials.json) cannot contain a
    # directory, so only existing directories matter here.
    return [path for path in forbidden if path.is_dir()]


def _external_reset_backup_root(root: Path, scope: str) -> Path:
    """Return a reset archive outside every path that may be reset."""
    override = os.environ.get("BUGX_RESET_BACKUP_ROOT")
    base = Path(override).expanduser() if override else Path.home() / ".bugx" / "reset-backups"
    project_id = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]

    def _candidate(base_dir: Path) -> Path:
        return base_dir.resolve() / scope / project_id / str(time.time_ns())

    def _blocked(candidate: Path) -> bool:
        return any(candidate == path or candidate.is_relative_to(path)
                   for path in _reset_forbidden_targets(root, scope))

    candidate = _candidate(base)
    if _blocked(candidate):
        if override:
            raise CliError(_i18n_exc("plugin.reset_backup_invalid", f"重置备份目录不能位于待清理目录内: {candidate}", candidate=candidate))
        # The default archive lives under ~/.bugx; a project reset of the home
        # directory itself would clear it, so fall back to the system tempdir.
        # Forbidden targets are named directories under ~/.bugx, and the
        # platform tempdir is outside all of them on Windows, macOS and Linux,
        # so this fallback can no longer defeat itself (N1).
        candidate = _candidate(Path(tempfile.gettempdir()) / "bugx-reset-backups")
        if _blocked(candidate):
            raise CliError(_i18n_exc("plugin.reset_backup_invalid", f"重置备份目录不能位于待清理目录内: {candidate}", candidate=candidate))
    _assert_no_symlink_components(candidate.parent)

    return candidate


def _reset_lock_is_held(path: Path) -> bool:
    """Check a persisted lock without treating stale lock files as active."""
    if not path.exists() or path.is_symlink():
        return False
    try:
        import fcntl
        backend = fcntl
    except ImportError:
        try:
            import msvcrt
            backend = msvcrt
        except ImportError as exc:  # pragma: no cover - unsupported platform
            raise CliError("当前平台无法检查 BugX 运行锁，已拒绝重置") from exc
    try:
        handle = path.open("a+b")
    except OSError:
        return True
    try:
        try:
            if backend.__name__ == "msvcrt":
                handle.seek(0)
                if handle.tell() == 0 and handle.read(1) == b"":
                    handle.write(b"\0")
                    handle.flush()
                    handle.seek(0)
                backend.locking(handle.fileno(), backend.LK_NBLCK, 1)
            else:
                backend.flock(handle.fileno(), backend.LOCK_EX | backend.LOCK_NB)
        except OSError:
            return True
        if backend.__name__ == "msvcrt":
            handle.seek(0)
            backend.locking(handle.fileno(), backend.LK_UNLCK, 1)
        else:
            backend.flock(handle.fileno(), backend.LOCK_UN)
        return False
    finally:
        handle.close()


def _is_pid_alive_cli(pid: object) -> bool:
    if pid is None or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as err:
        import errno
        return getattr(err, "errno", None) == errno.EPERM


def _reset_active_blockers(root: Path) -> list[str]:
    """Reject reset while a lifecycle can still write project state."""
    bugx = root.resolve() / ".bugx"
    if not bugx.is_dir() or bugx.is_symlink():
        return []
    blockers: list[str] = []
    state_files = list(bugx.rglob("runtime.json"))
    for path in state_files:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append(f"{path.relative_to(root)}（无法确认状态：{exc}）")
            continue
        active = payload.get("active") is True
        state = str(payload.get("state", ""))
        if active or state in {"running", "paused", "PRE", "BASELINE_CAPTURED",
                               "AGENT_RUNNING", "POST", "DECIDE"}:
            pid = payload.get("pid")
            lock_path = path.with_name("runtime.lock")
            lock_held = _reset_lock_is_held(lock_path)
            if pid is not None and isinstance(pid, int) and not isinstance(pid, bool):
                if _is_pid_alive_cli(pid) or lock_held:
                    blockers.append(f"{path.relative_to(root)}（{state or 'active'}）")
            elif lock_held:
                blockers.append(f"{path.relative_to(root)}（{state or 'active'}）")
            else:
                try:
                    mtime_age = time.time() - path.stat().st_mtime
                except OSError:
                    mtime_age = 0
                if mtime_age < 600:
                    blockers.append(f"{path.relative_to(root)}（{state or 'active'}）")
    for path in (
        bugx / "lifecycle.lock",
        bugx / "init.lock",
        bugx / "locks" / "project.lock",
        *bugx.glob("pollings/*/lifecycles/*/state/runtime.lock"),
        *bugx.glob("pollings/*/lifecycles/*/state/audit-supervisor.lock"),
        *bugx.glob("pollings/*/lifecycles/*/state/locks/*.lock"),
    ):
        if _reset_lock_is_held(path):
            blockers.append(f"{path.relative_to(root)}（锁被占用）")
    return blockers


def _reset_entries(root: Path, *, include_project_knowledge: bool) -> list[tuple[Path, str]]:
    bugx = root.resolve() / ".bugx"
    entries: list[tuple[Path, str]] = []
    for name in RESET_PROJECT_DIRECTORIES:
        entries.append((bugx / name, f"project/{name}"))
    for name in RESET_PROJECT_FILES:
        entries.append((bugx / name, f"project/{name}"))
    # Old releases could place mutable runtime files in the compatibility
    # core directory. They are part of a project reset even though current
    # runtime files live under pollings/*/state.
    entries.extend(_legacy_core_runtime_entries(root))
    # Sync cursors, inbox and outbox are transport/runtime state, not the
    # project knowledge ledger. A normal project reset must clear them too;
    # otherwise the recreated runtime can skip remote changes or replay stale
    # operations against a new Session.
    entries.append((bugx / "cloud-sync", "project/cloud-sync"))
    if include_project_knowledge:
        entries.append((bugx / "knowledge", "project/knowledge"))
    return entries


def _move_reset_entries(entries: list[tuple[Path, str]], archive: Path) -> list[tuple[Path, Path]]:
    """Move reset targets into an external archive and roll back on failure."""
    moved: list[tuple[Path, Path]] = []
    try:
        for source, relative in entries:
            if not source.exists() and not source.is_symlink():
                continue
            archived_link_target: Path | None = None
            if source.is_symlink():
                # Only reserved legacy runtime entries are allowed here. We
                # move the link itself into the external archive and never
                # follow or delete its target (which may be the shared engine).
                if not (relative.startswith("project/core/")
                        and _is_runtime_artifact_name(source.name)):
                    raise CliError(_i18n_exc("plugin.reset_symlink_refused", f"拒绝重置符号链接运行产物: {source}", source=source))
                archived_link_target = source.resolve(strict=False)
            else:
                _assert_no_symlink_components(source)
            destination = archive / relative
            _assert_no_symlink_components(destination.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            if archived_link_target is not None:
                # A relative link keeps its old relative base after a move and
                # becomes a misleading/dangling archive entry. Recreate only
                # the link itself with its absolute resolved target; the
                # target is never read, copied, or deleted.
                destination.unlink()
                destination.symlink_to(str(archived_link_target))
            moved.append((source, destination))
    except BaseException:
        _restore_reset_entries(moved)
        raise
    return moved


def _restore_reset_entries(moved: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moved):
        if destination.exists() or destination.is_symlink():
            source.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink() or source.is_file():
                source.unlink(missing_ok=True)
            elif source.is_dir():
                shutil.rmtree(source, ignore_errors=True)
            shutil.move(str(destination), str(source))


def _clean_reset_entries(entries: list[tuple[Path, str]]) -> None:
    """Directly remove reset targets when backup is skipped."""
    for source, relative in entries:
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_symlink():
            if not (relative.startswith("project/core/")
                    and _is_runtime_artifact_name(source.name)):
                raise CliError(_i18n_exc("plugin.reset_symlink_refused", f"拒绝重置符号链接运行产物: {source}", source=source))
            source.unlink(missing_ok=True)
        elif source.is_file():
            _assert_no_symlink_components(source)
            source.unlink(missing_ok=True)
        elif source.is_dir():
            _assert_no_symlink_components(source)
            shutil.rmtree(source, ignore_errors=True)


def _bootstrap_global_knowledge(root: Path, source: Path) -> None:
    """Create a clean global ledger after an explicit global reset."""
    global_root = (Path.home() / ".bugx" / "knowledge").resolve()
    override = os.environ.get("BUGX_GLOBAL_KNOWLEDGE_ROOT")
    if override:
        global_root = Path(override).expanduser().resolve()
    _assert_no_symlink_components(global_root)
    global_root.mkdir(parents=True, exist_ok=True)
    seed = global_root / "global.json"
    db = global_root / "global.sqlite3"
    marker = global_root / "manifest.json"
    source_seed = source / "lesson-ledger.json"
    if not source_seed.is_file() or source_seed.is_symlink():
        raise CliError(_i18n_exc("plugin.lesson_rebuild_seed_missing", "重建全局错题集失败：缺少内置 lesson-ledger.json 种子"))
    shutil.copy2(source_seed, seed)
    script = _script(root, "lesson_ledger.py")
    process = subprocess.run(
        [sys.executable, str(script), "init", "--ledger", str(seed), "--json"],
        cwd=root.resolve(), capture_output=True, text=True, check=False, timeout=30,
        env=_python_environment(),
    )
    if process.returncode != 0 or not db.is_file():
        detail = (process.stderr or process.stdout).strip()[:800]
        raise CliError(_i18n_exc("plugin.lesson_rebuild_failed", f"重建全局错题集失败：{detail or 'lesson_ledger init 返回非 0'}", detail=detail or 'lesson_ledger init 返回非 0'))

    seed.chmod(0o600)
    db.chmod(0o600)
    _write_private_json(marker, {
        "schema_version": 1,
        "scope": "global",
        "authority": "sqlite",
        "read_only_for_agents": True,
        "seed": seed.name,
        "database": db.name,
        "initialized_at": time.time(),
        "reset": True,
    })


def _reset_confirmation(summary: str, *, yes: bool, dry_run: bool, root: Path | None = None) -> None:
    if yes or dry_run:
        return
    print(summary)
    _ensure_core_importable()
    from core.runtime import interaction
    from core.runtime import startup_environment

    question = interaction.InteractionQuestion(
        question_id="reset_confirmation",
        phase="reset",
        kind="confirm",
        title="确认执行 BugX 重置",
        question="重置操作将按上方范围归档并重建运行产物，是否继续？",
        options=(
            interaction.InteractionOption("yes", "确认执行", "按已展示的范围执行重置"),
            interaction.InteractionOption("no", "取消", "不修改任何数据"),
        ),
        default=1,
    )
    environment = startup_environment.detect(root or Path.cwd())
    result = interaction.ask(question, environment)
    if result.status != interaction.ANSWERED:
        raise CliError(_i18n_exc("reset.not_confirmed", "未完成重置确认，未修改任何数据"))
    if result.value != "yes":
        raise CliError(_i18n_exc("reset.cancelled", "已取消重置，未修改任何数据"))

    return
