# 环境变量参考（Environment Variables）

> **生成方式**：从 `core/` + `interfaces/` 代码真值自动提取（扫描 `os.environ` 消费点与写入点），
> 覆盖 **141 个**直接消费的 `BUGX_*` 变量（另 56 个为 f-string 动态构造形态，以各消费点代码为准）。
> **最后对账**：2026-11-10。新增变量必须同一次提交内同步本表。

## 三级配置权限（先读这里）

| 级别 | 含义 | 谁该设置 | 数量 |
|---|---|---|---|
| **USER 用户可配** | 值格式与影响见下表；设置错误最多功能不生效，不会破坏状态 | 用户/宿主 AI 按需 | 113 |
| **AUTO 系统自动** | 引擎在生命周期内自动写入的状态管道——**手动设置会被下次运行覆盖，或干扰进行中的会话** | 引擎自动 | 20 |
| **INTERNAL 禁止修改** | 测试注入/内部管道专用——手动设置可能破坏哈希链校验或隔离边界 | 仅测试框架 | 8 |

**排障提示**：环境变量问题优先 `bugx doctor` 定位；清除自设变量用 `unset BUGX_X`（临时）
或删 shell profile 条目（永久）。**AUTO/INTERNAL 变量出现在你的 shell profile 里就是错误信号。**

---
## 0. 设置方式、清理和作用域

本页的“普通用户稳定设置”才是建议手动配置的公共入口；后面的源码消费清单只是审计和排障索引，不是让用户把所有 `BUGX_*` 变量都导出。`AUTO` 和 `INTERNAL` 变量只由 Host、引擎或测试框架注入；手动写入可能破坏 session、身份、哈希链或隔离边界。源码没有定义一条适用于所有变量的统一优先级：能用显式 CLI 参数时，优先使用 `--root`、`--scope`、`--privacy`、`--cloud-sync` 等参数，再按具体消费点判断环境变量与用户级配置的关系。

### 0.1 POSIX shell 和 PowerShell

POSIX shell（bash/zsh）可以只对一次命令设置变量：

```sh
BUGX_PROJECT_ROOT=/path/to/project BUGX_LOG_LEVEL=INFO \
  bugx engine --root /path/to/project status --json
```

需要在当前 shell 的后续命令中复用时：

```sh
export BUGX_PROJECT_ROOT=/path/to/project
export BUGX_LOG_LEVEL=INFO
bugx hosts --root "$BUGX_PROJECT_ROOT" --json
unset BUGX_PROJECT_ROOT BUGX_LOG_LEVEL
```

PowerShell 的对应写法是：

```powershell
$env:BUGX_PROJECT_ROOT = (Get-Location).Path
$env:BUGX_LOG_LEVEL = "INFO"
bugx hosts --root $env:BUGX_PROJECT_ROOT --json
Remove-Item Env:BUGX_PROJECT_ROOT,Env:BUGX_LOG_LEVEL
```

非交互初始化必须由命令显式选择同步范围；环境变量不会替代这个门禁：

```sh
BUGX_NON_INTERACTIVE=1 bugx init --root /path/to/project --cloud-sync local
```

BugX 不把仓库中的 `.env` 文件作为统一配置契约。不要把 token、API key、refresh token 或密码写进仓库、项目 `.bugx/`、命令历史或文档；秘密应通过受控的进程环境或 `auth` 凭据流程提供，使用后清理 shell 环境。

### 0.2 最容易混淆的 scope

| 变量 | 默认/示例 | 真实作用域和设置注意 |
|---|---|---|
| `BUGX_PROJECT_ROOT` | 当前目录；示例 `/path/to/project` | 项目范围；必须是已存在目录。没有显式 `--root` 时作为项目根候选；无效路径 fail-closed。它不改变全局 engine、knowledge 或 cloud 账号。 |
| `BUGX_HOME` | `~/.bugx` | 用户范围；云配置和部分安装/资产回落路径会使用它，**不是**所有 `BUGX_GLOBAL_*` 的统一前缀。 |
| `BUGX_GLOBAL_ENGINE_ROOT` / `BUGX_GLOBAL_KNOWLEDGE_ROOT` | `~/.bugx/core` / `~/.bugx/knowledge` | 用户全局范围；影响所有项目，不应指向被测项目或提交到仓库。显式 `BUGX_GLOBAL_ENGINE_ROOT` 优先于 `BUGX_DEV_MODE`。 |
| `BUGX_CLOUD_CONFIG` / `BUGX_CLOUD_CREDENTIALS` / `BUGX_CLOUD_DEVICE_ID` | `~/.bugx/` 下的受控文件 | 用户级云状态、凭据文件和设备身份文件的路径覆盖；`BUGX_CLOUD_CREDENTIALS` 是**文件路径**，不是 token 值。它们不是项目 scope。 |
| `BUGX_CLOUD_API_BASE` | 经授权的 HTTPS URL | 远端范围；改变网络目的地，可能影响 auth、sync、upload 和 report。它不是 project scope，设置后必须核对授权和 TLS。 |
| `BUGX_ACCEPT_DATA_SHARING=1` | 未设置 | 同意入口；实现可能把同意写入用户级云配置。没有用户明确授权时不要设置；它不替代 `--cloud-sync` 或 `cloud sync --scope`。 |
| `BUGX_REPORT_REDACTION=standard` | 源码默认 `off` | 远程报告分享的脱敏模式；`standard` 仍不是上传授权，也不能保证业务内容全部可公开。 |
| `BUGX_CA_BUNDLE` / `BUGX_ENV` | CA 文件 / `prod`、`dev`、`local`、`test` | 网络 TLS 范围；优先使用 `BUGX_CA_BUNDLE` 保留证书校验。未知 `BUGX_ENV` 按严格模式处理；不要用 `BUGX_DEV_TLS_NO_VERIFY=1` 修复生产证书问题。 |
| `BUGX_PYTHON` / `BUGX_NODE_BIN` / `BUGX_NPM_BIN` | 受支持的可执行文件路径 | 运行时或 MCP runtime 工具选择；不改变项目/云端 scope。 |

