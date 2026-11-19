---
title: BugX AI 宿主运行时上下文
audience: AI hosts and native agents
status: DOCUMENTATION_UNVERIFIED
last-verified: 2026-11-10
---

# AI：宿主运行时上下文

> **定位**：给 AI Agent 读的文档——CLI 契约、状态机模型、协议字段、错误码。**追求 Token 能效，不追求可读性。**
> **上级索引**：[docs/README.md](../../docs/README.md) ｜ **规范**：[02_architecture/07_DOC_GOVERNANCE_STANDARD.md](../02_architecture/07_DOC_GOVERNANCE_STANDARD.md)
> **读者**：接入 BugX 的 AI 宿主（Claude Code / Codex / OpenCode / Cline / DSH 等）。

## 内容

| 文件 | 定位 | 消费方式 |
|---|---|---|
| [`01_AI_REFERENCE.md`](01_AI_REFERENCE.md) `01_AI_REFERENCE.md` | 机器可读契约：CLI、MCP、Host 集合、生命周期、状态机、receipt、恢复 | 宿主启动时注入 |
| [`02_AI_USAGE.md`](02_AI_USAGE.md) `02_AI_USAGE.md` | 使用方式与集成示例 | 按需查阅 |
| [`03_AI_COMMANDS.md`](03_AI_COMMANDS.md) `03_AI_COMMANDS.md` | 命令选路与调用手册 | 按需查阅 |

## 写作约束（与其他目录不同）

| 要求 | 说明 |
|---|---|
| **信息密度优先** | 删掉一切修辞与过渡句；能表格不段落 |
| **契约精确** | 参数名、错误码、状态值必须与源码逐字一致 |
| **禁止冗长解释** | "为什么"放 `../02_architecture/`，这里只写"是什么/怎么调" |

> 本目录**属公开层**但受众是机器：仍需遵守零内部工单编号，但对可读性要求低于 `01_guides/`。
