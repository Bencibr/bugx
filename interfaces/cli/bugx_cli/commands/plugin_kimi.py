#!/usr/bin/env python3
"""interfaces/cli/bugx_cli/commands/plugin_kimi.py - Kimi Code CLI 与桌面端插件适配。"""

from __future__ import annotations

import datetime
import json
import os
import shutil
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


KIMI_PLUGIN_ICON_URL = "https://bugx.dev/brand-mark.svg"
KIMI_PLUGIN_BRAND_COLOR = "#0284c7"


def _kimi_code_home() -> Path:
    """返回 Kimi Code 当前跨平台数据根目录。

    Kimi Code 1.4.x 的桌面端与 CLI 都通过 ``KIMI_CODE_HOME``（未设置时
    ``~/.kimi-code``）解析插件目录；旧版 ``daimon-share`` 不是当前插件
    注册协议，不能作为回退路径。
    """
    configured = os.environ.get("KIMI_CODE_HOME")
    if configured is not None:
        if not configured.strip():
            raise CliError("KIMI_CODE_HOME 不能为空。")
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".kimi-code").resolve()


def _find_bugx_skill_source(root: Path, source: Path) -> Path | None:
    """定位可复用的 BugX SKILL.md 源文件。"""
    from ..infra.host_sync import find_bugx_skill_source

    return find_bugx_skill_source(root, source)


def _find_bugx_avatar_source() -> Path | None:
    """定位官方 BugX 图标源文件。"""
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


def _build_agent_bugx(locale: str | None = None) -> str:
    loc = _resolve_locale_name(locale)
    desc = get_agent_description(loc)
    is_zh = loc.startswith("zh")
    body = (
        "# BugX\n\n"
        "这是 BugX 的 Kimi Code 入口。\n\n"
        "先读取全局引擎协议：\n"
        "通过 `bugx engine path --name bugx-runtime-contract.md` 和\n"
        "`bugx engine path --name bugx-quick.md` 读取全局协议，并遵守其中的寿命、Supervisor、\n"
        "证据和报告协议。\n\n"
        "该入口只把任务交给当前 Kimi Code 会话；BugX CLI 只安装资源，不直接执行审计。\n\n"
        "## 交互适配\n"
        "- 使用结构化提示呈现交互决策。\n"
        "- 确保可访问 `bugx-supervisor` 与 `bugx-supervisor-worker` MCP 工具。\n"
    ) if is_zh else (
        "# BugX\n\n"
        "This is BugX's Kimi Code entry.\n\n"
        "Read the global engine protocol first:\n"
        "load the global protocol via `bugx engine path --name bugx-runtime-contract.md` and\n"
        "`bugx engine path --name bugx-quick.md`, and follow the lifetime, Supervisor,\n"
        "evidence, and reporting protocols in them.\n\n"
        "This entry only hands the task to the current Kimi Code session; BugX CLI installs\n"
        "resources and does not run audits directly.\n\n"
        "## Interaction Adaptation\n"
        "- Present interaction decisions with structured prompts.\n"
        "- Ensure `bugx-supervisor` and `bugx-supervisor-worker` MCP tools are accessible.\n"
    )
    rendered = (
        "---\n"
        "name: bugx\n"
        f"description: {desc}\n"
        "tools: Bash, Read, Grep, Glob, Edit, Write, AskUserQuestion\n"
        "color: cyan\n"
        "---\n\n"
        + body
    )
    return add_locale_marker(rendered, loc)


def _build_agent_worker(locale: str | None = None) -> str:
    loc = _resolve_locale_name(locale)
    desc = get_worker_description(loc)
    is_zh = loc.startswith("zh")
    body = (
        "# BugX Worker\n\n"
        "这是 BugX Worker 执行子智能体。\n\n"
        "## 执行规则\n"
        "- 遵守任务分配与租约限制。\n"
        "- 向 `bugx-supervisor-worker` 上报心跳与检查点。\n"
        "- 为确认的缺陷提供可独立执行的验证脚本（PoC）。\n"
    ) if is_zh else (
        "# BugX Worker\n\n"
        "This is the BugX Worker execution sub-agent.\n\n"
        "## Execution Rules\n"
        "- Obey task assignment and lease constraints.\n"
        "- Report heartbeats and checkpoints to `bugx-supervisor-worker`.\n"
        "- Provide standalone proof-of-concept (PoC) scripts for confirmed defects.\n"
    )
    rendered = (
        "---\n"
        "name: bugx-worker\n"
        f"description: {desc}\n"
        "tools: Bash, Read, Grep, Glob, Edit, Write\n"
        "color: green\n"
        "---\n\n"
        + body
    )
    return add_locale_marker(rendered, loc)


