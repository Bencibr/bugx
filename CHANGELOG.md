# Changelog

All notable changes to the **BugX** project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> This public copy contains user-facing release notes only; internal development records are not distributed.

## [Unreleased]

### 报告处置与上报口径

- 交互启动新增报告处置三选一门禁（不上报 / 仅本地 / 上报云端），选择按来源落盘并去重，不再把默认值当作已确认。
- `bugx engine pre --replay-share-pending` 可显式补偿「仅本地」策略留下的待共享队列。
- 运行结论上报默认不再脱敏（原样发送）；需要凭据/路径净化时显式开启 `BUGX_REPORT_REDACTION=standard`。
- 同一上报 operation_id 的重放返回 409 时按幂等成功处理，避免重复落库。

### Codebase Memory 版本选择与自愈

- 探针会检查同名命令候选并只接受固定版本；PATH 中较旧的首个命令不会遮蔽已安装的正确版本。
- 找不到固定版本时，启动会通过已登记的隔离安装方案自愈，并要求安装后精确版本复核与项目索引检查都通过才报告就绪。
- 版本不匹配会触发修复；仅索引失败不会重复安装。旧版程序不会被自动删除。

### Skill Manifest 工具声明校验

- `required_runtime.tools` 中未登记的 probe ID 现在会在 Manifest 校验时明确拒绝；已登记的工具身份不会被误当作其外部依赖已就绪。

### Doctor 诊断授权边界

- AI 只补充 Doctor 的根因分析；修复命令和可修复状态由本地确定性检查及实际修复路径决定，未知检查不再得到通用 `doctor --repair` 建议。
- Doctor 不再强制重装整套漂移宿主适配包，避免覆盖项目内本地修改；全局引擎自动替换继续受严格 Build ID 门禁约束。

### 全局遥测项目隐私

- 统计面板使用安装内不透明项目标签聚合成本，不再显示项目目录名；同一安装内的标签仍可关联多次 session。
- 终端成本排行完整显示 20 字符项目标签，避免不同项目显示成相同的截断前缀。
- 查询旧遥测库时先升级项目标识字段，保留统计数值。

### CLI 安装资源安全恢复

- 若当前 CLI 载荷损坏，只有在当前非 editable wheel 的完整 `RECORD` 与同 Build ID 全局引擎逐项一致时，才会先尝试本机原子恢复；不符合时才请求官方 stable wheel。
- Stable 资源恢复优先查询 BugX 官方 stable API；该 API 不可用时，可检查官方 PyPI 是否已发布匹配的 stable wheel。
- 两个来源均通过版本、SHA-256、wheel 身份、Build ID 下限和完整核心清单校验；PyPI 后备拒绝重定向、yanked 包、sdist、预发布和低版本。
- 没有足够新且通过验证的 wheel 时继续 fail-closed，不把旧版 PyPI 包或源码目录当作恢复来源。

### ACP/headless 探活隔离

- 所有真实 ACP/headless 探活强制在一次性私有项目中运行；Codex headless 使用隔离的 CODEX_HOME、临时信任/Git 配置，并把项目 MCP 路径重绑到副本。
- 探活 Git 不读取用户模板 hook；跨进程锁稳定绑定源项目，锁不可用或权限过宽时阻断且不改写外部目录。保留探活项目时报告可能留下临时凭据副本。移除关闭隔离的参数，并拒绝从符号链接目录读取项目外宿主配置。
- OpenCode 隔离副本中的已有 BugX MCP 项也会重绑到本次临时 root；源配置不变，第三方项保留。更新结果使用 `probe_mcp_updated`，原子写失败时在启动宿主前阻断。
- 校准宿主 ACP 状态矩阵：源码当前登记为 2 个 `ready`、5 个上游 `unavailable`、5 个 `todo`，设计文档不再沿用旧的 9 个 TODO 计数。

### CLI 安装资源自愈与完整载荷发布门

