<p align="center">
  <img src="https://raw.githubusercontent.com/stablyai/bug-hunter/main/core/assets/bugx-hero-en.png" alt="BugX - AI writes the code. BugX hunts the bugs." width="800" style="max-width: 100%; height: auto;" />
</p>

> **Version 0.1.0** | **Updated: 2026-10-09**

> **Search summary** — BugX is a local-first Python CLI for evidence-driven AI code auditing, security code review, vulnerability hunting, and reproducible bug verification. It records project-scoped coverage, Findings, execution evidence, artifacts, and recovery state for supported MCP/ACP coding hosts.

<p align="center">
  <strong>Host-neutral, evidence-driven autonomous code auditing and vulnerability-hunting agent for AI-assisted software engineering.</strong>
  <br>
  <em>AI writes the code. BugX hunts the bugs. · 专为现代 AI 协同软件工程设计的深度代码审计与漏洞挖掘智能体。</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/pypi/v/bugx-cli.svg?style=flat&color=0284c7&label=PyPI" alt="PyPI Version"></a>
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/pypi/pyversions/bugx-cli.svg?style=flat&color=334155&label=Python" alt="Python Versions"></a>
  <a href="https://github.com/stablyai/bugx/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-BSL--1.1-334155.svg?style=flat" alt="License: BSL-1.1"></a>
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-334155.svg?style=flat" alt="Platform"></a>
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/badge/security-AST--Redacted%20%26%20Local--First-0284c7.svg?style=flat" alt="Security"></a>
</p>

---

<p align="center">
  <a href="#-english-documentation">English Documentation</a> •
  <a href="#-简体中文说明">简体中文说明</a>
</p>

---

## 🌐 English Documentation

### 🌟 Why BugX?

Traditional static code analyzers and security linters flood developers with hundreds of theoretical warnings, unverified regex alerts, and false positives. 

**BugX** is different. It is an **evidence-driven, host-neutral auditing agent** that embeds deep adversarial inspection directly into your favorite AI programming assistants (Claude Code, OpenAI Codex, OpenCode, Cursor, Windsurf, DeepSeek Harness, WorkBuddy, Pi).

```
Traditional Scanners (Noisy & Speculative):
[Source Code] ──► (Regex / Pattern Matching) ──► 500+ Unverified Alerts (High Noise)

BugX Protocol (Evidence-Closed Loop):
[Source Code] ──► [White-box AST & Route Map]
                      │
                      ▼
               [Deep Mutation & Race Fuzzing]
                      │
                      ▼
               [Executable Minimal PoC Script] ──► Confirmed Reproducible Bug
                      │
                      ▼
               [TDD Red-Green Patch Synthesis] ──► Verified Self-Healing Fix
```

### ⚡ Key Highlights

1. **Host-Neutral & Non-Intrusive**: Does not usurp LLM execution rights. BugX coordinates tasks, manages leases, and monitors health while your trusted AI assistant reads code, tests logic, and writes fixes natively.
2. **Guaranteed Proof-of-Concept (PoC)**: Never reports a finding based on speculation. Every confirmed vulnerability includes an executable, self-contained reproduction script.
3. **Adversarial Differential Oracle**: Pits Red Agent (attacker generating edge-case exploit payloads) against Blue Agent (evaluating defenses) to eliminate false positives.
4. **Adaptive Lifecycle & Budget Governor**: Dynamic budget calculator (`RoundBudgetCalculator`) computes optimal wall-clock windows (180s–7200s) based on code volume and concurrency, automatically draining dangling worker leases.
5. **Cross-Device Session Roaming**: Seamlessly package, roam, and resume ongoing multi-round audit sessions between office workstations and remote laptops.
6. **Strictly Local-First & AST Redacted**: All parsing, mutations, reproduction logs, and findings stay locally on your machine in `.bugx/`. Sensitive secrets are AST-redacted before any optional telemetry.

---

### 📦 Installation

#### Requirements
- **Python**: `>= 3.11` (Python 3.11 or 3.12 recommended)
- **OS**: macOS, Linux, Windows (WSL2 / native)

#### Recommended Installation

```bash
# Standard installation via pip
pip install bugx-cli

# Or with uv (ultra-fast)
uv pip install bugx-cli

# Or isolated application via pipx
pipx install bugx-cli
```

#### Optional Feature Extras

