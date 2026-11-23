<p align="center">
  <img src="https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/bugx-hero-en.svg" alt="BugX — effortless audits of AI-written code" width="1000">
</p>

# BugX — AI Code Audit & Code Review Agent for Claude Code, Codex, Cline, OpenCode, and ZCode

BugX is an autonomous code-audit and automated code review agent that runs inside your AI coding assistant or on the command line. It audits AI-written code end to end: it builds a coverage list of your project, dispatches concurrent audit workers, verifies every candidate finding with real executions, and produces an actionable report where each finding ships with a command you can run yourself to reproduce it.

If you use AI coding tools to write production code, BugX is the automated code review and security audit agent that checks what the model actually delivered — not what it claims to have delivered.

<p align="center">
  <a href="https://www.npmjs.com/package/bugx-cli"><img src="https://img.shields.io/npm/v/bugx-cli.svg?style=flat&color=0284c7&label=npm" alt="npm version"></a>
  <a href="https://nodejs.org/"><img src="https://img.shields.io/badge/node-%3E%3D18.0.0-334155.svg?style=flat" alt="Node.js"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-BSL--1.1-334155.svg?style=flat" alt="License: BSL-1.1"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-334155.svg?style=flat" alt="Platform">
</p>

🌐 Website · 官网：**https://bugx.dev**

<p align="center">
  <a href="#-english-documentation">English</a> •
  <a href="#简体中文说明">简体中文</a>
</p>

---

## English Documentation

### Why AI-written code needs an audit

AI writes code faster than any human can review it, and the model carries a training-level bias toward sounding done rather than being done:

1. **The grader rewards confidence.** Human raters score "I checked it carefully, nothing found" higher than "I haven't finished reading the files." The model learns to report good news.
2. **Checking is expensive; claiming is cheap.** Reading hundreds of files takes real work. Replying "no issues found" takes a few words.
3. **It doesn't know what it hasn't checked.** The model cannot see its own coverage gaps and will not say "half done."

BugX counters this with a ledger and four hard rules: a per-unit coverage list (checked / blocked / pending), mandatory reproduction (the same attack input must trigger the finding twice and a clean input must trigger nothing), crash-resumable audits, and hash-chained records that keep every verdict reviewable.

### What BugX does

- **AI code review** — bugs and defects classified by logic, state, boundary, exception, concurrency, performance, and resource handling.
- **Functionality review** — a behavior diff: the same inputs go to your implementation and a reference implementation, and the differences are reported.
- **Performance and memory checks** — performance and resource defects, plus dedicated memory inspection for native, Python, JVM, Electron, Android, and Apple targets.
- **UI audit** — layout, contrast, keyboard paths, and design tokens on a running web page (`bugx probe ui`).
- **Reproducible findings & blind verification** — every vulnerability is blind-reviewed by an independent verifier with a reproduction command or PoC; verified with AST negative control assertions; refuted cases are logged to the FP Ledger.
- **Prompt Policy Engine** — declarative prompt profiles adapt to model-specific safety filters (e.g. OpenAI Cyber Risk / Daybreak) without losing code fidelity.
- **Mutation-verified tests** — `bugx test-verify` mutates your code to check whether the tests actually catch the bugs.
- **Patch bundles** — `bugx patch-forge` builds a patch with static checks and hands it to you for review; it never commits on its own.
- **Lessons base and unattended runs** — `bugx lesson` / `bugx knowledge` accumulate project and global experience; `bugx autonomous` and `bugx goal` run end-to-end audits without supervision.

### BugX Studio

BugX ships with a built-in web console. Run `bugx report serve` and open http://127.0.0.1:8080:

<p align="center">
  <img src="https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/webui-studio.jpg" alt="BugX Studio workbench" width="860">
</p>

Every connected host keeps its own audit sessions (ChatGPT, ZCode, Claude, Codex, DSH, OpenCode, Pi, and more):

