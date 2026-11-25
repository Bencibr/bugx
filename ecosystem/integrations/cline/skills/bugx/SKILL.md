---
name: bugx
description: BugX 是一个具备自主代码审查与测试能力的智能体，严把代码交付关：它帮助开发人员在写完代码后自动排查隐藏的深层 Bug，减少反复排错与返工；帮助测试人员自动化运行黑盒测试、功能验证与性能测试，自动覆盖手工盲测难以覆盖的极端场景；确保代码功能符合要求、具备良好的健壮性和稳定性。让开发和测试在繁琐的工作中解放双手，大幅提高工作效率、全面提升代码质量、显著降低后期维护成本，实现科技服务于人。
---
<!-- bugx-locale: zh-CN -->

# BugX

这是 BugX 的 Cline 项目级 Skill 入口。先读取全局引擎协议：通过 `bugx engine path --name bugx-runtime-contract.md` 和 `bugx engine path --name bugx-quick.md` 读取全局协议，并遵守其中的寿命、Supervisor、证据和报告协议。

该入口只把任务交给当前 Cline；BugX CLI 只安装资源，不启动 Cline、不执行审计。项目未被 Cline 信任时，请先在 Cline 中信任项目；进入会话后本 Skill 由项目 `.cline/skills/bugx/SKILL.md` 自动发现，也可在对话中用 `/bugx` 显式触发（Cline CLI 没有 `--agent` 启动参数）。
公开 Agent 模板来源：`bugx engine path --name bugx.md`。


## Cline 原生交互适配（强制）

启动器必须通过 `--host cline` 将宿主写入当前会话 runtime，不得让项目中的 `opencode.json` 推断宿主。使用当前 Cline 会话提供的 `ask_question`（或宿主等价的结构化用户选择控件）；按选项的唯一 `value` 及受控 `action` 执行。本入口由 Cline 项目级 Skill（`.cline/skills/bugx/SKILL.md`，Cline 自动发现或 `/bugx` 触发）加载，Cline 桌面端通过当前工作区的 BugX Skill 进入；Cline CLI 使用 `cline <prompt>`，无头自治由 CLI 的 JSON/headless 模式承载提示和会话恢复，`cline --acp` 是 ACP 服务入口，不能当作一次性 prompt 命令直接运行。Cline MCP 由安装器写入 Cline 数据目录内的 `<CLINE_DATA_DIR>/settings/cline_mcp_settings.json`（默认 `~/.cline/data/settings/cline_mcp_settings.json`）的 `mcpServers`：Cline 3.x 没有工作区级 MCP 作用域，项目内 `.cline/mcp.json` 永不被加载；必须同时存在并启用 `bugx-supervisor` 与 worker，Skill 文件不能替代 MCP。Cline 不支持 BugX 的 `Task(subagent_type="bugx-worker")` 派发契约；桌面端内置研究子 Agent不具备 Worker 写入与 MCP 权限，CLI 自带的 Agent/Team 接口也不等价于该 Task 契约。因此交互式单会话保持单 Claim 串行：运行 `bugx engine --root . workers --count 1`，再由当前会话通过 `bugx-supervisor-worker` 完成 worker-start、heartbeat/tool_call 和 checkpoint，核对有效 checkpoint 后才领取下一条。多进程 CLI/自治执行使用 `bugx autonomous --host cline --workers N` 或 workers 的显式 `--execute`，运行时上下文会把 host/execute/worker_capacity 原子写入当前项目会话，切换到 2~6 路有界进程池；每个 claim 在 `<artifact_root>/.cline_data` 中运行，注入 `CLINE_DATA_DIR` 与 `--data-dir`，仅加载 `bugx-supervisor-worker`（`--role worker`）和 `bugx-docs`，绝不加载主控 `bugx-supervisor`。进程池按 Supervisor 自适应槽位使用滑动窗口和 runtime context 的 `stagger_seconds` 错峰补位；超时、异常退出或无持久化 checkpoint 时回收该 claim，只有有效 Supervisor checkpoint 才算成功。mine 阶段只读共享源码，PoC 与证据只能写入 claim 的 artifact_root。


## 启动交互适配：启动策略（无问卷）

开工策略按 CLI 参数 > 项目 `.bugx/config.json` > 用户 `~/.bugx/config.json` > 系统默认值读取。在自治启动（--autonomous）模式下，常规启动不呈现云同步、审查方式、轮询、修复或提交问卷；在非自治交互模式下，由宿主调用原生问答控件呈现作战方案供用户确认（一键批准、微调或暂存稍后）。可选 MCP/探针不可用时自动回退到 Native Protocol Mode 与内置能力，不因缺少 session proof 要求重启。
## 宿主路由（先于意图分流）

- **ZCode 宿主**：优先加载同插件 `bugx-zcode` skill（ZCode 专属手册：直驱路径、rpc 回退、坑位对策）。若当前会话未注册该 skill（新装 skill 需重启宿主会话才可见），直接按其核心结论执行：`workers` 预占后**由宿主会话直驱协议通道**（worker MCP 工具或 `bugx supervisor rpc --role worker`，caller_role=worker），勿默认为 worker 派发子代理；`run_sandboxed_probe` 必须携带本次 Task 的 lease_id/execution_id。
- 其余宿主按后续章节执行。



