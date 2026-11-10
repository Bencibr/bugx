# Codex 对话原生 Worker 派发

本协议用于 BugX Skill 在 Codex 对话中因用户明确要求漏洞挖掘/审计而启动时。它只规定宿主 Worker handoff，不跳过 BugX Gate、授权、预算或生命周期协议；CLI 自治入口继续使用各自的启动协议。

1. **能力预检**：在调用会创建 claim 的 `workers` 阶段前，确认当前会话实际提供原生子 Agent 创建和等待/结果收取能力（例如 `collaboration.spawn_agent` 及对应 wait 工具）。缺少任一能力时，不运行 `workers`、不产生 claim，报告 `BLOCKED/native_subagent_unavailable`。
2. **真实派发**：`workers` 返回的每个完整 Task 块创建一个原生子 Agent，并尽可能并发创建。实际调用宿主工具，不得只输出 `Task(...)`。任务块逐字传递，保留 `worker_id`、`unit_ids`、`lease_id`、`execution_id`、`artifact_root`、`provider`；不摘要、截断、改写、合并或重领 claim，不编造身份，并记录 claim 与 Agent 的映射。
3. **等待与核验**：等待所有已创建 Agent；用 Supervisor Host/Worker MCP status 和 checkpoint 核实每个 claim 的 `worker-start`、进度及结算。自然语言“完成”不能代替 checkpoint；不直接读取受保护的 `.bugx/` 状态 JSON。
4. **父级结算**：所有 claim 核验后，由父 Agent 串行裁决 Finding、决定修复、运行定向验证、合并 coverage 并调用 `post`。遵守真实 Worker 并发要求及单可执行单元例外。子 Agent 不裁决 Finding、不合并总 coverage、不调用 `post`，也不并行修改共享工作树源码。
5. **失败恢复**：部分创建失败时保留并核验已创建 Agent 与 claim 映射；对失联或未结算 claim 只通过 Host recovery/fencing 处理，再调用 `workers` 领取新 claim。不得等待已消失的 Agent、复用旧身份或伪造 checkpoint；恢复不可用时报告 `BLOCKED` 或 `RECOVERY_REQUIRED`。