```bash
# Terminal interactive TUI menu support
pip install "bugx-cli[tui]"

# Tree-sitter advanced multi-language AST graph indexing
pip install "bugx-cli[graph]"

# Cryptographic telemetry & signing tools
pip install "bugx-cli[crypto]"

# Full bundle with all extensions
pip install "bugx-cli[tui,graph,crypto]"
```

---

### 🚀 30-Second Quick Start

Get BugX up and running in your repository in three simple commands:

```bash
# 1. Initialize repository audit workspace (creates local .bugx/)
bugx init

# 2. Automatically discover and connect to all local AI assistants
bugx install

# 3. Verify tool connectivity and runtime health
bugx doctor
```

Once installed, simply open your target project in your AI coding tool (e.g. Claude Code or OpenCode) and prompt your assistant:
> *"Audit authentication and database boundary logic with BugX."*

---

### 🤖 Supported AI Coding Ecosystem

| AI Coding Assistant | Integration Mechanism | Setup Command |
| :--- | :--- | :--- |
| **Claude Code** | Native MCP / Autonomous Protocol | `bugx install --agent claude` |
| **OpenAI Codex** | Workspace Subagent / Native Plugin | `bugx install --agent codex` |
| **OpenCode** | Headless ACP / Native MCP | `bugx install --agent opencode` |
| **DeepSeek Harness (DSH)** | Official Web UI Extension & MCP | `bugx install --agent dsh` |
| **WorkBuddy** | Official IDE Connector Bundle | `bugx install --agent workbuddy` |
| **Cline / Roo Code** | MCP; interactive single-claim or CLI process-pool execution | `bugx install --agent cline` |
| **Cursor / Windsurf** | Standard MCP Server | `bugx mcp serve --role host` |

Run `bugx hosts` to inspect all detected AI environments on your machine.

---

### 🛠️ CLI Quick Reference

```bash
# Inspection & Health
bugx doctor                  # Comprehensive environment, MCP, and project diagnostics
bugx hosts                   # List all detected AI host environments
bugx stats                   # View audit metrics, findings summary, and token economy

# Autonomous & Orchestration
bugx autonomous              # Unattended multi-round autonomous audit campaign
bugx autonomous --host cline --workers 4 # Cline CLI process pool (2~6; 0=adaptive)
bugx init --commit-policy life # Configure automatic Git checkpoints (life | round | none)

# Testing & Verification
bugx test-generate           # Synthesize standardized AAA unit tests or reproduction PoCs
bugx test-audit              # 4-dimensional quality audit for existing test suites
bugx test-verify             # Verify test-suite defect-catching efficacy via AST mutation

# Maintenance & Clean-Up
bugx update                  # Upgrade BugX engine to the latest release
bugx uninstall               # Remove AI host connectors while safely retaining audit findings
```

Autonomous sessions persist their host, execution mode, worker capacity, and dispatch settings in the current project’s `.bugx/bugx-runtime.json`. Runtime state is workspace-local, so concurrent hosts do not share global scheduling state. Explicit command arguments take precedence over the current session context.

---

## 🇨🇳 简体中文说明

### 🌟 为什么选择 BugX？

传统静态代码扫描工具（SAST）往往依靠正则或死板规则进行匹配，产生成百上千条未经证实的“口头报警”和噪音，让开发者疲于甄别。

**BugX** 专为大模型协同编程时代而设计。它是一款**宿主中立、对抗式、证据闭环驱动的深度代码审计与漏洞挖掘智能体**，原生集成在主流 AI 编程助手（Claude Code、OpenAI Codex、OpenCode、Cursor、Windsurf、DeepSeek Harness 等）中运转。

```
传统静态扫描（海量噪音）：
[源代码] ──► (死板正则匹配) ──► 500+ 未经证实的口头告警 (极高噪音)

BugX 闭环协议（事实证据自洽）：
[源代码] ──► [白盒 AST 与接口图谱遍历]
                   │
                   ▼
            [深度变异与异步竞态扰动]
                   │
                   ▼
            [单文件自包含可执行 PoC 脚本] ──► 确认真实可复现漏洞
                   │
                   ▼
            [TDD 红绿沙箱自愈补丁合成] ──► 验证通过的最小安全补丁
```

### ⚡ 核心能力与技术特色

