# BugX 运行时契约（0.1.0）

这是部署到用户级全局引擎 `~/.bugx/core/` 的最小运行时契约。项目不再保存
`.bugx/core` 可执行入口；需要定位本文件时使用 `bugx engine path --name bugx-runtime-contract.md`。
详细设计文档只用于设计审计；Agent 执行时以本文件和当前版本 `bugx.md` 为准（`AGENT_GUIDE.md` 是面向宿主集成与
操作指引的辅助手册，不属于 Agent 执行权威链；冲突时以本文件和 `bugx.md` 为准）。

## 层级与所有权

唯一层级是 `Project → Session → Round → Life`：Session 是一次宿主启动到退出，
Round 是唯一正式报告边界，Life 是 Round 内的一次执行与结算。

- 全局引擎：`~/.bugx/core/`，只读脚本和工具知识；不同项目共享但不得写入项目结果。
- 项目公共层：`.bugx/knowledge/`，项目错题集、经验账本及其人读快照；跨 Session 共享。
- 当前运行层：`.bugx/pollings/<polling-id>/lifecycles/<lifecycle-id>/`，Session/Round/Life
  中间状态、事件、证据和报告；不得跨项目或跨活动 Session 复用。
- 当前准备与覆盖：当前生命周期
  `.bugx/pollings/<polling-id>/lifecycles/<lifecycle-id>/state/prep-record.md` 和
  `.bugx/pollings/<polling-id>/lifecycles/<lifecycle-id>/state/module-coverage.md`；
  `audit-todo.md` 是同一目录下供宿主和用户查看的操作进度视图。

全局引擎中的 `bug-log.md`、`mistake-book.md`、`lesson-ledger.json`、`prep-record.md` 和
`module-coverage.md` 不是有效的项目数据路径；
同目录下的 `bugx-runtime.json`、`bugx-life.json`、`finding-ledger.json`、`coverage-evidence.json`、
`audit-supervisor.json`、`findings_round*` 等旧运行态文件/链接同样无效；项目重置会将其归档并移出
活动目录，初始化/升级会摘除它们，不能重新接入当前运行态。
有效路径为 `.bugx/knowledge/bug-log.md` / `mistake-book.md` / `lesson-ledger.json` 与
`.bugx/pollings/<polling-id>/lifecycles/<lifecycle-id>/state/prep-record.md` /
`module-coverage.md`（前者跨 Session 共享，后者随生命周期隔离）。

提交策略由启动前 gate 或项目 `.bugx/profile.json` 决定：`life` 在每个 Life 的 `post` 收尾
提交，`round` 仅在 Round 完成/安全停止时提交，`none` 归一为内部 `manual` 并登记
`commit_status=manual_pending`。提交只允许带入项目相对 Git `HEAD` 的用户变更，排除
`.bugx/` 和运行态文件；提交失败保留 pending 状态并由恢复 worker 重试，不阻断本地自动续行；
只有不可恢复的控制面/状态损坏才阻断自动流程。
runtime 的 `commit_status`、`commit_boundary`、`commit_ref`、`commit_error` 是最近一次
边界结果的可观测摘要，不替代 Git 历史。

## MCP 命名与角色

| 层级 | 固定值 |
|---|---|
| server id | `bugx-supervisor` |
| tool name | `bugx_supervisor_*` |
| 宿主控制角色 | `caller_role=host` |
| Worker 角色 | `caller_role=worker` |

`bugx-docs` 是独立的本地只读 stdio MCP，不属于 Supervisor 角色通道。它只索引当前项目
`README.md`、`docs/**/*.md` 和 `core/**/*.md` 到 `.bugx/knowledge/docs.sqlite3` 的 SQLite FTS5
数据库；工具调用必须带匹配的 `project_id`，设置 `BUGX_DOCS_TOKEN` 时还必须通过 token 校验。
返回结果必须带文档版本、来源、SHA-256、捕获时间和私有范围标记；scope/token/索引失败时拒绝返回
内容，不能伪造 Context7 citation。

