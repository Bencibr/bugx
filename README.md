---
audience: 开发者、安全工程师与 AI 编程宿主
status: DOCUMENTATION_UNVERIFIED
version: 0.1.0
last-verified: 2026-11-10
website: https://bugx.dev
description: "BugX 是面向开发者和 AI 编程宿主的本地优先、证据驱动 AI 代码审计与漏洞挖掘 CLI，支持 MCP/ACP 宿主集成、缺陷复现、发现验证和安全报告。"
keywords: "AI 代码审计, 应用安全, 代码安全审查, 漏洞挖掘, 漏洞检测, 缺陷复现, MCP, ACP, Claude Code, OpenAI Codex, OpenCode, Cline, ZCode, Python CLI, Node.js CLI, npm, PyPI"
---

<p align="center">
  <a href="https://bugx.dev">
    <img src="core/assets/bugx-hero-cn.png" alt="BugX — 代码审计智能体、全流程真实证据闭环、杜绝海量扫描误报" width="1200">
  </a>
</p>

<p align="center">
  <strong>简体中文</strong> · <a href="README.en.md">English</a> · <a href="https://bugx.dev">项目官网</a>
</p>

<p align="center">
  <a href="https://bugx.dev"><img alt="BugX 项目官网" src="https://img.shields.io/badge/Website-bugx.dev-38bdf8?style=for-the-badge"></a>
  <a href="docs/01_guides/01_USER_GUIDE.md"><img alt="宿主适配器" src="https://img.shields.io/badge/Host%20adapters-12%20integrations-0f172a?style=for-the-badge"></a>
  <a href="docs/01_guides/01_USER_GUIDE.md"><img alt="ACP 就绪宿主" src="https://img.shields.io/badge/ACP%20ready-Cline%20%7C%20OpenCode-06b6d4?style=for-the-badge"></a>
  <a href="docs/01_guides/01_USER_GUIDE.md"><img alt="自治 CLI 宿主" src="https://img.shields.io/badge/Autonomous%20CLI-Claude%20%7C%20Codex%20%7C%20OpenCode%20%7C%20DSH%20%7C%20Cline%20%7C%20ZCode-2563eb?style=for-the-badge"></a>
</p>

# BugX

> **搜索摘要** —— BugX 是面向开发者和 AI 编程宿主的本地优先、证据驱动 AI 代码审计与漏洞挖掘 CLI。它可连接 Claude Code、OpenAI Codex、OpenCode、Cline、ZCode 等 MCP/ACP 宿主，检查真实源代码、复现缺陷、验证发现，并生成有证据支撑的安全报告。