`BUGX_GLOBAL_*` 改动会跨项目生效；`BUGX_CLOUD_*` 可能改变用户级文件或远端目的地；`BUGX_PROJECT_ROOT` 只绑定项目。不要因为同名都以 `BUGX_` 开头就把三者混用。

### 0.3 不能手写来伪造状态

- `BUGX_INTERACTION_REQUEST` 是引擎输出给宿主的结构化交互请求，不是用户答案；真实选择通过 `interaction` 命令或宿主控件回传。
- `BUGX_NATIVE_AGENT_ID`、`BUGX_MCP_SESSION_PROOF`、`BUGX_PROCESS_PID`、`BUGX_HOST_PID`、`BUGX_CLI_VERSION`、`BUGX_BUILD_ID` 等是身份、session、进程或版本证据；不能用固定字符串冒充 Worker/Host。
- `BUGX_HOST` **不是宿主选择器**。当前宿主识别以主动探查和配置事实为准；该变量最多作为缺少 runtime host 时的报告 fallback，设置成 `opencode` 等不能替代真实宿主。
- `BUGX_TEST_*`、`BUGX_SANDBOX_TEST`、`BUGX_TEST_CONTEXT`、`BUGX_TEST_ALLOW_ENGINE_REPO_GIT_WRITE` 是测试/探针写保护上下文，不是生产配置；不要用它们绕过门禁。

### 0.4 Dev 模式：设置时机、最小参数和恢复

Dev 模式要先区分“源码 engine 来源”和“生命周期快捷策略”：

| 配置 | 必须值 | 生效条件 | 作用域 | 是否会改变生命周期策略 |
|---|---|---|---|---|
| `BUGX_DEV_MODE` | `1` | 当前进程必须能识别真实 BugX checkout；根目录有 `.git`、`scripts/bump_version.py`，且 `core/bugx.md`、`core/launch_bugx.py` 存在 | 当前进程 | 否，只让 engine 解析到源码 `core/` |
| `BUGX_DEV_ASSET_ROOT` | `1` | 与上行相同 | 当前进程 | 否，是兼容别名 |
| `BUGX_GLOBAL_ENGINE_ROOT` | 完整 engine 目录 | 显式设置时优先级高于 Dev engine | 用户全局/当前进程 | 否，但会使 Dev engine 不生效 |
| `BUGX_ASSET_ROOT` | 完整、受信的资产目录 | 必须包含所需入口并通过安全路径校验 | 当前进程 | 否；不是 Dev mode 开关 |
| `engine ... pre --dev` | CLI flag | 只在用户明确接受自动化副作用时使用 | 当前 lifecycle | 是：自动审查、自动修复、自动轮询、按轮次提交、resume、启动动作 start、策略 approve |

源码开发但保留人工修复/提交策略时，先用一次性环境变量确认 engine，再运行普通 `pre`：

```sh
env -u BUGX_GLOBAL_ENGINE_ROOT \
    BUGX_DEV_MODE=1 \
    bugx engine --root <PROJECT_ROOT> path --name launch_bugx.py

env -u BUGX_GLOBAL_ENGINE_ROOT \
    BUGX_DEV_MODE=1 \
    bugx engine --root <PROJECT_ROOT> pre \
    --fix-mode log-only \
    --commit-policy manual \
    --cloud-sync local
```

PowerShell 中使用同样的顺序，并把当前进程的显式 engine override 暂时移除：

```powershell
$env:BUGX_DEV_MODE = "1"
Remove-Item Env:BUGX_GLOBAL_ENGINE_ROOT -ErrorAction SilentlyContinue
bugx engine --root <PROJECT_ROOT> path --name launch_bugx.py
bugx engine --root <PROJECT_ROOT> pre --fix-mode log-only --commit-policy manual --cloud-sync local
Remove-Item Env:BUGX_DEV_MODE -ErrorAction SilentlyContinue
```

纯源码 Dev engine 的第一条命令必须输出：

```text
<BUGX_SOURCE_CHECKOUT>/core/launch_bugx.py
```

输出仍来自 `~/.bugx/core`、wheel、npm 包或其他目录时，说明当前进程没有使用源码 engine；先检查 `command -v bugx`、checkout marker、`BUGX_GLOBAL_ENGINE_ROOT` 和 `BUGX_ASSET_ROOT`，不要继续执行 `pre`。

只有在隔离、可回滚且得到明确授权的项目中，才使用极速生命周期：

```sh
env -u BUGX_GLOBAL_ENGINE_ROOT \
    -u BUGX_ASSET_ROOT \
    BUGX_DEV_MODE=1 \
    bugx engine --root <PROJECT_ROOT> pre --dev --cloud-sync local
```

`--dev` 的 `fix-mode` 当前默认为 `auto`，`commit-policy` 默认为 `round`；它不是只读模式，可能执行目标代码、写 `.bugx/`、修改目标文件和创建 Git commit。显式 `--cloud-sync` 或项目 profile 中已有 cloud policy 会覆盖 Dev 的 local 缺省；`sync` 是联网/数据分享出口，`local` 不会撤销用户级 consent。

退出时只清理本次配置，并恢复用户原有的 override：

