---
title: BugX CLI 命令参考
audience: Public users
status: DOCUMENTATION_UNVERIFIED
source-of-truth: interfaces/cli/bugx_cli/cli.py build_parser and forwarded engine parser
last-verified: 2026-11-10
---

# BugX CLI 命令参考

本参考按当前源码的 build_parser() 命令树整理，engine 部分另外按 round_orchestrator 的实际转发契约整理。命令树快照日期为 2026-11-10；本次文档核对没有执行命令 handler、安装、同步、宿主或测试。

## 1. 阅读规则

- <value> 表示必填值，[value] 表示可选值。
- “可重复”表示 parser 使用重复选项；除非命令另有说明，不要把逗号分隔字符串当成重复参数。
- [R] 只读或查询为主；[W] 会写本地状态或配置；[N] 可能访问网络；[D] 具有删除、覆盖、停止或不可逆风险；[P] 生成报告或 artifact。一个命令可以同时带多个标签。
- 帮助文本不能替代本参考的副作用说明。某些 handler 的 --help 路径和安装/修复逻辑共享代码，使用前先确认目标和源码契约。
- 命令失败时保留 stdout、stderr、JSON、.bugx/logs/bugx.jsonl 和实际退出码；不要用重复重试覆盖第一份证据。

## 2. 顶层命令清单

当前 parser 有 42 个顶层命令：

| 命令 | 子命令或定位 | 主要作用 / 默认行为 |
|---|---|---|
| help | help [COMMAND ...] | 输出命令帮助 |
| completion | bash、zsh、fish | 输出 shell completion |
| init | — | 初始化项目布局和全局运行时 |
| dsh | 透传 dsh_args | 调用 DSH 适配入口 |
| hosts | — | 查看 Host 适配器和检测状态 |
| install | — | 安装或更新 Host 资产 |
| reset | — | 按 project/global 重建状态 |
| uninstall | — | 卸载 Host 或 BugX 自身 |
| doctor | all、mcp-runtime | 检查安装、资产、MCP runtime |
| mcp | serve、prune、schema、spy、docs | 本地 MCP server、schema、日志和 docs index |
| capability | plan、install | 生成或安装能力 runtime 计划 |
| mcp-runtime | install、verify、start、rollback | 管理 MCP runtime |
| lesson | 40 个 ledger 操作 | 知识/lesson ledger 操作 |
| logic-analysis | validate、bind、bind-finding | 校验或绑定逻辑分析输入 |
| probe | 多种 probe kind | 调用受控探针透传入口 |
| knowledge | status、export、approve、import、rollback | 知识 bundle 生命周期 |
| skill | list、status、validate、rollback、import、pull | Skill 包管理 |
| self-audit | 透传 self_audit_args | 自审计入口 |
| engine | 生命周期参数透传 | 调用全局 engine；无参数默认为 status |
| cloud | 配置、同步、冲突、项目等 | 云同步和本地 bundle |
| auth | login、status、set-key、logout | 云端认证 |
| telemetry | inspect、export、flush、upload | 本地遥测查看和传输 |
| config | set | 设置受支持的配置键 |
| daemon | start、stop、status、attach | 管理项目守护进程 |
| autonomous | — | 目标 Host 的自治运行 |
| goal | — | 目标驱动的自治生命周期 |
| supervisor | 多个控制动作 | Supervisor 状态、恢复、Finding 和 gate |
| plugin | list、status、install、uninstall、zcode | 插件管理 |
| update | — | 检查或更新全局安装 |
| patch-forge | --from-issue、--verify、--status | Patch 工件生成/验证/状态 |
| log | 多个观察开关 | 日志、时间线和诊断 bundle |
| stats | — | 本地统计 |
| project | analyze、show、sync、list、refresh、rebind、settings | 项目绑定、分析和设置 |
| view | — | 安全查看项目文件片段 |
| test-generate | — | 生成测试草案/文件 |
| test-audit | — | 审核测试文件 |
| mining-audit | audit、explore、harden | 挖掘计划审计/探索/加固 |
| test-verify | verify、mutate | 测试验证和 mutation |
| audit | — | 项目审计 graph/deep 入口 |
| interaction | list、show、answer、cancel | 查看或处理待回答交互 |
| advisory | list、export | Advisory 查看和导出 |
| report | formal、visual、both、serve | 报告生成和本地服务 |

没有命令时，bugx 输出帮助并返回成功；这不代表项目已经初始化或审计完成。

## 3. 全局选项和退出码

这些选项位于顶层 parser，可放在命令前：

| 选项 | 默认 / 值 | 影响 |
|---|---|---|
| --version | — | 输出声明版本 |
| --log-level {DEBUG,INFO,WARNING,ERROR} | None；环境变量 BUGX_LOG_LEVEL 可覆盖 | 日志级别 |
| --accept-data-sharing | 关闭 | 明确允许当前流程的数据分享 |
| --privacy {standard,strict} | standard | 当前流程隐私策略 |
| --no-backup / --skip-backup | 关闭 | reset/uninstall 等动作跳过外部备份；有数据损失风险 |

常见退出码：

| 退出码 | 解释 |
|---:|---|
| 0 | 该 handler 按自身契约成功；对自治/生命周期仍需检查结构化状态 |
| 1 | 运行失败、未完成、阻塞、预算/期限失败或未处理异常；具体看 JSON 和日志 |
| 2 | CLI 语法、参数、项目布局、硬门禁或已移除入口错误 |
| 130 | 进程被操作员中断 |

Handler 可以返回更具体的非零码。例如 Goal 的 Provider rate-limit 有专门错误路径。不要只根据“有输出”判断成功。

## 4. 安装、项目和系统命令

### 4.1 help 和 completion [R]

    bugx help
    bugx help init
    bugx help engine
    bugx completion bash
    bugx completion zsh
    bugx completion fish

help 接受可变数量的 COMMAND 词；completion 的值只有 bash、zsh、fish。

### 4.2 init [W]

    bugx init [--root ROOT] [--force]
              [--commit-policy {life,round,manual,none}]
              [--cloud-sync {local,sync}]
              [--accept-data-sharing] [--privacy {standard,strict}]

