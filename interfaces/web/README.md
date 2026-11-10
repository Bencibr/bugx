# BugX Interfaces: Web 可视化控制台与报告系统

本目录为 BugX 的 **统一可视化交付界面与交互中心**，承载直接面向人类工程师与安全审计员的交互控制台、HTML 漏洞报告工作台与双向图谱洞察。

## 1. 领域职责与定位

依据 `DOC-257`（六大正交领域收敛架构）与 `DOC-256`（微内核解耦规范）：

- **微内核解耦原则**：底层漏洞挖掘调度、数据契约模型、SQLite FTS5 引擎由 `core/report_engine/` 与 `core/webui/` 提供纯粹后端支撑；
- **交互交付中心**：本目录作为面向人类交互的物理聚焦点，集中呈现：
  1. **实时挖掘工作台 (Live Mining Workbench)**：任务 DAG 状态推进、节点执行监控、Worker 心跳与状态机；
  2. **双轨证据链与报告系统 (Dual-Track Report System)**：CVE 级别漏洞证明材料、反向复现 PoC、调用栈与修复补丁比对；
  3. **架构拓扑与攻击面图谱 (Attack Graph Visualizer)**：组件依赖拓扑、污染传播路径与交互决策点；
  4. **文档治理与知识检索 (Docs Governance & Search)**：Diátaxis 架构规范检索与自解释知识库。

## 2. 常用启动命令

- **启动可视化工作台**：
  ```bash
  bugx report serve --port 18991
  ```
- **生成独立离线 HTML 报告**：
  ```bash
  bugx report both --eval-dir verification/evals/oss --out verification/evals/oss
  ```