- 默认 Python 安装缺少 BugX Agent 资源时，CLI 可校验 stable wheel 并修复当前 Python 环境；也可运行 `bugx update --repair-assets` 显式修复。在线恢复需要 stable 服务已发布 Agent wheel；发布未配置或校验失败时会停止并给出修复指引。
- 显式资源修复会识别 CLI 资源 Build ID 落后于共享引擎的情况，只接受达到所有现有 Build ID 下限的 stable wheel，并在安装前复核共享引擎版本，避免用旧 wheel 覆盖新引擎。
- 发布流水线在发布前核对 wheel 中核心清单的文件、哈希及 CLI/core 版本，阻止残缺或不一致的载荷发布。
- 安装资源自动恢复固定查询官方 stable 发行地址，不再把可配置的云端知识同步地址误用为 stable 包源。
- 新项目仅在只读状态明确返回 `UNINITIALIZED` 时由 `pre` 自动建立初始布局；已有数据但布局不完整时保留现场并阻断，不调用 `init` 覆盖。
- ZCode、Claude、Cline、Qoder 的 Worker 模板，以及 Claude/ZCode Agent 边界和 locale 缺失 fallback，统一要求先检查只读项目状态；只有 `UNINITIALIZED` 才运行 `pre`，`FATAL_BLOCKED` 或检查失败时保留现场并停止。
- 签入的 ChatGPT Skill、Cline/DSH 项目 Skill 与 DSH/Pi 安装模板按各自 locale 使用统一 renderer 重建。
- 新增安装资源自愈的 wheel 安全回归，以及 Worker 启动路由一致性守卫；构建产物和发布状态以当前发行流水线结果为准。
- wheel data-files 源 glob 必须覆盖核心完整性清单中的每个文件；新增适配器子目录映射与一致性回归，避免清单签名完整但发行 wheel 漏装模块。

### opencode ACP 模型轴打通 + L1 文档层收口

- **你遇到的现象**：`bugx` 的前置探活在 opencode 上一直报
  「`MODEL_NOT_PINNABLE`：请求模型未被宿主应用」，即使会话本身能正常跑完、MCP 工具也确实
  被调用——于是"指定模型跑过了"这条结论永远拿不到。历史报告里甚至请求过
  `anthropic/claude-sonnet-4-5`，而本机 `opencode models` 49 个模型里根本没有这个名字。
- **根因（探针只认一个已废弃的方法名）**：opencode 2.1.x 的 ACP **没有** `session/set_model`
  （回 `-32601`），模型是 `session/new` 返回的 **`configOptions`** 里的一个
  `category=model`、`type=select` 选项；切换走 **`session/set_config_option
  {configId: "model", value: <模型>}`**，宿主会在结果里回写 `currentValue`。
- **修法**：探针按「config option 优先、`set_model` 仅降级」的阶梯下发模型，并**只认宿主回写的
  `currentValue`** 作为"真的切过去了"的证据；请求值不在宿主目录时不再盲发（盲发会得到
  "宿主不报错但没换模型"的假通过），改为登记新失败码 **`MODEL_NOT_OFFERED`** 并继续取通道证据。
  回执新增 `acp_model_api`（`method` / `config_id` / `applied` / `applied_value` / `tried`）。
- **真机证据（opencode 2.1.3）**：`coder/glm-5.2-free`、`opencode/beam-free`、
  `opencode-go/deepseek-v4-pro` 三次都拿到 `applied=true` + `currentValue` 一致；
  其中 beam-free 还产出了 host `structured=true` 的工具调用证据。**钉住 ≠ 可用**：glm-5.2-free
  整轮零输出（`HOST_SILENT_NO_FIRST_TOKEN`）、deepseek-v4-pro 需要 provider 认证
  （`AUTH_REQUIRED`），两种失败都已分开归因，回执见 `state/l1b-acceptance/acp-opencode-model-pinned-*.json`。
- **L1 文档层收口（U-2）**：`CORE-202` 图纸与源码逐条复核完成——全部行号锚点按当前源码刷新、
  删除「旧声明回退分支仍在」的过期措辞（`_autonomous_degraded_choices` 已是严格分类器）、B5
  同步为 4 条可执行断言；`C5_RESULT.md` 状态 `DRAFT → AUDITED_OK`，U-1/U-2/U-3 全部闭环。
- 影响面：仅 ACP 探针与文档；引擎运行期行为不变。

## [Unreleased]

### Codex 整机打通：宿主沙箱不再"假装看不到 MCP"

- **你遇到的现象**：在 Codex（`--approve-for-me` 无头/自治）里跑 BugX，`pre` 每次都报
  「Supervisor MCP 已配置但当前宿主未加载，请重启宿主」，重启多少次都一样；就算绕过它，
  还会被「本轮启动已停止：semgrep」拦住。
