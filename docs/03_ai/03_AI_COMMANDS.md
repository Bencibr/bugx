# BugX AI 命令选路与使用手册

> **版本**：0.1.0+build.470 ｜ **更新日期**：2026-11-10
> 本手册按当前仓库 CLI 解析器整理，覆盖 42 个顶层命令及分组 leaf。宿主和全局引擎可能独立升级；当前安装版的安全帮助入口、命令分发源码与本手册共同确定语法。完整参数表见 [02_COMMANDS.md](../../docs/01_guides/02_COMMANDS.md)，生命周期与协议细节见 [01_AI_REFERENCE.md](01_AI_REFERENCE.md)。

## AI 选命令的固定步骤

1. 把请求归类为：诊断 BugX、启动/恢复项目生命周期、找漏洞、验证测试、治理知识/云同步、生成报告、接入/配置工具。
2. 先选下表中的命令族。安全地查顶层帮助：`bugx help <顶层命令>` 或 `bugx <顶层命令> --help`。只在已确认该 parser 支持时才查 leaf；例如 `bugx help cloud sync` 可读出同步参数，但 `bugx help lesson init` 不支持，而 `bugx supervisor install --help` 在当前 CLI 会实际执行安装，不能拿它探帮助。单层分发器要查本手册与对应 CLI 分发源码，不得把父级 flags 当成 leaf 参数。
3. 标记该次调用的副作用：只读、本地写入、执行目标代码、联网、长时间运行、删除/重置、Git commit。只执行用户请求范围内已授权的动作。
4. 运行后读取退出码和结构化结果；退出码 `2` 在 `engine pre` 中表示等待交互，不能当作普通失败，也不能靠重复执行绕过。

命令帮助、运行时来源和目标路径可能不一致。任何状态或命令不熟时，先用 `which bugx`、`bugx --version`、`bugx --help` 和本手册确认，再按命令族选择只读帮助或查源码；不要引用别的工作区、旧 build 或全局引擎路径的记忆来拼命令。不能假设 `--help` 永远只读：先确认当前解析器把它当帮助选项，而不是传给有副作用的 positional 子命令。
若 `bugx --help` 自身报错或当前安装中找不到该命令，停止执行该命令；先核对 `which bugx`、`bugx --version` 和 traceback，确认命中的是已安装 CLI 还是源码工作树。此时不能把本手册里的旧语法当作可执行证据。

## 42 个顶层命令：什么时候用、如何调用

### 帮助、只读检查与诊断

| 命令 | 什么时候用 | 常见调用与边界 |
| --- | --- | --- |
| `help` | 不确定参数或子命令时 | `bugx help cloud`。只读；优先于猜参数。 |
| `completion` | 用户要求配置 shell 补全时 | `bugx completion zsh`。只输出补全脚本，不擅自写入 shell 启动文件。 |
| `hosts` | 先查看当前项目可连接哪些 AI 宿主 | `bugx hosts --root <ROOT> --json`。只读发现。 |
| `doctor` | 诊断 BugX 安装、宿主或项目状态 | `bugx doctor --root <ROOT> --json`。默认只读；`--repair` 执行已实现的确定性修复。AI 只补充根因分析，不决定修复权限；Doctor 不强制重装整套漂移宿主资产。副本诊断仅在当前 asset 严格高于 global、发现完整且无更高副本时建议 `install --force`。 |
| `log` | 查命令错误、某次调用链、时序或耗时 | `bugx log --root <ROOT> --errors`；按需使用 `--trace <ID>`、`--timing`。只读。 |
| `stats` | 查看跨项目时间和 Token 汇总 | `bugx stats --days 30 --json`。`--global` 扩大到全局统计。 |
| `view` | 安全读取仓库内指定文件的局部切片 | `bugx view --root <ROOT> <PATH> --lines <RANGE>`。按需读取，不把它当作全库检索。 |
| `project` | 建立或查看代码规模、技术栈画像 | `bugx project --root <ROOT> analyze --json`；`show/list` 查看已有画像，`settings` 修改项目配置，`sync` 会向云端看板发送指标。 |
| `self-audit` | 用户要审查 BugX 自身 CLI/MCP 入口 | `bugx self-audit --root <BUGX_SOURCE>`。目标是 BugX，不等于审计业务项目。 |

### 初始化、宿主接入与生命周期控制