<p align="center">
  <img src="https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/webui-chatgpt.jpg" alt="ChatGPT workspace" width="420">
  <img src="https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/webui-zcode.jpg" alt="ZCode workspace" width="420">
</p>

The skill marketplace ships with `/audit` (one-shot full-surface audit with a dual-track report), `/review` (structured findings for branch changes), `/mining` (attack-surface survey and hardening), `/fix` (defensive patches with sandboxed regression), and `/test` (AAA tests with mutation-kill scoring):

<p align="center">
  <img src="https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/webui-skills.jpg" alt="Skill marketplace" width="860">
</p>

### Quick start

Requires Node.js `>=18`. Python `>=3.11` is detected and managed by the wrapper.

**Run without installing (zero-config):**

```bash
npx bugx-cli doctor --strict    # check system readiness and AI tool connectivity
npx bugx-cli init               # initialize the current repository (.bugx/)
npx bugx-cli install            # register local AI assistants
```

**Or install globally** (pick your package manager):

```bash
npm install -g bugx-cli
# pnpm add -g bugx-cli
# yarn global add bugx-cli
# bun add -g bugx-cli
```

After installing, run this once from any directory:

```bash
bugx install          # lists detected AI hosts and registers the BugX MCP servers
bugx doctor --strict  # health diagnostics
```

To install everywhere at once: `bugx install --agent all`.

**Option 1 — inside your AI assistant (recommended).** Open Claude Code, Codex, Cline, OpenCode, or ZCode in any project and just tell it what to check:

**ZCode**
![Running a BugX audit in ZCode](https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/zcode.png)

**Codex**
![Running a BugX audit in Codex](https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/codex.png)