`status`、`gate` 是只读查询；`start`、`bootstrap_coverage`、`next_round`、
`bind_context`、`sync_finding_decisions`、`finish`、`fail`、`recover` 只允许宿主角色；`claim`、`worker_start`、
`tool_call`、`heartbeat`、`checkpoint` 只允许 Worker 角色。每个 Worker 每次只能领取
一个 coverage unit；`claim` 仅表示租约已领取，真实启动后必须调用 `worker_start`，
只有 `execution_state=running` 才计入并发重叠；`checkpoint.life_delta` 必须为 `0`。
MCP 不执行 Worker 提交的命令。claim 返回的 `artifact_root` 是该 Worker 唯一允许写入
证据的目录，越界 `artifact_path` 会被拒绝；一个 checkpoint 内每个 coverage unit 必须使用
独立 artifact，复用同一文件会被拒绝。目标漂移门禁刷新 Git HEAD 缓存后再比较，避免刚提交的
代码仍按短时旧指纹继续记账。
本地 `execute_autonomous_worker` 的启动或未处理异常由 runtime 调用 Supervisor 的
`record_worker_failure`，通过 worker/lease/execution 三元身份仅释放仍活动的匹配 claim，并持久写入失败原因。
此内部路径不增加 ACP MCP 工具、不创建 checkpoint 或更改 coverage；真实模型调用错误仍写为 `unverified/error`
checkpoint。原生 ACP Worker 的宿主断联/强杀仍由 lease expiry/recovery 回收。
`sync_finding_decisions` 校验活动 lifecycle 的 `findings.json` 绑定、确认观察工件、候选身份和
confirmed Finding 的 remediation。若报告遗漏 Supervisor 中的非终态候选，host sync 会把权威候选
快照原样补入报告并保持 pending；这不会确认、拒绝或改变该候选。没有 severity 的 pending 候选可
保留为待分类状态，但 confirmed 必须有有效 severity。报告添加 Supervisor 不存在的 key、身份漂移、
缺绑定、缺观察工件、遗漏已终态候选或 remediation 无效时仍会拒绝。`finish` 仍以 Supervisor
权威状态重新计算确认与关闭门禁。

`rebind_target` 从活动 lifecycle 的受信覆盖清单重建调度集合：新 ID 变为 `pending`，
已移除 ID 留在历史中并标记 `removed`，仍在清单中的旧 terminal ID 标记 `stale`。
非终态债务保持原状；旧 evidence 与审计历史不会被删除。

既有终态的旧证据能否沿用，由**本轮重扫结果**决定：`inventory-diff.json` 的
`changed`/`removed`/`renamed` 列出的 scope 对应单元一律作废（保持 `stale`，重新
验证），其余单元的证据仍然有效，恢复为 `covered` 并把这些证据记录的
`target_fingerprint` 改写到新目标（不改写就会留下 state 与证据的指纹分叉，下一次
post 的 checkpoint 结算必然判 stale）。没有 `inventory-diff.json`（缺失/损坏）时
退回严格模式：证据必须匹配新目标，全部重新覆盖——「看不见」不能当成「没变化」。

post 在 `supervisor_parallel` 之前有这个自动重绑的唯一入口
（`core/launch_phases/post_phase.py::_reconcile_additive_plan_drift`）：覆盖清单被
自身重扫扩写、或本轮提交了修复代码，都不会再把自己判成外部漂移。
Worker checkpoint 的最小结构以全局引擎 `schemas/worker-checkpoint.schema.json` 为准；
其中 `findings` 必须带 `fingerprint`，`covered` 单元必须带完整 evidence，`notes` 按
`unit_id → 文本` 保存，错误签名最多 10 条。
triage 升档控制标记 `upgrade_tier=deep` 必须在 note 中单独一行；普通文字提及不触发 stale 或深度重派。

`COMPLETE` 和 `FAILED` 都是终态，状态写入时必须同时保存 `completed_at`；宿主或新的
MCP 进程随后读取 `status` 必须能够稳定恢复该终态。`fail` 释放活动租约但不删除 findings、
coverage、事件或错误原因。