| 命令 | 什么时候用 | 常见调用与边界 |
| --- | --- | --- |
| `init` | 用户明确要求执行独立 CLI 初始化操作 | `bugx init --root <ROOT> --cloud-sync <local|sync>`。会写项目状态。`--cloud-sync` 也可作为 engine argument passthrough 传给 `engine pre/audit`，但 `init` 的选择会持久化初始化策略，`pre` 的选择只控制本次启动 policy；不要混淆两者。通过宿主插件启动挖掘时走只读 `engine status --json` → 仅精确 `UNINITIALIZED` 时 `engine pre`；不要把 `init` 当成挖掘启动或损坏状态修复命令。`FATAL_BLOCKED`、未知状态或检查失败时保留现场。源码工作树可能被拒绝作为全局引擎安装来源，不能未经检查就加 `--force`。 |
| `dsh` | 仅在 DSH Web Session 兼容启动流程中 | `bugx dsh --root <ROOT> --help`。仅 DSH 场景；不要当成通用启动器。 |
| `install` | 用户要求安装 BugX 到项目/AI 宿主 | `bugx install --root <ROOT> --agent <HOST>`。不只是写宿主配置：当前实现还会安装/更新全局 `~/.bugx/core`、初始化项目 layout、ledger、画像/生命状态，再写宿主入口；源码树 guard 或 `--force` 会影响全局引擎。先核对来源、root、宿主和既有状态。 |
| `uninstall` | 用户要求移除宿主接入 | 先 `bugx uninstall --root <ROOT> --dry-run`，确认影响面后才执行实际移除；`--purge-*` 会删除数据。 |
| `reset` | 用户要求归档并重建项目运行态/知识库 | 先 `bugx reset --scope project --root <ROOT> --dry-run`。实际 reset 会改变状态，需明确授权；不能用来试错。 |
| `mcp` | 查看协议、运行/维护本地 Supervisor MCP | 子命令有 `serve`, `prune`, `schema`, `spy`, `docs`。`serve` 是常驻服务；`schema/docs` 用于查契约；`prune` 会清理配置。 |
| `capability` | 先判断项目需要什么 MCP/工具，或按计划安装 | `bugx capability plan --root <ROOT> --json` 只读；`install` 会安装运行时和修改接入。 |
| `mcp-runtime` | 管理 lock-hash 离线 MCP runtime | 子命令为 `install`, `verify`, `start`, `rollback`。先 `verify`；安装/启动/回滚会变更本机运行时。 |
| `engine` | 查询或推进某个项目的 BugX 生命周期 | `bugx engine --root <ROOT> status --json`；再按需要 `pre`。源码开发先用 `BUGX_DEV_MODE=1 bugx engine --root <ROOT> path --name launch_bugx.py` 验证 engine 来源；`pre --dev` 另有自动修复/按轮次提交副作用。`pre` 是启动门禁，不代表已经挖掘；`workers/post` 是生命周期阶段，不是通用 CLI 参数容器。内部帮助和资源用 `bugx engine path --name <NAME>`。 |
| `supervisor` | 排查/控制本地 Supervisor 状态、Claim、Finding 或恢复流程 | 子命令有 `install`, `status`, `rpc`, `reset-budget`, `recover`, `rebind-target`, `gate`, `repair-fence`, `findings`, `next-round`, `finish`。`recover/rebind/reset-budget/finish` 会改状态，只有诊断确认和授权后使用。 |
| `plugin` | 查看或管理 BugX 插件包 | 先 `list/status` 查看，再按需 `install/uninstall/zcode`；安装可能访问源并写入本机插件目录。 |
| `update` | 用户明确要求升级 CLI 或检查新版本 | `bugx update --check`（若当前版本支持；以实时帮助为准）。实际更新会修改全局安装。 |
| `daemon` | 用户要求后台持续挖掘或重连已有守护进程 | `status` 只读；`start/stop/attach` 控制长期进程，必须确认 root 和生命周期。 |
| `config` | 用户要求修改 BugX 本机隐私/遥测配置 | 先 `bugx config set --help` 看有效键和值；`set` 会改本机配置，不自行降低隐私保护。 |

### 审计、挖掘与测试

| 命令 | 什么时候用 | 常见调用与边界 |
| --- | --- | --- |
| `probe` | 只检查一种明确的静态/动态能力 | 先 `bugx probe --root <ROOT> <KIND> --help`。当前种类含 `efficiency`, `sast`, `mobile`, `ios_binary`, `codebase-memory`, `platform`, `memory`, `pattern`, `differential`, `openwiki`；按探针自己的范围执行。 |
| `autonomous` | 用户明确要求由指定宿主启动一轮前台无人值守挖掘 | `bugx autonomous --root <ROOT> --host <HOST> --help`。长时间运行；检查轮数、时间、模型、覆盖清单和既有 fix/commit 策略，不能默认扩大到修复/提交。 |
| `goal` | 用户给出可验收目标，需要长程自治收敛 | `bugx goal --root <ROOT> --host <HOST> '<GOAL>'`。会持续多轮；先确认时限、开放 Finding 处理和坏单元隔离策略。 |
| `audit` | 用户要项目级审计，且已确认所需审计模式 | `--goal find_bugs|repair_preview` 是隔离副本的原生图任务；`--deep/--format` 是另一条全息审计路径。读返回的真实状态和观察记录；等待原生 Agent 的任务不能报告成审计完成，模板或演练指标不能冒充目标漏洞证据。 |
| `mining-audit` | 探测可挖掘单元，或审查/加固挖掘计划 | `explore --root <ROOT> --json` 勘测；`audit <PLAN>` 评估计划；`harden <PLAN>` 会写新计划文件。 |
| `test-generate` | 用户明确要求生成测试或自包含 PoC | `bugx test-generate --root <ROOT> --scope <PATH::SYMBOL> --out <FILE>`。会新建文件；先选定符号、输出路径和测试命令。 |
| `test-audit` | 判断一份现有测试是否有有效断言和覆盖 | `bugx test-audit --scope <SCOPE> <TEST_FILE> --json`。审查测试质量，不运行目标项目审计。 |
| `test-verify` | 用户要求用变异验证测试是否能抓到缺陷 | 子命令 `mutate` 仅预览变异；`verify` 在隔离沙箱执行目标/测试代码，执行前核实路径、命令、超时和授权。 |
| `logic-analysis` | 校验或绑定意图、逻辑链、Oracle 与 Finding 证据包 | `validate`, `bind`, `bind-finding`；先看输入 schema。它处理结构化证据，不代替代码扫描。 |
| `patch-forge` | 生成或静态检查 BugX 补丁包 | `--from-issue <SPEC>` 生成；`--verify <DIR>` 校验。`--test-red-green` 会执行沙箱验证；`--submit` 会联网，必须有对应授权。 |

### 云端、知识、报告与交付