def _build_desktop_plugin_manifest(version: str = "0.1.3", locale: str | None = None) -> dict[str, Any]:
    """生成具备完整桌面端 UI / 徽标与 MCP 通道配置的 kimi.plugin.json。"""
    loc = _resolve_locale_name(locale)
    desc = get_agent_description(loc)
    return {
        "name": "bugx",
        "version": version,
        "description": desc,
        "homepage": "https://bugx.dev",
        "author": {
            "name": "BugX",
            "url": "https://bugx.dev",
        },
        "license": "MIT",
        "keywords": [
            "security",
            "code-audit",
            "code-review",
            "vulnerability",
            "mcp",
            "bugx",
        ],
        "agents": "./agents",
        "skills": "./skills",
        "interface": {
            "defaultLocale": "zh-CN",
            "displayName": "BugX",
            "shortDescription": "自主代码审查与自主测试智能体",
            "longDescription": "BugX 是一个具备自主代码审查与测试能力的智能体，严把代码交付关：它帮助开发人员在写完代码后自动排查隐藏的深层 Bug，减少反复排错与返工；帮助测试人员自动化运行黑盒测试、功能验证与性能测试，自动覆盖手工盲测难以覆盖的极端场景；确保代码功能符合要求、具备良好的健壮性和稳定性。让开发和测试在繁琐的工作中解放双手，大幅提高工作效率、全面提升代码质量、显著降低后期维护成本，实现科技服务于人。",
            "developerName": "BugX",
            "websiteURL": "https://bugx.dev",
            "category": "PRODUCTIVITY",
            "hostKind": "local",
            "tryPrompts": [
                "对当前项目执行自主代码审查，找出深层 Bug",
                "为当前项目执行自动化测试，验证功能与稳定性",
            ],
            "brandColor": {
                "light": KIMI_PLUGIN_BRAND_COLOR,
                "dark": KIMI_PLUGIN_BRAND_COLOR,
            },
            "iconUrl": KIMI_PLUGIN_ICON_URL,
        },
        "mcpServers": {
            "bugx-supervisor": {
                "command": "bugx",
                "args": [
                    "mcp",
                    "serve",
                    "--root",
                    ".",
                    "--role",
                    "host",
                ],
            },
            "bugx-supervisor-worker": {
                "command": "bugx",
                "args": [
                    "mcp",
                    "serve",
                    "--root",
                    ".",
                    "--role",
                    "worker",
                ],
            },
            "bugx-docs": {
                "command": "bugx",
                "args": [
                    "mcp",
                    "docs",
                    "serve",
                    "--root",
                    ".",
                    "--db",
                    ".bugx/knowledge/docs.sqlite3",
                ],
            },
        },
    }