云同步的 `tool_experience` 主键是 `tool_name/category`；pull 收到 tombstone 时必须在
提交 cursor 前写入同一复合主键的 deleted 记录。Pattern tombstone 必须在同一账本事务内
撤销仍可运行的子 Incident。cursor 虽然是 opaque 字符串不可排序，但 `snapshot_version`
低于已提交版本且不是同一 snapshot/cursor 的幂等重放时必须拒绝，不能回退已提交状态。
隔离文件 `.bugx/knowledge/cloud-synced.json` 仅作为 Pull 结果的诊断留痕，不是第二个导入入口；
实际导入只能由 `cloud_sync.sync_pull` 在 inbox 校验和账本事务中完成。文件仍必须携带覆盖五类
`records` 的 canonical `manifest_hash`，缺失或不匹配时不得落地为可消费知识。

`pre/round` 产生的 `lesson_context` 通过 `worker_orchestrator` 进入 Worker 共享指令，但只包含有界
的项目分类、路由标签、短查询和检索能力，不包含 pattern、incident 或 tool experience 正文。Worker
先调用 `bugx_knowledge_route` 做确定性风险判断；高风险或命中错误签名时再按“分类 → 受限检索 →
快速碰撞”调用只读 MCP，低风险任务不产生检索；返回值明确标记为不可信数据；实际复用
经验时在最终 Finding 回填 `pattern_id`，实际调用工具回填 `tools`。MCP 检索消费事件按请求参数哈希
幂等，账本 metrics 分开展示 retrieved 与 review/tool-feedback 结果。

## 诊断追踪边界

`logs/bugx.jsonl` 只保存结构化诊断事件。只有 Alpha 构建的 `metadata.txt` 明确写入
`bugx_channel: "alpha"` 时，模型 runner 才可在本地独立的
`.bugx/sessions/<session-id>/artifacts/transcripts/<trace-id>/` 保存模型可见 Prompt、stdout、
stderr 和 manifest；没有 Session 时使用 `.bugx/debug/transcripts/`。Stable、缺失或非法通道
必须 fail-closed，不保存原文。Transcript 受输出上限、私有权限和原子写入约束；不记录隐藏思维链。
Alpha transcript 可以作为不可信证据被 Supervisor 的知识复盘消费：只提炼为项目级 candidate
Pattern/Incident/Tool Experience，并以 `transcript://` 逻辑引用绑定来源，不得直接激活规则。
与 Finding/Incident 相关且对复现、定位或修复有帮助的非敏感源码片段，也可以进入项目级错题集/经验集
并在私有 project full sync 中同步；全局层只允许验证后、去项目化且可跨项目复用的最小源码示例随结构化
经验进入。无关源码、凭据、恶意载荷和越权 scope 仍必须阻断。
启用私有 project Cloud Sync 后，经过递归安全扫描的 transcript 工件可进入异步 outbox；结构化
复盘结果按现有 project/global 边界同步，global 只接收验证并去项目化后的结构化经验。Transcript
不得进入 public scope。更新检查失败只
记录 `update_check_failed` 的阶段、状态、错误码、request id、错误摘要和耗时，不记录完整响应。
BugX 的优先级是挖掘收益（效率、准确、速度、覆盖和复用）第一，安全与可信第二，隐私第三；
安全边界仍是不可绕过的前提。

## 控制者闭环

主 Agent/宿主是唯一的生命周期控制者：

- MCP 自动审查时，主 Agent 使用 `caller_role=host` 调用 Supervisor 的控制工具；
- 手动审查或 MCP 不可用时，主 Agent 使用受保护的
  `bugx engine --root . pre|post|heartbeat|pause|resume|abort` 通道；
- Worker 只能使用 Worker 角色工具，不能调用 launcher 的控制命令，也不能直接改写
  `.bugx` 状态、报告、覆盖总表或项目知识数据库；
- 用户只负责启动 Gate 和异常回退选择，不需要手动执行 `pre`/`post`。没有原生选择控件、
  真实 TTY 或可用控制通道时，当前阶段返回 `blocked`，不得伪造“手动回退”并继续。

