<p align="center">
  <img src="https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/bugx-hero-en.svg" alt="BugX — effortless audits of AI-written code" width="1000">
</p>

# BugX — Autonomous Code Review & Automated Testing Agent

BugX is an **autonomous code review and automated testing agent** that runs inside AI coding assistants and CLI workflows.

It partitions codebases into deterministic audit units, orchestrates concurrent Workers to explore complex logic branches and edge cases, and deploys isolated verifiers to **execute sandboxed PoCs for real bug confirmation**. It eliminates AI hallucinations, premature "all good" claims, and false alarms, ensuring robust, crash-free software releases.

<p align="center">
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/pypi/v/bugx-cli.svg?style=flat&color=0284c7&label=PyPI%20Version" alt="PyPI Version"></a>
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/pypi/pyversions/bugx-cli.svg?style=flat&color=334155&label=Python" alt="Python Versions"></a>
  <a href="https://github.com/Bencibr/bugx/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-BSL--1.1-334155.svg?style=flat" alt="License: BSL-1.1"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-334155.svg?style=flat" alt="Platform">
</p>

🌐 Website · 官网：**https://bugx.dev** · GitHub：**https://github.com/Bencibr/bugx**

<p align="center">
  <a href="#-english-documentation">English</a> •
  <a href="#简体中文说明">简体中文</a>
</p>

---

## English Documentation

### ✨ Key Features

- 🛡️ **Real Execution Verification (Zero False Alarms)**: Suspected flaws are verified in an isolated sandbox with dual positive tests and a clean negative control. Every finding carries executable PoC commands, exit codes, and output hashes.
- 📋 **Ledger-Tracked State Machine Coverage**: Codebases are decomposed into atomic units (covered / blocked / pending). Audits resume seamlessly across sessions, and completion is determined by ledger state—not conversational claims.
- 🔌 **Universal Host Integration via MCP / ACP**: Seamlessly plugs into Google Antigravity, Claude Code, Cursor, Trae, OpenAI Codex, Kimi Code, MiniMax Code, and ZCode. Fully supports Gemini 4, Claude 5.5, and GPT-4o models.
- 📊 **Built-in Web Studio Dashboard**: Live interactive web interface (`bugx report serve`) visualizing coverage status, call chains, PoC proof artifacts, and mutation test efficacy.

### 🚀 Quick Start

#### 1. Installation

Requires Python `>=3.11`.

```bash
# Standard install:
python -m pip install bugx-cli

# Or with uv (fast resolver):
uv pip install bugx-cli

# Or as an isolated application:
pipx install bugx-cli
```

#### 2. Configure Your AI Host

Run this command once in your user home directory or target project root:

```bash
bugx install
```

> 💡 **Tip**: After installation, **completely restart your host IDE / AI assistant** (Cursor, Claude Code, Antigravity, etc.).

### 💬 How to Use

- **Inside your AI assistant (Recommended)**: Open Cursor, Claude Code, Antigravity, or ZCode, and prompt:
  > "Audit this project"  
  > "Check the authentication service for concurrency issues and exception leaks"
- **Autonomous CLI execution**:
  ```bash
  bugx autonomous --host opencode   # Autonomous audit
  bugx report serve                 # Live web dashboard at http://127.0.0.1:8080
  ```

### 🖥️ BugX Studio Web Dashboard

Run `bugx report serve` to inspect audit topology, evidence chains, and verified findings:

<p align="center">
  <img src="https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/webui-studio.jpg" alt="BugX Studio workbench" width="860">
</p>

### 🔌 Supported AI Coding Hosts

| Host Assistant | Integration Protocol | Setup Command |
| :--- | :--- | :--- |
| **Google Antigravity** | Native Agent/Worker, Skill, Project MCP | `bugx install --agent antigravity` |
| **Claude Code** | Native MCP / Autonomous Lifecycle | `bugx install --agent claude` |
| **OpenAI Codex** | Workspace Sub-Agent / Native Plugin | `bugx install --agent codex` |
| **OpenCode** | Native ACP / Multi-Agent Concurrency / Project MCP | `bugx install --agent opencode` |
| **Cline / Roo Code** | Isolated Data Sandbox / Project Agent / User MCP | `bugx install --agent cline` |
| **Qoder AI** | Native Agent/Worker, Subagent Concurrency, User/Project MCP | `bugx install --agent qoder` |
| **Trae / Trae CN** | Project Agent, Multi-variant MCP Sync Mirror | `bugx install --agent trae` |
| **DeepSeek Harness (DSH)** | Native Headless Profile / Cordis Patch / MCP Injection | `bugx install --agent dsh` |
| **Kimi Code** | Headless ACP / Native MCP Plugin | `bugx install --agent kimi` |
| **MiniMax Code** | Custom Agent, Plugin V1 Worker, Skill & MCP | `bugx install --agent minimax` |
| **ZCode** | Native MCP / User-Level Registry | `bugx install --agent zcode` |
| **Agnes Code** | Terminal Native TUI / Agent MCP Injection | `bugx install --agent agnescode` |
| **WorkBuddy / CodeBuddy** | Native Skill, Desktop Multi-variant Mirror, Project MCP | `bugx install --agent workbuddy` |
| **GitHub Copilot** | Project Agent/Instructions, User Agent, Project MCP | `bugx install --agent copilot` |
| **Cursor / Windsurf** | Standard User/Project MCP Integration | `bugx install` (auto-detected) |