```sh
unset BUGX_DEV_MODE BUGX_DEV_ASSET_ROOT
```

Dev 诊断的停止条件：engine path 不是当前 checkout、出现未知来源的 engine/asset 混用、`pre` 返回 2/3 或其他非零、目标代码发生未授权修改、状态为 `BLOCKED`/`RECOVERY_REQUIRED`，或没有新的外部证据却准备重复同一命令。详细症状路由见[用户故障排查](../01_guides/03_TROUBLESHOOTING.md) §2.5。

## 1. 普通用户稳定设置

普通用户通常只需要下面这些变量；其余变量先按源码消费清单或内部排障说明处理，不要因为变量名以 `BUGX_` 开头就导出：

| 变量 | 什么时候设置 | 值与默认 | 作用域/副作用 |
|---|---|---|---|
| `BUGX_PROJECT_ROOT` | 当前 cwd 不是目标项目，或宿主工作区路径不可靠 | 已存在目录；默认候选为 cwd | project；不改变全局 engine、knowledge 或 cloud 账号 |
| `BUGX_DEV_MODE` | 需要让真实源码 checkout 的 engine 立即生效 | `1`；默认未设置 | 当前进程；只改变 engine 来源，不开启自治 |
| `BUGX_DEV_ASSET_ROOT` | 旧脚本只能识别 Dev 兼容别名 | `1`；默认未设置 | 当前进程；与 `BUGX_DEV_MODE` 二选一 |
| `BUGX_GLOBAL_ENGINE_ROOT` | 需要隔离多份已安装 engine，且不使用源码 Dev engine | 完整 engine 目录 | user-global/current process；显式设置会覆盖 Dev engine |
| `BUGX_ASSET_ROOT` | 需要使用已确认完整且受信的资产目录 | 完整目录路径 | current process；不是 Dev mode，可能改变资产来源 |
| `BUGX_LOG_LEVEL` | 需要增加或减少本地日志粒度 | 由当前实现接受的级别；默认通常为 `INFO` | current process；改变日志量，不改变证据状态 |
| `BUGX_REPORT_REDACTION` | 远程报告需要标准脱敏 | `standard`；源码默认 `off` | report/network；不等于上传授权 |
| `BUGX_CA_BUNDLE` | 企业 CA 或受控 TLS 链需要显式指定 | CA 文件路径 | network；保留证书校验，不能用来绕过 TLS |
| `BUGX_ENV` | 当前部署明确要求 prod/dev/local/test 环境语义 | 按消费点接受的值；未知值走严格处理 | network/runtime；不应随意从生产改成 dev |
| `BUGX_PYTHON` | 需要指定受支持的 Python 解释器 | 可执行文件路径 | current process/runtime；不改变项目 scope |
| `BUGX_NODE_BIN` / `BUGX_NPM_BIN` | 使用 npm/MCP runtime 且默认 Node 不可用 | 可执行文件路径 | current process/runtime；不代表 Host 已 ready |
| `BUGX_NON_INTERACTIVE` | CI/headless 已明确准备好所有选择 | `1` | current process；不会替用户同意 cloud、修复或删除动作 |
| `BUGX_CLOUD_CONFIG` | 需要把云端配置放在受控用户目录，或做一次性隔离配置 | 文件路径；默认 `~/.bugx/cloud-config.json` | user-global；改变 consent、scope/detail/policy、Base URL 等云配置，可能改变所有项目的网络行为 |
| `BUGX_CLOUD_CREDENTIALS` / `BUGX_CLOUD_DEVICE_ID` | 需要隔离登录凭据或设备身份文件 | 文件路径；默认位于 `~/.bugx/` | user-global；只设置路径，不要把 token/ID 值直接写进变量说明、项目或日志 |
| `BUGX_CLOUD_API_BASE` | 需要使用已授权的自建/代理云端 | HTTPS URL；默认由云端配置/正式端点决定 | remote destination；改变 auth、cloud sync、bundle upload、log-report 等网络目的地，必须重新核对 TLS/权限 |
| `BUGX_CLOUD_ROUTE_CACHE` | 需要隔离或清理云端路由测速缓存 | 文件路径；默认 `~/.bugx/cloud-route-cache.json` | user-global；只影响路由缓存，不改变数据授权；`cloud config --refresh-route` 是更安全的清理入口 |
| `BUGX_TELEMETRY_ENDPOINT` | 需要把飞轮遥测发送到已授权端点 | HTTPS URL；未设置时跟随 cloud API 配置 | remote destination；只影响 telemetry，不等于 knowledge sync endpoint |
| `BUGX_TELEMETRY_PASSPHRASE` | 非交互导出加密遥测包 | 口令；默认未设置 | current process；可能出现在进程环境，优先交互输入，绝不写日志或提交到仓库 |
| `BUGX_FLYWHEEL_OUTBOX` / `BUGX_FLYWHEEL_AUDIT` | 需要把遥测队列/出网审计放入隔离目录 | 文件路径；默认 `~/.bugx/telemetry/flywheel_outbox.sqlite3` 与 `egress_audit.log` | user-global；改变本地遥测证据位置，不改变 consent 或网络目的地 |
| `BUGX_PRIVACY_STRICT` | 当前进程需要严格限制飞轮字段 | `1` | current process/telemetry；进一步缩小遥测 payload，不代替用户授权，也不改变 knowledge `sensitive_policy` |

设置后应立即用对应命令验证实际解析结果；没有验证输出时，变量只能报告为“已设置”，不能报告为“已生效”。普通用户不应手动设置 `BUGX_NATIVE_AGENT_ID`、`BUGX_MCP_SESSION_PROOF`、`BUGX_HOST`、`BUGX_PROCESS_PID`、`BUGX_HOST_PID`、`BUGX_TEST_*` 或 AUTO/INTERNAL 变量。