宿主交互适配固定为：生命周期 gate 使用 OpenCode `question(multiple=false)`、Claude Code
`AskUserQuestion(multiSelect=false)`；工具准备批量 gate 必须按请求使用对应的多选参数。Codex 交互会话且当前工具列表明确提供
`request_user_input` 时使用原生选择控件，按 question id + 精确 label 映射回稳定 `value`（通常要求 Plan 模式或已启用实验特性），Pi、dsh、ChatGPT
使用各自按请求 `multiple` 选择单选/多选的控件；无控件且无 TTY 时阻断。
每次只提交一个 gate，答案写入并验证成功后才进入下一 gate。
非 TTY 的 `BUGX_STARTUP_GATE` 选项必须有唯一稳定的 `value`；需要执行动作时同时提供
受控的 `action` 或 `command`。宿主按这些机器字段执行并校验结果，禁止从中文 `label` 推断命令。

### `next_action` 与 `status` 取值契约（宿主必须消费）

Supervisor 每次调用都返回 `next_action`（机器字段，禁止按中文文案推断）。宿主必须按下表动作，
不得把「无法派发」误当作「继续补位」：

| `next_action` | 触发条件 | 宿主动作 |
|---|---|---|
| `START_RUN` | 尚无运行态 | 调 `start` 建 run |
| `RETRY_START` | 覆盖清单刚生成/复用 | 重试 `start` |
| `RESOLVE_STARTUP_GATE` | 启动门禁未过 | 先解决启动 gate |
| `REBUILD_COVERAGE` | 覆盖清单未通过完整性校验 | 重建覆盖清单 |
| `RECOVER_OR_NEW_SESSION` | 检测到已中断会话 | 先 `recover` 或新建会话 |
| `REBUILD_PLAN` | 目标指纹漂移（`parallel_gate.target_drift`） | 调 `rebind_target` 后重读 status |
| `RESET_BUDGET` | `status=BUDGET_EXHAUSTED` | 调 `reset_budget(force=true)` 后重读 status |
| `REPORT_OR_RECOVER` | `status ∈ {COMPLETE, FAILED, INCOMPLETE}` | `finish` 收口或走 `recover` |
| `RECLAIM_EXPIRED_WORKERS` | 存在过期 worker | 回收/接手过期 claim |
| `CLAIM_WORKERS` | 有可派发单元且并发未满 | 运行 `bugx engine --root . workers` 领取新 claim |
| `WAIT_OR_HEARTBEAT_WORKERS` | 有 active worker 但并发已满 | 等待心跳/checkpoint，**不得**再领 claim |
| `FINISH_OR_NEXT_ROUND` | 门禁满足且无待覆盖 | `finish` 或 `next_round` |
| **`STALLED_OR_RECOVER`** | 覆盖债务存在但仍有非 `worker_attempts_exhausted` 的调度阻断 | **禁止运行 `workers`**（必然 exit 1 空转）。先调 `recover(caller_role="host")` 或处理对应 gate，再重读 status；若仍无可派发，输出 `BLOCKED` 并 `finish` |
| `ESCALATE_RESET_ATTEMPTS` | 无 active worker，且所有未完成单元均因 `worker_attempts_exhausted` 熔断 | 调 `recover(caller_role="host", reset_worker_attempts=true)` 隔离旧代际尝试历史，再重读 status；若仍无可派发，输出 `BLOCKED` 并 `finish` |
| `POLL_LATER` | 轮询窗口未到 | 稍后重读 |
| `PROMPT_USER` | MCP 调用失败需用户裁决 | 询问用户（retry/fallback/later） |
| `RECOVER_OR_FINISH` | `next_round` 门禁未满足（只读预检） | `recover_expired` 接手 / `finish(coverage_complete=false)` |

`status` 与 `next_action` 必须一致，不得自相矛盾：`STALLED_OR_RECOVER` 对应
`status=STALLED` 或普通恢复阻断，`ESCALATE_RESET_ATTEMPTS` 对应覆盖债务仍存在且所有
未完成单元均已触及 `worker_attempts_exhausted`。`STALLED` 表示能力枯竭而非正常推进；
普通 `recover` 推进 `scheduler_epoch`，显式 `reset_worker_attempts=true` 另外把
`expired`/`released`/`submitted` 历史 claim fence 到旧代际，解除全 Run 尝试硬顶。
引擎退出文案中的 run 状态为**动态读取的真实值**，宿主应以结构化字段为准。

### 宿主 Agent 自治模式