def _ensure_kimi_plugin(
    root: Path,
    source: Path,
    *,
    locale: str | None = None,
) -> dict[str, Any]:
    """安装并激活 Kimi Code 当前插件协议中的 BugX 插件。

    Kimi Code 1.4.x 的真实注册面是 ``<KIMI_CODE_HOME>/plugins``：插件
    文件放在 ``managed/bugx``，启用状态登记在 ``installed.json``。这里
    只写入这一套当前契约；若宿主数据根不存在则立即失败，避免安装命令
    返回成功但插件实际没有注册。
    """
    loc = _resolve_locale_name(locale)
    kimi_home = _kimi_code_home()
    if not kimi_home.is_dir():
        raise CliError(
            "未找到 Kimi Code 数据目录: "
            f"{kimi_home}。请先启动一次 Kimi Code，或设置有效的 KIMI_CODE_HOME。"
        )

    plugins_root = kimi_home / "plugins"
    installed_file = plugins_root / "installed.json"
    if installed_file.is_file():
        try:
            installed_data = json.loads(installed_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError(
                f"Kimi Code 插件注册表无法解析，已阻断写入: {installed_file}"
            ) from exc
    else:
        installed_data = {"version": 1, "plugins": []}

    if not isinstance(installed_data, dict):
        raise CliError(f"Kimi Code 插件注册表顶层必须是对象: {installed_file}")
    plugins_value = installed_data.get("plugins")
    if plugins_value is None:
        plugins_list: list[dict[str, Any]] = []
        installed_data["plugins"] = plugins_list
    elif isinstance(plugins_value, list):
        plugins_list = plugins_value
    else:
        raise CliError(f"Kimi Code 插件注册表 plugins 必须是数组: {installed_file}")

    dest = plugins_root / "managed" / "bugx"
    dest.mkdir(parents=True, exist_ok=True)

    written: list[dict[str, Any]] = []

    # 2. 写入 kimi.plugin.json 与 .kimi-plugin/plugin.json
    manifest_obj = _build_desktop_plugin_manifest(locale=loc)
    manifest_bytes = (
        json.dumps(manifest_obj, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")

    manifest_file = dest / "kimi.plugin.json"
    manifest_file.write_bytes(manifest_bytes)
    written.append({
        "path": str(manifest_file),
        "status": "created",
        "backup": None,
        "created": True,
    })

    dot_plugin_dir = dest / ".kimi-plugin"
    dot_plugin_dir.mkdir(parents=True, exist_ok=True)
    (dot_plugin_dir / "plugin.json").write_bytes(manifest_bytes)

    # 3. 部署官方 Logo 图标 (icon.png / assets/icon.png)
    avatar_src = _find_bugx_avatar_source()
    if avatar_src and avatar_src.is_file():
        avatar_bytes = avatar_src.read_bytes()
        icon_dest = dest / "icon.png"
        icon_dest.write_bytes(avatar_bytes)
        written.append({
            "path": str(icon_dest),
            "status": "created",
            "backup": None,
            "created": True,
        })
        assets_dir = dest / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "icon.png").write_bytes(avatar_bytes)

    # 4. 部署 skills/ 与 agents/
    agents_dir = dest / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_main = agents_dir / "bugx.md"
    agent_main.write_text(_build_agent_bugx(locale=loc), encoding="utf-8")
    written.append({
        "path": str(agent_main),
        "status": "created",
        "backup": None,
        "created": True,
    })
    agent_worker = agents_dir / "bugx-worker.md"
    agent_worker.write_text(_build_agent_worker(locale=loc), encoding="utf-8")
    written.append({
        "path": str(agent_worker),
        "status": "created",
        "backup": None,
        "created": True,
    })

    skills_dir = dest / "skills" / "bugx"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_src = _find_bugx_skill_source(root, source)
    if skill_src and skill_src.is_file():
        target_skill = skills_dir / "SKILL.md"
        target_skill.write_bytes(skill_src.read_bytes())
        written.append({
            "path": str(target_skill),
            "status": "created",
            "backup": None,
            "created": True,
        })

    # 5. 直接写入并激活到当前 Kimi Code 的 installed.json（免用户点击安装）
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    entry = next(
        (p for p in plugins_list if isinstance(p, dict) and p.get("id") == "bugx"),
        None,
    )
    if entry is None:
        entry = {
            "id": "bugx",
            "root": str(dest),
            "source": "local-path",
            "origin": "custom",
            "enabled": True,
            "installedAt": now_iso,
            "updatedAt": now_iso,
            "originalSource": str(dest),
        }
        plugins_list.append(entry)
    else:
        entry["root"] = str(dest)
        entry["enabled"] = True
        entry["updatedAt"] = now_iso

    installed_file.parent.mkdir(parents=True, exist_ok=True)
    installed_file.write_text(
        json.dumps(installed_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    written.append({
        "path": str(installed_file),
        "status": "updated",
        "backup": None,
        "created": False,
    })

    return {
        "dir": str(dest),
        "home": str(kimi_home),
        "registry": str(installed_file),
        "written": written,
        "backups": [],
    }


def validate_install(root: Path) -> tuple[bool, str]:
    """Verify Kimi Code plugin registration, files, and MCP server channels."""
    del root
    try:
        kimi_home = _kimi_code_home()
    except CliError as exc:
        return False, str(exc)
    plugins_root = kimi_home / "plugins"
    installed_file = plugins_root / "installed.json"
    dest = plugins_root / "managed" / "bugx"
    manifest = dest / "kimi.plugin.json"
    dot_manifest = dest / ".kimi-plugin" / "plugin.json"
    agent_main = dest / "agents" / "bugx.md"
    agent_worker = dest / "agents" / "bugx-worker.md"
    skill_file = dest / "skills" / "bugx" / "SKILL.md"
    missing = [
        label for label, path in (
            ("installed.json", installed_file),
            ("kimi.plugin.json", manifest),
            (".kimi-plugin/plugin.json", dot_manifest),
            ("主智能体", agent_main),
            ("Worker 智能体", agent_worker),
            ("Skill", skill_file),
        )
        if path.is_symlink() or not path.is_file()
    ]
    if missing:
        return (
            False,
            f"Kimi Code 插件不完整（{dest} 缺少 {'、'.join(missing)}）；"
            "请运行 `bugx install --agent kimi`",
        )
    try:
        installed_data = json.loads(installed_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"Kimi Code installed.json 无法解析（{installed_file}）：{exc}"
    plugins_list = installed_data.get("plugins") if isinstance(installed_data, dict) else None
    entry = (
        next((p for p in plugins_list if isinstance(p, dict) and p.get("id") == "bugx"), None)
        if isinstance(plugins_list, list) else None
    )
    if not entry or not entry.get("enabled"):
        return False, f"Kimi Code installed.json 未启用 bugx 插件（{installed_file}）；请运行 `bugx install --agent kimi`"
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"Kimi Code kimi.plugin.json 无法解析（{manifest}）：{exc}"
    servers = manifest_data.get("mcpServers") if isinstance(manifest_data, dict) else None
    if not isinstance(servers, dict):
        return False, f"Kimi Code kimi.plugin.json 缺少 mcpServers（{manifest}）"
    missing_channels = [
        ch for ch in ("bugx-supervisor", "bugx-supervisor-worker", "bugx-docs")
        if not isinstance(servers.get(ch), dict)
    ]
    if missing_channels:
        return (
            False,
            f"Kimi Code kimi.plugin.json 缺少 {', '.join(missing_channels)}（{manifest}）；"
            "请运行 `bugx install --agent kimi`",
        )
    return True, f"Kimi Code 插件与三通道 MCP 已就绪并启用（{dest}）"
