# BugX 主流 AI 宿主接入与派发避坑速查卡

> 适用场景：安装配置宿主、MCP 未加载、Worker 派发方式选择、各宿主交互原语与常见报错。

## 1. 主流宿主配置与原语矩阵

| 宿主 (`--agent`) | MCP 配置文件位置与作用域 | 交互单选工具 | Worker 并发方式 | 核心注意与坑位 |
|---|---|---|---|---|
| **Agnes Code** | 项目根 `agnescode.json` (`mcp.servers`) / 镜像 `.agnescode/` | `question` (`multiple=false`) | `subagent` (`bugx.md`, `background=false`) | 终端原生 TUI；支持 `--agent agnescode` 一键安装与 ACP 无头探活，`AGNESCODE_CONFIG_DIR` 隔离 |
| **Google Antigravity** | 工作区 `.agents/mcp_config.json` | Agent selector | 串行多进程会话 | 插件本体位于用户级 `~/.gemini/config/plugins`，项目级 MCP 须与插件隔离 |
| **OpenCode** | 项目根 `opencode.json` (`mcp.servers`) | `question` (`multiple=false`) | `subagent` (`bugx-worker`, `background=false`) | 必须在项目根启动；`pre` 需同步前台跑 (`timeout=300000`)；禁止从 shell 另起 `bugx mcp serve`（会触发 `session_occupied`） |
| **Claude Code** | 项目根 `.mcp.json` + `.claude/mcp.json` (`mcpServers`) | `AskUserQuestion` (`tooling:batch` 用多选) | `Task(subagent_type="bugx-worker")` | **勿传 `process_pid`**；上游无 ACP（`claude -p` 走 headless）；确保探针输出非空 stdout/stderr 满足双独立证据通道 |
| **Codex** | `.codex/config.toml` + `$CODEX_HOME/config.toml` (`mcp_servers`) | `request_user_input_async` | 原生子智能体（`fork_turns="none"`） | 须具备项目信任表 `[projects]`；支持 `command` (`stdio`) 或 `url` (Remote HTTP MCP) |
| **ZCode** | 用户级全局 `~/.zcode/cli/config.json` (`mcp.servers`) | 原生交互选择 | 小中项目 `collect(--all) -> bugx-judge`；大项目 `workers` | 配置为全局单例（切项目需 `bugx install --root <新项目>` 重绑）；开发验证走 `BUGX_DEV_MODE=1`，**严禁用 `pip install -e`** |
| **Cline** | 全局 `<CLINE_DATA_DIR>/settings/cline_mcp_settings.json` | `ask_question` | 交互会话单 Claim 串行；自治跑 `autonomous --workers N` | 无项目级 MCP，`.cline/mcp.json` 不会被加载 |
| **DSH / Pi / Kimi / MiniMax / Qoder / WorkBuddy** | DSH (`dsh-patch.yml`) / Pi (`.pi/mcp.json`) / Kimi (`.kimi-code/mcp.json`) 等 | 各自宿主单选或回落 `bugx interaction` | `workers --count` 或 `autonomous --host <HOST>` | 运行 `bugx hosts --root . --json` 查看本机检测状态 |

## 2. 默认接入与远程优先通道
* **默认方式（100% 全宿主兼容）**：所有宿主均注册 `bugx-supervisor`（Host 本地 stdio）、`bugx-supervisor-worker`（Worker 本地 stdio）。
* **远程优先官档通道（Remote-First）**：`bugx-docs` 默认采用「远程优先」策略直连云端官档（如 `https://bugx.dev/api/v1/mcp/docs?contract_version=0.1.5`），零本地常驻进程；在断网环境或传入 `--docs-transport stdio` 时自动无感降级为本地 `stdio` 进程与随包离线快照。`mcp_process_tracker` 会自动识别 URL 形态，不会误报“缺少本地进程”。

## 3. 通用高代价避坑（实证复盘）
1. **静默 `sys.exit(1)` 导致双证据通道校验失败**：正例探针失败退出时，引擎依赖非空 stdout/stderr 构造第二验证通道；纯静默退出会被 Confirmation Gate 拒收。务必打印具体可观测差分值（如 `SENTINEL=True` / `ACTUAL=...`）。
2. **新装/更新 MCP 后工具目录未刷新**：宿主通常不热加载 stdio MCP；执行 `bugx install` 后按提示重开宿主会话，并用 `bugx doctor --root .` 验证绿灯。
3. **开发调试禁止 `pip install -e`**：会破坏生产 console script 与全局引擎契约；统一使用 `BUGX_DEV_MODE=1 python3 -m bugx_cli.cli ...`。
