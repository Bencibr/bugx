#!/usr/bin/env python3
"""core/hosts/codex.py - Codex 插件（DOC-239 S3：纵切，事实只在这里声明）。

事实来源（逐字搬入，不重新解释）：

* identity  ← ``host_registry.HOST_MARKERS["codex"]``（``.codex/config.toml`` 等）
* install   ← ``src/bugx_cli/hosts/registry.HOST_COMPONENTS`` +
              ``host_adapters.HOST_ADAPTERS``（``.agents/skills/bugx/SKILL.md``）
* engine    ← ``supervisor_host.CodexAdapter``（无头 ``codex exec <prompt>``；自治
              ``--approve-for-me`` + ``resume``/``--json``/``--enable multi_agent_v2``
              + ``-c mcp_servers.*`` 覆盖）
* mcp_write ← 项目级 ``.codex/config.toml`` 的 ``[mcp_servers.<name>]`` 表 +
              ``.bugx/state/host-mcp/codex.json`` 运行期回执。**Codex 只读 TOML**：
              ``codex-cli 0.163.0`` 实测 ``.codex/config.json`` 永不被加载（旧 JSON
              写入器产生的是死配置），因此本插件声明 ``config_format="toml"``，
              写入/清理/诊断三件由 :mod:`hosts.mcp_toml` 承担，CLI 通用 JSON 骨架
              不得接管。
* trust     ← ``$CODEX_HOME/config.toml`` 的 ``[projects."<绝对路径>"]``。官方文档
              明示 “Untrusted projects skip project-scoped ``.codex/`` layers,
              including project-local config”，即项目未受信任时上面写的 TOML 会被
              **整层跳过**。启动期自愈为 ``trust_level = "trusted"``；用户显式写
              ``untrusted`` 时 fail-closed，绝不按死配置继续跑。
* acp       ← **无 ACP 入口**：``codex-cli 0.163.0`` 的 ``--help`` 没有 ``acp``
              子命令、也没有 ``--acp`` flag（以当前宿主帮助输出为准）。
              该事实由 ``core/host_acp/codex.py`` 以 ``status = "unavailable"``
              **机读声明**，探针如实报 ``BLOCKED(adapter_status=unavailable)``——
              它不是「未迁移」，而是宿主产品本身没有该协议面；把它写进 TODO 会让
              审计把「等上游」误读成本仓拖欠。
              因此 Codex 的受支持通道是**无头 ``codex exec``**（``engine_run`` 面），
              调用契约按实测钉死：prompt 走 argv 时 stdin 必须关闭、会话 id 取
              ``thread.started.thread_id``、``resume`` 子命令不接受
              ``--approve-for-me`` / ``--add-dir``。

* git      ← **不归本插件负责，也不许本插件绕过**：项目根是 Git 仓库是 ``pre``
              前置门禁（``core/launch_phases/startup_gates.py::git_startup_gate``，
              状态取自 ``core/startup_environment.py::detect_git``：``missing`` →
              ``install_git``、``not-repository`` → ``git_init``、``unborn`` 等 →
              ``repair_git``；未通过直接 ``POLLING_DECISION_REQUIRED``，**不创建
              Session**）。契约合规的项目到这里一定已有 ``.git``（目录或 worktree
              gitfile），所以 argv **绝不**带 ``--skip-git-repo-check``：该 flag 既
              多余，又会顺手放开宿主自己的目录信任边界，把「前置门禁失效」伪装成
              「跑得通」。真缺 ``.git`` 就让 Codex 自己报
              ``Not inside a trusted directory ...``，由上游 ``git_environment``
              门禁修复，不做静默降级。

为什么 ``CodexAdapter`` 的 argv 逻辑必须搬进插件：它此前与 CLI 的
``HostComponent`` / ``HOST_ADAPTERS`` 各自声明同一批量（名字、入口、能力），
漏改一处就静默降级（DES-01 的成因）。纵切后引擎/CLI 通用层零 Codex 字面量。

仍然在插件外的 Codex 事实（``tests/test_host_plugins.py`` 的
``OUTSIDE_PLUGIN_CODEX`` 按文件逐一核对）：

* ``src/bugx_cli/commands/plugin.py`` / ``lifecycle.py``：ChatGPT 分发流程经
  ``codex plugin`` 命令注册 marketplace 并读 ``~/.codex/config.toml`` ——那是
  **chatgpt 宿主的安装实现**（S4 负责迁移），不是 Codex 宿主接线；
* ``src/bugx_cli/host_adapters.CodexAdapter``：只剩 Windows 安装位探测这一 CLI
  行为覆写（``%LOCALAPPDATA%\\OpenAI\\Codex`` 与 MSIX），声明字段全部来自本模块。
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any, Mapping

# 同包私有模块一律走相对导入：平铺绝对回退会触发架构棘轮的跨模块私有符号
# 泄露计数（private_leaks 棘轮基线 0），且 core.* 命名空间由引擎自举保证。
from . import _mcp_toml as mcp_toml
from .base import (
    AcpFacet,
    EngineRunFacet,
    HostPlugin,
    IdentityFacet,
    InstallFacet,
    McpWriteFacet,
    RunContext,
    STATUS_READY,
    WorkerRuntimeFacet,
    UsageFacet,
)

__all__ = ["PLUGIN", "bootstrap_mcp", "ensure_project_trust", "inspect_project", "remove_mcp"]

_NAME = "codex"

#: Codex 项目级配置目录（``identity.markers`` 的同源推导，不重复写路径）。
_CONFIG_DIR = ".codex"


def _config_paths(root: Path) -> tuple[Path, ...]:
    """Codex 唯一会被加载的项目级配置（``config.json`` 不是 Codex 的格式）。"""

    return (root / _CONFIG_DIR / "config.toml",)


def _agent_config_path(root: Path) -> Path:
    """``agent_config_path`` 的 Codex 口径：真实文件优先，其次目录本身。"""

    for candidate in _config_paths(root):
        if candidate.is_file():
            return candidate
    return root / _CONFIG_DIR


def _writable_state_dirs() -> list[Path]:
    """BugX 运行期必须可写的宿主外状态/缓存目录（``workspace-write`` 之外）。

    真机事实（``codex-cli 0.163.0``，``--approve-for-me`` 的 managed 权限档）：沙箱
    只允许写工作区，``$HOME`` 一律 ``EPERM``。未授权时会连锁失败：

    * ``~/.bugx/mcp-runtime/**`` 写不进 ⇒ 可选 MCP（如 semgrep）供给必然失败，而该
      条目是 critical，``pre`` 直接阻断（实测：``本轮启动已停止：semgrep``）；
    * ``~/.semgrep/semgrep.log`` / ``~/.cache/semgrep`` 写不进 ⇒ 即使扫描器已装好，
      ``semgrep --version`` 也直接抛 ``PermissionError``（实测原文）。

    因此只授权这些**运行期状态/缓存**子目录，**绝不**授权 ``~/.bugx`` 本身：
    ``~/.bugx/core`` 是引擎本体，授权写等于允许被审代码改写审计工具。
    目录不存在时不下发（不主动创建用户主目录里的新路径）。
    """

    home = Path.home()
    candidates = (
        home / ".bugx" / "mcp-runtime",
        home / ".bugx" / "logs",
        home / ".semgrep",
        home / ".cache" / "semgrep",
    )
    return [path for path in candidates if path.is_dir() and not path.is_symlink()]


def _state_grants() -> list[str]:
    """``--add-dir`` 参数对（运行期可写状态目录）。"""

    grants: list[str] = []
    for state_dir in _writable_state_dirs():
        grants += ["--add-dir", str(state_dir)]
    return grants


def _headless_argv(ctx: RunContext) -> list[str]:
    # Codex ``exec`` 没有 OpenCode 风格的 ``--agent``：``ctx.agent`` / ``ctx.session``
    # 被刻意忽略（会话续跑用 ``resume``，见 ``_autonomous_argv``）。
    #
    # 调用契约（0.163.0 实测，别按「看起来合理」改）：
    # * prompt 走 argv 时 stdin 里**不能有可读数据**：stdin 非 TTY 时 Codex 仍会打印
    #   ``Reading additional input from stdin...``（``stdin=DEVNULL`` 实测也照打，
    #   见 ``scripts/smoke_host_headless.py`` 的 ``informational.stdin_notice``），
    #   但它读到 EOF 就继续；真正的风险是把管道里的内容追加成 ``<stdin>`` 块污染
    #   prompt。因此判据是「事件流里没有 ``<stdin>`` 块 + Agent 文本未被污染」，
    #   而不是「stderr 没有这行提示」——后者永远做不到，把它当契约只会制造假阴性。
    # * **不加** ``--skip-git-repo-check``：Git 仓库是 ``pre`` 的 ``git_environment``
    #   门禁（``startup_gates.git_startup_gate`` → ``git_init``/``repair_git``），
    #   合规项目必然已有 ``.git``；在这里放行等于绕过宿主目录信任边界，掩盖前置
    #   门禁失效（见模块 docstring 的 ``git`` 面）。
    # * **必须加** ``--approve-for-me``（2026-10-24 真机反证，0.163.0）：不带该 flag 时
    #   ``codex exec`` 的 approval policy 是 ``never``，**任何** MCP 工具调用都被拒绝——
    #   事件流里 ``item.started``/``item.completed`` 的 ``mcp_tool_call`` 依次出现
    #   ``{"server":"bugx-supervisor","tool":"bugx_supervisor_status"}`` 与
    #   ``{"status":"failed","error":{"message":"MCP tool call requires approval, but
    #   approval policy is never"}}``；即「宿主发现了项目级 MCP 与工具名，却调用不了」。
    #   这种形态极易被误读成「MCP 没加载」或「模型不听话」，实际是 approval 通路缺失。
    #   带上之后同一命令返回 ``status=completed`` +
    #   ``structured_content={"status":"NO_RUN","root":"<项目根>"}``，证明项目级
    #   ``.codex/config.toml`` 的 MCP 表真的被加载并按项目根服务（反例实验见
    #   ``scripts/smoke_host_headless.py --mode mcp``）。
    #   ``--approve-for-me`` 的官方语义是「approval 请求走自动复核，沙箱固定
    #   workspace-write」（``codex exec --help``），与 ``_autonomous_argv`` 保持同一
    #   approval 语义：无头通道没有人类可以点「允许」，缺它等于让每个 Worker 的
    #   控制面调用恒失败。
    return [
        "codex", "exec", "--approve-for-me",
        *_state_grants(),
        ctx.prompt,
    ]


_SCOUT_FEATURE_OPTIONS = (
    "-c", "features.plugins=false", "-c", "features.apps=false",
    "-c", "features.multi_agent=false",
)


def _scout_inventory_command(root: Path) -> tuple[list[str], str]:
    """声明侦察前要执行的清单查询：``(argv, 结果键)``，由引擎执行。

    插件只声明命令与结果位置，不在本模块启动子进程——``subprocess`` 属于被
    依赖方向门禁禁止的模块（插件事实只能单向流出，不得自己驱动外部进程）。
    """
    _ = root
    # 实测：`codex mcp list --json` 返回**顶级数组**，不是 {"servers": [...]}。
    # 结果键留空表示「整个负载就是清单」，避免把包装键写死成错误事实。
    return ["codex", "mcp", "list", "--json", *_SCOUT_FEATURE_OPTIONS], ""


def _disable_mcp_options(servers: object) -> list[str]:
    """把 CLI 的有效 MCP 清单转成 ``-c mcp_servers.<name>.enabled=false``。

    纯函数：输入是引擎读回的 JSON 结果，不接触进程、文件或全局配置，因此
    可以独立断言「哪些服务器会被关掉」。名字无法用 CLI 键表达时显式告警并
    跳过，绝不猜测或吞掉。
    """
    import re
    import warnings

    if not isinstance(servers, list):
        raise ValueError("Codex MCP inventory must be an array")
    options: list[str] = []
    for server in servers:
        name = server.get("name") if isinstance(server, dict) else None
        if not isinstance(name, str) or not server.get("enabled"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            warnings.warn(f"Scout retained MCP with unaddressable CLI key: {name!r}", RuntimeWarning)
            continue
        options.extend(["-c", f"mcp_servers.{name}.enabled=false"])
    return options


def recon_scout_command(
    root: Path, prompt: str, inventory: object | None = None,
) -> list[str]:
    """Context-only Scout; no global config writes, model/effort remain intact.

    ``inventory`` 由引擎按 ``_scout_inventory_command`` 读取后传入；未提供时
    （例如清单查询失败）不追加禁用项，侦察仍在 read-only 沙箱内执行。
    """
    return [
        "codex", "exec", "--json", "--ephemeral", "--sandbox", "read-only",
        *_SCOUT_FEATURE_OPTIONS, *_disable_mcp_options(inventory), "-C", str(root),
        ("This is a semantic-review subroutine called by the active audit Agent, not a request to launch that Agent. "
         "The caller owns every user gate, lifecycle transition and Worker dispatch. "
         "Perform the original semantic-review task below without recursively starting the parent workflow or loading its full manuals. "
         "You may inspect relevant source read-only when needed; report missing evidence as unverified. "
         "Return the requested JSON object only.\n\n" + prompt),
    ]


def _autonomous_mcp_options() -> list[str]:
    """BugX 内置 MCP 的命令行覆盖（子 Agent 也要能拿到，故同时写项目配置）。"""

    options: list[str] = []
    for name, role in (
        ("bugx-supervisor", "host"),
        ("bugx-supervisor-worker", "worker"),
    ):
        prefix = f"mcp_servers.{name}"
        options.extend([
            "-c", f'{prefix}.command="bugx"',
            "-c", f'{prefix}.args=["mcp","serve","--root",".","--role","{role}"]',
        ])
    options.extend([
        "-c", 'mcp_servers.bugx-docs.command="bugx"',
        "-c", 'mcp_servers.bugx-docs.args=["mcp","docs","serve","--root",".","--db",".bugx/knowledge/docs.sqlite3"]',
    ])
    return options


def _git_metadata_dirs(root: Path) -> list[Path]:
    """本仓库需要授权给 Codex 的 Git 元数据目录（普通克隆 1 个，worktree/submodule 2 个）。

    ``.git`` 是目录 → 就是它自己；``.git`` 是 **gitfile**（``gitdir: <path>``，``git
    worktree`` 与 submodule 都这样）→ 授权 ``gitdir`` **和**它的 ``commondir``：worktree
    私有目录只有 ``HEAD``/``index``，``objects``/``refs``/``config`` 落在共同目录里，
    只授权前者仍然写不进对象库。

    无法解析的 gitfile（缺指针 / 指针指向已清理的目录）返回空列表，**不在 argv 层抛错**：
    ``startup_environment.detect_git`` 对这两种形态实测都是 ``not-repository`` +
    ``ready=False``，``pre`` 的 ``git_environment`` 门禁会先阻断（被动修复或中断），
    argv 只是投影，不该变成第二套门禁——build 191 的教训就是插件别替宿主门禁做决定。
    完全没有 ``.git`` 同理属门禁职责。
    """

    git_path = Path(root).expanduser().resolve() / ".git"
    if git_path.is_dir() and not git_path.is_symlink():
        return [git_path]
    if not git_path.is_file():
        # 符号链接的 ``.git`` 也走这里：不跟随，避免把沙箱授权扩到仓库外的任意路径。
        return []
    try:
        text = git_path.read_text(encoding="utf-8")
    except OSError:
        return []
    pointer = next(
        (line.strip() for line in text.splitlines() if line.strip().startswith("gitdir:")),
        "",
    )
    raw = pointer[len("gitdir:"):].strip()
    if not raw:
        return []
    gitdir = Path(raw).expanduser()
    if not gitdir.is_absolute():
        gitdir = git_path.parent / gitdir
    gitdir = gitdir.resolve()
    if not gitdir.is_dir():
        return []
    dirs = [gitdir]
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file():
        try:
            raw_common = commondir_file.read_text(encoding="utf-8").strip()
        except OSError:
            raw_common = ""
        if raw_common:
            common = Path(raw_common).expanduser()
            if not common.is_absolute():
                common = gitdir / common
            common = common.resolve()
            if common != gitdir and common.is_dir():
                dirs.append(common)
    return dirs


def _autonomous_argv(ctx: RunContext) -> list[str]:
    # 不加 ``--skip-git-repo-check``：Git 仓库由 ``pre`` 的 ``git_environment`` 门禁
    # 保证（见 ``_headless_argv`` 注释与模块 docstring 的 ``git`` 面）。
    command = ["codex", "exec", "--approve-for-me"]
    # --approve-for-me keeps workspace-write; grant only this clone's Git metadata so
    # its startup write probe and local commits work. worktree/submodule 的 ``.git``
    # 是 gitfile，需要同时授权 gitdir 与 commondir（见 ``_git_metadata_dirs``）。
    for metadata_dir in _git_metadata_dirs(Path(ctx.root)):
        command += ["--add-dir", str(metadata_dir)]
    # 运行期状态/缓存目录同样必须在 resume 之前下发（同上：resume 不接受 --add-dir）。
    command += _state_grants()
    if ctx.session:
        # ``resume`` 是 ``exec`` 的子命令，且**只**接受自己那套选项：实测
        # ``codex exec resume --help`` 没有 ``--approve-for-me`` / ``--add-dir``，
        # 因此这两个 flag 必须放在子命令**之前**（exec 级），放进 resume 之后会
        # 变成「未知参数」直接退出。
        command += ["resume", ctx.session]
    command += [
        "--json", "--enable", "multi_agent_v2",
        *_autonomous_mcp_options(), ctx.prompt,
    ]
    return command


def _mcp_health(name: str, cfg: dict, root: Path | None = None) -> bool:
    del name, cfg, root  # bugx-supervisor 是宿主加载的 stdio MCP，恒健康
    return True


#: BugX 受管条目的命名/命令前缀：既是冲突判定（能否覆盖）的所有权依据，也是
#: 卸载清理依据。
_BUGX_NAME_PREFIX = "bugx"

#: 追加表块前的横幅：用户据此识别哪一段由安装/启动流程维护。
_MCP_BANNER = "# BugX-managed MCP servers; generated for autonomous child Agents."

#: 旧实现把 MCP 写进 ``.codex/config.json``（Codex 从不加载该文件）；只有在
#: 「整份内容都是 BugX 条目」时清扫，用户自写内容一律保留。
_LEGACY_JSON = "config.json"


def _codex_home() -> Path:
    """``$CODEX_HOME`` 或 ``~/.codex``：Codex 用户级配置根（trust 与全局配置同处）。"""

    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    return Path.home() / _CONFIG_DIR


def _is_bugx_entry(name: str, spec: Mapping[str, Any]) -> bool:
    """条目是否由 BugX 维护：只看 command/args 是否指向 ``bugx``。

    刻意**不看名字**：``bugx-supervisor`` 这个名字虽由本工具保留，但若用户把它
    指向别的程序，那属于用户配置，必须冲突可见（拒绝覆盖），而不是被安装流程
    当成「旧版 BugX 条目」静默改写。
    """

    del name
    tokens = [str(spec.get("command") or "")]
    tokens.extend(str(arg) for arg in (spec.get("args") or ()))
    return any(_BUGX_NAME_PREFIX in token.lower() for token in tokens)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def _strip_banner(text: str) -> str:
    """去掉孤立的受管横幅注释（条目清空后它不再指向任何表块）。"""

    lines = [line for line in text.splitlines() if line.strip() != _MCP_BANNER]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _backup_config(root: Path, target: Path, suffix: str) -> Path | None:
    """备份到项目 ``.bugx/backups/host-mcp/``（0600）；非普通文件不备份。"""

    if not target.is_file() or target.is_symlink():
        return None
    backup_dir = root / ".bugx" / "backups" / "host-mcp"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"codex-{time.time_ns()}{suffix}"
    backup.write_bytes(target.read_bytes())
    backup.chmod(0o600)
    return backup


def _trust_state(root: Path) -> str:
    """只读判定项目信任态：``trusted`` / ``untrusted`` / ``absent`` / ``unknown``。"""

    home = _codex_home()
    target = home / "config.toml"
    if not home.is_dir():
        return "absent"
    try:
        parsed = tomllib.loads(_read_text(target))
    except (ValueError, UnicodeError):
        return "unknown"
    projects = parsed.get("projects")
    entry = projects.get(str(root)) if isinstance(projects, dict) else None
    if isinstance(entry, dict) and entry.get("trust_level") == "trusted":
        return "trusted"
    return "untrusted"


def ensure_project_trust(root: Path) -> dict[str, object]:
    """确保项目在 ``$CODEX_HOME/config.toml`` 中受信任。

    官方文档：*Untrusted projects skip project-scoped ``.codex/`` layers,
    including project-local config*。即项目未受信任时，本插件写的
    ``[mcp_servers.*]`` 会被整层跳过——原生子 Agent 拿不到 MCP，且没有任何报错。

    * 已 trusted → ``unchanged``（零写入）；
    * 无条目 → 追加 ``[projects."<绝对路径>"] trust_level = "trusted"``；
    * 用户显式写 ``untrusted`` → **fail-closed** 抛错，绝不静默降级；
    * 写入后**复读校验**：磁盘态若不是 ``trusted``（例如另一进程并发改写覆盖了
      本次写入），同样 fail-closed——信任丢失时 Codex 会整层跳过项目级
      ``.codex/`` 配置且**不报错**，静默继续等于让子 Agent 无 MCP 裸跑；
    * 本机无 ``$CODEX_HOME`` 目录 → ``absent``（宿主未安装，不凭空创建用户目录）。

    ``root`` 入口即归一化为**解析后的绝对路径**：Codex 用进程 ``cwd``（已解析，
    macOS 上是 ``/private/...``）作为 ``[projects."<path>"]`` 的键。传入未解析路径
    （``/tmp/...``、相对路径）会**并为同一项目写第二个条目**，真正的项目仍是
    ``untrusted``，且与上面所有判定共用同一个错键 ⇒ 全程「看起来成功」。
    """

    root = Path(root).resolve()
    import core.secure_atomic as secure_atomic

    home = _codex_home()
    target = home / "config.toml"
    if not home.is_dir():
        return {"status": "absent", "path": str(target), "created": False}
    if target.is_symlink():
        raise OSError(f"拒绝通过符号链接写入 Codex 用户级配置: {target}")
    text = _read_text(target)
    state = _trust_state(root)
    if state == "trusted":
        return {"status": "unchanged", "path": str(target), "created": False}
    if state != "absent" and f'[projects."{root}"]' in text:
        raise OSError(
            f"{root} 已在 {target} 中被显式标记为非 trusted，"
            "Codex 会整层跳过项目级 .codex/ 配置（含 BugX MCP）。"
            '如需自治运行，请把该项目改为 trust_level = "trusted"，或改用其它宿主。'
        )
    # 归属事实必须在写入前采样：卸载时只有「本工具新增的条目」才允许回收，
    # 用户自己加过的信任条目属于用户配置（见 ``_forget_project_trust``）。
    created = not _trust_entry_present(root)
    try:
        new_text, status = mcp_toml.set_table_key(
            text, ("projects", str(root)), "trust_level", '"trusted"',
        )
    except ValueError as exc:
        raise OSError(f"Codex 用户级配置无法登记项目信任（{target}）: {exc}") from exc
    if status == "unchanged":
        return {"status": "unchanged", "path": str(target), "created": False}
    backup = _backup_config(root, target, ".toml")
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    secure_atomic.atomic_write_text(target, new_text, mode=mode, parent_mode=0o700)
    # 写后复读：原子替换只保证"不会读到半截文件"，不保证"本次写入仍是最新一次"。
    # 并发改写（另一个 BugX 运行 / 用户编辑器保存）会让信任丢失，而 Codex 对未受
    # 信任项目是**静默跳过**项目级配置——此时继续跑等于 Worker 无 MCP 裸跑。
    verified = _trust_state(root)
    if verified != "trusted":
        raise OSError(
            f"Codex 用户级信任写入后未生效（{target} 复读为 {verified}）："
            "可能有其它进程并发改写该文件。未受信任的项目会被 Codex 整层跳过项目级 "
            ".codex/ 配置（含 BugX MCP）且不报错，拒绝按降级态继续。"
        )
    return {
        "status": "trusted",
        "path": str(target),
        "backup": str(backup) if backup else None,
        "created": created,
    }


#: 全局 MCP 条目（用户级 ``$CODEX_HOME/config.toml``）：``--root .`` 相对绑定
#: ——Codex 以项目 cwd 派发 stdio MCP，任意项目内启动即绑定该项目；引擎侧
#: root 自愈（``_resolve_serve_root``）兜底 cwd 非 BugX 项目的场景（结构化
#: INITIALIZATION_REQUIRED，提示按需安装）。``bugx-docs`` 的 ``--db`` 同理按
#: 项目相对路径解析。
_GLOBAL_MCP_SERVERS: dict[str, dict[str, object]] = {
    "bugx-supervisor": {
        "command": "bugx",
        "args": ["mcp", "serve", "--root", ".", "--role", "host"],
    },
    "bugx-supervisor-worker": {
        "command": "bugx",
        "args": ["mcp", "serve", "--root", ".", "--role", "worker"],
    },
    "bugx-docs": {
        "command": "bugx",
        "args": [
            "mcp", "docs", "serve", "--root", ".",
            "--db", ".bugx/knowledge/docs.sqlite3",
        ],
    },
}


def ensure_global_mcp(root: Path) -> dict[str, object]:
    """把 BugX MCP 三通道安装到**用户级** ``$CODEX_HOME/config.toml``。

    与项目级 ``bootstrap_mcp`` 互补：项目级让 Worker 子代理稳定拿到 MCP
    （含 trust 语义），全局级让宿主在**任意目录**都能直接审计当前项目，
    无需每项目跑一次 ``bugx install --agent codex``。两者同名同定义时
    Codex 按就近覆盖合并，互不冲突；项目未安装时全局条目是唯一来源。

    纪律与 :func:`ensure_project_trust` 同源：备份（0600）、用户自有同名
    条目拒绝覆盖（``_is_bugx_entry`` 归属判定）、原子写入 + 写后复读校验、
    幂等（二跑 unchanged）。**绝不触碰** ``[projects]`` 信任表——全局 MCP
    不依赖项目信任语义，也绝不静默扩大信任授权。
    """
    import core.secure_atomic as secure_atomic

    home = _codex_home()
    if not home.is_dir():
        raise OSError(
            f"未找到 Codex 用户级配置目录（{home}）：宿主未安装，"
            "拒绝凭空创建用户目录。请先安装 Codex CLI 后重试。"
        )
    target = home / "config.toml"
    if target.is_symlink():
        raise OSError(f"拒绝通过符号链接写入 Codex 全局 MCP 配置: {target}")
    text = _read_text(target)
    try:
        new_text, added, updated = mcp_toml.upsert_servers(
            text, _GLOBAL_MCP_SERVERS, owned=_is_bugx_entry, banner=_MCP_BANNER,
        )
    except ValueError as exc:
        raise OSError(f"Codex 全局 MCP 配置写入被拒绝（{target}）: {exc}") from exc
    changed = bool(added or updated)
    backup: Path | None = None
    if changed:
        backup = _backup_config(root, target, ".toml")
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
        secure_atomic.atomic_write_text(target, new_text, mode=mode, parent_mode=0o700)
    # 写后复读：与信任写入同一防线——并发改写会让本次条目丢失且无报错。
    final = mcp_toml.read_servers(_read_text(target))
    missing = [n for n in _GLOBAL_MCP_SERVERS if n not in final]
    if missing:
        raise OSError(
            f"Codex 全局 MCP 写入后未生效（{target} 复读缺失: {', '.join(missing)}）："
            "可能有其它进程并发改写该文件，拒绝按降级态继续。"
        )
    return {
        "status": "unchanged" if not changed else "written",
        "path": str(target),
        "added": added,
        "updated": updated,
        "backup": str(backup) if backup else None,
    }


def _trust_entry_present(root: Path) -> bool:
    """``$CODEX_HOME/config.toml`` 里是否**已存在** ``[projects."<root>"]`` 条目。

    与 :func:`_trust_state` 的分工：后者回答「现在是不是 trusted」，本函数回答
    「这个条目是不是本工具新增的」。卸载时只有本工具新增的条目才允许回收——用户
    自己写过的条目（哪怕值同为 ``trusted``）属于用户配置。
    """

    home = _codex_home()
    try:
        parsed = tomllib.loads(_read_text(home / "config.toml"))
    except (OSError, ValueError):
        return False
    projects = parsed.get("projects")
    return isinstance(projects, dict) and str(root) in projects


def _forget_project_trust(root: Path, receipt_path: Path) -> dict[str, object]:
    """回收**本工具新增**的用户级信任条目（卸载闭环的一部分）。

    为什么必须回收：``[projects."<绝对路径>"] trust_level = "trusted"`` 是一条**永久**
    信任授权。项目被删除或临时目录被重建（``/tmp`` 复用）后，Codex 仍会静默加载该
    路径下的项目级 ``.codex/config.toml``（其中可声明任意 MCP 命令），等于把「谁先
    在那个路径写文件」升级成「谁能让宿主执行命令」。安装期写了用户级配置，卸载期就
    必须物理回收，否则「安装成功」自带一条越权后门。

    三条同时满足才删（任一条不满足都保留并说明原因，不猜归属）：

    1. 运行期回执 ``.bugx/state/host-mcp/codex.json`` 记录 ``trust.created == true``；
    2. 当前信任态仍是 ``trusted``（用户改成 ``untrusted`` 是显式表达，不得抹除）；
    3. 切除后 ``tomllib`` 复核通过且复读不再是 ``trusted``；成功即删回执，避免陈旧
       回执二次触发删除。
    """

    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"status": "preserved", "reason": "无运行期回执：无法证明信任条目归属"}
    trust = payload.get("trust") if isinstance(payload, dict) else None
    if not isinstance(trust, dict) or not trust.get("created"):
        return {"status": "preserved", "reason": "信任条目不是本工具新增（用户原有配置）"}
    if _trust_state(root) != "trusted":
        return {"status": "preserved", "reason": "当前信任态非 trusted（用户显式表达），不回收"}

    import core.secure_atomic as secure_atomic

    target = _codex_home() / "config.toml"
    if target.is_symlink():
        return {"status": "preserved", "reason": f"{target} 是符号链接，拒绝改写"}
    try:
        new_text, removed = mcp_toml.remove_table(
            _read_text(target), ("projects", str(root)),
        )
    except (OSError, ValueError) as exc:
        return {"status": "preserved", "reason": f"用户级配置无法解析：{exc}"}
    if not removed:
        return {"status": "preserved", "reason": "未找到可切除的信任表块"}
    backup = _backup_config(root, target, ".trust.toml")
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    secure_atomic.atomic_write_text(target, new_text, mode=mode, parent_mode=0o700)
    if _trust_state(root) == "trusted":
        return {"status": "failed", "reason": "切除后复读仍为 trusted（疑似并发改写）"}
    try:
        receipt_path.unlink()
    except OSError:
        pass
    return {
        "status": "removed",
        "path": str(target),
        "backup": str(backup) if backup else None,
    }


def _sweep_legacy_json(root: Path) -> list[str]:
    """清扫旧的 ``.codex/config.json`` 死配置（Codex 从不加载它）。

    仅在「整份内容都是 BugX 条目」时删除；任何用户自写字段/条目都保留原文件，
    不猜归属。
    """

    path = root / _CONFIG_DIR / _LEGACY_JSON
    if path.is_symlink() or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return []
    if not isinstance(payload, dict) or set(payload) - {"mcp_servers"}:
        return []
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict) or not servers:
        return []
    if any(
        not isinstance(spec, dict) or not _is_bugx_entry(name, spec)
        for name, spec in servers.items()
    ):
        return []
    try:
        path.unlink()
    except OSError:
        return []
    return [f"{_CONFIG_DIR}/{_LEGACY_JSON}"]


def bootstrap_mcp(root: Path, servers: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    """安装 BugX 固定的 MCP 条目（项目作用域，绝不改用户全局 MCP 表）。

    三层缺一不可：

    * ``$CODEX_HOME/config.toml`` 的 ``[projects."<root>"]``：**项目未受信任时
      项目级配置整层被跳过**（见 :func:`ensure_project_trust`）；
    * ``.codex/config.toml``：**原生子 Agent 不继承父进程的 ``-c`` 覆盖**，项目级
      配置才让 Worker 拿到 MCP；BugX 自有条目按最新定义原地更新，用户自有条目
      冲突时拒绝覆盖；
    * ``.bugx/state/host-mcp/codex.json``：运行期回执，供门禁复核「本次启动到底
      写了什么」，不被宿主读取。

    备份落 ``.bugx/backups/host-mcp/``（0600），写入走 ``secure_atomic``（同目录
    临时文件 + ``os.replace``）；解析失败/冲突一律抛错，不静默降级。
    """

    import core.secure_atomic as secure_atomic

    root = Path(root).resolve()
    config_target = root / _CONFIG_DIR / "config.toml"
    if config_target.is_symlink():
        raise OSError(f"拒绝通过符号链接写入 Codex MCP 配置: {config_target}")
    trust = ensure_project_trust(root)
    config_text = _read_text(config_target)
    try:
        new_text, added, updated = mcp_toml.upsert_servers(
            config_text, servers, owned=_is_bugx_entry, banner=_MCP_BANNER,
        )
    except ValueError as exc:
        raise OSError(f"Codex MCP 配置写入被拒绝（{config_target}）: {exc}") from exc
    config_changed = bool(added or updated)
    config_backup: Path | None = None
    created = not config_target.exists()
    if config_changed:
        config_backup = _backup_config(root, config_target, ".toml")
        mode = config_target.stat().st_mode & 0o777 if config_target.exists() else 0o600
        secure_atomic.atomic_write_text(
            config_target, new_text, mode=mode, parent_mode=0o700,
        )
    legacy_removed = _sweep_legacy_json(root)
    # 回执记录**生效态**（而不是本次是否发生写入），否则二跑会因「首次写了信任、
    # 二次是 unchanged」而误报 changed，幂等断言形同虚设。
    effective_trust = _trust_state(root)

    receipt = root / ".bugx" / "state" / "host-mcp" / "codex.json"
    try:
        current: object = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        current = None
    # 归属证据必须**跨跑稳定**：首次写入时 created=True，二跑拿到的是 unchanged，
    # 若照抄本次结果就会把归属洗成 False（卸载随即拒绝回收，信任条目永久残留）。
    previous_created = False
    if isinstance(current, dict):
        previous_trust = current.get("trust")
        previous_created = bool(
            isinstance(previous_trust, dict) and previous_trust.get("created")
        )
    trust_created = bool(trust.get("created")) or (
        previous_created and effective_trust == "trusted"
    )
    payload = {
        "mcpServers": {name: dict(spec) for name, spec in servers.items()},
        "scope": "project",
        "managed": True,
        # 只记**稳定事实**（路径 + 条目名）。本次的新增/更新属于一次性事件，写进
        # 回执会让「二跑零写入」永远为假。
        "config": {"path": str(config_target), "servers": sorted(servers)},
        # ``created`` 是**归属证据**：卸载时只有它记录的条目才允许回收（见
        # ``_forget_project_trust``）。它是稳定事实（一次写入后不再变化），不会
        # 破坏「二跑零写入」。
        "trust": {
            "status": effective_trust,
            "path": trust["path"],
            "created": trust_created,
        },
        "legacy_removed": legacy_removed,
    }
    changed = current != payload
    if changed:
        secure_atomic.atomic_write_json(receipt, payload, mode=0o600)
    return {
        "host": _NAME,
        "path": str(receipt),
        "changed": changed,
        "backup": str(config_backup) if config_backup else None,
        "config_path": str(config_target),
        "config_changed": config_changed,
        "created": created,
        "trust": trust["status"],
        "trust_changed": trust["status"] == "trusted",
        "trust_created": trust_created,
        "trust_path": trust["path"],
        "legacy_removed": legacy_removed,
    }


def remove_mcp(root: Path) -> dict[str, object]:
    """卸载清理：项目 ``.codex/config.toml`` 条目 + **本工具新增**的用户级信任条目。

    项目文件只由表块和横幅组成时整文件删除（避免留下空壳 ``.codex/config.toml``）；
    仍有用户配置时按表块精准切除，绝不重排用户内容。用户级信任条目按
    :func:`_forget_project_trust` 的归属规则回收——只删本工具新增的那条，用户原有
    偏好与显式 ``untrusted`` 一律保留（原因写进 ``preserved``，不静默跳过）。
    """

    root = Path(root).resolve()
    target = root / _CONFIG_DIR / "config.toml"
    receipt_path = root / ".bugx" / "state" / "host-mcp" / "codex.json"
    removed: list[str] = []
    preserved: list[str] = []
    rel = f"{_CONFIG_DIR}/config.toml"
    if target.is_symlink() or not target.is_file():
        preserved.append(f"{rel}（不存在或为符号链接：无项目级条目可清理）")
    else:
        text = _read_text(target)
        names: list[str] = []
        new_text = text
        try:
            new_text, names = mcp_toml.remove_servers(text, _is_bugx_entry)
        except (ValueError, UnicodeError) as exc:
            preserved.append(f"{rel}（无法解析：{exc}）")
        if not names:
            preserved.append(f"{rel}（未发现 BugX 受管条目）")
        else:
            # 横幅只服务于「下面还有受管表块」；条目清空后它成了孤儿注释，一并清掉。
            new_text = _strip_banner(new_text)
            remaining = mcp_toml.validate(new_text) if new_text.strip() else {}
            if not remaining:
                try:
                    target.unlink()
                except OSError as exc:
                    preserved.append(f"{rel}（删除失败：{exc}）")
                else:
                    removed.append(f"{rel}（已安全完全删除）")
            else:
                try:
                    target.write_text(new_text, encoding="utf-8")
                except OSError as exc:
                    preserved.append(f"{rel}（写入失败：{exc}）")
                else:
                    removed.append(f"{rel}（移除 BugX MCP 服务器：{', '.join(sorted(names))}）")

    trust = _forget_project_trust(root, receipt_path)
    if trust["status"] == "removed":
        removed.append(f"{trust['path']}（回收本工具新增的项目信任条目：{root}）")
    elif trust["status"] == "failed":
        preserved.append(f"Codex 用户级信任条目未回收：{trust['reason']}")
    return {"removed": removed, "preserved": preserved, "trust": trust}


def inspect_project(root: Path, expected: tuple[str, ...] = ()) -> dict[str, object]:
    """只读诊断：项目 TOML 里的 BugX 条目 + 信任态（供 CLI ``validate_install``）。"""

    root = Path(root).resolve()
    target = root / _CONFIG_DIR / "config.toml"
    trust = _trust_state(root)
    try:
        servers = mcp_toml.read_servers(_read_text(target))
    except (ValueError, UnicodeError) as exc:
        return {
            "path": str(target),
            "error": str(exc),
            "servers": [],
            "missing": list(expected),
            "trust": trust,
        }
    present = [name for name, spec in servers.items() if _is_bugx_entry(name, spec)]
    return {
        "path": str(target),
        "error": None,
        "servers": present,
        "missing": [name for name in expected if name not in servers],
        "trust": trust,
        "trust_path": str(_codex_home() / "config.toml"),
    }


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="Codex",
        # 顺序即 ``host_registry.SUPPORTED_HOSTS`` 的展示顺序（opencode/claude/codex）。
        order=2,
        executable=_NAME,
        markers=(
            ".codex/config.toml",
            ".agents/skills/bugx/SKILL.md",
            _CONFIG_DIR,
        ),
        aliases=(),
        app_markers=None,
        native_question=True,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="skill",
        # DOC-169：Codex 官方项目发现路径是 $CWD/.agents/skills。
        path=".agents/skills/bugx/SKILL.md",
        marker=_CONFIG_DIR,
        # 旧 .codex/skills 为遗留清扫面：模板一致才删，用户改动保留。
        legacy_paths=(".codex/skills/bugx/SKILL.md",),
        capabilities=frozenset({"native-mcp-config"}),
        interaction={
            "tool": "request_user_input（Codex 交互宿主原生控件）",
            "single_select": "questions 仅 1 个；原生控件单选；按 question id + 精确 label 映射稳定 value",
            "availability": "通常为 Plan；Default 需显式启用对应实验特性且工具出现在当前工具列表",
            "fallback": "BUGX_INTERACTION_REQUEST → Codex/ACP 外层；无承接者则阻断",
            "failure": "控件失败时重问当前 gate；无控件且无 TTY 时阻断",
        },
        worker_subagent=False,
        # 原 ``common._SWEEPABLE_SKILL_ADAPTERS`` 成员：入口按 Skill 语义整体清扫。
        sweepable=True,
    ),
    engine_run=EngineRunFacet(
        supports_headless=True,
        headless_argv=_headless_argv,
        autonomous_argv=_autonomous_argv,
        mcp_config_paths=_config_paths,
        agent_config_path=_agent_config_path,
        mcp_health=_mcp_health,
        host_surface="tui",
        # Recon Scout：一次性只读语义复核（见 ``recon_scout_command``）。
        recon_scout_argv=lambda root, prompt_text, _prompt_file, inventory=None: (
            recon_scout_command(root, prompt_text, inventory)
        ),
        # 侦察前先向 CLI 查有效 MCP 清单，逐个 -c enabled=false；执行留在引擎侧
        # （插件不得 import subprocess），这里只声明命令与结果键。
        recon_scout_inventory=_scout_inventory_command,
        # Codex 有原生 ``collaboration.spawn_agent``（multi_agent_v2）：Worker 由
        # 宿主派发，禁止 ``workers --execute`` 进程池代跑。
        native_task=True,
    ),
    worker_runtime=WorkerRuntimeFacet(
        mode="multi",
        max_concurrency=None,
        requires_process_mode=False,
        sandbox_sync=(),
    ),
    mcp_write=McpWriteFacet(
        container_key="mcp_servers",
        writer_key=_NAME,
        # Codex 只加载 ``.codex/config.toml``（0.163 实测 ``config.json`` 永不被读）。
        project_relpath=".codex/config.toml",
        # 全局作用域：``$CODEX_HOME/config.toml``（ensure_global_mcp，相对
        # ``--root .`` 绑定项目 cwd；engine root 自愈兜底）。
        global_scope="~/.codex/config.toml",
        mirror_relpaths=(),
        requires_absolutize=True,
        config_format="toml",
        bootstrap=bootstrap_mcp,
        remove=remove_mcp,
        inspect=inspect_project,
    ),
    acp=AcpFacet(plugin=_NAME),
    usage=UsageFacet(
        plugin=_NAME,
        module="_codex_usage",
        state_filename="codex-usage.json",
        notes="Read-only local thread index and identity-filtered JSONL usage; unknown values remain null.",
    ),
)

