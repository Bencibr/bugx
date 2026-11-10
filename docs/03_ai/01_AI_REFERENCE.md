---
title: BugX AI Host and Worker Reference
audience: AI hosts and native agents
status: DOCUMENTATION_UNVERIFIED
version: 0.1.0
protocol: MCP 2024-11-05
server: bugx-supervisor 0.1.0
last-verified: 2026-11-10
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

### 1.2 Host capability sets

    install_adapters =
      chatgpt, claude, cline, codex, dsh, funide,
      opencode, pi, qoder, trea, workbuddy, zcode

    ready_engine_plugins =
      opencode, codex, cline, dsh, claude, pi, zcode

    acp_ready = cline, opencode
    acp_todo = chatgpt, workbuddy, qoder, trea, funide
    acp_unavailable = claude, codex, dsh, pi, zcode

    autonomous_cli_choices =
      claude, codex, opencode, dsh, cline, zcode

这些集合不可互换。install adapter 不代表 engine plugin、ACP 或 autonomous CLI 可用。插件 ready 需要六面契约：identity、install、engine_run、worker_runtime、mcp_write、acp。ACP 状态含义：

- ready：实现了 ACP argv/启动契约；
- todo：仓库尚未适配，probe 必须 BLOCKED；
- unavailable：Host 产品没有 ACP entry，probe 不适用。

## 2. CLI contract

### 2.1 Public entry

项目生命周期必须使用：

    bugx engine --root <project> pre
    bugx engine --root <project> status [--json]
    bugx engine --root <project> workers ...
    bugx engine --root <project> post ...

顶层 bugx pre、bugx status、bugx workers、bugx post 等旧入口不执行，会返回迁移错误。bugx engine 不带 engine args 时默认为 status。engine CLI 为全局 engine 进程设置 project root、global engine root 和 CLI version 环境。

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
      --host {claude,codex,opencode,dsh,cline,zcode}
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
    serverVersion: 0.1.0
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

run_sandboxed_probe 的边界是 argv-only、禁止 shell launcher/operators、timeout 0.5–60 秒和 artifact/evidence 记录；它不是 OS 文件系统 sandbox。AI 不得向用户宣称更强隔离。

## 6. Finding verification and finish policy

Finding state machine：

    candidate -> verify -> confirmed
                       \-> rejected

submit_finding 只产生 candidate。verify_finding 由 Host 执行两条独立 positive cases 加固定 negative control，并记录 receipt/hash。confirm_finding 必须绑定 verify receipt；Worker 自报、模型判断或单个正例不够。

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

report consumption phase：

    OPEN -> POSTING -> SETTLED
                   \-> RECOVERY_REQUIRED

普通 status、claim 不会默默恢复损坏状态；recover 是 Host 显式动作。任何失败必须进入 COMPLETE、ABORTED、BLOCKED、RECOVERY_REQUIRED、INCOMPLETE、BUDGET_EXHAUSTED 或 FAILED 等可观察状态，不得无限等待。

## 8. Interaction contract

待回答问题由 Host/用户控制：

    bugx interaction list --root <project> --json
    bugx interaction show <question_id> --root <project> --json
    bugx interaction answer <question_id> --value <value> --root <project> --json
    bugx interaction cancel <question_id> --root <project> --reason <reason> --json

AI 不得猜测 data sharing consent、report policy、tooling choice、repair strategy、destructive action 或 recovery decision。未回答问题必须保持 BLOCKED/待交互；回答需要真实 question_id 并写入状态。

## 9. Privacy and network contract

- model name 不等于 Provider credential。
- auth status 默认只显示存在性和状态；auth verify 可能网络访问。
- credentials 默认在用户级 BugX 路径；不得读取、打印、上传 token/refresh token/API key。
- cloud status 是本地状态查询；sync、upload、log-report、非 dry-run feedback 是显式网络动作。
- sync scope 为 project、global、all；未指定时读取用户级默认，显式 scope 优先。
- sensitive policy 为 block、redact、allow；detail mode 为 full、summary；data sharing false 硬阻断 cloud push/pull。
- 发送前要记录 consent、scope、detail mode、sensitive policy、payload 摘要和网络结果。
- 不要把 .bugx/、MCP wire log、业务源代码或完整 artifact 放入公开模型上下文。
- 环境变量设置和作用域以[环境变量参考](../04_reference/01_ENVIRONMENT_VARIABLES.md)为准。`BUGX_PROJECT_ROOT` 是项目根候选，`BUGX_CLOUD_API_BASE` 是远端地址覆盖，`BUGX_GLOBAL_*` 是用户全局范围；不能互相替代。
- `BUGX_INTERACTION_REQUEST` 是引擎输出，不是 AI 可自行填写的配置；`BUGX_NATIVE_AGENT_ID`/`BUGX_MCP_SESSION_PROOF` 是身份或 session 证据，不得伪造。`BUGX_NON_INTERACTIVE=1` 或 `BUGX_HEADLESS=1` 不代表同意 gate；`BUGX_ACCEPT_DATA_SHARING=1` 只有在用户明确授权后才能设置。