| 命令 | 什么时候用 | 常见调用与边界 |
| --- | --- | --- |
| `cloud` | 查看同步状态、治理冲突或同步云端知识 | 先用 `status --scope <...> --json`；直接同步时显式指定 `sync --scope project|global|all`。当前缺省是 `all`。`onboard on`、`init --cloud-sync sync` 和已持久化 sync 决策后的 `engine pre` 会走 onboarding 默认路径，可能同步 global + project；它们不等价于 `cloud sync --scope project`。 |
| `auth` | 管理 BugX 云端登录态 | `status` 只读；`login` 会打开授权流程；`set-key/logout` 修改或清除凭据。禁止打印/保存密钥到报告。 |
| `telemetry` | 查看、导出或发送脱敏飞轮数据 | `inspect` 本地查看；`export` 生成本地包；`flush/upload` 会出网并清空/发送队列，必须由用户明确要求。 |
| `knowledge` | 查看或治理候选知识、项目/全局知识包 | `status/export` 查看或导出；`approve/import/rollback` 会改知识库。此命令本身不会自动同步云端。 |
| `lesson` | 管理项目级结构化错题集、Finding 经验和工具经验 | 子命令完整列表在下方；先确认 ledger/root、数据类型和是否会修改/归档。不要把 lesson 当作普通 Markdown 文件。 |
| `skill` | 查看、校验、导入、回滚或拉取技能包 | `list/status/validate` 查询；`import/rollback` 修改本地包；`pull` 可能联网。先看包 ID、来源和签名契约。 |
| `advisory` | 列出现有 GHSA 或生成标准公告/PR 草案 | `list` 查询；`export` 生成本地草案。导出不等于发布 GitHub Advisory 或创建 PR。 |
| `report` | 对已有审计产物生成 formal/visual/both 报告或本地预览 | `formal`, `visual`, `both`, `serve`。需要明确现有产物目录；报告生成本身不会补做漏洞挖掘。 |

## 当前 CLI 分组子命令清单

以下是同一份 `bugx --help` 对应的子命令组。记住命令路由，不记忆易漂移的 flag；运行前仍要查最末级帮助。

- `mcp`: `serve`, `prune`, `schema`, `spy`, `docs`
- `capability`: `plan`, `install`
- `mcp-runtime`: `install`, `verify`, `start`, `rollback`
- `logic-analysis`: `validate`, `bind`, `bind-finding`
- `probe`: `efficiency`, `sast`, `mobile`, `ios_binary`, `codebase-memory`, `platform`, `memory`, `pattern`, `differential`, `openwiki`
- `knowledge`: `status`, `export`, `approve`, `import`, `rollback`
- `skill`: `list`, `status`, `validate`, `rollback`, `import`, `pull`
- `cloud`: `config`, `status`, `log-report`, `feedback`, `sync`, `export`, `upload`, `conflicts`, `dead-letter`, `projects`, `devices`, `onboard`
- `auth`: `login`, `status`, `set-key`, `logout`
- `telemetry`: `inspect`, `export`, `flush`, `upload`
- `config`: `set`
- `daemon`: `start`, `stop`, `status`, `attach`
- `supervisor`: `install`, `status`, `rpc`, `reset-budget`, `recover`, `rebind-target`, `gate`, `repair-fence`, `findings`, `next-round`, `finish`
- `plugin`: `list`, `status`, `install`, `uninstall`, `zcode`
- `project`: `analyze`, `show`, `sync`, `list`, `refresh`, `settings`
- `lesson`: `init`, `bootstrap`, `migrate`, `validate`, `rebuild`, `rebuild-fts`, `archive-events`, `incident-archive`, `retrieve`, `record`, `review`, `feedback`, `metrics`, `hygiene`, `merge`, `retire`, `revoke`, `export`, `backup`, `replay`, `incident-add`, `incident-lookup`, `incident-activate`, `incident-revoke`, `tool-feedback`, `tool-metrics`, `tool-lookup`, `tool-matrix`, `export-book`, `promote`, `promote-incident`, `promote-tool`, `inherit`, `evidence-add`, `incident-list`, `consume`, `tool-exp-export`, `tool-exp-import`
- `mining-audit`: `audit`, `explore`, `harden`
- `test-verify`: `verify`, `mutate`
- `advisory`: `list`, `export`
- `report`: `formal`, `visual`, `both`, `serve`

## 每个子命令的用途速查

以下清单把当前安装版的每个分组 leaf 都列出。它解释“何时选它”和常见影响，不替代运行时参数表；安装版升级后先重读 `bugx --version` 与 `bugx --help`。带有“写入/联网/运行”的项不能只根据名称执行。

### 安装、生命周期与运行时

| 命令 | 用途与影响 |
| --- | --- |
| `mcp serve` | 启动本地 MCP Supervisor 常驻 stdio 服务；仅由宿主配置调用，前台手工运行会占用当前进程。 |
| `mcp prune` | 清理未使用/过期 MCP 配置；先看 dry-run/精确目标，属于配置写入。 |
| `mcp schema` / `mcp docs` | 输出 MCP 工具 schema 或说明；查协议、集成或参数契约时使用，只读。 |
| `mcp spy` | 诊断 MCP 请求/轮询；用于宿主调用异常定位，可能读取本地日志。 |
| `capability plan` / `capability install` | `plan` 只读生成能力建议；`install` 按计划安装运行时与宿主能力，会写本机/项目配置。 |
| `mcp-runtime verify` | 校验已有锁定 runtime 的文件与 hash，只读。 |
| `mcp-runtime install` / `start` / `rollback` | 分别安装、启动或回滚隔离 runtime；会改变本机运行环境，按当前 `--server` 和 runtime 路径核对目标。 |
| `engine status` | 读取项目生命周期状态；启动前、失败后先用它。 |
| `engine pre` | 准备启动并执行门禁；退出码 2 表示宿主必须处理交互，不表示已经开始挖掘。按门禁 action 映射成对应命令，不能把 `value/action` 拼到 `pre` 后。 |
| `engine workers` / `heartbeat` / `post` | 只在当前 lifecycle 阶段要求时生成 Worker、执行安全心跳或结算；不作为任意 CLI 参数容器。 |
| `engine judge` | 判读式并发审计（适合小/中项目）。`bugx engine --root <ROOT> judge N` 宿主确定性完成 claim+边界电池取证并输出 N 个判读包提示词（~1s/单元；机器初审直接关单平凡单元，零模型回合）；把每个判读包并发交给 `bugx-judge` 子代理（一回合只输出裁决 JSON）；收齐后合并为数组写入文件，`bugx engine --root <ROOT> judge --settle <verdicts.json>` 实跑复现取证并机械收口（~0.4s/单元）。judge 不碰 claim/心跳/checkpoint 协议。 |
| `engine pause` / `resume` / `abort` | 暂停、恢复、终止当前生命周期；改运行态，确认任务 ID/当前轮后使用。 |
| `engine bootstrap-coverage` | 初始化或补建覆盖资产；先检查当前状态与已有清单，不可用于绕过 layout gate。 |
| `engine prompt` / `path` | 读取阶段提示或全局引擎资源；`path` 只读，`prompt` 按阶段取协议。 |
| `supervisor status` / `gate` / `findings` | 查看监督状态、门禁与 Finding；优先用于诊断，不把空列表解释为全库无漏洞。 |
| `supervisor rpc` | 通过 Supervisor 协议执行已定义 RPC；先看 schema 与请求副作用。 |
| `supervisor next-round` / `finish` | 在 Supervisor 允许时推进下一轮或完成当前轮；会改 lifecycle 状态。 |
| `supervisor install` | 安装 canonical `bugx-supervisor` MCP；这与启动 gate 的 `install_supervisor_mcp` action 不同。不要运行 `bugx supervisor install --help`：当前 positional dispatcher 会执行安装。 |
| `supervisor reset-budget` / `recover` / `rebind-target` / `repair-fence` | 监督预算、恢复、目标绑定或修复围栏的带外操作；可能改变运行状态，先读 status/日志和精确对象。 |
| `daemon status` / `attach` | 查询或连接已有常驻任务；`attach` 会连接长运行会话。 |
| `daemon start` / `stop` | 创建或停止后台生命周期进程；先确认项目 root 和是否存在运行任务。 |