- **根因一（引擎）**：macOS 上进程启动指纹只靠 `/bin/ps`，而 Codex 沙箱**禁止执行 ps**。
  拿不到指纹 ⇒ 所有 MCP 会话证明都被判不可信 ⇒ 引擎永远认为宿主没加载 MCP，自愈探针再去
  抢宿主已持有的锁，最后要求重启。现在改走 macOS **libproc 系统调用**（沙箱内外取到同一
  指纹）并把 Windows 实现并入同一处，`ps` 只作兜底；跨版本 `ps:`/`libproc:` 指纹按启动时间
  比较，不再互相误判。
- **根因二（宿主接线）**：Codex 沙箱只允许写工作区。`~/.bugx/mcp-runtime` 写不进 ⇒ 可选
  MCP（semgrep）供给失败并被判 critical；`~/.semgrep` 写不进 ⇒ 即使装好 semgrep 也直接
  `PermissionError`。现在 headless/autonomous 两条入口都会把
  `~/.bugx/mcp-runtime`、`~/.bugx/logs`、`~/.semgrep`、`~/.cache/semgrep` 作为可写目录授权
  （存在才授），**不**授权 `~/.bugx` 本体，引擎本体仍不可被改写。
- **C 路径探针支持 `--host codex`**：`scripts/smoke/smoke_host_native_task.py --host codex` 用
  Codex 原生 `collaboration.spawn_agent` 并发派发 Worker，派发证据取 Codex 自己的会话账本
  （exec 事件流不报 spawn）；环境画像在沙箱外先采集再复用，避免 critical 探针被沙箱误伤。
- 影响面：只影响 Codex 宿主与 MCP 会话证明；opencode/Cline 路径不变。


### L1-ADV-D4 修复：改完代码可以在同一轮内重新覆盖（不再"永远收不了口"）

- **你遇到的现象**：本轮里真的把漏洞修掉了，但 `post` 报 `#1 calc=未覆盖` 之类的覆盖门禁失败，
  而且重试也没用——只能等下一轮。这不是"覆盖数据不准"，而是**内容绑定覆盖**判定旧证据失效后
  缺一条退回路径。
- **根因（两处）**：①`coverage check --auto-backfill` 在 post 内对内容变化的单元会重置为
  `未覆盖` 并落盘，却打印「不改覆盖状态」；②Supervisor 账本仍记 `covered`，而 `covered` 是
  终态、同一轮又不允许重复领取 ⇒ 单元再也领不到。
- **修复**：post 在检测到内容失效时调用新增的 `invalidate_coverage()`，把失效单元退回
  `pending`（旧 claim 标记为历史代），并打印
  `COVERAGE_CONTENT_INVALIDATED` + `next_action=REDISPATCH_UNITS`；你只需重新
  `claim → worker-start → checkpoint` 产出新证据，再跑一次 post 即可同轮收敛。
  失效 scope 同时写入清单同目录 `inventory-diff.json`，可机器读取。
- **不会误伤**：`not_applicable`/`removed`/`superseded` 不因内容变化被退回；单元仍有活跃租约时
  失效会被拒绝（先收工或等租约到期）；`--allow-incomplete` 不再是这条路的提示（它只接受
  诚实的未覆盖债务）。
- **故障矩阵不再"假通过"**：`target_drift_rebind` 过去用「抛任何异常就算拦截」，实际是被
  execution-receipt 门挡下的。现在先绑定合法 receipt、再注入真实身份漂移，并加**控制组**
  （同提交不漂移必须被受理）——9/9 PASSED（收据 SHA `c6e09cbf…`）。
- 证据：`state/l1b-acceptance/adversarial.json`（对抗探针 50.9s `PASS`）、
  `state/l1b-acceptance/l1-hostfree.json`（全流程 67 断言 `PASS` 83.3s）、
  `fault-matrix-receipt.json`。

### L1-ADV-D1 修复：Finding 裁决证据只能来自引擎核验（破坏性契约收紧）

- **根因**：`bugx_supervisor_confirm_finding` 只写 `confirmation={state,reason,at}`，
  而 `post` 的 Finding 收敛门禁要求 `oracle`/`provenance`/`observations`/
  `negative_control`/`root_cause_verified`/`target_execution` ⇒ `verify → confirm → post`
  永远无法收敛（`log-only` 只放宽"修不修"，不放宽"谁裁决"）。
