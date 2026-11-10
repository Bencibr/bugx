# BugX 智能体执行协议与交互契约子系统 (Agent Runtime Contracts & Protocols)

> **版本契约**: `0.1.0` | **父系统**: `core/`  
> **公开架构说明**: 参见 [`docs/02_architecture/01_ARCHITECTURE.md`](../../docs/02_architecture/01_ARCHITECTURE.md)。

本文档定义 BugX 运行于各大主流宿主（Claude, Cline, OpenCode, Codex, Pi, Qoder 等）以及并发 Worker 执行时的核心提示词契约、生命周期控制协议与人机交互规范。

---

## 1. 核心契约文件索引 (Contract & Protocol Indices)

| 文件名 | 类型 | 职责说明 |
| :--- | :--- | :--- |
| [`bugx-runtime-contract.md`](bugx-runtime-contract.md) | 核心运行时契约 | 宿主交互、Supervisor 管道、生命周期阶段流转与 Finding 证据格式唯一权威契约 |
| [`bugx-worker.md`](bugx-worker.md) | Worker 执行契约 | 并发 Worker 子智能体单元级执行规范、开工四步、挖掘纪律、立案 Oracle 与 Checkpoint 协议 |
| [`bugx-quick.md`](bugx-quick.md) | L0 快速协议卡 | 极简常驻协议卡片（≤1000 tokens），提供红线禁令与快速上手检索路径 |
| [`bugx-blackbox.md`](bugx-blackbox.md) | 黑盒审计规约 | 黑盒模式运行、外部工具协同、无源码攻防探测与多智能体分工规范 |
| [`bugx-opencode.md`](bugx-opencode.md) | OpenCode 原生协议 | OpenCode 宿主深度集成、沙箱探针纪律、分阶段 Harness 装载与 ACP 协议对接契约 |
| [`AGENT_GUIDE.md`](AGENT_GUIDE.md) | AI 智能体工作流指南 | 面向宿主适配层开发者与 AI 编码智能体的全生命周期操作指引、示例与集成注意事项 |
| [`engine_gap_contracts.py`](engine_gap_contracts.py) | 缺口防御契约门面 | 引擎能力断层与未实现功能前置防御契约，强类型阻断虚假放行与假跑 |

---

## 2. 交互拓扑与寻址规范

所有外部宿主与内部调度器均通过统一寻址接口定位本目录契约：

```bash
bugx engine path --name bugx-runtime-contract.md
bugx engine path --name bugx-worker.md
bugx engine path --name bugx-quick.md
```
`bugx engine path` 会自动在 `contracts/` 子系统中解析并返回物理绝对路径，保障各宿主开箱即用与跨平台稳定执行。
