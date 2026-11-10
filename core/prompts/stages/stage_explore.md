# BugX 阶段 1：全域覆盖勘察 Harness（Stage Explore Protocol）

## 1. 阶段目标与覆盖基准
本阶段的核心任务是遍历被测工程的攻击面，建立完备的单元覆盖清单，并完成白盒 AST 与黑盒基线探测。
**终局目标**：内部最小单元 100% 覆盖，关键业务链路 100% 覆盖。

## 2. 覆盖清单建立与恢复（Coverage Manifest）
1. 检查是否存在既有清单：`.bugx/pollings/<polling-id>/lifecycles/<lifecycle-id>/state/module-coverage.md`
   （不存在时由 `coverage_bootstrap` 生成；文件名恒为 `module-coverage.md`，不存在 `.csv` 形态）。
2. 若不存在或需增量补齐，运行探测工具：
   ```bash
   python3 "$(bugx engine path --name module_coverage.py)" discover --root .
   ```
3. 清单必须覆盖以下三大维度：
   - **函数/类接口单元**：白盒 AST 提取的所有可导出的公有方法与核心私有逻辑。
   - **网络/RPC 路由单元**：HTTP Endpoints、GraphQL Query/Mutation、WebSocket 处理器。
   - **业务链路单元**：跨模块调用图谱（从 Controller 到 Service 再到 Repository/DB）。

## 3. 探针深度调用协议（Probes Invocation）
依据目标工程技术栈，按需调用探针建立全局上下文事实：
- **代码库知识图谱探针**：全局引擎 `codebase_memory_probe.py`
  - 提取架构拓扑、调用边关系与依赖包清单。未就绪或未建索引时立即执行自愈构建（`codebase-memory-mcp cli index_repository`），严禁借故消极降级。
- **SAST 语法树安全探针**：全局引擎 `sast_probe.py`
  - 静态模式匹配 SQL 注入、命令注入、未授权反序列化及硬编码凭证。
- **业务行为图谱探针**：全局引擎 `behavior_graph.py --export .bugx/behavior_graph.json`
  - 提取系统核心状态流转、实体模型与鉴权边界。

## 4. 覆盖状态与优先级排序
- 优先选择**高风险、高频变更、多层调用链路**的单元作为变异测试入口。
- 确认覆盖清单各单元的初始基线状态（PASS / UNCOVERED）。
- 完成后进入 **阶段 2：`core/prompts/stages/stage_mutate.md`**。