- **写侧收口**：新增 `finding_confirmation.apply_decision()` 作为唯一裁决写入口——
  深拷贝 → 合并既有证据（不再覆盖 worker 已提交观测）→ 门禁自检 → 通过才写回；
  失败抛 `ConfirmationError`（MCP 返回 `-32602` 并给出下一步），不再产生半成品。
- **证据派生**：新增 `build_confirmation_from_verification()`，只重放引擎核验 receipt
  （`positive_runs[].execution_id/exit_code/stdout_sha256` + `negative_control`）——
  宿主自述不构成证据；`root_cause_verified` 必须宿主显式断言，引擎不代为生成。
- **oracle 不再自报**：`oracle.provenance.verified` 由引擎重算 `oracle_reference`
  文件 SHA-256 与 recipe 内 `oracle_hash` 比对得出；`independent_from_change`
  要求该文件不在 finding 证据路径集合内（不拿被测缺陷文件当判据）。
  `oracle.tier`/`provenance_kind` 仍是宿主声明标签（文档已写明该边界）。
- **transaction 化**：裁决搬进 `AuditSupervisor.confirm_finding()`（revision 栅栏 +
  终态保护 + `finding_confirmed/rejected` 事件含 `channel=confirm_finding`），
  MCP handler 只做参数校验与协议映射。
- **`rejected` 分档**：驳回只要求 `rejection_class` + `decision_reason`，
  不再因缺 oracle/observations 被确认侧契约反噬（附带字段仍逐字段校验）。
- **`L1-ADV-D5`（本轮新发现并修复）**：修复后同配方复验会得到 `CANDIDATE` 并覆盖
  `finding["verification"]`，导致无法再记录 remediation；现单独留存
  `verification_confirmed` 供确认/修复事务复用。
- 探针 `--phase adversarial` 重写为 5 道正向/反向断言：未裁决阻断 → **未核验就确认被拒（红队）**
  → 引擎核验 + 确认 3 条 → 宿主把决策写入结算报告 → `post` 收敛
  （报告 `发现问题总数 3 / 已确认修复数 3 / COMPLETE`，账本与报告双轨一致）。
  另留 `BH_L1_INPLACE_FIX=1` 取证开关，记录 `L1-ADV-D4`（本轮内修复后覆盖证据
  内容绑定失效、claim 仍认为已覆盖 ⇒ 只能跨轮收口）。

## [Unreleased]

### L1 20 节点覆盖矩阵 + 负向门禁 + 对抗夹具（仍在无宿主车道内）

- `scripts/smoke/smoke_l1_flow.py` 从 4 阶段 19 条断言扩到 **7 阶段 58 条断言**：
  - `--phase audit`（0.0s）：把 `CORE-200` 的 **N1.1~N1.20** 逐节点裁决成
    `ASSERTED`(14) / `PARTIAL`(5) / `MANUAL`(1)，写回执 `node_coverage` + `node_summary`；
    只有 `ASSERTED` 会守卫生效，`PARTIAL`/`MANUAL` 必须在报告里显式披露。
  - `--phase gates`（2.6s）：在一次性临时项目上验两条 fail-closed——无 `.git` → `pre` exit 2；
    清单 `bugx_version` 漂移 → `pre` exit 2（`BUGX_CLI_VERSION` 覆盖会被 CLI wrapper 吃掉，不能当注入点）。
  - `--phase adversarial`（≈38s）：planted/negative-control/bait 三类样本 + `adversarial_expectations.json`
    ground truth，走真实 claim → checkpoint（带 11 字段结构化证据 + 受信 `execution_receipt`）→
    `post` → 宿主 `verify`/`confirm` 裁决，断言"未裁决必须阻断结算"。
- **新发现 P0 缺陷 `L1-ADV-D1`（未修复）**：`confirm_finding` 只写 `{state,reason,at}` 覆盖
  `confirmation`，而 `post` 的 Finding 收敛门禁要求 `oracle`/`provenance`/`observations` ⇒
  `verify → confirm → post` 永远无法收敛（`log-only` 只放宽"修不修"，不放宽"谁裁决"）。
  探针把该缺陷结构化写进回执 `defects[]`，`core/` 未改动（修复需先过 6 问审计）。
- `tests/gates/test_smoke_l1_flow.py` 从 11 例扩到 **18 例**：新增 20 节点清单/裁决白名单、
  findings 账本 dict/list 归一、ground truth 三类互斥、`_gate_output` 全流拼接。