- --root 默认当前目录。
- 非 TTY 中必须显式给出 --cloud-sync local|sync，除非 preset 已提供。
- --force 更新受管理的静态 Agent 文件，不覆盖运行态。
- 初始化可能清理旧项目级 .bugx/core 可执行布局；先确认目标目录。

### 4.3 dsh [W]

    bugx dsh [--root ROOT] [--profile PROFILE] [dsh_args ...]

--root 使用向上发现；--profile 默认 web；其余参数原样透传给 DSH 入口。

### 4.4 hosts [R]

    bugx hosts [--root ROOT] [--json]

--root 指定项目根；--json 输出机器可读的 Host 适配器、检测和能力信息。

### 4.5 install [W]

    bugx install [--root ROOT] [--agent AGENT ...] [--force]
                  [--allow-missing] [--non-interactive]
                  [--locale LOCALE] [--json]

--agent 可重复；当前安装/检测值为 chatgpt、claude、cline、codex、dsh、funide、opencode、pi、qoder、trea、workbuddy、zcode。不指定 --agent 时不要假设会配置所有 Host。--allow-missing 允许缺失可选资产时继续，但不改变 Host 的 ready/ACP 状态。

### 4.6 reset [W][D]

    bugx reset [--scope {project,global}] [--root ROOT]
                [--reset-knowledge] [--reset-identity]
                [--reset-credentials] [--reset-mcp-runtime]
                [--reset-backups] [--dry-run] [--yes] [--force]
                [--no-backup|--skip-backup] [--json]

- --scope 默认 project。
- --reset-knowledge 是项目 knowledge 选项；identity、credentials、MCP runtime、backups 是全局目标选项，非法组合会被拒绝。
- --dry-run 只生成计划；实际 reset 默认需要 --yes。
- 默认创建外部 backup；--no-backup/--skip-backup 明确放弃该保护。
- --force 只在输出明确要求且确认活动 runtime 无法自动恢复时使用。

### 4.7 uninstall [W][D]

    bugx uninstall [--root ROOT] [--agent AGENT ...] [--self]
                   [--dry-run] [--all] [--purge-data]
                   [--purge-global-data] [--force] [--yes]
                   [--no-backup|--skip-backup] [--json]

- --self 删除机器级 BugX-owned 资产；不等同于项目 Host 卸载。
- --all 不能与 --agent 同时使用；无 --agent/--all 时进入指导流程。
- --purge-data 必须与 --all 一起使用；--purge-global-data 还要求 --purge-data。
- purge 默认需要 --yes；--dry-run 可预览。
- 普通卸载尽量保留用户文件、已修改文件和非 owned 文件；全局 engine 的删除边界不同于普通项目 purge。

### 4.8 doctor [R/W]

    bugx doctor [--root ROOT] [target]
                 [--json] [--installation-only]
                 [--repair-assets|--repair-workbuddy-assets|--repair|--fix]
                 [--agent AGENT ...] [--strict] [--ai]

- target 可选 all（默认）或 mcp-runtime。
- --installation-only 只检查安装层；它是 CLI 在未完整导入核心时的安全例外。
- 四个 repair 选项是同一语义的别名；修复会写本地资产，不能当成纯查询。
- --strict 把更多诊断问题提升为失败；--agent 可重复。

## 5. MCP、能力和运行时

### 5.1 mcp serve、schema、prune、spy

    bugx mcp serve [--root ROOT] [--role {host,worker}] [--takeover]
    bugx mcp schema [--role {host,worker}]
    bugx mcp prune [--root ROOT] [--older-than SECONDS] [--yes]
    bugx mcp spy [--root ROOT] [--log LOG] [spy_cmd ...]

- serve 的 --role 默认 host；这是本地 MCP stdio 服务，不会自动创建 .bugx/。
- --takeover 允许接管已有服务时改变本地运行状态，先确认 lease/进程。
- prune 的 --older-than 默认 1800.0 秒；实际清理需要 --yes。
- spy 透传 spy_cmd，并可写 --log；MCP wire log 可能包含敏感路径和参数，默认按私密 artifact 处理。

### 5.2 mcp docs

    bugx mcp docs index [--root ROOT]
                         [--db DB] [--project-id ID] [--version VERSION]
    bugx mcp docs serve [--root ROOT]
                         [--db DB] [--project-id ID] [--version VERSION]
                         [--no-auto-index]

--db 默认 .bugx/knowledge/docs.sqlite3；serve 默认允许自动 index，--no-auto-index 关闭。index 会写本地数据库，不是只读预览。

### 5.3 capability

    bugx capability plan [--root ROOT] [--json]
    bugx capability install [--root ROOT] [--runtime-root ROOT]
                             [--lock LOCK] [--json]

plan 生成能力计划；install 写入 runtime。--runtime-root 和 --lock 用于固定安装目标和锁文件。

### 5.4 mcp-runtime

    bugx mcp-runtime install [--root ROOT] [--runtime-root ROOT]
                             [--lock LOCK] [--json]
    bugx mcp-runtime verify [--root ROOT] [--runtime-root ROOT] [--json]
    bugx mcp-runtime start [--root ROOT] [--runtime-root ROOT]
                            --server SERVER [--print-command]
    bugx mcp-runtime rollback [--root ROOT] [--runtime-root ROOT] [--json]

start 的 --server 必填；--print-command 只打印启动命令而不替你启动服务。install、rollback 和不带 print-only 的 runtime 动作都可能修改全局 runtime。

## 6. 知识、Lesson、Skill 和 Probe

### 6.1 lesson

lesson 的动作名是：

    init bootstrap migrate validate rebuild rebuild-fts archive-events
    incident-archive retrieve record review feedback metrics hygiene merge
    retire revoke export backup replay incident-add incident-lookup
    incident-activate incident-revoke tool-feedback tool-metrics tool-lookup
    tool-matrix export-book promote promote-incident promote-tool inherit
    evidence-add incident-list consume tool-exp-export tool-exp-import

