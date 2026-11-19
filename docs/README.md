---
title: BugX 文档中心
audience: Public users and maintainers
status: DOCUMENTATION_UNVERIFIED
last-verified: 2026-11-10
---

# BugX 文档中心（Documentation Hub）

> **版本**：0.1.0 ｜ **体系**：按读者分区（Diátaxis + 三受众隔离）
> **规范**：[02_architecture/07_DOC_GOVERNANCE_STANDARD.md](02_architecture/07_DOC_GOVERNANCE_STANDARD.md) — 本目录的命名、分区、状态与维护规则以该文件为唯一权威。

---

## 目录结构（按读者找，不按文件名猜）

```text
docs/
├── README.md / README_EN.md      ← 你在这里
├── 02_architecture/07_DOC_GOVERNANCE_STANDARD.md    ← 文档规范（唯一权威）
│
├── 01_guides/            【操作用】我要照着做
├── 02_architecture/      【理解用】我要看懂系统
├── 03_ai/                【给 AI】宿主运行时上下文
├── 04_reference/         【查值用】速查
├── 05_release-notes/     【追溯用】微版本流水账本（自动生成）
├── 06_diagrams/          【资源】图表文件
├── 07_distribution/      【发布用】分发与打包说明
└── 08_internal/          【内部】仅存运行期必需资产（含工单号，禁止外传）
```

**序号即阅读顺序**。插入新分类时整体重排，不留空号；`README.md` 不加序号（工具按固定名定位）。

---

## 1. 操作指南（`01_guides/`）— 我要照着做

| 文档 | 内容 |
|---|---|
| [01_USER_GUIDE.md](01_guides/01_USER_GUIDE.md) `01_guides/01_USER_GUIDE.md` | 安装、四层 Host 能力矩阵、审计配置、生命周期、报告解读与恢复 |
| [02_COMMANDS.md](01_guides/02_COMMANDS.md) `01_guides/02_COMMANDS.md` | 当前 CLI 的 42 个顶层命令、分组子命令、参数、副作用与退出码 |
| [03_TROUBLESHOOTING.md](01_guides/03_TROUBLESHOOTING.md) `01_guides/03_TROUBLESHOOTING.md` | 按症状分流的诊断、证据收集、恢复和终止标准 |
| [目录说明](01_guides/README.md) | 该目录索引与维护责任 |

## 2. 架构与设计（`02_architecture/`）— 我要看懂系统

| 文档 | 内容 | 建议读序 |
|---|---|---|
| [04_REPOSITORY_TOPOLOGY.md](02_architecture/04_REPOSITORY_TOPOLOGY.md) `02_architecture/04_REPOSITORY_TOPOLOGY.md` | 六大正交领域：目录树与根层白名单 | **① 先看全貌** |
| [01_ARCHITECTURE.md](02_architecture/01_ARCHITECTURE.md) `02_architecture/01_ARCHITECTURE.md` | 微内核 7 层拓扑、33 个子系统、生命周期时序 | ② 再看分层 |
| [03_BUGX_AUDIT_FLOW.md](02_architecture/03_BUGX_AUDIT_FLOW.md) `02_architecture/03_BUGX_AUDIT_FLOW.md` | Pre / Run / Post / Settle 对抗博弈时序 | ③ 最后看流程 |
| [02_DESIGN_SPEC.md](02_architecture/02_DESIGN_SPEC.md) `02_architecture/02_DESIGN_SPEC.md` | 内部规格演进、修复契约、单测断言总账本 | 查细节时按子系统检索 |
| [05_ARCH_NOMENCLATURE_GUIDE.md](02_architecture/05_ARCH_NOMENCLATURE_GUIDE.md) `02_architecture/05_ARCH_NOMENCLATURE_GUIDE.md` | 命名规范、7 层单向依赖矩阵 | 改代码前 |
| [06_MODULE_GOVERNANCE_SPEC.md](02_architecture/06_MODULE_GOVERNANCE_SPEC.md) `02_architecture/06_MODULE_GOVERNANCE_SPEC.md` | 目录归类、索引守卫、测试调度 | 加文件前 |
| [目录说明](02_architecture/README.md) | 该目录索引与维护责任 |