推荐显式入口为 `bugx autonomous --host claude|codex`。该入口在当前宿主进程启动前补齐受信任的
项目级 MCP，并由宿主 Agent 执行原生 Task/subagent；控制器不创建独立 worker daemon，不接受交互
提问，也不修改 Codex 全局配置。Claude 的 `.mcp.json` 变更保留到 `.bugx/backups/host-mcp/`，
Codex 的注册仅通过进程级 `-c` 覆盖和 `.bugx/state/host-mcp/codex.json` 收据生效。

当显式使用 `--autonomous`，或项目 `.bugx/profile.json` 的 `active_profile` 为
`autonomous` 时，启动器读取并校验自治策略快照；策略 hash 写入 runtime。策略类 Gate
（同步、审查方式、轮询、修复、提交、分享、覆盖引导、启动动作和中断恢复）不得再发起用户问题，
但 Git、版本、完整性、Supervisor/MCP、覆盖、证据、预算和权限硬校验仍必须执行。
原生 Task 必须使用宿主预占 claim 的完整身份；Worker MCP 仅允许对应 `caller_role=worker`，重复 claim、
身份不一致或 evidence 缺字段时拒绝 checkpoint，并要求宿主走恢复路径。
`worker-start` 的 `execution_id` 无法逐字复制时可省略，由服务端从预占 claim 绑定；显式错误值仍拒绝。
原生 Task 只有在确认长生命周期本地 Worker 子进程时才提供 `process_pid`；模型会话 PID、shell PID 和
短命命令 PID 禁止登记。无 PID Worker 以 Worker MCP heartbeat/tool-call 作为活性证据，避免宿主把
正常的原生 Task 误判为死进程。
Worker stdio 通道可补齐缺失的 `caller_role`，但显式错误角色仍拒绝；验证工具的 timeout_sec 超出
0.5~10 秒时必须收敛到该安全边界，不能扩大执行权限。

自治模式的宿主职责是：调用 `workers` 生成提示词后，立即用 Claude Code/Codex 原生
Task/subagent 并发启动全部 Worker；回收并核对 `worker-start`、heartbeat 和 checkpoint 后，
由宿主串行确认 Finding、修复、运行受影响测试和 Live 复验，再调用 `post`。自治宿主不得使用
`workers --execute` 或 runtime context 的 `execute=true` 驱动独立 Worker runtime；独立 daemon/进程
Worker 不是本模式的执行主体。

只有同时满足“覆盖清单全覆盖”和“`finding_confirmation.open_findings` 为空”才允许
`COMPLETE`、Round 正常收尾或报告为零未关闭 Bug。confirmed Finding 没有有效 fixed
remediation、测试/Live 复验失败、MCP 需要重启、认证失效或同步未完成时，必须停在
`BLOCKED`、`RECOVERY_REQUIRED` 或 `sync_pending`，不能以 coverage 完成代替缺陷收敛。

自治模式的首次登录仍是外部一次性前置；登录态失效不得在无人值守过程中猜测或弹出替代问题。
启动 gate 的 `question_id` 全集（loop-probe 探测器清单为准）：
`startup_interaction`、`git_environment`、`version_contract`、`project_layout`、`cloud_sync`、
`supervisor_mcp`、`review_mode`、`polling_policy`、`fix_mode`、`commit_policy`、
`report_policy`、`startup_action`、`aborted_session`、`coverage_bootstrap`、`strategy_plan`。
Round 阶段没有独立交互 id：报告处置由 L1 一次采集的 `report_policy`
（`none`/`local`/`submit`）决定，L5 `post` 按该策略无交互执行（DOC-236 C6）；
L1 的采集入口即 `report_policy` gate（答案经 `pre --report-policy <value>` 重试，
来源记录在 `runtime.report_policy_source`，已决策来源不再重复询问）。
适配层不得对清单之外的 id 做特殊分支。
退出码语义：`2` 且本次**没有**输出新的 `BUGX_INTERACTION_REQUEST` =
启动已被用户选择停止（stop/later/restart），宿主**不得重试**；
`2` 且有新 REQUEST = 停留当前 gate，按答案重试。