完整调用形状：

    bugx lesson <lesson_command> [common options]

Common options：

| 选项 | 默认 / 说明 |
|---|---|
| --root ROOT | 项目根 |
| --ledger LEDGER | 指定 ledger |
| --input INPUT、--output OUTPUT | 输入/输出 |
| --query QUERY | 默认空字符串 |
| --category CATEGORY | 分类过滤 |
| --limit N | 默认 6 |
| --offset N | 默认 0 |
| --token-budget N | 默认 1500 |
| --byte-budget N | 默认 12000 |
| --exclude-lesson-id ID | 可重复 |
| --finding-fingerprint FP | Finding 指纹 |
| --tool TOOL | 可重复 |
| --signature SIGNATURE、--pattern-id ID、--finding FINDING | 关联信息 |
| --lesson-id ID、--incident-id ID、--evidence-receipt-id ID、--lifecycle-id ID | 对象 ID |
| --round N、--outcome OUTCOME、--event-id ID | 轮次/结果/事件 |
| --subject-type TYPE、--subject-id ID、--event-type TYPE | 事件主体 |
| --source-pattern-id ID、--target-pattern-id ID、--reason REASON | 迁移/关系原因 |
| --min-feedback N | 默认 3 |
| --deep、--from-seed、--confirm-data-loss | 深度、seed、数据损失确认开关 |
| --keep-tail N | 默认 0 |
| --older-than-days N | 默认 90 |
| --max-archive N | 默认 50 |
| --cases CASES | case 输入 |
| --mode {cases,gain,tools} | 默认 cases |
| --holdout-mod N | 默认 5 |
| --holdout-remainder | holdout 开关 |
| --json | JSON 输出 |

confirm-data-loss、archive、rebuild、retire、revoke 和导入/导出动作可能写入或改变 ledger；先用适当的 status/review 查询。

### 6.2 logic-analysis

    bugx logic-analysis <logic_command> --input INPUT
                              [--root ROOT] [--task TASK]
                              [--finding FINDING] [--output OUTPUT]

logic_command 只能是 validate、bind 或 bind-finding；--input 必填。

### 6.3 probe

    bugx probe <probe_kind> [--root ROOT] [probe_args ...]

probe_kind 必须为：

efficiency、sast、mobile、ios_binary、codebase-memory、platform、memory、pattern、differential、openwiki。

其余 probe_args 由具体 probe 透传；probe 可能读取项目或产生 artifact，不应按纯 parser 查询处理。

### 6.4 knowledge

    bugx knowledge <knowledge_command> [--root ROOT]
                             [--bundle BUNDLE] [--output OUTPUT]
                             [--bundle-id ID] [--contributor NAME]
                             [--reviewer NAME] [--reason REASON]

knowledge_command 为 status、export、approve、import、rollback；--contributor 默认 local-supervisor。export/status 可读或生成本地 bundle，approve/import/rollback 会改变知识状态。

### 6.5 skill

    bugx skill <skill_action> [--root ROOT] [--skill SKILL]
                     [--bundle BUNDLE] [--json]

skill_action 为 list、status、validate、rollback、import、pull；--skill 默认 web-audit。import、pull、rollback 可能写入全局或项目 skill。

### 6.6 self-audit

    bugx self-audit [--root ROOT] [self_audit_args ...]

其余参数透传自审计入口；不要把 parser 接受参数当成自审计已经完成。

## 7. engine 转发命令

### 7.1 外层语法

    bugx engine [--root ROOT] [engine_args ...]

--root 必须是已存在的项目目录；engine_args 原样传给全局 engine。没有 engine_args 时默认为 status。CLI 设置项目 root、全局 engine root 和 CLI 版本环境，再运行全局 engine；项目级 .bugx/core 不是当前可执行入口。

当前 engine 命令集合：

pre、post、status、polling、heartbeat、pause、resume、abort、bootstrap-coverage、fix-review、audit、control、workers、judge、findings-confirm、revalidate-candidates、prompt、daemon、coverage、path、help、--help、-h。

help、--help、-h 会在进入真实 phase 前截断，按只读帮助处理。pre 和 audit 不接受 --allow-degraded；该选项明确返回错误。

### 7.2 pre / audit

    bugx engine --root . pre [options]
    bugx engine --root . audit [options]

**何时用**：开始一轮新的挖掘 Round（首次或续轮）。**结果**：建立 Supervisor 会话、
快照防篡改基线（输出 `BH_PRE_BASELINE`）、生成覆盖清单绑定目标；pre 成功后紧跟
`workers` 或 `judge` 派发任务，完成后 `post` 结算。

当前实现读取的选项（值域摘自 `engine pre --help` 真值；何时用/结果见说明列）：