- 新增 `--exempt-cbm`（默认关）：把 `codebase-memory-mcp/cli` 写进探活项目 `tooling_exemptions`
  ——同机已有 CBM 会话时 `pre` 会 fail-closed（版本冲突），这是环境并发条件，豁免留痕进回执。
- ACP 复核（opencode，回执 `state/l1b-acceptance/acp-opencode.json`）：`adapter_status=ready`，
  host/worker 双通道 Session Proof `loaded/callable/healthy` 全真且 `fresh=true`，两侧各 2 次
  `bugx_supervisor_status` 真实调用（`mode=code_mode`）；但 `session/set_model` 返回
  `-32601 Method not found` ⇒ **`MODEL_NOT_PINNABLE`，模型轴 `UNVERIFIED`**。
  旁证：`opencode run --model opencode/deepseek-v4.1-flash-free` 返回 `PROBE_OK`（headless 可用，ACP 不可钉）。

### L1 秒级闭环车道（无需宿主 / 无需模型）

- 新增 `scripts/smoke/smoke_l1_flow.py`：在固定探活项目（`~/.bugx/probes/l1-canonical`）上串起
  `bootstrap → pre → protocol → post`，用官方 `core/supervisor/cli.py rpc` 驱动
  `status/claim/worker_start/tool_call/heartbeat`，用 `core/supervisor/worker_checkpoint.py finalize`
  生成合法 checkpoint，最后 `bugx engine post --final` 结算。19 条断言全部取自产物
  （`runtime.json`/`supervisor.json`/`report.md`/协议 `structuredContent`/预算账本增量），
  不读模型自述。实测冷启动 31.2s、热复用 19.6s，`--phase protocol post` 约 20s；
  对比真实宿主 + LLM 会话单轮 5~20 分钟。回执落 `state/l1b-acceptance/l1-hostfree.json`。
- 新增 `tests/gates/test_smoke_l1_flow.py`（11 例纯逻辑）并登记 `tests/test_registry.json`；
  首跑即抓到真实缺陷：`--phase post pre post all` 会重复执行 `pre`（`all` 分支未去重），
  已改为末位去重保序。
- L1 推进从「宿主 × 流程」纠正回宿主无关的
  「轴 × 流程」——宿主无关主干由本探针每次复测，宿主只承担一次性事实取证（DoD 四条）。

### codex 无头通道闭环 + CLI 驱动宿主升级

- 修复无头 `codex exec` 缺少 `--approve-for-me` 的缺陷：默认 approval policy 为 `never` 时，
  任何 MCP 工具调用都会以 `MCP tool call requires approval` 失败（宿主能发现项目级 MCP 与
  工具名却无法调用，Worker 等于没有控制面）。现在无头 argv 固定带该 flag，与自治通道同一
  approval 语义。真机反证：加 flag 后 `bugx_supervisor_status` 调用 `status=completed` 并返回
  `structured_content`。
- 修复 `bugx uninstall` 不回收本工具写入的用户级信任条目：卸载后会物理删除
  `$CODEX_HOME/config.toml` 中由 BugX 新增的 `[projects."<root>"]`，用户原有偏好与显式
  `untrusted` 一律保留（原因写进 `preserved`）。此前临时项目路径被重建即可让宿主静默加载
  任意项目级 MCP 命令。
- 无头通道探针新增 `--mode mcp`（判据 8 → 10）：先用真实 `bugx install` 装配项目，再用无头
  argv 发一次真实会话要求调用 `bugx-supervisor` 的工具，只认 `status=completed` + 无 error +
  带 `structured_content` 的调用（裸出现 `mcp_tool_call` 视为假阳性）。
- 真机验收（codex-cli 0.163.0）：`--mode argv/session/commit/mcp` 全 PASS，回执见
  `state/l1b-acceptance/codex-headless-{session,commit,mcp}.json` 与残差
  `codex-headless.residual.json`。
- 宿主引擎改为**只用 CLI 升级**：`bugx init/install` 把 `~/.bugx/core` 从 build 198 升到 199，
  `bump_version.py --bump-build-id` 经同一安装器重建 build 200；`bugx doctor` 报 449 个静态
  文件哈希一致。本轮版本号改为 `0.1.0`，Build ID 递增至 200。

## [Unreleased]

### 提交门禁与宿主并发验收

- 提交门禁（`scripts/pre_commit_check.py`）现在会校验自己安装的 Git 钩子是否被改写、`--no-tests`
  必须留痕（审计日志 0600），并支持把测试集绑定到显式白名单，越界即拒绝执行。
