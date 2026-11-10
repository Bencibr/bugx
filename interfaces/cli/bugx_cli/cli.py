#!/usr/bin/env python3
"""BugX 统一命令行入口（模块化调度器）。"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import typing
from pathlib import Path

# 兼容低版本启动环境：若当前解释器 < 3.11，优先尝试全系统多源自举重拉
if sys.version_info < (3, 11):
    try:
        _core = Path(__file__).resolve().parent.parent.parent / "core"
        for _entry in (_core.parent, _core):
            if _core.is_dir() and str(_entry) not in sys.path:
                sys.path.insert(0, str(_entry))
        from core.infra import python_runtime
        python_runtime.transparent_reexec_if_needed(min_version=(3, 11))
    except Exception:
        pass

from . import __version__
from .infra.engine_installer import MissingAgentAssetError

# 导出基础公共工具与命令模块（保持 100% 向后兼容性）
from . import common
from .hosts.registry import (
    autonomous_host_names,
    engine_host_concurrency_bounds,
    engine_host_label,
)
from .commands import (
    plugin, lifecycle, agent, doctor, knowledge, cloud, system, stats, project,
    patch, skill, daemon, autonomous, goal, advisory, report, audit, interaction,
)


class _CloudSyncExplicitAction(argparse.Action):
    """Record that init's cloud-sync decision came from the command line."""

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, "cloud_sync_explicit", True)

# 将各领域子模块的符号（含公共工具与测试挂载点）全量导出到顶层 cli 命名空间
for _mod in (
    common, plugin, lifecycle, agent, doctor, knowledge, cloud, system, stats,
    project, patch, skill, daemon, autonomous, goal, advisory, report,
    audit, interaction,
):
    for _k, _v in vars(_mod).items():
        if not _k.startswith("__"):
            globals()[_k] = _v


_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "init": "初始化项目 BugX 核心环境与运行态",
    "doctor": "环境健康度诊断、依赖检测与一键自愈",
    "hosts": "发现系统中已安装的可接入 AI 宿主 Agent",
    "install": "安装 BugX MCP / Skill 至已发现的 AI 宿主",
    "uninstall": "从指定 AI 宿主卸载 BugX 集成",
    "audit": "项目级漏洞渗透挖掘与代码安全审计",
    "interaction": "查看/回答 BugX 抛出的结构化交互问题（非交互宿主答案回灌）",
    "autonomous": "启动全自动无人值守 BugX 漏洞挖掘",
    "analyze": "项目画像深度分析与代码覆盖度全景扫描",
    "project": "项目元数据管理、画像展示与云端同步",
    "patch": "补丁合成、验证、对抗与自动应用",
    "skill": "BugX 动态扩展 Skill 市场管理与挂载",
    "cloud": "BugX 云端多设备协同、同步与设备认证",
    "auth": "云端登录、凭据管理与设备配对",
    "stats": "代码漏洞挖掘统计指标与 ROI 分析",
    "report": "本地 Web 控制台与审计报告预览",
    "engine": "全局引擎核心管理与状态查询",
    "daemon": "BugX 后台常驻守护进程管理",
    "reset": "重置项目运行态或全局错题集缓存",
    "help": "查看 BugX 命令使用说明",
}

_INTENT_SYNONYMS: dict[str, str] = {
    "体检": "doctor",
    "健康": "doctor",
    "修复": "doctor",
    "自愈": "doctor",
    "画像": "project",
    "项目": "project",
    "分析": "analyze",
    "审计": "audit",
    "漏洞": "audit",
    "扫描": "audit",
    "挖掘": "autonomous",
    "自主": "autonomous",
    "无人值守": "autonomous",
    "补丁": "patch",
    "合成": "patch",
    "登录": "auth",
    "认证": "auth",
    "同步": "cloud",
    "云端": "cloud",
    "技能": "skill",
    "插件": "plugin",
    "宿主": "hosts",
    "安装": "install",
    "卸载": "uninstall",
    "重置": "reset",
    "清理": "reset",
    "状态": "stats",
    "统计": "stats",
    "守护": "daemon",
}


class _BugXArgumentParser(argparse.ArgumentParser):
    """带 AI 意图理解与智能模糊纠错的增强参数解析器。"""

    def error(self, message: str) -> typing.NoReturn:
        match = re.search(r"argument command: invalid choice: '([^']+)'", message)
        if match:
            bad_cmd = match.group(1).strip()
            from .infra.ai_advisor import CommandIntentArbitrator
            arbitrator = CommandIntentArbitrator()
            res = arbitrator.infer_intent(bad_cmd)

            err_output = [
                f"[bugx] ✗ 未知命令 '{bad_cmd}'",
            ]
            if res.suggestions:
                err_output.append("  🤖 AI 意图理解：您是否想要执行以下命令？")
                for cmd_name, cmd_desc in res.suggestions:
                    desc_str = f" - {cmd_desc}" if cmd_desc else ""
                    err_output.append(f"     👉 bugx {cmd_name}{desc_str}")
                if res.reasoning:
                    err_output.append(f"  💡 意图推断依据: {res.reasoning}")
            else:
                err_output.append("  查看所有可用命令: bugx --help")
            print("\n".join(err_output), file=sys.stderr)
            sys.exit(2)

        match_arg = re.search(r"unrecognized arguments:\s*(.+)", message)
        if match_arg:
            bad_args = match_arg.group(1).strip()
            err_output = [f"[bugx] ✗ 参数错误: 未知参数 {bad_args}"]
            if "--mdoe" in bad_args:
                err_output.append("  💡 AI 纠错建议: 您是否想要输入 '--mode'？")
            elif "--cloude" in bad_args:
                err_output.append("  💡 AI 纠错建议: 您是否想要输入 '--cloud'？")
            print("\n".join(err_output), file=sys.stderr)
            sys.exit(2)

        super().error(message)