### 9.1 Cloud egress matrix

把云端能力按数据面区分，禁止用一条路径的成功替代另一条路径的证据：

| path | entry | payload | scope/default | network/local effect | completion evidence |
|---|---|---|---|---|---|
| knowledge sync | `bugx cloud sync` | V2 knowledge records（patterns/incidents/tool experience/consumption/tombstones；project 层有本地过滤） | `project|global|all`；显式 `--scope` 优先，省略时读 `cloud.sync_scope_default`，缺省 `all` | 双向；每层 `pull baseline → push → pull`，写 SyncStore/outbox/inbox/conflict/dead-letter | 每层 summary + `cloud status --json` cursor/error/queue/conflict 收敛；0/`ok` 不足够 |
| project profile | `bugx project sync` | `wire_project_profile(profile)` 的紧凑项目画像/代码指标 | 当前 root；无 knowledge scope | 单向网络，登记/写 `<ROOT>/.bugx/cloud-project.json`；完整 profile 留本地 | JSON `status=ok`, `project_id`, `synced=true`, remote result |
| telemetry | `bugx telemetry flush|upload` | 独立飞轮 outbox 的脱敏事件/metrics；strict 会缩减字段 | 用户级 outbox；batch default 20 | 单向；成功发送项从 outbox 清除，失败保留/递增 attempts | `telemetry inspect --latest --json`、egress audit、flush 输出与退出码 |
| diagnostic report | `bugx cloud log-report` | 诊断日志尾部；default 256 KB，handler clamp 1..1024；device/version/kind/payload | 当前 root；kind default `cli-diagnostics` | 单向 POST，需 auth/配额；日志可能含路径和上下文 | 上传前审查 + bytes/lines、网络结果、退出码 |
| lesson feedback | `bugx cloud feedback` | 本地 feedback 的 lesson_id/outcome，最多 100，附可选 run/project | 当前项目/lifecycle filter | `--dry-run` 无网络；非 dry-run 单向 POST | dry-run 预览或 updated/missing、网络结果、退出码 |
| bundle | `cloud export|upload` | export 生成的指定 V2 JSON bundle | export default project/full；upload 以输入 bundle scope 为准 | export 本地原子写；upload 网络并采用 snapshot、强制 baseline pull | bundle SHA-256 + snapshot_id/version + upload 后 status |

知识同步的 `detail-mode=summary` 只是删除部分 pattern/incident 详情，不能当作“无业务内容”或“无敏感数据”。`sensitive_policy` 的值是 `block|redact|allow`；配置缺失时实现缺省 `allow`，所以 AI 在用户未选择策略时必须停下来询问，而不是静默上传原始记录。非默认策略的审计摘要在 `~/.bugx/logs/sync-policy-audit.jsonl`。

注意：`config set telemetry.data_sharing false` 会清空飞轮 outbox，并阻止正常生产者入队，但当前 CLI `telemetry flush/upload` handler 构造 uploader 时显式传入 `data_sharing=True`，因此它不是独立的 consent gate。AI 必须在调用前自行确认用户授权；不能把该命令返回 0 写成“隐私策略已放行”。

### 9.2 Cloud state machine and recovery

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

## 10. Readiness rule

以下任何一项缺失，都不能返回“产品功能已验收”：

1. 真实 Host ACP/Headless 入口建立 session；
2. tools/list handshake 和 session proof；
3. Host/Worker identity、project/polling/lifecycle/run 映射；
4. claim、worker-start、tool-call/heartbeat、checkpoint 的真实 receipt；
5. Finding verify/confirm 或明确无 Finding 的 gate 证据；
6. coverage、finish、post、report/integrity 的结构化结果；
7. 至少覆盖 ACP timeout/cancel、MCP failure、checkpoint retry/failure、heartbeat loss/lease reclaim、provider limit、budget exhaustion、open Finding 和 output truncation 的可观察处置。

仅有 parser、schema、prompt、模型文本、loopback 或单元结果时，机器状态必须是 UNVERIFIED 或 BLOCKED。当前实现和本文件的契约可以被源码确认，但真实宿主 ACP、自治闭环、远端 cloud 和跨平台安装的运行证据仍需目标环境单独提供。