| 选项 | 值域（真值） | 何时用 / 带来什么结果 |
|---|---|---|
| --polling | `auto\|manual` | Round 报告后的续轮策略：auto=自动续轮，manual=人工决定 |
| --review-mode | `mcp-auto\|manual` | 代码审查方式：mcp-auto=Supervisor MCP 自动审查，manual=宿主手动 |
| --fix-mode | `auto\|log-only` | 待确认缺陷处理：auto=自动修复并复验，log-only=只记录不修（保留 open Finding 收口） |
| --commit-policy | `round\|manual\|life\|none`（none 归一为 manual） | 会话代码提交策略：round=每轮自动提交，manual=宿主手动提交 |
| --report-policy | `none\|local\|submit` | 报告处置：none=不共享，local=仅本地+待重放队列，submit=匿名上报云端（隐私出口，非交互 headless 默认 local） |
| --session-recovery | `resume\|new` | 会话恢复策略：resume=接续上次，new=全新会话 |
| --startup-action | `start\|restart\|later` | 处理 MCP_SESSION_STALE 的最终启动动作 |
| --strategy-plan | `approve\|reject\|custom` | 回传策略门禁决策（宿主单选回传通道）；`--approve-strategy` 等价 approve |
| --tooling-choice | `<tool=choice>` 可重复 | 回传工具门禁处置（如 `platform=skip`）；也可经 `BUGX_TOOLING_CHOICES` 回传 |
| --report- VALUE 系（--report-policy 除外） | 见 --help | 其余 report 前缀选项按 --help 现值 |
| --host | `<name>`（缺省读 runtime 或物理探查） | 显式绑定本次 Session 宿主 |
| --max-round-wall-seconds | 正的有限秒数 | 覆盖本轮 Supervisor 墙钟预算 |
| --autonomous | 旗标 | 已校验自治策略：跳过策略提问但不跳过硬门禁 |
| --dev | 旗标 | 开发者极速免交互：autonomous=true、审查=mcp-auto、修复=auto、轮询=auto、提交=round、恢复=resume、启动动作=start、策略=approve；没有显式/profile cloud policy 时才默认 local |
| --cloud-sync | `local\|sync`（engine 转发参数；同名参数也用于 `init`） | 本次 `pre/audit` 的 cloud policy；显式值优先于 Dev 默认和项目 profile。`sync` 是联网/数据分享出口，`local` 不是全局撤销 |
| --allow-serial | 旗标 | 允许串行运行；需 serial reason/授权 |
| --serial-reason | `<文本>` | 串行理由（配合 --allow-serial） |
| --ignore-global-budget | 旗标 | 忽略全局每日 Token 与耗时预算熔断；**需明确授权** |
| --bootstrap-coverage | 旗标 | 启动时自动引导创建缺失的覆盖清单（缺清单的 pre 会停在此 gate） |
| --refresh-probes | 旗标 | 强制重新探测环境探针（跳过缓存） |
| --replay-share-pending | 旗标 | 重放待分享结果 |
| --life-kind | 缺省 `mine` | life 类型 |
| --lesson-query / --lesson-category | `<文本>` | lesson 查询过滤 |
| --startup-interaction | `<选择>` | 启动交互选择回传 |

### 7.2.1 Dev 模式的两层开关、前置条件和验证

`BUGX_DEV_MODE=1` 与 `pre --dev` 不是同一个功能：前者选择源码 engine，后者改变本次生命周期策略。源码 engine 只在当前进程能识别真实 checkout 时生效；显式 `BUGX_GLOBAL_ENGINE_ROOT` 优先于 Dev engine，`BUGX_ASSET_ROOT` 是另一条资产覆盖路径。

仅验证 BugX 源码、保留人工决策时：

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

使用极速生命周期时，必须明确接受自动修复、自动轮询和按轮次提交：

```sh
env -u BUGX_GLOBAL_ENGINE_ROOT \
    -u BUGX_ASSET_ROOT \
    BUGX_DEV_MODE=1 \
    bugx engine --root <PROJECT_ROOT> pre --dev --cloud-sync local
```

验证步骤：

1. `path --name launch_bugx.py` 的输出必须是 `<BUGX_SOURCE_CHECKOUT>/core/launch_bugx.py`；输出仍是 `~/.bugx/core` 时，Dev engine 没有生效。
2. `pre` 退出码 `0` 只表示启动门禁就绪；`2` 表示等待交互，`3` 表示需要重启 Host 后重跑；任何其他非零都要转排障。
3. 不得把 `--dev` 的返回 0、模型文本、Task 打印或 `ready` 当作完整审计成功；仍需 Worker receipt、coverage、Finding、post 和 settlement。
4. 发现 `BUGX_GLOBAL_ENGINE_ROOT`、`BUGX_ASSET_ROOT` 与源码路径混用、未授权目标代码修改、`BLOCKED` 或 `RECOVERY_REQUIRED` 时，保存日志和 diff 后停止，不要连续重试。

退出 Dev 模式时清理本次设置的变量，并恢复用户原先的显式 engine/asset override：

```sh
unset BUGX_DEV_MODE BUGX_DEV_ASSET_ROOT
```

排障：选项值非法时 pre 停在对应 gate 并输出
`BUGX_INTERACTION_REQUEST`；非交互环境 report_policy 自动取安全默认
local；宿主入口 build 与引擎不一致时报
`host_entry_not_ready`——重跑 `bugx install` 后重启宿主会话。详见
[03_TROUBLESHOOTING.md](03_TROUBLESHOOTING.md) §3/§4/§5。

### 7.3 post

    bugx engine --root . post [--final] [--share|--no-share]
                             [--allow-incomplete] [--allow-serial]
                             [--dry-run]

**何时用**：worker/judge 完成后结算本轮；结束当前 Round 用 `post --final`。
**结果**：外部基线核对 → 证据/覆盖校验 → 计命结算 → 生成 Round 报告
（`report_policy=local` 时写入待重放队列）；git 提交策略在此触发。

| 选项 | 值域/语义 | 何时用 / 结果 |
|---|---|---|
| --final | 旗标 | 最终结算并封存当前 Round（终态 COMPLETE/INCOMPLETE/ABORTED） |
| --allow-incomplete | 旗标 | 覆盖不完整时以 INCOMPLETE 诚实封存（**不能**改写为 COMPLETE；业务链路未覆盖时用） |
| --share / --no-share | 旗标 | 生命周期结果分享意图（受 report_policy 约束） |
| --dry-run | 旗标 | 冻结 snapshot 只读预览（评估将做什么，不落盘） |
| --allow-serial | 旗标 | 串行授权相关 |

排障：仍有 open Finding 时 post 拒绝 COMPLETE（auto 模式）——按提示
`pre --fix-mode log-only` 重开新会话再收口；「必须紧接对应的活动
pre/runtime 会话」错误=会话已过期，重新 pre（resume）后再 post。

### 7.4 status、控制和恢复

    bugx engine --root . status [--json]
    bugx engine --root . polling [VALUE]
    bugx engine --root . heartbeat
    bugx engine --root . pause
    bugx engine --root . resume
    bugx engine --root . abort [--reason REASON]
    bugx engine --root . bootstrap-coverage [--confirm] [--fresh-coverage]
    bugx engine --root . control [pause|resume|abort [--reason REASON]|status [--json]]