### 1.1 云端变量的最小核对清单

遇到“会不会上传”的问题，不要只检查变量是否存在，按数据面核对：

| 数据面 | 触发命令 | 关键变量/配置 | 本地证据 | 结论边界 |
|---|---|---|---|---|
| Knowledge sync | `bugx cloud sync`、部分 `pre/onboard` | `BUGX_CLOUD_CONFIG`、`telemetry.data_sharing`、`cloud.sync_scope_default`、`cloud.sync_detail_mode`、`cloud.sensitive_policy` | `<ROOT>/.bugx/cloud-sync/state.sqlite3` 或全局 store、policy audit | 设置 cloud config 不等于已同步；启动 onboarding 可能扩大到 global+project |
| Project profile | `bugx project sync` | `BUGX_CLOUD_API_BASE`、credential/device path | `<ROOT>/.bugx/cloud-project.json` 与 `--json` 的 `status/project_id/synced` | 绑定文件不等于 profile 已被云端接收 |
| Telemetry | `bugx telemetry flush|upload` | `BUGX_FLYWHEEL_OUTBOX`、`BUGX_FLYWHEEL_AUDIT`、`BUGX_TELEMETRY_ENDPOINT`、`BUGX_PRIVACY_STRICT` | outbox、egress audit、`telemetry inspect --latest --json` | 与 knowledge schema 独立；发送成功项才从队列清除 |
| Diagnostic/feedback | `cloud log-report`、非 dry-run `cloud feedback` | `BUGX_CLOUD_API_BASE`、credential、`BUGX_CLOUD_CONFIG` | 命令输出的 bytes/lines 或 updated/missing、退出码 | 默认不上传；日志内容需人工审查 |

环境变量只改变解析、路径、策略或端点；它们不会凭空生成 consent、Host session、Worker receipt、远端 ACK 或完整生命周期。对于 `BUGX_CLOUD_API_BASE`、`BUGX_TELEMETRY_ENDPOINT`、`BUGX_CLOUD_CREDENTIALS` 等变量，必须把实际值视为敏感/受控配置，文档和诊断报告只记录“已设置、来源和脱敏 URL”。

## 2. 源码消费清单（USER 类别，不等于公共 API）

### 开发与引擎装配（5）

