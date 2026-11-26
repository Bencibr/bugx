---
title: BugX AI Host and Worker Reference
audience: AI hosts and native agents
status: DOCUMENTATION_UNVERIFIED
version: 0.1.5
protocol: MCP 2024-11-05
server: bugx-supervisor 0.1.5
last-verified: 2026-11-17
---

# BugX AI Reference

本文件是给 Host、Native Agent、Worker 编排器和 MCP 客户端使用的机器契约，不是用户教程。实现存在不等于真实宿主已经验收；没有 session proof、真实工具调用、checkpoint 和 receipt 时，AI 必须返回 UNVERIFIED 或 BLOCKED，不得返回 COMPLETE。

## 1. Runtime identity

### 1.1 Project and scope

    project_root = BUGX_PROJECT_ROOT if it is an existing directory else cwd
    project_state = project_root/.bugx
    global_engine = user BugX global engine root

项目状态绑定绝对 root。manifest identity mismatch、unsafe symlink、缺少 lifecycle/state 或布局不存在时 fail-closed；MCP server 不隐式创建项目布局。需要先由用户或 CLI 执行 init/install。

常用路径：

    .bugx/manifest.json
    .bugx/pollings/<polling-id>/state.json
    .bugx/pollings/<polling-id>/lifecycles/<lifecycle-id>/
    .bugx/state/life.json
    .bugx/state/runtime.json
    .bugx/state/supervisor.json
    .bugx/state/coverage-evidence.json
    .bugx/state/prep-record.md
    .bugx/logs/bugx.jsonl
    .bugx/artifacts/

生命周期目录的权威产物包括 events.jsonl、conclusion.json、report.md、integrity.json。不要把完整 artifact、credential、wire log 或未脱敏源码放入模型上下文或云端 payload。

### 1.2 CLI 版本检查与更新契约

公共入口只有：

```text
bugx --version
bugx update --check --root <ROOT>
bugx update --root <ROOT> [--channel stable|alpha] [--allow-downgrade]
```

不要生成 `bugx version --check`。`--check` 是只读联网检查：退出码 `0` 表示当前无需更新，`1` 表示有更新/必须更新，`2` 表示网络、服务端元数据或安装来源检查失败。`bugx update` 具有全局写入副作用：它识别当前 pip/npm 安装来源，按服务端返回的 `channel + artifact_kind + release_id + checksum_sha256` 下载制品，随后刷新全局 engine、已登记 Host 和目标 root 的 installation-only doctor。

AI 必须按以下证据门判断：

```text
version response with update_available -> 只说明存在候选更新
release_id/checksum/artifact missing   -> STOP, report BLOCKED or UNVERIFIED
source/editable installation           -> STOP, do not overwrite source
pip/npm installer exit 0               -> 仍未完成，继续读取 installed version
engine/host/doctor failure after install -> RECOVERY_REQUIRED
receipt final_state=COMPLETE + doctor success -> update COMPLETE
```

Windows 上若用户级 wheel 与旧系统级 `share/bugx-agent` 并存，`asset_root()` 必须先解析当前
`bugx-cli` distribution 的资源，再回退系统 data-files；看到旧 Build ID 时不得把它当成当前
CLI 的安装源，也不得用它覆盖更高的全局引擎。

更新收据位于用户级 `~/.bugx/update/receipts/<operation_id>.json`，成功状态必须为 `COMPLETE`；安装器之后任一阶段失败时状态为 `RECOVERY_REQUIRED`。没有真实 receipt、版本核对、引擎 integrity 和 doctor 结果时，不得把 CLI 返回 0、模型文本、Task 打印、服务端配置写入或工具列表当作更新完成。启动阶段的版本检查是后台提示，`BUGX_DISABLE_VERSION_CHECK=1` 或 cloud-config 的 `disable_version_check=true` 只禁用启动提示，不禁用显式命令。

### 1.3 Host capability sets

    install_adapters =
      agnescode, antigravity, chatgpt, claude, cline, codex, dsh, funide, kimi,
      minimax, opencode, pi, qoder, trea, workbuddy, zcode

    ready_engine_plugins =
      opencode, codex, cline, dsh, claude, pi, zcode, kimi, minimax, antigravity, agnescode

    acp_ready = agnescode, antigravity, cline, kimi, opencode
    acp_todo = chatgpt, workbuddy, qoder, trea, funide
    acp_unavailable = claude, codex, dsh, pi, zcode, minimax

    autonomous_cli_choices =
      claude, codex, opencode, dsh, pi, kimi, cline, zcode

    desktop_registration =
      claude, kimi, zcode, qoder, minimax

autonomous host 选取不是独立事实表：baseline 五宿主来自 `AUTONOMOUS_HOST_BASELINE`
（`interfaces/cli/bugx_cli/hosts/registry.py`），并自动追加声明了
`worker_runtime.autonomous_cli` 的插件（cline、zcode）。`bugx autonomous --host` 与
`bugx goal --host` 的 choices 均由 `autonomous_host_names()` 动态派生。pi 无原生 Task
（native_task=False），自治走无头通道 `pi --approve -p <prompt> --mode json`
（`core/hosts/pi.py` engine_run.autonomous_argv）。

`desktop_registration` 宿主（claude、kimi、zcode、qoder、minimax）在执行 `bugx install` 时自动完成各自当前契约要求的用户级注册，无需用户在应用内手动点击安装。Kimi Code 1.4.x 的唯一注册面是
`<KIMI_CODE_HOME>/plugins/managed/<id>` 与 `<KIMI_CODE_HOME>/plugins/installed.json`
（未设置 `KIMI_CODE_HOME` 时为 `~/.kimi-code`）；旧 `daimon-share`、`plugin-meta.json` 和
个人市场文件不是当前 Kimi Code 读取路径，安装器不再写入。Kimi 注册通过
`interfaces/cli/bugx_cli/commands/plugin_kimi.py::_ensure_kimi_plugin` 实现，能力声明为
`user-agent-registration`；Kimi 的 managed 插件注册由该宿主专用注册器完成，不能落入
ZCode 专用的 `distribution-plugin` 分支（`core/hosts/kimi.py`）。

Qoder `install --agent qoder` 合并根 `.mcp.json:mcpServers`，以项目绝对 `--root` 登记 `bugx-supervisor`、`bugx-supervisor-worker`、`bugx-docs`。Windows 使用安装进程的绝对 Python `.exe` 与 `-P -m bugx_cli.cli`，npm 额外传递该安装的 `PYTHONPATH`、`BUGX_ASSET_ROOT`、`BUGX_CLI_SOURCE`。Doctor 和运行时读取项目级 `.qoder/settings.json` 与根 `.mcp.json`，根定义覆盖同名 server。Doctor 将相对 `--root` 或过期 Windows 启动器判为过期。工具调用需要 Qoder 新会话加载配置并完成项目级批准。卸载依据 Claude / FunIDE / Qoder 的剩余登记决定是否保留根配置中的共享条目。

MiniMax Code 3.1.17 使用 `<DATA_DIR>/agents/bugx/agent.md` 注册 `BugX` 自定义 Agent。
`<DATA_DIR>/plugins/bugx/.minimax-plugin/plugin.json` 的 `author` 为字符串，
`servers.mcp.json` 用 `schemaVersion: 1` 和 `mcpServers` 声明插件 MCP。插件提供
`bugx:bugx-worker` 和 `bugx` Skill。默认 `<DATA_DIR>` 为 `~/.minimax`，可用
`MINIMAX_DATA_DIR` 隔离。主 Agent 的 `x-mavis.avatar` 相对引用同目录的 `bugx-avatar-512.png`。
`minimax_user_agents` 回执含主 Agent、头像、Worker 与 MCP 路径。
注册验证覆盖两个入口，实际 MCP 调用、headless 和标准 ACP 尚未验证。

