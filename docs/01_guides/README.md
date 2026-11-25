---
title: BugX 操作指南与恢复手册
audience: Public users
status: DOCUMENTATION_UNVERIFIED
last-verified: 2026-11-10
---

# Guides：操作指南与恢复手册

> **定位**：读者要**照着做**的文档——安装、使用、命令怎么敲、出错怎么排。
> **上级索引**：[docs/README.md](../../docs/README.md) ｜ **规范**：[02_architecture/07_DOC_GOVERNANCE_STANDARD.md](../02_architecture/07_DOC_GOVERNANCE_STANDARD.md)
> **读者**：终端用户、安全测试工程师、运维。**严禁出现内部工单编号。**

## 内容

| 文件 | 定位 |
|---|---|
| [`01_USER_GUIDE.md`](01_USER_GUIDE.md) `01_USER_GUIDE.md` | 安装、四层 Host 能力矩阵、审计任务配置、生命周期、报告解读与恢复 |
| [`02_COMMANDS.md`](02_COMMANDS.md) `02_COMMANDS.md` | 当前 CLI 的 43 个顶层命令、分组子命令、参数、副作用与退出码速查 |
| [`03_TROUBLESHOOTING.md`](03_TROUBLESHOOTING.md) `03_TROUBLESHOOTING.md` | 按症状分流的诊断、证据收集、恢复和终止标准 |
| [`04_CLOUD_AUTH_AND_SYNC_COOKBOOK.md`](04_CLOUD_AUTH_AND_SYNC_COOKBOOK.md) `04_CLOUD_AUTH_AND_SYNC_COOKBOOK.md` | 速查卡：云端认证、`cloud sync` 与 `bugx-docs` 官档防污染热同步 |
| [`05_HOST_MATRIX_AND_DISPATCH.md`](05_HOST_MATRIX_AND_DISPATCH.md) `05_HOST_MATRIX_AND_DISPATCH.md` | 速查卡：11 家 AI 宿主接入形态、`--docs-transport` 与 Worker 派发 |
| [`06_PROBES_AND_POC_COOKBOOK.md`](06_PROBES_AND_POC_COOKBOOK.md) `06_PROBES_AND_POC_COOKBOOK.md` | 速查卡：单点复现、PoC、图谱工具与 UI 审计命令矩阵 |
| [`07_LIFECYCLE_AND_RECOVERY_STATES.md`](07_LIFECYCLE_AND_RECOVERY_STATES.md) `07_LIFECYCLE_AND_RECOVERY_STATES.md` | 速查卡：生命周期终态、`UNINITIALIZED`/`FATAL_BLOCKED` 恢复与租约边界 |
| [`08_LESSONS_AND_PRIVACY_BOUNDARY.md`](08_LESSONS_AND_PRIVACY_BOUNDARY.md) `08_LESSONS_AND_PRIVACY_BOUNDARY.md` | 速查卡：经验集、`bugx-docs`、数据集正交边界与脱敏隐私红线 |

## 什么进这里

| 进 | 不进 |
|---|---|
| 分步骤的操作流程 | 系统设计与架构原理（→ `../02_architecture/`） |
| 面向使用者的参数说明 | 内部实现契约与单测断言（→ `../08_internal/`） |
| 常见错误的现象与处置 | 未发布的规划（禁止外泄） |

## 推荐阅读顺序

新用户按以下顺序阅读：

1. [完整用户指南](01_USER_GUIDE.md)
2. [命令参考](02_COMMANDS.md)
3. [故障排查与恢复手册](03_TROUBLESHOOTING.md)

AI 宿主和自动化系统还应阅读 [AI Reference](../03_ai/01_AI_REFERENCE.md) 与 [AI 使用指南](../03_ai/02_AI_USAGE.md)。用户指南说明“人如何完成任务”，AI Reference 说明“宿主如何遵守生命周期和证据契约”，两者不互相替代。

## 维护责任

- `02_COMMANDS.md` 的命令树以 `interfaces/cli/bugx_cli/cli.py` 的 `build_parser` 和 engine 转发器为事实来源；增删命令必须同步更新。
- 任何 CLI 行为变更，同一次提交内更新本目录，并在交付报告中说明尚未执行的真实环境验证。