status 在未初始化项目上可以执行有限的只读检查；其他动作通常写入运行态。bootstrap-coverage --confirm 和 --fresh-coverage 可能建立或重置 coverage 相关状态，先确认目标。

### 7.5 workers

    bugx engine --root . workers [--count N] [--strategy STRATEGY]
                              [--execute] [--dry-run] [--brief]
                              [--provider PROVIDER] [--model MODEL]
                              [--executable PATH] [--host HOST]
                              [--subagent-type TYPE] [--max-workers N]
                              [--stagger-seconds SECONDS]

- --strategy 默认 risk，实现接受 risk、bfs、dfs、hybrid、auto。
- --count、--max-workers 是非负整数；--stagger-seconds 是数值。
- --execute 进入执行路径；无该选项时主要打印可领取的 Worker 任务/提示词。
- --dry-run 和 --brief 控制输出/执行；它们不是全局 dry-run。
- --provider、--model、--executable、--host、--subagent-type 覆盖本次 Worker 装配。

### 7.6 资源查询和其他 engine 命令

#### 资源查询

    bugx engine --root . path --name ENGINE_FILE
    bugx engine --root . path -n ENGINE_FILE

`path` 是 CLI 外层 wrapper 处理的资源查询，不是生命周期 phase：它解析并打印全局 engine 中指定文件的路径，不运行审计，也不写项目运行态；`--root` 仍必须是已存在的目录。

#### 转发到 engine 的命令

| 命令 | 语法和默认值 | 副作用/边界 |
|---|---|---|
| `fix-review` | `fix-review [--ci-detect] [--input JSON] [--duration-seconds N] [--round N] [--signature S] [--summary S] [--context S] [--before S] [--after S] [--diff-ref REF] [--commit-ref REF] [--decision D] [--lifecycle-id ID] [--pattern-id ID] [--finding-fingerprint FP] [--json]` | `--ci-detect` 单独使用时是只读 CI 诊断；普通路径会写入 lesson/incident/runtime 记录。 |
| `judge` | `judge [COUNT] [--all] [--settle DIR] [--settle-dir DIR]`；数字 `COUNT` 默认 `8` | 进入裁决/结算处理；`--all`、`--settle` 会扩大待处理范围或写入结算结果。 |
| `findings-confirm` | `findings-confirm [RECIPE] [--recipe RECIPE] [--all-pending] [--reject FP:CLASS:REASON ...] [--from-verdicts]` | 进入 Finding 确认；`--reject` 可重复；不是“打印候选”或“模型判断”即完成。 |
| `revalidate-candidates` | `revalidate-candidates [--root DIR] [--dry-run] [--json] [--auto-install-deps] [--deps-lock PATH]` | 只生成候选重新验证提案，不自动确认 Finding；非 `--dry-run` 可能写入 `.bugx/state/revalidation-report.json`；依赖安装必须有明确授权和 lock。 |
| `prompt` | `prompt [--stage {l0,gate,explore,mutate,repair}]` | 只生成阶段提示；没有工具调用、checkpoint 或 receipt，不得当成审计执行。 |
| `daemon` | `daemon start [--root DIR] [--foreground] [--max-wall-time SEC] [--interval SEC]`; `daemon stop [--root DIR] [--force]`; `daemon status [--root DIR] [--json]`; `daemon attach [--root DIR]` | engine 转发的 `daemon attach` 不接受顶层 `bugx daemon attach` 的 `--lines/-n`；两者不能混用。start/stop 会改变守护进程状态。 |
| `coverage` | `coverage [discover\|discover-flows\|check\|final-check\|status\|hits\|review-packages\|review-apply] [--root DIR] [--manifest PATH] [--findings PATH ...] [--verdicts PATH] [--reviewed-by ID] [--evidence PATH] [--require-evidence] [--auto-backfill] [--allow-business-partial] [--json]`；命令默认 `check` | `discover-flows` 在未完整时返回 `2`；`review-apply` 需要 verdicts 和 reviewer；`--auto-backfill`、`review-apply` 可能写入 coverage 状态，不是只读检查。 |

这些入口都属于运行时/审计状态链，不应在没有项目状态和 receipt 的情况下单独宣称完成。

## 8. Cloud、认证和 Telemetry

### 8.1 cloud 公共选项

大多数 cloud 子命令接受：

    --root ROOT
    --project-slug SLUG
    --accept-data-sharing
    --privacy {standard,strict}

这些选项不改变命令的基本副作用：`status`/`config` 主要是本地读取或配置写入；`sync`、`upload`、非 dry-run `feedback` 和有日志内容时的 `log-report` 会联网。`--accept-data-sharing` 只有在用户已经明确授权时才可使用；它不是 scope 选择器。

### 8.2 cloud config、status、log-report、feedback

    bugx cloud config [--base-url URL|--clear|--refresh-route]
                       [--disable-version-check {true,false}] [--json]
    bugx cloud status [--scope {project,global,all}] [--json]
    bugx cloud log-report [--tail-kb N] [--kind KIND] [--json]
    bugx cloud feedback [--lifecycle ID] [--run-id ID] [--project PROJECT]
                        [--limit N] [--dry-run] [--json]

- cloud status 的 scope 默认 all，是本地同步状态查询，不主动登录或网络 store。
- log-report 的 --tail-kb 默认 256、--kind 默认 `cli-diagnostics`，是显式网络 POST；不要把它当本地日志查看。
- feedback 的 --run-id/--project 默认空，--limit 默认 100；--dry-run 只预览。
- config 的选项互斥；修改 base URL、路由或版本检查是本地写入。
- `cloud status --json` 返回的是认证元数据、onboarding、项目绑定以及各 scope 的 cursor/error/pending/inflight/dead-letter/conflict 状态；它不是远端健康检查。需要网络认证证据时使用 `auth status --verify`。
- `log-report` 只读取诊断日志尾部；handler 会把 `--tail-kb` 限制到 1..1024 KB，日志缺失或为空时不联网并返回 0。日志可能包含路径和业务上下文，上传前必须审查。
- `feedback` 读取本地 lesson feedback，最多 100 条；`--dry-run` 不联网，空列表也不联网。非 dry-run 会 POST lesson_id/outcome，不是知识同步。