def _generate_ai_exception_advice(exc: Exception, command: str | None = None) -> str | None:
    """根据异常类型和上下文，提供 AI 根因诊断与解决建议。"""
    text = str(exc)
    normalized = text.casefold()

    if "fatal_blocked" in normalized or "project_layout=" in normalized:
        return (
            "BugX 启动门禁已阻断；保留项目现场并停止。先阅读 "
            "`bugx engine --root <ROOT> status --json` 的阻断详情，禁止用 init、reset 或 --force 绕过。"
        )
    if (
        "尚未初始化" in text
        or "not initialized" in text.lower()
        or "manifest.json" in text
        or "缺少项目身份" in text
        or ".bugx-project.json" in text
    ):
        return (
            "先运行只读命令 `bugx engine --root <ROOT> status --json`；仅当状态明确为 "
            "`UNINITIALIZED` 时再运行 `bugx engine --root <ROOT> pre`。其他状态保留项目数据并停止。"
        )
    if "已存在" in text or "already exists" in text.lower() or "拒绝覆盖" in text:
        return "目标资产已存在；如需强制覆盖更新，请追加 --force 参数"
    if "未登录" in text or "unauthorized" in text.lower() or "凭据" in text or "token" in text.lower():
        return "当前操作需要云端鉴权，请运行: bugx auth login 登录，或在交互终端执行 bugx project sync"
    if "Connection refused" in text or "网络连接失败" in text or "timed out" in text.lower():
        return "网络通信异常或云端端点超时，请检查系统网络代理，或检查 BUGX_CLOUD_API_BASE 配置"
    if "Permission denied" in text or "权限不足" in text or isinstance(exc, PermissionError):
        return "文件系统读写权限受限，请检查当前用户对相关文件/目录的操作权限"
    if "未找到" in text or "not found" in text.lower() or "missing" in text.lower():
        return "缺少关键依赖或受管配置；先运行只读命令 `bugx doctor --root <ROOT> --json` 查看具体失败项"
    if isinstance(exc, json.JSONDecodeError):
        return (
            "项目 JSON 状态损坏时不会由通用自愈器改写；保留原文件，运行 "
            "`bugx engine --root <ROOT> status --json` 和 `bugx doctor --root <ROOT> --json` 只读诊断，"
            "再从可信备份恢复。"
        )
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = _BugXArgumentParser(prog="bugx", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {common.get_full_version()}")
    parser.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=None, help="诊断日志等级（默认 INFO，可由 BUGX_LOG_LEVEL 覆盖）",
    )
    parser.add_argument(
        "--accept-data-sharing", action="store_true",
        help="明确同意脱敏后的单向攻防数据反哺（CI/headless 默认关闭）",
    )
    parser.add_argument(
        "--privacy", choices=("standard", "strict"), default="standard",
        help="strict 仅允许匿名度量出网",
    )
    parser.add_argument(
        "--no-backup", "--skip-backup", action="store_true", dest="no_backup",
        help="跳过执行前自动创建的外部备份归档",
    )
    subparsers = parser.add_subparsers(dest="command", parser_class=_BugXArgumentParser)

    help_parser = subparsers.add_parser(
        "help", help="显示 BugX 或指定命令的使用说明",
    )
    help_parser.add_argument(
        "help_command", nargs="*", metavar="COMMAND",
        help="可选的命令路径，例如：bugx help cloud sync",
    )

    completion = subparsers.add_parser(
        "completion", help="输出 Bash、Zsh 或 Fish 的 Tab 补全脚本",
    )
    completion.add_argument("shell", choices=("bash", "zsh", "fish"))
    completion.set_defaults(handler=command_completion)

    init = subparsers.add_parser("init", help="把宿主无关的 BugX 核心初始化到项目")
    init.add_argument("--root", type=Path, default=Path.cwd())
    init.add_argument("--force", action="store_true", help="更新已有静态文件")
    init.add_argument(
        "--commit-policy", choices=("life", "round", "manual", "none"),
        help="项目默认提交策略：每个 Life、每个 Round、手动提交（manual）或不提交",
    )
    init.add_argument(
        "--cloud-sync", choices=("local", "sync"), default=argparse.SUPPRESS,
        action=_CloudSyncExplicitAction,
        help="项目云端多设备同步策略：local=保持纯本地运行，sync=开启云端同步（非交互模式必须显式指定）",
    )
    init.set_defaults(cloud_sync_explicit=False)
    init.add_argument("--accept-data-sharing", action="store_true", default=argparse.SUPPRESS)
    init.add_argument("--privacy", choices=("standard", "strict"), default=argparse.SUPPRESS)
    init.set_defaults(handler=command_init)

    dsh = subparsers.add_parser(
        "dsh", help="兼容启动器：为首个 Web Session 绑定项目并启动 DeepSeek Harness",
    )
    dsh.add_argument("--root", type=Path, default=None, help="项目根目录；默认从当前目录向上自动发现")
    dsh.add_argument("--profile", default="web", help="DSH profile（默认 web）")
    dsh.add_argument("dsh_args", nargs=argparse.REMAINDER, help="传给 DSH profile 的参数")
    dsh.set_defaults(handler=command_dsh)

    hosts = subparsers.add_parser("hosts", help="只读发现可接入的 AI 宿主")
    hosts.add_argument("--root", type=Path, default=Path.cwd())
    hosts.add_argument("--json", action="store_true")
    hosts.set_defaults(agent_command="discover", handler=command_agent)

    install = subparsers.add_parser(
        "install", help="发现并安装到已安装的 AI Agent",
    )
    install.set_defaults(agent_command="install", handler=command_agent)
    install.add_argument("--root", type=Path, default=Path.cwd())
    install.add_argument("--agent", action="append", default=[], dest="agent")
    install.add_argument("--force", action="store_true")
    install.add_argument("--allow-missing", action="store_true")
    install.add_argument("--non-interactive", action="store_true")
    install.add_argument("--locale", type=str, default=None, help="显式指定安装语言（如 zh-CN, en-US）")
    install.add_argument("--json", action="store_true")

    reset = subparsers.add_parser(
        "reset", help="归档后重建项目运行态或全局错题集（不删除全局引擎）",
    )
    reset.add_argument("--scope", choices=("project", "global"), default="project",
                       help="重置范围：project=当前项目运行态，global=全局错题集")
    reset.add_argument("--root", type=Path, default=Path.cwd())
    reset.add_argument(
        "--reset-knowledge", action="store_true",
        help="项目范围同时重置项目错题集/经验集；仅需一次确认",
    )
    reset.add_argument(
        "--reset-identity", action="store_true",
        help="全局范围同时重置设备 ID 和安装身份密钥",
    )
    reset.add_argument(
        "--reset-credentials", action="store_true",
        help="全局范围同时删除云端凭证（会先备份）",
    )
    reset.add_argument(
        "--reset-mcp-runtime", action="store_true",
        help="全局范围同时清理 MCP lock-hash 运行时缓存",
    )
    reset.add_argument(
        "--reset-backups", action="store_true",
        help="全局范围同时清理历史备份（当前重置备份仍保留）",
    )
    reset.add_argument("--dry-run", action="store_true", help="只显示范围，不修改文件")
    reset.add_argument("--yes", action="store_true", help="确认本次重置，避免交互提示")
    reset.add_argument("--force", action="store_true", help="强制重置（忽略活跃状态检查；执行前依然创建外部备份）")
    reset.add_argument(
        "--no-backup", "--skip-backup", action="store_true", dest="no_backup",
        help="跳过执行前自动创建的外部备份归档",
    )
    reset.add_argument("--json", action="store_true")
    reset.set_defaults(handler=command_reset)

    uninstall = subparsers.add_parser(
        "uninstall", help="安全卸载 BugX 宿主入口；默认保留报告、证据、配置和错题集",
    )
    uninstall.add_argument("--root", type=Path, default=Path.cwd())
    uninstall.add_argument("--agent", action="append", default=[], dest="agent")
    uninstall.add_argument(
        "--self", action="store_true",
        help="移除全机 BugX 自有工件：~/.bugx（引擎/知识库/云数据）、OpenCode 全局链接、"
             "ZCode plugins.dirs 登记、Pi/dsh 全局注册与 codex 插件登记；不卸载 pip 包",
    )
    uninstall.add_argument(
        "--dry-run", action="store_true",
        help="只预览将清理的目标，不修改任何文件",
    )
    uninstall.add_argument("--all", action="store_true", help="卸载 hosts.json 中全部已安装宿主")
    uninstall.add_argument(
        "--purge-data", action="store_true",
        help="与 --all --yes 同用；外部备份后清理项目 .bugx 和 BugX 共享静态资源",
    )
    uninstall.add_argument(
        "--purge-global-data", action="store_true",
        help="额外清理全局 ~/.bugx/knowledge；会影响其他项目，必须显式确认",
    )
    uninstall.add_argument("--force", action="store_true", help="强制卸载已修改或历史版本的受管宿主入口（卸载前自动备份）")
    uninstall.add_argument("--yes", action="store_true", help="确认不可逆数据清理")
    uninstall.add_argument(
        "--no-backup", "--skip-backup", action="store_true", dest="no_backup",
        help="跳过执行前自动创建的外部备份归档",
    )
    uninstall.add_argument("--json", action="store_true")
    uninstall.set_defaults(handler=command_uninstall)

    doctor = subparsers.add_parser("doctor", help="诊断安装与项目状态")
    doctor.add_argument("--root", type=Path, default=Path.cwd())
    doctor.add_argument(
        "target", nargs="?", choices=("all", "mcp-runtime"), default="all",
        help="诊断目标：默认整体状态；mcp-runtime 只输出离线 runtime 诊断",
    )
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--installation-only", action="store_true",
        help="只验证安装完整性；仍报告但不要求本机已有 OpenCode 模型",
    )
    doctor.add_argument(
        "--repair-assets", "--repair-workbuddy-assets", "--repair", "--fix",
        action="store_true", dest="repair",
        help="--repair-assets：自动修复可自愈项（重建 current.json、对齐版本清单、补齐缺失的受管宿主入口与全局软链等）",
    )
    doctor.add_argument(
        "--agent", action="append", default=[], dest="agent",
        help="定向诊断指定 AI 宿主（可多次指定或逗号分隔，如 --agent claude；默认诊断全部宿主）",
    )
    doctor.add_argument(
        "--strict", action="store_true",
        help="退出码计入可选增强与运行时就绪（默认只以必需安装项判定）",
    )
    doctor.add_argument(
        "--ai", action="store_true",
        help="开启 AI 智能排障与自愈分析（检测到异常时深度剖析根因并输出精准修复处方）",
    )
    doctor.set_defaults(handler=command_doctor)

    mcp = subparsers.add_parser(
        "mcp", help="启动本地 MCP Supervisor（不执行 worker 命令）",
    )
    mcp_subparsers = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_subparsers.add_parser(
        "serve", help="以 stdio 方式启动本地 BugX Supervisor MCP",
    )
    mcp_serve.add_argument(
        "--root", type=Path, default=None,
        help="项目根目录；缺省时依次读 BUGX_PROJECT_ROOT / 宿主工作区环境变量，最后才用 cwd",
    )
    mcp_serve.add_argument(
        "--role", choices=("host", "worker"), default="host",
        help="MCP 通道角色：host 仅主控，worker 仅任务执行",
    )
    mcp_serve.add_argument(
        "--takeover", action="store_true",
        help=(
            "DOC-230 3.1 层 3：仅当 host 持有者闲置超阈值且未自退时接管其租约"
            "（默认关闭）"
        ),
    )
    mcp_serve.set_defaults(handler=command_mcp)
    mcp_prune = mcp_subparsers.add_parser(
        "prune", help="清理被遗弃的同项目 MCP 会话进程（默认 dry-run）",
    )
    mcp_prune.add_argument(
        "--root", type=Path, default=None, help="项目根目录（缺省按宿主环境解析）",
    )
    mcp_prune.add_argument(
        "--older-than", type=float, default=1800.0, dest="older_than",
        help="仅清理存活超过该秒数且**无活动记录**的进程（默认 1800）",
    )
    mcp_prune.add_argument(
        "--yes", action="store_true", help="确认执行（缺省只打印将清理的进程）",
    )
    mcp_prune.set_defaults(handler=command_mcp_prune)
    mcp_schema = mcp_subparsers.add_parser(
        "schema", help="输出 MCP 工具契约（不需要项目目录，不启动 Supervisor）",
    )
    mcp_schema.add_argument(
        "--role", choices=("host", "worker"), default="host",
        help="输出 host 或 worker 通道工具",
    )
    mcp_schema.set_defaults(handler=command_mcp_schema)

    mcp_spy = mcp_subparsers.add_parser(
        "spy", help="以透明中间件管道代理 MCP 子进程并无损 dump 报文流",
    )
    mcp_spy.add_argument(
        "--root", type=Path, default=None,
        help="项目根目录（用于记录 wire 日志到 .bugx/logs/mcp-wire.log）",
    )
    mcp_spy.add_argument(
        "--log", type=Path, default=None,
        help="指定 dump 文件路径（默认 .bugx/logs/mcp-wire.log）",
    )
    mcp_spy.add_argument("spy_cmd", nargs=argparse.REMAINDER, help="被代理的底层 MCP 命令与参数")
    mcp_spy.set_defaults(handler=command_mcp_spy)

    mcp_docs = mcp_subparsers.add_parser(
        "docs", help="索引或启动本地项目文档 MCP（无第三方 API Key）",
    )
    mcp_docs_subparsers = mcp_docs.add_subparsers(
        dest="mcp_docs_command", required=True,
    )
    for command_name, help_text in (
        ("index", "索引 README/docs/core Markdown 到 SQLite FTS5"),
        ("serve", "以 stdio 方式启动只读文档 MCP"),
    ):
        docs_command = mcp_docs_subparsers.add_parser(command_name, help=help_text)
        docs_command.add_argument(
            "--root", type=Path, default=None,
            help="项目根目录；缺省时依次读 BUGX_PROJECT_ROOT / 宿主工作区环境变量，最后才用 cwd",
        )
        docs_command.add_argument(
            "--db", type=Path, default=Path(".bugx/knowledge/docs.sqlite3"),
        )
        docs_command.add_argument("--project-id", default=None)
        docs_command.add_argument("--version", default=None)
        if command_name == "serve":
            docs_command.add_argument("--no-auto-index", action="store_true")
        docs_command.set_defaults(handler=command_mcp_docs)

    capability = subparsers.add_parser(
        "capability",
        help="探查项目形态并装配能力：plan（只读）/ install（装齐所需 MCP 运行时）",
    )
    capability_sub = capability.add_subparsers(
        dest="capability_command", required=True,
    )

    def _add_capability_common(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("--root", type=Path, default=None,
                                help="项目根目录（缺省当前目录）")
        sub_parser.add_argument("--json", action="store_true")

    cap_plan = capability_sub.add_parser(
        "plan", help="只读输出「画像 → 技能包 → MCP/工具」装配计划",
    )
    _add_capability_common(cap_plan)
    cap_plan.set_defaults(handler=command_capability)

    cap_install = capability_sub.add_parser(
        "install",
        help="按计划装齐所需 MCP 运行时（缺凭据的能力自动跳过并提示环境变量）",
    )
    _add_capability_common(cap_install)
    cap_install.add_argument("--runtime-root", type=Path, default=None,
                             help="覆盖 runtime 根目录（默认 ~/.bugx/mcp-runtime）")
    cap_install.add_argument("--lock", type=Path, default=None,
                             help="覆盖 lock 文件（默认使用随 BugX 发布的锁定版本）")
    cap_install.set_defaults(handler=command_capability)

    mcp_runtime = subparsers.add_parser(
        "mcp-runtime",
        help="lock-hash 专用离线 MCP runtime：install/verify/start/rollback",
    )
    mcp_runtime_sub = mcp_runtime.add_subparsers(
        dest="mcp_runtime_command", required=True,
    )

    def _add_runtime_common(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("--root", type=Path, default=Path.cwd())
        sub_parser.add_argument(
            "--runtime-root", type=Path, default=None,
            help="覆盖 runtime 根目录（默认 ~/.bugx/mcp-runtime）",
        )

    rt_install = mcp_runtime_sub.add_parser(
        "install", help="按 mcp-lock.json 安装并原子发布新 lock-hash runtime",
    )
    _add_runtime_common(rt_install)
    rt_install.add_argument("--lock", type=Path, default=None,
                            help="覆盖 lock 文件（默认使用随 BugX 发布的锁定版本）")
    rt_install.add_argument("--json", action="store_true")
    rt_install.set_defaults(handler=command_mcp_runtime)

    rt_verify = mcp_runtime_sub.add_parser(
        "verify", help="深度校验当前 runtime（文件树/入口/manifest/Node）",
    )
    _add_runtime_common(rt_verify)
    rt_verify.add_argument("--json", action="store_true")
    rt_verify.set_defaults(handler=command_mcp_runtime)

    rt_start = mcp_runtime_sub.add_parser(
        "start", help="校验后直接以 node 启动指定 MCP 服务入口（不联网下载）",
    )
    _add_runtime_common(rt_start)
    rt_start.add_argument("--server", required=True,
                          help="MCP 服务名（由 mcp-lock.json servers 定义）")
    rt_start.add_argument(
        "--print-command", action="store_true",
        help="只打印将执行的 argv 与策略（JSON），不真正启动",
    )
    rt_start.set_defaults(handler=command_mcp_runtime)

    rt_rollback = mcp_runtime_sub.add_parser(
        "rollback", help="把 current 切回上一批准 lock-hash；不重新下载",
    )
    _add_runtime_common(rt_rollback)
    rt_rollback.add_argument("--json", action="store_true")
    rt_rollback.set_defaults(handler=command_mcp_runtime)

    lesson = subparsers.add_parser("lesson", help="管理项目级结构化错题集")
    lesson.add_argument(
        "lesson_command",
        choices=("init", "bootstrap", "migrate", "validate", "rebuild", "rebuild-fts", "archive-events",
                 "incident-archive", "retrieve", "record",
                 "review", "feedback", "metrics", "hygiene", "merge", "retire", "revoke",
                 "export", "backup", "replay", "incident-add", "incident-lookup",
                 "incident-activate", "incident-revoke",
                 "tool-feedback", "tool-metrics", "tool-lookup", "tool-matrix",
                 "export-book", "promote", "promote-incident", "promote-tool", "inherit", "evidence-add", "incident-list",
                 "consume", "tool-exp-export", "tool-exp-import"),
    )
    lesson.add_argument("--root", type=Path, default=Path.cwd())
    lesson.add_argument("--ledger", type=Path)
    lesson.add_argument("--input", type=Path)
    lesson.add_argument("--output", type=Path)
    lesson.add_argument("--query", default="")
    lesson.add_argument("--category")
    lesson.add_argument("--limit", type=int, default=6)
    lesson.add_argument("--offset", type=int, default=0)
    lesson.add_argument("--token-budget", type=int, default=1500)
    lesson.add_argument("--byte-budget", type=int, default=12000)
    lesson.add_argument("--exclude-lesson-id", action="append", default=[])
    lesson.add_argument("--finding-fingerprint")
    lesson.add_argument("--tool", action="append", default=[],
                        help="tool-feedback 归因的工具名，可重复")
    lesson.add_argument("--signature", action="append", default=[])
    lesson.add_argument("--pattern-id")
    lesson.add_argument(
        "--finding", type=Path,
        help="hit 反馈对应的已通过确认门禁的 finding JSON",
    )
    lesson.add_argument("--lesson-id")
    lesson.add_argument("--incident-id")
    lesson.add_argument("--evidence-receipt-id")
    lesson.add_argument("--lifecycle-id")
    lesson.add_argument("--round", type=int)
    lesson.add_argument("--outcome")
    lesson.add_argument("--event-id")
    lesson.add_argument("--subject-type")
    lesson.add_argument("--subject-id")
    lesson.add_argument("--event-type")
    lesson.add_argument("--source-pattern-id")
    lesson.add_argument("--target-pattern-id")
    lesson.add_argument("--reason")
    lesson.add_argument("--min-feedback", type=int, default=3)
    lesson.add_argument(
        "--deep", action="store_true",
        help="validate 的深度校验：事件链、关系、receipt、tombstone、FTS 和归档段",
    )
    lesson.add_argument(
        "--from-seed", action="store_true",
        help="rebuild 从全局 JSON 种子重建 SQLite（仅用于显式恢复）",
    )
    lesson.add_argument(
        "--confirm-data-loss", action="store_true",
        help="确认替换损坏或缺失的 SQLite 运行库，并保留 .corrupt.bak",
    )
    lesson.add_argument(
        "--keep-tail", type=int, default=0,
        help="archive-events 保留活动事件尾部数量（默认 0）",
    )
    lesson.add_argument(
        "--older-than-days", type=int, default=90,
        help="incident-archive 只处理超过该天数的记录（默认 90）",
    )
    lesson.add_argument(
        "--max-archive", type=int, default=50,
        help="incident-archive 单次最多处理数量（默认 50）",
    )
    lesson.add_argument("--cases", type=Path)
    lesson.add_argument("--mode", choices=("cases", "gain", "tools"), default="cases",
                        help="replay 模式：cases=模式检索；gain=incident 时序 holdout；tools=工具经验")
    lesson.add_argument("--holdout-mod", type=int, default=5)
    lesson.add_argument("--holdout-remainder", type=int, default=0)
    lesson.add_argument("--json", action="store_true")
    lesson.set_defaults(handler=command_lesson)

    logic_analysis = subparsers.add_parser(
        "logic-analysis", help="校验或绑定意图、逻辑链、Oracle 与证据包",
    )
    logic_analysis.add_argument(
        "logic_command", choices=("validate", "bind", "bind-finding"),
        help="validate=校验证据包；bind=绑定到 CoverageTask；bind-finding=绑定到 Finding",
    )
    logic_analysis.add_argument("--root", type=Path, default=Path.cwd())
    logic_analysis.add_argument("--input", type=Path, required=True,
                                help="logic analysis bundle JSON")
    logic_analysis.add_argument("--task", type=Path,
                                help="bind 时提供待绑定的 CoverageTask JSON")
    logic_analysis.add_argument("--finding", type=Path,
                                help="bind-finding 时提供待绑定的 Finding JSON")
    logic_analysis.add_argument("--output", type=Path,
                                help="bind 输出路径（缺省输出到 stdout）")
    logic_analysis.set_defaults(handler=command_logic_analysis)

    probe = subparsers.add_parser(
        "probe", help="运行 core 探针（efficiency/sast/mobile 等），透传其参数",
    )
    probe.add_argument(
        "probe_kind",
        choices=tuple(PROBE_SCRIPTS),
        help="探针类别（对应 <kind>_probe.py）",
    )
    probe.add_argument("--root", type=Path, default=Path.cwd(),
                       help="项目根（默认当前目录）")
    probe.add_argument("probe_args", nargs=argparse.REMAINDER,
                       help="透传给探针的参数（如 check --json --strict）")

    knowledge = subparsers.add_parser(
        "knowledge", help="治理项目候选与用户全局错题集（不自动同步）",
    )
    knowledge.add_argument(
        "knowledge_command", choices=("status", "export", "approve", "import", "rollback"),
    )
    knowledge.add_argument("--root", type=Path, default=Path.cwd())
    knowledge.add_argument("--bundle", type=Path)
    knowledge.add_argument("--output", type=Path)
    knowledge.add_argument("--bundle-id")
    knowledge.add_argument("--contributor", default="local-supervisor")
    knowledge.add_argument("--reviewer")
    knowledge.add_argument("--reason")
    knowledge.set_defaults(handler=command_knowledge)
    probe.set_defaults(handler=command_probe)

    skill_cmd = subparsers.add_parser(
        "skill", help="审计技能包管理、图谱状态查询、契约校验与原子回滚",
    )
    skill_cmd.add_argument(
        "skill_action", choices=("list", "status", "validate", "rollback", "import", "pull"),
    )
    skill_cmd.add_argument("--root", type=Path, default=Path.cwd())
    skill_cmd.add_argument("--skill", default="web-audit")
    skill_cmd.add_argument("--bundle", type=Path, help="离线技能包归档")
    skill_cmd.add_argument("--json", action="store_true")
    skill_cmd.set_defaults(handler=command_skill)

    self_audit = subparsers.add_parser(
        "self-audit", help="对 BugX 本地 CLI/MCP 只读入口做自举动态审计",
    )
    self_audit.add_argument("--root", type=Path, default=Path.cwd(),
                            help="项目根（默认当前目录）")
    self_audit.add_argument(
        "self_audit_args", nargs=argparse.REMAINDER,
        help="透传给 self_audit.py（缺省运行 all 模式）",
    )
    self_audit.set_defaults(handler=command_self_audit)

    engine = subparsers.add_parser(
        "engine", help="从全局引擎运行生命周期命令（项目不再提供 .bugx/core 入口）",
    )
    engine.add_argument(
        "--root", type=Path, default=None,
        help="项目根目录；缺省时依次读 BUGX_PROJECT_ROOT / 宿主工作区环境变量，最后才用 cwd",
    )
    engine.add_argument(
        "engine_args", nargs=argparse.REMAINDER,
        help="透传给全局生命周期引擎；例如 pre、status、workers、post",
    )
    engine.set_defaults(handler=command_engine)

    cloud = subparsers.add_parser("cloud", help="云端错题/经验集同步（显式命令）")
    cloud_sub = cloud.add_subparsers(dest="cloud_command", required=True)

    def _cloud_common(p):
        p.add_argument("--root", type=Path, default=Path.cwd())
        p.add_argument("--project-slug", dest="project_slug", help="项目 slug（缺省=仓库目录名）")
        p.add_argument("--accept-data-sharing", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("--privacy", choices=("standard", "strict"), default=argparse.SUPPRESS)
        p.set_defaults(handler=command_cloud)
        return p

    auth = subparsers.add_parser("auth", help="登录、查看、设置或清除云端登录态")

    def _auth_common(p):
        p.add_argument("--root", type=Path, default=Path.cwd())
        p.add_argument("--project-slug", dest="project_slug", help="项目 slug（缺省=仓库目录名）")
        p.add_argument("--accept-data-sharing", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("--privacy", choices=("standard", "strict"), default=argparse.SUPPRESS)
        p.set_defaults(handler=command_cloud, cloud_command="auth")
        return p

    p_auth = _auth_common(auth)
    p_auth.add_argument("auth_action", choices=("login", "status", "set-key", "logout"))
    p_auth.add_argument("--api-key", dest="api_key")
    p_auth.add_argument("--json", action="store_true")
    p_auth.add_argument(
        "--verify", dest="verify_auth", action="store_true",
        help="通过只读云端接口验证登录态（可能触发刷新令牌，不创建或修改业务数据）",
    )
    p_config = _cloud_common(cloud_sub.add_parser(
        "config", help="查看或设置全局云端 API Base URL / 版本检查（不保存凭证）",
    ))
    config_group = p_config.add_mutually_exclusive_group()
    config_group.add_argument("--base-url", help="云端 API Base URL，例如 https://bugx.example.com")
    config_group.add_argument("--clear", dest="clear_base_url", action="store_true",
                              help="清除持久化 Base URL，恢复默认地址")
    config_group.add_argument(
        "--refresh-route", dest="refresh_route", action="store_true",
        help="强制清除路由测速缓存并重新并发竞速优选",
    )
    config_group.add_argument(
        "--disable-version-check", dest="disable_version_check",
        choices=("true", "false"), default=None,
        help="设置是否禁用启动时版本检查（true=禁用，false=启用）",
    )
    p_config.add_argument("--json", action="store_true")
    p_status = _cloud_common(cloud_sub.add_parser(
        "status", help="只读查看本地同步 cursor、失败、队列和冲突状态",
    ))
    p_status.add_argument(
        "--scope", dest="status_scope", choices=("project", "global", "all"), default="all",
        help="查看范围；默认 all",
    )
    p_status.add_argument("--json", action="store_true")
    p_log = _cloud_common(cloud_sub.add_parser(
        "log-report",
        help="上报本项目的诊断日志（内测期排查用；仅在 AI/用户显式要求时调用，默认不上报）",
        description="打包项目诊断日志（bugx.jsonl 尾部）上报到云端 POST"
                    " /api/v1/agent/log-reports（登录认证 + 每日限额，限额管理端可配）。",
    ))
    p_log.add_argument("--tail-kb", dest="tail_kb", type=int, default=256,
                       help="日志尾部大小上限（KB，默认 256）")
    p_log.add_argument("--kind", dest="kind", default="cli-diagnostics",
                       help="上报类型标记（默认 cli-diagnostics）")
    p_log.add_argument("--json", action="store_true")
    p_feedback = _cloud_common(cloud_sub.add_parser(
        "feedback",
        help="扫描收尾批量上报错题卡质量反馈（hit-feedback 质量闭环）",
        description="把本地错题卡反馈批量回报云端 POST /api/v1/agent/lessons/hit-feedback"
                    "（outcome ∈ hit|miss|false_positive|rejected）。",
    ))
    p_feedback.add_argument("--lifecycle", dest="lifecycle",
                            help="仅上报指定 lifecycle 的反馈（缺省=全部本地反馈）")
    p_feedback.add_argument("--run-id", dest="run_id", default="",
                            help="云端日志上下文 run_id（可选，不落库）")
    p_feedback.add_argument("--project", dest="project_name", default="",
                            help="云端日志上下文项目名（可选，不落库）")
    p_feedback.add_argument("--limit", type=int, default=100,
                            help="单次上报上限（1..100，默认 100）")
    p_feedback.add_argument("--dry-run", action="store_true",
                            help="只打印将上报的条目，不联网")
    p_feedback.add_argument("--json", action="store_true")
    p_sync = _cloud_common(cloud_sub.add_parser(
        "sync", help="立即执行云端同步；未登录时自动打开授权页",
    ))
    p_sync.add_argument("--scope", dest="sync_scope", choices=("project", "global", "all"), default=None,
                        help="显式授权同步范围；缺省读 cloud-config.json sync_scope_default（默认 all）")
    p_sync.add_argument("--detail-mode", choices=("full", "summary"), default=None,
                        help="私有同步详情粒度；缺省读 cloud-config.json sync_detail_mode（默认 full），summary 仅上传摘要/计数/状态")
    p_sync.add_argument("--calibrate", action="store_true",
                        help="自动校准基线：重置失效游标、修剪已收敛冲突并自愈恢复基线状态")
    p_export = _cloud_common(cloud_sub.add_parser(
        "export", help="把本地知识导出为可恢复的 V2 JSON bundle（不联网）",
    ))
    p_export.add_argument("--scope", dest="bundle_scope", choices=("global", "project"),
                          default="project", help="导出范围；默认 project")
    p_export.add_argument("--detail-mode", choices=("full", "summary"), default="full")
    p_export.add_argument("--output", required=True, help="JSON 输出文件路径")
    p_upload = _cloud_common(cloud_sub.add_parser(
        "upload", help="上传本地 V2 JSON bundle 并可恢复本地同步基线",
    ))
    p_upload.add_argument("--input", required=True, help="JSON bundle 文件路径")
    p_upload.add_argument("--mode", choices=("merge", "local_authoritative"),
                          default="merge")
    p_upload.add_argument("--json", action="store_true")
    p_conflicts = _cloud_common(cloud_sub.add_parser(
        "conflicts", help="查看和治理本地同步冲突队列（list / resolve / prune，不联网）",
    ))
    p_conflicts.add_argument(
        "conflict_action", nargs="?", choices=("list", "resolve", "prune"), default="list",
        help="list=列出待治理冲突（默认）；resolve=按选边解决一条冲突；prune=自动修剪已收敛冲突",
    )
    p_conflicts.add_argument("conflict_id", nargs="?", help="resolve 的目标 conflict_id")
    p_conflicts.add_argument(
        "--side", dest="conflict_side", choices=("local", "remote"),
        help="resolve 选边：local=保留本地丢弃远端记录；remote=接受远端",
    )
    p_conflicts.add_argument(
        "--scope", dest="conflict_scope", choices=("project", "global", "all"), default="all",
        help="查看/解决范围；默认 all",
    )
    p_conflicts.add_argument(
        "--all", dest="conflict_all", action="store_true",
        help="resolve 批量形态：按选边解决范围内全部 open 冲突（与 conflict_id 互斥）",
    )
    p_conflicts.add_argument(
        "--reason-filter", dest="conflict_reason_filter", default="",
        help="配合 --all：仅解决 reason 匹配的冲突，例如 content_diverged",
    )
    p_conflicts.add_argument("--json", action="store_true")
    p_dead = _cloud_common(cloud_sub.add_parser(
        "dead-letter", help="查看与清理同步死信队列（list / purge，不联网）",
    ))
    p_dead.add_argument(
        "dead_letter_action", nargs="?", choices=("list", "purge"), default="list",
        help="list=列出死信（默认）；purge=清理死信（需 --confirm，先归档后删除）",
    )
    p_dead.add_argument(
        "--confirm", dest="dead_letter_confirm", action="store_true",
        help="确认执行删除；缺省仅 dry-run 打印将删条数",
    )
    p_dead.add_argument(
        "--scope", dest="dead_letter_scope", choices=("project", "global", "all"), default="all",
        help="列出/清理范围；默认 all",
    )
    p_dead.add_argument(
        "--older-than-days", dest="dead_letter_older_than_days", type=int, default=0,
        help="仅处理 created_at 早于 N 天的死信；0=全部（默认）",
    )
    p_dead.add_argument("--json", action="store_true")
    p_proj = _cloud_common(cloud_sub.add_parser("projects"))
    p_proj.add_argument("cloud_project_action", choices=("list", "add"))

    _cloud_common(cloud_sub.add_parser("devices"))
    p_onb = _cloud_common(cloud_sub.add_parser(
        "onboard", help="查看或切换项目同步；on 会立即同步 global + project",
    ))
    p_onb.add_argument(
        "onboard_action", nargs="?", choices=("status", "on", "off"), default="status",
        help="status=查看；on=开启并立即同步；off=关闭自动同步并进入本地模式",
    )

    telemetry = subparsers.add_parser("telemetry", help="查看或导出单向飞轮取证数据")
    telemetry_sub = telemetry.add_subparsers(dest="telemetry_command", required=True)
    telemetry_inspect = telemetry_sub.add_parser("inspect", help="检查本地出网取证日志")
    telemetry_inspect.add_argument("--latest", action="store_true")
    telemetry_inspect.add_argument("--json", action="store_true")
    telemetry_inspect.set_defaults(handler=command_telemetry)
    telemetry_export = telemetry_sub.add_parser("export", help="导出加密离线穿梭包")
    telemetry_export.add_argument("--out", type=Path, required=True)
    telemetry_export.add_argument("--passphrase")
    telemetry_export.set_defaults(handler=command_telemetry)
    telemetry_flush = telemetry_sub.add_parser("flush", help="主动推送并清空本地脱敏出网飞轮队列 (upload)")
    telemetry_flush.add_argument("--batch-size", type=int, default=20, help="每批聚合上报条数")
    telemetry_flush.set_defaults(handler=command_telemetry)
    telemetry_upload = telemetry_sub.add_parser("upload", help="主动推送并清空本地脱敏出网飞轮队列 (flush 别名)")
    telemetry_upload.add_argument("--batch-size", type=int, default=20, help="每批聚合上报条数")
    telemetry_upload.set_defaults(handler=command_telemetry)

    config = subparsers.add_parser("config", help="设置本地 BugX 隐私与遥测配置")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_set = config_sub.add_parser("set", help="设置一项配置")
    config_set.add_argument(
        "config_key",
        choices=("telemetry.data_sharing",
                 "cloud.sync_detail_mode", "cloud.sensitive_policy",
                 "cloud.sync_scope_default"),
        help="telemetry.data_sharing=遥测数据分享；cloud.*=DOC-211 同步内容策略",
    )
    config_set.add_argument(
        "config_value",
        help="telemetry.data_sharing 取 true/false；cloud.sync_detail_mode 取 full/summary；"
             "cloud.sensitive_policy 取 block/redact/allow；cloud.sync_scope_default 取 all/project/global",
    )
    config_set.set_defaults(handler=command_telemetry_config)

    daemon = subparsers.add_parser(
        "daemon", help="BugX 自治持续挖掘常驻守护进程管理",
    )
    daemon_sub = daemon.add_subparsers(dest="daemon_action", required=True)
    d_start = daemon_sub.add_parser("start", help="启动常驻守护进程")
    d_start.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    d_start.add_argument("--foreground", action="store_true", help="前台运行")
    d_start.add_argument("--max-wall-time", type=float, default=14400.0, help="单次 Session 软预算检测窗口（秒）")
    d_start.add_argument("--interval", type=float, default=5.0, help="调度巡检间隔（秒）")
    d_start.set_defaults(handler=command_daemon)

    d_stop = daemon_sub.add_parser("stop", help="停止常驻守护进程")
    d_stop.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    d_stop.add_argument("--force", action="store_true", help="强制终止")
    d_stop.set_defaults(handler=command_daemon)

    d_status = daemon_sub.add_parser("status", help="查询守护进程状态与健康度")
    d_status.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    d_status.add_argument("--json", action="store_true", help="输出 JSON 格式")
    d_status.set_defaults(handler=command_daemon)

    d_attach = daemon_sub.add_parser("attach", help="热回显并连接常驻日志输出")
    d_attach.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    d_attach.add_argument("-n", "--lines", type=int, default=50, help="初始回显行数")
    d_attach.set_defaults(handler=command_daemon)

    # 宿主清单与并发区间都来自插件事实（``worker_runtime.autonomous_cli`` /
    # ``concurrency_bounds``），参数层不再写宿主名。
    autonomous_hosts = autonomous_host_names()
    autonomous_labels = "/".join(engine_host_label(name) for name in autonomous_hosts)
    process_pool_host = next(
        (name for name in autonomous_hosts if engine_host_concurrency_bounds(name)), "",
    )
    final_bounds = engine_host_concurrency_bounds(process_pool_host)
    workers_help = (
        f"{engine_host_label(process_pool_host)} CLI 进程池并发度"
        f"（{final_bounds[0]}~{final_bounds[1]}）；0 表示按 Supervisor 自适应容量"
        if final_bounds
        else "进程池并发度；0 表示按 Supervisor 自适应容量"
    )
    autonomous_cmd = subparsers.add_parser(
        "autonomous", help=f"显式启动 {autonomous_labels} 前台无人值守挖掘",
    )
    autonomous_cmd.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    autonomous_cmd.add_argument("--host", choices=autonomous_hosts, required=True,
                                help="宿主 Agent；不自动猜测")
    autonomous_cmd.add_argument(
        "--workers", type=int, default=0,
        help=workers_help,
    )
    autonomous_cmd.add_argument(
        "--model", type=str, default="",
        help="指定宿主调用的模型 ID（如 gpt-4o、deepseek-chat 等）；缺省时自动使用宿主已配置的同款自定义模型",
    )
    autonomous_cmd.add_argument(
        "--provider", type=str, default="",
        help="指定宿主调用的 Provider ID（如 openai-compatible、deepseek 等）；缺省时自动读取宿主已配置的 Provider",
    )
    autonomous_cmd.add_argument(
        "--max-turns", type=int, default=0,
        help="宿主回合上限；0 表示持续恢复同一会话，直到权威运行态完成或硬失败",
    )
    autonomous_cmd.add_argument(
        "--max-wall-time", type=float, default=0.0,
        help="宿主墙钟上限（秒）；0 表示持续运行，直到权威运行态完成或硬失败",
    )
    autonomous_cmd.add_argument(
        "--agent-timeout", type=float, default=600.0,
        help="单次宿主回合无进展上限（秒）；supervisor 心跳新鲜的回合自动滑动续期（硬上限 8 倍），心跳静默超时后读取权威状态并自动恢复，默认 600",
    )
    autonomous_cmd.add_argument(
        "--coverage-manifest", type=Path, default=None,
        help=(
            "显式覆盖清单（Markdown 表格）：把本轮审计范围限定为清单里的单元，"
            "不 reconcile 到全仓。大仓/引擎自审/带 vendored 目录的项目必须用它，"
            "否则清单门禁会把全仓文件都当单元"
        ),
    )
    autonomous_cmd.add_argument(
        "--no-adaptive-wall-time", action="store_false", dest="adaptive_wall_time", default=True,
        help="关闭根据进展与复杂度自适应动态延长墙钟预算（默认开启）",
    )
    autonomous_cmd.add_argument(
        "--adaptive-baseline", action="store_true", default=False,
        help="在未显式指定 --max-wall-time 时，依据覆盖清单与单元规模自动估算初始基准墙钟时间",
    )
    autonomous_cmd.add_argument("--json", action="store_true")
    autonomous_cmd.set_defaults(handler=command_autonomous)

    goal_cmd = subparsers.add_parser(
        "goal", help="目标导向无人值守自治挖掘与长程自愈循环",
    )
    goal_cmd.add_argument("target_description", nargs="?", default="", help="目标描述或指定范围")
    goal_cmd.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    # 可选宿主与 autonomous 同源（插件声明 + 未迁移基线），不再写第二份清单。
    goal_cmd.add_argument("--host", choices=autonomous_hosts, default="claude",
                          help="宿主 Agent (默认 claude)")
    goal_cmd.add_argument("--target-coverage", type=float, default=100.0,
                          help="目标覆盖率百分比 (0.0~100.0，默认 100.0)")
    goal_cmd.add_argument("--allow-open-findings", action="store_true",
                          help="允许存在未闭环漏洞即收敛（默认必须零遗留漏洞）")
    goal_cmd.add_argument("--disallow-quarantine", action="store_true",
                          help="禁止隔离坏单元（默认开启 3-strike 坏单元隔离）")
    goal_cmd.add_argument(
        "--max-turns", type=int, default=0,
        help="宿主回合上限；0 表示持续推进，直到达成目标或硬失败",
    )
    goal_cmd.add_argument(
        "--max-wall-time", type=float, default=14400.0,
        help="全局最大硬超时（秒）；默认 14400 (4小时)，0 表示无限制",
    )
    goal_cmd.add_argument(
        "--agent-timeout", type=float, default=600.0,
        help="单次回合无进展上限（秒）；supervisor 心跳新鲜的回合自动滑动续期（硬上限 8 倍），默认 600",
    )
    goal_cmd.add_argument(
        "--max-unit-retries", type=int, default=3,
        help="单单元最大连续失败重试次数，超限进入隔离区，默认 3",
    )
    goal_cmd.add_argument(
        "--max-transient-retries", type=int, default=5,
        help="最大连续瞬态错误自愈重试次数，默认 5",
    )
    goal_cmd.add_argument("--json", action="store_true", help="JSON 格式输出")
    goal_cmd.set_defaults(handler=command_goal)

    sup = subparsers.add_parser(
        "supervisor",
        help=(
            "本地监督 MCP：install / status / rpc；"
            "带外动作：reset-budget / recover / rebind-target / gate / findings"
        ),
    )
    sup.add_argument(
        "supervisor_command",
        choices=(
            "install", "status", "rpc",
            # 带外动作白名单由 commands.system 单一真源派生（恢复 + 数据修复）：
            # 此处不再复制枚举，避免「帮助列出但 dispatch 拒绝」的静默漂移。
            *system.SUPERVISOR_PASSTHROUGH_ACTIONS,
        ),
    )
    sup.add_argument("--root", type=Path, default=Path.cwd())
    sup.add_argument("supervisor_args", nargs=argparse.REMAINDER, default=[])
    sup.set_defaults(handler=command_supervisor)

    plugin = subparsers.add_parser(
        "plugin", help="安装、校验或查看私有 BugX 插件（不执行插件）",
    )
    plugin.add_argument(
        "plugin_command",
        choices=("list", "status", "install", "uninstall", "zcode"),
    )
    plugin.add_argument("plugin_name", nargs="?")
    plugin.add_argument("--root", type=Path, default=Path.cwd())
    plugin.add_argument("--source", type=Path)
    plugin.add_argument("--force", action="store_true")
    plugin.add_argument(
        "--remove", action="store_true",
        help="plugin zcode：从 ZCode plugins.dirs 移除本项目插件目录",
    )
    plugin.add_argument("--json", action="store_true")
    plugin.add_argument("--yes", action="store_true", help="确认插件卸载")
    plugin.set_defaults(handler=command_plugin)

    # ── update：自动更新 ──
    update_parser = subparsers.add_parser(
        "update", help="检查并安装最新 BugX CLI 版本",
    )
    update_parser.add_argument("--root", type=Path, default=Path.cwd())
    update_parser.add_argument(
        "--check", action="store_true",
        help="只检查版本，不下载或安装",
    )
    update_parser.add_argument(
        "--force", action="store_true",
        help="即使已是最新版本也强制重新安装",
    )
    update_parser.add_argument(
        "--repair-assets", action="store_true",
        help="从已验证的 stable wheel 恢复缺失的 CLI 安装资源",
    )
    update_parser.add_argument(
        "--allow-downgrade", action="store_true",
        help="允许服务端版本低于当前版本时执行回退（服务器版本回退场景）",
    )
    update_parser.add_argument(
        "--channel", "-c", type=str, default="",
        choices=["stable", "alpha", ""],
        help="指定发布通道：stable 或 alpha（默认使用 stable 稳定通道）",
    )
    update_parser.set_defaults(handler=command_update)

    # ── patch-forge：静态补丁包 ──
    patch_parser = subparsers.add_parser(
        "patch-forge", help="生成或静态校验补丁包（不执行包内代码、不提交远端）",
    )
    patch_mode = patch_parser.add_mutually_exclusive_group(required=False)
    patch_mode.add_argument("--from-issue", help="issue/spec Markdown 路径（相对 --root）")
    patch_mode.add_argument("--verify", help="只静态校验已有补丁包目录")
    patch_mode.add_argument("--status", help="查询远程补丁验证记录 ID")
    patch_parser.add_argument("--root", type=Path, default=Path.cwd())
    patch_parser.add_argument("--kind", choices=("engine",), help="补丁对象：仅允许 BugX 自身引擎代码")
    patch_parser.add_argument("--base-version", default=__version__)
    patch_parser.add_argument("--patch-id", default=None)
    patch_parser.add_argument("--diff", action="append", default=[])
    patch_parser.add_argument("--repro", action="append", default=[])
    patch_parser.add_argument("--test", action="append", default=[])
    patch_parser.add_argument("--signature")
    patch_parser.add_argument("--severity", choices=("high", "medium", "low", "info"))
    patch_parser.add_argument("--output")
    patch_parser.add_argument("--dry-run", action="store_true")
    patch_parser.add_argument("--json", action="store_true")
    patch_parser.add_argument("--submit", action="store_true", help="生成后提交远程静态验证（显式副作用）")
    patch_parser.add_argument("--api-key", help="远程补丁验证专用 API key（默认读取环境变量）")
    patch_parser.add_argument("--base-url", help="远程云端基址")
    patch_parser.add_argument("--source-commit", default="", help="绑定 Jenkins/源仓库提交 SHA")
    patch_parser.add_argument("--spec-only", action="store_true")
    patch_parser.add_argument("--session-id", "--session", dest="session_id")
    patch_parser.add_argument("--run-id")
    patch_parser.add_argument("--lifecycle-id")
    patch_parser.add_argument("--repository")
    patch_parser.add_argument("--finding-fingerprint", action="append", default=[])
    patch_parser.add_argument(
        "--test-red-green", action="store_true",
        help="执行本地红绿闭环受限验证（repro 失败 -> 补丁应用 -> repro 消除 -> tests 通过）",
    )
    patch_parser.set_defaults(handler=command_patch_forge)

    # ── log：诊断日志分析与瀑布图 ──
    log_parser = subparsers.add_parser(
        "log", help="查看诊断日志、错误排查、链路追踪与耗时瀑布图",
    )
    log_parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    log_parser.add_argument(
        "--tail", nargs="?", const=20, type=int, default=None,
        help="显示最新 N 条日志（默认 20 条）",
    )
    log_parser.add_argument(
        "--watch", "-f", "--follow", action="store_true", dest="watch",
        help="实时流式监听日志新增记录（支持 Ctrl-C 退出）",
    )
    log_parser.add_argument(
        "--errors", action="store_true",
        help="仅显示警告、错误日志及异常堆栈",
    )
    log_parser.add_argument(
        "--trace", type=str, default=None,
        help="按 trace_id 聚合检索执行链路流水",
    )
    log_parser.add_argument(
        "--timing", "--waterfall", action="store_true", dest="timing",
        help="生成阶段耗时与 Token 消耗双轨 ASCII 瀑布图",
    )
    log_parser.add_argument(
        "--timeline", "--replay", action="store_true", dest="timeline",
        help="回放对话与操作每一步执行流（含耗时、状态与摘要）",
    )
    log_parser.add_argument(
        "--anomalies", "--audit", action="store_true", dest="anomalies",
        help="智能审计耗时异常、慢命令、高危扫描与死循环",
    )
    log_parser.add_argument(
        "--tokens", action="store_true",
        help="按 Token 消耗量排名展示阶段贡献度排行榜",
    )
    log_parser.add_argument(
        "--bundle", action="store_true",
        help="生成一键脱敏诊断打包文件 (.tar.gz)",
    )
    log_parser.add_argument(
        "--mcp-polling", action="store_true",
        help="输出 tools/list 服务端缓存命中与外部轮询间隔诊断（不证明客户端已缓存）",
    )
    log_parser.add_argument(
        "--json", action="store_true",
        help="以 JSON 输出诊断结果",
    )
    log_parser.add_argument(
        "--output", "-o", type=Path, default=None,
        help="指定诊断包导出路径",
    )
    log_parser.set_defaults(handler=command_log)

    # ── stats：全局与跨项目时间与 Token 计量统计看板 (DOC-049) ──
    stats_parser = subparsers.add_parser(
        "stats", help="查看全局跨项目时间与 Token 消耗治理看板",
    )
    stats_parser.add_argument(
        "--global", action="store_true", dest="global_mode",
        help="查看全机跨项目全局时间、Token 与 ROI 统计看板",
    )
    stats_parser.add_argument(
        "--days", type=int, default=30,
        help="统计时间窗口（天数，默认 30 天）",
    )
    stats_parser.add_argument(
        "--json", action="store_true",
        help="以 JSON 格式输出统计指标",
    )
    stats_parser.set_defaults(handler=command_stats)

    # ── project：工程代码体量、技术栈画像与规模估模分析 ──
    project_parser = subparsers.add_parser(
        "project", help="工程代码体量、技术栈画像与规模估模分析",
    )
    project_sub = project_parser.add_subparsers(dest="project_action")

    p_analyze = project_sub.add_parser("analyze", help="全景扫描并分析当前或指定工程的代码体量与画像")
    p_analyze.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录（默认当前目录）")
    p_analyze.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p_analyze.set_defaults(handler=command_project)

    p_show = project_sub.add_parser("show", help="查看已缓存的项目画像与度量")
    p_show.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录（默认当前目录）")
    p_show.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p_show.set_defaults(handler=command_project)

    p_sync = project_sub.add_parser("sync", help="将项目画像与代码指标同步至云端看板")
    p_sync.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录（默认当前目录）")
    p_sync.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p_sync.set_defaults(handler=command_project)

    p_list = project_sub.add_parser("list", help="查看全局所有受管工程列表 (~/.bugx/projects.json)")
    p_list.add_argument("--root", type=Path, default=Path.cwd(), help="当前工作区根目录")
    p_list.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p_list.set_defaults(handler=command_project)

    p_refresh = project_sub.add_parser("refresh", help="全量扫描并刷新全局受管工程列表 (~/.bugx/projects.json)")
    p_refresh.add_argument("--root", type=Path, default=Path.cwd(), help="当前工作区根目录")
    p_refresh.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p_refresh.set_defaults(handler=command_project)

    p_rebind = project_sub.add_parser(
        "rebind",
        help="诊断或迁移 .bugx 的项目身份（项目目录搬移/改名后使用）",
    )
    p_rebind.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录（默认当前目录）")
    p_rebind.add_argument(
        "--migrate", action="store_true",
        help="把盘上 project_id 重写为当前路径指纹并保留 project_uid（确认是搬移而非误用他人布局）",
    )
    p_rebind.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p_rebind.set_defaults(handler=command_project)

    p_settings = project_sub.add_parser("settings", help="查看或修改项目的门禁配置、挖掘情报、MCP 与 Skill")
    p_settings.add_argument("target", nargs="?", default=None, help="目标工程路径或工程 Hash（默认当前工作区）")
    p_settings.add_argument("--root", type=Path, default=Path.cwd(), help="当前工作区根目录")
    p_settings.add_argument("--set-cloud-sync", choices=["on", "off"], default=None, help="设置是否开启云端同步")
    p_settings.add_argument("--set-review", choices=["auto", "interactive", "off"], default=None, help="设置审查执行模式")
    p_settings.add_argument("--set-polling", choices=["auto", "manual", "off"], default=None, help="设置漏洞轮询策略")
    p_settings.add_argument("--set-auto-fix", choices=["on", "off", "confirm"], default=None, help="设置是否自动修改代码")
    p_settings.add_argument("--set-commit", choices=["round", "task", "off"], default=None, help="设置代码自动提交策略")
    p_settings.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p_settings.set_defaults(handler=command_project)

    project_parser.set_defaults(project_action="analyze", handler=command_project)
    project_parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录（默认当前目录）")
    project_parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出")

    # ── view：安全查看源文件切片与视窗（DOC-225，具越界自愈与沙箱隔离）──
    view_parser = subparsers.add_parser(
        "view", help="安全查看源文件切片与视窗（具备越界自愈与沙箱隔离）",
    )
    view_parser.add_argument("path", help="待查看的文件路径")
    view_parser.add_argument(
        "--lines", "-L", default="1:200",
        help="行范围，例如 42:90、42-90 或 50（默认 1:200）",
    )
    view_parser.add_argument(
        "--no-numbers", "-N", action="store_true",
        help="不显示行号前缀",
    )
    view_parser.add_argument(
        "--json", action="store_true",
        help="以 JSON 结构化格式输出",
    )
    view_parser.add_argument(
        "--root", type=Path, default=None,
        help="项目根目录（用于沙箱白名单）",
    )
    view_parser.add_argument(
        "--max-lines", type=int, default=500,
        help="单次最大读取行数（默认 500）",
    )
    view_parser.set_defaults(handler=command_view)

    # ── test-generate：全息自动化测试脚手架生成 ──
    tg_parser = subparsers.add_parser(
        "test-generate", help="为指定单元自动生成标准化 AAA 测试用例或自包含 PoC",
    )
    tg_parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根路径")
    tg_parser.add_argument("--scope", required=True, help="被测单元符号范围，如 path/foo.py::func")
    tg_parser.add_argument("--techniques", default="boundary,negative-control", help="测试策略逗号列表")
    tg_parser.add_argument("--out", type=Path, help="输出文件路径")
    tg_parser.add_argument("--standalone", action="store_true", help="生成自包含 0/1 PoC 验证脚本")
    tg_parser.add_argument("--assert-expr", default="", help="成功结果断言表达式；测试套件中可引用 res")
    tg_parser.add_argument("--expected-exc", default="", help="测试套件/PoC 的预期异常类名")
    tg_parser.add_argument("--json", action="store_true")
    tg_parser.set_defaults(handler=command_test_generate)

    # ── test-audit：测试用例四维质量量化审计 ──
    ta_parser = subparsers.add_parser(
        "test-audit", help="对测试用例进行四维（准确、完整、有效、全面）量化质量审计",
    )
    ta_parser.add_argument("file", type=Path, help="待审计测试源文件路径")
    ta_parser.add_argument("--scope", default="", help="被测单元符号范围")
    ta_parser.add_argument("--min-score", type=float, default=70.0, help="最低质量准入得分")
    ta_parser.add_argument("--strict", action="store_true", help="严格模式：有违规即退出 1")
    ta_parser.add_argument("--json", action="store_true")
    ta_parser.set_defaults(handler=command_test_audit)

    # ── mining-audit：挖掘方案与用例全息质量审计 ──
    ma_parser = subparsers.add_parser(
        "mining-audit", help="对漏洞挖掘方案与用例进行四维（准确、完整、有效、全面）质量量化审计与加固",
    )
    ma_sub = ma_parser.add_subparsers(dest="mining_subcommand", required=True)

    ma_p_audit = ma_sub.add_parser("audit", help="量化审计挖掘方案")
    ma_p_audit.add_argument("plan_file", type=Path, help="挖掘方案 JSON 文件路径")
    ma_p_audit.add_argument("--root", type=Path, default=Path.cwd(), help="项目根路径")
    ma_p_audit.add_argument("--min-score", type=float, default=80.0, help="最低通过分数（默认 80.0）")
    ma_p_audit.add_argument("--strict", action="store_true", help="严格模式（有违规项即阻断）")
    ma_p_audit.add_argument(
        "--allow-degraded", action="store_true",
        help="明确接受降级方案；否则 DEGRADED 退出码为 2，要求先取得用户决策",
    )
    ma_p_audit.add_argument("--json", action="store_true")
    ma_p_audit.set_defaults(handler=command_mining_audit)

    ma_p_explore = ma_sub.add_parser("explore", help="勘测项目可挖掘功能主体（多语种 AST/Web路由/CLI/反序列化/临界区）")
    ma_p_explore.add_argument("--root", type=Path, default=Path.cwd(), help="项目根路径")
    ma_p_explore.add_argument("--language", help="显式指定扫描语言；省略时按文件扩展名识别")
    ma_p_explore.add_argument("--json", action="store_true")
    ma_p_explore.set_defaults(handler=command_mining_audit)

    ma_p_harden = ma_sub.add_parser("harden", help="自动加固补全挖掘方案")
    ma_p_harden.add_argument("plan_file", type=Path, help="待加固挖掘方案 JSON 文件")
    ma_p_harden.add_argument("--root", type=Path, default=Path.cwd(), help="项目根路径")
    ma_p_harden.add_argument("--out", type=Path, help="加固后保存路径")
    ma_p_harden.set_defaults(handler=command_mining_audit)
    ma_parser.set_defaults(handler=command_mining_audit)

    # ── test-verify：测试用例能力动态验证框架 ──
    tv_parser = subparsers.add_parser(
        "test-verify", help="基于 AST 语义变异与沙箱隔离动态验证测试用例的抓 Bug 杀伤能力",
    )
    tv_sub = tv_parser.add_subparsers(dest="verify_subcommand", required=True)

    tv_p_verify = tv_sub.add_parser("verify", help="动态验证测试用例对目标代码的变异杀伤能力")
    tv_p_verify.add_argument("--target", required=True, help="被测源码文件路径 (例如 core/foo.py)")
    tv_p_verify.add_argument("--test", required=True, help="测试用例模块或路径 (例如 tests/test_foo.py)")
    tv_p_verify.add_argument("--root", type=Path, default=Path.cwd(), help="项目根路径")
    tv_p_verify.add_argument("--max-mutants", type=int, default=30, help="最大变异体数量 (默认 30)")
    tv_p_verify.add_argument("--timeout", type=float, default=2.0, help="单次变异测试超时秒数 (默认 2.0s)")
    tv_p_verify.add_argument("--strict", action="store_true", help="严格门禁模式 (击杀率 < 60%% 时非零退出)")
    tv_p_verify.add_argument("--json", action="store_true")
    tv_p_verify.set_defaults(handler=command_test_verify)

    tv_p_mutate = tv_sub.add_parser("mutate", help="仅生成并预览 AST 变异体")
    tv_p_mutate.add_argument("--target", required=True, help="目标源码路径")
    tv_p_mutate.add_argument("--root", type=Path, default=Path.cwd(), help="项目根路径")
    tv_p_mutate.add_argument("--max-mutants", type=int, default=30, help="最大变异体数量")
    tv_p_mutate.add_argument("--json", action="store_true")
    tv_p_mutate.set_defaults(handler=command_test_verify)
    tv_parser.set_defaults(handler=command_test_verify)

    # ── audit：项目级一键全息攻防渗透审计、Artifact Graph 编排与双轨报告交付 ──
    audit.register_parser(subparsers)

    # ── interaction：非交互宿主的启动门禁答案回灌通道 ──
    interaction.register_parser(subparsers)

    # ── advisory：GitHub Security Advisory (GHSA) 标准公告与 PR 草稿 ──
    adv_parser = subparsers.add_parser("advisory", help="GitHub Security Advisory (GHSA) 标准安全公告与 PR 修复草案管理")
    adv_sub = adv_parser.add_subparsers(dest="advisory_command")
    adv_list = adv_sub.add_parser("list", help="查看已注册的 GHSA 安全公告")
    adv_list.add_argument("--json", action="store_true")
    adv_list.set_defaults(handler=command_advisory)

    adv_export = adv_sub.add_parser("export", help="导出指定漏洞的标准化 GHSA 安全公告与 PR 修复草稿")
    adv_export.add_argument("--cve", default="CVE-2026-9001", help="CVE 编号 (默认 CVE-2026-9001)")
    adv_export.add_argument("--package", default="target-package", help="受影响软件包名称")
    adv_export.add_argument("--ecosystem", default="npm", help="软件包生态 (npm, Maven, PyPI, crates.io, Composer, etc.)")
    adv_export.add_argument("--category", default="generic_logic", help="漏洞威胁类别")
    adv_export.add_argument("--symbol", default="processInput", help="受影响函数或方法符号")
    adv_export.add_argument("--target", default="src/core.py", help="受影响源文件路径")
    adv_export.add_argument("--format", choices=("markdown", "json"), default="markdown", help="输出格式 (markdown, json)")
    adv_export.add_argument("--out", type=Path, help="保存文件路径")
    adv_export.set_defaults(handler=command_advisory)
    adv_parser.set_defaults(handler=command_advisory)

    # ── report：双轨制人性化安全报告生成器 (formal 商务正式报告, visual 高颜值技术报告) ──
    rep_parser = subparsers.add_parser("report", help="双轨制人性化安全审计报告生成器 (formal 商务正式, visual 高颜值交付)")
    rep_sub = rep_parser.add_subparsers(dest="report_type")

    report_source_help = (
        "报告数据来源：project=项目运行态 Supervisor 权威 findings（默认）；"
        "eval=--eval-dir 下的引擎自测收据；auto=优先项目运行态、不可用时退回收据"
    )

    def _add_report_source_args(p: Any) -> None:
        p.add_argument("--source", choices=("project", "eval", "auto"), default="project",
                       help=report_source_help)
        p.add_argument("--root", type=Path, help="项目根目录 (默认当前工作目录)")
        p.add_argument("--eval-dir", type=Path, help="审计收据目录 (默认 evals/oss)")
        p.add_argument("--out", type=Path, help="报告输出目录 (默认与 eval-dir 相同)")
        p.add_argument("--format", choices=("html", "md", "pdf", "both"), default="both", help="文件格式 (默认 both)")
        p.add_argument("--title", help="自定义报告主标题")
        p.add_argument("--json", action="store_true", help="以 JSON 输出报告元数据与生成清单")

    for sub_name in ("formal", "visual", "both"):
        p = rep_sub.add_parser(sub_name, help=f"生成 {sub_name} 格式安全报告")
        _add_report_source_args(p)
        p.set_defaults(handler=command_report)

    serve_parser = rep_sub.add_parser("serve", help="启动双轨报告本地实时 Web 预览服务")
    serve_parser.add_argument("--host", default="127.0.0.1", help="服务监听地址 (默认 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8080, help="服务监听端口 (默认 8080)")
    _add_report_source_args(serve_parser)
    serve_parser.add_argument("--once", action="store_true", help="单次请求后退出 (测试模式)")
    serve_parser.set_defaults(handler=command_report)

    _add_report_source_args(rep_parser)
    rep_parser.set_defaults(handler=command_report)

    completion.set_defaults(
        completion_commands=tuple(sorted(subparsers.choices)),
    )

    return parser


# 与引擎只读帮助契约共用一份枚举（定义在 common，CLI 不复制）。
_ENGINE_HELP_TOKENS = common.ENGINE_HELP_TOKENS


def _route_engine_help_tokens(tokens: list[str], engine_index: int) -> list[str]:
    """把 ``bugx engine`` 之后的首个帮助词改写成引擎自己的 ``help`` 子命令。

    ``engine`` 子解析器同时声明了 ``--root`` 与 ``argparse.REMAINDER``，并保留
    argparse 自动生成的 ``-h/--help``。当帮助词是 ``engine`` 之后的第一个参数时，
    自动 help 先于 REMAINDER 消费掉它，只打印外层 usage——而 ``bugx engine --help``
    正是我们在生命周期迁移提示里推荐的入口，引擎帮助（含 ``--dev``）不能丢。
    引擎对 ``help``/``-h``/``--help`` 三种写法都按只读处理，因此统一折叠成
    ``help``，既保住只读契约，也不依赖 REMAINDER 的边角行为。
    """
    index = engine_index + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--root":
            index += 2
        elif token.startswith("--root="):
            index += 1
        else:
            break
    if index >= len(tokens) or tokens[index] not in _ENGINE_HELP_TOKENS:
        return tokens
    rewritten = list(tokens)
    rewritten[index] = "help"
    return rewritten


def main(argv: list[str] | None = None) -> int:
    from . import common as _common_ns
    _common_ns._ACTIVE_TRACE_ID = None  # placeholder replaced below

    raw_tokens = list(sys.argv[1:] if argv is None else argv)
    first_cmd = None
    first_cmd_index = 0
    for index, token in enumerate(raw_tokens):
        if token.startswith("-"):
            continue
        first_cmd = token
        first_cmd_index = index
        break

    if first_cmd:
        lifecycle_map = {
            "pre": "bugx engine --root <项目路径> pre",
            "status": "bugx engine --root <项目路径> status",
            "workers": "bugx engine --root <项目路径> workers",
            "heartbeat": "bugx engine --root <项目路径> heartbeat",
            "post": "bugx engine --root <项目路径> post",
            "polling": "bugx engine --root <项目路径> polling",
            "pause": "bugx engine --root <项目路径> pause",
            "resume": "bugx engine --root <项目路径> resume",
            "abort": "bugx engine --root <项目路径> abort",
            "bootstrap-coverage": "bugx engine --root <项目路径> bootstrap-coverage --confirm",
        }
        if first_cmd in lifecycle_map:
            print(
                f"[bugx] ✗ 命令 '{first_cmd}' 是生命周期引擎子命令，不再作为顶层命令。\n"
                f"  生命周期控制已统一由全局引擎调度：\n"
                f"    - 执行子命令: {lifecycle_map[first_cmd]}\n"
                f"    - 或启动无人值守挖掘: bugx autonomous\n"
                f"  查看引擎支持的完整生命周期子命令: bugx engine --help",
                file=sys.stderr,
            )
            return 2

        removed_map = {
            "matrix": "项目级攻防渗透审计已统一收敛至: bugx audit",
            "console": "启动统一 Web 控制台与报告预览请使用: bugx report serve",
            "watch": "实时流式监视日志与 MCP 工具调用请使用: bugx log --watch",
            "test": "受影响测试选择器属于引擎核心开发工具，公共 CLI 已移除；开发者请使用: python3 scripts/test_selector.py",
        }
        if first_cmd in removed_map:
            print(
                f"[bugx] ✗ 命令 '{first_cmd}' 已废弃清理或收敛。\n"
                f"  推荐用法: {removed_map[first_cmd]}\n"
                f"  查看所有可用命令: bugx --help",
                file=sys.stderr,
            )
            return 2

    parser = build_parser()
    parse_tokens = argv
    if first_cmd == "engine":
        routed = _route_engine_help_tokens(raw_tokens, first_cmd_index)
        if routed is not raw_tokens:
            parse_tokens = routed
    args = parser.parse_args(parse_tokens)
    setattr(args, "_console_entrypoint", argv is None)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "help":
        return command_help(parser, args)
    root = Path(getattr(args, "root", None) or Path.cwd()).resolve()
    defer_logging = _defer_command_logging_until_authorized(args)
    logger = None if defer_logging else _configure_logging(
        root, getattr(args, "command", "unknown"), args.log_level,
    )
    if logger is not None:
        logging_module = _logging_module(root)
        _ACTIVE_TRACE_ID = str(logging_module.get_context(logger)["trace_id"])
        # 0.6.109：同步写 common 命名空间副本——模块 __setattr__ 桥不拦截
        # STORE_GLOBAL，拆分后 common._run_script 读不到 cli 侧副本
        # （DOC-034 复审 P1-1：trace 链路断裂）。
        common._ACTIVE_TRACE_ID = _ACTIVE_TRACE_ID
        logging_module.log_event(logger, "command_started", command=args.command, root=str(root))
    else:
        _ACTIVE_TRACE_ID = None
    try:
        read_only_version_check = (
            args.command == "update" and getattr(args, "check", False)
        )
        installation_only_diagnosis = (
            args.command == "doctor" and getattr(args, "installation_only", False)
        )
        if (args.command != "completion" and not read_only_version_check
                and not installation_only_diagnosis):
            # Centralize the installation-resource gate so command handlers
            # cannot swallow a missing asset error and accidentally skip the
            # one-shot CLI recovery path.
            # Installation-only doctor is the diagnostic escape hatch: it
            # reports broken assets itself and must remain runnable when the
            # central resource gate cannot import the engine.
            _ensure_core_importable()
        if args.command == "probe":
            probe_args = getattr(args, "probe_args", [])
            result = int(command_probe(args, probe_args))
        elif args.command == "audit":
            result = int(command_audit(args))
        else:
            result = int(args.handler(args))
        # reset may have moved the active diagnostic log into its archive;
        # do not append a completion event through the closed old handler.
        if logger is not None and args.command != "reset":
            logging_module.log_event(logger, "command_finished", command=args.command,
                                      exit_code=result)
        return result
    except MissingAgentAssetError as exc:
        if (argv is None and exc.repairable and not read_only_version_check
                and os.environ.get(_ASSET_REPAIR_ATTEMPT_ENV) != "1"):
            try:
                detail = repair_cli_agent_assets(channel="stable", root=root)
                print(f"[bugx] {detail}；正在重启原命令以加载新安装...", file=sys.stderr)
                restart_cli_after_asset_repair()
            except Exception as repair_exc:  # noqa: BLE001
                if logger is not None and args.command != "reset":
                    logging_module.log_event(
                        logger, "asset_repair_failed", str(repair_exc), level=40,
                        command=args.command, failure_kind=type(repair_exc).__name__,
                    )
                print(f"[bugx] ✗ {exc}", file=sys.stderr)
                print(f"[bugx] ✗ 安全自愈未完成：{repair_exc}", file=sys.stderr)
                print(
                    "  可在发布服务恢复后重试，或先安装经过校验的 bugx-cli wheel。",
                    file=sys.stderr,
                )
                return 2

        if logger is not None and args.command != "reset":
            logging_module.log_event(logger, "command_failed", str(exc),
                                      level=40, command=args.command,
                                      failure_kind="agent_assets_missing")
        print(f"[bugx] ✗ {exc}", file=sys.stderr)
        advice = _generate_ai_exception_advice(exc, getattr(args, "command", None))
        if advice:
            print(f"  💡 AI 解决建议: {advice}", file=sys.stderr)
        return 2
    except CliError as exc:
        from .infra.ai_advisor import RuntimeSelfHealInterceptor
        interceptor = RuntimeSelfHealInterceptor(root)
        if interceptor.can_heal(exc, command=args.command):
            print(f"[bugx] ⚠️  检测到命令 '{args.command}' 执行中断: {exc}", file=sys.stderr)
            heal_ok, heal_detail = interceptor.try_heal(exc, command=args.command)
            if heal_ok:
                print(f"  ✨ [AI 动态自愈] {heal_detail}，正在原位恢复执行...", file=sys.stderr)
                try:
                    if args.command == "probe":
                        result = int(command_probe(args, getattr(args, "probe_args", [])))
                    elif args.command == "audit":
                        result = int(command_audit(args))
                    else:
                        result = int(args.handler(args))
                    if logger is not None and args.command != "reset":
                        logging_module.log_event(logger, "command_recovered_and_finished",
                                                  command=args.command, exit_code=result)
                    print(f"  ✓ 命令 '{args.command}' 自愈后成功恢复并完成！", file=sys.stderr)
                    return result
                except Exception as retry_exc:
                    print(f"  ✗ 自愈后重试仍失败 (反自旋终止): {retry_exc}", file=sys.stderr)
                    return 2

        if logger is not None and args.command != "reset":
            logging_module.log_event(logger, "command_failed", str(exc),
                                      level=40, command=args.command)
        print(f"[bugx] ✗ {exc}", file=sys.stderr)
        advice = _generate_ai_exception_advice(exc, getattr(args, "command", None))
        if advice:
            print(f"  💡 AI 解决建议: {advice}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - exercised by integration failures.
        if logger is not None and args.command != "reset":
            logging_module.log_event(
                logger, "command_crashed", str(exc), level=40,
                command=args.command, exception_type=type(exc).__name__,
                exc_info=True,
            )
        err = f"[bugx] ✗ 未处理异常: {type(exc).__name__}: {exc}"
        try:
            import core.i18n as i18n
            err = i18n.exception_text("cli.unhandled", fallback=err, exc_type=type(exc).__name__, exc=exc)
        except Exception:
            pass
        print(err, file=sys.stderr)
        advice = _generate_ai_exception_advice(exc, getattr(args, "command", None))
        if advice:
            print(f"  💡 AI 解决建议: {advice}", file=sys.stderr)
        return 1
    finally:
        # Release the diagnostic log and its lock file once the command
        # finishes.  Windows refuses to delete a file with an open handle, so
        # leaving the handler attached breaks callers that clean the project
        # directory in-process (tests, embedded usage).  POSIX is unaffected;
        # a later configure() simply attaches a fresh handler.
        if logger is not None:
            for handler in list(logger.handlers):
                try:
                    handler.close()
                finally:
                    logger.removeHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())

_SUBMODULES = (
    common, plugin, lifecycle, agent, doctor, knowledge, cloud, system,
    stats, project, patch, skill, daemon, autonomous, goal, advisory, report,
)


class _CliModule(sys.modules[__name__].__class__):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if not name.startswith("__"):
            for mod in _SUBMODULES:
                if hasattr(mod, name):
                    setattr(mod, name, value)


sys.modules[__name__].__class__ = _CliModule