1. **宿主无关，零侵入性**：BugX 不争夺模型执行权。具体的读代码、跑测试、写代码全部由你信任的 AI 宿主原生驱动；BugX 提供底层任务调度、租约看门狗与证据检验。
2. **可执行 PoC 真实闭环**：绝不凭空推测！每一个被确认的缺陷，系统都必须自动合成可独立执行验证的最小复现代码（Proof-of-Concept）。
3. **红蓝对抗博弈差分**：红方 Agent 生成漏洞 Exploit PoC，蓝方 Agent 评估可防卫性或构造正常业务对照请求，基于差分对比彻底消除虚假误报。
4. **自适应弹性预算治理**：内置 `RoundBudgetCalculator`，根据代码行数、技术栈耗时与并发度自适应推算 180s~7200s 单轮墙钟预算，轮次流转自动排干孤儿租约。
5. **跨设备漫游断点续挖**：白名单轻量快照技术，自动剔除 PID 和本地锁；支持在办公室工作站与家用笔记本之间无缝漫游续挖，Git 漂移自动增量重测。
6. **本地优先与 AST 强脱敏**：所有语法树解析、变异数据、日志与证据均严格保存在本地 `.bugx/` 目录；发网前自动执行 AST 级秘钥过滤与伪匿名化。

---

### 📦 安装方式

#### 系统环境要求
- **Python**：`>= 3.11`（建议 3.11 或 3.12）
- **支持系统**：macOS、Linux、Windows (WSL2 或 原生环境)

#### 推荐安装命令

```bash
# 基础安装
pip install bugx-cli

# 或使用 uv（极速推荐）
uv pip install bugx-cli

# 或使用 pipx（独立应用环境隔离）
pipx install bugx-cli
```

#### 可选功能扩展

```bash
# 终端交互式 TUI 单选菜单增强
pip install "bugx-cli[tui]"

# Tree-sitter 高精度多语言 AST 语法树图谱解析
pip install "bugx-cli[graph]"

# 加密遥测与签名验证套件
pip install "bugx-cli[crypto]"

# 一键安装全功能合集
pip install "bugx-cli[tui,graph,crypto]"
```

---

### 🚀 30 秒极速上手

只需 3 条命令，即可在任意工程中完成 BugX 部署：

```bash
# 1. 初始化项目审计空间（创建本地 .bugx/）
bugx init

# 2. 自动检测并一键接入本机已安装的全部 AI 工具
bugx install

# 3. 运行环境诊断与连通性自检
bugx doctor
```

安装完成后，在你的 AI 编程助手（如 Claude Code 或 OpenCode）中直接提问即可：
> *“使用 BugX 对本项目的鉴权模块与数据操作边界进行深度审计。”*

---

### 🤖 支持的 AI 编程宿主

| AI 编程助手 | 接入机制 | 快速安装命令 |
| :--- | :--- | :--- |
| **Claude Code** | 原生 MCP / 自治生命周期 | `bugx install --agent claude` |
| **OpenAI Codex** | 工作区子 Agent / 原生插件 | `bugx install --agent codex` |
| **OpenCode** | Headless ACP / 原生 MCP | `bugx install --agent opencode` |
| **DeepSeek Harness (DSH)** | 官方 Web UI 扩展与 MCP | `bugx install --agent dsh` |
| **WorkBuddy** | 官方 IDE 连接器拓展包 | `bugx install --agent workbuddy` |
| **Cline / Roo Code** | 插件 MCP 交互会话（单 Claim 串行）/ CLI 多进程池自治并发 | `bugx install --agent cline` |
| **Cursor / Windsurf** | 通用 MCP 服务协议 | `bugx mcp serve --role host` |

运行 `bugx hosts` 可实时查看当前设备上支持与检测到的 AI 工具清单。

---

### 🛡️ 安全、隐私与数据合规承诺

- **严格本地优先（Local-First）**：所有 AST 解析、变异测试、PoC 脚本与审计报告均完整保留在本地磁盘的 `.bugx/` 目录中。
- **绝不擅自上传源代码**：严禁在未经授权的情况下将业务专有代码传输到外部网络。
- **沙盒自愈与资源有界防护**：严格限定最大并发度、单回合墙钟预算与文件资源锁，保障宿主系统的绝对稳定性。

---

### 📄 开源许可协议与商业授权

BugX Agent 客户端基于 [BSL 1.1 (Business Source License 1.1)](https://github.com/stablyai/bugx/blob/main/LICENSE) 许可协议分发，允许个人与企业免费用于内部研发与日常安全评估；发布 3 年后自动转为 Apache 2.0。BugX Cloud 云端服务采用商业授权，商务合作请联系 business@bugx.dev。