`bugx install --agent antigravity` 安装用户级 `~/.gemini/config/plugins/bugx-review`。
原生主 Agent 为 `BugX`（插件内部 slug 为 `bugx-review`），同一插件还包含 `bugx-worker`、审查 Skill 和官方
`assets/bugx-avatar.svg`；插件级
`mcp_config.json` 保持空容器，项目级 `.agents/mcp_config.json` 承载
`bugx-supervisor`、`bugx-supervisor-worker` 与 `bugx-docs`。安装回执键为
`antigravity_user_agents`，`doctor` 必须调用 Antigravity 专用校验读取用户级受管文件，不能
把 `.gemini/config/plugins` 拼成项目相对路径。Windows `agy 1.5.16` 已由真实
headless Agent 调用 docs MCP 并返回隔离 README 的引用哈希；错误项目作用域被拒绝。
ACP `initialize → session/new` 已用直通引擎启动器真实建立会话；本轮没有得到可解析的
Supervisor/Worker `tool_call` 完成证据，日志出现 `AgentBasePath is not set` 与
`unsupported_tool: call_mcp_tool`。真实 headless Agent 的 docs MCP 调用已通过，但这
不能替代 ACP 工具级证据。完整 claim→worker-start→checkpoint→post 生命周期仍为
`UNVERIFIED`，不得标记为 `COMPLETE`。

### 1.4 宿主逐平台路径审计（2026-11-24）

本轮按宿主逐一检查 Windows、macOS、Linux 的用户级注册根、桌面应用候选和失败语义：

| 宿主 | Windows | macOS | Linux | 结论 |
| --- | --- | --- | --- | --- |
| MiniMax Code | `~/.minimax/agents/bugx/agent.md` 和 `~/.minimax/plugins/bugx`；MiniMax Code.exe 与逻辑盘 Program Files 候选 | 同布局，应用包候选 | 同布局，XDG/PATH 候选 | Windows 3.1.17 包解析与注册验证通过，其他平台未取证 |
| Kimi Code | `KIMI_CODE_HOME`/`%USERPROFILE%\\.kimi-code`；补齐 `Kimi Code.exe`、非系统盘逻辑驱动器与 `.exe/.cmd/.bat` CLI | `KIMI_CODE_HOME`/`~/.kimi-code`；补齐 `Kimi Code.app` 候选 | `KIMI_CODE_HOME`/`~/.kimi-code`；补齐 `kimi-code` 桌面候选 | `AUDITED_OK`（当前协议） |
| Claude Code | `~/.claude`；补齐逻辑盘 `Program Files` 候选 | `~/.claude` + `~/Library/Application Support/Claude` | `~/.claude` + XDG/Claude 候选 | `AUDITED_OK` |
| ZCode | `storage.dir` 现在接受 Windows 绝对路径；逻辑盘候选补齐 | `~/.zcode`/`storage.dir` | `~/.zcode`/`storage.dir` | `AUDITED_OK` |
| Trae | 补齐 Trae/Trae CN 的逻辑盘 `Program Files` 候选与变体识别 | 应用包与 `~/Library/Application Support` | XDG 桌面入口与 PATH | `AUDITED_OK`（发现层） |
| Cline / Qoder | 用户根 `~/.cline` / `~/.qoder`，不依赖盘符 | 同一用户根契约 | 同一用户根契约 | `AUDITED_OK` |
| WorkBuddy / FunIDE | `APPDATA`、`LOCALAPPDATA`、数据目录覆盖和有界进程反查 | 应用包/用户数据目录 | XDG/`/proc` 受控反查 | `AUDITED_OK`（发现层） |
| OpenCode / Codex / Pi / ChatGPT / DSH | 各自项目或受控用户配置；MSIX/DSH 候选不做全盘扫描 | 原生应用/用户配置候选 | PATH/XDG/桌面入口 | `AUDITED_OK`（路径静态审计） |

所有声明 `user-agent-registration` 的适配器都必须返回结构化注册回执；返回空值会被安装事务直接阻断，不得再出现“命令成功但用户级插件未注册”的静默结果。当前轮实际运行环境为 Windows；Kimi Code 已完成实际 managed/installed 注册与项目 MCP 验收，macOS/Linux 的宿主真机闭环仍需各自宿主现场证据，不能由本表静态审计替代。

DSH 安装契约补充：`ecosystem/integrations/dsh/ui` 始终先发布到
`DSH_HOME/plugins/bugx-ui`。仅当 `dsh` launcher 位于已探测的 DSH Desktop 安装根下时，
安装器才调用 `dsh plugin --profile web add`；普通 npm/Python launcher 的插件管理请求
不得执行，失败时保持 `staged`，不影响 DSH Agent、Skill、MCP 或 preset。Windows Desktop
运行时的 preset 名称优先从固定候选路径下的 `resources/app.asar` 头部读取；候选只包括
`%LOCALAPPDATA%\Programs`、`%LOCALAPPDATA%`、`%PROGRAMFILES%`、`%PROGRAMFILES(X86)%`
和 `%PROGRAMW6432%` 下的 `DeepSeek Harness` 目录，不做全盘扫描。
ASAR 包名探测递归读取其 `files` 文件树，匹配任意层级的
`node_modules/@deepseek-ai/<package>`，覆盖 Desktop 当前 `files/dsh/files/node_modules`
布局；不能确认包名时不得猜测，回退到 profile 实际目录。

这些集合不可互换。install adapter 不代表 engine plugin、ACP 或 autonomous CLI 可用。插件 ready 需要六面契约：identity、install、engine_run、worker_runtime、mcp_write、acp。ACP 状态含义：

- ready：实现了 ACP argv/启动契约；
- todo：仓库尚未适配，probe 必须 BLOCKED；
- unavailable：Host 产品没有 ACP entry，probe 不适用。

宿主注册表 schema 版本 `REGISTRY_VERSION = 4`（`core/hosts/__init__.py`）：v3 新增
`WorkerRuntimeFacet.worker_timeout_seconds`，v4 新增 `McpWriteFacet.config_format` 取值
`yaml_patch`（`core/hosts/base.py`）。

- `worker_timeout_seconds`：单 claim 墙钟预算基数，`float | None`，导入期 validate_plugin
  强阻断非正数。当前 cline 与 pi 均声明 `600.0`（`core/hosts/cline.py`、
  `core/hosts/pi.py`）：cline 作为 `resolve_worker_timeout` 的 `base_override`，pi 经
  `core/host_runtime/pi.py` 预算透传执行器注入（见下）；未声明宿主为 None，tier 阶梯
  行为不变（`core/supervisor/worker_runtime.py`：recon 180 / exploit 600 / verify 300 秒，
  重试退避 `1.5**(attempt-1)`，钳制 10–1800 秒，exploit 保底 ≥600）。
- `config_format ∈ {json, toml, yaml_patch}`（`core/hosts/base.py` 导入期 validate_plugin
  强校验）：`json` 走通用 JSON 骨架；`toml`/`yaml_patch` 是非 JSON 形态，通用 JSON
  骨架不得接管，导入期强制 bootstrap/remove/inspect 三件套由插件自有，否则导入即阻断。
  dsh 用户级 cordis patch（`container_key="insert"` 顶层列表，无「名字→条目」映射形态）
  声明 `config_format="yaml_patch"`（`core/hosts/dsh.py`）：探针/诊断按声明分派
  `yaml.safe_load`——没有该声明时 YAML 被当 JSON 解析失败，会结构性误判「未配置」。
- `requires_absolutize` 声明性过期（`interfaces/cli/bugx_cli/commands/plugin.py`）：声明
  该 facet 的宿主（pi/claude 等，stdio MCP server 由宿主进程 spawn，cwd 不是工作区），
  磁盘条目 argv 携带相对 `--root`/`--db` 即判声明性过期并重写为绝对根；绝对形态按
  root 感知 pinned 语义比对，同根重跑逐字段相等 ⇒ unchanged（幂等，不依赖 CLI 进程
  cwd）。