**DSH**
![Running a BugX audit in DSH](https://raw.githubusercontent.com/Bencibr/bugx/main/core/assets/dsh.png)

> "Audit this project"
> "Check the login module for vulnerabilities"
> "Review the changes on this branch for security issues"

**Option 2 — from the terminal.** The AI still comes from your installed hosts: BugX reuses the model and credentials you already configured there and never takes over your API keys.

```bash
bugx auth login                # sign in to the cloud account (lesson and experience sync)
bugx init --cloud-sync sync    # enable cloud sync so the project works across devices

bugx engine pre                # build the coverage list and audit plan
bugx engine status             # follow progress
bugx engine post               # settle: cross-check coverage, evidence, and verdicts

bugx autonomous --host opencode    # full audit without supervision
bugx goal --host opencode "audit this project's login and payment flows"

bugx report serve              # live web console at http://127.0.0.1:8080
bugx report both --format html --out report.html   # offline archive
```

### Essential commands

| Command | What it does |
| :--- | :--- |
| `bugx install` | register the BugX MCP servers with detected AI hosts |
| `bugx hosts` | list every AI environment detected on this machine |
| `bugx doctor --strict` | health diagnostics for installation and connectivity |
| `bugx engine pre / status / post` | run and settle a structured audit lifecycle |
| `bugx autonomous --host <host>` | full unattended audit through your host |
| `bugx goal --host <host> "<target>"` | audit toward a stated target |
| `bugx report serve` | live web console (BugX Studio) |
| `bugx report both --format html` | export the formal + visual report |
| `bugx probe ui --url <url>` | audit a running web UI |
| `bugx test-generate` | generate AAA tests or a self-contained PoC |
| `bugx test-verify` | prove your tests catch real bugs via mutation |
| `bugx patch-forge` | build a reviewed patch bundle |
| `bugx lesson` / `bugx knowledge` | project and global experience base |
| `bugx daemon` | resident watchdog for unattended audits |

### Supported AI coding tools

| AI coding tool | Integration | Install |
| :--- | :--- | :--- |
| Claude Code | native MCP / autonomous lifecycle | `bugx install --agent claude` |
| OpenAI Codex | workspace subagents / native plugin | `bugx install --agent codex` |
| OpenCode | headless ACP / native MCP | `bugx install --agent opencode` |
| Cline / Roo Code | MCP sessions (serial) / CLI process-pool concurrency | `bugx install --agent cline` |
| ZCode | native MCP / user-level MCP registration | `bugx install --agent zcode` |

DeepSeek Harness (DSH), WorkBuddy, Cursor / Windsurf, and more are being adapted. Run `bugx hosts` to list everything detected on this machine.

### Security and privacy

- Project state lives in the project's `.bugx/` directory; the global engine, credentials, knowledge base, and caches live in a user-level BugX directory.
- Credentials are written to a controlled sandbox when needed (readable only by the current user by default). `bugx auth status` reports whether credentials exist — it never prints them.
- Cloud uploads, feedback, diagnostic log reporting, and sync are all explicit operations: each states its `--scope` before it runs.
- `reset` / `uninstall` / `purge` / `--no-backup` delete data. Check each command's dry-run and backup behavior first.

### FAQ

**What is BugX?**
An autonomous code-audit agent for AI-written code. It runs inside AI coding assistants (via MCP) or as a CLI, tracks audit coverage per unit, and verifies every finding by executing it.

**How does BugX verify a vulnerability?**
Rather than trusting the proposing model's self-report, findings are independently blind-reviewed: an attack input must reliably trigger the vulnerability twice, and a clean input must trigger zero times (two positive runs, one negative control) recorded with command, exit code, and artifact hashes, verified by AST syntax assertions. Refuted findings are recorded in the FP Ledger.

**Does BugX trigger AI model cyber-security risk blocks?**
BugX includes a declarative Prompt Policy Engine with adaptive prompt profiles (e.g., defensive QA phrasing for OpenAI Codex / GPT-4o) that prevents models from triggering platform Daybreak / Cyber Risk safety blocks, while preserving 100% of the underlying code and CWE details.

**Does BugX send my code to the cloud?**
The audit itself runs locally. Cloud sync of lessons and experience is an explicit opt-in; nothing is uploaded unless you run it.

**Does BugX replace human review?**
No. It gives you a reviewable ledger — what was checked, what wasn't, and reproducible evidence per finding — so human review starts from facts instead of the model's self-report.

**What languages does BugX audit?**
Multi-language static and behavioral analysis (Python, JavaScript/TypeScript, Java, Go, Rust, and more via tree-sitter), plus dedicated memory checks for native, Python, JVM, Electron, Android, and Apple targets.

### Keywords

vibe coding · vibe coding security · pr review · pull request review · zero false positive · poc generator · mcp security · automated code audit · ai code audit · automated code review · code review agent · ai agent · security audit · vulnerability scanner · vulnerability mining · sast · static analysis · mcp server · claude code · openai codex · opencode · cline · roo code · zcode · coding agent · llm code review · autonomous audit · behavior diff · memory leak detection · python security · javascript security · devsecops

---

## 简体中文说明

### 什么是 BugX？

BugX 是装进 AI 编程助手的**自动代码审计与自动化 Code Review 智能体（Agent）**。它对 AI 写出的代码做端到端自动化审计与代码审查：把项目拆成覆盖清单、派发并发审计 Worker、对每条候选漏洞做真实执行验证，最终产出一份详实的审计报告——每条漏洞都附带一条你可以亲手运行的复现命令。

如果你的生产代码来自 AI 编程工具，BugX 就是那个专注于**自动代码审计**、**Code Review** 与安全加固的智能体层：它核查模型**实际交付了什么**，而不是模型**声称交付了什么**。

### 为什么 AI 代码需要审计

AI 写代码的速度远超人工审查，而且模型带着训练层面的偏差——听起来做完了，比真的做完了更容易：

1. **评分机制奖励自信。** 人类评分员给「我仔细查过了，没发现问题」的分数高于「文件我还没看完」。模型学会的是报喜。
2. **检查贵，声称便宜。** 读几百个文件是体力活，回一句「没发现问题」只要几个字。
3. **它不知道自己没查完。** 模型看不见自己的覆盖盲区，也不会说「做了一半」。

BugX 用一本账和四条硬规矩对抗这件事：逐单元覆盖清单（查完 / 卡住 / 未查）、强制复现（同一段攻击输入触发两次、正常输入触发零次）、审计可断点续跑、哈希链记录让每条结论都可复查。

### 快速上手

要求 Node.js `>=18`；Python `>=3.11` 由包装器自动检测管理。

**免安装直接执行：**

```bash
npx bugx-cli doctor --strict    # 检查系统就绪与 AI 工具连通
npx bugx-cli init               # 初始化当前仓库（.bugx/）
npx bugx-cli install            # 注册本地 AI 助手
```

**或全局安装：**

```bash
npm install -g bugx-cli
```

装好后任意目录跑一次：

```bash
bugx install          # 列出检测到的 AI 宿主并注册 BugX MCP 服务
bugx doctor --strict  # 健康自检
```

一次装全所有宿主：`bugx install --agent all`。

**方式一：在 AI 助手里用（推荐）。** 在任意项目里打开 Claude Code、Codex、Cline、OpenCode 或 ZCode，直接说：

> 「审计一下这个项目」
> 「查一下登录模块有没有漏洞」

宿主里的 AI 会自动调用 BugX：建覆盖清单、派发并发 Worker、跑探针、验证漏洞、出报告。想实时看，终端跑 `bugx report serve`，浏览器打开 http://127.0.0.1:8080。

**方式二：命令行直接跑。** AI 同样来自你已装好的宿主——模型和凭据直接复用，不接管你的 Key。

```bash
bugx auth login                # 登录云端账号（错题/经验同步用）
bugx init --cloud-sync sync    # 开启云端同步

bugx engine pre                # 建立覆盖清单与审计计划
bugx engine status             # 看进度
bugx engine post               # 结账：核对覆盖、证据与裁决

bugx autonomous --host opencode    # 无人值守完整审计
bugx report serve                  # 实时 Web 报告 http://127.0.0.1:8080
```

### 支持的 AI 编程宿主

| AI 编程助手 | 接入方式 | 安装命令 |
| :--- | :--- | :--- |
| Claude Code | 原生 MCP / 自治生命周期 | `bugx install --agent claude` |
| OpenAI Codex | 工作区子 Agent / 原生插件 | `bugx install --agent codex` |
| OpenCode | Headless ACP / 原生 MCP | `bugx install --agent opencode` |
| Cline / Roo Code | MCP 会话（串行）/ CLI 多进程池并发 | `bugx install --agent cline` |
| ZCode | 原生 MCP / 用户级注册 | `bugx install --agent zcode` |

DeepSeek Harness（DSH）、WorkBuddy、Cursor / Windsurf 等持续适配中。运行 `bugx hosts` 查看本机检测到的宿主。

### 安全与隐私

- 项目状态存放在项目内 `.bugx/` 目录；全局引擎、凭据、知识库与缓存在用户级 BugX 目录。
- 凭据按需写入受控沙箱（默认仅当前用户可读）；`bugx auth status` 只报告是否存在，不输出内容。
- 云端上传、反馈、诊断上报与同步都是显式操作，执行前明确 `--scope`。
- `reset` / `uninstall` / `purge` / `--no-backup` 会删除数据，注意各自的 dry-run 与备份行为。

### 关键词

自动代码审计 · AI 代码审计 · 自动化 Code Review · Code Review · 代码审查 Agent · 代码审计智能体 · AI Agent · Vibe Coding 安全 · 氛围编程审计 · PR 自动审查 · 零误报 · 真实 PoC 复现 · 盲审验证 · 安全审计 · 漏洞扫描 · 漏洞挖掘 · 静态分析 · MCP 服务 · MCP 安全 · Claude Code 审计 · Codex 审计 · 编程助手安全 · 自主审计 · 行为差分 · 内存泄漏检测 · Python 安全 · JavaScript 安全 · DevSecOps

## License

[Business Source License 1.1](https://github.com/Bencibr/bugx/blob/main/LICENSE)