### 云、知识与项目配置

| 命令 | 用途与影响 |
| --- | --- |
| `cloud config` | 查/改云端 API 地址或版本检查设置；写配置，不含登录凭据。 |
| `cloud status` | 查询同步 cursor、队列、失败和冲突；按 `project|global|all` 缩小只读范围。 |
| `cloud sync` | 立即上传/拉取知识；CLI 允许省略 `--scope`，但 AI 为避免隐式扩大范围应显式写 `--scope project|global|all`。省略时读取配置，配置缺失默认 `all`；`summary` 降低部分详情粒度，但仍是联网传输。 |
| `cloud onboard status` / `on` / `off` | 查看、启用或停用项目云同步。`on` 在当前 CLI 会立即同步 global + project；不要拿它代替项目专用 `cloud sync --scope project`。 |
| `cloud conflicts list` / `resolve` | 查看冲突或按 local/remote 决议解决；`local` 主要本地处理，`remote` 会联网拉取权威内容，失败时冲突保持 open。 |
| `cloud dead-letter list` / `purge` | 查询或清理失败同步死信；purge 删除队列项，先检查内容和恢复方案。 |
| `cloud projects list` / `add` | 查看云项目或创建/绑定项目身份；`add` 联网并写 `<ROOT>/.bugx/cloud-project.json`。Git commit 需用户另行明确授权。绑定文件存在不等于 knowledge/profile 已上传。 |
| `cloud devices` | 查项目绑定设备；可能需要网络请求。 |
| `cloud export` / `upload` | 导出本地 V2 bundle（本地写文件），或上传并恢复远端同步基线（联网且可能覆盖基线）。 |
| `cloud feedback` / `log-report` | 批量上报错题卡质量反馈或诊断日志；均会出网，仅在用户明确要求且数据范围清楚时用。 |
| `auth status` | 查看本地登录态；加 `--verify` 才发网络请求验证。 |
| `auth login` / `set-key` / `logout` | 登录、写入密钥或清除凭据；绝不在 shell 输出/报告中回显 secret。 |
| `telemetry inspect` / `export` | 本地检查或导出独立的脱敏飞轮 outbox；`inspect` 读本地审计，`export` 创建加密离线包，不等于 cloud knowledge bundle。 |
| `telemetry flush` / `upload` | 发送独立遥测队列，默认 batch size=20；成功项从 outbox 清除、失败项保留并递增尝试；涉及网络与数据披露，需要明确授权。 |
| `knowledge status` / `export` | 查看/导出知识库；export 创建本地包。 |
| `knowledge approve` / `import` / `rollback` | 批准候选、导入知识或回退记录；修改知识库，先核实 bundle/版本/审查人。 |
| `project analyze` / `refresh` | 分析或刷新项目画像，写入画像资产。 |
| `project show` / `list` | 查询项目画像，不上传代码。 |
| `project settings` | 修改项目画像/分析设置，先确认当前值和要改的字段。 |
| `project sync` | 登记/绑定项目并把紧凑项目画像与代码规模指标上传云端；完整 profile 留本地。它不是知识库同步，仍会联网，成功 JSON 应包含 `status=ok`、`project_id`、`synced=true`。 |
| `config set` | 修改本机 BugX 隐私/遥测配置；先使用有效键/值，不得为了过门禁降低隐私级别。 |
| `lesson init` / `bootstrap` | 仅在 ledger/seed/schema 完全缺失且项目 layout 有效时建立项目知识账本；会写入知识数据。 |
| `lesson migrate` | 对现有 ledger 做 schema 升级；先备份并查看当前 revision。 |
| `lesson validate` | 校验账本；`--deep` 还检查事件链、关系、receipt、tombstone、FTS 与归档，只读。 |
| `lesson rebuild` | 重建 SQLite；`--from-seed --confirm-data-loss` 是数据恢复操作，会替换运行库，不能当初始化。 |
| `lesson rebuild-fts` | 仅重建全文检索索引；确认 DB 主数据可读后使用。 |
| `lesson retrieve` | 按 query/category 与预算检索经验卡，只读。 |
| `lesson record` / `consume` | 写入经验卡或消费事件；仅在有证据的 Finding/调用链和目标类别后使用。 |
| `lesson review` / `feedback` / `metrics` | 审查记录、反馈质量、查询指标；feedback/metrics 可能写入反馈或消费事件，按 CLI JSON 结果核验。 |
| `lesson hygiene` / `replay` / `tool-matrix` | 做知识清理/离线回放/工具能力矩阵；先选 cases/gain/tools 模式和预算。 |
| `lesson merge` / `retire` / `revoke` | 合并、退役或撤销记录；会改状态，先定位 lesson/pattern id 与 reason。 |
| `lesson export` / `backup` / `export-book` | 导出账本、备份或经验手册；会创建本地文件，先确认输出路径。 |
| `lesson archive-events` / `incident-archive` | 归档历史事件或事件记录；按保留 tail、时间和最大条数设边界。 |
| `lesson incident-add` / `incident-list` / `incident-lookup` | 增加、列举或查询 incident 记录。 |
| `lesson incident-activate` / `incident-revoke` | 启用或撤销 incident 模式；改变匹配策略，必须明确记录 ID。 |
| `lesson evidence-add` | 写入 incident 证据 receipt 前，复验规范 `.bugx` 布局中的 Supervisor Finding、claim、execution receipt、coverage evidence 与当前工件 SHA-256；无法建立唯一绑定时拒绝。该本地 hash-chain 不等于密钥签名或真实 ACP/宿主执行证明。 |
| `lesson promote` / `promote-incident` / `promote-tool` / `inherit` | 提升或继承项目经验到更高作用域；可能共享全局知识，不可由项目内推断全局授权。 |
| `lesson tool-feedback` / `tool-metrics` / `tool-lookup` | 记录、统计或检索工具经验；`tool`/`signature` 需与被测调用一致。 |
| `lesson tool-exp-export` / `tool-exp-import` | 导出或导入工具经验；导入会写 ledger，先验来源和文件格式。 |

