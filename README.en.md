<p align="center">
  <img src="core/assets/bugx-hero-en.svg" alt="BugX — Effortless audits of AI-written code" width="800">
</p>

<p align="center">
  <a href="README.md">中文</a> · English
</p>

# BugX — Automated Bug-Hunting & Testing Agent

BugX runs inside your AI coding tool or terminal to **find hidden bugs, write tests, and verify issues by actually running them**.

Instead of guessing from code alone, it splits the project into manageable parts, checks edge cases, and **runs real reproduction scripts or tests locally before reporting a bug**. That means fewer missed edge cases in large files and far fewer false alarms.

<p align="center">
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/pypi/v/bugx-cli.svg?style=flat&color=0284c7&label=PyPI" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/bugx-cli"><img src="https://img.shields.io/npm/v/bugx-cli.svg?style=flat&color=0284c7&label=npm" alt="npm"></a>
  <img src="https://img.shields.io/badge/python-%E2%89%A53.11-334155.svg?style=flat" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-334155.svg?style=flat" alt="Platform">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-BSL--1.1-334155.svg?style=flat" alt="License"></a>
</p>

🌐 Website: **https://bugx.dev**

---

### ✨ Key Features

- 🛡️ **Verified by Execution, Not Guesswork**: Suspected bugs are reproduced with real commands or tests, recording the exact command, exit code, and output so you don't waste time on false positives.
- 📋 **Trackable & Resumable Progress**: Keeps track of which modules have been checked or skipped, so large codebases can be audited across sessions without losing place.
- 🔌 **Works with Popular AI Coding Tools**: One command sets up Antigravity, Claude Code, Cursor, Trae, Codex, OpenCode, Cline, Qoder, DSH, Kimi Code, MiniMax Code, ZCode, Agnes Code, and more.
- 📊 **Local Web Report Viewer**: Browse findings, call paths, reproduction logs, and test results in a clean local web UI (`bugx report serve`).

---

### 🚀 Quick Start

#### 1. Install CLI (choose either)

```bash
# Python (>= 3.11)
pip install bugx-cli

# or Node.js (>= 18)
npm install -g bugx-cli
```

#### 2. Configure Your AI Host

Run this command once in your user home directory or target project root. BugX automatically detects installed AI coding assistants and configures MCP / plugins:

```bash
bugx install
```

> 💡 **Tip**: After installation, **completely restart your host IDE / AI assistant**. For custom configurations, refer to the [Host Matrix and Dispatch Guide](docs/01_guides/05_HOST_MATRIX_AND_DISPATCH.md).

---

### 💬 How to Use

#### Option 1: Inside Your AI Coding Assistant (Recommended)

Open any configured project in Cursor, Claude Code, Antigravity, or ZCode, and prompt directly:

> "Audit this project"  
> "Check the authentication service for concurrency issues and exception leaks"  
> "Review changes on this branch and provide reproducible PoC verification commands"

The host AI will autonomously orchestrate BugX Workers, execute sandbox probes, and confirm findings with real reproduction.

#### Option 2: Autonomous CLI Execution

```bash
# Autonomous end-to-end audit (reuses credentials from your host assistant)
bugx autonomous --host opencode

# Launch real-time web dashboard (opens http://127.0.0.1:8080)
bugx report serve
```

---

### 🖥️ BugX Studio Web Dashboard

Run `bugx report serve` anytime to inspect audit topology, evidence chains, and verified findings:

<p align="center">
  <img src="core/assets/webui-studio.jpg" alt="BugX Studio Dashboard" width="860">
</p>

---

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

---

### 📚 Documentation

- [User Guide](docs/01_guides/01_USER_GUIDE.md): Complete setup, audit lifecycle, MCP/ACP usage, and recovery.
- [CLI Reference](docs/01_guides/02_COMMANDS.md): Full command index and flag definitions.
- [Host Matrix & Dispatch Guide](docs/01_guides/05_HOST_MATRIX_AND_DISPATCH.md): Host-specific configurations.
- [Troubleshooting](docs/01_guides/03_TROUBLESHOOTING.md): Problem diagnosis and self-healing recipes.
- [Architecture & Design](docs/02_architecture/01_ARCHITECTURE.md): State machine, ledger integrity, and verification engines.

---

### 🔍 Keywords

Google Antigravity · Gemini 4 · Gemini 4 Carbon · Gemini 4 Neon · Gemini Code Assist · G4 Model · Claude 5.5 · Claude Sonnet 5.5 · Claude Opus 5.5 · Claude Code Audit · GPT-4o Code Review · Autonomous Code Review · Automated Code Audit · AI Code Review · Code Review Agent · AI Agent · Vibe Coding Security · PR Review · Zero False Positive · PoC Verification · Blind Verification · Security Audit · Vulnerability Scanner · Static Analysis · MCP Server · MCP Security · Cursor · Windsurf · Trae · OpenAI Codex · OpenCode · Cline · Qoder · GitHub Copilot · DeepSeek Harness · Behavior Diff · Memory Leak Detection · Python Security · JavaScript Security · DevSecOps

---

### 📄 License

Licensed under the [Business Source License 1.1](LICENSE).