> **项目官网：** [bugx.dev](https://bugx.dev)

BugX 是一个面向真实软件项目的、源码可见且证据驱动的安全工具。它把 AI 辅助代码审查、应用安全测试、漏洞挖掘和已验证缺陷复现放进同一套本地优先工作流。

BugX 不会把模型回答、`ready` 状态或零退出码当作审计完成的证明。它把项目状态保存到私有的 `.bugx/` 目录，通过本地 Supervisor 协调受支持的 AI 宿主与 Worker，并记录覆盖率、生命周期 receipt、Finding 决策、执行工件和恢复状态。

简单说：BugX 帮助开发者发现深层 Bug 和安全缺陷，复现问题，并把真实验证证据与模型文本区分开。

## 为什么使用 BugX？

- **证据优先的审计：** 跟踪覆盖率、执行 receipt、Finding 决策、工件哈希和完成门禁状态。
- **AI 宿主集成：** 把支持 MCP/ACP 的编程宿主接入结构化审计生命周期。
- **可复现结果：** 将项目状态、报告、缺陷复现工件和恢复信息统一保存。
- **本地优先控制：** 云端上传、诊断、反馈和同步都必须显式执行。

本仓库当前语义版本为 `0.1.0`。文档与当前源码树保持同步，但真实宿主 ACP、自治端到端运行、远端云同步和跨平台安装仍需要在对应环境中单独验证。

## Host Banner 与能力矩阵

BugX 可以集成多个 AI 编程宿主，但宿主能力是分开计算的。“可安装/可检测”不等于已经有 ready engine plugin，也不等于支持 ACP 或自治审计。

| 能力 | 当前宿主 |
| --- | --- |
| 安装与检测适配器 | ChatGPT、Claude Code、Cline、OpenAI Codex、DSH、FunIDE、OpenCode、Pi、Qoder、Trea、WorkBuddy、ZCode |
| Ready engine plugin | OpenCode、OpenAI Codex、Cline、DSH、Claude Code、Pi、ZCode |
| ACP 已就绪 | Cline、OpenCode |
| 自治 CLI 选项 | Claude Code、OpenAI Codex、OpenCode、DSH、Cline、ZCode |

请以当前机器检测结果为准：

`bugx hosts --root . --json`

完整说明见：[Host、MCP 和 ACP 能力矩阵](docs/01_guides/01_USER_GUIDE.md#5-hostmcp-和-acp-能力矩阵)。

## 环境要求与安装

- Python `>=3.11`：Python 包和 CLI 需要。
- Node.js `>=18`：使用 npm wrapper 时需要。

从 npm 或 PyPI 安装：

```bash
python -m pip install bugx-cli
# 或
npm install -g bugx-cli
```

部分能力提供可选 Python extras：`tui`、`graph` 和 `crypto`。基础包不会隐式安装这些可选依赖。

## 安全的首次运行

在目标项目根目录先初始化本地状态，再只安装你打算使用的宿主：

```bash
bugx init --root . --cloud-sync local
bugx install --root . --agent opencode
bugx doctor --root . --strict
bugx engine --root . status
```

`--agent` 必须使用当前安装/检测适配器中的名称。使用 `bugx hosts` 查看本机宿主矩阵。在非交互 shell 中，`bugx init` 需要明确指定 `--cloud-sync local|sync`，除非 preset 已经提供选择。

宿主能力按集成类别区分；请以上方矩阵和本机的 `bugx hosts` 输出为准，不要从“可安装”推断 ACP 或自治能力。

## 执行审计

公开 engine 入口位于 `engine` 命名空间下：

```bash
bugx engine --root . pre
bugx engine --root . status --json
bugx engine --root . post
```

按目标执行：

```bash
bugx autonomous --root . --host opencode
bugx goal --root . --host opencode "audit the project"
```

退出码或模型响应本身不代表审计完成。只有当生命周期、覆盖率、证据 receipt、Finding 决策和完成门禁一致时，才能交付审计结果。`bugx audit` 可能只启动一个等待原生 Agent 的图流程；`ready` 不等于 `complete`。

## BugX 会记录什么？

每次审计都会保留理解过程和复现结果所需的证据：

- 项目范围与覆盖率；
- 生命周期、Worker 和 Host receipt；
- 命令、退出码、工件路径与 SHA-256 哈希；
- Finding 候选、独立验证以及确认/拒绝决策；
- 报告、checkpoint 与可恢复状态。

这套证据模型让安全审查结果更容易检查、复现和交接。

## 文档

- [用户指南](docs/01_guides/01_USER_GUIDE.md)：安装、配置、宿主能力、生命周期、MCP/ACP、云端范围、结果与恢复。
- [命令参考](docs/01_guides/02_COMMANDS.md)：命令、选项、默认值、作用域、副作用和退出行为。
- [故障排查](docs/01_guides/03_TROUBLESHOOTING.md)：按症状诊断、证据收集和可恢复的下一步。
- [AI 参考](docs/03_ai/01_AI_REFERENCE.md)：面向机器的 MCP、生命周期、状态、证据与失败契约。
- [文档索引](docs/README.md)：架构与文档导航。
- [项目官网](https://bugx.dev)：产品介绍与公开入口。

## 数据与安全边界

项目状态保存在 `.bugx/` 下；全局引擎、凭据、知识库、缓存、遥测和云配置使用 BugX 用户级目录，除非文档化环境变量覆盖。`auth status` 只报告凭据是否存在及其状态，不输出秘密内容。模型名称不是凭据。

云端上传、反馈、诊断日志上报和同步都是显式操作。执行前请检查 `--scope`、授权、敏感数据策略和 detail mode。`reset`、`uninstall`、`purge` 与 `--no-backup` 可能删除数据；请谨慎使用 dry-run 和备份选项。

## 搜索关键词

AI 代码审计、应用安全、代码安全审查、漏洞挖掘、漏洞检测、缺陷复现、AI 安全测试、MCP、ACP、Claude Code、OpenAI Codex、OpenCode、Cline、ZCode、Python CLI、Node.js CLI、npm、PyPI。

## 开发策略

每次代码提交都必须同步更新受影响的文档。版本跃迁必须由人工显式确认，并传入 `--confirm-version-transition`。

## 许可证

许可证条款见 [LICENSE](LICENSE)。