- pi 执行器 `core/host_runtime/pi.py`：预算透传执行器，不复制审计逻辑（心跳、artifact、
  差分验证、checkpoint、UCB1 记账仍由 `worker_runtime._execute_autonomous_worker`
  独家承载）。注入通道是 `resolve_worker_timeout` 的设计扩展点 `BUGX_WORKER_TIMEOUT`
  环境变量（`core/supervisor/worker_runtime.py`）：预算只在委托调用窗口内生效，入口
  保存、`finally` 恢复，异常路径同样恢复，窗口外进程 env 与调用前逐字节一致；窗口内
  profile 预算优先于用户全局 `BUGX_WORKER_TIMEOUT`（宿主事实比全局 env 更具体）。
  语义与 cline `base_override` 一致（退避、钳制、exploit 保底同公式）。

### 1.4 Optional code-index MCP

项目需要结构化代码导航时使用已锁定的 `codindex` server：

    bugx mcp-runtime install --timeout 30
    bugx mcp-runtime start --server codindex

它通过本地 stdio 提供代码索引、调用图和影响面查询。要求 Node.js >=22.5；保持默认
`node:sqlite` 驱动，不设置 `CODINDEX_SQLITE=better-sqlite3`。MCP 配置不得改成运行时
`npx` 或未固定版本的包命令；缺少 lock runtime 时返回 BLOCKED/UNVERIFIED，不把配置存在
误报为服务已加载。

### 1.5 ACP probe status vocabulary

`scripts/smoke/smoke_host_acp.py` 的宿主探活只有四个机读状态；词值是下游
（adversarial_matrix_verify / layer_ceiling_probe）按值比对的契约，禁止改名或新增同义词：

    VERIFIED     探活通过：本次在带真实运行证据——host/worker 双通道结构化 MCP 工具调用，
                 或由本次探活刷新的双通道 Session Proof。唯一代表「探活通过」。
    CONFIGURED   仅 dry-run 配置体检通过：宿主配置里 host/worker 双通道 MCP 注册齐备，
                 未拉起宿主进程；不冒充 VERIFIED。
    UNVERIFIED   已登记环境类成因（AUTH_REQUIRED、PROVIDER_CREDITS_EXHAUSTED、
                 MODEL_UNAVAILABLE、PROVIDER_RATE_LIMITED、PROBE_BUDGET_EXHAUSTED、
                 MODEL_NOT_PINNABLE / MODEL_NOT_OFFERED 等登记项），或探活跑完但无本次
                 在带证据；修复环境或补齐证据后可重跑。
    BLOCKED      可执行阻断：参数非法、宿主 CLI 不可用、探针锁冲突、进程/协议层失败、
                 探活项目构建失败。

exit gate：`_EXIT_OK_STATUSES = {VERIFIED, CONFIGURED}` 返回 0，其余一律 1。失败码处置表
`_ACP_FAILURE_DISPOSITION` 必须完备，未登记失败码 fail-closed 到 BLOCKED，禁止静默降级。

MCP 配置判定按宿主 mcp_write 契约面分支（A1）：`_probe_mcp_write_facet` 读取宿主 MCP
写入面；用户级作用域宿主（`project_relpath=None`）走 `_user_scope_mcp_containers` /
`_verify_user_scope_mcp_config` 的用户级容器探查，project 级宿主走项目级注册检查。
`mcp_configured` 判定与 A1 装配门同一事实源，禁止跨作用域误报。

容器解析按 `mcp_write.config_format` 声明分派（`scripts/smoke/smoke_host_acp.py`）：
`json` 走 JSON 容器解析，`yaml_patch` 走 `_yaml_patch_container`。解析工具缺口（如
PyYAML 缺失）抛 `_UserScopeParseUnavailable`，调用方如实降级 UNVERIFIED/BLOCKED，
不得把工具缺口当成「未配置」的结构性假阴性。dsh 的用户级 YAML patch 含 bugx
host/worker 双通道条目 ⇒ `mcp_configured=True`，同样经该事实源判定。

### 1.6 Provider availability facts

宿主通道可用性事实共享提取：`core/host_runtime/provider_facts.py`（依赖图叶子，仅标准库，
可被上下游同时导入）。受控事实库路径：

    ~/.bugx/facts/cline_provider_facts.json    PROVIDER_FACTS_FILE，存储侧 0600 落盘

- `load_provider_availability_facts`：读取 providers 事实；缺失/损坏 fail-open 返回空，不阻断裁决。
- `credits_exhausted`：判定通道 0 余额/credits 耗尽。优先级：通道条目显式声明（facts/条目/
  settings 下的 `credits_exhausted` 布尔值或 `balance`/`credits` 数值）> 事实库声明 >
  无事实（False，行为不变）。
- 消费方：`host_provider_arbitrator`（聚合层守卫：携带耗尽事实的通道整体降级到健康通道之后，
  healthy + degraded 分区排序，不剔除——仅剩耗尽通道时仍可作为兜底返回）；
  `host_evaluators` 评估层守卫（`is_free_model` 命名短路前查事实，耗尽通道跳过
  TIER_1_FREE 短路，按常规梯队参与排序）：`evaluate_cline_provider` 之外，
  `evaluate_zcode_candidates` / `evaluate_opencode_candidates` / `evaluate_pi_candidates`
  均接受 `external_facts` 注入（None 时回 `load_provider_availability_facts()`），
  四宿主评估共用同一受控事实库，缺失/损坏 fail-open。
- 派发层 fail-closed（`core/launch_phases/worker_orchestrator.py`）：
  `FAIL_CLOSED_PROVIDER_HOSTS`（当前仅 pi）在 `workers --execute` 无任何显式 provider
  时走 `arbitrate_host_provider` 仲裁取最优真实通道；仲裁为空或 mock-only 时
  `_resolve_fail_closed_provider` 返回显式错误终止派发，绝不静默落 mock。显式
  `--provider` 或 runtime provider 100% 尊重，不仲裁；非在册宿主与提示词模式
  （execute=False）行为不变。新宿主加入前必须完成同型集成取证，不得凭宿主名猜测。
- 用户充值或事实变化后，删除或改写该文件即恢复通道优先级；显式通道声明优先于事实库。

## 2. CLI contract

### 2.1 Public entry

项目生命周期必须使用：

    bugx engine --root <project> pre
    bugx engine --root <project> status [--json]
    bugx engine --root <project> workers ...
    bugx engine --root <project> post ...

顶层 bugx pre、bugx status、bugx workers、bugx post 等旧入口不执行，会返回迁移错误。bugx engine 不带 engine args 时默认为 status。engine CLI 为全局 engine 进程设置 project root、global engine root 和 CLI version 环境。

CLI 的布尔选项统一按开关解析：`--force`、`--json`、`--execute` 等选项直接出现即为
启用，省略即为关闭，不生成 `--force=True`、`--json=False` 或 `force=True`。MCP/内部
Python API 的布尔字段仍按各自协议传递，不能把协议字段拼接到 CLI 参数后。

帮助可通过 `bugx help <COMMAND> [ACTION]` 查询。显式项目根在业务分发前解析，重复的 `--root` 会拒绝。引擎先校验结算参数，快照预览不创建 CLI 日志，报告共享策略由启动时的 `--report-policy` 决定。在用户主目录（`~`）不带 `--root` 执行 `bugx install` / `bugx doctor` 时进入用户全局模式：仅装配/校验 `~/.bugx/core`、`~/.bugx/knowledge/global.sqlite3`、全局核心依赖（`codebase-memory-mcp==0.12.1` 与 `tree-sitter-language-pack==1.14.3`，二者在 `install` 与 `doctor --repair` 直接安装、绝不降级放行）及宿主用户级资产（DSH 全局 `cordis.patch.yml` 写入动态无 `--root` 参数），绝不将 `~` 初始化为项目或删除 `~/.bugx/core`；项目级 `hosts.json` + `startup-todo.md` 就绪后即允许等待首次 `pre` 确认云端模式。可选能力（`playwright`/`postmcp`/`codindex`/UI/TUI/效率工具/`MCP 离线 runtime`）延迟至实际进入项目审计时按技术栈由 AI 与用户共同决策启用；修复 MCP 离线 runtime 使用 `bugx mcp-runtime install`，清理遗留 MCP 会话使用 `bugx mcp prune --root <ROOT> --yes`。安装载荷缺失时通过显式的 `bugx update --repair-assets` 恢复，CLI 输出端默认强制 UTF-8 编码与替代码保护，确保在 Windows GBK 及无终端管道下稳定运行；UI 视觉挖掘环境要求 Node >= 20。