- 新增宿主原生 Task 并发探针 `scripts/smoke/smoke_host_native_task.py`：真实驱动 `opencode` 会话跑完整
  生命周期（`pre → workers --execute（应拒） → workers（并发 subagent） → post`），只读产物裁决，
  回执落 `state/l1b-acceptance/`。
- 真机验收：`opencode` 单会话内 10 个原生 subagent 分两批并发派发，10/10 claim 真实开工并提交
  checkpoint（执行窗最大重叠 142.97s、整轮重叠 419.31s、峰值并发 3），`post --final` 以
  `INCOMPLETE`（覆盖未达标，显式 `--allow-incomplete`）正常封存。
- 本轮不改变版本号（仍 `0.1.0`），Build ID 递增至 199；codex 无头通道仍待真机取证。

## [0.1.0] - 2026-10-16

### FunIDE 与跨平台构建修复

- `bugx install --agent funide` 现在同时配置 Agent、Skill、MCP 以及 MCP 自动启动，并保留用户已有配置。
- Windows/macOS 构建意图锁改用平台兼容实现，避免 Unix 专用模块阻断 CLI 启动。
- 增加 FunIDE 路径发现、JSONC 配置、所有权清理和宿主检查的自动化覆盖。
- Build ID 从 115 递增至 116。

真实 FunIDE UI 的 ACP/Worker/MCP 闭环仍未在本构建中验收，状态为 `UNVERIFIED_HOST`。

## [0.1.0] - 2026-10-08

### Post 收尾与执行加速

- 合入 DOC-208 的进程内执行 harness、冻结快照 `post --dry-run`、单次 FindingsSnapshot、知识账本脱敏投影和 Supervisor 串行授权。
- post 失败现在同时保留稳定 attempt telemetry 和 Supervisor 权威 `lifecycle_blocked` 事件；遥测不可用时 fail-closed 标记为不可用，不伪称已落盘。
- 统一 findings 报告容量上限为 4 MiB，并校验快照绑定与可配置的 credited 上限，避免跨 runtime 复用报告或结算计数漂移。
- 语义版本继续固定为 0.1.0，Build ID 从 55 递增到 56。

真实 ACP/宿主 Agent → Worker → MCP → checkpoint → post 闭环仍未在本提交中验收，状态保持 `UNVERIFIED_HOST`。

## [0.1.0] - 2026-10-08

### 性能与测试基础设施

- 测试夹具可在同一进程复用启动命令分发器，减少重复 Python 冷启动。
- 批次运行测试并共享经过内容校验的只读模板，启动流程的 56 个回归用例最终三轮分别用时 61.19 秒、65.22 秒和 60.26 秒。
- 调用结束后恢复项目路径、输入输出、日志和环境，避免不同临时项目之间共享运行状态。
- 并发心跳、信号和进程组场景继续通过真实子进程验证。

### 修复

- `supervisor status --root <path>` 现在正确使用动作参数后的项目根目录。
- macOS 测试 HOME 统一使用真实路径，OpenCode/ZCode 卸载清理回归恢复通过。


## [0.1.0] - 2026-10-07

### 修复

- 修复 Supervisor MCP 缺失和安装成功后的重启指引错误，配置完成后仍等待新会话加载验证。
- 清理启动模块冗余转导出及过时测试，补齐交互、证据与新项目完整流程验证。
- 开发架构门禁改为真实导入环检测，删除会误判标准库导入和旧依赖删除的历史数量快照。

## [0.1.0] - 2026-10-07

### 修复与恢复

- 修复带行号标记的访问器验证分层，并保留高风险升级优先级。
- 宿主可带原因和 revision 重开旧误报，保留旧判定与证据摘要，重新验证后裁决。
- 业务提前准入授权持久化，自动调度与显式领取使用相同规则。
- 修复相对 Cline 数据目录配置的项目路径锚定。

## [0.1.0] - 2026-10-07

### Worker orchestration and proposal alignment

- Added `--dry-run` and `--brief` flags to the `workers` CLI command for machine readability and claim inspection.
- Enforced reproduction steps for candidate findings marked as reproducible across locale prompt sets.
- Fixed MCP response envelope stability with pinned `mcp_schema_version: 1`.
- Audited worker orchestration and internal architecture boundaries.


## [0.1.0] - 2026-10-07

### Profile boundary fix