宿主权限边界：`core/bugx.md` frontmatter 的 `permission` deny 矩阵与 `temperature` 只有
OpenCode 入口（原样安装 Agent 模板）会原生消费；其余宿主必须在部署时用宿主等价机制
deny 同一批引擎校验器文件（`verify_life.py`、`launch_bugx.py`、`module_coverage.py`、
`symbol_index.py`、`tools_kb.py`、`prep_validate.py`、`agent_eval.py`、
`finding_confirmation.py` 等核心脚本，清单以 frontmatter deny 矩阵为准），例如 Claude Code
的 settings 权限、ZCode 的权限模式。仅靠 Agent 提示词约定不构成防篡改边界；
被审计 Agent 不得改写审计工具是硬前提。L1 完整协议 `bugx.md` 禁止一次性整读，
必须按章节提取（红线，见该文件头部使用入口）。

## 启动与失败处理

宿主先根据用户目标或工作区确定唯一项目根的绝对路径，执行该路径的
`bugx engine --root <项目根> status --json`。返回 `UNINITIALIZED` 时直接执行同一目标的
`bugx engine --root <项目根> pre`：引擎会自动创建新项目状态，再进入既有启动门禁。
已有状态不完整时必须保留数据，不能仅补写布局标记、重建状态或将其误报为程序版本冲突。
`layout_version` 标识项目状态的存储布局，与 BugX 程序版本是不同字段。

环境快照必须先于任何启动交互，并且 Git 是创建 Session 前的硬前置：必须确认 `git` 可执行、
目标根目录就是仓库根、HEAD 和工作树状态可读、提交身份完整且 `.git` 元数据可写。缺失、
未初始化或不一致时只返回 `git_environment` gate，交由宿主安装/初始化/修复后重新检测；核心
不自行写 Git。环境和 Git 门禁通过后才进入交互；交互一次只处理一个当前 gate。答案无效、
登录/授权未完成、身份/协议失败、MCP 调用失败或工具缺失时停在当前 gate，向宿主返回 `PROMPT_USER`，不得静默采用默认值或推进；已完成授权后的云端网络/DNS/超时/5xx 同步失败记录 `sync_pending`，继续本地自动流程并交给恢复队列重试。
选择云端同步后若凭据缺失或服务端返回 401，宿主必须调用 device-code 登录，打开返回的 verification URI 并等待授权结果；
只有登录成功且设备登记成功后才能提交该 gate。知识 push/pull 仍可在 Session 提交后的工具准备阶段执行。
OpenCode TUI 使用 `question(multiple=false)`，Cline TUI 使用 `ask_question(multiple=false)`；
`opencode run`、Cline `--acp` 或其他 headless 子进程没有原生控件时，只转发唯一
`BUGX_STARTUP_GATE` 给可承接的 Orca/ACP 外层控制器；没有外层承接者就保持 blocked，不得由模型输出文本问卷或猜测选择。
若宿主进程树不可识别，启动命令必须显式提供 `--host <name>` 并将结果写入当前 runtime；项目
`opencode.json` 仅是配置提示，不能单独授予原生交互能力；
`.opencode.json` 不是 BugX 的配置路径。
MCP 配置变化通常需要重启宿主会话。当前子进程没有 `BUGX_MCP_SESSION_PROOF`，或宿主配置缺失/发生变化时，
不得用有界主动探活把会话视为已就绪，也不得把 MCP 自动模式静默降级为手动模式。启动器必须明确提示
“`bugx install --root <项目根> --agent <宿主> --non-interactive` 写入宿主配置 →
重启宿主 → 新会话重新启动 BugX”；配置已存在但当前工具列表没有 `bugx_supervisor_*` 时标记
`MCP_SESSION_STALE`。五组启动配置完成后，宿主只能使用 `multiple=false` 的原生 `question`（或结构化回退）询问
「重启宿主后开始」「暂不启动」；在用户明确选择并完成重启前，不得进入工具准备、创建运行态或开始挖掘。

准备完成后，宿主按 `start → claim → worker_start → heartbeat/checkpoint → gate → finish/next_round` 推进。
所有 Worker 结束并通过并发门禁后才可结算；崩溃、损坏和中断走显式 `recover` 或下次启动
恢复，不得用新 run 覆盖旧状态。