### 8.3 cloud sync、export、upload

    bugx cloud sync [--scope {project,global,all}]
                     [--detail-mode {full,summary}] [--calibrate]
    bugx cloud export [--scope {global,project}]
                      [--detail-mode {full,summary}] --output OUTPUT
    bugx cloud upload --input INPUT [--mode {merge,local_authoritative}] [--json]

- sync 的 scope 默认 None，读取 cloud config 的默认值；detail mode 为 full 或 summary。
- export 的 scope 默认 project，detail mode 默认 full，--output 必填，写本地 bundle。
- upload 的 --mode 默认 merge，local_authoritative 会改变冲突权威方向；这是显式网络动作。两种 mode 都可能采用服务端 snapshot 并更新本地同步基线，必须核对 `snapshot_id`、`snapshot_version` 和上传后的 `cloud status`。
- sync 每个 scope 的实现顺序是 baseline pull → push → post-push pull；`summary` 只降低部分记录详情，不等于敏感数据清除。敏感策略由 `cloud.sensitive_policy` 控制，允许值为 `block|redact|allow`，配置缺失时当前实现缺省为 `allow`。
- `cloud sync` 未指定 `--scope` 时使用 `cloud.sync_scope_default`，缺省为 `all`；`onboard on` 和启动 onboarding 可能直接同步 global + project，不能把它们当 project-only。
- 失败时先看 `cloud status --scope ... --json`、`cloud conflicts list`、`cloud dead-letter list`。`ack_pending` 表示本地可能已提交而 ACK 未完成，不要直接 reset 或盲目重复 upload；`--calibrate` 只用于明确的可恢复 cursor/baseline 失配。

### 8.4 cloud conflicts、dead-letter、projects、devices、onboard

    bugx cloud conflicts [list|resolve|prune] [CONFLICT_ID]
                           [--side {local,remote}]
                           [--scope {project,global,all}] [--all]
                           [--reason-filter TEXT] [--json]
    bugx cloud dead-letter [list|purge] [--confirm]
                            [--scope {project,global,all}]
                            [--older-than-days N] [--json]
    bugx cloud projects {list,add}
    bugx cloud devices
    bugx cloud onboard [status|on|off]

conflicts 的 action 默认 list，scope 默认 all，reason-filter 默认空；dead-letter action 默认 list，scope 默认 all，older-than-days 默认 0。resolve、prune、purge、onboard on/off 可能改变本地同步状态或远端意图；先用 list/status。

- `conflicts list` 和 `prune` 是本地队列治理；`resolve --side local` 保留本地方向，`resolve --side remote` 会联网拉取远端权威内容。`--all` 与单个 `CONFLICT_ID` 互斥，批量 remote 解决有实现上限，失败项应保持 open。
- `dead-letter purge` 没有 `--confirm` 时只是预览；确认后先归档到 `.bugx/cloud-sync/dead-letter-archive-<timestamp>.json` 再删除已归档行。删除前必须保存 list/status 证据。
- `cloud projects list|add` 会访问云端；`add` 会登记项目并写 `<ROOT>/.bugx/cloud-project.json`。绑定文件存在不等于知识同步或项目画像同步成功。
- `cloud onboard on` 立即同步 global + project；`off` 只修改本地 onboarding 状态。若只授权项目范围，优先使用显式 `cloud sync --scope project`。

### 8.5 auth

    bugx auth <auth_action> [--root ROOT] [--project-slug SLUG]
               [--accept-data-sharing] [--privacy {standard,strict}]
               [--api-key API_KEY] [--json] [--verify]

auth_action 必须是 login、status、set-key 或 logout。--api-key 只用于 set-key；--verify 可能访问网络。不要在 shell history、终端录屏或文档中留下真实 key。

### 8.6 telemetry

    bugx telemetry inspect [--latest] [--json]
    bugx telemetry export --out OUT [--passphrase PASSPHRASE]
    bugx telemetry flush [--batch-size N]
    bugx telemetry upload [--batch-size N]

flush/upload 的 --batch-size 默认 20；export 写本地产物，flush/upload 可能网络传输。telemetry 使用独立飞轮 outbox，不是 cloud knowledge 的五类 V2 record；成功发送的队列项会被清除，失败项保留并增加尝试次数。`inspect` 只看本地出网审计，`export` 生成加密离线包。passphrase 仅作为参数或受控环境传递，不能记录到日志。

### 8.7 config set

    bugx config set <config_key> <value>

| key | 允许值 |
|---|---|
| telemetry.data_sharing | true / false |
| cloud.sync_detail_mode | full / summary |
| cloud.sensitive_policy | block / redact / allow |
| cloud.sync_scope_default | all / project / global |

这些配置写入用户级 cloud config；telemetry.data_sharing=false 还会清理本地 flywheel outbox，并阻断 cloud push/pull。

## 9. Daemon、自治、Goal 和 Supervisor

### 9.1 daemon

    bugx daemon start [--root ROOT] [--foreground]
                       [--max-wall-time SECONDS] [--interval SECONDS]
    bugx daemon stop [--root ROOT] [--force]
    bugx daemon status [--root ROOT] [--json]
    bugx daemon attach [--root ROOT] [-n N|--lines N]

start 的 max wall time 默认 14400.0 秒，interval 默认 5.0 秒；attach 的 lines 默认 50。stop 会发送终止信号，--force 可能升级为强制终止；status 进程未运行时返回非零。

### 9.2 autonomous

    bugx autonomous [--root ROOT] --host {claude,codex,opencode,dsh,cline,zcode}
                      [--workers N] [--model MODEL] [--provider PROVIDER]
                      [--max-turns N] [--max-wall-time SECONDS]
                      [--agent-timeout SECONDS]
                      [--coverage-manifest PATH]
                      [--no-adaptive-wall-time]
                      [--adaptive-baseline VALUE] [--json]