帮助调用 help、--help、-h 必须只读。pre/audit 明确拒绝 --allow-degraded；不要通过该参数请求静默降级。

### 2.1.1 Dev mode contract

Dev mode has two independent layers:

| Input | Contract | Required guard |
|---|---|---|
| `BUGX_DEV_MODE=1` | When the current process is a real BugX source checkout, resolve the engine root to `<checkout>/core` so source edits are used without reinstalling | Checkout must have `.git` and `scripts/bump_version.py`; `core/bugx.md` and `core/launch_bugx.py` must exist |
| `BUGX_DEV_ASSET_ROOT=1` | Compatibility alias for `BUGX_DEV_MODE=1` | Prefer `BUGX_DEV_MODE`; exact value is `1` |
| `BUGX_GLOBAL_ENGINE_ROOT=<path>` | Explicit global engine override | Takes precedence over Dev engine; if present, do not claim source checkout is active |
| `BUGX_ASSET_ROOT=<path>` | Explicit asset candidate override | Not equivalent to Dev mode; validate source/installed asset provenance before use |
| `pre --dev` / `audit --dev` | Lifecycle shortcut, not engine-source selection | Defaults `autonomous=true`, `review-mode=mcp-auto`, `fix-mode=auto`, `polling=auto`, `commit-policy=round`, `session-recovery=resume`, `startup-action=start`, `strategy-plan=approve`; it skips strategy questions |

The first read-only proof for source Dev mode is:

    env -u BUGX_GLOBAL_ENGINE_ROOT BUGX_DEV_MODE=1 \
      bugx engine --root <project> path --name launch_bugx.py

The result must resolve to `<source-checkout>/core/launch_bugx.py`. A result under `~/.bugx/core`, a wheel, an npm package, or another directory means source Dev mode is not active. Do not continue to `pre` until the provenance is corrected.

Safe source verification keeps lifecycle mutation explicit:

    env -u BUGX_GLOBAL_ENGINE_ROOT BUGX_DEV_MODE=1 \
      bugx engine --root <project> pre \
      --fix-mode log-only --commit-policy manual --cloud-sync local

Only use `pre --dev` when the target is isolated, reversible, and the user explicitly authorized automatic repair and round commits. Explicit `--cloud-sync` or a saved profile overrides Dev's local cloud default. `--cloud-sync` is accepted through the engine argument passthrough and is also an `init` option; the two calls have different persistence semantics.

Dev mode does not bypass Git, version, MCP, credential, budget, evidence, or Host gates. The next action is determined as follows:

    engine path mismatch -> STOP, inspect process source and override variables
    pre exit 2          -> read BUGX_INTERACTION_REQUEST, wait for real choice
    pre exit 3          -> reinstall/load Host entry, restart Host, rerun pre once
    other non-zero      -> collect stderr/status/log, classify BLOCKED or RECOVERY_REQUIRED
    pre exit 0          -> startup gate only; continue lifecycle, not completion

No model text, Task print, `ready`, `claim`, or `pre` exit 0 can be reported as lifecycle completion. Completion still requires real Worker receipts, checkpoint, coverage/evidence, Finding verification, post and settlement.

### 2.2 Autonomous and goal

autonomous contract：

    bugx autonomous --root <project>
      --host {claude,codex,opencode,dsh,pi,kimi,cline,zcode}
      [--workers N] [--model MODEL] [--provider PROVIDER]
      [--max-turns N] [--max-wall-time SECONDS]
      [--agent-timeout SECONDS] [--coverage-manifest PATH]
      [--no-adaptive-wall-time] [--adaptive-baseline VALUE] [--json]

workers=0 means host-derived adaptive capacity. Explicit workers must satisfy the selected WorkerRuntimeFacet bounds. The command may run host installation first. model is not a credential; the current implementation uses a run-time OpenCode model override when applicable. Provider rate-limit is a persistent gate; do not delete provider-window state to bypass it. Successful exit requires result status complete; aborted, blocked, budget, timeout, or incomplete results are not success.

goal contract：

    bugx goal [TARGET_DESCRIPTION] [--root <project>]
      [--host HOST] [--target-coverage PERCENT]
      [--allow-open-findings] [--disallow-quarantine]
      [--max-turns N] [--max-wall-time SECONDS]
      [--agent-timeout SECONDS]
      [--max-unit-retries N] [--max-transient-retries N] [--json]

默认 host=claude、target-coverage=100.0、max-wall-time=14400.0、agent-timeout=600.0、unit-retries=3、transient-retries=5；默认要求无 open findings，quarantine 默认允许。任何放宽参数都必须出现在最终报告。

### 2.3 Audit and report

audit graph 可以返回 ready 与 awaiting_native_agent；awaiting_native_agent 表示等待真实 Native Agent，不是完成。audit deep 是一次性深度流程；报告仍以 receipt、Finding、coverage 和 gate 为权威。

report source 语义：

    project = authoritative project Supervisor state
    eval    = evaluation receipt directory
    auto    = eval fallback only when project source is blocked and eval exists

缺少项目 state 时，报告必须保留 NOT ATTESTED，不得制造 Finding、coverage 或 attestation。

## 3. MCP server contract

### 3.1 Server identity

    protocolVersion: 2024-11-05
    serverName: bugx-supervisor
    serverVersion: 0.1.5
    hostHeartbeatIntervalSeconds: 5.0
    transport: local stdio

Server 是有限请求处理器，不替 Host 长轮询，不自行启动 Worker，不进行隐式项目初始化。tools/list 的真实握手后写入 session proof；proof 必须绑定 scope、lifecycle、run 和实际会话身份。

### 3.2 Roles

host：

- Supervisor control plane；
- start、gate、finish、fail、recover、next-round；
- Finding verify/confirm；
- target rebinding、budget reset、checkpoint salvage；
- graph orchestration。

worker：

- claim、worker-start；
- tool-call、heartbeat；
- checkpoint、submit-finding；
- 受控 probe 和 worker graph checks。

shared：

- status、gate、coverage 和只读知识/观测工具；
- shared 不表示任意角色都可以执行所有副作用参数，仍按工具表 role gate。

Worker 必须通过 BUGX_NATIVE_AGENT_ID 或宿主等价机制传递真实身份。一个固定字符串不能代表多个 Worker。

### 3.3 Resources and prompts

Resources：

    bugx://state/status
    bugx://state/coverage
    bugx://state/coverage?offset=<n>&limit=<n>
    bugx://findings/recent
    bugx://knowledge/mistakes
    bugx://profile/target

Prompts：

    bugx-audit-explore
    bugx-fuzz-mutate
    bugx-repro-verify
    bugx-gate-decision

Prompt、resource 和 tools/list 只能提供上下文或协议事实；它们不构成生命周期完成证据。

### 3.4 Tool inventory by role

Host-only tools：

    bugx_supervisor_start
    bugx_supervisor_findings
    bugx_supervisor_reset_budget
    bugx_supervisor_rebind_target
    bugx_supervisor_sync_finding_decisions
    bugx_supervisor_verify_finding
    bugx_supervisor_confirm_finding
    bugx_supervisor_bootstrap_coverage
    bugx_supervisor_reopen_finding
    bugx_supervisor_admit_business
    bugx_supervisor_salvage_checkpoint
    bugx_supervisor_next_round
    bugx_supervisor_bind_context
    bugx_supervisor_finish
    bugx_supervisor_fail
    bugx_supervisor_recover
    graph_start
    graph_next
    graph_bind_agent
    graph_cancel
    graph_finalize
    bugx_mining_harden