云端命令的最小 AI 决策契约：

1. 先运行 `bugx cloud status --root <ROOT> --scope all --json`；需要网络证据才运行 `bugx auth status --root <ROOT> --verify --json`。
2. 用户只说“同步”而没有 scope 时，不能默认缩小范围；当前 `cloud sync` 缺省读取配置，配置缺失时为 `all`。先询问是 `project`、`global` 还是 `all`，并记录 `full|summary` 与 `block|redact|allow`。
3. 用户说“上传数据”时先分类：knowledge、project profile、telemetry、diagnostic log、lesson feedback 或指定 bundle。没有数据面和授权时返回 `NEEDS_USER_SCOPE`，不执行联网命令。
4. `cloud export` 先本地检查，`cloud upload` 才出网；上传必须保存输入 bundle SHA-256、mode、snapshot_id/version 和上传后的 status。`project sync` 必须单独检查 `status=ok/project_id/synced=true`。
5. `telemetry flush/upload`、`log-report`、非 dry-run `feedback` 都是独立网络动作；不能用 knowledge sync 的成功证明它们成功。
6. `data_sharing_revoked`、open conflict、认证/TLS/权限失败、`ack_pending=true` 或 snapshot 收敛失败时停止并报告 `BLOCKED`/`RECOVERY_REQUIRED`。只有明确 cursor/baseline 失配才建议 `--calibrate`；不要用 purge、reset 或重复 upload 掩盖不确定状态。

### `lesson` 每个 leaf 的参数与用法

`bugx lesson --help` 是本版本的安全总帮助，会列出全部共享参数；`bugx lesson <leaf> --help` 不提供可靠的 leaf 帮助。所有命令默认针对 `<ROOT>` 的项目 ledger，可用 `--ledger <PATH>` 显式改目标。以下 required 参数按当前 `command_lesson` 分发实现整理：