## 如何使用本技能（AI 执行元指南与意图决策树）

当你（AI）触发并阅读本技能时，请无条件遵循以下认知模型与执行时序：

### 1. 意图决策树（Triage Decision Tree）
拿到任务第一秒，判定属于哪个分支：
- **分支 A：单点敏捷排查 / 特定函数 / Diff / 报错堆栈**
  - **标志**：单文件排查、复现某个函数、分析异常报错。
  - **动作**：就地编写轻量复现脚本（PoC）或运行定向测试，使用 `bugx view`, `bugx test-generate`, `bugx test-verify`；**严禁启动重型全流程门禁！**
- **分支 B：全库漏洞挖掘 / 深度变异 / 多 Worker 并发审计**
  - **标志**：用户明确输入 `/bugx`、要求“全库审计”、“挖掘潜在漏洞”。
  - **动作**：按标准生命周期流转（`engine pre` -> 门禁确认 -> Explore -> Mutate -> Repair -> 结算）。
- **分支 C：环境诊断 / 依赖修复 / 宿主状态排查**
  - **动作**：优先运行 `bugx doctor --root <ROOT> --json` 与 `bugx engine --root <ROOT> status --json`，绝不盲目 `init` 或 `--force`。

### 2. 标准五步操作闭环（5-Step Execution Loop）
1. **定根**：确定被测项目根绝对路径 `<ROOT>`，严格在 `<ROOT>` 内部执行，禁止全盘发散扫描。
2. **探况**：先执行只读命令检查运行态（`bugx engine --root <ROOT> status --json` 或 `bugx doctor`）。
3. **选路**：根据意图挑选命令；若需查阅参数，运行 `bugx help <顶层命令>`（如 `bugx help cloud`）按需动态读取，严禁盲猜。
4. **承接**：若 `engine pre` 返回退出码 `2`，表示等待宿主单选门禁交互；必须调用宿主原生控件展示选项，按用户选择的 action 执行，禁止文本伪造选择或自旋重试。退出码 `0` 只代表门禁通过，不代表挖掘完成。
5. **闭环**：缺陷必须给出最小独立复现脚本（PoC）与完整调用栈证据；使用 6 大标准汇报用词（只读检查、项目就绪、门禁就绪、测试执行、挖掘启动、挖掘完成）如实汇报。

### 3. 高频场景命令（覆盖 90% 日常调用）
- **环境诊断**：`bugx doctor --root <ROOT> --json`（`--repair-assets` 按需修复）
- **启动与门禁**：先运行 `bugx engine --root <ROOT> status --json`；仅当状态为 `UNINITIALIZED` 时运行 `bugx engine --root <ROOT> pre` 自动准备新项目。挖掘启动不要先调用 `bugx init`；它是独立 CLI 初始化操作，不替代 status/pre 状态机。若状态为 `FATAL_BLOCKED`、其他阻断状态或检查失败，保留现场并停止；禁止用 `init`、`reset` 或 `--force` 绕过门禁。
- **单点复现与变异**：`bugx test-generate --root <ROOT> --scope <PATH::SYM> --out <FILE>` / `bugx test-verify verify --scope <SCOPE>`
- **Supervisor 与并发排查**：`bugx supervisor status --root <ROOT>`
- **脱敏飞轮与日志**：`bugx telemetry inspect` / `bugx log --root <ROOT> --errors`
- **双轨报告与预览**：`bugx report both --eval-dir verification/evals/oss --out verification/evals/oss` / `bugx report serve --port 18991`

### 4. 紧凑型命令全景雷达（43 个顶层命令分类索引）
如需调用以下命令，通过 `bugx help <命令>` 动态获取具体参数与子命令，严禁盲猜：
- **诊断与探索 (10)**：`help`, `completion`, `guide`, `hosts`, `doctor`, `log`, `stats`, `view`, `project`, `self-audit`
- **核心生命周期与宿主 (15)**：`init`, `install`, `reset`, `uninstall`, `dsh`, `engine`, `supervisor`, `daemon`, `config`, `plugin`, `mcp`, `capability`, `mcp-runtime`, `update`, `interaction`
- **深度审计与测试验证 (10)**：`audit`, `probe`, `logic-analysis`, `mining-audit`, `test-generate`, `test-audit`, `test-verify`, `patch-forge`, `autonomous`, `goal`
- **云端、知识与交付 (8)**：`cloud`, `auth`, `telemetry`, `knowledge`, `lesson`, `skill`, `advisory`, `report`

### 5. 核心军规与废弃命令重定向
- **阻断不授权暴力修复**：`FATAL_BLOCKED` 严禁使用 `init`、`reset`、`--force` 绕过，先保留报告和日志。
- **废弃命令重定向**：`matrix` -> `bugx audit`；`console` -> `bugx report serve`；`watch` -> `bugx log --watch`；`test` 不是公共命令。开发验证请在仓库外生成最小临时验证脚本，或运行被测目标项目自带的测试，完成后删除过程文件。
