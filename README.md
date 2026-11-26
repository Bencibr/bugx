<p align="center">
  <img src="core/assets/bugx-hero-zh.svg" alt="BugX — 让你轻松审计 AI 写的代码" width="800">
</p>

<p align="center">
  中文 · <a href="README_EN.md">English</a>
</p>

# BugX — 自主代码审查与智能测试智能体

BugX 是一款装在 AI 编程助手与终端里的**自主代码审查与自动化测试智能体（Autonomous Agent）**。

它通过确定性任务账本将待审项目拆解为独立单元，由并发 Worker 深入代码逻辑、边界分支与并发场景，并通过隔离的验证员**在沙箱中真实执行 PoC 验证漏洞**。彻底解决大模型“口头宣布查完、遇到长代码爱偷懒跳过、凭空脑补假漏洞”的问题，让代码交付透明、可查、上线不翻车。

<p align="center">
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/pypi/v/bugx-cli.svg?style=flat&color=0284c7&label=PyPI" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/bugx-cli"><img src="https://img.shields.io/npm/v/bugx-cli.svg?style=flat&color=0284c7&label=npm" alt="npm"></a>
  <img src="https://img.shields.io/badge/python-%E2%89%A53.11-334155.svg?style=flat" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-334155.svg?style=flat" alt="Platform">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-BSL--1.1-334155.svg?style=flat" alt="License"></a>
</p>

🌐 官网：**https://bugx.dev**

---

### ✨ 核心特性

- 🛡️ **真实执行验证（零误报承诺）**：疑似缺陷由独立沙箱执行“双正例触发 + 单负对照零触发”实跑验证，带完整命令、退出码与终端回显证据留案，杜绝凭空臆测与假报警。
- 📋 **状态机覆盖账本（杜绝偷懒跳过）**：以状态机跟踪全项目审计单元（已覆盖 / 阻塞 / 待审），断点可续，审查完成与否以账本终态为准，模型的口头承诺不算数。
- 🔌 **全生态宿主即插即用**：基于标准 MCP / ACP 协议，原生适配 Google Antigravity、Claude Code、Cursor、Trae、OpenAI Codex、Kimi、MiniMax、ZCode 等，深度支持 Gemini 4、Claude 5.5 与 GPT-4o 底座。
- 📊 **自带全息 Web 工作台**：内置 Studio 交互看板，实时追踪审计拓扑、漏洞调用链、复现凭据与测试变异杀伤力。

---

### 🚀 快速开始

#### 1. 安装 CLI（任选其一）

```bash
# Python 环境 (>= 3.11)
pip install bugx-cli

# 或 Node.js 环境 (>= 18)
npm install -g bugx-cli
```

#### 2. 一键装配 AI 宿主

在用户主目录或待审计项目下运行，BugX 会自动识别本机已有的 AI 编程助手并完成 MCP / 插件配置：

```bash
bugx install
```

> 💡 **提示**：安装完成后请**完全退出并重启你的 AI 编程助手/IDE**。如需高级配置，请参阅 [宿主矩阵与分发指南](docs/01_guides/05_HOST_MATRIX_AND_DISPATCH.md)。

---

### 💬 使用方式

#### 方式一：在 AI 编程助手中直接使用（推荐）

打开任意已配置的项目工作区，在你的 AI 助手（如 Cursor、Claude Code、Antigravity、ZCode 等）中直接对话：

> “审计一下这个项目”  
> “检查登录模块是否存在并发安全漏洞或异常吞噬”  
> “把当前分支的改动审一遍，输出可复现的 PoC 验证命令”

AI 会自动调度 BugX 编排 Worker 并发深挖、运行探针并在沙箱内执行验证。

#### 方式二：在命令行自主长跑

```bash
# 全自主闭环长跑（自动复用宿主配置好的模型与凭据）
bugx autonomous --host opencode

# 启动本地实时 Web 报告预览（打开 http://127.0.0.1:8080）
bugx report serve
```

---

### 🖥️ BugX Studio Web 工作台

审计进行中或结束后，运行 `bugx report serve` 即可在浏览器中查看全息交互工作台：

<p align="center">
  <img src="core/assets/webui-studio.jpg" alt="BugX Studio 工作台" width="860">
</p>

---

### 🔌 支持的 AI 编程宿主

| AI 编程助手 | 接入机制 | 安装命令 |
| :--- | :--- | :--- |
| **Google Antigravity（反重力）** | 原生 Agent/Worker、Skill、项目级 Supervisor/Worker/docs MCP | `bugx install --agent antigravity` |
| **Claude Code** | 原生 MCP / 自治生命周期 | `bugx install --agent claude` |
| **Cursor / Trae / Windsurf** | 项目级 / 用户级 MCP 标准接入 | `bugx install` 自动检测装配 |
| **OpenAI Codex** | 工作区子 Agent / 原生插件 | `bugx install --agent codex` |
| **Agnes Code** | 终端原生 TUI / 智能体与原生 MCP 注入 | `bugx install --agent agnescode` |
| **Kimi Code** | Headless ACP / 原生 MCP 插件 | `bugx install --agent kimi` |
| **MiniMax Code** | 自定义 Agent、Plugin V1 Worker、Skill 与 MCP | `bugx install --agent minimax` |
| **ZCode** | 原生 MCP / 用户级注册 | `bugx install --agent zcode` |
| **OpenCode / Cline** | 原生 ACP / MCP 交互管道 | `bugx install --agent opencode` |

---

### 📚 文档导航

- [用户完整指南](docs/01_guides/01_USER_GUIDE.md)：涵盖从配置、生命周期、MCP/ACP 到故障恢复全流程。
- [命令手册 (CLI Reference)](docs/01_guides/02_COMMANDS.md)：全部命令与参数定义。
- [宿主矩阵与分发指南](docs/01_guides/05_HOST_MATRIX_AND_DISPATCH.md)：各 AI 宿主特化支持与高级配置。
- [故障排查手册](docs/01_guides/03_TROUBLESHOOTING.md)：常见问题快速定位与自愈方案。
- [系统架构与内部设计](docs/02_architecture/01_ARCHITECTURE.md)：BugX 状态机、防伪账本与验证架构。

---

### 💬 交流群

欢迎加入 BugX 官方交流群反馈问题、探讨 AI 代码审计最佳实践：

<p align="center">
  <img src="core/assets/bugx-wechat-group.png" alt="BugX 交流群" width="260">
</p>

---

### 🔍 搜索关键词 (Keywords)

Google Antigravity · 反重力 · 反重力智能体 · Gemini 4 · Gemini 4 Carbon · Gemini 4 Neon · Gemini · Gemini 编程助手 · Gemini Code Assist · G4 模型 · Claude 5.5 · Claude Sonnet 5.5 · Claude Opus 5.5 · Claude Code 审计 · GPT-4o 代码审计 · 自动代码审计 · AI 代码审计 · 自动化 Code Review · Code Review · 代码审查 Agent · 代码审计智能体 · AI Agent · Vibe Coding 安全 · 氛围编程审计 · PR 自动审查 · 零误报 · 真实 PoC 复现 · 盲审验证 · 安全审计 · 漏洞扫描 · 漏洞挖掘 · 静态分析 · MCP 服务 · MCP 安全 · Cursor 审计 · Windsurf 审计 · Trae 审计 · Codex 审计 · 编程助手安全 · 自主审计 · 行为差分 · 内存泄漏检测 · Python 安全 · JavaScript 安全 · DevSecOps

---

### 📄 许可

本项目采用 [Business Source License 1.1](LICENSE)。
