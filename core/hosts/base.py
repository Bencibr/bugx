#!/usr/bin/env python3
"""core/hosts/base.py - 宿主插件的六面契约（唯一声明点）。

背景（DOC-239）：宿主事实此前分散在六处（``host_registry`` / ``supervisor_host`` /
``HostComponent`` / ``host_adapters`` / ``commands/plugin.py`` / ``worker_runtime``），
新增宿主必须同步改多处，漏一处就静默降级——历史上已发生两次真实缺陷
（DES-01 三份宿主清单漂移；build 182 ``opencode.json`` 少一路 MCP 导致探针误判）。

本模块只定义**数据契约**，不放任何宿主事实：

* 一个宿主一个模块（``core/hosts/<host>.py``），模块导出 ``PLUGIN``；
* 六个面，每个面只有一个 owner 层（见 ``FACET_NAMES``）；
* ``status`` 只有 ``ready`` / ``todo``；``ready`` 缺面在**导入期** fail-fast，
  ``todo`` 必须给原因且**不得**带任何面——「半可用」不允许被当成可用。

依赖约束：本模块只允许标准库，且**禁止** import ``host_registry`` /
``supervisor_host`` / ``bugx_cli.*``（依赖方向只能是它们读插件）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, ContextManager, Mapping

__all__ = [
    "AcpFacet",
    "EngineRunFacet",
    "HostPlugin",
    "HostPluginError",
    "IdentityFacet",
    "InstallFacet",
    "McpWriteFacet",
    "RunContext",
    "STATUS_READY",
    "STATUS_TODO",
    "WorkerRuntimeFacet",
    "UsageFacet",
    "validate_plugin",
]

STATUS_READY = "ready"
STATUS_TODO = "todo"

#: 六个核心面；usage 是可选增强面，不阻断 ready 宿主。
FACET_NAMES: tuple[str, ...] = (
    "identity",
    "install",
    "engine_run",
    "worker_runtime",
    "mcp_write",
    "acp",
    "usage",
)
REQUIRED_FACETS: tuple[str, ...] = FACET_NAMES[1:6]
OPTIONAL_FACETS: tuple[str, ...] = REQUIRED_FACETS

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
#: 点分模块名（``host_runtime.cline``）——执行器模块只按名字 import。
_MODULE_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")



class HostPluginError(RuntimeError):
    """宿主插件契约违约（导入期 fail-fast，禁止降级为警告）。"""


@dataclass(frozen=True)
class RunContext:
    """宿主启动所需的纯数据（替代四处散落的位置参数）。"""

    root: Path
    agent: str
    session: str
    prompt: str



@dataclass(frozen=True)
class IdentityFacet:
    """身份面：宿主是谁、在项目里留下什么痕迹。"""

    name: str
    label: str
    order: int
    executable: str
    markers: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    #: 应用安装位置探测（惰性：**不得**在 import 期调用）。
    app_markers: Callable[[], tuple[Path, ...]] | None = None
    #: 是否具备宿主原生单选控件（对应 ``NATIVE_QUESTION_HOSTS``）。
    native_question: bool = True


@dataclass(frozen=True)
class InstallFacet:
    """安装面：入口种类/路径/能力标签（写入实现留在 CLI 层）。"""

    kind: str
    path: str
    entry_mode: str = "rendered"
    #: 安装面目录标记（CLI ``HostAdapter.marker``，如 ``.opencode``）。
    #: 与 ``IdentityFacet.markers``（引擎项目标记，如 ``opencode.json``）是两个
    #: 不同问题：前者表示「本宿主已安装入口」，后者表示「本项目已接入该宿主」。
    marker: str = ""
    required_paths: tuple[str, ...] = ()
    legacy_paths: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    interaction: Mapping[str, Any] = field(default_factory=dict)
    mcp_relpaths: tuple[str, ...] = ()
    worker_subagent: bool = False
    #: Worker 子 Agent 入口路径（``worker_subagent=True`` 时必须声明；
    #: 原 ``host_adapters.CLINE_WORKER_INSTALL_PATH`` 的宿主事实）。
    worker_entry_path: str = ""
    #: 入口模板内容（渲染实现留在 CLI 层；此处只声明宿主事实）。
    #: 原先散落在 ``HostComponent.custom_title`` / ``custom_preamble`` /
    #: ``tools_line``，纵切宿主必须在插件里声明，否则「新增宿主」仍要改 CLI 注册表。
    custom_title: str = ""
    custom_preamble: str = ""
    tools_line: str = ""
    #: 本宿主在被测工作区里生成的目录（原 ``post_phase._GIT_HOST_GENERATED_DIRS``
    #: 与 ``symbol_index.HOST_RUNTIME_DIRS`` 的宿主事实）。这些目录属于宿主/运行时，
    #: 不是被审计产品的一部分：既不进 Git 提交面，也不进符号索引。
    generated_dirs: tuple[str, ...] = ()
    #: 入口是「按 Skill 清扫」语义（原 ``common._SWEEPABLE_SKILL_ADAPTERS``）：
    #: 卸载/升级时按适配器安装面整体清扫，而不是逐文件比对。
    sweepable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.interaction, MappingProxyType):
            object.__setattr__(self, "interaction", MappingProxyType(dict(self.interaction)))


@dataclass(frozen=True)
class EngineRunFacet:
    """引擎启动面：argv、MCP 配置、自治子进程环境和原生 Task 能力。"""

    supports_headless: bool
    headless_argv: Callable[[RunContext], list[str]]
    autonomous_argv: Callable[[RunContext], list[str]]
    mcp_config_paths: Callable[[Path], tuple[Path, ...]]
    #: 宿主「主配置」路径的宿主口径（如 codex 的 ``config.toml`` → ``config.json``
    #: → ``.codex/``）。缺省时适配器取 ``identity.markers[0]``。
    agent_config_path: Callable[[Path], Path] | None = None
    mcp_health: Callable[[str, dict, Path | None], bool] | None = None
    host_surface: str = "tui"
    #: 宿主具备原生 Task 派发（对应 ``worker_orchestrator.native_task_host``）。
    native_task: bool = False
    #: 为一次自治子进程准备临时环境；退出上下文时必须清理临时资源。
    autonomous_environment: Callable[[Path], ContextManager[Mapping[str, str | None]]] | None = None
    #: Recon Scout 的一次性无头侦察 argv：``(root, prompt_text, prompt_file) -> argv``。
    #:
    #: **为什么必须由插件声明**：``host_registry.build_recon_scout_command`` 曾对
    #: opencode/claude/codex/cline 各写一个 ``if host ==``，其余宿主静默落到
    #: ``[host, prompt]`` 兜底分支——DSH 因此拼出裸 ``dsh <prompt>``，实测返回
    #: ``error: --profile <name> is required``，侦察 100% 降级为静态播种，且没有任何
    #: 报错指向「少了一个宿主分支」。把 argv 交给插件后，新增宿主忘记声明会在导入期
    #: fail-fast，而不是在运行时静默丢能力。缺省 ``None`` 表示该宿主确无无头侦察通道，
    #: 调用方据此外显式降级。
    recon_scout_argv: Callable[[Path, str, Path, object | None], list[str]] | None = None
    #: 该宿主的 CLI 入口必须用**真 node** 携带这些 flag 启动（声明式，不写代码）。
    #:
    #: 背景（2026-11-02 实测）：DSH Desktop 会把 ``$DSH_HOME/.desktop-bin`` 放在
    #: PATH 首位，其中 ``node`` 是 ``ELECTRON_RUN_AS_NODE=1`` 的 shim；``dsh`` 的
    #: shebang 是 ``#!/usr/bin/env node`` 因而命中 shim。headless profile 的
    #: ``dsh-base`` 无条件加载 HMR，HMR 硬要求 ``--expose-internals``，而 Electron
    #: 宿主进程禁用 ``NODE_OPTIONS``——两条路同时堵死。宿主只能**声明**「我需要
    #: 真 node + 这个 flag」，由引擎侧的 node 解析器负责找可执行文件并拼 argv。
    #:
    #: 插件不得自己 import ``shutil``/``subprocess``（依赖方向门禁），所以这里
    #: 只放数据。空元组表示按常规方式直接执行 ``recon_scout_argv[0]``。
    recon_scout_node_flags: tuple[str, ...] = ()
    #: 该宿主 CLI 的**主执行路径**（headless/autonomous）是否同样需要真 node。
    #:
    #: 2026-11-03 实测补齐：``recon_scout_node_flags`` 只覆盖侦察，主挖掘走
    #: ``supervisor_host.headless_run`` 直接返回插件 argv、从不做 node 重写。结果是
    #: 「侦察能跑、真正开端挖掘必崩」——最坏的一种半修状态：静态播种产出看似正常，
    #: 用户看到的是挖掘刚起来就报 HMR 错。DSH Desktop 环境下 ``dsh --profile
    #: headless <task>`` 命中 ``.desktop-bin/node`` 就必抛
    #: ``--expose-internals is required for HMR service``。
    #:
    #: 与侦察字段分开而不复用：侦察和主执行是两条独立 argv 路径，将来可能有宿主只
    #: 需要其中一条。缺省空元组表示按常规方式直接执行。
    engine_run_node_flags: tuple[str, ...] = ()
    #: 侦察前需要向宿主 CLI 查询「有效配置清单」的声明：``(root) -> (argv, key)``。
    #:
    #: Codex 必须列出已启用的 MCP 服务器并逐个传 ``-c mcp_servers.<name>.enabled=false``，
    #: 否则侦察会带上目标项目的 MCP（含控制面），既慢又有副作用。查询要在**引擎侧**
    #: 执行——插件模块禁止 import ``subprocess``（依赖方向门禁，见
    #: ``test_plugin_modules_never_import_engine_or_cli``）。插件只声明「查什么命令、
    #: 结果的哪个键是清单」，由引擎读回后回调 ``recon_scout_argv`` 的 ``inventory`` 参数。
    #:
    #: 与 ``recon_scout_node_flags`` 同一设计：**数据出插件，执行留引擎**。
    recon_scout_inventory: Callable[[Path], tuple[list[str], str]] | None = None
    #: 侦察必须使用**隔离数据面**（``True`` 时引擎创建临时 data-dir 并注入）。
    #:
    #: Cline 会读 ``~/.cline/data`` 里已安装的 ``bugx-supervisor``，后台 hub 随即
    #: 拉起常驻 ``bugx mcp serve`` 并锁死被测项目的 controller.lock——侦察会从
    #: 「只读一次」变成「留下常驻进程」。目录创建与 ``mcpServers`` 清空是引擎侧
    #: 职责（插件不得碰文件系统），插件只声明这个事实。
    recon_scout_isolated_data: bool = False


@dataclass(frozen=True)
class WorkerRuntimeFacet:
    """运行期面：Worker 并发模式与沙箱同步清单。"""

    mode: str
    max_concurrency: int | None = None
    requires_process_mode: bool = False
    sandbox_sync: tuple[str, ...] = ()
    #: claim 级数据目录沙箱名（相对 artifact_root，如 ``.cline_data``）；
    #: 空串表示该宿主不需要沙箱（原 ``worker_runtime`` 里的宿主字面量）。
    sandbox_dir: str = ""
    #: 宿主 CLI 进程执行器模块（如 ``host_runtime.cline``）。非空表示「本宿主的
    #: Worker 由引擎以子进程方式驱动」；``worker_runtime`` 只按名字 import，不再
    #: 认识任何宿主名。
    runner_module: str = ""
    #: Supervisor ``provider=`` 标签（缺省等于宿主名）。
    provider_key: str = ""
    #: 缺少 ``process_pid`` 时孤儿租约回收会退化（原 ``worker_sync`` 的宿主分支）。
    pid_required: bool = False
    #: CLI 能以进程池方式自行驱动本宿主（原 ``autonomous.execute = host == "cline"``
    #: 与 ``--host`` 的 choices 清单）。
    autonomous_cli: bool = False
    #: CLI ``--workers`` 合法区间（原 ``autonomous`` 的 2~6 硬校验）。
    concurrency_bounds: tuple[int, int] | None = None


@dataclass(frozen=True)
class McpWriteFacet:
    """MCP 写入面：容器键与路径（写入函数以 ``writer_key`` 在 CLI 侧绑定）。"""

    container_key: str
    writer_key: str
    project_relpath: str | None = None
    global_scope: str | None = None
    mirror_relpaths: tuple[str, ...] = ()
    requires_absolutize: bool = True
    #: 每条 server 条目必须附带的宿主专有字段（如 ``disabled``/``autoApprove``
    #: 或 ``enabled``）。CLI 只做 ``entry.update(...)``，不再按宿主名分支。
    entry_extra: Mapping[str, Any] = field(default_factory=dict)
    #: 该宿主的 MCP 写入由 CLI 侧专用写入器负责（如 opencode.json 的
    #: permission/plugin 合并）。为真时通用 JSON 骨架不得接管。
    custom_writer: bool = False
    #: 项目级配置的序列化格式。``json`` 走通用 JSON 骨架；``toml`` 表示宿主只读
    #: TOML（如 Codex，2026-10-23 实测 ``.codex/config.json`` 永不被加载），
    #: 写入/清理/诊断一律委托给 ``bootstrap`` / ``remove`` / ``inspect``。
    config_format: str = "json"
    #: 引擎侧启动期写入实现：``(root, servers) -> 回执``。用于配置形态无法用
    #: 「JSON 容器键」表达的宿主（如 codex 的 TOML 表 + 运行期回执）。
    #: 缺省表示该宿主不需要引擎侧写入（项目级 JSON 由 CLI 负责）。
    bootstrap: Callable[[Path, Mapping[str, Mapping[str, Any]]], dict[str, Any]] | None = None
    #: 卸载期清理实现：``(root) -> {"removed": [...], "preserved": [...]}``。
    #: 非 JSON 形态（TOML）必须提供，否则卸载会按 JSON 解析并静默跳过。
    remove: Callable[[Path], Mapping[str, Any]] | None = None
    #: 只读诊断实现：``(root) -> {"servers": [...], "missing": [...], "trust": str}``。
    #: 供 CLI 的 ``validate_install`` 判定「配置是否真的会被宿主加载」。
    inspect: Callable[[Path], Mapping[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.entry_extra, MappingProxyType):
            object.__setattr__(self, "entry_extra", MappingProxyType(dict(self.entry_extra)))


@dataclass(frozen=True)
class AcpFacet:
    """ACP 探针面：指向 ``core/host_acp`` 的实现模块名。

    **只持指针，不复制状态**：ACP 可用性三态（``ready`` / ``todo`` /
    ``unavailable``）与原因的唯一声明点是 ``core/host_acp/<host>.py``；这里再写一份
    就必然漂移（DES-01 的成因就是同一批量在两个清单里各写一遍）。两层的对应关系由
    ``tests.test_host_plugins`` 的跨层守卫核对。
    """

    plugin: str


@dataclass(frozen=True)
class UsageFacet:
    """本地用量采集面：指向宿主专属收集器模块与本地状态文件。

    **复用契约**：任何宿主只要实现 ``core/hosts/_<host>_usage.py`` 并暴露
    ``collect_lifecycle_usage(project_root, runtime) -> dict``（返回
    ``scheduler_tokens`` / ``worker_sessions_tokens`` / ``models`` 数组 /
    ``duration_ms_total`` / ``detail_path``），即可声明本面接入 Round 报告的
    统一用量行。``state_filename`` 是落盘在 lifecycle ``state/`` 下的明细文件名。
    采集器内部必须只读宿主本地日志/DB，异常时抛宿主专属 ``UsageError``，
    调用方（``post_phase``）统一静默降级，绝不让用量失败阻断结算。
    """

    plugin: str
    module: str = ""          # 相对 core/hosts/ 的模块名，如 "_dsh_usage"
    state_filename: str = ""  # lifecycle 明细落盘名，如 "dsh-usage.json"
    notes: str = ""
    reader_format: str = ""  # "zstd" requests an engine-owned decompression reader


@dataclass(frozen=True)
class HostPlugin:
    """一个宿主的全部事实。字段为 ``None`` 表示该面尚未迁移（仅 ``todo`` 允许）。"""

    identity: IdentityFacet
    status: str = STATUS_TODO
    todo_reason: str = ""
    install: InstallFacet | None = None
    engine_run: EngineRunFacet | None = None
    worker_runtime: WorkerRuntimeFacet | None = None
    mcp_write: McpWriteFacet | None = None
    acp: AcpFacet | None = None
    usage: UsageFacet | None = None

    @property
    def name(self) -> str:
        return self.identity.name

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def declares(self, facet: str) -> bool:
        if facet not in FACET_NAMES:
            raise HostPluginError(f"未知宿主面: {facet}")
        return getattr(self, facet) is not None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HostPluginError(message)


def validate_plugin(plugin: HostPlugin) -> HostPlugin:
    """校验插件契约；任何违约都抛 :class:`HostPluginError`（不返回布尔值）。"""

    _require(isinstance(plugin, HostPlugin), f"插件必须是 HostPlugin，实得 {type(plugin)!r}")
    if plugin.usage is not None:
        _require(isinstance(plugin.usage, UsageFacet), f"{plugin.name}: usage 必须是 UsageFacet")
        _require(bool(plugin.usage.module.strip()), f"{plugin.name}: usage.module 不得为空")
        _require(plugin.usage.reader_format in {"", "zstd"}, f"{plugin.name}: usage.reader_format 无效")
    identity = plugin.identity
    _require(isinstance(identity, IdentityFacet), f"{plugin.identity!r} 不是 IdentityFacet")
    _require(bool(_NAME_RE.match(identity.name)), f"非法宿主名: {identity.name!r}")
    _require(bool(identity.label.strip()), f"{identity.name}: label 不得为空")
    _require(
        isinstance(identity.order, int) and identity.order >= 0,
        f"{identity.name}: order 必须为非负整数",
    )
    _require(bool(identity.markers), f"{identity.name}: markers 不得为空（否则永远无法被识别）")
    _require(
        plugin.status in {STATUS_READY, STATUS_TODO},
        f"{identity.name}: 非法 status: {plugin.status!r}",
    )

    present = [f for f in OPTIONAL_FACETS if getattr(plugin, f) is not None]
    if plugin.status == STATUS_TODO:
        _require(bool(plugin.todo_reason.strip()), f"{identity.name}: todo 必须给出 todo_reason")
        _require(not present, f"{identity.name}: todo 宿主不得声明任何面，实有 {present}")
        return plugin

    missing = [f for f in OPTIONAL_FACETS if getattr(plugin, f) is None]
    _require(not missing, f"{identity.name}: ready 宿主缺面 {missing}")

    engine = plugin.engine_run
    assert engine is not None  # 由 missing 检查保证
    _require(callable(engine.headless_argv), f"{identity.name}: headless_argv 必须可调用")
    _require(callable(engine.autonomous_argv), f"{identity.name}: autonomous_argv 必须可调用")
    _require(callable(engine.mcp_config_paths), f"{identity.name}: mcp_config_paths 必须可调用")
    _require(
        engine.autonomous_environment is None or callable(engine.autonomous_environment),
        f"{identity.name}: autonomous_environment 必须可调用",
    )

    mcp = plugin.mcp_write
    assert mcp is not None
    _require(bool(mcp.container_key.strip()), f"{identity.name}: mcp_write.container_key 不得为空")
    _require(bool(mcp.writer_key.strip()), f"{identity.name}: mcp_write.writer_key 不得为空")
    _require(
        mcp.project_relpath is not None or mcp.global_scope is not None,
        f"{identity.name}: mcp_write 必须给出 project_relpath 或 global_scope",
    )
    _require(
        mcp.config_format in {"json", "toml"},
        f"{identity.name}: 非法 mcp_write.config_format: {mcp.config_format!r}",
    )
    if mcp.config_format == "toml":
        # TOML 不是「换后缀」的 JSON：通用骨架读不了它，写入/清理/诊断三件必须
        # 全部由插件拥有，否则 CLI 会在无提示的情况下把配置写成死文件。
        _require(mcp.project_relpath is not None, f"{identity.name}: TOML mcp_write 必须给项目级路径")
        _require(callable(mcp.bootstrap), f"{identity.name}: TOML mcp_write 必须提供 bootstrap")
        _require(callable(mcp.remove), f"{identity.name}: TOML mcp_write 必须提供 remove")
        _require(callable(mcp.inspect), f"{identity.name}: TOML mcp_write 必须提供 inspect")
        _require(
            str(mcp.project_relpath).endswith(".toml"),
            f"{identity.name}: TOML mcp_write.project_relpath 必须是 .toml 文件",
        )

    runtime = plugin.worker_runtime
    assert runtime is not None
    _require(
        runtime.mode in {"multi", "serial"},
        f"{identity.name}: 非法 worker_runtime.mode: {runtime.mode!r}",
    )
    if runtime.runner_module:
        _require(
            bool(_MODULE_RE.match(runtime.runner_module)),
            f"{identity.name}: runner_module 必须是点分模块名: {runtime.runner_module!r}",
        )
    if runtime.provider_key:
        _require(
            bool(_NAME_RE.match(runtime.provider_key)),
            f"{identity.name}: 非法 provider_key: {runtime.provider_key!r}",
        )
    if runtime.concurrency_bounds is not None:
        bounds = runtime.concurrency_bounds
        if not (
            isinstance(bounds, tuple)
            and len(bounds) == 2
            and all(isinstance(value, int) for value in bounds)
        ):
            raise HostPluginError(
                f"{identity.name}: concurrency_bounds 必须是 (min, max) 整数二元组: {bounds!r}"
            )
        low, high = bounds
        _require(
            low >= 2 and high > low,
            f"{identity.name}: concurrency_bounds 必须满足 2 <= min < max: {bounds!r}",
        )
        _require(
            runtime.autonomous_cli,
            f"{identity.name}: 声明 concurrency_bounds 必须同时声明 autonomous_cli",
        )
    if runtime.autonomous_cli:
        _require(
            bool(runtime.runner_module),
            f"{identity.name}: autonomous_cli 必须同时声明 runner_module",
        )
    install = plugin.install
    assert install is not None
    for directory in install.generated_dirs:
        _require(
            directory.startswith(".") and "/" not in directory and "\\" not in directory,
            f"{identity.name}: generated_dirs 必须是工作区顶层点目录: {directory!r}",
        )

    acp = plugin.acp
    assert acp is not None
    _require(bool(acp.plugin.strip()), f"{identity.name}: acp.plugin 不得为空")
    _require(acp.plugin == identity.name, f"{identity.name}: acp.plugin 必须等于宿主名")
    return plugin