### 🔍 Keywords

Google Antigravity · Gemini 4 · Gemini 4 Carbon · Gemini 4 Neon · Gemini Code Assist · G4 Model · Claude 5.5 · Claude Sonnet 5.5 · Claude Opus 5.5 · Claude Code Audit · GPT-4o Code Review · Autonomous Code Review · Automated Code Audit · AI Code Review · Code Review Agent · AI Agent · Vibe Coding Security · PR Review · Zero False Positive · PoC Verification · Blind Verification · Security Audit · Vulnerability Scanner · Static Analysis · MCP Server · MCP Security · Cursor · Windsurf · Trae · OpenAI Codex · OpenCode · Cline · Qoder · GitHub Copilot · DeepSeek Harness · Behavior Diff · Memory Leak Detection · Python Security · JavaScript Security · DevSecOps

---

## 简体中文说明

BugX 是一款装在 AI 编程助手与终端里的**自主代码审查与自动化测试智能体（Autonomous Agent）**。

它通过确定性任务账本将待审项目拆解为独立单元，由并发 Worker 深入代码逻辑、边界分支与并发场景，并通过隔离的验证员**在沙箱中真实执行 PoC 验证漏洞**。彻底解决大模型“口头宣布查完、遇到长代码爱偷懒跳过、凭空脑补假漏洞”的问题，让代码交付透明、可查、上线不翻车。

### ✨ 核心特性

- 🛡️ **真实执行验证（零误报承诺）**：疑似缺陷由独立沙箱执行“双正例触发 + 单负对照零触发”实跑验证，带完整命令、退出码与终端回显证据留案，杜绝凭空臆测与假报警。
- 📋 **状态机覆盖账本（杜绝偷懒跳过）**：以状态机跟踪全项目审计单元（已覆盖 / 阻塞 / 待审），断点可续，审查完成与否以账本终态为准，模型的口头承诺不算数。
- 🔌 **全生态宿主即插即用**：基于标准 MCP / ACP 协议，原生适配 Google Antigravity、Claude Code、Cursor、Trae、OpenAI Codex、OpenCode、Cline、Qoder、Kimi、MiniMax、ZCode 等，深度支持 Gemini 4、Claude 5.5 与 GPT-4o 底座。
- 📊 **自带全息 Web 工作台**：内置 Studio 交互看板，实时追踪审计拓扑、漏洞调用链、复现凭据与测试变异杀伤力。

### 🚀 快速开始

#### 1. 安装 (Python >= 3.11)

```bash
pip install bugx-cli
# 或使用 uv:
uv pip install bugx-cli
```

#### 2. 一键配置 AI 宿主

在用户主目录或目标仓库下运行，自动识别并配置本机已有的 AI 编程助手：

```bash
bugx install
```

> 💡 **提示**：安装完成后请**完全退出并重启你的 AI 编程助手/IDE**。

### 💬 使用方式

- **方式一：在 AI 助手里直接对话（推荐）**：打开 Cursor、Claude Code、Antigravity、ZCode 等，对它说：
  > “审计这个项目”  
  > “检查登录模块是否存在并发安全漏洞或异常吞噬”
- **方式二：命令行自主长跑**：
  ```bash
  bugx autonomous --host opencode   # 全自动闭环审计
  bugx report serve                 # 启动本地实时 Web 看板 (http://127.0.0.1:8080)
  ```

### 📚 文档与支持

- [用户完整指南](https://github.com/Bencibr/bugx/blob/main/docs/01_guides/01_USER_GUIDE.md)
- [命令手册 (CLI Reference)](https://github.com/Bencibr/bugx/blob/main/docs/01_guides/02_COMMANDS.md)
- [宿主矩阵与分发指南](https://github.com/Bencibr/bugx/blob/main/docs/01_guides/05_HOST_MATRIX_AND_DISPATCH.md)
- [故障排查手册](https://github.com/Bencibr/bugx/blob/main/docs/01_guides/03_TROUBLESHOOTING.md)

### 🔍 搜索关键词 (Keywords)

Google Antigravity · 反重力 · 反重力智能体 · Gemini 4 · Gemini 4 Carbon · Gemini 4 Neon · Gemini · Gemini 编程助手 · Gemini Code Assist · G4 模型 · Claude 5.5 · Claude Sonnet 5.5 · Claude Opus 5.5 · Claude Code 审计 · GPT-4o 代码审计 · 自动代码审计 · AI 代码审计 · 自动化 Code Review · Code Review · 代码审查 Agent · 代码审计智能体 · AI Agent · Vibe Coding 安全 · 氛围编程审计 · PR 自动审查 · 零误报 · 真实 PoC 复现 · 盲审验证 · 安全审计 · 漏洞扫描 · 漏洞挖掘 · 静态分析 · MCP 服务 · MCP 安全 · Cursor 审计 · Windsurf 审计 · Trae 审计 · Codex 审计 · OpenCode 审计 · Cline 审计 · Qoder 审计 · DeepSeek Harness 审计 · 编程助手安全 · 自主审计 · 行为差分 · 内存泄漏检测 · Python 安全 · JavaScript 安全 · DevSecOps

### 📄 License

Licensed under the [Business Source License 1.1](https://github.com/Bencibr/bugx/blob/main/LICENSE).
