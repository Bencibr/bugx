from __future__ import annotations

import copy
import datetime
import io
import json
import os
import platform
from pathlib import Path
import shutil
import zipfile

from .. import __version__
from ..common import (
    CliError,
    _assert_no_symlink_components,
    _atomic_write_global_file,
    _backup_host_resource,
    _i18n_exc,
    _i18n_prompt,
    _resolve_locale_name,
    get_build_id,
)
from ..host_adapters import (
    CLAUDE_WORKER_INSTALL_PATH,
    get_adapter,
    render_claude_worker_entry,
)


def _claude_user_agents_dir() -> Path:
    """Return the user-level Claude Code agents directory (~/.claude/agents)."""
    override = os.environ.get("BUGX_CLAUDE_USER_AGENTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "agents"


def _claude_user_skills_dir() -> Path:
    """Return the user-level Claude skills directory (~/.claude/skills)."""
    override = os.environ.get("BUGX_CLAUDE_USER_SKILLS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "skills"


def _claude_plugins_dir() -> Path:
    """Return the user-level Claude plugins directory (~/.claude/plugins)."""
    override = os.environ.get("BUGX_CLAUDE_PLUGINS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "plugins"


def _claude_settings_path() -> Path:
    """Return the user-level Claude settings path (~/.claude/settings.json)."""
    override = os.environ.get("BUGX_CLAUDE_SETTINGS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".claude" / "settings.json").resolve()


def _claude_global_dist_dir() -> Path:
    """Return the global Claude desktop packages distribution directory (~/.bugx/dist/claude)."""
    override = os.environ.get("BUGX_CLAUDE_GLOBAL_DIST_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".bugx" / "dist" / "claude").resolve()


def _claude_desktop_support_dir() -> Path:
    """Return the Claude Desktop user data / application support directory."""
    override = os.environ.get("BUGX_CLAUDE_DESKTOP_SUPPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home().resolve()
    if platform.system() == "Darwin":
        return home / "Library" / "Application Support" / "Claude"
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata).resolve() if appdata else home / "AppData" / "Roaming"
        return base / "Claude"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).resolve() if xdg else home / ".config"
    return base / "Claude"


def _claude_desktop_skills_dirs() -> list[Path]:
    """Return all active Claude Desktop App skills directories (under local-agent-mode-sessions/skills-plugin)."""
    override = os.environ.get("BUGX_CLAUDE_DESKTOP_SKILLS_DIR")
    if override:
        p = Path(override).expanduser().resolve()
        return [p] if p.is_dir() else []
    base = _claude_desktop_support_dir() / "local-agent-mode-sessions" / "skills-plugin"
    if not base.is_dir():
        return []
    res: list[Path] = []
    try:
        for account_dir in sorted(base.glob("*/*")):
            if account_dir.is_dir() and not account_dir.is_symlink():
                p = account_dir / "skills"
                if p.is_dir() and not p.is_symlink():
                    res.append(p.resolve())
                elif account_dir.name != "skills":
                    p.mkdir(parents=True, exist_ok=True)
                    res.append(p.resolve())
    except OSError:
        pass
    return res


def _claude_desktop_session_dirs() -> list[Path]:
    """Return all active Claude Desktop user session directories (under local-agent-mode-sessions/<accountId>/<orgId>)."""
    override = os.environ.get("BUGX_CLAUDE_DESKTOP_SESSIONS_DIR")
    if override:
        p = Path(override).expanduser().resolve()
        return [p] if p.is_dir() else []
    base = _claude_desktop_support_dir() / "local-agent-mode-sessions"
    if not base.is_dir():
        return []
    res: list[Path] = []
    try:
        for account_dir in sorted(base.iterdir()):
            if account_dir.is_dir() and not account_dir.is_symlink() and account_dir.name != "skills-plugin":
                for org_dir in sorted(account_dir.iterdir()):
                    if org_dir.is_dir() and not org_dir.is_symlink():
                        res.append(org_dir.resolve())
    except OSError:
        pass
    return res


def _claude_skill_description(locale: str | None = None) -> str:
    """Return localized description for Claude skill."""
    loc = _resolve_locale_name(locale)
    return _i18n_prompt(
        "plugin.claude_skill_description",
        "专注于代码质量与安全的自主审查智能体。专为深层缺陷排查而生——在功能写完、提 PR 或排查疑难 Bug 时，它能深入分析代码逻辑，自动编写测试用例，挖掘、复现Bug和漏洞，生成经过验证的修复补丁。随时为您的代码质量和代码安全把关。",
        locale=loc,
    )


def _sync_claude_desktop_manifest(
    manifest_path: Path,
    *,
    register: bool = True,
    locale: str | None = None,
) -> bool:
    """Register or unregister bugx skill in Claude Desktop's manifest.json."""
    if manifest_path.is_symlink():
        return False
    if not manifest_path.is_file():
        if not register or not manifest_path.parent.is_dir():
            return False
        data = {"lastUpdated": 0, "skills": []}
    else:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
    skills = data.get("skills")
    if not isinstance(skills, list):
        skills = []
        data["skills"] = skills

    changed = False
    existing_idx = None
    for idx, item in enumerate(skills):
        if isinstance(item, dict) and (item.get("skillId") == "bugx" or item.get("name") == "bugx"):
            existing_idx = idx
            break

    if register:
        desc = _claude_skill_description(locale=locale)
        entry = {
            "skillId": "bugx",
            "name": "bugx",
            "description": desc,
            "creatorType": "user",
            "syncManaged": False,
            "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "enabled": True,
        }
        if existing_idx is None:
            skills.append(entry)
            changed = True
        else:
            old = skills[existing_idx]
            if (
                not isinstance(old, dict)
                or old.get("enabled") is not True
                or old.get("description") != desc
                or old.get("creatorType") != "user"
                or old.get("syncManaged") is not False
            ):
                skills[existing_idx] = entry
                changed = True
    else:
        if existing_idx is not None:
            skills.pop(existing_idx)
            changed = True

    if changed:
        import time
        data["lastUpdated"] = int(time.time() * 1000)
        _atomic_write_global_file(
            manifest_path,
            (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
    return changed




def _claude_user_agent_boundary(*, locale: str | None = None) -> str:
    """用户级智能体入口共用的跨项目边界段。"""
    loc = _resolve_locale_name(locale)
    boundary = _i18n_prompt(
        "plugin.claude_user_agent_boundary",
        "## 用户级智能体边界（用户级形态专用）\n\n"
        "本入口安装为 Claude Code 用户级智能体（`~/.claude/agents/`，"
        "跨目录全局可用）。操作项目前先运行只读命令 `bugx engine --root . status --json`："
        "仅当返回 `UNINITIALIZED` 时，才运行 `bugx engine --root . pre` 安全准备新项目。"
        "返回 `FATAL_BLOCKED`、其他阻断状态或检查失败时，保留数据并停止，严禁用 `bugx init`、"
        "`bugx reset` 或 `--force` 绕过。Worker 子智能体遇到布局缺失时只向派发方报告，"
        "不得自行运行主控初始化命令；本入口不能替代全局引擎载荷。\n",
        locale=loc,
    )
    return "\n\n" + boundary.rstrip() + "\n"


def _claude_user_agent_content_main(source: Path, *, locale: str | None = None) -> str:
    """用户级主审计智能体入口：宿主模板渲染 + 用户级边界段。"""
    loc = _resolve_locale_name(locale)
    entry = get_adapter("claude").render(source, locale=loc)
    return entry.rstrip("\n") + _claude_user_agent_boundary(locale=loc)


def _claude_user_agent_content_worker(*, locale: str | None = None) -> str:
    """用户级并发 worker 子智能体入口：宿主模板渲染 + 用户级边界段。"""
    loc = _resolve_locale_name(locale)
    entry = render_claude_worker_entry(locale=loc)
    return entry.rstrip("\n") + _claude_user_agent_boundary(locale=loc)


_DEFAULT_CLAUDE_SKILL_CONTENT = """---
name: bugx
description: 专注于代码质量与安全的自主审查智能体。专为深层缺陷排查而生——在功能写完、提 PR 或排查疑难 Bug 时，它能深入分析代码逻辑，自动编写测试用例，挖掘、复现Bug和漏洞，生成经过验证的修复补丁。随时为您的代码质量和代码安全把关。
metadata:
  bugx_version: "0.1.0"
---
<!-- bugx-locale: zh-CN -->

# BugX for Claude

BugX Claude Skill 镜像。执行前用 `bugx engine path --name AGENT_GUIDE.md` 和 `bugx engine path --name bugx.md` 读取全局协议；项目 `.bugx/` 只保存状态与配置。

协议镜像版本：0.1.0

## Claude 原生交互适配（强制）

使用 Claude 宿主提供的 AskUserQuestion 交互控件（或结构化单选交互），一次只展示一个当前阶段的问题；生命周期门禁为 `multiSelect=false`，`question_id=tooling:batch` 必须按 `multiSelect=true` 使用多选，按选项唯一 `value` 及受控 `action` 执行，禁止从 label 猜动作；答案返回后先执行并验证当前门禁状态，再展示下一组；无效、超时、取消、身份/协议失败或工具失败必须留在当前阶段；已完成授权后的网络/DNS/超时/5xx同步失败只记录 `sync_pending` 并继续本地自动流程。
`codebase_memory_probe`/`efficiency_probe` 为 `DEGRADED` 时三选「立即安装/配置 / 本轮回退 / 下次再说」。启动前先读 `startup_environment.py`，`opencode.json` 仅作提示。

## 宿主路由（Claude Code，先于意图分流）

**先加载同插件 `bugx-claude` skill 并完成其第〇节的强制阅读**（`bugx engine path --name AGENT_GUIDE.md` / `bugx.md` / `contracts/bugx-runtime-contract.md`），再动手 claim 或 pre。该 skill 记录本机实证的坑位：证据双通道、claim 粒度、指纹漂移自锁、大响应体、worker MCP 断线回退。未读而直接试错会重复付出同等代价。

# BugX L0 路由入口（系统最高原则与阶段调度器）

## 1. 你的身份与触发边界（防误入审计模式）
你是 BugX 深度漏洞挖掘 Agent。
- **何时使用**：明确要求漏洞挖掘/代码审计或 `/bugx`；Codex 读 `bugx engine path --name prompts/codex_dialog_dispatch.md`。
- **严禁使用**：日常交流答疑、常规开发、单点调试、文档任务。非审计场景保持常规助手，禁私调门禁！
- **模式分流**：
  - *Skill 模式（单点敏捷）*：针对单文件、特定函数、当前 Diff 或已知异常验证，当前会话就地编写 PoC/回归，不启动重型门禁，即查即走。
  - *Agent 模式（全库深变异）*：全工程扫描、并发挖掘或系统级修复，单会话极易溢出。AI 首次响应须主动建议切换至 BugX 专属 Agent（独立上下文与多 Worker 并发）；若用户坚持在当前会话排查，聚焦核心路径。

## 2. 核心红线与审计纪律（不可违背）
1. **源码安全与发布边界**：默认严禁向公网或未授权远端推送被测代码及审计产物；严格在本地或沙箱内测试。
2. **严禁凭空捏造与证据闭环**：发现缺陷必须给出最小独立复现脚本（PoC）与完整调用栈证据，禁止无证据汇报或盲测。
3. **状态机与门禁防逃逸锁**：严禁在文本中自述“我选选项 1”试图绕过门禁！启动决策必须执行 `bugx engine --root . pre` 或调 MCP 写入状态机。未写入前系统拒绝分配 Worker 配额。
4. **被测工程零污染边界**：`.bugx/` 是运行态账本与证据库，严禁将私有运行时、临时脚本或凭据引入被测工程生产源码与依赖。

## 3. 生命周期阶段路由（渐进式按需装载）
请首先执行 `bugx engine --root . status` 检查当前项目运行状态：

- **阶段 0：启动门禁阶段（Gate Phase）**
  - **触发条件**：当前尚未启动或状态为 `idle` / `aborted`。
  - **装载动作**：执行 `bugx engine --root . prompt --stage gate`，或 MCP 调用 `bugx_get_stage_harness(stage="gate")`，或用 `bugx engine path --name prompts/stages/stage_gate.md`。
  - **核心目标**：完成启动门禁（云端、审查、轮询、修复、提交、报告处置）确认并生成 `startup-todo.md`。

- **阶段 1：全域覆盖勘察阶段（Explore Phase）**
  - **触发条件**：已通过 Gate 阶段，正在建立单元覆盖清单或覆盖率未达标。
  - **装载动作**：执行 `bugx engine --root . prompt --stage explore`，或 MCP 调用 `bugx_get_stage_harness(stage="explore")`，或用 `bugx engine path --name prompts/stages/stage_explore.md`。
  - **核心目标**：提取 HTTP 路由、状态机与核心函数，建立 `module-coverage.md`（运行期落在 `.bugx/pollings/<polling-id>/lifecycles/<lifecycle-id>/state/module-coverage.md`）完成黑盒基线探测。

- **阶段 2：深变异与差分推演阶段（Mutate Phase）**
  - **触发条件**：覆盖清单就绪，进入有效负载模糊测试与竞争挖掘。
  - **装载动作**：执行 `bugx engine --root . prompt --stage mutate`，或 MCP 调用 `bugx_get_stage_harness(stage="mutate", unit_id="<unit>")`，或用 `bugx engine path --name prompts/stages/stage_mutate.md`。
  - **核心目标**：驱动语法感知变异、并发竞态时序扰动与状态机反常序调用，捕获崩溃、死锁与越权穿透。

- **阶段 3：TDD 修复、补丁打包与结算复盘阶段（Repair & Settle Phase）**
  - **触发条件**：已捕获确认的 Bug 异常，或进入 Round 终局。
  - **装载动作**：执行 `bugx engine --root . prompt --stage repair`，或 MCP 调用 `bugx_get_stage_harness(stage="repair")`，或用 `bugx engine path --name prompts/stages/stage_repair.md`。
  - **核心目标**：单文件自包含复现脚本合成、最小代码修复、定向回归验证、本地红绿沙箱复验（`bugx patch-forge --test-red-green`）及寿命结算。

## 4. 专项领域知识库（L2 按需查阅）
具体任务按需查阅对应规则（优先 MCP `bugx_get_stage_harness(rule="...")` 获取，或阅读对应文件）：
- 反模式与禁止项：全局引擎 `prompts/rules/anti_patterns.md`（rule: `anti_patterns`）
- 目标代码注入对抗：全局引擎 `prompts/rules/prompt_injection_defense.md`（rule: `prompt_injection`）
- 内存泄露与资源管理：全局引擎 `prompts/rules/memory_leak_rules.md`（rule: `memory_leak`）
- 并发时序与死锁防范：全局引擎 `prompts/rules/concurrency_rules.md`（rule: `concurrency`）
"""


def _extract_manifest_build_id(manifest: dict[str, object]) -> int | None:
    """提取清单中的 build_id（优先标准 metadata，向下兼容顶层字段）。"""
    meta = manifest.get("metadata")
    if isinstance(meta, dict) and type(meta.get("bugx_build_id")) is int:
        return meta["bugx_build_id"]
    top = manifest.get("bugx_build_id")
    if type(top) is int:
        return top
    return None


def _find_or_prepare_plugin_dir(root: Path) -> Path | None:
    """Find a source plugin whose manifest identifies this exact CLI build."""
    build_id = get_build_id()
    if build_id is None:
        return None
    candidates = [
        root / "plugins" / "bugx",
        Path(__file__).resolve().parent.parent.parent.parent / "plugins" / "bugx",
        Path.home() / ".bugx" / "plugins" / "bugx",
    ]
    for c in candidates:
        try:
            _assert_no_symlink_components(c)
            for path in c.rglob("*"):
                _assert_no_symlink_components(path)
            manifest_path = c / ".claude-plugin" / "plugin.json"
            _assert_no_symlink_components(manifest_path)
            if not manifest_path.is_file():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                isinstance(manifest, dict)
                and manifest.get("name") == "bugx"
                and manifest.get("version") == __version__
                and _extract_manifest_build_id(manifest) == build_id
            ):
                return c.resolve()
        except (CliError, OSError, json.JSONDecodeError):
            continue
    return None


def _claude_plugin_manifest() -> dict[str, object]:
    """Return the official Claude plugin manifest object."""
    return {
        "name": "bugx",
        "version": __version__,
        "description": "Autonomous code review and vulnerability hunting agent that uncovers, reproduces, and fixes deep bugs.",
        "author": {
            "name": "BugX",
            "url": "https://bugx.dev",
        },
        "homepage": "https://bugx.dev",
        "skills": "./skills/",
        "agents": [
            "./agents/bugx.md",
            "./agents/bugx-worker.md",
        ],
        "metadata": {
            "bugx_build_id": get_build_id(),
        },
    }


def _claude_skill_content(root: Path, *, locale: str | None = None) -> str:
    """Read or render Claude skill content."""
    plugin_dir = _find_or_prepare_plugin_dir(root)
    if plugin_dir:
        skill_file = plugin_dir / "skills" / "bugx" / "SKILL.md"
        if skill_file.is_file():
            try:
                raw = skill_file.read_text(encoding="utf-8")
                adapted = raw.replace("# BugX for ChatGPT", "# BugX for Claude")
                adapted = adapted.replace("BugX ChatGPT Skill", "BugX Claude Skill")
                adapted = adapted.replace("## ChatGPT 原生交互适配（强制）", "## Claude 原生交互适配（强制）")
                adapted = adapted.replace(
                    "使用 ChatGPT 宿主提供的结构化选择控件",
                    "使用 Claude 宿主提供的 AskUserQuestion 交互控件（或结构化单选交互）",
                )
                adapted = adapted.replace("multiple=false", "multiSelect=false")
                adapted = adapted.replace("multiple=true", "multiSelect=true")
                return adapted
            except OSError:
                pass
    return _DEFAULT_CLAUDE_SKILL_CONTENT


def _claude_usage_skill_path(root: Path, source: Path) -> Path:
    """定位 bugx-claude 使用手册 Skill 的唯一权威源。

    与 ZCode 的 ``bugx-zcode`` 同构（见 ``plugin_zcode._zcode_usage_skill_path``）：
    该 Skill 随宿主插件一起分发，源码维护于
    ``plugins/bugx/skills/bugx-claude/SKILL.md``；``source`` 为引擎载荷根
    （``asset_root()``），其同级 ``plugins/bugx/`` 即 wheel 打包副本
    （``share/bugx-agent/plugins/bugx/skills/bugx-claude/SKILL.md``）。
    源码仓库开发态下 ``root/plugins/bugx/`` 为同一文件，作为兜底。
    """
    candidates = (
        source.parent / "plugins" / "bugx" / "skills" / "bugx-claude" / "SKILL.md",
        root / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx-claude" / "SKILL.md",
        root / "plugins" / "bugx" / "skills" / "bugx-claude" / "SKILL.md",
    )
    for candidate in candidates:
        if not candidate.is_symlink() and candidate.is_file():
            return candidate
    return candidates[0]


def _ensure_claude_usage_skill(
    root: Path,
    source: Path,
    written: list[dict[str, object]],
    backups: list[Path],
) -> dict[str, object]:
    """把 bugx-claude 使用手册 Skill 写入用户级 ``~/.claude/skills/bugx-claude``。

    与 :func:`_ensure_claude_skills` 同纪律（拒符号链接、内容一致即 unchanged、
    已有文件先备份、原子写），但**只写用户级**——这是跨项目通用的宿主手册，
    写进被测工程的 ``.claude/`` 会污染生产目录（违反「被测工程零污染边界」）。
    """
    skill_source = _claude_usage_skill_path(root, source)
    if skill_source.is_symlink() or not skill_source.is_file():
        # 不是「增强件」，是硬依赖：``bugx/SKILL.md`` 第 26 行强制要求 Claude
        # 宿主先加载本 skill 完成必读清单。缺了它，用户照着 SKILL.md 的指引
        # 去找一个不存在的文件，而 install 却报告成功。
        #
        # 之前的 ``skipped_missing_source`` 让这条断裂完全静默：干净克隆里
        # ``bugx-claude/SKILL.md`` 从未 git add，wheel 缺件、install 跳过、
        # 用户毫不知情。这里改为 fail-closed 并给出可执行的修复指引。
        raise CliError(
            _i18n_exc(
                "plugin.claude_usage_skill_missing",
                "Claude 宿主手册 Skill 缺失，无法完成安装：{path}\n"
                "该文件是 bugx/SKILL.md 要求的强制加载项，不属于可选增强件。\n"
                "修复：确认仓库内 ecosystem/plugins/bugx/skills/bugx-claude/SKILL.md "
                "已纳入版本控制（git add），并重新构建 wheel。",
                path=str(skill_source),
            )
        )
    skills_dir = _claude_user_skills_dir()
    if skills_dir.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.claude_symlink_skills_dir",
                f"拒绝写入符号链接 Claude 用户级 skills 目录: {skills_dir}",
                skills_dir=skills_dir,
            )
        )
    target = skills_dir / "bugx-claude" / "SKILL.md"
    if target.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.claude_symlink_skill_refused",
                f"拒绝覆盖符号链接 Claude Skill: {target}",
                target=target,
            )
        )
    payload = skill_source.read_bytes()
    if target.is_file() and target.read_bytes() == payload:
        written.append({
            "path": str(target), "status": "unchanged",
            "backup": None, "created": False,
        })
        return {
            "dir": str(skills_dir),
            "written": [{"path": str(target), "status": "unchanged"}],
            "status": "ok",
        }
    backup = _backup_host_resource(root, "claude", target) if target.exists() else None
    if backup is not None:
        backups.append(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_global_file(target, payload)
    written.append({
        "path": str(target),
        "status": "created" if backup is None else "upgraded",
        "backup": str(backup) if backup is not None else None,
        "created": backup is None,
    })
    return {
        "dir": str(skills_dir),
        "written": [{
            "path": str(target),
            "status": "created" if backup is None else "upgraded",
            "backup": str(backup) if backup is not None else None,
        }],
        "status": "ok",
    }


def _ensure_claude_skills(
    root: Path,
    written: list[dict[str, object]],
    backups: list[Path],
    *,
    locale: str | None = None,
) -> dict[str, object]:
    """把 Claude Skill 写入用户级 (~/.claude/skills/bugx)、项目级 (.claude/skills/bugx) 及 Claude 桌面端活动会话技能库。"""
    content = _claude_skill_content(root, locale=locale).encode("utf-8")
    user_skill = _claude_user_skills_dir() / "bugx" / "SKILL.md"
    project_skill = root / ".claude" / "skills" / "bugx" / "SKILL.md"

    targets = [user_skill, project_skill]
    desktop_targets: list[str] = []
    for d_dir in _claude_desktop_skills_dirs():
        target_skill = d_dir / "bugx" / "SKILL.md"
        targets.append(target_skill)
        desktop_targets.append(str(target_skill))

        manifest_file = d_dir.parent / "manifest.json"
        if not manifest_file.is_file() and (d_dir / "manifest.json").is_file():
            manifest_file = d_dir / "manifest.json"
        existed = manifest_file.is_file()
        if _sync_claude_desktop_manifest(manifest_file, register=True, locale=locale):
            written.append({
                "path": str(manifest_file),
                "status": "created" if not existed else "upgraded",
                "backup": None,
                "created": not existed,
            })

    res: dict[str, object] = {
        "user": str(user_skill),
        "project": str(project_skill),
        "desktop": desktop_targets,
    }

    for target in targets:
        if target.is_symlink():
            raise CliError(
                _i18n_exc(
                    "plugin.claude_symlink_skill_refused",
                    f"拒绝覆盖符号链接 Claude Skill: {target}",
                    target=target,
                )
            )
        if target.is_file() and target.read_bytes() == content:
            written.append({
                "path": str(target),
                "status": "unchanged",
                "backup": None,
                "created": False,
            })
            continue
        backup = _backup_host_resource(root, "claude", target) if target.exists() else None
        if backup is not None:
            backups.append(backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_global_file(target, content)
        written.append({
            "path": str(target),
            "status": "created" if backup is None else "upgraded",
            "backup": str(backup) if backup is not None else None,
            "created": backup is None,
        })

    return res


def _build_claude_desktop_packages(
    root: Path,
    written: list[dict[str, object]],
    *,
    locale: str | None = None,
) -> dict[str, object]:
    """打包 bugx.plugin、bugx.skill 与 bugx.dxt (含 unpacked)，放置到项目 .claude/dist 与全局 ~/.bugx/dist/claude。"""
    plugin_dir = _find_or_prepare_plugin_dir(root)
    skill_content = _claude_skill_content(root, locale=locale).encode("utf-8")

    # 构建 in-memory plugin zip
    plugin_buf = io.BytesIO()
    with zipfile.ZipFile(plugin_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if plugin_dir and plugin_dir.is_dir():
            for p in sorted(plugin_dir.rglob("*")):
                _assert_no_symlink_components(p)
                if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts:
                    arcname = str(p.relative_to(plugin_dir))
                    zf.writestr(arcname, p.read_bytes())
        else:
            manifest = _claude_plugin_manifest()
            zf.writestr(".claude-plugin/plugin.json", (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
            zf.writestr("skills/bugx/SKILL.md", skill_content)
            main_agent = _claude_user_agent_content_main(root, locale=locale).encode("utf-8")
            worker_agent = _claude_user_agent_content_worker(locale=locale).encode("utf-8")
            zf.writestr("agents/bugx.md", main_agent)
            zf.writestr("agents/bugx-worker.md", worker_agent)
    plugin_bytes = plugin_buf.getvalue()

    # 构建 in-memory skill zip（严格遵循 Claude 官方 Skill 打包规范：单根目录 <name>/SKILL.md）
    skill_buf = io.BytesIO()
    with zipfile.ZipFile(skill_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bugx/SKILL.md", skill_content)
    skill_bytes = skill_buf.getvalue()

    # 构建 in-memory dxt zip (Claude Desktop Extension)
    dxt_manifest = {
        "manifest_version": "0.4",
        "name": "bugx",
        "display_name": "BugX",
        "version": __version__,
        "bugx_build_id": get_build_id(),
        "description": "专注于代码质量与安全的自主审查智能体。专为深层缺陷排查而生——在功能写完、提 PR 或排查疑难 Bug 时，它能深入分析代码逻辑，自动编写测试用例，挖掘、复现Bug和漏洞，生成经过验证的修复补丁。随时为您的代码质量和代码安全把关。",
        "author": {
            "name": "BugX",
            "url": "https://bugx.dev",
        },
        "homepage": "https://bugx.dev",
        "server": {
            "type": "binary",
            "entry_point": _resolved_bugx_command(),
            "mcp_config": {
                "command": _resolved_bugx_command(),
                "args": ["mcp", "serve", "--root", ".", "--role", "host"],
            },
        },
    }
    dxt_manifest_bytes = (json.dumps(dxt_manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    dxt_buf = io.BytesIO()
    with zipfile.ZipFile(dxt_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", dxt_manifest_bytes)
        zf.writestr("skills/bugx/SKILL.md", skill_content)
    dxt_bytes = dxt_buf.getvalue()

    project_dist = root / ".claude" / "dist"
    global_dist = _claude_global_dist_dir()

    for base_dir in (project_dist, global_dist):
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            _assert_no_symlink_components(base_dir)
            for filename, data in (
                ("bugx.plugin", plugin_bytes),
                ("bugx.skill", skill_bytes),
                ("bugx.zip", skill_bytes),
                ("bugx.dxt", dxt_bytes),
            ):
                target = base_dir / filename
                if target.is_symlink():
                    continue
                if target.is_file() and target.read_bytes() == data:
                    written.append({
                        "path": str(target),
                        "status": "unchanged",
                        "backup": None,
                        "created": False,
                    })
                    continue
                _atomic_write_global_file(target, data)
                written.append({
                    "path": str(target),
                    "status": "created",
                    "backup": None,
                    "created": True,
                })

            # 输出 unpacked 扩展目录供本地解包加载
            unpacked_dir = base_dir / "unpacked"
            unpacked_dir.mkdir(parents=True, exist_ok=True)
            unpacked_manifest = unpacked_dir / "manifest.json"
            if not unpacked_manifest.exists() or unpacked_manifest.read_bytes() != dxt_manifest_bytes:
                _atomic_write_global_file(unpacked_manifest, dxt_manifest_bytes)
            unpacked_skill = unpacked_dir / "skills" / "bugx" / "SKILL.md"
            unpacked_skill.parent.mkdir(parents=True, exist_ok=True)
            if not unpacked_skill.exists() or unpacked_skill.read_bytes() != skill_content:
                _atomic_write_global_file(unpacked_skill, skill_content)
        except OSError:
            pass

    return {
        "dist": str(project_dist),
        "global_dist": str(global_dist),
    }


def _ensure_claude_plugin_registry(
    root: Path,
    written: list[dict[str, object]],
    backups: list[Path],
    *,
    locale: str | None = None,
) -> dict[str, object]:
    """在 ~/.claude/plugins 与 ~/.claude/settings.json 中注册 BugX 本地插件与 marketplace。"""
    plugin_dir = _find_or_prepare_plugin_dir(root)
    plugins_dir = _claude_plugins_dir()
    cache_dir = plugins_dir / "cache" / "bugx" / "bugx" / "0.1.0"

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # 1. 复制/同步插件缓存 ~/.claude/plugins/cache/bugx/bugx/0.1.0
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if plugin_dir and plugin_dir.is_dir():
            for p in sorted(plugin_dir.rglob("*")):
                _assert_no_symlink_components(p)
                if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts:
                    rel = p.relative_to(plugin_dir)
                    dst = cache_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    payload = p.read_bytes()
                    if not dst.exists() or dst.read_bytes() != payload:
                        _atomic_write_global_file(dst, payload)
        else:
            manifest = _claude_plugin_manifest()
            manifest_target = cache_dir / ".claude-plugin" / "plugin.json"
            manifest_target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_global_file(
                manifest_target,
                (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            )
            skill_target = cache_dir / "skills" / "bugx" / "SKILL.md"
            skill_target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_global_file(
                skill_target,
                _claude_skill_content(root, locale=locale).encode("utf-8"),
            )
            agent_main = cache_dir / "agents" / "bugx.md"
            agent_worker = cache_dir / "agents" / "bugx-worker.md"
            agent_main.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_global_file(
                agent_main,
                _claude_user_agent_content_main(root, locale=locale).encode("utf-8"),
            )
            _atomic_write_global_file(
                agent_worker,
                _claude_user_agent_content_worker(locale=locale).encode("utf-8"),
            )
    except OSError:
        pass

    install_path_str = str(cache_dir.resolve())
    plugin_src_str = str(plugin_dir.resolve()) if plugin_dir else install_path_str

    # 2. ~/.claude/plugins/installed_plugins.json
    installed_path = plugins_dir / "installed_plugins.json"
    installed_data: dict[str, object] = {"version": 2, "plugins": {}}
    if installed_path.is_file() and not installed_path.is_symlink():
        try:
            loaded = json.loads(installed_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("plugins"), dict):
                installed_data = loaded
        except (OSError, json.JSONDecodeError):
            pass

    plugins_map = installed_data.setdefault("plugins", {})
    if isinstance(plugins_map, dict):
        plugins_map["bugx@bugx"] = [
            {
                "scope": "user",
                "installPath": install_path_str,
                "version": __version__,
                "bugx_build_id": get_build_id(),
                "installedAt": now_iso,
                "lastUpdated": now_iso,
            }
        ]
    try:
        plugins_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_global_file(
            installed_path,
            (json.dumps(installed_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        written.append({
            "path": str(installed_path),
            "status": "updated",
            "backup": None,
            "created": False,
        })
    except OSError:
        pass

    # 3. ~/.claude/plugins/known_marketplaces.json
    known_path = plugins_dir / "known_marketplaces.json"
    known_data: dict[str, object] = {}
    if known_path.is_file() and not known_path.is_symlink():
        try:
            loaded = json.loads(known_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                known_data = loaded
        except (OSError, json.JSONDecodeError):
            pass

    known_data["bugx"] = {
        "source": {
            "source": "directory",
            "path": plugin_src_str,
        },
        "installLocation": plugin_src_str,
        "lastUpdated": now_iso,
    }
    try:
        _atomic_write_global_file(
            known_path,
            (json.dumps(known_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        written.append({
            "path": str(known_path),
            "status": "updated",
            "backup": None,
            "created": False,
        })
    except OSError:
        pass

    # 4. ~/.claude/settings.json (enabledPlugins & extraKnownMarketplaces)
    settings_path = _claude_settings_path()
    settings_data: dict[str, object] = {}
    if settings_path.is_file() and not settings_path.is_symlink():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings_data = loaded
        except (OSError, json.JSONDecodeError):
            pass

    enabled_plugins = settings_data.setdefault("enabledPlugins", {})
    if isinstance(enabled_plugins, dict):
        enabled_plugins["bugx@bugx"] = True

    extra_marketplaces = settings_data.setdefault("extraKnownMarketplaces", {})
    if isinstance(extra_marketplaces, dict):
        extra_marketplaces["bugx"] = {
            "source": {
                "source": "directory",
                "path": plugin_src_str,
            }
        }

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_global_file(
            settings_path,
            (json.dumps(settings_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        written.append({
            "path": str(settings_path),
            "status": "updated",
            "backup": None,
            "created": False,
        })
    except OSError:
        pass

    return {
        "registered": True,
        "plugin": "bugx@bugx",
        "install_path": install_path_str,
    }


def _ensure_claude_desktop_cowork_plugins(
    root: Path,
    written: list[dict[str, object]],
    backups: list[Path],
    *,
    locale: str | None = None,
) -> dict[str, object]:
    """Deploy BugX plugin and marketplace directly into Claude Desktop's cowork_plugins registry."""
    session_dirs = _claude_desktop_session_dirs()
    if not session_dirs:
        return {"installed_sessions": 0}

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    plugin_desc = _claude_skill_description(locale=locale)
    skill_content = _claude_skill_content(root, locale=locale).encode("utf-8")
    agent_main_content = _claude_user_agent_content_main(root, locale=locale).encode("utf-8")
    agent_worker_content = _claude_user_agent_content_worker(locale=locale).encode("utf-8")

    plugin_manifest = copy.deepcopy(_claude_plugin_manifest())
    if plugin_desc:
        plugin_manifest["description"] = plugin_desc
    plugin_manifest_bytes = (json.dumps(plugin_manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    marketplace_name = "local-desktop-app-uploads"
    installed_count = 0

    for s_dir in session_dirs:
        cowork_dir = s_dir / "cowork_plugins"
        marketplaces_dir = cowork_dir / "marketplaces"
        local_marketplace = marketplaces_dir / marketplace_name
        bugx_plugin_dir = local_marketplace / "bugx"

        # 1. Ensure marketplace manifest exists and includes bugx
        marketplace_meta_dir = local_marketplace / ".claude-plugin"
        marketplace_meta_dir.mkdir(parents=True, exist_ok=True)
        marketplace_json_path = marketplace_meta_dir / "marketplace.json"

        m_data: dict[str, object] = {
            "name": marketplace_name,
            "version": "0.1.0",
            "description": "Locally uploaded plugins via Claude Desktop app",
            "owner": {"name": "Local User"},
            "plugins": [],
        }
        if marketplace_json_path.is_file() and not marketplace_json_path.is_symlink():
            try:
                loaded = json.loads(marketplace_json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    m_data = loaded
            except (OSError, json.JSONDecodeError):
                pass

        plugins_list = m_data.setdefault("plugins", [])
        if isinstance(plugins_list, list):
            found = False
            for item in plugins_list:
                if isinstance(item, dict) and item.get("name") == "bugx":
                    item["version"] = "0.1.0"
                    item["bugx_build_id"] = get_build_id()
                    item["source"] = "./bugx"
                    found = True
                    break
            if not found:
                plugins_list.append({
                    "name": "bugx",
                    "version": "0.1.0",
                    "bugx_build_id": get_build_id(),
                    "source": "./bugx",
                })

        _atomic_write_global_file(
            marketplace_json_path,
            (json.dumps(m_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )

        # 2. Write plugin files in bugx directory
        bugx_plugin_dir.mkdir(parents=True, exist_ok=True)

        # .claude-plugin/plugin.json
        meta_dir = bugx_plugin_dir / ".claude-plugin"
        meta_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_global_file(meta_dir / "plugin.json", plugin_manifest_bytes)

        # skills/bugx/SKILL.md
        skill_dir = bugx_plugin_dir / "skills" / "bugx"
        skill_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_global_file(skill_dir / "SKILL.md", skill_content)

        # agents/bugx.md and agents/bugx-worker.md
        agents_dir = bugx_plugin_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_global_file(agents_dir / "bugx.md", agent_main_content)
        _atomic_write_global_file(agents_dir / "bugx-worker.md", agent_worker_content)

        # 3. Write/update known_marketplaces.json
        known_path = cowork_dir / "known_marketplaces.json"
        known_data: dict[str, object] = {}
        if known_path.is_file() and not known_path.is_symlink():
            try:
                loaded = json.loads(known_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    known_data = loaded
            except (OSError, json.JSONDecodeError):
                pass
        known_data[marketplace_name] = {
            "source": {
                "source": "directory",
                "path": str(local_marketplace.resolve()),
            },
            "installLocation": str(local_marketplace.resolve()),
            "lastUpdated": now_iso,
        }
        _atomic_write_global_file(
            known_path,
            (json.dumps(known_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )

        # 4. Write/update installed_plugins.json
        installed_path = cowork_dir / "installed_plugins.json"
        installed_data: dict[str, object] = {"version": 2, "plugins": {}}
        if installed_path.is_file() and not installed_path.is_symlink():
            try:
                loaded = json.loads(installed_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("plugins"), dict):
                    installed_data = loaded
            except (OSError, json.JSONDecodeError):
                pass

        plugins_map = installed_data.setdefault("plugins", {})
        if isinstance(plugins_map, dict):
            plugin_key = f"bugx@{marketplace_name}"
            plugins_map[plugin_key] = [
                {
                    "scope": "user",
                    "installPath": str(bugx_plugin_dir.resolve()),
                    "version": __version__,
                    "bugx_build_id": get_build_id(),
                    "installedAt": now_iso,
                    "lastUpdated": now_iso,
                }
            ]
        _atomic_write_global_file(
            installed_path,
            (json.dumps(installed_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )

        # 5. Write/update cowork_settings.json
        settings_path = s_dir / "cowork_settings.json"
        settings_data: dict[str, object] = {}
        if settings_path.is_file() and not settings_path.is_symlink():
            try:
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    settings_data = loaded
            except (OSError, json.JSONDecodeError):
                pass
        enabled = settings_data.setdefault("enabledPlugins", {})
        if isinstance(enabled, dict):
            enabled[f"bugx@{marketplace_name}"] = True
        _atomic_write_global_file(
            settings_path,
            (json.dumps(settings_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )

        installed_count += 1
        written.append({
            "path": str(bugx_plugin_dir),
            "status": "created",
            "backup": None,
            "created": True,
        })

    return {"installed_sessions": installed_count}


def _ensure_claude_user_agents(
    root: Path,
    source: Path,
    *,
    locale: str | None = None,
) -> dict[str, object]:
    """把主/worker 智能体、技能、桌面安装包与插件注册写入 Claude Code 与 Claude Desktop。"""
    agents_dir = _claude_user_agents_dir()
    if agents_dir.is_symlink():
        raise CliError(
            _i18n_exc(
                "plugin.claude_symlink_agents_dir",
                f"拒绝写入符号链接 Claude Code 用户级 agents 目录: {agents_dir}",
                agents_dir=agents_dir,
            )
        )
    agents_dir.mkdir(parents=True, exist_ok=True)
    from ..host_adapters import render_claude_agent_entry, render_claude_worker_entry
    user_generated = {
        "bugx.md": _claude_user_agent_content_main(source, locale=locale),
        "bugx-worker.md": _claude_user_agent_content_worker(locale=locale),
    }
    project_generated = {
        "bugx.md": render_claude_agent_entry(source, locale=locale),
        "bugx-worker.md": render_claude_worker_entry(locale=locale),
    }
    written: list[dict[str, object]] = []
    backups: list[Path] = []

    target_dirs = [agents_dir]
    project_agents_dir = root / ".claude" / "agents"
    try:
        if project_agents_dir.resolve() != agents_dir.resolve():
            target_dirs.append(project_agents_dir)
    except OSError:
        target_dirs.append(project_agents_dir)

    for current_dir in target_dirs:
        if current_dir.is_symlink():
            raise CliError(
                _i18n_exc(
                    "plugin.claude_symlink_agents_dir",
                    f"拒绝写入符号链接 Claude Code agents 目录: {current_dir}",
                    agents_dir=current_dir,
                )
            )
        current_dir.mkdir(parents=True, exist_ok=True)
        current_generated = user_generated if current_dir == agents_dir else project_generated
        for filename, content in current_generated.items():
            target = current_dir / filename
            payload = content.encode("utf-8")
            if target.is_symlink():
                raise CliError(
                    _i18n_exc(
                        "plugin.claude_symlink_agent_refused",
                        f"拒绝覆盖符号链接 Claude Code 用户级智能体: {target}",
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
            backup = _backup_host_resource(root, "claude", target) if target.exists() else None
            if backup is not None:
                backups.append(backup)
            _atomic_write_global_file(target, payload)
            written.append({
                "path": str(target),
                "status": "created" if backup is None else "upgraded",
                "backup": str(backup) if backup is not None else None,
                "created": backup is None,
            })

    # 1. 部署 Claude Skills (用户级 ~/.claude/skills/bugx 与项目级 .claude/skills/bugx)
    skills_result = _ensure_claude_skills(root, written, backups, locale=locale)
    # 1b. 用户级宿主手册 bugx-claude（跨项目可见，仅用户级；对齐 ZCode 的 bugx-zcode）
    skills_result["host_manual"] = _ensure_claude_usage_skill(root, source, written, backups)

    # 2. 编译并输出 Claude Desktop 安装包 (.claude/dist 与 ~/.bugx/dist/claude)
    packages_result = _build_claude_desktop_packages(root, written, locale=locale)

    # 3. 注册 Claude 本地插件生态 (~/.claude/plugins/installed_plugins.json, known_marketplaces.json, settings.json)
    plugins_result = _ensure_claude_plugin_registry(root, written, backups, locale=locale)

    # 4. 注册 Claude Desktop 专用 cowork_plugins 插件生态
    desktop_plugins_result = None
    if _claude_desktop_available():
        desktop_plugins_result = _ensure_claude_desktop_cowork_plugins(root, written, backups, locale=locale)

    return {
        "dir": str(agents_dir),
        "desktop_plugins": desktop_plugins_result,
        "skills": skills_result,
        "packages": packages_result,
        "plugins": plugins_result,
        "written": written,
        "backups": backups,
    }


def _resolved_bugx_command() -> str:
    cmd = shutil.which("bugx")
    if cmd:
        return cmd
    return "bugx"


def _claude_desktop_available() -> bool:
    """Detect the desktop skill host without reading its MCP configuration."""
    if os.environ.get("BUGX_CLAUDE_DESKTOP_SUPPORT_DIR"):
        return True
    from ..host_adapters import _claude_app_markers
    return any(marker.exists() for marker in _claude_app_markers())


def _remove_claude_project_skills_and_dist(
    root: Path,
    removed: list[str],
    preserved: list[str],
    *,
    backup_dir: Path | None = None,
) -> None:
    """清理项目级 Claude 技能与安装包目录 (.claude/skills/bugx 与 .claude/dist)。"""
    project_skill = root / ".claude" / "skills" / "bugx"
    if project_skill.is_dir() and not project_skill.is_symlink():
        try:
            shutil.rmtree(project_skill)
            removed.append(f"{project_skill}（项目级 Claude Skill）")
        except OSError as exc:
            preserved.append(f"{project_skill}（删除失败：{exc}）")

    project_dist = root / ".claude" / "dist"
    _remove_claude_dist_assets(project_dist, removed, preserved, label="项目级 Claude 桌面安装包")

    from ..common import _remove_empty_parents
    _remove_empty_parents(root, [".claude/skills/bugx", ".claude/dist"])


def _remove_claude_user_skills(
    removed: list[str],
    preserved: list[str],
    *,
    backup_root: Path | None = None,
) -> None:
    """清理全局 Claude 用户级技能 (~/.claude/skills/bugx) 及 Claude Desktop 活动技能库。"""
    user_skill = _claude_user_skills_dir() / "bugx"
    if user_skill.is_dir() and not user_skill.is_symlink():
        try:
            shutil.rmtree(user_skill)
            removed.append(f"{user_skill}（Claude 用户级 Skill）")
        except OSError as exc:
            preserved.append(f"{user_skill}（删除失败：{exc}）")

    host_manual = _claude_user_skills_dir() / "bugx-claude"
    if host_manual.is_dir() and not host_manual.is_symlink():
        try:
            shutil.rmtree(host_manual)
            removed.append(f"{host_manual}（Claude 用户级宿主手册 Skill）")
        except OSError as exc:
            preserved.append(f"{host_manual}（删除失败：{exc}）")

    for d_dir in _claude_desktop_skills_dirs():
        target = d_dir / "bugx"
        if target.is_dir() and not target.is_symlink():
            try:
                shutil.rmtree(target)
                removed.append(f"{target}（Claude 桌面端 Skill）")
            except OSError as exc:
                preserved.append(f"{target}（删除失败：{exc}）")

        manifest_file = d_dir.parent / "manifest.json"
        if not manifest_file.is_file() and (d_dir / "manifest.json").is_file():
            manifest_file = d_dir / "manifest.json"
        if manifest_file.is_file():
            if _sync_claude_desktop_manifest(manifest_file, register=False):
                removed.append(f"{manifest_file}（已注销 Claude 桌面端 manifest.json bugx 技能）")

    desktop_support = _claude_desktop_support_dir()
    if desktop_support.is_dir():
        ext_id = "local.dxt.bugx.bugx"
        ext_dir = desktop_support / "Claude Extensions" / ext_id
        if ext_dir.is_dir() and not ext_dir.is_symlink():
            try:
                shutil.rmtree(ext_dir)
                removed.append(f"{ext_dir}（Claude 桌面端扩展）")
            except OSError as exc:
                preserved.append(f"{ext_dir}（删除失败：{exc}）")

        setting_file = desktop_support / "Claude Extensions Settings" / f"{ext_id}.json"
        if setting_file.is_file() and not setting_file.is_symlink():
            try:
                setting_file.unlink()
                removed.append(f"{setting_file}（Claude 桌面端扩展配置）")
            except OSError as exc:
                preserved.append(f"{setting_file}（删除失败：{exc}）")

        inst_file = desktop_support / "extensions-installations.json"
        if inst_file.is_file() and not inst_file.is_symlink():
            try:
                loaded = json.loads(inst_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("extensions"), dict):
                    if ext_id in loaded["extensions"]:
                        del loaded["extensions"][ext_id]
                        _atomic_write_global_file(
                            inst_file,
                            (json.dumps(loaded, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                        )
                        removed.append(f"{inst_file}（已注销 Claude 桌面端扩展元数据）")
            except (OSError, json.JSONDecodeError) as exc:
                preserved.append(f"{inst_file}（注销失败：{exc}）")

    _remove_claude_desktop_cowork_plugins(removed, preserved)


def _remove_claude_desktop_cowork_plugins(
    removed: list[str],
    preserved: list[str],
) -> None:
    """清理 Claude Desktop 所有 session 下的 bugx cowork_plugins 登记与文件。"""
    marketplace_name = "local-desktop-app-uploads"
    plugin_key = f"bugx@{marketplace_name}"

    for s_dir in _claude_desktop_session_dirs():
        cowork_dir = s_dir / "cowork_plugins"
        for inst_name in ("installed_plugins.json", "installed_plugins_v2.json"):
            installed_path = cowork_dir / inst_name
            if installed_path.is_file() and not installed_path.is_symlink():
                try:
                    data = json.loads(installed_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
                        if plugin_key in data["plugins"]:
                            del data["plugins"][plugin_key]
                            _atomic_write_global_file(
                                installed_path,
                                (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                            )
                            removed.append(f"{installed_path}（已注销 Claude 桌面端 bugx 插件）")
                except (OSError, json.JSONDecodeError) as exc:
                    preserved.append(f"{installed_path}（注销失败：{exc}）")

        settings_path = s_dir / "cowork_settings.json"
        if settings_path.is_file() and not settings_path.is_symlink():
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    enabled = data.get("enabledPlugins")
                    if isinstance(enabled, dict) and plugin_key in enabled:
                        del enabled[plugin_key]
                        _atomic_write_global_file(
                            settings_path,
                            (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                        )
                        removed.append(f"{settings_path}（已从 Claude 桌面端设置注销 bugx 启用）")
            except (OSError, json.JSONDecodeError) as exc:
                preserved.append(f"{settings_path}（设置注销失败：{exc}）")

        local_marketplace = cowork_dir / "marketplaces" / marketplace_name
        marketplace_json_path = local_marketplace / ".claude-plugin" / "marketplace.json"
        if marketplace_json_path.is_file() and not marketplace_json_path.is_symlink():
            try:
                m_data = json.loads(marketplace_json_path.read_text(encoding="utf-8"))
                if isinstance(m_data, dict) and isinstance(m_data.get("plugins"), list):
                    m_data["plugins"] = [p for p in m_data["plugins"] if isinstance(p, dict) and p.get("name") != "bugx"]
                    _atomic_write_global_file(
                        marketplace_json_path,
                        (json.dumps(m_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                    )
            except (OSError, json.JSONDecodeError):
                pass

        bugx_dir = local_marketplace / "bugx"
        if bugx_dir.is_dir() and not bugx_dir.is_symlink():
            try:
                shutil.rmtree(bugx_dir)
                removed.append(f"{bugx_dir}（Claude 桌面端插件目录）")
            except OSError as exc:
                preserved.append(f"{bugx_dir}（删除失败：{exc}）")


def _remove_claude_plugin_registry(
    removed: list[str],
    preserved: list[str],
    *,
    backup_root: Path | None = None,
) -> None:
    """注销 Claude 插件登记与清理全局缓存及安装包。"""
    plugins_dir = _claude_plugins_dir()

    # 1. ~/.claude/plugins/installed_plugins.json & installed_plugins_v2.json
    for inst_name in ("installed_plugins.json", "installed_plugins_v2.json"):
        installed_path = plugins_dir / inst_name
        if installed_path.is_file() and not installed_path.is_symlink():
            try:
                data = json.loads(installed_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
                    if "bugx@bugx" in data["plugins"]:
                        del data["plugins"]["bugx@bugx"]
                        _atomic_write_global_file(
                            installed_path,
                            (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                        )
                        removed.append(f"{installed_path}（已注销 bugx 插件）")
            except (OSError, json.JSONDecodeError) as exc:
                preserved.append(f"{installed_path}（注销失败：{exc}）")

    # 2. ~/.claude/plugins/known_marketplaces.json
    known_path = plugins_dir / "known_marketplaces.json"
    if known_path.is_file() and not known_path.is_symlink():
        try:
            data = json.loads(known_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "bugx" in data:
                del data["bugx"]
                _atomic_write_global_file(
                    known_path,
                    (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                )
                removed.append(f"{known_path}（已移除 bugx marketplace）")
        except (OSError, json.JSONDecodeError) as exc:
            preserved.append(f"{known_path}（移除失败：{exc}）")

    # 3. ~/.claude/settings.json (enabledPlugins & extraKnownMarketplaces)
    settings_path = _claude_settings_path()
    if settings_path.is_file() and not settings_path.is_symlink():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            changed = False
            if isinstance(data, dict):
                enabled = data.get("enabledPlugins")
                if isinstance(enabled, dict) and "bugx@bugx" in enabled:
                    del enabled["bugx@bugx"]
                    changed = True
                extra = data.get("extraKnownMarketplaces")
                if isinstance(extra, dict) and "bugx" in extra:
                    del extra["bugx"]
                    changed = True
                if changed:
                    _atomic_write_global_file(
                        settings_path,
                        (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                    )
                    removed.append(f"{settings_path}（已从 Claude 设置注销 bugx 插件与 marketplace）")
        except (OSError, json.JSONDecodeError) as exc:
            preserved.append(f"{settings_path}（设置注销失败：{exc}）")

    # 4. ~/.claude/plugins/cache/bugx
    cache_dir = plugins_dir / "cache" / "bugx"
    if cache_dir.is_dir() and not cache_dir.is_symlink():
        try:
            shutil.rmtree(cache_dir)
            removed.append(f"{cache_dir}（Claude 插件缓存）")
        except OSError as exc:
            preserved.append(f"{cache_dir}（删除失败：{exc}）")

    # 5. ~/.bugx/dist/claude
    global_dist = _claude_global_dist_dir()
    _remove_claude_dist_assets(global_dist, removed, preserved, label="全局 Claude 桌面安装包")

    # 6. Claude Desktop cowork_plugins
    _remove_claude_desktop_cowork_plugins(removed, preserved)


def _remove_claude_dist_assets(
    dist_dir: Path,
    removed: list[str],
    preserved: list[str],
    *,
    label: str,
) -> None:
    """Remove BugX's fixed dist files and retain all co-located foreign assets."""
    if not dist_dir.is_dir() or dist_dir.is_symlink():
        return
    try:
        _assert_no_symlink_components(dist_dir)
        generated = (
            dist_dir / "bugx.plugin",
            dist_dir / "bugx.skill",
            dist_dir / "bugx.zip",
            dist_dir / "bugx.dxt",
            dist_dir / "unpacked" / "manifest.json",
            dist_dir / "unpacked" / "skills" / "bugx" / "SKILL.md",
        )
        for path in generated:
            if path.is_symlink():
                continue
            _assert_no_symlink_components(path)
            if path.is_file():
                path.unlink()
                removed.append(f"{path}（{label}）")
        for directory in (
            dist_dir / "unpacked" / "skills" / "bugx",
            dist_dir / "unpacked" / "skills",
            dist_dir / "unpacked",
            dist_dir,
        ):
            if directory.is_dir() and not directory.is_symlink():
                directory.rmdir()
    except OSError as exc:
        preserved.append(f"{dist_dir}（删除失败：{exc}）")