Worker-only tools：

    bugx_supervisor_claim
    bugx_supervisor_worker_start
    bugx_supervisor_tool_call
    bugx_supervisor_heartbeat
    bugx_supervisor_checkpoint
    bugx_supervisor_begin_long_call
    bugx_supervisor_run_sandboxed_probe
    bugx_supervisor_submit_finding
    graph_context
    graph_run_check
    graph_checkpoint
    bugx_fuzz_concurrency
    bugx_fuzz_stateful
    bugx_verify_differential_poc
    bugx_ingest_coverage
    bugx_inspect_mobile_ui
    bugx_fuzz_input
    bugx_minimize_repro
    bugx_query_behavior_graph
    bugx_audit_web_conformance
    bugx_test_verify

Shared tools：

    bugx_supervisor_status
    bugx_supervisor_gate
    bugx_supervisor_coverage
    bugx_knowledge_route
    bugx_harness_status
    bugx_oracle_suggest
    bugx_lesson_retrieve
    bugx_tool_matrix
    bugx_incident_hints
    bugx_request_token_renewal
    bugx_efficiency_card
    bugx_get_stage_harness
    bugx_distill_evidence
    bugx_inspect_source
    bugx_scheduling_telemetry
    bugx_global_telemetry
    bugx_retrieval_metrics
    bugx_mining_explore
    bugx_mining_audit
    bugx_test_mutate
    bugx_retrieve_slice
    bugx_extract_ego_subgraph
    bugx_memory_query

总工具数为 66。工具名存在不代表当前 role、Host、scope 或生命周期允许调用；role gate 和 claim epoch 始终优先。

Web/UI 入口为 `bugx probe [--timeout-seconds <秒>] ui --root <项目> --url <URL>`。
外层看护默认 120 秒，允许显式设置到 1800 秒以内；它不替代页面/动作自己的超时。
`manifest.json` 绑定项目根、
run ID、Chromium、视口、原生缩放、截图哈希、axe-core 和场景结果。`dynamic-evidence.json`
是版本化清单引用。Web 标准检查器回读哈希、截图和观测结果。`DESIGN.md` 的 `bugx-ui`
提供 token、组件、对比度、重排、`journeys` 与 `keyboard`。`--journeys` 执行两类场景。
`--reference-url` 使用同配置的浏览器对照截图。`--repair-css` 默认预览，`--apply` 需要
回环目标，验证 `repair.json` 和二次收据，新增视觉退化会回滚。模型 `ADVISORY` 独立保存。

### 3.5 bugx-docs dual-scope server contract

- **Transport & Channels**：默认采用「远程优先（Remote-First）」策略直连只读官方官档端点 `/api/v1/mcp/docs?contract_version=<X.Y.Z>`；网络不可达或传入 `--docs-transport stdio` / `BUGX_DOCS_OFFLINE=1` 时自动无感降级为本地 `stdio` 网关 (`bugx mcp docs serve --root . --db .bugx/knowledge/docs.sqlite3`)。项目私有文档（`project:<id>`）严格本地隔离，永不上云。
- **Dual-Scope Isolation**：
  - `bugx:official`：优先读取 `~/.bugx/cache/docs/<contract_version>/<sha256>.sqlite3`（经 `sync.lock` 单飞锁、1.5s manifest 超时、`contract_version` 前缀校验、SHA-256 + `PRAGMA quick_check` 验签并原子切换 `current.json`，旧文件即时或 `.gc-pending` 延迟物理删除）；离线、未配置或服务端未就绪时自动回退至随包基线 `core/bundled_docs/`。
  - `project:<id>`：仅扫描当前工作区可控 Markdown（`README*`、`docs/**/*.md`、`core/**/*.md`），严格落盘在 `<ROOT>/.bugx/knowledge/docs.sqlite3`，绝不上云。
- **Section-Chunked Schema & Tools**：索引层采用 `document_sections` + `sections_fts (tokenize='trigram')`，提供 `resolve_library`、`search_docs`、`list_sections`、`fetch_doc`（按标题路径切分且保留嵌套 `###` 子标题，匹配失败返回规范 TOC 清单）。
- **Airgap & Orthogonality**：`BUGX_DOCS_OFFLINE=1`、`cloud.docs_remote_enabled=false` 或 `--mode local-only` 下保证 `bugx-docs` 零出网；`bugx-docs` 只做无状态只读操作指导 RAG，与 18 张表的结构化错题/经验台账 `lesson-ledger.sqlite3` 及语言数据集 `seed_corpus` 严格正交分离、互不合并。

Storybook 使用 `--storybook <base> --list-stories` 或 `--story <id>`，从 v4/v5 index 取证，
绑定 story ID、预览路径及 index 哈希。场景通过 `story` 选组件画布，`--reference-url`
指向参考 Storybook 基地址。`token_source` 将项目内 DTCG 或 Style Dictionary JSON 映射到
根 CSS 变量，收据保留文件哈希。失效引用、循环和越界路径都在采集前阻断。

## 4. Mandatory lifecycle algorithm

Host/Worker 编排必须实现以下顺序：

    1. Host runs pre and obtains project/polling/lifecycle/run context.
    2. Host calls bugx_supervisor_start.
    3. Worker calls bugx_supervisor_claim for one unit.
    4. Worker immediately calls bugx_supervisor_worker_start.
    5. Worker records every tool use with bugx_supervisor_tool_call.
    6. Worker renews lease with heartbeat during long work.
    7. Worker calls checkpoint with a stable checkpoint_id.
    8. Worker may submit candidate Finding; it cannot confirm it.
    9. Host independently verifies and then confirms/rejects the Finding.
   10. Host reads gate, coverage, findings and report state.
   11. Host calls next_round only when gate is unsatisfied and recovery/next work is valid.
   12. Host calls finish/post only after finish policy is satisfied.

要求：

- claim response execution_state=claimed，不是 running；
- claim/start 是两个独立证据；
- 单次 claim 默认一个 work unit；bundle allowance 只能来自本地 orchestrator 的明确契约；
- checkpoint retry 复用同一个 checkpoint_id，避免重复事实；
- lease epoch/claim epoch 不匹配时 fail-closed；
- Host 是 controller lease 的唯一控制方；
- next_round 返回 GATE_UNSATISFIED 时不得当作成功；
- 没有人工补写状态、补发命令或替 Agent 决策时，自治流程才可能进入终态。

## 5. Evidence and checkpoint contract

checkpoint payload 应绑定：

    checkpoint_id
    project_id / project_root identity
    polling_id
    lifecycle_id
    run_id
    round
    worker_id
    claim_epoch / lease
    findings
    coverage
    evidence
    notes

每个 evidence receipt 的最小可验证字段：

    command
    exit_code
    artifact_path
    artifact_sha256
    target_fingerprint
    timestamp
    worker_id
    run_id
    round
    lease
    status

evidence status 只有：

    passed, failed, error, skipped, blocked

Coverage status 是另一套枚举：

    pending, covered, blocked, unverified, partial,
    not_applicable, stale, removed, superseded, corrupted

covered 是 coverage value，不是 evidence status。pending、partial、unverified、stale、blocked 是 coverage debt；corrupted 是全局阻塞。缺少 artifact hash、target fingerprint、exit code 或身份绑定时，不能填 passed。

覆盖继承沿链溯源（`core/coverage/coverage_discovery.py`）：covered 判定带 `inherited_from`
备注时沿备注继续向祖先溯源。单跳直接采纳；多跳成功返回 `chain=<polling-id>/<lifecycle-id> -> ...`
供结算日志留痕。fail-closed 条件（不随链条放宽）：任一跳清单缺失/无法解析/scope 无 covered
判定；任一跳存在矛盾的本地非 passed 记录（自报 covered 却有 failed/error）；链条成环或深度超过
`MAX_INHERITED_CHAIN_DEPTH = 32`；链条终点自称本地产出却没有本地 passed 记录。继承绝不复制
证据文件、绝不改写字段，源记录 `target_fingerprint` 漂移与 `observed_at` 年龄信号原样保留。

