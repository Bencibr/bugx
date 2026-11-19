# Diagrams：图表资源

> **定位**：被其他文档引用的**图表文件**，不是供人通读的文档。
> **上级索引**：[docs/README.md](../../docs/README.md) ｜ **规范**：[02_architecture/07_DOC_GOVERNANCE_STANDARD.md](../02_architecture/07_DOC_GOVERNANCE_STANDARD.md)

## 内容

| 文件 | 用途 |
|---|---|
| `01_bugx-architecture.mmd` | Mermaid 源文件（**唯一真理来源**） |
| `01_bugx-architecture.svg` | 从 `.mmd` 导出的矢量图 |
| `01_bugx-architecture.png` | 从 `.mmd` 导出的位图 |
| `01_bugx-architecture.excalidraw` | 可编辑源文件（人工调整用） |
| `02_bugx-user-journey.mmd` | 从安装到审计归档的用户旅程流程图 |
| `02_bugx-user-journey.svg` | 用户旅程流程图矢量导出物 |
| `02_bugx-user-journey.png` | 用户旅程流程图位图导出物 |
| `02_bugx-user-journey.excalidraw` | 用户旅程流程图可编辑导出物 |
| `03_bugx-lifecycle-evidence.mmd` | 生命周期阶段与证据回执时序图 |
| `03_bugx-lifecycle-evidence.svg` | 生命周期与证据闭环矢量导出物 |
| `03_bugx-lifecycle-evidence.png` | 生命周期与证据闭环位图导出物 |
| `03_bugx-lifecycle-evidence.excalidraw` | 生命周期与证据闭环可编辑导出物 |
| `04_bugx-troubleshooting-router.mmd` | 安装、项目、宿主与运行态故障路由图 |
| `04_bugx-troubleshooting-router.svg` | 故障排查路由矢量导出物 |
| `04_bugx-troubleshooting-router.png` | 故障排查路由位图导出物 |
| `04_bugx-troubleshooting-router.excalidraw` | 故障排查路由可编辑导出物 |

## 维护规则

- **改图先改 `.mmd`**，再重新导出 `.svg`/`.png`。直接改导出物会导致源与产物失同步。
- 引用图表时用相对路径，确保 `docskill` 死链检查能覆盖。
- 本目录**不设逐文件索引豁免**：条目少，逐文件列出即可。
