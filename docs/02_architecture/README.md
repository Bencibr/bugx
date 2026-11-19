# Architecture：架构与设计规格

> **定位**：读者要**理解系统**的文档——分层拓扑、设计契约、流程时序、命名与治理规约。
> **上级索引**：[docs/README.md](../../docs/README.md) ｜ **规范**：[02_architecture/07_DOC_GOVERNANCE_STANDARD.md](07_DOC_GOVERNANCE_STANDARD.md)
> **读者**：架构师、核心开发者、需要理解内部机制的高级用户。

## 内容

| 文件 | 定位 | 规模 |
|---|---|---|
| [`01_ARCHITECTURE.md`](01_ARCHITECTURE.md) `01_ARCHITECTURE.md` | 微内核 7 层分层拓扑、33 个子系统、全生命周期时序 | 3.8k 行 |
| [`02_DESIGN_SPEC.md`](02_DESIGN_SPEC.md) `02_DESIGN_SPEC.md` | 内部规格演进、漏洞审定修复契约、单测断言总账本 | 5.7k 行 |
| [`03_BUGX_AUDIT_FLOW.md`](03_BUGX_AUDIT_FLOW.md) `03_BUGX_AUDIT_FLOW.md` | Pre / Run / Post / Settle 全周期对抗博弈时序 | 1.2k 行 |
| [`04_REPOSITORY_TOPOLOGY.md`](04_REPOSITORY_TOPOLOGY.md) `04_REPOSITORY_TOPOLOGY.md` | 六大正交领域、Mermaid 拓扑、三级目录树、根层白名单 | 181 行 |
| [`05_ARCH_NOMENCLATURE_GUIDE.md`](05_ARCH_NOMENCLATURE_GUIDE.md) `05_ARCH_NOMENCLATURE_GUIDE.md` | 代码与测试模块命名、7 层单向依赖矩阵 | 276 行 |
| [`06_MODULE_GOVERNANCE_SPEC.md`](06_MODULE_GOVERNANCE_SPEC.md) `06_MODULE_GOVERNANCE_SPEC.md` | 目录归类、100% 索引守卫、自适应测试调度 | 147 行 |
| [`07_DOC_GOVERNANCE_STANDARD.md`](07_DOC_GOVERNANCE_STANDARD.md) `07_DOC_GOVERNANCE_STANDARD.md` | 全仓文档命名、分区、状态字段、索引与溯源的唯一治理规范（原根层 `03_DOC_GOVERNANCE_STANDARD.md`，2026-11-10 迁入） | 全 docs |

## 阅读顺序建议

1. **新人**：`04_REPOSITORY_TOPOLOGY.md`（先看全貌）→ `01_ARCHITECTURE.md`（再看分层）→ `03_BUGX_AUDIT_FLOW.md`（最后看时序）
2. **改代码前**：`05_ARCH_NOMENCLATURE_GUIDE.md`（命名与依赖方向）+ `06_MODULE_GOVERNANCE_SPEC.md`（目录规约）
3. **查契约细节**：`02_DESIGN_SPEC.md`（最大最全，按子系统检索）

## 维护责任（AGENTS.md 硬约束）

> 每次功能开发完成后，**必须**把最新架构图、流程时序与规格变更同步到本目录，**严禁只在 `08_internal/04_proposals/` 建 DOC-xxx 而放任核心设计文档陈旧**。

- 代码路径引用必须 100% 真实存在（零虚构代码引用）
- 本目录属**公开层**：禁止出现内部工单编号
