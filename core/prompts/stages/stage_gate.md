# BugX 阶段 0：启动门禁 Harness（Stage Gate Protocol）

## 1. 阶段目标与门禁硬约束
本阶段确保会话在受控、清洁、合规的前提下启动。任何未通过门禁的会话，严禁推进至代码修改或测试执行。

## 2. 交互防逃逸硬状态锁（Interaction Gate Lock）
> [!IMPORTANT]
> **绝对防御**：在任何宿主环境中，若模型仅在对话回复中输出自然语言承诺（如“我选择第 1 项”、“已确认启动策略”），**将被判定为逃逸违规**！
> 启动器状态机只认 `.bugx/` 下真实落盘的决策标记。未完成真实写入前：
> 1. Supervisor 拒绝向当前 Worker 分配租约与执行预算；
> 2. 会话状态持续锁定为 `idle` 或 `blocked`。

## 3. 启动决策与策略规划执行协议
请通过命令行或交互组件执行启动协议：
```bash
bugx engine --root . pre [参数]
```
必须对齐的核心决策与策略流程：
1. **云端知识同步（Cloud Onboarding）**：
   - 选项：`login`（登录并同步全局/项目经验） / `skip`（仅使用本地离线知识）。
2. **策略规划层与人机对齐门禁（Strategy Plan Gate）**：
   - **项目客观画像定性**：自动识别语言生态、软件形态（Web/API/CLI/Mobile/Embedded）、攻击面与运行时；
   - **靶向工具链推导**：按项目形态匹配工具（如 Web→Playwright、API→postmcp+fuzz、并发协程→concurrency_fuzzer、驻留内存→memory_probe）；
   - **错题集靶向预选**：从云端与本地错题账本召回同技术栈历史高危缺陷；
   - **经验集规则预热**：激活 L2 专项规则库（并发竞态、内存泄露、反模式等）；
   - **推荐执行组合**：生成整合了审查方式、轮询策略、修复模式与提交策略的预设作战方案；
   - **人机策略对齐单选**：向用户呈现方案（`approve_and_start` 一键批准 / `customize` 自定义微调细分参数 / `save_and_later` 暂存草案退出）。
3. **自定义微调决策（仅在选择微调时依次对齐）**：
   - **审查方式（Review Mode）**：`manual`（人工复核）/ `mcp-auto`（Supervisor MCP 协同自动审查）/ `later`（暂缓决策）。
   - **轮询策略（Polling Policy）**：`auto`（自动化多轮推进直到无新发现）/ `manual`（单轮结算后暂停）。
   - **修复模式（Fix Mode）**：`auto`（发现后立即执行 TDD 最小修复）/ `log-only`（仅记录证据不改源码）。
   - **提交策略（Commit Policy）**：`round`（每 Round 提交）/ `life`（每小生命周期提交）/ `manual`（人工决策提交）。

## 4. 探针可用性检查（Probe Health Check）
启动器将自动触发 `probe_registry` 探针自检。若有探针未就绪或处于降级状态：
- 优先执行就地自愈装配与环境修复（如自动安装工具、注入 MCP 配置、构建图谱索引）；
- 结构化诊断日志记录至 `.bugx/prep-record.md`；
- 仅在物理环境不可抗力导致自愈失败时，才记录降级原因并启用内置安全回退模式。

## 5. 完成判定与下一步跃迁
执行成功后，检查 `.bugx/startup-todo.md`：
- 若所有门禁标记为 `[x]`，会话状态跃迁为 `running`；
- 紧接着装载 **阶段 1：`core/prompts/stages/stage_explore.md`** 开始全域覆盖勘察。
