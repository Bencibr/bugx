<p align="center">
  <img src="https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/bugx-hero-en.svg" alt="BugX — effortless audits of AI-written code" width="1000">
</p>

# BugX — Autonomous Code Review & Automated Testing Agent

BugX is an **autonomous code review and automated testing agent** that runs inside AI coding assistants and CLI workflows.

It partitions codebases into deterministic audit units, orchestrates concurrent Workers to explore complex logic branches and edge cases, and deploys isolated verifiers to **execute sandboxed PoCs for real bug confirmation**. It eliminates AI hallucinations, premature "all good" claims, and false alarms, ensuring robust, crash-free software releases.

<p align="center">
  <a href="https://www.npmjs.com/package/bugx-cli"><img src="https://img.shields.io/npm/v/bugx-cli.svg?style=flat&color=0284c7&label=npm%20version" alt="npm version"></a>
  <a href="https://nodejs.org/"><img src="https://img.shields.io/badge/node-%3E%3D18.0.0-334155.svg?style=flat" alt="Node.js"></a>
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

Requires Node.js `>=18`. Python `>=3.11` is detected and managed automatically by the wrapper.

```bash
# Global install (choose your preferred package manager):
npm install -g bugx-cli
# pnpm add -g bugx-cli
# yarn global add bugx-cli
# bun add -g bugx-cli

# Or check version directly via npx:
npx bugx-cli --version
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
| **DeepSeek Harness (DSH)** | Official marketplace plugin with built-in Skills, CLI & MCP ([Official Marketplace](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin)) | `bugx install --agent dsh` |
| **Kimi Code** | Official marketplace plugin with BugX Agent & MCP ([Official Marketplace](https://github.com/MoonshotAI/kimi-code)) | `bugx install --agent kimi` |
| **MiniMax Code** | Official marketplace plugin with Agent, Skills & MCP ([Official Marketplace](https://github.com/MiniMax-AI/MiniMax-Code-Plugins)) | `bugx install --agent minimax` |
| **ZCode** | Official marketplace plugin with BugX Agent, Skills & MCP ([Official Marketplace](https://github.com/zai-org/zcode-plugins)) | `bugx install --agent zcode` |
| **Agnes Code** | Official marketplace plugin for terminal BugX reviews ([Official Marketplace](https://github.com/AgnesAI-Labs/agh-plugin-market)) | `bugx install --agent agnescode` |
| **WorkBuddy / CodeBuddy** | Native Skill, Desktop Multi-variant Mirror, Project MCP | `bugx install --agent workbuddy` |
| **GitHub Copilot** | Project Agent/Instructions, User Agent, Project MCP | `bugx install --agent copilot` |
| **Cursor / Windsurf** | Standard User/Project MCP Integration | `bugx install` (auto-detected) |

### 🔍 Keywords

Google Antigravity · Gemini 4 · Gemini 4 Carbon · Gemini 4 Neon · Gemini Code Assist · G4 Model · Claude 5.5 · Claude Sonnet 5.5 · Claude Opus 5.5 · Claude Code Audit · GPT-4o Code Review · Autonomous Code Review · Automated Code Audit · AI Code Review · Code Review Agent · AI Agent · Vibe Coding Security · PR Review · Zero False Positive · PoC Verification · Blind Verification · Security Audit · Vulnerability Scanner · Static Analysis · MCP Server · MCP Security · Cursor · Windsurf · Trae · OpenAI Codex · OpenCode · Cline · Qoder · GitHub Copilot · DeepSeek Harness · Behavior Diff · Memory Leak Detection · Python Security · JavaScript Security · DevSecOps

---

## 简体中文说明

BugX 可以装在你的 AI 编程工具或命令行里，专门用来**排查代码里的隐藏 Bug、自动补测试并跑通验证**。

它会把项目拆成小块逐一检查，发现可疑问题后不会只靠“嘴说”，而是**真的在本地跑一遍复现脚本或测试，确认能触发才上报**。这样既不容易漏掉长文件里的边界问题，也不会报一堆假漏洞浪费时间。

### ✨ 核心特点

- 🛡️ **真跑验证，少报假问题**：找到可疑 Bug 后会实际运行复现脚本或测试用例，附上执行的命令、退出码和输出日志，有据可查。
- 📋 **进度可追踪，中断可续**：按模块和文件记录已查/未查进度，长项目不怕查一半漏掉，随时可以接着跑。
- 🔌 **主流 AI 编程工具开箱即用**：一条命令自动适配 Antigravity、Claude Code、Cursor、Trae、Codex、OpenCode、Cline、Qoder、DSH、Kimi、MiniMax、ZCode、Agnes Code 等。
- 📊 **本地 Web 报告看板**：提供可视化页面查看漏洞详情、调用路径、复现记录与测试结果。

### 🚀 快速开始

#### 1. 安装 (Node.js >= 18)

```bash
npm install -g bugx-cli
# 或
npx bugx-cli --version
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