| Leaf | 怎么用 / 关键参数 | 用途与副作用 |
| --- | --- | --- |
| `init` / `bootstrap` | `bugx lesson init --root <ROOT> --json`；可指定 `--ledger` | 初始化账本；目标 seed 不存在时会从 bundled JSON seed 复制。只用于经状态确认的全新 ledger。 |
| `migrate` | `bugx lesson migrate --root <ROOT> --json` | 升级现有 ledger schema；若目标 seed 路径不存在，当前 wrapper 也会先复制 seed，故先核对目标路径。 |
| `validate` | `bugx lesson validate --root <ROOT> [--deep] --json` | 校验 ledger；`--deep` 校验事件链/关系/receipt/tombstone/FTS/归档。只读。 |
| `rebuild` | 必须同时给 `--from-seed --confirm-data-loss` | 重建的是**全局** SQLite projection；先从 `~/.bugx/knowledge/` 找到 global seed，原 DB 被移到 `.corrupt.bak`。高影响恢复操作。 |
| `rebuild-fts` | `bugx lesson rebuild-fts --root <ROOT> --json` | 重建全文检索索引；仅主 ledger 正常时用。 |
| `archive-events` | 必须给 `--output <DIR>`；可设 `--keep-tail N` | 将旧事件归档到指定目录并保留尾部事件；会改账本和创建归档文件。 |
| `incident-archive` | 可设 `--older-than-days N`、`--max-archive N` | 归档较旧 incident；默认 90 天、每次最多 50 条。 |
| `retrieve` | `--query <TEXT>`；可加 `--category`、`--limit`、`--token-budget`、`--byte-budget`、重复 `--exclude-lesson-id` | 按需检索 lesson；只读，选择与当前问题同类的经验。 |
| `record` | 必须给 `--input <JSON>` | 从结构化 JSON 新增 lesson；输入需有 Finding/调用证据，不凭推测造经验。 |
| `review` | 必须给 `--input <JSON>` | 写入 round/lifecycle review；只在真实完成一轮并有结果时用。 |
| `feedback` | 必须给 `--lesson-id`、`--lifecycle-id`、`--finding-fingerprint`、`--outcome`、`--round`；可给 `--finding`、`--signature` | 记录 lesson 命中质量反馈；绑定具体 Finding 与轮次。 |
| `metrics` | `bugx lesson metrics --root <ROOT> --json` | 查询 ledger 指标；不代替命中反馈。 |
| `hygiene` | 可用 `--min-feedback N`（默认 3） | 检查过期/低反馈条目；按输出复核再治理。 |
| `merge` | 必须给 `--source-pattern-id`、`--target-pattern-id`、`--reason` | 合并 pattern；会改变 ledger 关系，先校验两个 ID 与理由。 |
| `retire` / `revoke` | 必须给 `--lesson-id`、`--reason` | 退役或撤销 lesson/pattern；修改状态，留足理由。 |
| `export` / `backup` / `export-book` | 必须给 `--output <FILE>` | 分别导出 snapshot、备份 DB、生成 Markdown 经验手册；写本地文件，先确认路径。 |
| `replay` | 必须给 `--cases <FILE>`；可设 `--mode cases|gain|tools`、`--limit` 和预算 | 离线回放检索/incident/tool 案例；读取输入并创建评估结果，不代表生产 Finding。 |
| `incident-add` | 必须给 `--input <JSON>` | 添加 incident 复现记录。 |
| `incident-list` | 可设 `--pattern-id`、`--limit`、`--offset` | 列举 incident；只读。 |
| `incident-lookup` | `--query <TEXT>`；可重复 `--signature`、`--limit` | 按相似上下文/签名检索 incident；只读。 |
| `incident-activate` | 必须给 `--incident-id`、`--evidence-receipt-id` | 重新复验 receipt 到 Supervisor Finding、claim、coverage evidence 和当前工件，再激活 incident；改匹配状态。 |
| `incident-revoke` | 必须给 `--incident-id`、`--reason` | 撤销 incident；会改变治理状态。 |
| `evidence-add` | 必须给 `--input <JSON>` | 只写入可与规范项目布局中的 Supervisor Finding、claim、coverage evidence 记录及当前工件重新绑定的 incident receipt；任一事实缺失或不唯一即拒绝。它不提供密码学来源认证。 |
| `consume` | `--input <JSON>`，或同时给 `--event-id`、`--subject-type`、`--subject-id`、`--event-type`；可选 `--lifecycle-id`、`--run-id` | 记录一次经验消费事件；会写 ledger，避免重复记账。 |
| `tool-feedback` | `--input <JSON>`，或给 `--category`、`--outcome`、`--finding-fingerprint`、重复 `--tool`、`--lifecycle-id`、`--round` | 记录工具对 Finding 的贡献/反馈；只在实际工具调用后使用。 |
| `tool-metrics` / `tool-matrix` | `bugx lesson tool-metrics|tool-matrix --root <ROOT> --json` | 查看工具效果统计或场景→工具矩阵；只读。 |
| `tool-lookup` | `--query <TEXT>`；可加 `--category`、`--limit`、预算 | 按任务上下文检索工具经验；只读。 |
| `promote` | 必须给 `--pattern-id` 或 `--lesson-id` | 将项目 lesson 提升到全局知识；影响跨项目共享，需单独授权。 |
| `promote-incident` | 必须给 `--incident-id`、`--evidence-receipt-id` | 消费前重新核验 receipt、Supervisor 状态及工件字节，再提升 incident 到全局知识；跨项目共享。 |
| `promote-tool` | 必须给 `--tool`、`--category` | 提升工具经验到全局知识；跨项目共享。 |
| `inherit` | 不需要额外参数 | 从全局知识继承到项目 ledger；会写项目账本。 |
| `tool-exp-export` | `bugx lesson tool-exp-export --root <ROOT> --json` | 从当前 ledger 导出工具经验结构化结果。 |
| `tool-exp-import` | 必须给 `--input <JSON>` | 把工具经验导入当前 ledger；会写数据，先核验来源与 JSON。 |

`--json` 控制结构化输出，不会把写操作变成只读。`promote*` 写全局层，`inherit` 写项目层；它们与 `cloud sync` 是不同操作，不能互相替代。

### 探测、审计、测试、报告与交付

| 命令 | 用途与影响 |
| --- | --- |
| `probe sast` / `pattern` / `memory` / `efficiency` | 选择静态安全、模式、内存或效率探针；范围与是否执行目标代码按该探针契约核实。 |
| `probe mobile` / `ios_binary` / `platform` | 移动端、iOS 二进制或平台专项探针；只在目标类型匹配时用。 |
| `probe differential` / `openwiki` / `codebase-memory` | 差分探测、外部知识查询或知识图谱探测；核对网络、索引与数据传输边界。 |
| `logic-analysis validate` | 校验意图、Oracle、证据包结构；诊断/前置检查。 |
| `logic-analysis bind` / `bind-finding` | 把证据绑定到逻辑链或 Finding；会写结构化证据状态。 |
| `mining-audit explore` | 勘测可挖掘单元；建立覆盖/计划基线。 |
| `mining-audit audit` | 评价现有挖掘计划；输出质量审计结果。 |
| `mining-audit harden` | 根据审计生成加固后的计划；先指定输出路径。 |
| `test-generate` | 为明确符号生成测试/PoC 文件；必须确定 scope、输出路径和目标测试框架。 |
| `test-audit` | 评估既有测试断言与覆盖质量；默认是分析，不等于运行全套测试。 |
| `test-verify mutate` | 生成/预览可用变异；不执行变异测试。 |
| `test-verify verify` | 在隔离沙箱执行目标与测试验证；先核实执行命令、路径、超时和沙箱。 |
| `audit --goal find_bugs|repair_preview` | 对隔离副本启动原生图审计或修复预览；读取状态/receipt，等待中的任务不算完成。 |
| `audit --deep` | 运行另一条全息攻防审计路径；长时间运行目标代码，先确认授权范围和产物目录。 |
| `autonomous` / `goal` | 前台无人值守单轮挖掘或长程目标循环；会调用外部 AI 宿主并持续改 `.bugx/`，须核定 host、预算、时限与修复/提交策略。 |
| `patch-forge --from-issue` / `--verify` | 生成或静态校验补丁包；`--test-red-green` 会执行沙箱测试，`--submit` 会联网。 |
| `advisory list` / `export` | 查看公告或生成本地 GHSA/PR 草案；不会自行发布。 |
| `report formal` / `visual` / `both` | 基于已有产物生成正式、可视或双轨报告；不会替代挖掘。 |
| `report serve` | 启动本地预览服务；是常驻进程，确认监听地址并在结束后关闭。 |
| `plugin list` / `status` | 查询插件包和安装状态。 |
| `plugin install` / `uninstall` / `zcode` | 管理私有插件或 ZCode 接入；会改本机/项目插件配置。 |
| `skill list` / `status` / `validate` | 列出、查询、校验技能包。 |
| `skill import` / `pull` / `rollback` | 导入、联网拉取或回滚技能包；先核实包来源、hash 与目标版本。 |

