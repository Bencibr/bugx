# Reference：参考型资料

> **定位**：读者要**查一个值**的文档——版本变更、兼容性、速查表。
> **上级索引**：[docs/README.md](../../docs/README.md) ｜ **规范**：[02_architecture/07_DOC_GOVERNANCE_STANDARD.md](../02_architecture/07_DOC_GOVERNANCE_STANDARD.md)

## 内容

| 文件 | 定位 |
|---|---|
| [`01_ENVIRONMENT_VARIABLES.md`](01_ENVIRONMENT_VARIABLES.md) | **全部 `BUGX_*` 环境变量速查**：141 个直接消费变量，按 7 类分组，含默认值与主消费点（从代码真值提取） |
| [`design-system/`](design-system/README.md) | **大厂设计规范基准**：9 个品牌 DESIGN.md（Apple/Airbnb/Vercel/Stripe 等）+ 外部规范链接——BugX UI 设计质量审计的对照基准 |
| [`UI_DESIGN_DEFECT_REPORT.md`](UI_DESIGN_DEFECT_REPORT.md) | **首份 UI 设计缺陷报告**：报告引擎 Formal/Visual Delivery 双主题实测审计（Playwright 截图 + 像素分析），5 项改进建议与 F-023 CSS 架构立案 |
| 见[根目录 CHANGELOG.md](../../CHANGELOG.md) | 面向外部使用者的语义版本大颗粒度变更汇总（按 AGENTS.md 必须置于仓库根目录） |

## 双轨制说明（AGENTS.md 硬约束）

| 轨道 | 位置 | 读者 | 粒度 |
|---|---|---|---|
| **语义版本汇总** | **[根目录 `CHANGELOG.md`](../../CHANGELOG.md)** | 外部使用者 | 大颗粒度，按 `X.Y.Z` |
| **微版本流水账本** | [`../05_release-notes/`](../../docs/05_release-notes/README.md) | 内部 CI 与追溯 | 每次提交一篇 |

> **禁止混合**：不得把微版本流水直接丢进 `CHANGELOG.md`，也不得缺失 `CHANGELOG.md`。