- --workers 默认 0，表示 Host-derived/adaptive；显式值受 Host 并发边界限制。
- --model 默认空；--provider 默认空；--max-turns 默认 0。
- --max-wall-time 默认 0.0；--agent-timeout 默认 600.0。
- adaptive wall time 默认开启；--no-adaptive-wall-time 关闭。
- --coverage-manifest 会通过运行时环境传递 coverage manifest。
- 只有结果状态为 complete 时才按自治契约返回成功。

### 9.3 goal

    bugx goal [TARGET_DESCRIPTION] [--root ROOT]
              [--host {claude,codex,opencode,dsh,cline,zcode}]
              [--target-coverage PERCENT]
              [--allow-open-findings] [--disallow-quarantine]
              [--max-turns N] [--max-wall-time SECONDS]
              [--agent-timeout SECONDS]
              [--max-unit-retries N] [--max-transient-retries N] [--json]

- TARGET_DESCRIPTION 默认空字符串；Host 默认 claude。
- target coverage 默认 100.0；默认不允许 open findings；quarantine 默认允许，--disallow-quarantine 收紧。
- max turns 默认 0，max wall time 默认 14400.0 秒，agent timeout 默认 600.0 秒。
- unit retries 默认 3，transient retries 默认 5。

### 9.4 supervisor

    bugx supervisor <supervisor_action> [--root ROOT] [supervisor_args ...]

supervisor_action 为：

install、status、rpc、reset-budget、recover、rebind-target、gate、repair-fence、findings、next-round、finish。

`install` 和 `status` 不需要额外参数。`rpc` 的实际参数为：

    bugx supervisor rpc --root ROOT [--role {host,worker}]
                         [--tool TOOL --args JSON]
                         [--method METHOD --params JSON]
                         [--timeout-seconds SECONDS]

`--role` 默认 `worker`；`--tool/--args` 与 `--method/--params` 二选一；`--timeout-seconds` 默认 15。rpc 只建立一次本地 MCP 回退调用并打印一条 JSON 响应，不等于真实 Host/Worker 生命周期验收。

其余 action 的公共参数由 Supervisor CLI 接收：`--root`、`--state`、`--coverage-manifest`、`--max-rounds`（默认 10）、`--max-wall-seconds`（默认 3600）、`--max-round-wall-seconds`（默认 900）、`--max-tool-calls`（默认 1000）、`--max-cost-units`（默认 10000）、`--max-concurrency`、`--allow-serial`、`--serial-reason`、`--require-confirmation`。BugX 外层 wrapper 会把 `--state` 绑定到当前项目真实 Supervisor 状态；不要拿默认的 `.bugx/audit-supervisor.json` 冒充当前生命周期状态。

action 专属参数：

- `reset-budget`: `--renew-wall-clock`、`--expected-revision N`、`--force`。
- `recover`: `--reset-worker-attempts`。
- `rebind-target`: `--reason REASON`（必填）。
- `findings`: 必须使用 `--fix-severity`；可配 `--finding KEY`、`--severity LEVEL`、`--reason REASON`、`--expected-revision N`。不带 `--fix-severity` 会失败，不能把它当只读 Finding 汇总。
- `gate`、`repair-fence`、`next-round`、`finish`: 无额外 action 参数，但会读取或改变 Supervisor 状态；其中 `finish` 只有在 Host gate 通过后才可执行。

reset-budget、recover、rebind-target、repair-fence、findings、next-round、finish 具有状态改变或结算风险。先用 status/gate 获取 JSON，再执行恢复；不要把 rpc 响应、Task 打印或 gate 单项结果写成完整审计通过。

## 10. 插件、更新、日志和统计

### 10.1 plugin

    bugx plugin <plugin_action> [PLUGIN_NAME] [--root ROOT]
                     [--source SOURCE] [--force] [--remove]
                     [--json] [--yes]

plugin_action 为 list、status、install、uninstall、zcode。--remove、--force、--yes 可能覆盖或移除受管理资产；插件状态不等同于 ACP ready。

### 10.2 update

    bugx update [--root ROOT] [--check] [--force]
                [--repair-assets] [--allow-downgrade]
                [--channel|-c {stable,alpha,''}]

--channel 默认空字符串，表示 stable；--check 是检查路径，实际 update/repair 可能写全局安装。--allow-downgrade 需明确版本风险。

### 10.3 patch-forge

    bugx patch-forge [--from-issue|--verify|--status] [--root ROOT]
                      [--kind {engine}] [--base-version VERSION]
                      [--diff DIFF ...] [--repro REPRO ...] [--test TEST ...]
                      [--finding-fingerprint FP] [--patch-id ID]
                      [--signature SIG] [--severity {high,medium,low,info}]
                      [--output OUTPUT] [--dry-run] [--json] [--submit]
                      [--api-key API_KEY] [--base-url URL]
                      [--source-commit COMMIT] [--spec-only]
                      [--session-id ID|--session ID] [--run-id ID]
                      [--lifecycle-id ID] [--repository REPOSITORY]
                      [--test-red-green]

--from-issue、--verify、--status 三者互斥；--kind 当前为 engine，--base-version 默认 0.1.0；--diff、--repro、--test 可重复。--submit、--api-key、--base-url 可能产生网络副作用，--dry-run 只做计划。

### 10.4 log

    bugx log [--root ROOT] [--tail [N]] [--watch|-f|--follow]
               [--errors] [--trace] [--timing|--waterfall]
               [--timeline|--replay] [--anomalies|--audit]
               [--tokens] [--bundle] [--mcp-polling] [--json]
               [--output|-o OUTPUT]

--tail 无值时默认 20；watch/follow 可能持续运行。--bundle 和 output 可能写诊断产物；日志可能包含路径、参数摘要和时间信息，分享前脱敏。

### 10.5 stats

    bugx stats [--global] [--days N] [--json]

--days 默认 30；--global 选择全局统计。

## 11. Project、View 和测试相关命令