### 基础诊断和维护命令（非分组）

| 命令 | 用途与影响 |
| --- | --- |
| `hosts` / `doctor` / `log` | 分别发现宿主、诊断安装/项目、查错误和调用链；优先用于排障。`doctor --repair` 会写入已实现的修复；`bugx update --repair-assets` 才会写入 CLI 安装资源。 |
| `view` / `stats` | 安全读取源码切片、查看统计；stats 的 `--global` 会扩大读取范围。 |
| `help` / `completion` | 查帮助、输出 shell 补全文本；completion 不负责改写 shell 配置文件。 |
| `self-audit` | 只读自审 BugX CLI/MCP 入口；目标要是 BugX 源码。 |
| `init` / `reset` / `uninstall` | 项目初始化、归档后重建、移除宿主入口；分别会写项目态、重建状态或删除宿主配置。检查 dry-run 与备份范围。 |
| `install` / `update` | `install` 会安装/更新全局引擎、初始化项目运行态/ledger 并写宿主入口；`update` 更新 CLI/全局引擎。若来源是含脏改动的 BugX 源码树，source-tree guard 拒绝后不得自行追加 `--force`。 |
| `dsh` | 仅 DSH Web Session 兼容启动。 |

## 任务场景的最短命令路线

| 需求 | 顺序 |
| --- | --- |
| 第一次挖掘 | `bugx --version` → `bugx engine --root <ROOT> status --json` → 核对 Git/数据/布局 → `bugx engine --root <ROOT> pre` → 按门禁 action 选路；通过后才按当前 phase 进入 `workers`/覆盖勘测。 |
| 验证 BugX 源码但保留人工策略 | `env -u BUGX_GLOBAL_ENGINE_ROOT BUGX_DEV_MODE=1 bugx engine --root <ROOT> path --name launch_bugx.py` → 确认输出为 `<BUGX_CHECKOUT>/core/launch_bugx.py` → `bugx engine --root <ROOT> pre --fix-mode log-only --commit-policy manual --cloud-sync local`。路径不匹配立即停止。 |
| 隔离项目中的极速 Dev 生命周期 | 先按上行验证 engine 来源 → `env -u BUGX_GLOBAL_ENGINE_ROOT -u BUGX_ASSET_ROOT BUGX_DEV_MODE=1 bugx engine --root <ROOT> pre --dev --cloud-sync local` → 读取真实 Worker/Finding/post receipt。`--dev` 默认自动修复、自动轮询、按轮次提交，不能在未授权或不可回滚项目使用。 |
| `FATAL_BLOCKED` / layout 不匹配 | `engine status --json` + `doctor` + `log --errors` → 保留状态 → 找出缺失/版本来源 → 只执行明确授权的窄修复；禁止 init/reset/force 猜修。 |
| 项目云同步 | `cloud status --scope project` → 明确联网授权 → `cloud sync --scope project` → 对比远端/本地状态。同步后不要默认执行 `pre`：若启动 onboarding 已持久化为 sync，`pre` 可能再次同步 global + project。 |
| 初始化缺失经验账本 | 仅当 layout 有效且 seed、SQLite DB、manifest 都缺失时 `lesson init` → `lesson validate`；若 SQLite marker/manifest 存在但 DB 缺失，先备份/恢复，不能 rebuild。 |
| 单元漏洞验证 | 定点代码读取 → `test-audit` 或 `test-generate` → 用户授权后运行 `test-verify verify` / 最小 PoC → 保存完整命令、退出码、断言和证据。 |
| 已有审计产物出报告 | `report formal|visual|both --root <ROOT>` → 核对产物清单/哈希；报告不会证明漏洞扫描已执行。 |

## 子命令怎么选

