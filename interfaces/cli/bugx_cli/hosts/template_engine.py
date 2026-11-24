"""BugX AI Agent 宿主协议模板装配插槽引擎。

基于声明式 HostComponent 规格统一组装宿主 Agent 入口，
消除硬编码 if-elif 拼接，保证生成的协议与全局引擎规范逐字一致。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import HostComponent

# 宿主入口 frontmatter 触发文案与 Worker 描述
AGENT_DESCRIPTION_EN = (
    "An autonomous code review and testing agent designed to safeguard software delivery. "
    "It surfaces hidden bugs for developers to eliminate painful debugging and rework, "
    "while automating black-box, functional, and performance testing across complex edge cases for QA. "
    "Ensures code runs reliably, meets all requirements, and frees engineers to focus on what matters."
)

AGENT_DESCRIPTION_ZH = (
    "BugX 是一个具备自主代码审查与测试能力的智能体，严把代码交付关：它帮助开发人员在写完代码后自动排查隐藏的深层 Bug，"
    "减少反复排错与返工；帮助测试人员自动化运行黑盒测试、功能验证与性能测试，自动覆盖手工盲测难以覆盖的极端场景；"
    "确保代码功能符合要求、具备良好的健壮性和稳定性。让开发和测试在繁琐的工作中解放双手，大幅提高工作效率、全面提升代码质量、显著降低后期维护成本，实现科技服务于人。"
)

BUGX_WORKER_DESCRIPTION_ZH = (
    "BugX 并发测试审查 Worker。由 BugX 主智能体并发调度，负责具体代码模块的审查、缺陷排查与自动化测试验证；"
    "仅作为子智能体工作节点协同运行。"
)

BUGX_WORKER_DESCRIPTION_EN = (
    "A concurrent test and review worker dispatched by BugX to inspect assigned modules, "
    "uncover deep bugs, and run automated verification as a collaborative subagent."
)

AGENT_DESCRIPTION = AGENT_DESCRIPTION_ZH
BUGX_WORKER_DESCRIPTION = BUGX_WORKER_DESCRIPTION_ZH


def get_agent_description(locale: str | None = None) -> str:
    """Return the localized host agent description (fallback to en-US)."""
    loc = (locale or "").strip()
    if not loc:
        try:
            import core.i18n as i18n
            loc = i18n.resolve_locale().resolved_locale
        except Exception:
            pass
    if loc.startswith("zh"):
        return AGENT_DESCRIPTION_ZH
    return AGENT_DESCRIPTION_EN


def get_worker_description(locale: str | None = None) -> str:
    """Return the localized concurrent worker description (fallback to en-US)."""
    loc = (locale or "").strip()
    if not loc:
        try:
            import core.i18n as i18n
            loc = i18n.resolve_locale().resolved_locale
        except Exception:
            pass
    if loc.startswith("zh"):
        return BUGX_WORKER_DESCRIPTION_ZH
    return BUGX_WORKER_DESCRIPTION_EN


def add_locale_marker(content: str, locale: str | None) -> str:
    """Record the selected locale without invalidating YAML frontmatter."""
    loc = (locale or "").strip()
    if not loc:
        try:
            import core.i18n as i18n
            loc = i18n.resolve_locale().resolved_locale
        except Exception:
            pass
    if not loc:
        return content
    normalized = loc or "en-US"
    marker = f"<!-- bugx-locale: {normalized} -->\n"
    if content.startswith("---\n"):
        closing = content.find("\n---", 4)
        if closing >= 0:
            end = closing + len("\n---")
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



# ---------------------------------------------------------------------------
# DOC-230 语言策略：宿主入口**正文按 locale 生成**。
#
# `core/locales/*.json` 的 `host.entry.*` 是正文的**唯一真源**（zh-CN / en-US 各一套），
# 由 `i18n.resolve_locale()` 决定实际语言；`bugx install --locale` 可显式覆盖。
# 代码中**不再保留**同语言的正文副本（避免"代码 + 目录"双事实源）。
# ---------------------------------------------------------------------------
# 目录不可读时的**极小兜底**（语言中立、只指向权威协议）。
# 正常路径永不使用：守卫测试断言两语言均含全部 `host.entry.*` 键。
_FALLBACK_PROTOCOL_POINTER = (
    "\n\n## Global engine protocol\n\nRead the authoritative protocol with "
    "`bugx engine path --name bugx-runtime-contract.md` and "
    "`bugx engine path --name bugx-quick.md`.\n"
)


def _entry_section(key: str, locale: str | None, fallback: str) -> str:
    """按 locale 解析宿主入口**正文段**（key 相对 `host.entry.`）。"""
    try:
        import core.i18n as i18n
    except ImportError:
        try:
            from ..common import _ensure_core_importable
            _ensure_core_importable()
            import core.i18n as i18n
        except Exception:  # pragma: no cover - 引擎目录不完整时
            return fallback
    return i18n.entry_text(f"host.entry.{key}", locale=locale, fallback=fallback)


def interaction_protocol(name: str, locale: str | None) -> str:
    """宿主交互段 = 宿主叶子 + 共享启动策略段 + 41个顶层命令全景选路与操作手册。"""
    leaf = _entry_section(f"section.{name}", locale, _FALLBACK_PROTOCOL_POINTER)
    startup = _entry_section("section.startup", locale, _FALLBACK_PROTOCOL_POINTER)
    commands = _entry_section("section.commands", locale, "")
    return leaf + startup + commands




def _build_body_header(
    component: "HostComponent", project_source: str, locale: str | None = None,
) -> str:
    """宿主头部：按 locale 从目录取模板（`{source}` 为公开模板来源占位符）。"""
    template = _entry_section(
        f"header.{component.name}", locale, _FALLBACK_PROTOCOL_POINTER,
    )
    try:
        return template.format(source=project_source)
    except (KeyError, IndexError, ValueError):
        return template


def render_host_entry(component: "HostComponent", source: Path, *, locale: str | None = None) -> str:
    """使用插槽模板渲染 AI Agent 宿主入口，保证与全局协议规范逐字一致。"""
    if component.custom_renderer is not None:
        return component.custom_renderer(component, source, locale)

    project_source = "bugx engine path --name bugx.md"
    desc = get_agent_description(locale)

    # ChatGPT 插件形态特殊处理（无 YAML frontmatter）
    if component.name == "chatgpt":
        content = (
            _build_body_header(component, project_source, locale)
            + interaction_protocol("chatgpt", locale)
        )
        return content

    # 1. Frontmatter
    fm_lines = ["---", "name: bugx", f"description: {desc}"]
    if component.name == "opencode":
        fm_lines.extend(["mode: all", "temperature: 0.1"])
    elif component.tools_line:
        fm_lines.append(f"tools: {component.tools_line}")
    if component.name == "zcode":
        fm_lines.append("color: cyan")
    fm_lines.append("---\n")
    frontmatter = "\n".join(fm_lines) + "\n"

    # 2. Header
    body_header = _build_body_header(component, project_source, locale)

    # 3. OpenCode L0 router payload
    l0_append = ""
    if component.name == "opencode":
        l0_file = source.parent / "prompts" / "l0_router.md"
        l0_body = l0_file.read_text(encoding="utf-8").strip() if l0_file.is_file() else ""
        if l0_body:
            l0_append = "\n\n" + l0_body

    # 4. Interaction protocol section
    section = component.interaction.custom_protocol
    if not section:
        section = interaction_protocol(component.name, locale)

    rendered = (
        frontmatter
        + body_header
        + section
        + l0_append
    )
    return rendered