双轨目录排除与渐进式发现（DOC-AIREV）：
- **Track 1（静态白名单）**：`symbol_index` 与探针层自动硬排除 `.git`、`node_modules`、`.tools`、`artifacts` 等已知通用依赖/构建产物；
- **Track 2（AI 语义扫尾）**：白名单不是万能的，白名单放行后的剩余顶层目录须通过 `coverage review-packages` / `review-apply` 由宿主 AI 基于证据逐一复审并移出非产品目录（持久化于 `.bugx/knowledge/coverage-review.json`）；
- **Tier-1 初始化骨架**：`bugx init` 默认以 `files` 模式建立轻量骨架避免大仓启动挂死；`symbol_index.audit_units` 采用 `.bugx/cache/symbol_cache.db` 持久化缓存与多核并发解析支持大仓全量运行。

run_sandboxed_probe 的边界是 argv-only、禁止 shell launcher/operators、timeout 0.5–60 秒和 artifact/evidence 记录；它不是 OS 文件系统 sandbox。AI 不得向用户宣称更强隔离。

探针包 `core/probes/__init__.py` 按 PEP 562 模块级 `__getattr__` 惰性加载
（`_LAZY_MEMBERS`）：BaseProbe 保持 eager（probe_registry 注册面依赖），其余探针成员
首次属性访问才 import；`from core.probes import X` / `from core.probes.X import Y` /
子模块直 import 三种消费形态语义不变。引擎门禁链不再为重型探针依赖支付 eager
import 税。

## 6. Finding verification and finish policy

Finding state machine：

    candidate -> verify -> confirmed
                       \-> rejected

submit_finding 只产生 candidate。verify_finding 由 Host 执行两条独立 positive cases 加固定 negative control，并记录 receipt/hash。confirm_finding 必须绑定 verify receipt；Worker 自报、模型判断或单个正例不够。

REFUTED 是结构性证伪终态，schema 强约束（`core/ledger/finding_lifecycle.py`，NR-5 fail-closed）：

    block_reason ∈ REFUTED_BLOCK_REASONS
      = non_reproducible | by_design | fixed_upstream
        | evidence_insufficient | dynamic_probe_cleared

并要求 `refuted_by` / `refuted_evidence` / `original_severity` 结构化字段。理由只允许取自
枚举，自由文本敷衍视为未提供；违规写入直接抛 `LifecycleError` 强阻断。旧格式（字符串
confirmation、空 block_reason、severity 塌缩 info）明确拒绝，补齐结构化字段后重判。

历史台账的原级已丢失时禁止补写合法枚举：`original_severity="unknown"` 必须同时携带
`severity_unknown_reason`，`refuted_strength()` 与 `refuted_validation_status="legacy"`
会把这类记录标为不可按证伪强度消费。`retriage-legacy` 只处理裁决文件中逐条给出证据引用的
记录（填合法原级还必须有 `severity_evidence`），未裁决记录保持 legacy 原样；
`reconcile()` 亦不再把历史 REFUTED 回补成 `unverified`。

Finding 定位按四键唯一匹配（`core/supervisor/finding_workflow.py`）：verify 预检、验证
落账与 `confirm_finding` 都把传入 key 与每条 finding 的 `fingerprint` / `id` /
`finding_id` / `_finding_key` 四键比对，唯一命中才通过；零命中或多命中一律
`InvalidCheckpoint` fail-closed。只放宽到唯一匹配，不放宽到模糊匹配。

派生 findings.json 结算前置校验（`core/launch_phases/finding_settlement.py`）：
`findings_json_stale_vs_authority` 对派生视图与 Supervisor 权威态做两条从严信号——
`supervisor_binding.findings_revision` 落后于权威 revision；权威终态（confirmed/rejected）
未在派生视图同态出现（落后或反向篡改）。命中即 stale：`refresh_stale_findings_json`
把旧文件改名为 `.stale-backup`（冲突追加时间戳，绝不覆盖）并放行 post auto_build 从
权威态重建；一致文件幂等不动。检查自身失败不放大阻断，交由快照校验兜底。

finish gate 需要同时检查：

- coverage 不是 false，且所需 coverage gate 已满足；
- 没有 open findings；
- Finding confirmation ready；
- 需要并发的 life 满足并发要求；
- 没有 budget/deadline blocker；
- 报告已经持久化；
- graph flow 的 recon、repro、review 有 digest，且没有 candidate bugs；
- repair_preview 还有 confirmed bug、patch/check 完成和 regression observation passed。

典型状态：

    RUNNING
    CHECKPOINT
    NEXT_ROUND
    STALLED
    INCOMPLETE
    COMPLETE
    BUDGET_EXHAUSTED
    FAILED

开放 Finding、confirmation pending、并发不足或 coverage debt 通常只能是 INCOMPLETE；预算或期限原因是 BUDGET_EXHAUSTED。不要通过 allow-incomplete 把 INCOMPLETE 改名为 COMPLETE。

## 7. Failure and recovery contract

| 观察 | 必须返回或保留 | Host 下一步 |
|---|---|---|
| ACP 未适配 | BLOCKED 或 UNAVAILABLE | 不伪造 ACP session；换 ready Host 或报告阻塞 |
| MCP role 错误 | FAILED/BLOCKED | 重新以正确 role 建立 session；不调用越权工具 |
| claim 后未 start | STALLED/INCOMPLETE | 重新派发或回收 claim；不要声称 Worker running |
| heartbeat/lease 丢失 | RECOVERY_REQUIRED 或 STALLED | Host salvage/recover；校验 epoch |
| checkpoint 失败 | CHECKPOINT/RECOVERY_REQUIRED | 用同 checkpoint_id 重试或 Host salvage |
| MCP/tool error | FAILED 或可观测重试状态 | 记录错误与退出码；超过 retry 转 ABORTED/BLOCKED |
| provider rate limit | BLOCKED | 等待持久化窗口或按明确输出恢复；不删 window |
| 预算耗尽 | BUDGET_EXHAUSTED | 由 Host/用户确认后 reset-budget 或结束 |
| coverage debt/open Finding | INCOMPLETE | 新轮次、verify/confirm 或明确报告债务 |
| 宿主输出截断 | UNVERIFIED/ERROR | 缺失 receipt 字段不能补写 passed |
| post/share 失败 | RECOVERY_REQUIRED | 读取 report-consumption 和 post receipt，走 recovery |
| owner 进程死亡后跨会话 --final | aborted + successor_closeout_admitted 留痕 | 证据齐全才 admit（见下）；无死亡证明 fail-closed 拒绝 |
| serial_authorization 身份漂移 | BLOCKED | runtime_run_uuid 必须是 canonical UUID 或引擎 lifecycle run_id 形态 |

跨会话 owner_cancelled 收口（E1 观察 #3，`core/launch_phases/post_phase.py`
`_supervisor_owner_cancelled_proof`）：owner 进程死亡后 runtime 被孤儿裁决收敛为
aborted，「本会话 pre 授权」跨会话不可达，诚实 INCOMPLETE 收口永久不可达。合法接手 =
以下证据全部在场，任一缺失 fail-closed：1) runtime 为 aborted 终态；2) 项目布局下
runtime 的 lifecycle_id 等于 current.json 当前绑定；3) Supervisor 权威链在场
`operation=lifecycle` / `gate=owner_cancelled` 阻断事件（D27 宿主取消回传，即 owner
死亡证明）；4) 链身份绑定同一 lifecycle（round_id 相等且 run_id 在允许形态集合内）且
会话代际一致。admit 后向 runtime 追加 `successor_closeout_admitted` 事件留痕。

serial_authorization lifecycle run_id 形态：引擎 lifecycle run_id 由
`_expected_supervisor_run_id`（`core/launch_phases/supervisor_bridge.py`）派生为
`lifecycle-<session_id>-<lifecycle_id>`（session_id 即 polling_id，消除跨 polling 撞键；
无 polling 上下文的遗留单轮布局回退裸 `lifecycle-<lifecycle_id>`）。
`core/supervisor/serial_authorization.py` 强制 runtime_run_uuid 为 canonical UUID 或该
形态；Supervisor run_id 与 runtime 采纳形态必须一致，身份映射不清时不得宣称完成。