- Prevented JSX/Vue/Svelte/TSX fixtures under test and sample paths from promoting backend projects to the Web form.
- Preserved `NOT_APPLICABLE` probe states for transparent shape-guard accounting.


## [0.1.0] - 2026-10-06

### Evidence integrity and recovery hardening

- Differential PoC verification now accepts raw text, project-local paths, JSON objects, and JSON arrays through the library and MCP boundary.
- Infrastructure failures such as missing commands/files or permission errors return `INFRA_ERROR`; they are not reported as confirmed target defects.
- Checkpoint Findings now require real workspace evidence and stable SHA-256 fingerprints. Verification failures remain `unverified`; only a genuine `FALSE_POSITIVE` becomes a false-positive lifecycle result.
- Added bounded worker retries, structured budget-exhaustion recovery, aligned 300-second model/Worker defaults, and the CLI `reset-budget --force` recovery flag.
- Improved risk-tier routing for cryptography, keys, SSH, and database surfaces while keeping trivial accessors in triage.

---

## [1.0.2] - 2026-09-28

### Added & Improved

- **Qoder Native Agent & Dual-Channel MCP Architecture (阿里巴巴 Qoder 原生智能体与 MCP 连接器集成)**:
  - **Custom Agents**: Project-level primary audit agent (`.qoder/agents/bugx.md`) and concurrent audit worker (`.qoder/agents/bugx-worker.md`), mirrored into `~/.qoder/agents/` for global `@bugx` and `@bugx-worker` task dispatch.
  - **Connectors & Dual-Channel MCP**: Stdio MCP supervisor configuration in `.qoder/mcp.json` and user-level `~/.qoder/settings.json` (`mcpServers`) with separate `bugx-supervisor` and `bugx-supervisor-worker` control channels, accompanied by `integrations/qoder/connector-meta.json` and security guide.
  - **Conflict De-duplication**: Converged onto a clean, single-source Agent + MCP architecture (Option A), eliminating redundant plugin containers and loose skills to prevent `overrides same-source skill` collisions in Qoder CLI and Desktop.
- **Host Lifecycle & Doctor**: Full lifecycle orchestration via `bugx install --agent qoder` and `bugx uninstall --agent qoder`, with 100% automated health validation in `bugx doctor`.

---

## [1.0.1] - 2026-09-30

### Fixed

- MCP request logs now retain request/tool correlation and request-level trace spans.
- Worker checkpoints reject source scripts used as execution artifacts; generated output remains hash-verified.
- Supervisor `max_wall_seconds` now remains a run-wide hard cap across budget resets; recovery cannot restart or extend the global wall clock.

## [0.1.0] - 2026-09-27

### 🎉 Official General Availability (GA) Release

BugX 0.1.0 marks the initial public release of BugX — an open-source, host-neutral, adversarial white-box & black-box AI-driven code auditing and vulnerability hunting agent.

### Added & Improved

- Optimized DeepSeek Harness (DSH) and WorkBuddy human-facing agent preset card descriptions with clear, conversion-oriented copywriting highlighting role definition, key benefits, and workflow trigger timing.
- Unified security, privacy, and data compliance commitments across `README.md`, `01_README_NPM.md`, `02_README_PYPI.md`, and `docs/01_guides/01_USER_GUIDE.md` (Local-First, `--privacy=strict`, `--accept-data-sharing`, secret redaction).
- Synchronized WorkBuddy connector metadata (`integrations/workbuddy/connector-meta.json`) with bilingual promotional descriptions and structured audit examples.
- Improved DeepSeek Harness compatibility across DSH CLI and macOS DSH Desktop, including deployment of the Web UI and session MCP plugins.
- Kept the session MCP bridge compatible with both current and legacy MCP client APIs.

### 🌟 Key Highlights

- **Host-Neutral AI Orchestration (宿主中立 Agent 编排)**:
  - Native integration with 8 major AI programming environments: **OpenCode**, **Claude Code**, **OpenAI Codex**, **Cursor / Windsurf**, **Cline**, **DeepSeek Harness (dsh)**, **ZCode**, and **WorkBuddy AI**.
  - Non-intrusive runtime: BugX does not hijack model execution; it coordinates workers via standard Model Context Protocol (MCP) and structured Prompt Harnesses.
  - Elastic lease governance with sliding renewal (up to 8x adaptive wall-time upper bound).