| 变量 | 值格式 | 设置后的影响 | 默认值 | 主消费点 |
|---|---|---|---|---|
| `BUGX_ASSET_ROOT` | 绝对路径 | 资产目录覆盖（配合 readonly view 加固使用） | `""` | `interfaces/cli/bugx_cli/commands/system.py (+3 处)` |
| `BUGX_BUILD_ID` | 正整数 | 覆盖 Build ID 读取（仅构建脚本内部用） | `无默认(None/空)` | `core/infra/build_info.py (+1 处)` |
| `BUGX_CLI_VERSION` | 语义版本 | 覆盖 CLI 版本识别（仅诊断用） | `必填` | `core/launch_phases/startup_version_gate.py (+1 处)` |
| `BUGX_DEV_ASSET_ROOT` | 1 | BUGX_DEV_MODE 兼容别名 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/system.py (+1 处)` |
| `BUGX_DEV_MODE` | 1 | 源码开发模式：引擎根随仓库 core/ 走，改源码即时生效免安装 | `无默认(None/空)` | `interfaces/cli/bugx_cli/infra/engine_installer.py` |

### 运行时行为（56）

| 变量 | 值格式 | 设置后的影响 | 默认值 | 主消费点 |
|---|---|---|---|---|
| `BUGX_CLI_SOURCE` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `interfaces/cli/bugx_cli/commands/lifecycle.py` | `""` | `interfaces/cli/bugx_cli/commands/lifecycle.py` |
| `BUGX_CODEBASE_MEMORY_TIMEOUT_SECONDS` | 正秒数 | codebase-memory 探针超时 | `无默认(None/空)` | `core/probes/codebase_memory_probe.py` |
| `BUGX_DISABLE_EXTERNAL_SAST` | 1 | 禁用外部 SAST 工具（semgrep/bandit 等），仅用内置探针 | `无默认(None/空)` | `core/probes/sast_probe.py` |
| `BUGX_DOCTOR_NO_LLM` | 1 | doctor 跳过 LLM 顾问 | `无默认(None/空)` | `interfaces/cli/bugx_cli/infra/ai_advisor.py` |
| `BUGX_ENV` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/infra/client_metadata.py` | `""` | `core/infra/client_metadata.py` |
| `BUGX_FORCE_INSTALL` | 1 | 跳过安装前置确认 | `无默认(None/空)` | `core/launch_phases/supervisor_bridge.py` |
| `BUGX_GLOBAL_CACHE_ROOT` | 绝对路径 | 覆盖全局缓存根目录 | `无默认(None/空)` | `core/bugx_paths.py` |
| `BUGX_GLOBAL_ENGINE_ROOT` | 绝对路径 | 覆盖全局引擎根目录（默认 ~/.bugx/core）。多环境隔离、沙箱测试必备 | `"" \| str(Path.home(` | `core/bugx_paths.py (+8 处)` |
| `BUGX_GLOBAL_SKILLS_DIR` | 绝对路径 | 覆盖全局 skills 目录 | `无默认(None/空)` | `core/bugx_paths.py` |
| `BUGX_HEADLESS` | 1 | 声明 headless 环境（无 TUI/无原生交互） | `""` | `core/cloud_sync/onboard_wizard.py (+2 处)` |
| `BUGX_HOME` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/cloud_sync/consent.py (+7 处)` | `"" \| str(Path.home(` | `core/cloud_sync/consent.py (+7 处)` |
| `BUGX_HOST` | 宿主名 | 宿主提示（进程树探查优先，无兜底权） | `无默认(None/空)` | `core/launch_phases/finding_settlement.py (+1 处)` |
| `BUGX_INTENT_NO_LLM` | 1 | 意图推断跳过 LLM（纯规则匹配） | `无默认(None/空)` | `interfaces/cli/bugx_cli/infra/ai_advisor.py` |
| `BUGX_LOCALE` | locale 代码（如 zh-CN/en-US） | 覆盖界面与报告语言 | `无默认(None/空)` | `core/planning/prompt_harness.py` |
| `BUGX_LOG_LEVEL` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/runtime/bugx_logging.py (+1 处)` | `"" \| "INFO"` | `core/runtime/bugx_logging.py (+1 处)` |
| `BUGX_LOG_VERBOSE` | 1 | 开启详细日志输出 | `无默认(None/空)` | `core/mcp_handlers/rpc_server.py` |
| `BUGX_MAX_AUTO_BUSINESS_UNITS` | 正整数 | 自动业务链路单元的数量上限 | `无默认(None/空)` | `core/coverage/coverage_discovery.py` |
| `BUGX_MAX_CONCURRENCY` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/supervisor/supervisor_models.py` | `无默认(None/空)` | `core/supervisor/supervisor_models.py` |
| `BUGX_MAX_CONCURRENCY_CEILING` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/supervisor/supervisor_models.py` | `无默认(None/空)` | `core/supervisor/supervisor_models.py` |
| `BUGX_MCP_RUNTIME_ROOT` | 绝对路径 | 覆盖 MCP runtime 目录 | `无默认(None/空)` | `core/runtime/project_init.py (+1 处)` |
| `BUGX_NATIVE_AGENT_ID` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/skills/bugx_mcp_server.py` | `无默认(None/空)` | `core/skills/bugx_mcp_server.py` |
| `BUGX_NODE_BIN` | 绝对路径 | 覆盖 node 可执行文件路径（npm wrapper 用） | `无默认(None/空)` | `core/mcp_runtime_support/verifier.py` |
| `BUGX_NONINTERACTIVE` | 1 | 强制非交互模式（全部 gate 走默认或回灌） | `""` | `core/cloud_sync/onboard_wizard.py` |
| `BUGX_NON_INTERACTIVE` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/cloud_sync/onboard_wizard.py (+2 处)` | `无默认(None/空)` | `core/cloud_sync/onboard_wizard.py (+2 处)` |
| `BUGX_NO_AUTO_HEAL` | 1 | 禁用自动自愈（.bugx 布局损坏时 fail-closed 而非重建） | `无默认(None/空)` | `interfaces/cli/bugx_cli/infra/ai_advisor.py` |
| `BUGX_NO_PROGRESS` | 1 | 禁用进度条输出（CI/日志采集环境） | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/agent.py` |
| `BUGX_NPM_BIN` | 绝对路径 | 覆盖 npm 可执行文件路径 | `无默认(None/空)` | `core/mcp_runtime_support/environment.py` |
| `BUGX_OFFLINE` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/cloud_sync/endpoint_router.py (+2 处)` | `无默认(None/空)` | `core/cloud_sync/endpoint_router.py (+2 处)` |
| `BUGX_PATCH_VERIFICATION_API_KEY` | API Key | 补丁验证 API 的凭据 | `""` | `interfaces/cli/bugx_cli/commands/patch.py` |
| `BUGX_PATCH_VERIFY_TIMEOUT` | 正秒数 | 补丁验证命令的超时上限 | `"30"` | `core/supervisor/worker_runtime.py` |
| `BUGX_POC_TIMEOUT` | 正秒数 | PoC 复现命令的超时上限 | `"10"` | `core/supervisor/worker_runtime.py` |
| `BUGX_PRIVACY_STRICT` | 1/true | 严格隐私模式（等价 --privacy strict） | `无默认(None/空)` | `core/cloud_sync/flywheel_client.py` |
| `BUGX_PROBE_TIMEOUT` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/cloud_sync/cloud_client.py (+1 处)` | `"0.8"` | `core/cloud_sync/cloud_client.py (+1 处)` |
| `BUGX_PROBE_TIMEOUT_CODEBASE_MEMORY` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/probes/codebase_memory_probe.py` | `无默认(None/空)` | `core/probes/codebase_memory_probe.py` |
| `BUGX_PROBE_TIMEOUT_SECONDS` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/probes/codebase_memory_probe.py (+1 处)` | `无默认(None/空)` | `core/probes/codebase_memory_probe.py (+1 处)` |
| `BUGX_PROFILE_NO_LLM` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/project_profiler/arbitrator.py (+2 处)` | `必填` | `core/project_profiler/arbitrator.py (+2 处)` |
| `BUGX_PROJECT_ROOT` | 绝对路径 | 钉住项目根目录，优先级高于 cwd 探查 | `""` | `core/bugx_paths.py (+11 处)` |
| `BUGX_PYTHON` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/infra/python_runtime.py` | `必填` | `core/infra/python_runtime.py` |
| `BUGX_PYTHON_RUNTIME_DIR` | 绝对路径 | 覆盖 Python 运行时目录 | `无默认(None/空)` | `core/infra/python_runtime.py` |
| `BUGX_RECON_REFRESH` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/launch_phases/pre_recon_scout.py` | `无默认(None/空)` | `core/launch_phases/pre_recon_scout.py` |
| `BUGX_RECON_TIMEOUT` | 正秒数 | 侦察阶段超时上限 | `无默认(None/空)` | `core/launch_phases/pre_recon_scout.py (+1 处)` |
| `BUGX_REPORT_REDACTION` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/report_engine/report_client.py` | `REDACTION_DEFAULT` | `core/report_engine/report_client.py` |
| `BUGX_RESET_BACKUP_ROOT` | 绝对路径 | 覆盖 reset 备份的存放根目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin.py` |
| `BUGX_SCOUT_MODEL` | provider/model | 侦察阶段模型覆盖 | `无默认(None/空)` | `core/hosts/opencode.py` |
| `BUGX_SELF_HEAL_SEARCH` | 1 | 允许自愈时的全局搜索定位 | `""` | `core/repair/self_heal.py` |
| `BUGX_SELF_HEAL_SEARCH_CMD` | 命令模板 | 自愈搜索使用的自定义命令 | `""` | `core/repair/self_heal.py` |
| `BUGX_SKIP_ROUTE_PROBE` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/cloud_sync/endpoint_router.py` | `无默认(None/空)` | `core/cloud_sync/endpoint_router.py` |
| `BUGX_SLOW_THRESHOLD_SECONDS` | 正秒数 | 慢命令判定阈值（超过记性能异味） | `无默认(None/空)` | `core/launch_phases/round_orchestrator.py (+1 处)` |
| `BUGX_SMOKE_MODEL` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/hosts/opencode.py` | `无默认(None/空)` | `core/hosts/opencode.py` |
| `BUGX_STRICT_BUDGET` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/launch_phases/startup_gates.py` | `""` | `core/launch_phases/startup_gates.py` |
| `BUGX_TARGET_PORT` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/runtime/daemon_runner.py` | `无默认(None/空)` | `core/runtime/daemon_runner.py` |
| `BUGX_TARGET_START_CMD` | 命令行 | 被测目标服务的启动命令（服务探活/恢复用） | `无默认(None/空)` | `core/runtime/daemon_runner.py` |
| `BUGX_TOOLING_CHOICES` | tool=action ... | 工具门禁处置回传 | `""` | `core/launch_phases/supervisor_degraded.py` |
| `BUGX_TRANSPORT` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/supervisor/supervisor_host.py` | `""` | `core/supervisor/supervisor_host.py` |
| `BUGX_UNINSTALL_BACKUP_ROOT` | 绝对路径 | 覆盖 uninstall 备份的存放根目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin.py` |
| `BUGX_USE_CODEBASE_MEMORY` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/launch_phases/supervisor_bridge.py (+3 处)` | `必填` | `core/launch_phases/supervisor_bridge.py (+3 处)` |

### 生命周期与并发（4）

| 变量 | 值格式 | 设置后的影响 | 默认值 | 主消费点 |
|---|---|---|---|---|
| `BUGX_DAEMON` | 1 | 守护进程模式标记（daemon 子进程内部传递） | `无默认(None/空)` | `core/launch_phases/post_round_lifecycle.py` |
| `BUGX_RELEASE_CHANNEL` | stable/alpha | 发布通道选择（影响 update 源） | `无默认(None/空)` | `core/infra/client_metadata.py` |
| `BUGX_WORKERS_MAX` | 正整数 | 覆盖 Worker 并发上限（默认按 CPU 与宿主能力） | `无默认(None/空)` | `core/supervisor/supervisor_models.py` |
| `BUGX_WORKER_TIMEOUT` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/supervisor/worker_runtime.py` | `无默认(None/空)` | `core/supervisor/worker_runtime.py` |

### 宿主适配（23）

| 变量 | 值格式 | 设置后的影响 | 默认值 | 主消费点 |
|---|---|---|---|---|
| `BUGX_CHATGPT_PLUGIN_ROOT` | 绝对路径 | 覆盖 ChatGPT 插件根目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin.py` |
| `BUGX_CLAUDE_DESKTOP_SESSIONS_DIR` | 绝对路径 | 覆盖 Claude Desktop 会话目录探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_claude.py` |
| `BUGX_CLAUDE_DESKTOP_SKILLS_DIR` | 绝对路径 | 覆盖 Claude Desktop skills 目录探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_claude.py` |
| `BUGX_CLAUDE_DESKTOP_SUPPORT_DIR` | 绝对路径 | 覆盖 Claude Desktop support 目录探测位置 | `""` | `interfaces/cli/bugx_cli/commands/plugin_claude.py` |
| `BUGX_CLAUDE_GLOBAL_DIST_DIR` | 绝对路径 | 覆盖 Claude 全局分发目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_claude.py` |
| `BUGX_CLAUDE_PLUGINS_DIR` | 绝对路径 | 覆盖 Claude 插件目录探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_claude.py` |
| `BUGX_CLAUDE_SETTINGS_PATH` | 绝对路径 | 覆盖 Claude settings.json 探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_claude.py` |
| `BUGX_CLAUDE_USER_AGENTS_DIR` | 绝对路径 | 覆盖 Claude Desktop/Code 用户级 agents 目录探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_claude.py` |
| `BUGX_CLAUDE_USER_SKILLS_DIR` | 绝对路径 | 覆盖 Claude 用户级 skills 目录探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_claude.py` |
| `BUGX_CLINE_USER_AGENTS_DIR` | 绝对路径 | 覆盖 Cline 用户级 agents 目录探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_cline.py` |
| `BUGX_CODEX_CONFIG` | 绝对路径 | 覆盖 Codex config.toml 探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin.py` |
| `BUGX_FUNIDE_DATA_DIR` | 绝对路径 | 覆盖 FunIDE 数据目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/host_adapters.py` |
| `BUGX_FUNIDE_PROFILE_DIR` | 绝对路径 | 覆盖 FunIDE profile 目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/host_adapters.py` |
| `BUGX_FUNIDE_SETTINGS_PATH` | 绝对路径 | 覆盖 FunIDE settings 路径 | `无默认(None/空)` | `interfaces/cli/bugx_cli/host_adapters.py` |
| `BUGX_FUNIDE_USER_AGENTS_DIR` | 绝对路径 | 覆盖 FunIDE 用户级 agents 目录探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/host_adapters.py` |
| `BUGX_OPENCODE_MODEL` | provider/model | OpenCode 子进程模型 | `""` | `core/hosts/opencode.py (+1 处)` |
| `BUGX_QODER_USER_AGENTS_DIR` | 绝对路径 | 覆盖 Qoder 用户级 agents 目录探测位置 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_qoder.py` |
| `BUGX_QODER_USER_MCP_PATH` | 绝对路径 | 覆盖 Qoder 用户级 MCP 配置路径 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_qoder.py` |
| `BUGX_QODER_USER_PLUGINS_DIR` | 绝对路径 | 覆盖 Qoder 插件目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_qoder.py` |
| `BUGX_QODER_USER_PLUGIN_CACHE_DIR` | 绝对路径 | 覆盖 Qoder 插件缓存目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_qoder.py` |
| `BUGX_QODER_USER_SETTINGS_PATH` | 绝对路径 | 覆盖 Qoder settings 路径 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_qoder.py` |
| `BUGX_QODER_USER_SKILLS_DIR` | 绝对路径 | 覆盖 Qoder skills 目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_qoder.py` |
| `BUGX_ZCODE_PLUGIN_ROOT` | 绝对路径 | 覆盖 ZCode 插件根目录 | `无默认(None/空)` | `interfaces/cli/bugx_cli/commands/plugin_zcode.py` |

### 云端与数据共享（18）

| 变量 | 值格式 | 设置后的影响 | 默认值 | 主消费点 |
|---|---|---|---|---|
| `BUGX_ACCEPT_DATA_SHARING` | 1/true | 预同意数据共享（等价 init --accept-data-sharing） | `无默认(None/空)` | `core/cloud_sync/flywheel_client.py (+1 处)` |
| `BUGX_AIR_GAP` | 1 | 气隙模式：禁用所有外部网络访问 | `无默认(None/空)` | `core/launch_phases/common.py (+1 处)` |
| `BUGX_CLOUD_API` | URL | 云端 API 地址覆盖（自建/代理部署） | `"https://bugx.dev"` | `core/skills/skill_pack.py` |
| `BUGX_CLOUD_API_BASE` | URL | 云端 API 基址覆盖 | `""` | `core/cloud_sync/endpoint_router.py (+3 处)` |
| `BUGX_CLOUD_MAX_PULL_PAGES` | 正整数 | 云端 pull 的最大分页数 | `"1000"` | `core/cloud_sync/cloud_client.py` |
| `BUGX_CLOUD_PAGE_SIZE` | 正整数 | 云端分页大小 | `"100"` | `core/cloud_sync/cloud_client.py` |
| `BUGX_CLOUD_RETRIES` | 正整数 | 云端请求重试次数 | `"1"` | `core/cloud_sync/cloud_client.py` |
| `BUGX_CLOUD_ROUTE_TTL` | 正秒数 | 云端路由缓存 TTL | `"86400" \| str(DEFAULT_ROUTE_TTL` | `core/cloud_sync/cloud_client.py (+1 处)` |
| `BUGX_CLOUD_TIMEOUT` | 正秒数 | 云端请求超时 | `"8"` | `core/cloud_sync/cloud_client.py` |
| `BUGX_GLOBAL_TELEMETRY_PATH` | 绝对路径 | 覆盖全局遥测数据文件路径 | `无默认(None/空)` | `core/bugx_paths.py` |
| `BUGX_GLOBAL_TELEMETRY_ROOT` | 绝对路径 | 覆盖全局遥测数据根目录 | `无默认(None/空)` | `core/bugx_paths.py` |
| `BUGX_OTLP_ENDPOINT` | URL | OTLP 遥测上报端点（自建观测平台） | `无默认(None/空)` | `core/runtime/bugx_logging.py` |
| `BUGX_SHARE_API_BASE` | URL | 分享 API 地址覆盖 | `"https://bugx.dev"` | `core/report_engine/report_client.py` |
| `BUGX_SHARE_API_PATH` | 路径 | 分享 API 路径覆盖 | `"/api/v1/agent/runs"` | `core/report_engine/report_client.py` |
| `BUGX_SHARE_RETRIES` | 正整数 | 分享上传重试次数 | `"1"` | `core/report_engine/report_client.py` |
| `BUGX_SHARE_TIMEOUT` | 正秒数 | 分享上传超时 | `"8"` | `core/report_engine/report_client.py` |
| `BUGX_TELEMETRY_ENDPOINT` | URL | 遥测上报端点覆盖 | `无默认(None/空)` | `core/cloud_sync/flywheel_client.py` |
| `BUGX_TELEMETRY_PASSPHRASE` | 口令 | 遥测载荷加密口令 | `无默认(None/空)` | `core/cloud_sync/flywheel_client.py (+1 处)` |

### 网络与安全（5）

| 变量 | 值格式 | 设置后的影响 | 默认值 | 主消费点 |
|---|---|---|---|---|
| `BUGX_CA_BUNDLE` | PEM 文件路径 | 自定义 CA 证书包（企业自签 TLS） | `""` | `core/cloud_sync/network_transport.py (+1 处)` |
| `BUGX_DISABLE_VERSION_CHECK` | 1 | 禁用云端版本检查 | `""` | `core/launch_phases/startup_version_gate.py (+2 处)` |
| `BUGX_DOCS_PROJECT` | 项目标识 | bugx-docs MCP 的项目标识 | `无默认(None/空)` | `core/skills/bugx_docs_mcp_server.py` |
| `BUGX_DOCS_TOKEN` | 令牌 | bugx-docs MCP 的访问令牌 | `无默认(None/空)` | `core/skills/bugx_docs_mcp_server.py` |
| `BUGX_DOCS_VERSION` | 版本号 | bugx-docs MCP 检索的文档版本 | `无默认(None/空)` | `core/skills/bugx_docs_mcp_server.py` |

### 测试与探针（2）

| 变量 | 值格式 | 设置后的影响 | 默认值 | 主消费点 |
|---|---|---|---|---|
| `BUGX_TEST_ALLOW_ENGINE_REPO_GIT_WRITE` | 字符串（按消费点校验） | 覆盖该功能默认行为；语义见 `core/launch_phases/post_commit.py` | `无默认(None/空)` | `core/launch_phases/post_commit.py` |
| `BUGX_TEST_CONTEXT` | 1 | 测试上下文标记：触发 git 写门护栏 | `无默认(None/空)` | `core/cloud_sync/flywheel_client.py (+1 处)` |

---
## 3. 系统自动设置（AUTO，勿手动设置）

### 运行时行为（13）

| 变量 | 引擎写入时机 | 注意 |
|---|---|---|
| `BUGX_CLIENT_MARKER` | `core/infra/client_metadata.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_COVERAGE_MANIFEST` | `interfaces/cli/bugx_cli/commands/autonomous.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_HOST_PID` | `core/launch_phases/session_runtime.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_HOST_SURFACE` | `core/supervisor/supervisor_host.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_INTERACTION` | `core/supervisor/supervisor_host.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_INTERACTION_ENVIRONMENT` | `core/launch_phases/startup_interaction.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_INTERACTION_VERIFIED` | `core/supervisor/supervisor_host.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_PROCESS_MODE` | `core/supervisor/supervisor_host.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_PROCESS_PID` | `core/launch_phases/session_runtime.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_RECON_SCOUT_ACTIVE` | `core/launch_phases/pre_recon_scout.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_SELF_CHECK` | `interfaces/cli/bugx_cli/commands/agent.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_TRACE_ID` | `core/runtime/model_runner.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_WEBUI_DRY_RUN_IDE` | `core/webui/ide_cloud_service.py` | 手动设置会干扰进行中的会话状态 |

### 生命周期与并发（1）

| 变量 | 引擎写入时机 | 注意 |
|---|---|---|
| `BUGX_ACP` | `core/supervisor/supervisor_host.py` | 手动设置会干扰进行中的会话状态 |

### 云端与数据共享（6）

| 变量 | 引擎写入时机 | 注意 |
|---|---|---|
| `BUGX_CLOUD_AUTOSYNC_WORKER` | `core/ledger/schema.py` | 手动设置会干扰进行中的会话状态 |
| `BUGX_CLOUD_CONFIG` | `core/cloud_sync/cloud_client.py (+4 处)` | 手动设置会干扰进行中的会话状态 |
| `BUGX_CLOUD_CREDENTIALS` | `core/cloud_sync/cloud_client.py (+2 处)` | 手动设置会干扰进行中的会话状态 |
| `BUGX_CLOUD_DEVICE_ID` | `core/cloud_sync/cloud_client.py (+1 处)` | 手动设置会干扰进行中的会话状态 |
| `BUGX_CLOUD_ROUTE_CACHE` | `core/cloud_sync/cloud_client.py (+2 处)` | 手动设置会干扰进行中的会话状态 |
| `BUGX_CLOUD_SYNC_ACTIVE` | `core/cloud_sync/cloud_onboard.py (+1 处)` | 手动设置会干扰进行中的会话状态 |

---
## 4. 禁止修改（INTERNAL）

### 运行时行为（6）

| 变量 | 引擎写入时机 | 注意 |
|---|---|---|
| `BUGX_FLYWHEEL_AUDIT` | `core/cloud_sync/flywheel_client.py` | 仅测试框架注入；手动设置可能破坏校验边界 |
| `BUGX_FLYWHEEL_OUTBOX` | `core/cloud_sync/flywheel_client.py` | 仅测试框架注入；手动设置可能破坏校验边界 |
| `BUGX_FREE_MODEL` | `core/host_runtime/free_models.py` | 仅测试框架注入；手动设置可能破坏校验边界 |
| `BUGX_GLOBAL_KNOWLEDGE_ROOT` | `core/bugx_paths.py (+3 处)` | 仅测试框架注入；手动设置可能破坏校验边界 |
| `BUGX_READONLY_CONCURRENCY` | `core/launch_phases/worker_orchestrator.py` | 仅测试框架注入；手动设置可能破坏校验边界 |
| `BUGX_TEXT_MENU` | `core/runtime/interactive_menu.py` | 仅测试框架注入；手动设置可能破坏校验边界 |

### 生命周期与并发（1）

| 变量 | 引擎写入时机 | 注意 |
|---|---|---|
| `BUGX_AUTO_PROVISION` | `core/launch_phases/supervisor_degraded.py` | 仅测试框架注入；手动设置可能破坏校验边界 |

### 测试与探针（1）

| 变量 | 引擎写入时机 | 注意 |
|---|---|---|
| `BUGX_TEST_TARGET_DIR` | `core/repair/test_generator.py` | 仅测试框架注入；手动设置可能破坏校验边界 |