恢复会话结算语义（`core/supervisor/budget_governor.py`，三维度）：维度 1 在把 expired
报成重试债前，先检查各 expired claim 的单元是否 100% 沿链继承覆盖（manifest 行带
`inherited_from` 备注 + 溯源可达 passed）；全继承的 claim 标记
`superseded_by_inheritance`（hash-chain 事件 `expired_claim_superseded_by_inheritance`
留痕）并移出 expired 集合不再重派，任一单元未覆盖/伪造/溯源失败则整包保留进重派
路径。维度 2 只豁免 `require_parallel` 的并发证明债（`_RECOVERY_PARALLEL_PROOF_REASONS`）：
`_recovery_closeout_exemption` 三条件全真才豁免并落 `parallel_gate_inherited_closeout`
留痕——① Supervisor 权威链在场 `operation=lifecycle` / `gate=owner_cancelled` 死亡
证明；② 覆盖普查全部清单行沿链继承覆盖或历史 covered，且本轮无新增 passed 证据；
③ owner 死亡锚点后 `found_total` 增量为 0（finding 无 `origin_receipt.sealed_at`
不可归属时同样不成立）。fail-closed 红线：三条件缺一即拒绝；探针/清单/证据基础设施
故障一律维持原门禁（legacy 行为），只在快照留诊断字段，绝不借故障放宽。

report consumption phase：

    OPEN -> POSTING -> SETTLED
                   \-> RECOVERY_REQUIRED

普通 status、claim 不会默默恢复损坏状态；recover 是 Host 显式动作。任何失败必须进入 COMPLETE、ABORTED、BLOCKED、RECOVERY_REQUIRED、INCOMPLETE、BUDGET_EXHAUSTED 或 FAILED 等可观察状态，不得无限等待。

## 8. RSI autonomous loop ledgers

`scripts/verification/rsi_self_reflection.py` 的自治循环按 `DIMENSION_PIPELINE` 顺序流转：
static_scan → runtime_e2e → capability_audit → performance_baseline → pain_mining →
capability_expansion → 回到 static_scan。循环由以下台账驱动（路径相对仓库根）：

    state/rsi-loop/task_ledger.json        TASK_LEDGER 能力工作台账
    state/rsi-loop/pain_signals.json       PAIN_SIGNALS_LEDGER 痛点信号
    state/rsi-loop/upgrade_plan.json       UPGRADE_PLAN_LEDGER 升级方案
    .bugx/rsi-loop/question_stats.json     QUESTION_STATS_LEDGER 题目退役统计
    .bugx/rsi-loop/time_ledger.json        TIME_LEDGER 耗时账本
    .bugx/rsi-loop/startup_baseline.json   time_model 性能基线（within_budget 判定）

TASK_LEDGER 双动作 `_TASK_LEDGER_ACTIONS`（只此两个）：`degrade_contract` = 契约存在
（expansion 面）；`verify_pass` = 真机验证闭环（audit 面，桥接进 `_verified_capabilities`）。
`_record_task_done` 幂等：同 module+action+status=done 不重复写。契约存在不等于验证通过，
两个动作不互相替代（F-328 防重复触发的单一真源）。

耗时预算按场景分档（F-P3）：`TIME_PROFILES` = interactive 0.5h / ci 8h / overnight 24h /
weekly 168h，`ACTIVE_TIME_PROFILE = "ci"`。派生规则：大型 = 档位×7，小型 = 档位÷8（下限
0.1h）；权威判定走 `_budget_hours_for()`（读激活场景档）。`PRODUCT_IDENTITY.time_budget`
的 `*_budget_hours` 字段只是同公式派生镜像（保留规模分界 small 1000 / medium 200000 /
large 2000000 行、`sample_module_minutes=9`、`assumed_concurrency=2.1` 实测校准）；
外推挖掘时长超预算即输出 P0 缺口并降级策略。

思考门禁情境题库 `QUESTION_REGISTRY`：每题字段 id / stage / text / applies / evaluate，
可选 `required` 与 `blocking`（默认 True）；stage 取值 `preflight`（执行前）与
`postflight`（执行后）。选择规则：stage 匹配 + 未退役 + applies(ctx)；维度声明的
`required_questions` 强制保底注入。机械化进化退役：非 required 且 blocking 的题，累计评估
≥ 20 次（`_QUESTION_RETIRE_EVALS`）且从未阻断（blocks==0）→ 写入 question_stats.json 的
retired；required 题永不退役；账本损坏视为无退役（宁可多问，不少问）。
`pre-time-budget`（时长在用户预算内）与 `pre-waste`（无未消灭的耗时浪费）是 preflight
required 题（仅 performance_baseline 维度 applies），构成执行前的耗时/浪费双闸。

pain_mining 维度执行器：对 `PRODUCT_IDENTITY.pains` 逐痛点检索 HN Algolia
（`https://hn.algolia.com/api/v1/search`，免认证，tags=story，hitsPerPage=5，超时 10 秒），
命中痛点关键词的条目落账 pain_signals.json 且 `bugx_coverage=uncovered`（升级 covered 需要
证据）；同一 URL 被多个痛点命中时 matched_pains 并集合并；网络不可达 fail-open 记 skipped，
不阻断循环。

## 9. Interaction contract

待回答问题由 Host/用户控制：

    bugx interaction list --root <project> --json
    bugx interaction show <question_id> --root <project> --json
    bugx interaction answer <question_id> --value <value> --root <project> --json
    bugx interaction cancel <question_id> --root <project> --reason <reason> --json

AI 不得猜测 data sharing consent、report policy、tooling choice、repair strategy、destructive action 或 recovery decision。未回答问题必须保持 BLOCKED/待交互；回答需要真实 question_id 并写入状态。

信箱消费语义（F-RSI-20261118-001）：`answer` 写入的答案在宿主重跑 pre 时被对应门禁消费。cloud_sync/report_policy/strategy_plan 均走统一信箱通道（TTY 与非 TTY 对称）；strategy_plan 取值 `approve_and_start|customize|save_and_later` 与 `--strategy-plan` flag 等效，显式 flag 优先于信箱。blocked 交互环境下 report_policy 维持安全默认 local（信箱答案不消费，防陈旧值放大外发）。

## 10. Privacy and network contract

- model name 不等于 Provider credential。
- auth status 默认只显示存在性和状态；auth verify 可能网络访问。
- credentials 默认在用户级 BugX 路径；不得读取、打印、上传 token/refresh token/API key。
- cloud status 是本地状态查询；sync、upload、log-report、非 dry-run feedback 是显式网络动作。
- sync scope 为 project、global、all；未指定时读取用户级默认，显式 scope 优先。
- sensitive policy 为 block、redact、allow；detail mode 为 full、summary；data sharing false 硬阻断 cloud push/pull。
- 发送前要记录 consent、scope、detail mode、sensitive policy、payload 摘要和网络结果。
- 不要把 .bugx/、MCP wire log、业务源代码或完整 artifact 放入公开模型上下文。
- 环境变量设置和作用域以[环境变量参考](../reference/01_ENVIRONMENT_VARIABLES.md)为准。`BUGX_PROJECT_ROOT` 是项目根候选，`BUGX_CLOUD_API_BASE` 是远端地址覆盖，`BUGX_GLOBAL_*` 是用户全局范围；不能互相替代。
- `BUGX_INTERACTION_REQUEST` 是引擎输出，不是 AI 可自行填写的配置；`BUGX_NATIVE_AGENT_ID`/`BUGX_MCP_SESSION_PROOF` 是身份或 session 证据，不得伪造。`BUGX_NON_INTERACTIVE=1` 或 `BUGX_HEADLESS=1` 不代表同意 gate；`BUGX_ACCEPT_DATA_SHARING=1` 只有在用户明确授权后才能设置。

### 10.1 Cloud egress matrix

