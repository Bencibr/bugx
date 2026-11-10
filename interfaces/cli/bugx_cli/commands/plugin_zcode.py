#!/usr/bin/env python3
"""src/bugx_cli/commands/plugin_zcode.py - ZCode 插件与用户级智能体适配命令 (DOC-072)。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .. import __version__
from ..host_adapters import (
    ZCODE_WORKER_INSTALL_PATH,
    get_adapter,
    render_zcode_worker_entry,
)
from ..common import (
    CHATGPT_PLUGIN_NAME,
    CliError,
    asset_root,
    global_engine_root,
    _assert_no_symlink_components,
    _atomic_write_global_file,
    _backup_host_resource,
    _i18n_exc,
    _i18n_prompt,
    _remove_empty_parents,
    _resolve_locale_name,
    _resolve_plugin_version,
    _write_host_resource,
    strip_locale_marker,
)


# ZCode 官方规范（plugin-creator plugin-json-spec）要求 icon 必须是「可达的
# HTTPS URL；资产缺失时宁可省略也不得伪造」。插件图标使用公开品牌资源；如果
# 资源不可达，应省略图标而不是写入无法验证的镜像地址。
ZCODE_PLUGIN_ICON_URL = "https://bugx.dev/brand-mark.svg"


def _zcode_plugin_manifest(version: str = __version__) -> dict[str, object]:
    """ZCode 插件清单：声明 ZCode 插件元数据与图标。

    ZCode 在 ``.zcode-plugin/plugin.json`` 中按目录名加载 ``agents`` 与
    ``skills`` 等组件字段；当前镜像贡献主审计 agent 与并发 worker 子智能体。
    UI 渲染器根据 ``icon`` / ``listing.icon`` (https:// 格式) 显示自定义图标。
    """
    icon_url = ZCODE_PLUGIN_ICON_URL
    return {
        "name": CHATGPT_PLUGIN_NAME,
        "version": version,
        "description": "Autonomous code review and vulnerability hunting agent that uncovers, reproduces, and fixes deep bugs.",
        "homepage": "https://bugx.dev",
        "author": {
            "name": "BugX",
            "url": "https://bugx.dev",
        },
        "source": "local",
        "icon": icon_url,
        "listing": {
            "displayName": "BugX",
            "icon": icon_url,
            "category": "developer-tools",
        },
        "agents": "agents",
        "skills": "skills",
    }


def _zcode_plugin_boundary_section(agent_ref: str, *, locale: str | None = None) -> str:
    """插件形态入口共用的用户级边界段；agent_ref 为插件内的完整 agent 命名。"""
    loc = _resolve_locale_name(locale)
    boundary = _i18n_prompt(
        "plugin.zcode_plugin_boundary",
        "## 用户级插件边界（插件形态专用）\n\n"
        f"本入口以 ZCode 插件（agent 命名 `{agent_ref}`）注册，可跨项目使用。"
        "协议由全局引擎通过 `bugx engine path --name bugx-runtime-contract.md` 提供。"
        "操作项目前先运行只读命令 `bugx engine --root . status --json`：仅当返回 `UNINITIALIZED` 时，"
        "才运行 `bugx engine --root . pre` 安全准备新项目。返回 `FATAL_BLOCKED`、其他阻断状态或检查失败时，"
        "保留数据并停止，严禁用 `bugx init`、`bugx reset` 或 `--force` 绕过。"
        "Worker 子智能体遇到布局缺失时只向派发方报告，不得自行运行主控初始化命令；本入口不能替代全局引擎载荷。\n",
        locale=loc,
        agent_ref=agent_ref,
    )
    return "\n\n" + boundary.rstrip() + "\n"


def _zcode_plugin_agent_content(source: Path, *, locale: str | None = None) -> str:
    """插件形态的 ZCode Agent 入口：项目级入口 + 用户级插件边界段。"""
    loc = _resolve_locale_name(locale)
    entry = get_adapter("zcode").render(source, locale=loc)
    return entry.rstrip("\n") + _zcode_plugin_boundary_section("bugx:bugx", locale=loc)


def _zcode_plugin_worker_content(*, locale: str | None = None) -> str:
    """插件形态的 ZCode 并发 worker 子智能体入口。

    与项目级 worker 入口同源（render_zcode_worker_entry），仅追加用户级
    插件边界段；跨项目派发时 agent 命名为 `bugx:bugx-worker`。
    """
    loc = _resolve_locale_name(locale)
    entry = render_zcode_worker_entry(locale=loc)
    return entry.rstrip("\n") + _zcode_plugin_boundary_section("bugx:bugx-worker", locale=loc)


def _zcode_storage_dir_override() -> Path:
    """解析 ZCode 的 storage.dir（桌面端 bootstrap 的 storageRoot）。

    默认 ``~/.zcode``；可在宿主配置 ``storage.dir`` 中覆盖（``~/`` 前缀按
    HOME 展开，与 ZCode 自身的 ws() 语义一致）。依次探测 CLI 与 v2 配置，
    都未设置时返回默认值。
    """
    home = Path.home()
    for config_path in (
        home / ".zcode" / "cli" / "config.json",
        home / ".zcode" / "v2" / "config.json",
    ):
        try:
            if config_path.is_symlink() or not config_path.is_file():
                continue
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        storage = payload.get("storage") if isinstance(payload, dict) else None
        raw = storage.get("dir") if isinstance(storage, dict) else None
        if isinstance(raw, str) and raw.strip():
            raw = raw.strip()
            if raw.startswith("~/"):
                return home / raw[2:]
            if raw.startswith("/"):
                return Path(raw)
    return home / ".zcode"


def _zcode_user_agents_dir() -> Path:
    """ZCode 用户级子智能体目录（storageRoot/agents，默认 ``~/.zcode/agents``）。

    桌面端 bootstrap 以 ``storage.dir/agents`` 为 user 作用域扫描根，
    Settings → Subagents 直接展示；``~/.zcode/cli/agents`` 只是子智能体
    运行输出目录（sess_* 工作区），不是注册面。
    """
    return _zcode_storage_dir_override() / "agents"


def _zcode_user_skills_dir() -> Path:
    """ZCode 用户级 Skill 目录（storageRoot/skills，默认 ``~/.zcode/skills``）。

    与 agents 同层（storage.dir 可覆盖）；ZCode 以目录名作 skill 名发现
    （``<dir>/<skill-name>/SKILL.md``）。
    """
    return _zcode_storage_dir_override() / "skills"


def _zcode_usage_skill_path(root: Path, source: Path) -> Path:
    """定位 bugx-zcode 使用手册 Skill 的唯一权威源。

    该 Skill 跟随宿主插件一起分发，源码维护于
    ``plugins/bugx/skills/bugx-zcode/SKILL.md``；``source`` 为引擎载荷根
    （``asset_root()``，即 wheel 内的 ``share/bugx-agent/core``），其同级
    ``plugins/bugx/`` 即同一插件包的打包副本。源码仓库开发态下
    ``root/plugins/bugx/`` 为同一文件，作为兜底。
    """
    candidates = (
        source.parent / "plugins" / "bugx" / "skills" / "bugx-zcode" / "SKILL.md",
        root / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx-zcode" / "SKILL.md",
        root / "plugins" / "bugx" / "skills" / "bugx-zcode" / "SKILL.md",
    )
    for candidate in candidates:
        if not candidate.is_symlink() and candidate.is_file():
            return candidate
    return candidates[0]


def _ensure_zcode_user_skill(root: Path, source: Path) -> dict[str, object]:
    """把 bugx-zcode 使用手册 Skill 写入 ZCode 用户级 skills 目录。

    与 :func:`_ensure_zcode_user_agents` 同分层（全局层产物，跨项目可见）、
    同纪律：符号链接拒绝、内容一致即 unchanged、已有文件先备份、原子写。
    内容源为宿主插件包内的 ``plugins/bugx/skills/bugx-zcode/SKILL.md``
    （wheel 载荷 ``share/bugx-agent/plugins/bugx/skills/bugx-zcode/SKILL.md``），
    随插件版本演进而非本地手工副本——新环境 ``bugx install --agent zcode``
    即获得。
    """
    skill_source = _zcode_usage_skill_path(root, source)
    if skill_source.is_symlink() or not skill_source.is_file():
        # 增强件语义：旧 wheel 载荷没有该资产时跳过部署（不阻塞安装），
        # 升级到携带资产的新 wheel 后自动生效。
        return {
            "dir": str(_zcode_user_skills_dir()),
            "written": [],
            "backups": [],
            "status": "skipped_missing_source",
        }
    skills_dir = _zcode_user_skills_dir()
    if skills_dir.is_symlink():
        raise CliError(_i18n_exc(
            "plugin.zcode_symlink_skills_dir",
            f"拒绝写入符号链接 ZCode 用户级 skills 目录: {skills_dir}",
            skills_dir=skills_dir,
        ))
    target = skills_dir / "bugx-zcode" / "SKILL.md"
    if target.is_symlink():
        raise CliError(_i18n_exc(
            "plugin.zcode_symlink_skill_refused",
            f"拒绝覆盖符号链接 ZCode 用户级 Skill: {target}",
            target=target,
        ))
    payload = skill_source.read_bytes()
    if target.is_file() and target.read_bytes() == payload:
        return {
            "dir": str(skills_dir),
            "written": [{
                "path": str(target), "status": "unchanged",
                "backup": None, "created": False,
            }],
            "backups": [],
        }
    backup = _backup_host_resource(root, "zcode", target) if target.exists() else None
    backups = [backup] if backup is not None else []
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_global_file(target, payload)
    return {
        "dir": str(skills_dir),
        "written": [{
            "path": str(target),
            "status": "created" if backup is None else "upgraded",
            "backup": str(backup) if backup is not None else None,
            "created": backup is None,
        }],
        "backups": backups,
    }


def _zcode_user_agent_boundary(*, locale: str | None = None) -> str:
    """用户级子智能体入口共用的跨项目边界段。"""
    loc = _resolve_locale_name(locale)
    boundary = _i18n_prompt(
        "plugin.zcode_user_agent_boundary",
        "## 用户级子智能体边界（用户级形态专用）\n\n"
        "本入口安装为 ZCode 用户级子智能体（`~/.zcode/agents/`，"
        "Settings → Subagents 全项目可见），可跨项目使用。"
        "操作项目前先运行只读命令 `bugx engine --root . status --json`：仅当返回 `UNINITIALIZED` 时，"
        "才运行 `bugx engine --root . pre` 安全准备新项目。返回 `FATAL_BLOCKED`、其他阻断状态或检查失败时，"
        "保留数据并停止，严禁用 `bugx init`、`bugx reset` 或 `--force` 绕过。"
        "Worker 子智能体遇到布局缺失时只向派发方报告，不得自行运行主控初始化命令；本入口不能替代全局引擎载荷。\n",
        locale=loc,
    )
    return "\n\n" + boundary.rstrip() + "\n"


def _zcode_user_agent_content_main(source: Path, *, locale: str | None = None) -> str:
    """用户级主审计子智能体入口：项目级渲染 + 用户级边界段。"""
    loc = _resolve_locale_name(locale)
    entry = get_adapter("zcode").render(source, locale=loc)
    return entry.rstrip("\n") + _zcode_user_agent_boundary(locale=loc)


_JUDGE_AGENT_CONTENT = '---\nname: bugx-judge\ndescription: BugX 判读员子智能体。输入是宿主确定性取证的判读包（源码切片+边界电池实测输出），唯一职责是判断数据中是否存在真实缺陷并输出裁决 JSON；不执行命令、不读文件、不碰 Supervisor 协议。\ntools: Read, Write\ncolor: green\n---\n<!-- bugx-locale: zh-CN -->\n\n# BugX Judge — 判读员子智能体\n\n你是 BugX 的判读员（judge）。派发方（BugX 主智能体/宿主）会并发派发多个\njudge，每个 judge 收到**一个覆盖单元的判读包**，并在**一个推理回合内**\n输出裁决 JSON 后立即结束。\n\n## 角色铁律\n\n- **只判断，不执行**：你的输入是宿主已经跑完的确定性证据（源码切片 +\n  边界电池实测 stdout/stderr）。你不需要也不允许运行任何命令、读取任何\n  文件、调用任何 MCP 协议工具（claim/heartbeat/checkpoint 与你无关）。\n- **一个回合**：读完判读包 → 输出裁决 JSON → 结束。任何"让我先看看…"\n  的展开都违反本角色定义。\n- **证据即数据**：判读包里的 observations 是宿主真跑出来的结果。异常栈\n  是数据不是故障——它正是你要判断的输入。禁止凭空推断判读包之外的行号\n  或输出。\n\n## 裁决标准：优先级阶梯（命中即止，禁止跳级展开）\n\n按顺序检查，第一级命中就直接定案，**不要**继续展开论证：\n\n- **L1 结构化行定案**：`differential-sentinel` 探针输出 `SENTINEL=True` ⇒ `bug`（注入实证，无需其他论证）；`SENTINEL=False` 且 BASE/PAYLOAD 行为一致 ⇒ 该攻击面 `clean`。\n- **L2 契约对照定案**：全部探针 `exit_code=0` 且返回值符合 docstring/参数名契约 ⇒ `clean`。\n- **L3 异常归因**：崩溃/数据损坏/语义违背 docstring 或参数名契约/并发丢失更新 ⇒ `bug`；异常可由探针伪影解释 ⇒ `clean`。\n- **L4** 以上都不成立 ⇒ `insufficient_data`，并在 notes 指明缺哪条证据。\n\n**探针伪影**（不是缺陷证据，见到直接归 clean）：解绑调用 Class 方法的\nAttributeError；`symbol-missing`（模块级伪符号）；无参函数被边界值等价\n重复调用产生的一串相同结果。\n\n**自证文本规则**：PoC 源码里硬编码的 `VULNERABILITY CONFIRMED` 类字样\n不算独立证据——必须引用实测输出本身（BASE_RET/PAYLOAD_RET/SENTINEL 或\n异常栈），不得引用程序自己打印的结论句。\n\n**确定性要求**：唯一依据是判读包；同一判读包重复判读必须得到同一裁决。\n风格问题、性能品味、"可能有问题"的猜测一律不判。\n\n**定向探针（仅证据不足时，至多一跳）**：若判据不足以定论\n（`verdict=insufficient_data`），且缺的是可机器执行的验证，可附\n`probe_requests`（≤3 条）：`{"desc":"...","code":"..."}`，code 为不含\nshell 字符（`; && || \\` $( > <`）的 python 代码。宿主会确定性执行并把\n结果作为增广证据回给你复审一次。判读包带 `probe_round` 标记时禁止再提，\n必须给终态裁决。\n\n**规则反哺（可选，有把握才填）**：顶层可加 `rule_feedback` 对象反哺引擎\n初筛规则——`risk_tokens`（新风险特征，更严方向，即时生效）、`safe_calls`\n（可安全直关的调用名）、`artifact_markers`（新探针伪影标记）。后两类属\n更松方向，需独立出现 ≥3 次才生效。各 ≤5 项，字符串 4–64 字符。\n\n## 输出格式（唯一输出，无其他文字）\n\n```json\n{"unit_id":"<原样返回判读包的 unit_id>","verdict":"clean|bug|insufficient_data",\n "findings":[{"fingerprint":"<file>::<symbol>::<短slug>","title":"...",\n "severity":"low|medium|high","root_cause":"...（引用 observations 中的具体证据）",\n "evidence":[{"path":"<file>","line":<源码切片中的真实行号>}],\n "reproduction":"python3 -c \'...\'（可直接执行，可改写自 observations 中已验证的探针）"}],\n "notes":"<=200字"}\n```\n\n- `verdict=clean` 时 `findings` 为空数组。\n- `reproduction` 必须是一条可直接执行的 python3 命令，宿主会真跑它生成\n  收据；把说明文字拼进命令会导致整条 finding 被丢弃。\n'


def _zcode_user_agent_content_judge(*, locale: str | None = None) -> str:
    """用户级判读员子智能体（DOC-250）：判读包进、裁决 JSON 出。"""
    return _JUDGE_AGENT_CONTENT


def _zcode_user_agent_content_worker(*, locale: str | None = None) -> str:
    """用户级并发 worker 子智能体入口：项目级渲染 + 用户级边界段。"""
    loc = _resolve_locale_name(locale)
    entry = render_zcode_worker_entry(locale=loc)
    return entry.rstrip("\n") + _zcode_user_agent_boundary(locale=loc)


def _sweep_legacy_zcode_project_entries(
    root: Path, main_entry_rendered: str,
) -> tuple[list[str], list[Path]]:
    """安装期清扫旧版本写入的项目级 ZCode 子智能体副本。

    ZCode 桌面端 Settings → Subagents 以名字合并 user/workspace 作用域且
    项目级同名副本胜出（宿主 list() 先 user 后 workspace 依次覆盖），
    项目级 `.zcode/agents/bugx*.md` 会把同名的用户级条目遮蔽出"用户级"
    列表（0.7.22 实机验证）。0.7.18 及之前版本写过项目级主/worker 入口；
    仅当文件内容与当前模板渲染逐字一致（locale 标记归一后）才认定为
    BugX 自有并删除（先备份），有出入一律保留交由人工处理。
    """
    swept: list[str] = []
    backups: list[Path] = []
    zcode_adapter = get_adapter("zcode")
    candidates: list[tuple[str, str]] = [
        (zcode_adapter.install_path, main_entry_rendered),
        (ZCODE_WORKER_INSTALL_PATH, render_zcode_worker_entry(locale="zh-CN")),
        (ZCODE_WORKER_INSTALL_PATH, render_zcode_worker_entry(locale="en-US")),
    ]
    engine_desc = global_engine_root() / "bugx.md"
    if not engine_desc.is_file():
        engine_desc = asset_root() / "bugx.md"
    if engine_desc.is_file():
        for loc in ("zh-CN", "en-US"):
            try:
                candidates.append((zcode_adapter.install_path, zcode_adapter.render(engine_desc, locale=loc)))
            except Exception:
                pass
    for relative, rendered in candidates:
        target = root / relative
        if target.is_symlink() or not target.is_file():
            continue
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if strip_locale_marker(current) != strip_locale_marker(rendered):
            continue
        backup = _backup_host_resource(root, "zcode", target)
        if backup is not None:
            backups.append(backup)
        target.unlink()
        swept.append(relative)
    if swept:
        _remove_empty_parents(root, swept)
    return swept, backups


def _ensure_zcode_user_agents(
    root: Path, source: Path, *, locale: str | None = None,
) -> dict[str, object]:
    """把主/worker 子智能体写入 ZCode 用户级 agents 目录。

    与用户级 MCP 注册同一分层：这是全局层产物，使 BugX 在任何项目打开
    ZCode 时 Settings → Subagents 均可见。项目作用域不再写同名副本：
    宿主按名字合并各作用域时项目级胜出，双写会把用户级条目遮蔽出
    "用户级"列表（0.7.22 起主/worker 均收敛为仅用户级 + 插件镜像）。
    返回与 `_write_host_mcp_config` 一致的逐文件状态，供安装事务回滚
    与输出。
    """
    agents_dir = _zcode_user_agents_dir()
    if agents_dir.is_symlink():
        raise CliError(_i18n_exc("plugin.zcode_symlink_agents_dir", f"拒绝写入符号链接 ZCode 用户级 agents 目录: {agents_dir}", agents_dir=agents_dir))
    generated = {
        "bugx.md": _zcode_user_agent_content_main(source, locale=locale),
        "bugx-worker.md": _zcode_user_agent_content_worker(locale=locale),
        "bugx-judge.md": _zcode_user_agent_content_judge(locale=locale),
    }
    written: list[dict[str, object]] = []
    backups: list[Path] = []
    for filename, content in generated.items():
        target = agents_dir / filename
        payload = content.encode("utf-8")
        if target.is_symlink():
            raise CliError(_i18n_exc("plugin.zcode_symlink_agent_refused", f"拒绝覆盖符号链接 ZCode 用户级子智能体: {target}", target=target))
        if target.is_file() and target.read_bytes() == payload:
            written.append({
                "path": str(target), "status": "unchanged",
                "backup": None, "created": False,
            })
            continue
        backup = _backup_host_resource(root, "zcode", target) if target.exists() else None
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
        "dir": str(agents_dir),
        "written": written,
        "backups": backups,
    }


def _ensure_plugin_gitignored(root: Path, plugin_root: Path) -> None:
    """把安装器自产插件包自声明进项目 .gitignore（幂等、宽容失败）。

    插件包（``plugins/bugx/``）是引擎可再生工件而非产品材料；不声明会让
    覆盖发现把它当审计单元（实测首轮审计的 self-pollution）。幂等：已有
    精确匹配行即跳过；.gitignore 不可写时静默放弃——发现层对该路径另有
    指纹排除兜底，不因忽略声明失败而阻断安装。
    """
    entry = plugin_root.relative_to(root).as_posix().rstrip("/") + "/"
    gitignore = root / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
        lines = {line.strip() for line in existing.splitlines()}
        if entry in lines:
            return
        block = existing
        if block and not block.endswith("\n"):
            block += "\n"
        block += (
            "\n# BugX engine-generated plugin package (auto-declared by bugx install)\n"
            f"{entry}\n"
        )
        gitignore.write_text(block, encoding="utf-8")
    except OSError:
        return


def _ensure_zcode_plugin(
    root: Path, source: Path, *, locale: str | None = None,
) -> dict[str, object]:
    """生成 ZCode 本地插件包（清单 + 主 agent + worker 子智能体 + 使用手册），不登记全局状态。

    ZCode 通过 Settings → Plugin Management 以本地目录/Git 源添加插件；
    BugX 只准备标准插件目录，不写用户级插件登记文件。使用手册 Skill
    随插件包同址生成（``skills/bugx-zcode/``），与全局插件实体保持同构。
    """
    root = root.resolve()
    plugin_root = root / "plugins" / CHATGPT_PLUGIN_NAME
    manifest_path = plugin_root / ".zcode-plugin" / "plugin.json"
    agent_path = plugin_root / "agents" / "bugx.md"
    worker_agent_path = plugin_root / "agents" / "bugx-worker.md"
    usage_skill_path = plugin_root / "skills" / "bugx-zcode" / "SKILL.md"
    for path in (plugin_root, root / "plugins"):
        if path.is_symlink():
            raise CliError(_i18n_exc("plugin.zcode_symlink_plugin_path", f"拒绝写入符号链接 ZCode Plugin 路径: {path}", path=path))

    plugin_version = _resolve_plugin_version(root, __version__)
    generated = {
        manifest_path: json.dumps(
            _zcode_plugin_manifest(version=plugin_version), ensure_ascii=False, indent=2,
        ) + "\n",
        agent_path: _zcode_plugin_agent_content(source, locale=locale),
        worker_agent_path: _zcode_plugin_worker_content(locale=locale),
    }
    usage_skill = _zcode_usage_skill_path(root, source)
    if usage_skill.is_file() and usage_skill != usage_skill_path:
        generated[usage_skill_path] = usage_skill.read_text(encoding="utf-8")
    copied = skipped = 0
    backups: list[Path] = []
    for target, content in generated.items():
        copied_count, skipped_count, backup = _write_host_resource(
            root, target, content.encode("utf-8"), "zcode",
        )
        copied += copied_count
        skipped += skipped_count
        if backup is not None:
            backups.append(backup)
    _ensure_plugin_gitignored(root, plugin_root)

    def portable_relative(path: Path) -> str:
        """Serialize project-relative plugin paths with stable separators."""
        return path.relative_to(root).as_posix()

    return {
        "status": "prepared",
        "plugin": CHATGPT_PLUGIN_NAME,
        "plugin_path": portable_relative(plugin_root),
        "manifest_path": portable_relative(manifest_path),
        "agent_path": portable_relative(agent_path),
        "worker_agent_path": portable_relative(worker_agent_path),
        "usage_skill_path": (
            portable_relative(usage_skill_path)
            if usage_skill_path.is_file() else None
        ),
        "copied": copied,
        "skipped": skipped,
        "backups": backups,
        "next_step": _i18n_prompt(
            "plugin.zcode_next_step",
            "在 ZCode 的 Settings → Plugin Management 中以本地目录添加 "
            f"{plugin_root} 并启用；随后新会话可通过 bugx:bugx 审计、"
            "bugx:bugx-worker 并发派发",
            plugin_root=plugin_root,
        ),
    }


ZCODE_GLOBAL_PLUGIN_DIR_NAME = "zcode-plugin"


def _zcode_global_plugin_root() -> Path:
    """Return the one BugX-owned ZCode plugin source for this machine.

    ``plugins.dirs`` registration is user-global while the BugX plugin used
    to be generated per project (and in development repositories pointed at
    the source tree).  A single user-owned copy under ~/.bugx keeps every
    project's ZCode session on one identical plugin, mirroring the ChatGPT
    global plugin model.
    """
    override = os.environ.get("BUGX_ZCODE_PLUGIN_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bugx" / ZCODE_GLOBAL_PLUGIN_DIR_NAME


def _write_zcode_global_plugin(
    root: Path, source: Path, *, locale: str | None = None,
) -> Path:
    """Publish the canonical global ZCode plugin atomically under ~/.bugx."""
    # 延迟导入避免 plugin.py <-> plugin_zcode.py 顶层循环导入。
    from .plugin import _chatgpt_skill_content

    base = _zcode_global_plugin_root()
    plugin_dir = base / "plugins" / CHATGPT_PLUGIN_NAME
    # macOS exposes /var as a system symlink; validate its physical target
    # while keeping the user-facing path stable for ZCode configuration.
    security_base = (
        base.resolve(strict=False)
        if str(base).startswith("/var/") and Path("/var").is_symlink()
        else base
    )
    _assert_no_symlink_components(security_base)
    generated = {
        plugin_dir / ".zcode-plugin" / "plugin.json": json.dumps(
            _zcode_plugin_manifest(), ensure_ascii=False, indent=2,
        ) + "\n",
        plugin_dir / "agents" / "bugx.md": _zcode_plugin_agent_content(
            source, locale=locale,
        ),
        plugin_dir / "agents" / "bugx-worker.md": _zcode_plugin_worker_content(
            locale=locale,
        ),
        plugin_dir / "agents" / "bugx-judge.md": _JUDGE_AGENT_CONTENT,
        # ZCode 的插件加载器按目录约定扫描 skills/ 子目录；全局实体保持
        # 同结构，宿主侧行为与项目内插件一致。
        plugin_dir / "skills" / CHATGPT_PLUGIN_NAME / "SKILL.md":
            _chatgpt_skill_content(source, locale=locale),
    }
    # ZCode 专属使用手册与插件同址分发：宿主按 skills/ 目录约定发现，
    # 因此该 Skill 随全局插件实体一起生成，而不是散落在别处。
    usage_skill = _zcode_usage_skill_path(root, source)
    if usage_skill.is_file():
        generated[plugin_dir / "skills" / "bugx-zcode" / "SKILL.md"] = (
            usage_skill.read_text(encoding="utf-8")
        )
    for target, content in generated.items():
        if target.is_symlink():
            raise CliError(_i18n_exc(
                "plugin.zcode_global_symlink_refused",
                f"拒绝覆盖符号链接的全局 ZCode 插件文件: {target}", target=target,
            ))
        _atomic_write_global_file(target, content.encode("utf-8"))

    # 同步插件图标本地静态资源，离线时备用
    avatar_src = root / "plugins" / CHATGPT_PLUGIN_NAME / "assets" / "bugx-avatar-512.png"
    if not avatar_src.is_file():
        avatar_src = root / "core" / "assets" / "bugx-avatar-512.png"
    if not avatar_src.is_file():
        avatar_src = asset_root() / "bugx-avatar-512.png"
    if avatar_src.is_file():
        avatar_target = plugin_dir / "assets" / "bugx-avatar-512.png"
        if avatar_target.is_symlink():
            raise CliError(_i18n_exc(
                "plugin.zcode_global_symlink_refused",
                f"拒绝覆盖符号链接的全局 ZCode 插件文件: {avatar_target}", target=avatar_target,
            ))
        _atomic_write_global_file(avatar_target, avatar_src.read_bytes())
    return plugin_dir


def _is_legacy_zcode_plugin_dir(candidate: Path) -> bool:
    """Check whether *candidate* is a BugX plugin dir registered per project."""
    manifest = candidate / ".zcode-plugin" / "plugin.json"
    try:
        if manifest.is_symlink() or not manifest.is_file():
            return False
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        return isinstance(payload, dict) and payload.get("name") == CHATGPT_PLUGIN_NAME
    except (OSError, json.JSONDecodeError):
        return False


def _upsert_zcode_plugin_dirs(
    *, register: Path | None, remove: bool,
) -> dict[str, object]:
    """Rewrite plugins.dirs against the global plugin dir.

    Legacy per-project entries (including ones pointing into a BugX source
    repository) are recognized by their manifest and dropped, so a single
    user-owned registration remains.
    """
    config_path = _zcode_config_path()
    cfg: dict[str, object] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CliError(_i18n_exc(
                "plugin.zcode_config_unreadable",
                f"ZCode 配置无法读取，拒绝写入: {config_path}: {exc}",
                config_path=config_path, exc=exc,
            )) from exc
        if not isinstance(loaded, dict):
            raise CliError(_i18n_exc(
                "plugin.zcode_config_not_json_object",
                f"ZCode 配置不是 JSON 对象，拒绝写入: {config_path}",
                config_path=config_path,
            ))
        cfg = loaded
    plugins_cfg = cfg.get("plugins")
    if plugins_cfg is not None and not isinstance(plugins_cfg, dict):
        raise CliError(_i18n_exc(
            "plugin.zcode_config_plugins_not_object",
            f"ZCode 配置 plugins 键不是对象，拒绝写入: {config_path}",
            config_path=config_path,
        ))
    plugins_cfg = dict(plugins_cfg or {})
    dirs_raw = plugins_cfg.get("dirs")
    if dirs_raw is not None and not isinstance(dirs_raw, list):
        raise CliError(_i18n_exc(
            "plugin.zcode_config_dirs_not_array",
            f"ZCode 配置 plugins.dirs 不是数组，拒绝写入: {config_path}",
            config_path=config_path,
        ))
    dirs = [str(item) for item in dirs_raw or []]
    global_dir = _zcode_global_plugin_root() / "plugins" / CHATGPT_PLUGIN_NAME
    global_key = str(global_dir.resolve(strict=False))
    register_key = (
        str(Path(register).resolve(strict=False))
        if register is not None else global_key
    )
    kept: list[str] = []
    migrated: list[str] = []
    for item in dirs:
        key = str(Path(item).resolve(strict=False))
        if key == global_key or key == register_key:
            continue
        if _is_legacy_zcode_plugin_dir(Path(item)):
            migrated.append(item)
            continue
        kept.append(item)
    action = "not-registered"
    if not remove and register_key not in [str(Path(k).resolve(strict=False)) for k in kept]:
        kept.append(register_key)
        action = "registered" if not migrated else "registered+migrated"
    elif remove:
        action = "removed" if migrated or register_key in (
            str(Path(k).resolve(strict=False)) for k in dirs
        ) else "not-registered"
    plugins_cfg["dirs"] = kept
    cfg["plugins"] = plugins_cfg
    if config_path.parent.exists() is False:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_global_file(config_path, json.dumps(
        cfg, ensure_ascii=False, indent=2,
    ).encode("utf-8") + b"\n")
    return {
        "action": action, "config": str(config_path),
        "plugin_dir": register_key, "dirs": kept, "migrated": migrated,
    }


def _zcode_config_path() -> Path:
    """ZCode 用户级配置文件（plugins.dirs 所在层；测试经 HOME 隔离）。"""
    return Path.home() / ".zcode" / "cli" / "config.json"


def _zcode_workspace_mcp_config_path(root: Path) -> Path:
    """ZCode 工作区级 MCP 配置；项目根是唯一允许的 BugX 绑定边界。"""
    return root.resolve() / ".zcode" / "mcp.json"


def command_plugin_zcode(args: argparse.Namespace) -> int:
    """Publish the user-global ZCode plugin and register it in plugins.dirs.

    ZCode loads plugins from user-level ``plugins.dirs`` entries; the BugX
    plugin entity is published once under ``~/.bugx/zcode-plugin`` (never a
    project or source-repository path), and legacy per-project registrations
    are migrated away in the same rewrite.
    """
    root = args.root.resolve()
    project_manifest = root / "plugins" / CHATGPT_PLUGIN_NAME / ".zcode-plugin" / "plugin.json"
    source = asset_root()
    if not project_manifest.is_file() and not args.remove:
        raise CliError(_i18n_exc(
            "plugin.missing_zcode_manifest",
            f"缺少 ZCode 插件清单: {project_manifest}；请先运行 bugx install --root {root} --agent zcode --non-interactive",
            manifest=project_manifest,
            root=root,
        ))
    if not args.remove:
        target_locale = getattr(args, "locale", None) or _resolve_locale_name()
        _write_zcode_global_plugin(root, source, locale=target_locale)
    result = _upsert_zcode_plugin_dirs(register=None, remove=bool(args.remove))
    action = str(result["action"])
    migrated = list(result.get("migrated") or [])
    next_step = (
        _i18n_prompt("plugin.zcode_next_step_removed", "已从 ZCode plugins.dirs 移除；已加载会话需重启后失效")
        if args.remove else
        _i18n_prompt("plugin.zcode_next_step_registered", "重启 ZCode 或新开会话后，Subagents/Agent 列表出现 bugx:bugx")
    )
    if migrated and not args.remove:
        next_step = (
            "已把旧的项目内 ZCode 插件注册（"
            + "、".join(migrated[:3])
            + "）迁移为用户目录全局插件；重启 ZCode 或新开会话后生效"
        )
    payload = {
        "action": action,
        "config": result["config"],
        "plugin_dir": result["plugin_dir"],
        "dirs": result["dirs"],
        "migrated": migrated,
        "next_step": next_step,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[bugx] ZCode 插件目录已更新（用户级全局实体）: {result['plugin_dir']}")
        if migrated:
            print(f"[bugx] 已迁移旧项目内注册: {'、'.join(migrated)}")
        print(f"[bugx] 配置: {result['config']} → plugins.dirs = {result['dirs']}")
        print(f"[bugx] {payload['next_step']}")
    return 0