| 命令组 | 什么时候用、对应动作 | 使用边界 |
| --- | --- | --- |
| `mcp` | `schema/docs` 查协议；`serve` 启动常驻服务；`spy` 诊断 MCP 请求；`prune` 清理过期配置 | `serve` 会常驻；`prune` 会写配置。先查当前项目/宿主的 MCP 状态。 |
| `capability` / `mcp-runtime` | `capability plan` 先算项目要什么；`install` 才部署。runtime 用 `verify` 查已有安装，再按需 `install/start/rollback` | 计划与校验只读；安装、启动、回滚会改变本机运行环境。 |
| `engine` | `status` 看生命周期；`pre` 过启动门禁；`prompt/path` 查阶段协议/资源；`workers/post` 只有在状态机当前阶段要求时才用 | `pre` 不等于开始挖掘；按状态机的调用顺序使用。退出码 2 是待决策门禁，不是语法错误。 |
| `supervisor` | `status` 查 Supervisor；`rpc` 走协议回退；`install` 部署 canonical MCP；`findings/next-round/finish` 查看或推进监督状态；`recover/rebind-target/reset-budget/repair-fence` 做定向恢复 | `install` 会写宿主配置；恢复命令会改状态。`install_supervisor_mcp` 门禁 action 的宿主接入命令是 `bugx install --root <ROOT> --agent <HOST> --allow-missing --non-interactive`，不是 `bugx supervisor install`；安装成功后可能必须重启宿主。 |
| `auth` / `cloud` | `auth status` 查看本地认证，`--verify` 才联网验证；`login/set-key/logout` 管凭据。`cloud status` 看 scope 状态，`sync` 同步，`conflicts` 治理冲突，`projects/devices` 查绑定，`onboard` 切换项目同步 | API key/令牌不得写日志。sync 需明确范围；`onboard on` 与启动 `pre` 可能同步 global+project。 |
| `cloud config/log-report/feedback/export/upload/dead-letter` | `config` 管端点；`log-report` 上报诊断日志；`feedback` 上报反馈；`export/upload` 导出或导入同步包；`dead-letter` 处理失败队列 | `log-report/feedback/upload` 会出网；upload 会导入业务数据；先读精确帮助和预览选项。 |
| `lesson` | `init/bootstrap` 初始化缺失的项目账本；`migrate` 升级 schema；`validate` 校验（`--deep` 深校验）；`retrieve/incident-lookup/tool-lookup/metrics` 查询；`record/review/feedback/consume` 记录结果；`incident-add/evidence-add/tool-feedback` 添加证据；`promote* / inherit` 在项目/全局知识间治理；`retire/revoke/merge/archive-events/incident-archive` 管理生命周期；`backup/export/export-book/tool-exp-export` 导出；`tool-exp-import` 导入；`replay/tool-matrix/hygiene/rebuild-fts` 做离线评估或维护 | 参数在 `bugx lesson --help` 共用；当前末级 `bugx lesson <verb> --help` 可能只打印总帮助。若项目布局有效但 seed、SQLite DB、知识 manifest 都缺失，用 `bugx lesson init --root <ROOT> --json` 创建并用 `validate` 验证；若 marker 存在但 SQLite 缺失，先备份/恢复，不能从 seed 重建覆盖可能过期的状态。`rebuild --from-seed --confirm-data-loss` 是显式数据恢复，不是首次初始化捷径。 |
| `knowledge` / `skill` | `knowledge status/export` 查看或导出，`approve/import/rollback` 治理知识；`skill list/status/validate` 查技能包，`import/pull/rollback` 获取或回退技能 | 导入、批准、回退可能覆盖语义或版本；先确认来源、目标及签名/校验结果。 |
| `daemon` / `plugin` | daemon 的 `status` 看后台状态，`start/stop/attach` 管长驻进程；plugin 的 `list/status` 查包，`install/uninstall/zcode` 管插件 | daemon 命令影响长期进程；plugin 安装/卸载会写本机目录。 |
| `project` | `analyze/refresh` 生成或更新画像；`show/list` 查看；`settings` 修改项目设置；`sync` 上传指标 | `sync` 会出网；settings 会写项目配置。 |
| `probe` | 按已确定的问题域选择 `sast/mobile/memory/pattern/differential/...` 探针 | 探针的执行目标与参数以该种类入口为准；先确认它会不会执行目标程序或联网。 |
| `logic-analysis` / `mining-audit` | 前者 `validate/bind/bind-finding` 校验结构化证据；后者 `explore` 勘测、`audit` 评估方案、`harden` 生成新方案 | 证据绑定可能写文件；harden 会新建/覆盖方案，先选定输出路径。 |
| `test-verify` | `mutate` 只生成变异预览；`verify` 执行隔离变异测试 | verify 会执行目标和测试命令，必须先检查路径、测试命令、时限和沙箱。 |
| `advisory` / `report` | `advisory list/export` 查询或导出公告草案；`report formal/visual/both` 基于已有产物出报告，`serve` 本地预览 | export/report 不等于发布漏洞公告；serve 是长驻本地服务。 |

上表按当前安装版命令族给出选路；所有可选值与 leaf flags 仍须对照该安装版帮助。若一次门禁里给出的 action、文字描述和当前帮助互相矛盾，不猜测、不执行扩大范围的命令，先用 `bugx log`/状态文件定位当前状态并让用户确认范围。

## 交互 action 到命令的映射

- 通用规则：先读 question/action 的 label、description、detail，再对照当前源码/CLI；内部 `action` 名不是可直接运行的命令或参数。运行命令前确认其 scope 与用户选择的 scope 完全一致。
- 当前引擎在 `question_id=cloud_sync` 的“立即登录并同步”选项上曾输出 `value=sync`, `action=cloud_sync_all`，但可见说明写“同步本项目”；`engine pre` 随后会调用 onboarding 的默认 `sync_layers`，实际 push/pull global + project。若用户仅授权当前项目同步，这个 action 与范围不一致，必须停下确认，不能用 `cloud sync --scope project` 成功后就假定后续 `pre` 不会再同步全局层。
- 用户明确批准**项目范围直接同步**且现有账本有效时：`bugx cloud sync --root <ROOT> --scope project`。省略 `--scope` 当前默认 `all`；`cloud sync --scope project` 的成功不证明后续 `engine pre` 会保持 project-only。
- `project_identity_missing` 选择 `create_project_identity`：`bugx cloud projects add --root <ROOT>` 会联网登记并写 `<ROOT>/.bugx/cloud-project.json`；不会提交文件。只有用户明确选“创建并提交”时才 stage/commit 此单文件，并复核 cached file list 恰好只有 `.bugx/cloud-project.json`。
- layout 有效但全新的项目账本文件、SQLite DB 和 `knowledge/manifest.json` 都不存在时：`bugx lesson init --root <ROOT> --json` 创建账本，再 `bugx lesson validate --root <ROOT> --json` 验证。marker 已存在但 DB 缺失时不要执行 init/rebuild。
- 启动返回 `install_supervisor_mcp`：只有用户选“安装”后才执行 `bugx install --root <ROOT> --agent <HOST> --allow-missing --non-interactive`。该命令修改宿主配置并要求按门禁提示重启；`bugx supervisor install` 是不同操作，不能代替。
- 选择 `stop_startup` / “下次再说”：结束本轮，不启动 Worker。
- 同一门禁再次出现：不要重跑刚才的命令；用 `bugx engine --root <ROOT> status --json`、`bugx cloud status --root <ROOT> --scope <...> --json` 和相关日志定位哪个状态没变。
