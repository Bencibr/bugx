"""AI 宿主发现与项目级 Agent 安装适配器。

这里的适配器只负责发现宿主和写入静态 Agent 资源，不启动模型、不执行审计。
共享核心运行时放在用户级全局引擎，各宿主只保存自己的原生入口文件或
标准 Plugin 包；OpenCode 只有被明确选择时才创建 ``.opencode``。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from .hosts.registry import engine_host_plugin


def _windows_drive_roots() -> tuple[Path, ...]:
    """Return mounted Windows drive roots without recursively scanning them."""
    if os.name != "nt":
        return ()
    try:
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except (AttributeError, OSError, TypeError, ValueError):
        return ()
    return tuple(
        Path(f"{chr(ord('A') + index)}:/")
        for index in range(26)
        if mask & (1 << index)
    )


def _running_zcode_markers() -> tuple[Path, ...]:
    """Read executable paths of running ZCode processes without starting them."""
    if platform.system() == "Windows":
        command = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not command:
            return ()
        query = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "$paths=@(); "
            "$paths += @(Get-CimInstance Win32_Process -Filter \"Name = 'ZCode.exe'\" "
            "| ForEach-Object { $_.ExecutablePath }); "
            "$paths += @(Get-Process -Name ZCode | ForEach-Object { $_.Path }); "
            "$paths | Where-Object { $_ } | Select-Object -Unique"
        )
        try:
            result = subprocess.run(
                [command, "-NoProfile", "-NonInteractive", "-Command", query],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        raw_paths = result.stdout.splitlines()
        expected_names = {"zcode.exe"}
    elif platform.system() == "Linux":
        try:
            process_dirs = list(Path("/proc").iterdir())
        except OSError:
            return ()
        raw_paths = []
        expected_names = {"zcode"}
        for process_dir in process_dirs:
            if not process_dir.name.isdigit():
                continue
            try:
                raw_paths.append(os.readlink(process_dir / "exe").removesuffix(" (deleted)"))
            except OSError:
                continue
    else:
        return ()
    markers: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw.strip().strip('"'))
        if candidate.name.casefold() in expected_names and candidate.is_file():
            markers.append(candidate)
    return tuple(markers)


# 宿主入口 frontmatter 的触发文案。description 是各宿主决定"何时自动
# 启用 BugX agent"的路由依据，必须跨宿主保持一致。支持中英双语自适应，
# 非 zh 语言一律回退为规范英文。
AGENT_DESCRIPTION_EN = (
    "An autonomous agent dedicated to code review, vulnerability hunting, and automated bug fixing. "
    "Built for deep investigations, BugX steps in when you are wrapping up a feature, opening a pull request, or hunting down an elusive bug. "
    "It analyzes code logic, automatically writes test cases to uncover and reproduce hidden flaws, and delivers verified patches that resolve root causes. "
    "Always safeguarding your code quality and security."
)

AGENT_DESCRIPTION_ZH = (
    "专注于代码质量与安全的自主审查智能体。专为深层缺陷排查而生——在功能写完、提 PR 或排查疑难 Bug 时，"
    "它能深入分析代码逻辑，自动编写测试用例，挖掘、复现Bug和漏洞，生成经过验证的修复补丁。随时为您的代码质量和代码安全把关。"
)

# 并发 worker 子智能体的路由描述。OpenCode v2 通过 ``subagent`` 的 agent ID
# ``bugx-worker`` 派发；描述用于让主 Agent 在子智能体目录中识别其职责；文案与
# core/bugx-worker.md 的 frontmatter 保持一致，支持中英双语自适应。
BUGX_WORKER_DESCRIPTION_ZH = (
    "BugX 并发审查 Worker。由 BugX 主智能体并发调度，负责具体代码模块的深度审查、缺陷挖掘与可复现用例验证；"
    "仅作为子智能体工作节点协同运行。"
)

BUGX_WORKER_DESCRIPTION_EN = (
    "A concurrent audit worker dispatched in parallel by the primary BugX agent to inspect assigned modules, "
    "hunt vulnerabilities, and verify defect reproduction as a collaborative subagent."
)

# 默认导出别名（维持向后兼容性，默认中文）
AGENT_DESCRIPTION = AGENT_DESCRIPTION_ZH
BUGX_WORKER_DESCRIPTION = BUGX_WORKER_DESCRIPTION_ZH


def get_agent_description(locale: str | None = None) -> str:
    """Return the localized host agent description (fallback to en-US)."""
    loc = (locale or "").strip()
    if loc.startswith("zh"):
        return AGENT_DESCRIPTION_ZH
    return AGENT_DESCRIPTION_EN


def get_worker_description(locale: str | None = None) -> str:
    """Return the localized concurrent worker description (fallback to en-US)."""
    loc = (locale or "").strip()
    if loc.startswith("zh"):
        return BUGX_WORKER_DESCRIPTION_ZH
    return BUGX_WORKER_DESCRIPTION_EN


# ZCode 并发 worker 子智能体的模板路径与历史项目级安装位置。
# 0.7.18 起 worker 不再写项目级入口，只做用户级注册（storageRoot/agents）
# 与插件镜像；该路径保留给无登记遗留清扫（清理旧版本写入的项目级副本）。
ZCODE_WORKER_INSTALL_PATH = ".zcode/agents/bugx-worker.md"


_FALLBACK_WORKER_POINTER = (
    "\n# BugX Worker\n\nRead the authoritative protocol with "
    "`bugx engine path --name bugx-worker.md`, "
    "`bugx engine path --name bugx-runtime-contract.md`, and "
    "`bugx engine path --name bugx-quick.md`.\n"
)


def _worker_body(name: str, locale: str | None) -> str:
    """worker 入口正文：按 locale 从 `core/locales` 的 `host.entry.body.*` 取（单一真源）。"""
    try:
        import core.i18n as i18n
    except ImportError:  # pragma: no cover - 引擎目录不完整时
        return _FALLBACK_WORKER_POINTER
    return i18n.entry_text(
        f"host.entry.body.{name}", locale=locale, fallback=_FALLBACK_WORKER_POINTER,
    )

_PUBLIC_AGENT_TEMPLATE_SOURCE = "bugx engine path --name bugx.md"


def header_text(name: str, locale: str | None) -> str:
    """宿主头部正文：按 locale 从 `core/locales` 的 `host.entry.header.*` 取。

    与 `hosts.template_engine` 同源（修复 cline/qoder 此前自带一套头部与旧交互段的
    双事实源缺陷）。
    """
    try:
        import core.i18n as i18n
    except ImportError:  # pragma: no cover - 引擎目录不完整时
        return ""
    template = i18n.entry_text(f"host.entry.header.{name}", locale=locale, fallback="")
    try:
        return template.format(source=_PUBLIC_AGENT_TEMPLATE_SOURCE)
    except (KeyError, IndexError, ValueError):
        return template



ZCODE_WORKER_MCP_TOOLS = ", ".join(
    f"mcp__bugx-supervisor-worker__{name}"
    for name in (
        "bugx_supervisor_claim",
        "bugx_supervisor_worker_start",
        "bugx_supervisor_heartbeat",
        "bugx_supervisor_run_sandboxed_probe",
        "bugx_supervisor_checkpoint",
        "bugx_supervisor_begin_long_call",
        "bugx_supervisor_status",
        "bugx_supervisor_submit_finding",
    )
)


def render_zcode_worker_entry(*, locale: str | None = None) -> str:
    """生成 bugx-worker 子智能体入口（用户级注册与插件镜像共用的唯一源）。

    0.7.18 起 worker 不写项目级入口；正文按 locale 生成（`host.entry.body.zcode_worker`）。
    tools 行直接授权 Worker 通道 MCP 工具：被派子代理不再因缺 MCP 而降级
    Bash rpc 回退或被改派 general-purpose（实测每任务多 4-10 分钟）。
    """
    desc = get_worker_description(locale)
    rendered = (
        "---\n"
        "name: bugx-worker\n"
        f"description: {desc}\n"
        f"tools: Bash, Read, Grep, Glob, Edit, Write, {ZCODE_WORKER_MCP_TOOLS}\n"
        "color: cyan\n"
        "---\n"
        + _worker_body("zcode_worker", locale)
    )
    rendered = add_locale_marker(rendered, locale)
    return rendered if rendered.endswith("\n") else rendered + "\n"


CLAUDE_WORKER_INSTALL_PATH = ".claude/agents/bugx-worker.md"


def render_claude_worker_entry(*, locale: str | None = None) -> str:
    """生成 Claude Code 的 bugx-worker 子智能体入口（按 locale 生成正文）。"""
    desc = get_worker_description(locale)
    rendered = (
        "---\n"
        "name: bugx-worker\n"
        f"description: {desc}\n"
        "tools: Bash, Read, Grep, Glob, Edit, Write\n"
        "---\n"
        + _worker_body("claude_worker", locale)
    )
    rendered = add_locale_marker(rendered, locale)
    return rendered if rendered.endswith("\n") else rendered + "\n"


def render_claude_agent_entry(source: Path, *, locale: str | None = None) -> str:
    """生成 Claude Code 的 bugx 主智能体入口（项目级与用户全局级共用）。"""
    desc = get_agent_description(locale)
    rendered = (
        "---\n"
        "name: bugx\n"
        f"description: {desc}\n"
        "tools: Bash, Read, Grep, Glob, Edit, Write, AskUserQuestion\n"
        "---\n\n"
        + header_text("claude", locale)
        + interaction_protocol("claude", locale)
    )
    rendered = add_locale_marker(rendered, locale)
    return rendered if rendered.endswith("\n") else rendered + "\n"



QODER_WORKER_INSTALL_PATH = ".qoder/agents/bugx-worker.md"




def add_locale_marker(content: str, locale: str | None) -> str:
    """Record the selected locale without invalidating YAML frontmatter.

    Several host skill formats require ``---`` to be the first bytes in the
    file.  The marker is intentionally placed after that metadata block so it
    remains an inert HTML comment while preserving host discovery semantics.
    """
    if not locale:
        return content
    content = strip_locale_marker(content)
    normalized = str(locale).strip() or "en-US"
    marker = f"<!-- bugx-locale: {normalized} -->\n"
    if content.startswith("---\n"):
        closing = content.find("\n---", 4)
        if closing >= 0:
            end = closing + len("\n---")
            # Keep the original newline after the metadata delimiter; insert
            # the marker between that delimiter and the existing body.
            body_start = end + 1 if content[end:end + 1] == "\n" else end
            return content[:end] + "\n" + marker + content[body_start:]
    return marker + content


def strip_locale_marker(content: str) -> str:
    """Remove the generated locale comment for ownership comparisons."""
    return re.sub(r"^<!-- bugx-locale: [^>]+ -->\n?", "", content, count=1, flags=re.MULTILINE)


def read_locale_marker(content: str) -> str | None:
    """Extract the locale recorded in a generated host file, if present."""
    match = re.search(r"^<!-- bugx-locale: ([^>]+) -->", content, flags=re.MULTILINE)
    return match.group(1).strip() if match else None



def render_cline_worker_entry(*, locale: str | None = None) -> str:
    """生成 Cline 的 bugx-worker 子智能体入口（项目级与用户全局级共用）。"""
    desc = get_worker_description(locale)
    rendered = (
        "---\n"
        "name: bugx-worker\n"
        f"description: {desc}\n"
        "tools: Bash, Read, Grep, Glob, Edit, Write\n"
        "mcpServers:\n"
        "  - bugx-supervisor-worker\n"
        "---\n"
        + _worker_body("cline_worker", locale)
    )
    rendered = add_locale_marker(rendered, locale)
    return rendered if rendered.endswith("\n") else rendered + "\n"


def render_cline_agent_entry(source: Path, *, locale: str | None = None) -> str:
    """生成 Cline 的 bugx 主智能体入口（项目级与用户全局级共用）。

    正文（头部 + 交互段）统一取自 `hosts.template_engine` 的规范实现，
    与其余宿主同源（修复"同一启动策略在不同宿主表述不同"的双事实源缺陷）。
    """
    desc = get_agent_description(locale)
    rendered = (
        "---\n"
        "name: bugx\n"
        f"description: {desc}\n"
        "tools: Bash, Read, Grep, Glob, Edit, Write, ask_question\n"
        "skills:\n"
        "  - bugx\n"
        "mcpServers:\n"
        "  - bugx-supervisor\n"
        "---\n\n"
        + header_text(_adapter_host_name(ClineAdapter), locale)
        + interaction_protocol(_adapter_host_name(ClineAdapter), locale)
    )
    rendered = add_locale_marker(rendered, locale)
    return rendered if rendered.endswith("\n") else rendered + "\n"



def render_qoder_worker_entry(*, locale: str | None = None) -> str:
    """生成 Qoder 的 bugx-worker 子智能体入口（按 locale 生成正文）。"""
    desc = get_worker_description(locale)
    rendered = (
        "---\n"
        "name: bugx-worker\n"
        f"description: {desc}\n"
        "tools: Bash, Read, Grep, Glob, Edit, Write\n"
        "mcpServers:\n"
        "  - bugx-supervisor-worker\n"
        "---\n"
        + _worker_body("qoder_worker", locale)
    )
    rendered = add_locale_marker(rendered, locale)
    return rendered if rendered.endswith("\n") else rendered + "\n"


def render_qoder_agent_entry(source: Path, *, locale: str | None = None) -> str:
    """生成 Qoder 的 bugx 主智能体入口（项目级与用户全局级共用）。

    正文统一取自规范实现（同 ``render_cline_agent_entry``）：修复 Qoder 此前
    缺少专属交互段、且启动策略措辞与其余宿主不一致的缺陷。
    """
    desc = get_agent_description(locale)
    rendered = (
        "---\n"
        "name: bugx\n"
        f"description: {desc}\n"
        "tools: Bash, Read, Grep, Glob, Edit, Write, AskUserQuestion\n"
        "skills:\n"
        "  - bugx\n"
        "mcpServers:\n"
        "  - bugx-supervisor\n"
        "---\n\n"
        + header_text("qoder", locale)
        + interaction_protocol("qoder", locale)
    )
    rendered = add_locale_marker(rendered, locale)
    return rendered if rendered.endswith("\n") else rendered + "\n"


from .hosts import (
    HOST_COMPONENTS,
    get_component,
    render_host_entry,
    interaction_protocol,
)

_TOOLING_MULTI_SELECT = "Startup policies are resolved from the layered project configuration."
INTERACTION_CONTRACTS = {
    comp.name: comp.interaction.to_dict()
    for comp in HOST_COMPONENTS
}

# ChatGPT Plugin 由 cli.py 从共享核心动态生成，公开该段宿主规则以保持生成路径与
# 直接宿主入口一致。


def _chatgpt_app_markers() -> tuple[Path, ...]:
    """Return well-known ChatGPT desktop application locations.

    ChatGPT is a GUI host and normally has no stable ``chatgpt`` executable on
    PATH.  Detection is deliberately read-only and conservative: a project
    marketplace marker is also accepted so a previously prepared installation
    remains diagnosable on machines where the GUI app is not discoverable.
    """
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Windows":
        for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            value = os.environ.get(variable)
            if value:
                base = Path(value)
                markers.extend((base / "ChatGPT" / "ChatGPT.exe", base / "OpenAI" / "ChatGPT.exe"))
        # Microsoft Store 执行别名与用户级 Electron 安装。
        local = os.environ.get("LOCALAPPDATA")
        if local:
            base = Path(local)
            markers.extend((
                base / "Microsoft" / "WindowsApps" / "ChatGPT.exe",
                base / "Programs" / "ChatGPT" / "ChatGPT.exe",
            ))
    elif platform.system() == "Darwin":
        markers.extend((Path("/Applications/ChatGPT.app"), home / "Applications" / "ChatGPT.app"))
    else:
        markers.append(home / ".local" / "share" / "applications" / "chatgpt.desktop")
    msix = _msix_openai_location()
    if msix is not None and msix.name == "ChatGPT.exe":
        markers.append(msix)
    return tuple(markers)


# OpenAI 的 Microsoft Store 包（MSIX）在同一包身份 OpenAI.Codex 下同时发布
# ChatGPT.exe 与 Codex.exe。WindowsApps 目录受 ACL 保护、无法列目录，
# 但解析出的完整路径可以直接访问，因此只能从 AppX 目录（Get-AppxPackage）
# 拿安装位置，而不是文件系统通配。
_MSIX_OPENAI_CACHE: dict[str, Path | None] = {}


def _reset_msix_cache() -> None:
    _MSIX_OPENAI_CACHE.clear()


def _msix_openai_location() -> Path | None:
    """Resolve the OpenAI.Codex MSIX install location (ChatGPT/Codex app)."""
    if platform.system() != "Windows":
        return None
    if "location" in _MSIX_OPENAI_CACHE:
        return _MSIX_OPENAI_CACHE["location"]
    location: Path | None = None
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage -Name 'OpenAI.Codex').InstallLocation"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        candidate = (result.stdout or "").strip()
        if result.returncode == 0 and candidate:
            root = Path(candidate)
            # 包内布局：可执行文件在 InstallLocation 的 app/ 子目录下，
            # 也兼容直接平铺在根目录的旧布局。
            subdirs = (root, root / "app")
            for name in ("ChatGPT.exe", "Codex.exe"):
                for base in subdirs:
                    exe = base / name
                    try:
                        if exe.is_file():
                            location = exe
                            break
                    except OSError:
                        continue
                if location is not None:
                    break
    except (OSError, ValueError):
        location = None
    _MSIX_OPENAI_CACHE["location"] = location
    return location


def _codex_app_markers() -> tuple[Path, ...]:
    """Return well-known OpenAI Codex CLI locations missed by PATH lookup.

    The Codex Windows installer (and the ChatGPT desktop-managed install)
    drops ``codex.exe`` under ``%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\``
    without touching PATH, so ``shutil.which("codex")`` alone reports
    NOT_FOUND on working installs.  POSIX installs live on PATH (homebrew,
    ~/.local/bin), so nothing extra is needed there.
    """
    markers: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "OpenAI" / "Codex"
        versioned = base / "bin"
        try:
            markers.extend(sorted(versioned.glob("*/codex.exe")))
        except OSError:
            pass
        markers.append(base / "bin" / "codex.exe")
        markers.append(base / "codex.exe")
        markers.append(Path(local) / "Microsoft" / "WindowsApps" / "codex.exe")
    msix = _msix_openai_location()
    if msix is not None and msix.name == "Codex.exe":
        markers.append(msix)
    return tuple(markers)


def _zcode_app_markers() -> tuple[Path, ...]:
    """Return well-known ZCode desktop application locations.

    ZCode ships primarily as a desktop app and usually has no stable
    ``zcode`` executable on PATH, so detection needs the same conservative
    app-marker fallback as ChatGPT.
    """
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Darwin":
        markers.extend((
            Path("/Applications/ZCode.app"),
            home / "Applications" / "ZCode.app",
        ))
    elif os.name != "nt":
        markers.append(home / ".local" / "share" / "applications" / "zcode.desktop")
    bases: list[Path] = []
    for variable in (
        "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    ):
        value = os.environ.get(variable)
        if value:
            bases.append(Path(value))
    bases.extend(_windows_drive_roots())
    for base in bases:
        markers.extend((
            base / "ZCode" / "ZCode.exe",
            base / "Z.ai" / "ZCode.exe",
            base / "Z.ai" / "ZCode" / "ZCode.exe",
            base / "Programs" / "ZCode" / "ZCode.exe",
            base / "Programs" / "Z.ai" / "ZCode" / "ZCode.exe",
            base / "Program Files" / "ZCode" / "ZCode.exe",
            base / "Program Files" / "zcode" / "ZCode.exe",
            base / "Program Files" / "Z.ai" / "ZCode" / "ZCode.exe",
            base / "Program Files (x86)" / "ZCode" / "ZCode.exe",
            base / "Program Files (x86)" / "zcode" / "ZCode.exe",
            base / "Program Files (x86)" / "Z.ai" / "ZCode" / "ZCode.exe",
            base / "Apps" / "ZCode" / "ZCode.exe",
            base / "Apps" / "zcode" / "ZCode.exe",
        ))
    if not any(marker.exists() for marker in markers):
        markers.extend(_running_zcode_markers())
    return tuple(markers)


def _claude_app_markers() -> tuple[Path, ...]:
    """Return well-known Claude desktop application locations."""
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Darwin":
        markers.extend((
            Path("/Applications/Claude.app"),
            home / "Applications" / "Claude.app",
        ))
    elif platform.system() == "Windows":
        bases = [
            Path(value) for variable in
            ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
            if (value := os.environ.get(variable))
        ]
        bases.extend(_windows_drive_roots())
        relative_paths = (
            "Claude/Claude.exe",
            "Programs/Claude/Claude.exe",
        )
        for base in bases:
            markers.extend(base / rel for rel in relative_paths)
    else:
        markers.extend((
            home / ".local" / "share" / "applications" / "claude.desktop",
            Path("/usr/bin/claude-desktop"),
            Path("/opt/Claude/claude"),
        ))
    return tuple(dict.fromkeys(markers))


def _qoder_app_markers() -> tuple[Path, ...]:
    """Return well-known Qoder desktop application locations."""
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Darwin":
        markers.extend((
            Path("/Applications/Qoder.app"),
            Path("/Applications/Qoder IDE.app"),
            home / "Applications" / "Qoder.app",
            home / "Applications" / "Qoder IDE.app",
        ))
    elif platform.system() == "Windows":
        bases = [
            Path(value) for variable in
            ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
            if (value := os.environ.get(variable))
        ]
        bases.extend(_windows_drive_roots())
        relative_paths = (
            "Qoder/Qoder.exe",
            "Qoder/qoder.exe",
            "Qoder IDE/Qoder IDE.exe",
            "Programs/Qoder/Qoder.exe",
            "Programs/Qoder/qoder.exe",
            "Program Files/Qoder/Qoder.exe",
            "Program Files (x86)/Qoder/Qoder.exe",
        )
        for base in bases:
            markers.extend(base / rel for rel in relative_paths)
    else:
        markers.extend((
            home / ".local" / "share" / "applications" / "qoder.desktop",
            Path("/usr/bin/qoder"),
            Path("/opt/Qoder/qoder"),
        ))
    return tuple(dict.fromkeys(markers))


def _trea_app_markers() -> tuple[Path, ...]:
    """Return well-known Trae desktop application locations.

    Trae ships primarily as a desktop IDE; its ``trae`` CLI lives inside the
    app bundle (``Contents/Resources/app/bin/trae``) and is usually absent
    from PATH, so detection needs the same conservative app-marker fallback
    as ZCode/Qoder.  The in-bundle CLI is listed first so ``detect()`` can
    surface a real executable path; the bundle root still proves presence.
    """
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Darwin":
        bundle_cli = Path(
            "/Applications/Trae.app/Contents/Resources/app/bin/trae"
        )
        cn_bundle_cli = Path(
            "/Applications/Trae CN.app/Contents/Resources/app/bin/trae-cn"
        )
        user_bundle_cli = (
            home / "Applications" / "Trae.app"
            / "Contents" / "Resources" / "app" / "bin" / "trae"
        )
        user_cn_bundle_cli = (
            home / "Applications" / "Trae CN.app"
            / "Contents" / "Resources" / "app" / "bin" / "trae-cn"
        )
        markers.extend((
            bundle_cli,
            cn_bundle_cli,
            Path("/Applications/Trae.app"),
            Path("/Applications/Trae CN.app"),
            user_bundle_cli,
            user_cn_bundle_cli,
            home / "Applications" / "Trae.app",
            home / "Applications" / "Trae CN.app",
        ))
    elif platform.system() == "Windows":
        bases = [
            Path(value) for variable in
            ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
            if (value := os.environ.get(variable))
        ]
        bases.extend(_windows_drive_roots())
        relative_paths = (
            "Programs/Trae/Trae.exe",
            "Programs/trae/Trae.exe",
            "Trae/Trae.exe",
            "trae/Trae.exe",
            "Programs/Trae CN/Trae CN.exe",
            "Programs/Trae CN/trae-cn.exe",
            "Programs/Trae CN/Trae.exe",
            "Trae CN/Trae CN.exe",
            "Trae CN/trae-cn.exe",
            "Trae CN/Trae.exe",
        )
        for base in bases:
            markers.extend(base / rel for rel in relative_paths)
    else:
        markers.extend((
            home / ".local" / "share" / "applications" / "trae.desktop",
            home / ".local" / "share" / "applications" / "trae-cn.desktop",
            Path("/opt/Trae/trae"),
            Path("/opt/Trae CN/trae-cn"),
            Path("/opt/Trae-CN/trae-cn"),
            Path("/usr/bin/trae"),
            Path("/usr/bin/trae-cn"),
        ))
    return tuple(dict.fromkeys(markers))


WORKBUDDY_PROJECT_PATHS = (
    ".workbuddy/skills/bugx/SKILL.md",
    ".codebuddy/skills/bugx/SKILL.md",
)

# 桌面端从 <configDir>/skills 发现用户级 Skill（app.asar getSkillList），
# 其中 configDir 由 WORKBUDDY_CONFIG_DIR 或 cli/product.json#dataFolderName
# 决定；海外版为 ~/.workbuddy-ai，专享版为自定义目录名。
_WORKBUDDY_KNOWN_CONFIG_DIRNAMES = (".workbuddy-ai", ".workbuddy", ".codebuddy")


def _workbuddy_app_bundle_candidates() -> tuple[Path, ...]:
    """Bounded WorkBuddy/CodeBuddy app bundle locations for product.json probing."""
    home = Path.home()
    if platform.system() == "Darwin":
        return (
            Path("/Applications/WorkBuddy AI.app"),
            Path("/Applications/WorkBuddy.app"),
            Path("/Applications/CodeBuddy.app"),
            home / "Applications" / "WorkBuddy AI.app",
            home / "Applications" / "WorkBuddy.app",
            home / "Applications" / "CodeBuddy.app",
        )
    return ()


def _workbuddy_product_data_folder_names() -> tuple[str, ...]:
    """Read dataFolderName/customUserDataDir from a discovered app bundle.

    ``cli/product.json`` is the single authority for the per-edition data
    directory name (overseas build writes ``.workbuddy-ai``).  Probing is
    bounded to known bundle locations and every failure is a silent skip.
    """
    import json

    names: list[str] = []
    for bundle in _workbuddy_app_bundle_candidates():
        candidates = (
            bundle / "Contents" / "Resources" / "app.asar.unpacked" / "cli" / "product.json",
            bundle / "resources" / "cli" / "product.json",
            bundle / "resources" / "app.asar.unpacked" / "cli" / "product.json",
        )
        for product in candidates:
            try:
                payload = json.loads(product.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            config = payload.get("config")
            for value in (
                payload.get("dataFolderName"),
                config.get("customUserDataDir") if isinstance(config, dict) else None,
            ):
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
            break
    return tuple(dict.fromkeys(names))


def _workbuddy_config_roots() -> tuple[Path, ...]:
    """Return documented and compatibility config roots without scanning disks."""
    roots: list[Path] = []
    for variable in ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value).expanduser())
    home = Path.home()
    for dirname in _workbuddy_product_data_folder_names():
        roots.append(home / dirname)
    for dirname in _WORKBUDDY_KNOWN_CONFIG_DIRNAMES:
        candidate = home / dirname
        if candidate.is_dir():
            roots.append(candidate)
    roots.extend((home / ".workbuddy", home / ".codebuddy"))
    if platform.system() == "Windows":
        for variable in ("APPDATA", "LOCALAPPDATA"):
            value = os.environ.get(variable)
            if value:
                base = Path(value)
                roots.extend((base / "WorkBuddy", base / "CodeBuddy"))
    elif platform.system() == "Linux":
        xdg = os.environ.get("XDG_CONFIG_HOME")
        roots.extend(((Path(xdg) if xdg else home / ".config") / name
                      for name in ("workbuddy", "codebuddy")))
    return tuple(dict.fromkeys(path for path in roots))


def workbuddy_user_skill_root() -> Path:
    """Resolve the primary WorkBuddy user-level skills root.

    Precedence: explicit config env (the running desktop host injects it) >
    the edition's data folder from ``cli/product.json`` (e.g. ``.workbuddy-ai``
    for the overseas build) > first existing documented/config dirname >
    ``~/.workbuddy``.  The result is always an absolute path under ``$HOME``
    or an explicit env override; nothing is created here.
    """
    for variable in ("WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR"):
        value = os.environ.get(variable)
        if value and value.strip():
            return Path(value.strip()).expanduser()
    home = Path.home()
    for dirname in _workbuddy_product_data_folder_names():
        candidate = home / dirname
        if candidate.is_dir():
            return candidate
    for dirname in _WORKBUDDY_KNOWN_CONFIG_DIRNAMES:
        candidate = home / dirname
        if candidate.is_dir():
            return candidate
    return home / ".workbuddy"


def workbuddy_cli_venv() -> Path | None:
    """探测 WorkBuddy 私有 Python 虚拟环境 (~/.workbuddy-ai/binaries/python/envs/bugx-cli)。"""
    for config_root in _workbuddy_config_roots():
        candidate = config_root / "binaries" / "python" / "envs" / "bugx-cli"
        bin_dir = candidate / ("Scripts" if os.name == "nt" else "bin")
        py_exe = bin_dir / ("python.exe" if os.name == "nt" else "python")
        if candidate.is_dir() and py_exe.is_file():
            return candidate
    return None


def workbuddy_cli_venv_version(venv_path: Path | None = None) -> str | None:
    """快速探测 WorkBuddy 私有环境中的 bugx-cli 版本，优先读 dist-info 免进程开销。"""
    venv = venv_path or workbuddy_cli_venv()
    if not venv:
        return None
    lib_dir = venv / ("Lib" if os.name == "nt" else "lib")
    if lib_dir.is_dir():
        for sp in lib_dir.glob("python*/site-packages"):
            for dist in sp.glob("bugx_cli-*.dist-info"):
                match = re.search(r"bugx_cli-([\d.]+)\.dist-info", dist.name)
                if match:
                    return match.group(1)
        for dist in (lib_dir / "site-packages").glob("bugx_cli-*.dist-info"):
            match = re.search(r"bugx_cli-([\d.]+)\.dist-info", dist.name)
            if match:
                return match.group(1)
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    bugx_bin = bin_dir / ("bugx.exe" if os.name == "nt" else "bugx")
    if bugx_bin.is_file():
        try:
            import subprocess
            res = subprocess.run([str(bugx_bin), "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                parts = res.stdout.strip().split()
                if len(parts) >= 2:
                    return parts[1]
        except Exception:
            pass
    return None


def _workbuddy_app_markers() -> tuple[Path, ...]:
    """Return bounded WorkBuddy/CodeBuddy locations for both product variants.

    WorkBuddy's Windows installer lets the user choose its directory, so these
    are only default/compatibility candidates.  Unknown custom locations are
    resolved from PATH or the running process below; no drive-wide scan occurs.
    """
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Windows":
        bases = [
            Path(value) for variable in
            ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
            if (value := os.environ.get(variable))
        ]
        bases.extend(_windows_drive_roots())
        relative_paths = (
            "WorkBuddy/WorkBuddy.exe",
            "WorkBuddy/workbuddy.exe",
            "Tencent/WorkBuddy/WorkBuddy.exe",
            "CodeBuddy/CodeBuddy.exe",
            "Tencent/CodeBuddy/CodeBuddy.exe",
            "Programs/WorkBuddy/WorkBuddy.exe",
            "Programs/CodeBuddy/CodeBuddy.exe",
            "Program Files/WorkBuddy/WorkBuddy.exe",
            "Program Files/CodeBuddy/CodeBuddy.exe",
            "Program Files (x86)/WorkBuddy/WorkBuddy.exe",
            "Program Files (x86)/CodeBuddy/CodeBuddy.exe",
        )
        for base in bases:
            markers.extend(base / relative for relative in relative_paths)
        markers.extend(
            root / "AppData/Local/codebuddy/bin/codebuddy.exe"
            for root in (Path(os.environ["USERPROFILE"]) if os.environ.get("USERPROFILE") else home,)
        )
    elif platform.system() == "Darwin":
        for bundle in _workbuddy_app_bundle_candidates():
            markers.extend((
                bundle,
                bundle / "Contents" / "MacOS" / "Electron",
                bundle / "Contents" / "MacOS" / "WorkBuddy",
                bundle / "Contents" / "MacOS" / "CodeBuddy",
            ))
    elif platform.system() == "Linux":
        markers.extend((
            home / ".local" / "bin" / "workbuddy",
            home / ".local" / "bin" / "codebuddy",
            home / ".local" / "share" / "applications" / "workbuddy.desktop",
            home / ".local" / "share" / "applications" / "codebuddy.desktop",
            Path("/opt/WorkBuddy/WorkBuddy"),
            Path("/opt/workbuddy/workbuddy"),
            Path("/opt/CodeBuddy/CodeBuddy"),
            Path("/usr/bin/workbuddy"),
            Path("/usr/bin/codebuddy"),
        ))
    return tuple(dict.fromkeys(markers))


def _running_workbuddy_markers() -> tuple[Path, ...]:
    """Resolve a custom install directory from a running WorkBuddy process."""
    if platform.system() == "Windows":
        command = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not command:
            return ()
        query = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "Get-CimInstance Win32_Process | Where-Object { "
            "$_.Name -match '^(WorkBuddy|CodeBuddy)(\\.exe)?$' } "
            "| ForEach-Object { $_.ExecutablePath } | Where-Object { $_ } "
            "| Select-Object -Unique"
        )
        try:
            result = subprocess.run(
                [command, "-NoProfile", "-NonInteractive", "-Command", query],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        raw_paths = result.stdout.splitlines()
        expected_names = {"workbuddy.exe", "codebuddy.exe"}
    elif platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["ps", "-axo", "command="],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        raw_paths = []
        expected_names = {"electron", "workbuddy", "codebuddy", "codebuddy-lite-wb"}
        for line in result.stdout.splitlines():
            command = line.strip()
            lowered = command.casefold()
            if not any(token in lowered for token in (
                "workbuddy ai.app/contents/macos/",
                "workbuddy.app/contents/macos/",
                "codebuddy.app/contents/macos/",
            )):
                continue
            raw_paths.append(command.split(" ", 1)[0])
    elif platform.system() == "Linux":
        try:
            process_dirs = list(Path("/proc").iterdir())
        except OSError:
            return ()
        raw_paths = []
        expected_names = {"workbuddy", "codebuddy", "codebuddy-lite-wb"}
        for process_dir in process_dirs:
            if not process_dir.name.isdigit():
                continue
            try:
                raw_paths.append(os.readlink(process_dir / "exe").removesuffix(" (deleted)"))
            except OSError:
                continue
    else:
        return ()
    markers: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw.strip().strip('"'))
        if candidate.name.casefold() in expected_names and candidate.is_file():
            markers.append(candidate)
    return tuple(markers)


def _running_funide_markers() -> tuple[Path, ...]:
    """Resolve a custom FunIDE install directory from a running process.

    FunIDE is a desktop Electron host and its installer does not add
    ``FunIDE.exe`` to PATH.  Process metadata is the only reliable fallback
    for a user-selected Windows install such as ``E:\\FunIDE``; the probe is
    read-only, bounded, and never launches the host.
    """
    if platform.system() == "Windows":
        command = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not command:
            return ()
        query = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "Get-CimInstance Win32_Process -Filter \"Name = 'FunIDE.exe'\" "
            "| ForEach-Object { $_.ExecutablePath }; "
            "Get-Process -Name FunIDE | ForEach-Object { $_.Path } "
            "| Where-Object { $_ } | Select-Object -Unique"
        )
        try:
            result = subprocess.run(
                [command, "-NoProfile", "-NonInteractive", "-Command", query],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        raw_paths = result.stdout.splitlines()
        expected_names = {"funide.exe"}
    elif platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["ps", "-axo", "command="],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        raw_paths = []
        expected_names = {"funide", "electron"}
        for line in result.stdout.splitlines():
            command_line = line.strip()
            lowered = command_line.casefold()
            if "funide.app/contents/macos/" not in lowered:
                continue
            raw_paths.append(command_line.split(" ", 1)[0])
    elif platform.system() == "Linux":
        try:
            process_dirs = list(Path("/proc").iterdir())
        except OSError:
            return ()
        raw_paths = []
        expected_names = {"funide", "electron"}
        for process_dir in process_dirs:
            if not process_dir.name.isdigit():
                continue
            try:
                raw_paths.append(os.readlink(process_dir / "exe").removesuffix(" (deleted)"))
            except OSError:
                continue
    else:
        return ()
    markers: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw.strip().strip('"'))
        if candidate.name.casefold() in expected_names and candidate.is_file():
            markers.append(candidate)
    return tuple(dict.fromkeys(markers))


def _funide_app_markers() -> tuple[Path, ...]:
    """Return bounded FunIDE application locations on supported platforms."""
    home = Path.home()
    markers: list[Path] = []
    if platform.system() == "Windows":
        bases = [
            Path(value) for variable in
            ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
            if (value := os.environ.get(variable))
        ]
        bases.extend(_windows_drive_roots())
        relative_paths = (
            "FunIDE/FunIDE.exe",
            "FunIDE/bin/FunIDE.exe",
            "Programs/FunIDE/FunIDE.exe",
            "Program Files/FunIDE/FunIDE.exe",
            "Program Files (x86)/FunIDE/FunIDE.exe",
        )
        for base in bases:
            markers.extend(base / relative for relative in relative_paths)
    elif platform.system() == "Darwin":
        markers.extend((
            Path("/Applications/FunIDE.app/Contents/MacOS/FunIDE"),
            home / "Applications" / "FunIDE.app" / "Contents" / "MacOS" / "FunIDE",
        ))
    elif platform.system() == "Linux":
        markers.extend((
            home / ".local" / "bin" / "funide",
            home / ".local" / "share" / "applications" / "funide.desktop",
            Path("/opt/FunIDE/FunIDE"),
            Path("/usr/bin/funide"),
        ))
    if not any(marker.exists() for marker in markers):
        markers.extend(_running_funide_markers())
    return tuple(dict.fromkeys(markers))


def _funide_user_agents_dir() -> Path:
    """Return FunIDE/Copilot's confirmed user-level Agent directory."""
    override = os.environ.get("BUGX_FUNIDE_USER_AGENTS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".copilot" / "agents").resolve()


def _funide_user_profile_dir() -> Path:
    """Return FunIDE's VS Code-compatible user profile directory.

    Profile resources such as ``settings.json`` and ``mcp.json`` remain in
    ``<User>`` even when FunIDE's own ``funide.dataDir`` is moved elsewhere.
    Keep this path separate from the native data root so installing MCP
    autostart does not accidentally write beside a custom data directory.
    ``BUGX_FUNIDE_PROFILE_DIR`` is intentionally available for isolated
    tests and portable profiles.
    """
    override = os.environ.get("BUGX_FUNIDE_PROFILE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    # The data-dir override is the historical test/portable-profile hook; its
    # parent is the corresponding VS Code profile in that isolated layout.
    data_override = os.environ.get("BUGX_FUNIDE_DATA_DIR")
    if data_override:
        return Path(data_override).expanduser().resolve().parent
    system = platform.system()
    if system == "Windows":
        roaming = os.environ.get("APPDATA")
        base = Path(roaming) if roaming else Path.home() / "AppData" / "Roaming"
        return (base / "FunIDE" / "User").resolve()
    if system == "Darwin":
        return (Path.home() / "Library" / "Application Support" / "FunIDE" / "User").resolve()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return (base / "FunIDE" / "User").resolve()


def _read_funide_json_object(raw: str) -> dict[str, object]:
    """Parse FunIDE's JSON/JSONC user files without external dependencies."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as original:
        chars: list[str] = []
        index = 0
        in_string = False
        escaped = False
        while index < len(raw):
            char = raw[index]
            if in_string:
                chars.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                index += 1
                continue
            if char == '"':
                in_string = True
                chars.append(char)
                index += 1
                continue
            if char == "/" and index + 1 < len(raw) and raw[index + 1] == "/":
                index += 2
                while index < len(raw) and raw[index] not in "\r\n":
                    index += 1
                continue
            if char == "/" and index + 1 < len(raw) and raw[index + 1] == "*":
                index += 2
                while index + 1 < len(raw) and raw[index:index + 2] != "*/":
                    if raw[index] in "\r\n":
                        chars.append(raw[index])
                    index += 1
                index = min(len(raw), index + 2)
                continue
            chars.append(char)
            index += 1
        normalized = re.sub(r",(\s*[}\]])", r"\1", "".join(chars))
        parsed = json.loads(normalized)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("FunIDE JSON root must be an object", raw, 0)
    return parsed


def _funide_data_dir() -> Path:
    """Return FunIDE's native user data directory.

    FunIDE's settings ``智能体`` page is backed by ``funide/agents.json``;
    the generic ``~/.copilot/agents`` discovery path does not populate that
    page.  Keep the platform defaults aligned with Electron's user-data
    layout and provide a test/portable override for custom FunIDE profiles.
    """
    override = os.environ.get("BUGX_FUNIDE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    default = (_funide_user_profile_dir() / "funide").resolve()

    # FunIDE exposes ``funide.dataDir`` as a restricted setting.  Honor an
    # existing absolute setting so installs follow a user-selected data root
    # instead of silently writing the default profile beside it.
    settings = default.parent / "settings.json"
    try:
        raw = _read_funide_json_object(settings.read_text(encoding="utf-8"))
        configured = raw.get("funide.dataDir")
        if isinstance(configured, str) and configured.strip() and Path(configured).is_absolute():
            return Path(configured).expanduser().resolve()
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return default


def _funide_profile_mcp_path() -> Path:
    """Return FunIDE's current profile-level MCP resource.

    FunIDE's MCP settings page reads the profile resource at ``<User>/mcp.json``;
    its native Agent and Skill stores live below ``<User>/funide``.  Keep the
    profile path independent from a custom ``funide.dataDir``.
    """
    return _funide_user_profile_dir() / "mcp.json"


def _funide_profile_settings_path() -> Path:
    """Return FunIDE's profile settings file used by MCP autostart."""
    override = os.environ.get("BUGX_FUNIDE_SETTINGS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return _funide_user_profile_dir() / "settings.json"


def _funide_user_skills_dir() -> Path:
    """Return FunIDE's native user Skill discovery directory."""
    return _funide_data_dir() / "skills"


def _reverse_workbuddy_install_root(executable: Path | None) -> Path | None:
    """Return the app root from a resolved executable, including custom paths."""
    if executable is None:
        return None
    resolved = executable.resolve(strict=False)
    names = {"workbuddy", "codebuddy"}
    for ancestor in (resolved.parent, *resolved.parents):
        stem = ancestor.name.casefold()
        if stem.endswith(".app"):
            stem = stem[: -len(".app")]
            if stem in names or any(stem.startswith(f"{name} ") for name in names):
                return ancestor
        if ancestor.name.casefold() in names:
            return ancestor
        if ancestor.name.casefold() == "bin" and ancestor.parent.name.casefold() in names:
            return ancestor.parent
    return resolved.parent


# 入口发布方式（`entry_mode`）：描述「本宿主的入口资源如何落到项目里」，
# 而不是宿主叫什么名字。安装命令据模式分支，宿主名与模式的绑定只在
# HOST_ADAPTERS 声明处发生一次。
ENTRY_PLUGIN_CATALOG = "plugin-catalog"      # 整包插件 + 市场清单（ChatGPT）
ENTRY_SKILL_PAYLOAD = "skill-payload"        # 直接复制宿主 skill 载荷（Pi）
ENTRY_SHARED_SYMLINK = "shared-symlink"      # 相对符号链接指向共享载荷（OpenCode）
ENTRY_USER_REGISTRATION = "user-registration"  # 仅用户级注册，不写项目级入口（ZCode）
ENTRY_MULTI_PATH = "multi-path"              # 同一内容写入多个项目级路径（WorkBuddy）
ENTRY_RENDERED = "rendered"                  # 渲染模板写入 adapter.target()（其余宿主）

# 安装流程能力标签。CLI 命令据标签决定分支（`adapter.supports(...)`），
# 而不是比较宿主名字符串；新增宿主只需在 HOST_ADAPTERS 声明其能力。
CAP_PREVALIDATE_CATALOG = "prevalidate-catalog"      # 共享写入前先校验插件市场清单
CAP_PROJECT_MCP_CONFIG = "project-mcp-config"        # 需要创建/合并项目级 MCP 配置
CAP_PROJECT_PATCH_OVERLAY = "project-patch-overlay"  # 需要安装项目级 patch 覆盖层
CAP_DISTRIBUTION_PLUGIN = "distribution-plugin"      # 额外产出可分发插件包
CAP_EXTENSION_PAYLOAD = "extension-payload"          # 额外复制宿主扩展载荷
CAP_EXTRA_TRACKED_PATHS = "extra-tracked-paths"      # required_paths 也计入哈希清单
CAP_GLOBAL_AGENT_LINK = "global-agent-link"          # 注册用户级全局 agent 链接
CAP_GLOBAL_SKILL_LINK = "global-skill-link"          # 注册用户级全局 skill 链接
CAP_NATIVE_MCP_CONFIG = "native-mcp-config"          # 需要写宿主原生项目/工作区 MCP 容器
CAP_USER_AGENT_REGISTRATION = "user-agent-registration"  # 需要用户级子智能体注册
CAP_USER_LEVEL_ARTIFACT = "user-level-artifact"      # 需要写用户级 Skill / Connector 产物
CAP_WORKSPACE_MIRROR = "workspace-mirror"            # 支持工作区同构镜像（如 Roo Code .roo）
#: 需要把入口额外镜像到本宿主的**变体目录**（如 Trae 的 .trae / .trae-cn）。
#: DES-14：该差异必须声明在适配器上，命令层只做能力查询，不得按宿主名分支。
CAP_WORKSPACE_MIRROR_VARIANTS = "workspace-mirror-variants"


HOST_NAME_ZCODE = "zcode"
HOST_NAME_CLAUDE = "claude"
HOST_NAME_QODER = "qoder"
HOST_NAME_FUNIDE = "funide"


@dataclass(frozen=True)
class HostAdapter:
    """一个 AI CLI 宿主的发现和安装规则。"""

    name: str
    label: str
    executable: str
    marker: str
    install_path: str
    install_kind: str
    required_paths: tuple[str, ...] = ()
    entry_mode: str = ENTRY_RENDERED
    capabilities: frozenset[str] = frozenset()
    #: DOC-169：历史遗留安装路径；安装与无登记卸载按当前模板逐字比对清理。
    legacy_paths: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def supports(self, capability: str) -> bool:
        """查询本宿主是否声明了某项安装流程能力。

        调用方据此分支，避免在命令层按宿主名字符串判断；宿主名到能力的
        映射是 HOST_ADAPTERS 声明处的单一事实。
        """
        return capability in self.capabilities

    def managed_surfaces(self) -> tuple[str, ...]:
        """返回本宿主涉及的项目级目录/文件前缀，供安装事务快照聚合。"""
        candidates = (self.marker, self.install_path) + self.required_paths + self.legacy_paths
        surfaces: list[str] = []
        for p in candidates:
            if not p:
                continue
            part = Path(p).parts[0]
            if part not in surfaces:
                surfaces.append(part)
        return tuple(surfaces)

    def pre_validate(self, root: Path) -> None:
        """安装前预校验钩子（默认无操作）。"""
        pass

    def _app_markers(self) -> tuple[Path, ...]:
        return ()

    def _user_marker(self) -> bool:
        return False

    def _project_marker(self, root: Path) -> bool:
        marker_path = root / self.marker
        return marker_path.is_dir() or marker_path.is_file()

    def ensure_user_agents(
        self, root: Path, source: Path, *, locale: str | None = None,
    ) -> dict[str, object] | None:
        """Register user-level subagents for hosts declaring CAP_USER_AGENT_REGISTRATION."""
        return None

    def render_required_entry(
        self, relative_path: str, source: Path, *, locale: str | None = None,
    ) -> str | None:
        """Render auxiliary entry for extra tracked paths (e.g. subagents)."""
        return None

    @property
    def installed_components(self) -> list[str]:
        if self.supports(CAP_EXTENSION_PAYLOAD):
            return ["skill", "extension"]
        if self.supports(CAP_DISTRIBUTION_PLUGIN):
            return ["plugin"]
        return ["entry"]

    def detect(self, root: Path) -> dict[str, object]:
        """只读检测本机命令和当前项目标记。"""
        executable = shutil.which(self.executable)
        project_marker = self._project_marker(root)
        app_markers = self._app_markers()
        app_path = next((path for path in app_markers if path.exists()), None)
        user_marker = self._user_marker()
        if executable is None and app_path is not None:
            # PATH 查不到但标准安装位置存在（如 Windows 版 Codex CLI 不写
            # PATH）：把解析出的绝对路径当作可执行文件，安装流程才能调用。
            executable = str(app_path)
        return {
            "name": self.name,
            "label": self.label,
            "executable": executable,
            "installed": bool(executable or project_marker or app_path or user_marker),
            "project_marker": project_marker,
            "app_path": str(app_path) if app_path else None,
            "install_path": self.install_path,
            "install_kind": self.install_kind,
            "required_paths": list(self.required_paths),
            "interaction": dict(INTERACTION_CONTRACTS.get(
                self.name,
                {"tool": "unknown", "single_select": "multiple=false",
                 "failure": "阻断"},
            )),
        }

    def target(self, root: Path) -> Path:
        return root / self.install_path

    def render(self, source: Path, *, locale: str | None = None) -> str:
        rendered = self._render_entry(source, locale=locale)
        # The runtime request is the authoritative localized surface.  A tiny
        # non-instructional marker lets an installer and a human inspect which
        # locale was selected without breaking host frontmatter.
        rendered = add_locale_marker(rendered, locale)
        # 生成的宿主入口统一以换行结尾，避免签入副本出现 no-newline-at-eof 漂移。
        return rendered if rendered.endswith("\n") else rendered + "\n"

    def _render_entry(self, source: Path, *, locale: str | None = None) -> str:
        """生成宿主入口；source 是公开 Agent 的绝对路径。"""
        try:
            component = get_component(self.name)
            return render_host_entry(component, source, locale=locale)
        except (ValueError, RuntimeError) as exc:
            raise ValueError(f"未知宿主适配器: {self.name}") from exc


class OpenCodeAdapter(HostAdapter):
    """OpenCode CLI adapter."""
    pass


class CodexAdapter(HostAdapter):
    """Codex CLI adapter（仅 CLI 侧行为覆写）。

    DOC-239 S3：声明字段（名字/可执行/marker/入口/能力）全部来自
    ``core/hosts/codex.py``；本类保留 Windows 安装位探测和 Codex Skill 装配。
    Skill 正文仍取自 locale 唯一模板，不附加通用全命令目录。
    """

    def _app_markers(self) -> tuple[Path, ...]:
        return _codex_app_markers()

    def _render_entry(self, source: Path, *, locale: str | None = None) -> str:
        # The Codex-specific Skill is self-contained for routine execution.
        # Do not append the generic full command catalog and triage manual.
        from .hosts.template_engine import _build_body_header, _entry_section, get_agent_description
        from . import __build_id__
        component = get_component(self.name)
        description = _entry_section("description.codex", locale, get_agent_description(locale))
        return (
            "---\nname: bugx\ndescription: " + description + "\n---\n"
            + f"<!-- bugx-build: {__build_id__} -->\n\n"
            + _build_body_header(component, "bugx engine path --name bugx.md", locale)
            + _entry_section("section.codex", locale, "")
        )


class ClineAdapter(HostAdapter):
    """Cline CLI and Extension adapter.

    DOC-239：Cline 已纵切到 ``core/hosts/cline.py``。本类只保留 CLI 侧**行为**覆写
    （用户级 Agent 注册、入口渲染）；检测面（项目/用户标记、MCP 镜像目录）全部由
    插件的 ``identity.aliases`` / ``install.mcp_relpaths`` 推导，不再写宿主字面量。
    """

    def managed_surfaces(self) -> tuple[str, ...]:
        mirrors = tuple(
            Path(rel).parts[0]
            for rel in engine_host_plugin(self.name).install.mcp_relpaths
            if Path(rel).parts
        )
        return tuple(dict.fromkeys(super().managed_surfaces() + mirrors))

    def _project_marker(self, root: Path) -> bool:
        if super()._project_marker(root):
            return True
        return any(
            (root / f".{alias}").exists()
            for alias in engine_host_plugin(self.name).identity.aliases
        )

    def _user_marker(self) -> bool:
        home = Path.home()
        marker = engine_host_plugin(self.name).install.marker
        if marker and (home / marker).is_dir():
            return True
        return any(
            (home / f".{alias}").is_dir()
            for alias in engine_host_plugin(self.name).identity.aliases
        )

    def ensure_user_agents(
        self, root: Path, source: Path, *, locale: str | None = None,
    ) -> dict[str, object] | None:
        from .commands.plugin_cline import _ensure_cline_user_agents
        return _ensure_cline_user_agents(root, source, locale=locale)

    def render_required_entry(
        self, relative_path: str, source: Path, *, locale: str | None = None,
    ) -> str | None:
        if "worker" in relative_path:
            return render_cline_worker_entry(locale=locale)
        return render_cline_agent_entry(source, locale=locale)


class ClaudeAdapter(HostAdapter):
    """Claude Code CLI adapter."""

    def _app_markers(self) -> tuple[Path, ...]:
        return _claude_app_markers()

    def _user_marker(self) -> bool:
        return (Path.home() / ".claude").is_dir()

    def render(self, source: Path, *, locale: str | None = None) -> str:
        return render_claude_agent_entry(source, locale=locale)

    def render_required_entry(
        self, relative: str, source: Path, *, locale: str | None = None,
    ) -> str | None:
        if relative.endswith("bugx-worker.md"):
            return render_claude_worker_entry(locale=locale)
        if relative.endswith("bugx.md"):
            return self.render(source, locale=locale)
        return None

    def ensure_user_agents(
        self, root: Path, source: Path, *, locale: str | None = None,
    ) -> dict[str, object] | None:
        from .commands.plugin_claude import _ensure_claude_user_agents
        return _ensure_claude_user_agents(root, source, locale=locale)


class ZCodeAdapter(HostAdapter):
    """ZCode IDE adapter."""

    def _app_markers(self) -> tuple[Path, ...]:
        return _zcode_app_markers()

    def render_required_entry(
        self, relative_path: str, source: Path, *, locale: str | None = None,
    ) -> str | None:
        # ZCode 不再将项目级 worker 纳入安装 required_paths；保留按路径渲染仅用于
        # 卸载时精确识别旧版本声明的 legacy_paths。
        if relative_path == ZCODE_WORKER_INSTALL_PATH:
            return render_zcode_worker_entry(locale=locale)
        return None

    def ensure_user_agents(
        self, root: Path, source: Path, *, locale: str | None = None,
    ) -> dict[str, object] | None:
        from .commands.plugin_zcode import _ensure_zcode_user_agents
        return _ensure_zcode_user_agents(root, source, locale=locale)


class WorkBuddyAdapter(HostAdapter):
    """WorkBuddy desktop plus its CodeBuddy code-mode compatibility surface."""

    def detect(self, root: Path) -> dict[str, object]:
        project_markers = tuple(root / relative for relative in WORKBUDDY_PROJECT_PATHS)
        user_roots = _workbuddy_config_roots()
        user_markers = tuple(
            directory / "skills" / "bugx" / "SKILL.md" for directory in user_roots
        )
        commands = ("workbuddy", "codebuddy", "codebuddy-lite-wb")
        command_paths = tuple(
            (name, Path(found)) for name in commands
            if (found := shutil.which(name))
        )
        app_markers = _workbuddy_app_markers()
        app_marker = next((path for path in app_markers if path.exists()), None)
        app_path = next(
            (path for path in app_markers if path.is_file() and path.suffix.casefold() != ".desktop"),
            None,
        )
        running_path = (
            next(iter(_running_workbuddy_markers()), None)
            if not command_paths and app_path is None else None
        )
        executable_path = (command_paths[0][1] if command_paths else app_path or running_path)
        variants: list[str] = []
        if any(name == "workbuddy" for name, _ in command_paths) or app_marker is not None:
            variants.append(
                "codebuddy" if app_marker and app_marker.name.casefold().startswith("codebuddy")
                else "workbuddy"
            )
        if any(name in {"codebuddy", "codebuddy-lite-wb"} for name, _ in command_paths):
            variants.append("codebuddy")
        if not variants:
            variants = [
                "workbuddy" if project_markers[0].exists() else "codebuddy"
                if project_markers[1].exists() else "workbuddy"
            ]
        project_marker = any(path.is_file() or path.is_dir() for path in project_markers)
        user_marker = any(path.is_file() for path in user_markers)
        return {
            "name": self.name,
            "label": self.label,
            "executable": str(executable_path) if executable_path else None,
            "installed": bool(executable_path or app_marker or project_marker or user_marker),
            "project_marker": project_marker,
            "user_marker": user_marker,
            "app_path": str(app_path) if app_path else None,
            "discovery_source": (
                "project-config" if project_marker else
                "user-config" if user_marker else
                "path" if command_paths else
                "default-path" if app_marker else
                "running-process" if running_path else None
            ),
            "install_path": self.install_path,
            "install_kind": self.install_kind,
            "required_paths": list(self.required_paths),
            "variants": variants,
            "config_roots": [str(path) for path in user_roots],
            "user_skill_root": str(workbuddy_user_skill_root()),
            "user_skill_dirs": [str(path) for path in user_markers],
            "install_root": (
                str(_reverse_workbuddy_install_root(executable_path))
                if executable_path else None
            ),
            "interaction": dict(INTERACTION_CONTRACTS["workbuddy"]),
        }


class PiAdapter(HostAdapter):
    """Pi CLI adapter."""
    pass


class ChatGptAdapter(HostAdapter):
    """ChatGPT and Codex-plugin adapter."""

    def _app_markers(self) -> tuple[Path, ...]:
        return _chatgpt_app_markers()

    def pre_validate(self, root: Path) -> None:
        from .commands.plugin import _chatgpt_marketplace
        _chatgpt_marketplace(root)


class QoderAdapter(HostAdapter):
    """Qoder AI adapter."""

    def _app_markers(self) -> tuple[Path, ...]:
        return _qoder_app_markers()

    def _user_marker(self) -> bool:
        return (Path.home() / ".qoder").is_dir()

    def ensure_user_agents(
        self, root: Path, source: Path, *, locale: str | None = None,
    ) -> dict[str, object] | None:
        from .commands.plugin_qoder import _ensure_qoder_user_agents
        return _ensure_qoder_user_agents(root, source, locale=locale)

    def render_required_entry(
        self, relative_path: str, source: Path, *, locale: str | None = None,
    ) -> str | None:
        if relative_path.endswith("bugx-worker.md"):
            return render_qoder_worker_entry(locale=locale)
        return render_qoder_agent_entry(source, locale=locale)

    @property
    def installed_components(self) -> list[str]:
        return ["agent", "worker", "connector"]

    def _render_entry(self, source: Path, *, locale: str | None = None) -> str:
        return render_qoder_agent_entry(source, locale=locale)


class TreaAdapter(HostAdapter):
    """Trea (Trae) IDE adapter supporting both Trae (Global) and Trae CN variants."""

    def _app_markers(self) -> tuple[Path, ...]:
        return _trea_app_markers()

    def _user_marker(self) -> bool:
        home = Path.home()
        markers = (
            home / ".trae",
            home / ".trae-cn",
            home / "Library" / "Application Support" / "Trae",
            home / "Library" / "Application Support" / "Trae CN",
        )
        return any(p.exists() for p in markers)

    def _project_marker(self, root: Path) -> bool:
        return any((root / marker).exists() for marker in (".trea", ".trae", ".trae-cn"))

    def managed_surfaces(self) -> tuple[str, ...]:
        surfaces = list(super().managed_surfaces())
        for extra in (".trae", ".trae-cn"):
            if extra not in surfaces:
                surfaces.append(extra)
        return tuple(surfaces)

    def detect(self, root: Path) -> dict[str, object]:
        info = super().detect(root)
        home = Path.home()
        variants: list[str] = []

        cn_paths = [
            Path("/Applications/Trae CN.app"),
            home / "Applications" / "Trae CN.app",
            home / ".trae-cn",
            home / "Library" / "Application Support" / "Trae CN",
            root / ".trae-cn",
        ]
        if shutil.which("trae-cn") or any(p.exists() for p in cn_paths):
            variants.append("trae-cn")

        global_paths = [
            Path("/Applications/Trae.app"),
            home / "Applications" / "Trae.app",
            home / ".trae",
            home / "Library" / "Application Support" / "Trae",
            root / ".trae",
            root / ".trea",
        ]
        if shutil.which("trae") or any(p.exists() for p in global_paths):
            variants.append("trae")

        info["detected_variants"] = variants

        # If executable wasn't resolved by self.executable="trae", fallback to trae-cn
        if info.get("executable") is None:
            cn_exe = shutil.which("trae-cn")
            if cn_exe:
                info["executable"] = cn_exe
            else:
                for marker in self._app_markers():
                    if "trae-cn" in marker.name and marker.is_file():
                        info["executable"] = str(marker)
                        break
        return info


class FunIdeAdapter(HostAdapter):
    """FunIDE desktop Agent Host adapter."""

    def _app_markers(self) -> tuple[Path, ...]:
        return _funide_app_markers()

    def _user_marker(self) -> bool:
        return (_funide_user_agents_dir() / "bugx.agent.md").is_file()

    def detect(self, root: Path) -> dict[str, object]:
        result = super().detect(root)
        user_entry = _funide_user_agents_dir() / "bugx.agent.md"
        data_dir = _funide_data_dir()
        native_entry = data_dir / "agents.json"
        native_skill = _funide_user_skills_dir() / "bugx" / "SKILL.md"
        native_mcp = _funide_profile_mcp_path()
        result.update({
            "user_marker": any(path.is_file() for path in (user_entry, native_entry, native_skill, native_mcp)),
            "user_agent_path": str(user_entry),
            "user_agents_dir": str(user_entry.parent),
            "native_agent_path": str(native_entry),
            "native_skill_path": str(native_skill),
            "native_mcp_path": str(native_mcp),
            "native_data_dir": str(data_dir),
        })
        return result

    def ensure_user_agents(
        self, root: Path, source: Path, *, locale: str | None = None,
    ) -> dict[str, object] | None:
        from .commands.plugin_funide import _ensure_funide_user_agents
        return _ensure_funide_user_agents(root, source, locale=locale)


# dsh 宿主适配器 — 基于补丁覆盖层 + Skills 注入
class DshAdapter(HostAdapter):
    """DeepSeek Harness (dsh) adapter."""

    def detect(self, root: Path) -> dict[str, object]:
        # P3f：project_marker 语义与基类对齐——patch 文件存在即 marker=True，
        # 与宿主可执行文件是否在 PATH（installed）解耦；否则一个已接入
        # BugX 的项目在未安装 dsh CLI 的机器上会被误报为未接入。
        executable = shutil.which("dsh")
        installed = bool(executable)
        marker = (root / ".dsh" / "patch.yml").exists()
        return {
            "name": self.name,
            "label": self.label,
            "executable": executable or None,
            "installed": installed,
            "project_marker": marker,
            "app_path": None,
            "install_path": ".dsh",
            "install_kind": "patch+skill",
            "required_paths": [".dsh/patch.yml", ".dsh/skills/bugx/SKILL.md",
                               ".dsh/skills/bugx-dsh/SKILL.md", ".dsh/agents/bugx.md"],
            "interaction": dict(INTERACTION_CONTRACTS["dsh"]),
        }

    def render(self, source: Path, *, locale: str | None = None) -> str:
        # Keep the generated project Agent entry aligned with the shared DSH
        # protocol. The specialised detector only adds DSH capability facts;
        # it keeps the project-scoped startup policy and native interaction forwarding.
        return super().render(source, locale=locale)


def _adapter_host_name(factory: type[HostAdapter]) -> str:
    """宿主名由 CLI 覆写类的类名派生（``ClineAdapter`` → ``cline``）。

    覆写类只承载 CLI 侧**行为**；名字/可执行/marker/入口等都来自插件。派生结果必须
    命中 ready 插件（``_adapter_from_plugin`` 内 fail-closed），因此「两个宿主共用同一个
    覆写类」或「类名拼错」都会在导入期暴露。
    """

    return factory.__name__.removesuffix("Adapter").casefold()


def _override_adapter(factory: type[HostAdapter]) -> HostAdapter:
    """用「类名派生的宿主名 + 插件声明字段」构造 CLI 适配器（无宿主字面量）。"""

    return _adapter_from_plugin(_adapter_host_name(factory), factory)


def _plugin_backed_adapters(*factories: type[HostAdapter]) -> tuple[HostAdapter, ...]:
    """构造插件承载宿主的适配器；引擎快照不可用时跳过（不抛错）。

    与 ``hosts.registry.plugin_backed_components`` 同理：适配器清单在 import 期
    构建，全新机器上全局引擎尚未安装，若在这里 fail-closed，``bugx init``（安装
    引擎的入口）会因为引擎没装而无法启动。真正需要该宿主适配器的调用点走
    :func:`get_adapter` / :func:`hosts.registry.get_component`，那里 fail-closed。
    """

    adapters: list[HostAdapter] = []
    for factory in factories:
        try:
            adapters.append(_adapter_from_plugin(_adapter_host_name(factory), factory))
        except (RuntimeError, ImportError):
            continue
    return tuple(adapters)


def _adapter_from_plugin(name: str, factory: type[HostAdapter]) -> HostAdapter:
    """用引擎插件的声明字段构造 CLI 安装适配器（DOC-239）。

    行为覆写（managed_surfaces / 用户级注册 / 入口渲染）仍留在宿主子类里——它们是
    CLI 侧实现；**声明**（名字/可执行/marker/入口路径/种类/必需路径/能力/别名）
    只允许来自 ``core/hosts/<host>.py``，否则又会退回「同一宿主两份清单」。
    """

    plugin = engine_host_plugin(name)
    identity, install = plugin.identity, plugin.install
    if not install.marker:
        raise RuntimeError(f"{name}: install.marker 未声明，无法投影 CLI 安装适配器")
    return factory(
        identity.name,
        identity.label,
        identity.executable,
        install.marker,
        install.path,
        install.kind,
        install.required_paths,
        entry_mode=install.entry_mode,
        capabilities=install.capabilities,
        legacy_paths=install.legacy_paths,
        aliases=identity.aliases,
    )


HOST_ADAPTERS: tuple[HostAdapter, ...] = (
    OpenCodeAdapter(
        "opencode", "OpenCode", "opencode", ".opencode", ".opencode/agents/bugx.md", "agent",
        entry_mode=ENTRY_SHARED_SYMLINK,
        capabilities=frozenset({CAP_PROJECT_MCP_CONFIG, CAP_GLOBAL_AGENT_LINK}),
    ),
    DshAdapter(
        "dsh", "DeepSeek Harness", "dsh", ".dsh", ".dsh/skills/bugx/SKILL.md", "patch+skill",
        (
            ".dsh/patch.yml",
            ".dsh/skills/bugx/SKILL.md",
            ".dsh/agents/bugx.md",
        ),
        capabilities=frozenset({
            CAP_PROJECT_PATCH_OVERLAY, CAP_EXTRA_TRACKED_PATHS,
            CAP_NATIVE_MCP_CONFIG,
        }),
    ),
    # DOC-239：声明字段全部来自引擎插件（core/hosts/codex.py），
    # CodexAdapter 只保留 CLI 侧行为覆写。引擎快照不可用时跳过（见
    # _plugin_backed_adapters：全新机器上必须先能启动 bugx init）。
    *_plugin_backed_adapters(CodexAdapter),
    # DOC-239：声明字段全部来自引擎插件（core/hosts/cline.py），
    # ClineAdapter 只保留 CLI 侧行为覆写。引擎快照不可用时跳过（见
    # _plugin_backed_adapters：全新机器上必须先能启动 bugx init）。
    *_plugin_backed_adapters(ClineAdapter),

    # DOC-239：声明字段全部来自引擎插件（core/hosts/claude.py），
    # ClaudeAdapter 只保留 CLI 侧行为覆写。引擎快照不可用时跳过（见
    # _plugin_backed_adapters：全新机器上必须先能启动 bugx init）。
    *_plugin_backed_adapters(ClaudeAdapter),
    # DOC-239：声明字段全部来自引擎插件（core/hosts/zcode.py），
    # ZCodeAdapter 只保留 CLI 侧行为覆写。引擎快照不可用时跳过（见
    # _plugin_backed_adapters：全新机器上必须先能启动 bugx init）。
    *_plugin_backed_adapters(ZCodeAdapter),
    WorkBuddyAdapter(
        "workbuddy", "WorkBuddy AI", "workbuddy", ".workbuddy",
        WORKBUDDY_PROJECT_PATHS[0], "skill", WORKBUDDY_PROJECT_PATHS,
        entry_mode=ENTRY_MULTI_PATH,
        capabilities=frozenset({CAP_USER_LEVEL_ARTIFACT, CAP_NATIVE_MCP_CONFIG}),
    ),
    # DOC-239：声明字段全部来自引擎插件（core/hosts/pi.py），
    # PiAdapter 只保留 CLI 侧行为覆写。引擎快照不可用时跳过（见
    # _plugin_backed_adapters：全新机器上必须先能启动 bugx init）。
    *_plugin_backed_adapters(PiAdapter),
    ChatGptAdapter(
        "chatgpt", "ChatGPT", "chatgpt", ".agents/plugins/marketplace.json",
        ".agents/plugins/marketplace.json", "plugin",
        (
            "plugins/bugx/.codex-plugin/plugin.json",
            "plugins/bugx/skills/bugx/SKILL.md",
        ),
        entry_mode=ENTRY_PLUGIN_CATALOG,
        capabilities=frozenset({CAP_PREVALIDATE_CATALOG, CAP_NATIVE_MCP_CONFIG}),
    ),
    QoderAdapter(
        "qoder", "Qoder AI", "qoder", ".qoder", ".qoder/agents/bugx.md", "subagent",
        (
            ".qoder/agents/bugx-worker.md",
        ),
        capabilities=frozenset({
            CAP_USER_AGENT_REGISTRATION,
            CAP_NATIVE_MCP_CONFIG,
            CAP_EXTRA_TRACKED_PATHS,
        }),
    ),
    TreaAdapter(
        "trea", "Trea", "trae", ".trea", ".trea/agents/bugx.md", "agent",
        # DOC-169 / DOC-210：补全项目级原生 MCP 写入能力与双变体工作区镜像。
        capabilities=frozenset({
            CAP_NATIVE_MCP_CONFIG,
            CAP_WORKSPACE_MIRROR,
            CAP_WORKSPACE_MIRROR_VARIANTS,
        }),
        aliases=("trea-cli", "trae", "trae-cn", "trea-cn", "trae_cn", "trea_cn", "trae-ai"),
    ),
    FunIdeAdapter(
        "funide", "FunIDE", "funide", ".github/agents/bugx.agent.md",
        ".github/agents/bugx.agent.md", "agent",
        capabilities=frozenset({CAP_NATIVE_MCP_CONFIG, CAP_USER_AGENT_REGISTRATION}),
    ),
)


def discover_hosts(root: Path) -> list[dict[str, object]]:
    """按固定顺序返回所有受支持宿主，绝不运行宿主命令。"""
    return [adapter.detect(root.resolve()) for adapter in HOST_ADAPTERS]


def get_adapter(name: str) -> HostAdapter:
    for adapter in HOST_ADAPTERS:
        if adapter.name == name or name in adapter.aliases:
            return adapter
    choices = ", ".join(adapter.name for adapter in HOST_ADAPTERS)
    hint = (
        # 不再建议 BUGX_HOST：DOC-217 D211.3 已废除它，设置后不会有任何效果。
        "（未检测到宿主上下文：请用 --agent / --host 显式指定）"
        if str(name).strip().lower() in {"", "unknown", "none", "auto"}
        else ""
    )
    raise ValueError(f"未知 AI Agent: {name}；可选：{choices}{hint}")