进入项目启动流程时，宿主/控制面先生成或校准项目级 `.bugx/startup-todo.md`，让用户看到环境检测、启动配置、工具准备、
漏洞挖掘、同步复盘、Round 报告/共享和结束七个大步骤，即使启动停在门禁也必须保留这份总览。启动动作确认且当前 Round
运行身份建立后，再生成或校准当前 Round 的 `state/audit-todo.md` 细分清单。两者都只是派生视图，不替代运行态、事件、
覆盖证据、Finding 或 Round 报告；恢复时可以由这些权威事实重建，清单滞后不改变门禁结论。

## 黑盒功能清单与资源引用契约

黑盒/半黑盒调度清单使用当前 Session 独立的 `audit_campaign.json` `schema_version=2`，根节点必须带
`session_id`。CLI 的非创建操作必须显式提供匹配的 `--session-id`；省略路径时使用
`.bugx/sessions/<session-id>/audit-campaign.json`，创建不得覆盖既有 Session 清单。
`audit_campaign.py` 只负责当前 Session 最小功能单元的 DAG 前置依赖、执行状态、阻断原因和安全资源引用；
项目公共层的长期覆盖率、错题集和经验集不由 campaign 替代，旧 schema 直接拒绝，
不做猜测性迁移。`next`/`claim` 只能返回已满足前置条件的单元及不含秘密值的
`resource_context`：资源定位符限定为 `runtime:<id>` 或受控 `env:BUGX_*`，过期、撤销、缺失
或未声明的资源依赖必须阻断。

真实 Worker 的 lease、并发、资源冲突和 artifact 隔离仍以 Supervisor 为唯一事实源。campaign
不得启动进程或写 Worker 状态；宿主领取清单单元后，还必须通过 Supervisor 的 Worker 协议启动和
结算。`lesson_ledger` 只沉淀非敏感的测试方法、错误签名和复盘结果，不存放账号、密码、Token、
Cookie 或资产秘密。`asset_summary` 是调度层复用计数，不等价于漏洞检出率，收益需通过 A/B
测试确认。

## 代码库图增强契约

`codebase-memory-mcp` 是环境必检的语义增强 MCP，但不是全量覆盖硬依赖。READY 且索引代际
匹配时，启动器设置图增强标志，`codebase_memory_bridge.py` 将高可信跨文件调用关系交给
`project_graph.py`；每条关系必须映射到当前 `symbol_index.py` 已发现的文件和符号。低可信、
过期、部分解析、缺失或调用失败的关系不得进入业务链路，保留本地关系图并记录 DEGRADED。
源码、配置、依赖、构建、CI、插件和部署资产的全量清单始终由本地枚举负责；图谱只能增强
语义分组和跨模块链路，不能单独证明覆盖完成。

### 启动降级通道的四分类（机器语义，禁止文案判定）

启动前探针的降级项由 `probe_registry` 产出**机器字段**，门禁只认字段、不解析任何人类文案；
四类语义绝不可混用：

1. `NOT_APPLICABLE` / `DEFERRED`（项目形态不适用）：报告状态本身即结论，降级项被**剪枝为 0 项**。
   例：纯 Python 工程的 `native`（无原生源文件）与 `desktop`（无桌面框架）探针返回
   `NOT_APPLICABLE`，其 CLI 退出码为 `0`——"本项目没有该形态"不是"环境能力缺口"。
2. `advisory`（`required=False` 建议性探针）：缺失/超时按注册表策略**不阻断**，但决策与收据
   必须留痕（`status=not_applicable`、`receipt.source=probe_advisory`），不得静默消失。
3. `transient`（已安装但本次探测超时）：由注册中心超时路径或探针 `check.transient` 置位。
   `critical=False` 的能力走瞬态降级并强制 `BUGX_USE_CODEBASE_MEMORY=0`（本轮走内置回退），
   `critical=True` 的能力仍然阻断——超时不能替关键能力背书。
4. 真实缺口：先穷尽自愈装配；仍失败时非关键能力可经项目 `.bugx/config.json` 的
   `tooling_exemptions` 显式豁免（支持探针级前缀匹配，如 `codebase-memory-mcp` 命中
   `codebase-memory-mcp/cli`），`critical=True` 的能力**不得**豁免。豁免读取只要求该配置文件
   存在且可读，不要求完整 lifecycle 布局——阻断提示指向的通道必须真的可用。