把云端能力按数据面区分，禁止用一条路径的成功替代另一条路径的证据：

| path | entry | payload | scope/default | network/local effect | completion evidence |
|---|---|---|---|---|---|
| knowledge sync | `bugx cloud sync` | V2 knowledge records（patterns/incidents/tool experience/consumption/tombstones；project 层有本地过滤） | `project|global|all`；显式 `--scope` 优先，省略时读 `cloud.sync_scope_default`，未配置默认为 `project` | 双向；每层 `pull baseline → push → pull`，写 SyncStore/outbox/inbox/conflict/dead-letter | 每层 summary + `cloud status --json` cursor/error/queue/conflict 收敛；0/`ok` 不足够 |
| project profile | `bugx project sync` | `wire_project_profile(profile)` 的紧凑项目画像/代码指标 | 当前 root；无 knowledge scope | 单向网络，登记/写 `<ROOT>/.bugx/cloud-project.json`；完整 profile 留本地 | JSON `status=ok`, `project_id`, `synced=true`, remote result |
| telemetry | 普通 CLI 命令每日最多自动记录一次安装/心跳；`bugx telemetry flush|upload` 显式排空队列 | 独立飞轮 outbox 的脱敏事件/metrics；安装事件只带作用域化 installation ID、匿名 device ID、版本/渠道/制品类型/平台；strict 会缩减字段 | 用户级 outbox；batch default 20；未配置时门禁默认开启 | 单向；成功发送项从 outbox 清除，失败保留/递增 attempts；`telemetry.data_sharing=false` 或 `BUGX_TELEMETRY_ENABLED=0` 时本地/远程均停 | 启用时看 `telemetry inspect --latest --json`、egress audit、flush 的 `sent/remaining` 和服务端安装统计；关闭时 `flush` 不联网且返回 0 只表示跳过 |
| diagnostic report | `bugx cloud log-report` | 诊断日志尾部；default 256 KB，handler clamp 1..1024；device/version/kind/payload | 当前 root；kind default `cli-diagnostics` | 单向 POST，需 auth/配额；日志可能含路径和上下文 | 上传前审查 + bytes/lines、网络结果、退出码 |
| lesson feedback | `bugx cloud feedback` | 本地 feedback 的 lesson_id/outcome，最多 100，附可选 run/project | 当前项目/lifecycle filter | `--dry-run` 无网络；非 dry-run 单向 POST | dry-run 预览或 updated/missing、网络结果、退出码 |
| bundle | `cloud export|upload` | export 生成的指定 V2 JSON bundle | export default project/full；upload 以输入 bundle scope 为准 | export 本地原子写；upload 网络并采用 snapshot、强制 baseline pull | bundle SHA-256 + snapshot_id/version + upload 后 status |

知识同步的 `detail-mode=summary` 只是删除部分 pattern/incident 详情，不能当作“无业务内容”或“无敏感数据”。`sensitive_policy` 的值是 `block|redact|allow`；配置缺失时实现缺省 `redact`，`allow` 须显式设置。非 `allow` 策略的出网决策摘要在 `~/.bugx/logs/sync-policy-audit.jsonl`。

遥测门禁统一由 `core/cloud_sync/consent.py:telemetry_enabled()` 裁决。未配置持久化值时默认 `true`；`bugx config set telemetry.data_sharing false` 会清空飞轮 outbox、停止本地统计写入，并让知识同步 push/pull 进入撤销态。持久化的 `false` 是硬撤销，不能用 `BUGX_TELEMETRY_ENABLED=1` 或调用方传入 `data_sharing=True` 绕过；该环境变量只在未持久化撤销时覆盖当前进程，非法值按关闭处理。`telemetry flush/upload` 关闭时不联网、返回 0 并打印已关闭；该 0 只表示安全跳过。普通 CLI 命令还会每日最多尝试一次安装/心跳事件，首次 `installed`、之后 `heartbeat`，通过同一 outbox 传输。

AI 在用户要求“上传数据”时仍必须先分类并说明 telemetry 数据面；但不能把“未见用户逐次确认”误判为实现上的关闭，因为当前产品默认开启。用户明确要求不上传时，先执行 `bugx config set telemetry.data_sharing false` 或在当前进程设置 `BUGX_TELEMETRY_ENABLED=0`，再继续只读/本地路径。

### 10.2 Cloud state machine and recovery

`bugx init` 从 CLI 发行资产安装标准通用 Skill `.agents/skills/bugx/SKILL.md`（DOC-004 极简 RAG 架构），包括重新初始化已有项目的路径。普通初始化保留已有文件，`--force` 刷新不同内容前备份。用户上手以云登录、项目绑定、首轮推拉和经验复用为主线，按需运行 `bugx guide "<关键词>"` 获取操作指引，见 [BugX 平台 Skill](../hosts/platform_bugx_SKILL.md)。仅当用户明确要离线才引导 Local。已有 `local` 先核对当前账号和授权，获得全量授权后再执行 `cloud sync --scope all`。只授权部分数据时拒绝该同步请求。同步包含 project 且成功时设置 `done`。认证验收看 `auth_verified=true`，同步验收看本次各层推拉和队列证据。初始化成功和 `onboarding=done` 都可早于本轮实际同步成功。

AI 处理 cloud 命令时必须先执行以下只读分流：

```text
cloud status --scope all --json
auth status --json
auth status --verify --json   # 只有需要网络认证证据时
cloud conflicts list --scope all --json
cloud dead-letter list --scope all --json
```

决策规则：

1. `data_sharing_revoked`：`STOP`。不得换 scope、删配置或继续 `pre`；只有收到用户明确重新授权后才更新 consent。
2. `auth_state` 非 valid 或 `--verify` 返回 network/TLS/auth error：`BLOCKED`。修复认证/端点后最多重试一次原 scope；不要直接重试完整 `all`。
3. open conflict：`STOP` 扩大同步；让用户选择 `conflicts resolve --side local|remote`。remote 方向会联网拉权威内容，失败时冲突保持 open。
4. dead-letter：先保留 list/status 和错误；后续同 scope sync 处理可重试项。`purge --confirm` 是删除动作，先确认归档文件存在；不能用 purge 作为恢复。
5. `ack_pending=true` 或返回 `committed=true`：标记 `RECOVERY_REQUIRED`，因为本地可能已导入而 ACK 未完成；不 reset、不重复 upload，先恢复网络/认证，再进行一次明确的同 scope sync。
6. 只有错误明确指向可恢复 cursor/baseline 失配时才建议 `cloud sync --calibrate`；payload、权限、TLS、账号和 project identity 错误不能用 calibrate。
7. `project sync`、telemetry、log-report、feedback 彼此独立；必须分别记录 payload 类别和结果。

最终状态只能根据结构化状态和实际网络结果选择 `COMPLETE`、`BLOCKED`、`RECOVERY_REQUIRED` 或 `UNVERIFIED`。工具列表、配置文件写入、服务端任意 JSON、Task 打印和命令退出 0 均不是 cloud egress 的充分证据。

## 11. Readiness rule

以下任何一项缺失，都不能返回“产品功能已验收”：

1. 真实 Host ACP/Headless 入口建立 session；
2. tools/list handshake 和 session proof；
3. Host/Worker identity、project/polling/lifecycle/run 映射；
4. claim、worker-start、tool-call/heartbeat、checkpoint 的真实 receipt；
5. Finding verify/confirm 或明确无 Finding 的 gate 证据；
6. coverage、finish、post、report/integrity 的结构化结果；
7. 至少覆盖 ACP timeout/cancel、MCP failure、checkpoint retry/failure、heartbeat loss/lease reclaim、provider limit、budget exhaustion、open Finding 和 output truncation 的可观察处置。

仅有 parser、schema、prompt、模型文本、loopback 或单元结果时，机器状态必须是 UNVERIFIED 或 BLOCKED。当前实现和本文件的契约可以被源码确认，但真实宿主 ACP、自治闭环、远端 cloud 和跨平台安装的运行证据仍需目标环境单独提供。
