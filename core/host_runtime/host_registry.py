#!/usr/bin/env python3
"""core/host_registry.py - 宿主身份的唯一来源（单一真源）。

历史问题（DES-01）：同一「宿主识别」职责曾存在三份各自维护的字面量清单
（``supervisor_host.HOST_NAMES`` / ``supervisor_host._ADAPTERS`` /
``startup_environment.HOSTS``），且互相漂移——引擎侧只认识 6 个宿主，CLI 侧
``src/bugx_cli/host_adapters.py`` 已声明 9 个。后果是 ``workbuddy`` / ``dsh`` /
``chatgpt`` 三个宿主**永远无法被识别**：旧的全局宿主声明会被静默丢弃，
宿主原生单选能力被降级为 ``interaction=blocked``，启动门禁停在第一步。

本模块是引擎侧唯一的宿主名与项目标记来源。``supervisor_host`` 与
``startup_environment`` 都必须从这里取，不得再写本地字面量；CLI 侧
``host_adapters.HOST_ADAPTERS`` 由 ``tests/test_consistency.py`` 与本清单做
双向一致性守护。

依赖约束：本模块只允许依赖标准库，避免成为新的 import 环。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from types import MappingProxyType

try:
    from core import hosts as host_plugins
except ImportError:
    import core.hosts as host_plugins

# 受支持宿主（顺序即稳定展示顺序）。新增宿主必须同时更新：
#   1. 本清单；2. HOST_MARKERS；3. CLI 侧 src/bugx_cli/host_adapters.py。
# 缺任何一处都会被 tests/test_consistency.py 拦下。
SUPPORTED_HOSTS: tuple[str, ...] = (
    "opencode",
    "claude",
    "codex",
    "pi",
    "cline",
    "zcode",
    "dsh",
    "chatgpt",
    "workbuddy",
    "qoder",
    "trea",
    "funide",
)

# ``unknown`` 是「无法识别」这一一等结果，不是宿主，恒排在最后。
UNKNOWN_HOST = "unknown"
HOST_NAMES: tuple[str, ...] = SUPPORTED_HOSTS + (UNKNOWN_HOST,)

# 项目级宿主标记。任一相对路径存在（文件或目录）即认为该宿主在本项目中已接入。
# 语义与 CLI 侧 ``HostAdapter.project_marker`` 对齐：只描述「项目已接入」，
# 不证明该宿主是当前会话的宿主。
HOST_MARKERS: dict[str, tuple[str, ...]] = {
    "claude": (".mcp.json", ".claude/mcp.json", ".claude"),
    "pi": (".pi",),
    "zcode": (".zcode",),
    "dsh": (".dsh/patch.yml", ".dsh"),
    "chatgpt": (".agents/plugins/marketplace.json",),
    "workbuddy": (
        ".workbuddy/skills/bugx/SKILL.md",
        ".codebuddy/skills/bugx/SKILL.md",
        ".workbuddy",
        ".codebuddy",
    ),
    "qoder": (".qoder", ".qoder/config.json", ".qoder/mcp.json"),
    # 真实 Trae / Trae CN 打开过的项目只会有 .trae/ 或 .trae-cn/ 目录；.trea 是 BugX 安装面。
    "trea": (".trea", ".trea/agent.yml", ".trea/mcp.json", ".trae", ".trae-cn"),
    "funide": (".github/agents/bugx.agent.md",),
}


def _apply_plugin_markers(markers: dict[str, tuple[str, ...]]) -> None:
    """已迁移到 ``core/hosts`` 的宿主，标记由插件拥有，此处不得重复声明。

    DOC-239：``identity.markers`` 是「本项目已接入该宿主」的唯一真源；本模块只为
    尚未迁移的宿主保留本地表。插件声明优先，因此把旧字面量删掉后行为不变。
    """

    for plugin in host_plugins.all():
        markers[plugin.name] = plugin.identity.markers


_apply_plugin_markers(HOST_MARKERS)

#: 已迁移插件（只读视图）：宿主事实的唯一来源。
_HOST_PLUGINS = MappingProxyType(dict(host_plugins.PLUGINS))

# FunIDE 当前确认的是桌面 Copilot Agent UI，尚未确认独立的
# AskUserQuestion 原生工具；保持安全默认，交互交给外层控制器或 TTY。
NATIVE_QUESTION_HOSTS: frozenset[str] = frozenset(SUPPORTED_HOSTS) - {"funide"}

# 宿主可执行名别名 → 规范宿主名。进程树识别必须经此归一化，避免
# 「同一宿主多个可执行名」散落在各调用点。
HOST_EXECUTABLE_ALIASES: dict[str, str] = {
    "claude-code": "claude",
    "deepseek": "dsh",
    "codebuddy": "workbuddy",
    "codebuddy-lite-wb": "workbuddy",
    "qoder-cli": "qoder",
    "ali-qoder": "qoder",
    "trea-cli": "trea",
    "trae": "trea",
    "trae-cn": "trea",
    "trae_cn": "trea",
    "trea-cn": "trea",
    "trea_cn": "trea",
    "trae-ai": "trea",
}


def normalize_host_name(value: object) -> str | None:
    """把宿主名/可执行名归一化为规范宿主名；无法识别时返回 None。"""
    if not isinstance(value, str):
        return None
    name = value.strip().casefold()
    if not name:
        return None
    if name in SUPPORTED_HOSTS:
        return name
    return HOST_EXECUTABLE_ALIASES.get(name)


def host_marker(root: Path, name: str) -> bool:
    """Return whether the project carries any marker for ``name``.

    A missing/unknown host name is never a marker hit: callers rely on this
    predicate to decide host attribution, and silently returning True for
    ``unknown`` would misattribute every project to it.
    """
    markers = HOST_MARKERS.get(name)
    if not markers:
        return False
    try:
        for relative in markers:
            candidate = root / relative
            if candidate.exists():
                return True
    except OSError:
        return False
    return False


def configured_hosts(root: Path) -> list[str]:
    """Return every supported host with a project marker, in registry order."""
    try:
        resolved = root.resolve()
    except OSError:
        return []
    matches = [name for name in SUPPORTED_HOSTS if host_marker(resolved, name)]
    # ``.mcp.json`` is shared by Claude Code and FunIDE.  A FunIDE-only
    # installation also carries the unambiguous ``.github/agents`` marker;
    # do not turn that shared file into a false Claude ambiguity.  If Claude
    # has its own directory/config marker, retain both hosts as configured.
    if "funide" in matches and "claude" in matches:
        claude_specific = any(
            (resolved / relative).exists()
            for relative in (".claude", ".claude/mcp.json")
        )
        if not claude_specific:
            matches.remove("claude")
    return matches


def configured_host(root: Path) -> str | None:
    """Return the single configured host, or ``None`` when ambiguous.

    Multiple host entries are common when a project has been installed for more
    than one AI IDE.  In that case a marker cannot identify the current host and
    must not silently select one.
    """
    hosts = configured_hosts(root)
    return hosts[0] if len(hosts) == 1 else None


def host_plugin(name: object):
    """Return the migrated plugin for ``name``（``None`` = 未迁移/未知）。

    DOC-239：宿主事实（展示名、串行性、并发硬顶、沙箱）只允许来自
    ``core/hosts/<host>.py``；引擎其它模块一律经本模块或 ``launch_phases.common``
    取用，不得再写 ``host == "<宿主>"``。
    """

    if not isinstance(name, str):
        return None
    plugin = _HOST_PLUGINS.get(name.strip().casefold())
    return plugin if plugin is not None and plugin.ready else None


def host_label(name: object) -> str:
    """宿主展示名（插件 ``identity.label``）；未迁移宿主回退为规范名本身。"""

    plugin = host_plugin(name)
    if plugin is not None:
        return plugin.identity.label
    return str(name or "").strip().casefold() or UNKNOWN_HOST


def host_serial_worker(name: object) -> bool:
    """``name`` 是否是声明为**串行 Worker** 的宿主（插件 ``worker_runtime.mode``）。"""

    plugin = host_plugin(name)
    return bool(plugin is not None and plugin.worker_runtime.mode == "serial")


#: 尚未迁移、但具备原生 Task 派发的宿主基线（逐宿主切片时删除对应项）。
#: 已迁移宿主由 ``engine_run.native_task`` 自行声明，不进本表。
LEGACY_NATIVE_TASK_HOSTS: frozenset[str] = frozenset({"claude"})


def host_native_task(name: object) -> bool:
    """``name`` 是否能由宿主原生 Task 派发 Worker（插件 ``engine_run.native_task``）。

    这是 ``worker_orchestrator`` 旧 ``selected_host in {"claude","codex","opencode"}``
    字面量的唯一替身：已迁移宿主读插件，未迁移宿主读基线表。
    """

    plugin = host_plugin(name)
    if plugin is not None:
        return bool(plugin.engine_run.native_task)
    return isinstance(name, str) and name.strip().casefold() in LEGACY_NATIVE_TASK_HOSTS


def host_worker_concurrency(name: object) -> int | None:
    """宿主声明的 Worker 并发硬顶；未声明返回 ``None``（按调用方自适应容量）。"""

    plugin = host_plugin(name)
    return None if plugin is None else plugin.worker_runtime.max_concurrency


def host_workspace_dirs() -> frozenset[str]:
    """已迁移插件在被测工作区生成的顶层目录（``install.marker`` / ``generated_dirs``）。

    契约：只有 ``ready`` 插件才算数；未迁移宿主的目录事实仍留在调用方的
    「待迁移基线」字面量里，逐宿主切片时删除。
    """

    dirs: set[str] = set()
    for plugin in host_plugins.all():
        if not plugin.ready:
            continue
        install = plugin.install
        if install is None:  # pragma: no cover - ready 宿主缺面已在导入期 fail-fast
            continue
        marker_root = install.marker.split("/", 1)[0].strip()
        if marker_root.startswith("."):
            dirs.add(marker_root)
        for directory in install.generated_dirs:
            dirs.add(directory.split("/", 1)[0])
        for marker in plugin.identity.markers:
            head = marker.split("/", 1)[0].strip()
            if head.startswith(".") and "." not in head[1:]:
                dirs.add(head)
    return frozenset(dirs)


def host_requires_pid(name: object) -> bool:
    """``name`` 的 Worker 缺 ``process_pid`` 时孤儿租约回收会退化（插件事实）。"""

    plugin = host_plugin(name)
    return bool(plugin is not None and plugin.worker_runtime.pid_required)


#: 尚未迁移（``TODO_HOSTS``）但仍支持 MCP 写入的宿主。逐宿主切片时删除对应名字；
#: 已迁移插件一律由 ``mcp_write`` 面自行声明，不进本表。
LEGACY_MCP_WRITE_HOSTS: frozenset[str] = frozenset(
    {"claude", "pi", "dsh", "chatgpt", "workbuddy", "codebuddy", "zcode", "qoder", "trea"}
)


def host_mcp_writable(name: object) -> bool:
    """``name`` 是否能被 BugX 写入 MCP 配置（插件面优先，未迁移宿主走基线表）。"""

    if host_plugin(name) is not None:
        return True
    return isinstance(name, str) and name.strip().casefold() in LEGACY_MCP_WRITE_HOSTS


def host_autonomous_cli(name: object) -> bool:
    """CLI 是否能以进程池方式自行驱动 ``name``（插件 ``worker_runtime.autonomous_cli``）。"""

    plugin = host_plugin(name)
    return bool(plugin is not None and plugin.worker_runtime.autonomous_cli)


def host_concurrency_bounds(name: object) -> tuple[int, int] | None:
    """宿主声明的 ``--workers`` 合法区间（插件事实）；未声明返回 ``None``。"""

    plugin = host_plugin(name)
    return None if plugin is None else plugin.worker_runtime.concurrency_bounds


def _prepare_scout_cline_data(root: Path) -> Path:
    """DOC-241: 为 Cline Recon Scout 构造隔离的零 MCP 运行时环境。

    Cline 默认会读取 ~/.cline/data/settings/cline_mcp_settings.json，其中若已通过
    bugx install 安装了 bugx-supervisor，Cline 的后台 hub 守护进程会在执行任何
    任务时自动启动 bugx mcp serve 并长久驻留，从而锁死当前项目的 controller.lock。
    此处构造隔离的 data-dir：保留模型凭据（providers.json），但强制 mcpServers 为空，
    确保纯 LLM 文本推理，零进程残留、零锁争抢。
    """
    # recon scout 的隔离 data-dir 必须落在**项目树之外**：bugx doctor 承诺默认
    # 只读诊断，而本函数会在项目内建 .bugx/scratch/scout_cline_data（20+ 条目），
    # 直接把零写盘契约破掉（test_a1_doctor_is_read_only_by_default 实测）。
    # 放到系统临时目录，按项目根区分，避免同机多项目互相覆盖。
    import hashlib as _hashlib
    import tempfile as _tempfile
    _scope = _hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    scout_dir = Path(_tempfile.gettempdir()) / f"bugx-scout-{_scope}" / "scout_cline_data"
    settings_dir = scout_dir / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    mcp_file = settings_dir / "cline_mcp_settings.json"
    mcp_file.write_text('{"mcpServers": {}}\n', encoding="utf-8")

    target_providers = settings_dir / "providers.json"
    if not target_providers.exists() or target_providers.stat().st_size == 0:
        candidate_paths = [
            Path.home() / ".cline" / "data" / "settings" / "providers.json",
            Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "providers.json",
        ]
        for cand in candidate_paths:
            if cand.is_file():
                try:
                    target_providers.write_bytes(cand.read_bytes())
                    break
                except Exception:
                    pass
    return scout_dir


def _real_node_binary() -> str | None:
    """返回 PATH 中第一个**非 Electron shim** 的 node 绝对路径。

    DSH Desktop 会把 ``$DSH_HOME/.desktop-bin`` 放到 PATH 首位，其中 ``node``
    是 ``ELECTRON_RUN_AS_NODE=1`` 的 shell shim。任何 ``#!/usr/bin/env node``
    脚本（含 ``dsh`` 本身）都会命中它，于是 node 自身的 flag（如
    ``--expose-internals``）和 ``NODE_OPTIONS`` 全部失效。

    判定：目录名含 ``.desktop-bin`` **或** 文件头出现 ``ELECTRON_RUN_AS_NODE``
    即跳过——只查目录名挡不住被复制的 shim，只查内容要读满整个二进制。
    """
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    seen: set[str] = set()
    for entry in path_entries:
        if not entry:
            continue
        candidate = Path(entry) / "node"
        try:
            resolved = str(candidate.resolve())
        except OSError:
            continue
        if resolved in seen or not os.access(candidate, os.X_OK):
            continue
        seen.add(resolved)
        if ".desktop-bin" in candidate.parts:
            continue
        try:
            head = candidate.read_bytes()[:200]
        except OSError:
            continue
        if b"ELECTRON_RUN_AS_NODE" in head:
            continue
        return str(candidate)
    return None


def _with_real_node(argv: list[str], node_flags: tuple[str, ...]) -> list[str]:
    """把 ``argv`` 的启动器换成真 node，并前置宿主要求的 node flags。

    宿主声明 ``recon_scout_node_flags`` 说明「本家 CLI 必须由真 node 携带这些
    flag 启动」。找不到真 node 时原样返回：让失败信息指向真实原因（Electron
    shim 的 HMR 报错），而不是被替换成另一个更难懂的错。
    """
    if not argv:
        return argv
    node = _real_node_binary()
    if not node:
        return argv
    launcher = shutil.which(argv[0]) or argv[0]
    return [node, *node_flags, launcher, *argv[1:]]


def _read_scout_inventory(engine_run: object, root: Path) -> object | None:
    """按插件声明执行宿主 CLI 的配置清单查询，返回解析后的 JSON。

    为什么放在引擎而不是插件里：插件模块禁止 import ``subprocess``/``shutil``
    （依赖方向门禁，插件事实只能单向流出）。插件只声明 ``(argv, key)``，本函数
    负责真实执行与解析；任何失败（命令不存在、超时、非零、非 JSON）都返回
    ``None``——侦察退化为「不禁用 MCP」而不是整轮失败，同时把原因打到 stderr
    以便区分「清单空」与「查询失败」。
    """
    import json
    import subprocess
    import sys

    declaration = getattr(engine_run, "recon_scout_inventory", None)
    if not callable(declaration):
        return None
    try:
        argv, key = declaration(Path(root))
    except Exception:  # noqa: BLE001 - 声明本身失败不得阻断侦察
        return None
    try:
        completed = subprocess.run(
            list(argv), cwd=str(root), capture_output=True, text=True,
            timeout=5, check=True,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"  [ReconScout] 清单查询失败，按不禁用 MCP 继续：{exc}", file=sys.stderr)
        return None
    # 空结果键 = 整个负载就是清单（插件声明该宿主 CLI 直接返回顶层数组）。
    # 非空时按键取：不同宿主的清单形状不同，形状事实由插件声明，不在引擎猜。
    if not key:
        return payload
    return payload.get(key) if isinstance(payload, dict) else payload


def _prepare_scout_isolated_data(host: str, root: Path) -> Path:
    """为声明了 ``recon_scout_isolated_data`` 的宿主构造零 MCP 的临时数据面。

    放在系统临时目录（按项目根分目录），**绝不写项目树**：``bugx doctor`` 承诺
    默认只读诊断，在项目内建 scout 目录会直接把零写盘契约破掉
    （``test_a1_doctor_is_read_only_by_default`` 曾实测失败）。

    Cline 具体形态：保留 ``providers.json`` 里的模型凭据，但把
    ``settings/cline_mcp_settings.json`` 的 ``mcpServers`` 强制为空——否则 Cline
    的后台 hub 会为已安装的 ``bugx-supervisor`` 拉起常驻 ``bugx mcp serve``，
    锁死被测项目的 controller.lock。
    """
    import hashlib
    import json
    import tempfile

    scope = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    scout_dir = Path(tempfile.gettempdir()) / f"bugx-scout-{scope}" / f"scout_{host}_data"
    settings_dir = scout_dir / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "cline_mcp_settings.json").write_text(
        json.dumps({"mcpServers": {}}) + "\n", encoding="utf-8",
    )

    # 模型凭据必须带过去：清空 mcpServers 是为了「无 MCP」，不是「无凭据」。
    # 漏掉这段会让 Scout 因缺少 provider 配置直接失败（迁移时曾丢失，实测
    # 隔离目录里只有 settings/cline_mcp_settings.json）。
    if host == "cline":
        target = settings_dir / "providers.json"
        if not target.exists() or target.stat().st_size == 0:
            for candidate in (
                Path.home() / ".cline" / "data" / "settings" / "providers.json",
                Path.home() / "Library" / "Application Support" / "Code" / "User"
                / "globalStorage" / "saoudrizwan.claude-dev" / "settings"
                / "providers.json",
            ):
                if candidate.is_file():
                    try:
                        target.write_bytes(candidate.read_bytes())
                        break
                    except OSError:
                        pass
    return scout_dir


def build_recon_scout_command(host_name: str, root: Path, prompt_file: Path) -> list[str]:
    """为不同活跃宿主构建无头 Recon Scout 侦察指令 (DOC-241 A3)。

    **插件声明优先**：宿主在 ``EngineRunFacet.recon_scout_argv`` 里声明自己的
    一次性侦察 argv，本函数只做分派。历史缺陷（2026-11-02 实测）：本函数曾对
    opencode/claude/codex/cline 各写一个 ``if host ==``，其余宿主静默落到
    ``[host, prompt]`` 兜底——DSH 因此拼出裸 ``dsh <prompt>``，实测返回
    ``error: --profile <name> is required``，侦察 100% 退化为静态播种，而报错
    完全不指向「少了一个宿主分支」。改为插件驱动后，新增宿主漏声明是显式的
    （``None``），不再静默丢能力。

    未声明该面的宿主保留以下兼容分支（原实测口径）：
    - opencode: opencode run --format json -f <prompt_file> <message>
    - claude: claude -p @<prompt_file>
    - codex: codex exec --json -C <root> <prompt>
    - cline: cline --json -p --data-dir <scout_dir> -c <root> <prompt>
    """
    host = str(host_name).strip().casefold()
    prompt_text = ""
    try:
        if prompt_file.is_file():
            prompt_text = prompt_file.read_text(encoding="utf-8")
    except Exception:
        pass

    # 插件声明优先：宿主自己知道「怎么让本家 CLI 一次性回答这个 prompt」。
    try:
        plugin = host_plugins.get(host)
    except Exception:
        plugin = None
    engine_run = getattr(plugin, "engine_run", None)
    scout_argv = getattr(engine_run, "recon_scout_argv", None)
    if callable(scout_argv):
        inventory = _read_scout_inventory(engine_run, root)
        kwargs: dict[str, object] = {}
        if bool(getattr(engine_run, "recon_scout_isolated_data", False)):
            kwargs["data_dir"] = _prepare_scout_isolated_data(host, root)
        argv = list(
            scout_argv(Path(root), prompt_text, Path(prompt_file), inventory, **kwargs)
        )
        node_flags = tuple(getattr(engine_run, "recon_scout_node_flags", ()) or ())
        if node_flags:
            argv = _with_real_node(argv, node_flags)
        return argv

    # 未声明 recon_scout_argv 的宿主：没有可验证的一次性侦察通道。此前这里是
    # 一串 ``if host == "opencode"/"claude"/"codex"/"cline"`` 死分支（四个宿主都已
    # 在插件里声明，走不到这里），再兜底 ``[host, prompt]``。兜底对任何 CLI 都不
    # 成立——DSH 曾因此拼出裸 ``dsh <prompt>`` 并 100% 退化为静态播种。现在显式
    # 返回空 argv，让调用方按「无侦察通道」降级，而不是发一条注定失败的假命令。
    return []


def build_recon_scout_env(host_name: str, root: Path) -> dict[str, str]:
    """为不同活跃宿主构造 Recon Scout 侦察环境变量 (DOC-241/DOC-243)。"""
    host = str(host_name).strip().casefold()
    env: dict[str, str] = {}
    # 同上：scout 的隔离数据面一律放系统临时目录，不写项目树，
    # 否则只读命令（doctor 等）会污染被测工作区。
    import hashlib as _hashlib2
    import tempfile as _tempfile2
    _scope2 = _hashlib2.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    _scout_base = Path(_tempfile2.gettempdir()) / f"bugx-scout-{_scope2}"
    if host == "opencode":
        opencode_data = _scout_base / "opencode_data"
        opencode_data.mkdir(parents=True, exist_ok=True)
        env["XDG_DATA_HOME"] = str(opencode_data)
        env["PWD"] = str(Path(root).resolve())
    elif host == "cline":
        scout_dir = _scout_base / "scout_cline_data"
        scout_dir.mkdir(parents=True, exist_ok=True)
        env["CLINE_DATA_DIR"] = str(scout_dir)
    return env


def host_project_mcp_relpaths() -> list[str]:
    """返回所有已适配宿主声明的项目级 MCP 配置相对路径 (DOC-243 消除硬编码宿主事实字面量)。"""
    relpaths: list[str] = []
    for plugin in _HOST_PLUGINS.values():
        rel = getattr(getattr(plugin, "mcp_write", None), "project_relpath", None)
        if rel and isinstance(rel, str) and rel not in relpaths:
            relpaths.append(rel)
    return relpaths


def resolve_scout_host(root: Path, preferred: str | None = None) -> str:
    """DOC-241: 智能级联解析可用于 Recon Scout / Profile 裁决的活跃 AI 宿主。

    优先级：
    1. preferred (若调用方显式传入 --host 或 runtime["host"])
    2. 项目配置宿主 (configured_hosts(root)):
       - 若唯一定位一个宿主，优先采用该宿主
       - 若存在多个宿主标记，优先选取在系统 PATH 中可调用的宿主
    3. 宿主适配器主动探查 (进程祖先链 supervisor_host.detect_host)
    4. 当前会话环境特征变量:
       - OPENCODE_SESSION_ID / OPENCODE_SERVER_PORT -> "opencode"
       - CLAUDE_CODE_ENTRY / CLAUDE_PROJECT_ROOT -> "claude"
       - CODEX_CLI / CODEX_SESSION -> "codex"
       - CLINE_DATA_DIR / CLINE_APP -> "cline"
    5. 全局 PATH 中首个物理可调用的标准 AI 宿主 CLI (opencode, claude, codex, cline, workbuddy)

    注意：**不读 ``BUGX_HOST``**。DOC-217 D211.3 已废除该变量作为运行时/调度
    契约，见下方第 5 步注释。
    """
    # 1. 显式指定 preferred 宿主
    if preferred and isinstance(preferred, str) and preferred.strip().casefold() != UNKNOWN_HOST:
        norm = normalize_host_name(preferred) or preferred.strip().casefold()
        if norm and (shutil.which(norm) or norm in SUPPORTED_HOSTS):
            return norm

    # 2. 项目标记解析 (configured_hosts)
    try:
        cfg = configured_hosts(root)
        if len(cfg) == 1:
            return cfg[0]
        if len(cfg) > 1:
            for h in cfg:
                if shutil.which(h):
                    return h
    except Exception:
        pass

    # 3. 进程祖先链主动探查（原语下沉 host_probe，消除与 supervisor_host 的双向依赖）
    try:
        from core.host_runtime import host_probe

        probed = host_probe.probe_active_host()
        if probed:
            name = normalize_host_name(probed) or UNKNOWN_HOST
            if name != UNKNOWN_HOST and name in HOST_NAMES and shutil.which(name):
                return name
    except Exception:
        pass

    # 4. 当前会话环境特征变量探查
    if os.environ.get("OPENCODE_SESSION_ID") or os.environ.get("OPENCODE_SERVER_PORT"):
        if shutil.which("opencode"):
            return "opencode"
    if os.environ.get("CLAUDE_CODE_ENTRY") or os.environ.get("CLAUDE_PROJECT_ROOT"):
        if shutil.which("claude"):
            return "claude"
    if os.environ.get("CODEX_CLI") or os.environ.get("CODEX_SESSION"):
        if shutil.which("codex"):
            return "codex"
    if os.environ.get("CLINE_DATA_DIR") or os.environ.get("CLINE_APP"):
        if shutil.which("cline"):
            return "cline"

    # 5. **不读 BUGX_HOST**（DOC-217 D211.3 已废除它为运行时/调度契约）。
    #    同库的 ``supervisor_host.detect_host`` 早已明令禁止该变量覆盖或兜底：
    #    陈旧全局 env 在多宿主并存/换宿主时必然错绑（实测 workbuddy 残留 env
    #    曾把 ZCode 会话错绑成 workbuddy）。此处原先在第 3 步进程祖先探查之后
    #    仍读它，等于给同一个错绑留下第二条通路；测试
    #    ``test_resolve_env_bugx_host`` 失败正是这条废弃路径被暴露。
    #    宿主身份只来自：显式 preferred、项目标记、真实进程祖先证据。

    # 6. 系统全局兜底：检测标准 AI 宿主 CLI 是否安装
    for fallback in ("opencode", "claude", "codex", "cline", "workbuddy"):
        if shutil.which(fallback):
            return fallback

    return ""


__all__ = [
    "HOST_EXECUTABLE_ALIASES",
    "HOST_MARKERS",
    "HOST_NAMES",
    "LEGACY_MCP_WRITE_HOSTS",
    "LEGACY_NATIVE_TASK_HOSTS",
    "NATIVE_QUESTION_HOSTS",
    "SUPPORTED_HOSTS",
    "UNKNOWN_HOST",
    "build_recon_scout_command",
    "configured_host",
    "configured_hosts",
    "host_autonomous_cli",
    "host_concurrency_bounds",
    "host_label",
    "host_marker",
    "host_mcp_writable",
    "host_native_task",
    "host_plugin",
    "host_requires_pid",
    "host_serial_worker",
    "host_worker_concurrency",
    "host_workspace_dirs",
    "normalize_host_name",
    "resolve_scout_host",
]