启动恢复阶段对已经授权的 `share_pending` 执行幂等重放；该恢复动作位于当前 Session
覆盖清单门禁之前，失败则保留队列并继续进入当前启动门禁，不改写已封存 Round 报告。
`--review-mode later` 表示"本轮不启动"：判定必须早于任何运行态落盘（含 `report_policy`
持久化），否则"下次再说"仍会在磁盘上留下 runtime 状态。

## 下一代智能 Prompt Harness 与异构模型调度契约

为消除长会话上下文膨胀与单体提示词注意力漂移，BugX 建立物理级三层渐进式 Harness 装配、主动拦截注入与异构模型分级建议契约：

1. **L0 路由入口 (Routing Foundation, ≤1.5k Tokens)**：
   仅声明角色认知、防投毒防御与宪法红线（严禁公网推送），适配不同宿主的原生单选与交互通道。
2. **L1 阶段专精 Harness (Stage Harness, ≤2.5k Tokens)**：
   `gate`、`explore`、`mutate`、`repair` 随状态机阶段流转按需挂载，声明各阶段独占的行为准入与交付标准。
3. **L2 专项规则智能切片 (Semantic AST Slicing, ≤500 Tokens)**：
   通过 Python AST 静态语法树推断目标单元源码涉及的并发（`concurrency`）、资源泄漏（`memory_leak`）或输入解析注入（`prompt_injection`）特征，精准按需切片挂载，杜绝传统字符串弱匹配脱靶。
4. **状态机主动注入拦截器 (Active Task Injection)**：
   在任务分派（如 `workers`）与阶段流转时，统一使用 `decorate_stage_task` 将阶段职责、规则切片与格式约束前置包裹于 Worker 任务描述中，杜绝依赖 Agent 自觉拉取导致的规则逃逸。
5. **长输出证据蒸馏器 (Evidence Distiller)**：
   提供 `distill_evidence`，自动识别堆栈帧、断言差异与关键行号，将动辄数百行控制台/Fuzz 原始输出压缩至 ≤15 行结构化提要，保障主上下文 Token 密度与推理清晰度。
6. **异构模型分级调度建议契约 (Model Tiering Recommendation)**：
   通过 `get_stage_metadata` 与 MCP 工具 `bugx_get_stage_harness` 返回阶段算力与认知复杂度分级建议：
   - `tier_1_lightweight`：适用于 `gate`（环境与门禁预检）与 `explore`（大规模源码与路由初筛），偏好高吞吐、高性价比轻量模型；
   - `tier_2_deep_reasoning`：适用于 `mutate`（并发时序竞态、状态反常序突变、差分双跑验证），偏好强长程逻辑推理模型；
   - `tier_3_precise_code`：适用于 `repair`（TDD 最小修复与定向单测闭环），偏好高精度代码工程模型。
   分级契约为建议性契约（Advisory），宿主可根据本地/云端可用模型自主映射调度，单模型环境下完全平滑兼容。
7. **Token 预算硬强制 (Enforced Hard Cap)**：
   上述 L0/L1/L2 上限不再只是提示词标签，而是在 `prompt_harness.py` 出口用
   `token_budget.estimate_tokens()` 强制。`decorate_stage_task` 对 L1 阶段 Harness
   （2500T）、L2 规则切片（500T）与 L2 脆弱性雷达（500T）逐段硬截断；
   `decorate_skill_stage_task` 对技能包 L1 清单（默认 300T）、Attack Graph 切片
   （200T）与 L2 经验检索建议（默认 600T）逐段硬截断（可由技能包 `budgets` 覆盖）。
   任何截断都会在截断处留下 `BUDGET-DEGRADED` 标记，并在输出尾部追加
   `=== Budget Degradation Record (DEGRADED) ===` 段落，同时以
   `harness.budget_degraded` 事件写入日志（`status=DEGRADED`）。
   预算上限集中定义于 `prompt_harness.HARNESS_SECTION_BUDGETS`，不允许多处复制。
   截断只削减提示词装饰层，绝不改动任务主体（`task_text`）。