### 11.1 project

    bugx project analyze [--root ROOT] [--json]
    bugx project show [--root ROOT] [--json]
    bugx project sync [--root ROOT] [--json]
    bugx project list [--root ROOT] [--json]
    bugx project refresh [--root ROOT] [--json]
    bugx project rebind [--root ROOT] [--migrate] [--json]
    bugx project settings [TARGET] [--root ROOT]
                          [--set-cloud-sync {on,off}]
                          [--set-review {auto,interactive,off}]
                          [--set-polling {auto,manual,off}]
                          [--set-auto-fix {on,off,confirm}]
                          [--set-commit {round,task,off}] [--json]
    bugx project [--root ROOT] [--json]

裸 project 默认执行 analyze；`project sync` 不是 `cloud sync` 的别名，它只上传紧凑项目画像/代码指标，并写项目云绑定；成功 JSON 应包含 `status=ok`、`project_id`、`synced=true`。`rebind --migrate` 迁移项目身份/状态；settings 的每个 --set-* 选项写项目设置。--root 默认按命令解析到当前项目。

### 11.2 view

    bugx view PATH [--lines|-L RANGE] [--no-numbers|-N]
                [--json] [--root ROOT] [--max-lines N]

--lines 默认 1:200，--max-lines 默认 500。view 只显示受限文件片段；不要把它当成任意路径读取器。

### 11.3 test-generate、test-audit

    bugx test-generate --scope SCOPE [--root ROOT]
                       [--techniques TECHNIQUES] [--out OUT]
                       [--standalone] [--assert-expr EXPR]
                       [--expected-exc EXC] [--json]
    bugx test-audit FILE [--scope SCOPE] [--min-score SCORE]
                       [--strict] [--json]

test-generate 的 --techniques 默认 boundary,negative-control；--scope 必填。test-audit --scope 默认空，--min-score 默认 70.0。生成文件属于本地写入，审核分数不是实际执行结果。

### 11.4 mining-audit

    bugx mining-audit audit PLAN_FILE [--root ROOT]
                           [--min-score SCORE] [--strict]
                           [--allow-degraded] [--json]
    bugx mining-audit explore [--root ROOT] [--language LANGUAGE] [--json]
    bugx mining-audit harden PLAN_FILE [--root ROOT] --out OUT

audit 的 min-score 默认 80.0；harden 的 plan_file 和 out 必填。--allow-degraded 在这里是该 parser 路径的选项，不能转写成 engine pre/audit 的允许降级开关。

### 11.5 test-verify

    bugx test-verify verify --target TARGET --test TEST [--root ROOT]
                             [--max-mutants N] [--timeout SECONDS]
                             [--strict] [--json]
    bugx test-verify mutate --target TARGET [--root ROOT]
                             [--max-mutants N] [--json]

verify 的 max-mutants 默认 30、timeout 默认 2.0 秒；mutate 也默认 max-mutants 30。这些命令的结果是测试/变异观察，不自动变成 Finding confirmation。

## 12. Audit、Interaction、Advisory 和 Report

### 12.1 audit

    bugx audit [--root ROOT] [--goal {find_bugs,repair_preview}]
               [--host HOST] [--scope SCOPE ...] [--deep]
               [--concurrency N] [--format {all,html,markdown,md,both,pdf}]
               [--json] [--open-browser] [--out OUT]
               [--min-kill-rate RATE]

- root 默认当前目录；goal 默认 find_bugs。
- --scope 可重复；--deep 开启一次性深度流程。
- concurrency 默认 6；format 默认 all；min-kill-rate 默认 0.85。
- --open-browser 可能启动本地浏览器；--out 写报告。
- graph 模式返回 ready/awaiting_native_agent 仍可能只是等待原生 Agent。

### 12.2 interaction

    bugx interaction list [--root ROOT] [--json] [--all]
    bugx interaction show QUESTION_ID [--root ROOT] [--json]
    bugx interaction answer QUESTION_ID --value VALUE [--root ROOT] [--json]
    bugx interaction cancel QUESTION_ID [--root ROOT]
                              [--reason REASON] [--json]

list 的 --all 包含非 pending 项；answer 的 --value 必填；cancel 的 reason 默认空。交互动作会改变 startup gate 或 review 状态，不要替用户猜答。

### 12.3 advisory

    bugx advisory list [--json]
    bugx advisory export [--cve CVE] [--package PACKAGE]
                         [--ecosystem ECOSYSTEM] [--category CATEGORY]
                         [--symbol SYMBOL] [--target TARGET]
                         [--format {markdown,json}] [--out OUT]

export 的 parser 默认值为：--cve CVE-2026-9001、--package target-package、--ecosystem npm、--category generic_logic、--symbol processInput、--target src/core.py、format markdown。这些是模板默认值，不代表真实漏洞、包或文件存在；输出前应替换为实际项目数据。

### 12.4 report

共同选项：

    --source {project,eval,auto}     default project
    --root ROOT
    --eval-dir DIR
    --out OUT
    --format {html,md,pdf,both}      default both
    --title TITLE
    --json

调用：

    bugx report [formal|visual|both] [common options]
    bugx report serve [common options] [--host HOST] [--port PORT] [--once]

report 裸命令接受 common report args；subcommand 为 formal、visual、both 或 serve。serve 的 host 默认 127.0.0.1、port 默认 8080；--once 只处理一次请求。source=project 读取项目 Supervisor state；source=eval 读取 eval receipt；source=auto 的回退受项目阻塞和 eval 存在条件限制。

## 13. 已移除或不再接受的入口

以下旧的顶层生命周期参数不再直接执行，CLI 会返回错误并提示使用 bugx engine --root <project> ...：

pre、status、workers、heartbeat、post、polling、pause、resume、abort、bootstrap-coverage。

以下旧顶层命令明确移除：matrix、console、watch、test。当前替代关系是：

| 旧入口 | 当前入口 |
|---|---|
| bugx pre/status/workers/post | bugx engine --root . pre/status/workers/post |
| bugx watch | bugx log --root . --watch |
| bugx console | 使用 Host/ACP/MCP 入口；不存在同名通用 console |
| bugx test | 使用 test-generate、test-audit、test-verify 或仓库的开发测试选择器 |

本参考只描述当前 parser 和转发器能到达的命令，不恢复已删除的别名或兼容层。