- **Host CLI Lifecycle Usability & Dual Package Isolation (宿主生命周期易用性与双包隔离治理)**:
  - Supported `--agent all` and comma-separated multiple hosts (e.g. `--agent opencode,dsh`) in `bugx install` and `bugx uninstall`.
  - Added `--locale <zh-CN|en-US>` to `bugx install` for explicit installation language selection.
  - Added `--force` flag to `bugx uninstall` to backup and cleanly purge drifted or modified host entry files across version upgrades.
  - Enhanced `bin/bugx.js` npm thin wrapper to inject `BUGX_CLI_SOURCE=npm`.
  - Upgraded `bugx doctor` to classify executable CLI entries across npm, uv, pip, and pyenv, diagnosing version conflicts and generating exact uninstallation instructions.
  - Protected development repository git-tracked mirror declarations from ephemeral pre-release overwrite during `bugx install`.

- **Autonomous Lifecycle & Adaptive Budget (自治生命周期与弹性预算)**:
  - Strict hierarchical execution: `Project → Session → Round → Life`.
  - Dynamic `RoundBudgetCalculator` automatically calculates wall-clock budgets (180s ~ 7200s) based on codebase lines of code, detected technology stack, and worker concurrency.
  - Automatic orphan worker draining and clean state recovery.

- **Continuous Vulnerability Hunting & Immunization (持续漏洞挖掘与免疫包交付)**:
  - Reinforcement learning-based UCB1 multi-armed bandit algorithm for exploration vs. exploitation unit scheduling.
  - Three-tier model routing: lightweight reconnaissance (Recon) → deep exploitation (Exploit) → script-driven deterministic verification (Verify).
  - Red-blue adversarial game loop to eliminate false positives.
  - Automated synthesis of project-specific Semgrep AST rules and unit-level regression immunization test suites.

- **Code Intelligence & Supply Chain Security (代码智能度量与依赖供应链安全)**:
  - Multi-language AST parsing and local constant folding.
  - Software Composition Analysis (SCA) with deterministic lockfile parsing (`package-lock.json`, `poetry.lock`, `Cargo.lock`, `go.sum`, etc.).
  - Enterprise code intelligence metrics, technical debt economics model, and architectural risk topology visualization.

- **Knowledge Double-Ledger & Defense Flywheel (知识双账本与单向攻防飞轮)**:
  - Bounded isolation between Project and Global lesson ledgers.
  - SQLite FTS5 millisecond-level full-text retrieval for patterns and incidents.
  - Automatic injection of historical pitfalls and hardened probes into worker prompts of matching tech stacks.
  - Outbox-buffered, privacy-redacted, unidirectional telemetry flywheel.

- **Sandbox Self-Healing & Compliance Redaction (目标沙盒自愈与合规脱敏)**:
  - Whitelist-based target code and environment snapshot rollback.
  - Port health probes and cold-restart self-healing.
  - Strict AST syntax-tree level token redaction (`--privacy=strict`) ensuring private credentials and sensitive strings never leave the local environment.

- **Change-Aware Targeted Regression Engine (变更感知定向测试引擎)**:
  - Sub-second feedback loop: tests only what was changed based on AST dependency closure and test registry mappings.
  - 4-dimensional anti-drift algorithm balancing rapid iteration with test suite coverage.

### 📦 Distribution Packages

- **npm**: Available as [`bugx-cli`](https://www.npmjs.com/package/bugx-cli) (`npx bugx-cli --help`).
- **PyPI**: Available as [`bugx-cli`](https://pypi.org/project/bugx-cli/) (`pip install bugx-cli`).

### 📚 Official Documentation

- [User Guide (01_USER_GUIDE.md)](docs/01_guides/01_USER_GUIDE.md) - Getting started, workflows, and troubleshooting.
- [Command Reference (02_COMMANDS.md)](docs/01_guides/02_COMMANDS.md) - Complete CLI commands, options, and environment variables.
- [Architecture Overview (01_ARCHITECTURE.md)](docs/02_architecture/01_ARCHITECTURE.md) - System architecture and component topology.
- [Audit & Verification Workflow (03_BUGX_AUDIT_FLOW.md)](docs/02_architecture/03_BUGX_AUDIT_FLOW.md) - Lifecycle flow and adversarial game loop.
- [AI Reference (01_AI_REFERENCE.md)](docs/03_ai/01_AI_REFERENCE.md) - High-density MCP context reference for AI coding agents.