## 3. AI 宿主上下文（`03_ai/`）— 给 Agent 读

| 文档 | 消费方式 |
|---|---|
| [01_AI_REFERENCE.md](03_ai/01_AI_REFERENCE.md) `03_ai/01_AI_REFERENCE.md` | 宿主启动时注入：CLI 契约、MCP 协议、状态机、验收门禁 |
| [02_AI_USAGE.md](03_ai/02_AI_USAGE.md) `03_ai/02_AI_USAGE.md` | 集成示例，按需查阅 |
| [03_AI_COMMANDS.md](03_ai/03_AI_COMMANDS.md) `03_ai/03_AI_COMMANDS.md` | 命令选路手册 |
| [目录说明](03_ai/README.md) | 该目录索引与写作约束 |

## 4. 参考（`04_reference/`）

| 文档 | 内容 |
|---|---|
| [CHANGELOG.md](../CHANGELOG.md) `CHANGELOG.md` | 面向外部使用者的语义版本变更汇总 |
| [环境变量参考](04_reference/01_ENVIRONMENT_VARIABLES.md) `04_reference/01_ENVIRONMENT_VARIABLES.md` | 环境变量的设置方式、作用域、敏感边界和源码消费清单 |
| [目录说明](04_reference/README.md) | 双轨制说明 |

---

## 5. 内部研发资产（`08_internal/`）— 禁止外传

含内部工单编号，**严禁出现在公开分发物中**。

内部目录不属于公开分发物，当前包括标准、发布和文档证据矩阵：

```text
docs/08_internal/
├── 01_standards/       测试设计规范、宿主迁移清单、L1 快循环、契约清单
├── 02_release/         发布指南和内部审计留档
└── 03_documentation/   产品文档覆盖矩阵和源码证据边界
```

**架构真值以 [02_architecture/](02_architecture/README.md) 为准**；内部文档只记录研发操作规范、发布约束和本次文档审计证据。

详见 [08_internal/README.md](08_internal/README.md)。

---

## 我要做 X，该看哪个？

| 我想…… | 去这里 |
|---|---|
| 装上并跑起来 | [01_guides/01_USER_GUIDE.md](01_guides/01_USER_GUIDE.md) |
| 查某个命令怎么用 | [01_guides/02_COMMANDS.md](01_guides/02_COMMANDS.md) |
| 理解系统怎么分层 | [02_architecture/01_ARCHITECTURE.md](02_architecture/01_ARCHITECTURE.md) |
| 搞清审计流程怎么走 | [02_architecture/03_BUGX_AUDIT_FLOW.md](02_architecture/03_BUGX_AUDIT_FLOW.md) |
| 提一个新需求 | [02_architecture/02_DESIGN_SPEC.md](02_architecture/02_DESIGN_SPEC.md) 对应章节 |
| 写一份技术方案 | `python3 scripts/dev_doc.py plan --title "..."`（方案产出到 08_internal/）|
| 记录一个 bug 的原因与修复 | git 提交说明 + [CHANGELOG.md](../CHANGELOG.md) |
| 看要做什么还没做 | `python3 scripts/dev_doc.py status`（无独立 TODO 文件）|
| 查历史提案 | git 历史（`git log --all -- docs/08_internal/04_proposals`） |
| 改代码前看规范 | [08_internal/01_standards/](08_internal/01_standards/README.md) |
| 发版 | [05_release-notes/](05_release-notes/README.md) |
| 知道文档该怎么写 | [02_architecture/07_DOC_GOVERNANCE_STANDARD.md](02_architecture/07_DOC_GOVERNANCE_STANDARD.md) |

---

## 维护规则（摘要）

1. **每个目录必须有 README**，且与实际文件双向一致 —— 由 `scripts/verify_directory_indices.py --check` 强制。
2. **新增/删除/改名必须在同一次提交内同步 README** —— 否则提交被拒。
3. **公开层禁止内部工单编号** —— 由 `docskill status` 检查。
4. **最大深度 3 层** —— 超过说明分类划分错了。

完整规则见 [02_architecture/07_DOC_GOVERNANCE_STANDARD.md](02_architecture/07_DOC_GOVERNANCE_STANDARD.md)。
