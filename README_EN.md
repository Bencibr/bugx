<p align="center">
  <a href="README.md">中文</a> · English
</p>

<p align="center">
  <img src="core/assets/bugx-hero-en.svg" alt="BugX — effortless audits of AI-written code" width="800">
</p>

# BugX — Autonomous Code Review & Automated Testing Agent

BugX is an autonomous code review and automated testing agent that lives inside your AI coding assistant or runs standalone on the command line.

**It safeguards software delivery at every step:**
- **For Developers**: Catches subtle, high-impact bugs immediately after code is written, eliminating painful debugging loops and rework.
- **For QA & Test Engineers**: Automates black-box, functional, and performance testing across complex edge cases that manual reviews often miss.
- **For Engineering Teams**: Guarantees code strictly meets functional contracts with rock-solid stability and resilience, ensuring dependable, crash-free releases.

By replacing subjective self-reporting with automated, evidence-backed audits, BugX accelerates release velocity, elevates software quality, and reduces long-term maintenance costs. Supported environments include **Claude Code, OpenAI Codex, Cursor, Trae, Windsurf, Kimi Code, MiniMax Code, OpenCode, ZCode, Cline, DeepSeek Harness (DSH), WorkBuddy, and Qoder**.

<p align="center">
  <a href="https://pypi.org/project/bugx-cli/"><img src="https://img.shields.io/pypi/v/bugx-cli.svg?style=flat&color=0284c7&label=PyPI" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/bugx-cli"><img src="https://img.shields.io/npm/v/bugx-cli.svg?style=flat&color=0284c7&label=npm" alt="npm"></a>
  <img src="https://img.shields.io/badge/python-%E2%89%A53.11-334155.svg?style=flat" alt="Python">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-334155.svg?style=flat" alt="Platform">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-BSL--1.1-334155.svg?style=flat" alt="License"></a>
</p>

🌐 Website: **https://bugx.dev**

## Contents

- [The problem it solves](#the-problem-it-solves)
- [Quick start](#quick-start)
- [What a full audit looks like](#what-a-full-audit-looks-like)
- [Supported AI coding tools](#supported-ai-coding-tools)
- [Data and security](#data-and-security)
- [Documentation](#documentation)
- [Development](#development)
- [License](#license)

## The problem it solves

AI writes code faster than any human can review it. And the model carries a quirk that makes this worse: it cuts corners. That isn't malice — it's how the model was trained. Three mechanisms stack together:

1. **The grader rewards confidence.** The last stage of training is human raters scoring responses. One answer says "I checked it carefully, nothing found." Another says "I haven't finished reading the files." The first scores better — it sounds reliable and useful. So the model learns that reporting good news earns the reward.
2. **Checking is expensive; claiming is cheap.** Reading a few hundred files takes real work. Replying "no issues found" takes a few words. When both options sit on the table, the cheap one always wins.
3. **It doesn't know what it hasn't checked.** The model can't see its own coverage gaps, and nothing in training taught it to say "half done." So it says "done" — every time.

BugX replaces that with a ledger and four hard rules:

1. **A coverage list.** Your project is split into audit units, each with a real status: checked, blocked, or not yet checked. Coverage is tracked, not assumed.
2. **Independent blind review & reproduction.** Suspicious findings are blind-reviewed by an isolated verification agent. The attack input must reproduce the bug twice, and a clean input must trigger zero times, backed by AST syntax anti-empty assertions. Refuted findings are logged to the FP Ledger to prevent repeat false alarms.
3. **It picks up where it left off.** Close your AI tool and reopen it later — the audit resumes from the last recorded step instead of starting over.
4. **Every verdict stays reviewable.** Who checked what, when, and on what evidence goes into the ledger line by line, with hash chaining between pages. Nothing gets edited quietly.

## Quick start

Python `>=3.11`; the npm wrapper needs Node.js `>=18`.

Install from PyPI:

```bash
python -m pip install bugx-cli
```

Or install from npm:

```bash
npm install -g bugx-cli
```

Check and update using the server release matching your current install source:

```bash
bugx update --check          # check only: 0=up-to-date, 1=update available, 2=check failed
bugx update                  # update pip or npm wrapper installation
bugx update --channel alpha  # explicitly choose pre-release channel
```

Then run this once from any directory:

```bash
bugx install
```

> ⚠️ **Important (Restart Required)**: After configuring or updating project MCP settings, **completely restart your host IDE or AI coding assistant** (e.g., Cursor, Trae, VS Code, Kimi Code, MiniMax Code, ZCode) so the host process reloads the project-level MCP server configuration.

CLI boolean flags are switches: pass `--force` to enable, omit it to disable. Do not pass `--force=True` or `force=True`.
It lists every AI host it detects on your machine and walks you through picking one. To install everywhere at once, use `bugx install --agent all`.

### Connect the project to BugX Cloud

From the repository root, confirm the account, sync scope, and sensitive-data policy. Run `bugx auth login --root .` and check that `bugx auth status --root . --verify --json` reports the expected account and `auth_verified=true`.

When both the account-wide and project scopes are authorized, initialize a fresh project with `bugx init --root . --cloud-sync sync` and check the first transfer with `bugx cloud sync --root . --scope all`. Initialization installs `.agents/skills/bugx-user/SKILL.md`. Invoke `$bugx-user` in a host that discovers project Skills there. For existing projects, use `bugx cloud sync --root . --scope all` after the full scope is authorized, then check the binding and queue with `bugx cloud status --root . --scope all --json`. This flow includes project registration and profile, project knowledge, and account-wide knowledge. Decline requests for partial sync.

### Option 1: use it inside your AI assistant (recommended)

No terminal and no commands to remember. Open your AI assistant in any project — Claude Code, Codex, Cline, OpenCode, ZCode — and just tell it what to check:

**ZCode**
![Running a BugX audit in ZCode](core/assets/zcode.png)

**Codex**
![Running a BugX audit in Codex](core/assets/codex.png)

**DSH**
![Running a BugX audit in DSH](core/assets/dsh.png)

> "Audit this project"
> "Check the login module for vulnerabilities"
> "Review the changes on this branch for security issues"

The AI inside your host calls BugX on its own: it builds the coverage list, dispatches concurrent workers, runs probes, verifies the findings, and writes the report. To watch an audit live, run `bugx report serve` in a terminal and open **http://127.0.0.1:8080** — that's the full web report.

### Option 2: run it from the command line

On the command line, the AI still comes from your installed hosts: BugX reuses the model and credentials you already configured there and never takes over your API keys (`--model` / `--provider` default to your host's configuration).

Check the selected host and connection with `bugx doctor --root . --strict`. For a staged CLI run, follow the [lifecycle guide](docs/01_guides/01_USER_GUIDE.md#8-从-pre-到-post-的完整生命周期) through execution, evidence review, and settlement. See the current stage with `bugx engine --root . status --json`.

Or let BugX run the whole thing while you do something else:

```bash
bugx autonomous --host opencode    # model and credentials come from your OpenCode setup
# or give it a target:
bugx goal --host opencode "audit this project's login and payment flows"
```

Fixes and outputs are command-line work too:

```bash
bugx test-generate ...     # generate AAA tests or a self-contained PoC for a unit
bugx test-verify ...       # mutate the code to prove your tests catch real bugs
bugx patch-forge --from-issue <id> --diff fix.diff --repro repro.md --test test_ok.py --dry-run
```

`patch-forge` builds a patch bundle and runs static checks on it. It produces the bundle but commits nothing — remove `--dry-run` once you've reviewed it.

Open the web UI for reports (during or after an audit):

```bash
bugx report serve     # then open http://127.0.0.1:8080 for live coverage, evidence, and findings
```

For offline archives, export a static report:

```bash
bugx report both --format html --out report.html
```

See the host table below for valid `--agent` values; `bugx hosts` lists every environment detected on this machine with its capabilities.

## What a full audit looks like

1. **pre (with the game plan).** BugX enumerates your source and configuration files, then builds a minimal list of audit units (single modules plus cross-module business flows). Before anything runs, you get a plan with five decisions in one pass: **cloud sync** (sign in and sync lessons and experience, or explicitly choose offline use), **review mode**, **polling strategy**, **fix mode**, and **commit handling**. Confirm the actual sync scope and review policies presented by the startup gates.
2. **Workers fan out.** The host hands audit units to concurrent workers. Each worker takes a lease, sends heartbeats, runs probes, and commits checkpoints.
3. **Evidence lands on disk.** Every worker conclusion carries the command it ran, the exit code, and the artifact hash. Coverage is only ever `covered` or `blocked` — "roughly checked" doesn't exist here.
4. **Findings get adjudicated.** Candidate bugs first go through the engine's verification channel: two positive runs plus a negative control, executed for real. Only then are they confirmed or rejected, and a rejection needs a stated reason.
5. **post.** Coverage and findings are cross-checked and the audit settles. Whether it's actually done is decided by the state machine's final state — the model saying "done" doesn't count.
6. **The report.** `bugx report both --format html --out report.html` produces the formal and visual reports; `bugx report serve` starts the live preview.

## A look at the interface

BugX ships with BugX Studio, a web workbench. Run `bugx report serve` and open http://127.0.0.1:8080:

<p align="center">
  <img src="core/assets/webui-studio.jpg" alt="BugX Studio workbench" width="860">
</p>

The workspace covers every connected host, and each host keeps its own audit sessions (the sidebar shows ChatGPT, ZCode, Claude, Codex, DSH, OpenCode, Pi, and more):

<p align="center">
  <img src="core/assets/webui-chatgpt.jpg" alt="ChatGPT workspace" width="420">
  <img src="core/assets/webui-zcode.jpg" alt="ZCode workspace" width="420">
</p>

The skill marketplace ships with `/audit` (one-shot full-surface audit with a dual-track report), `/review` (structured findings for branch changes), `/mining` (attack-surface survey and hardening), `/fix` (defensive patches with sandboxed regression), and `/test` (AAA tests with mutation-kill scoring):

<p align="center">
  <img src="core/assets/webui-skills.jpg" alt="Skill marketplace" width="860">
</p>

## What it can do

**Audit**
- **Code review**: bugs and defects, classified by logic / state / boundary / exception / concurrency / performance / resource.
- **Functionality review**: business-flow audit plus a behavior diff — feed the same inputs to your implementation and a reference implementation, then compare what differs.
- **Performance and memory**: performance and resource bugs, plus dedicated memory checks for six target families: native, Python, JVM, Electron, Android, and Apple.
- **UI audit**: layout, contrast, keyboard paths, and design tokens on a running web page (`bugx probe ui`).

**Reproduce and verify**
- Every finding ships with a command or input you can run yourself: the same bad input breaks it twice, a clean input breaks nothing, and all three runs stay on record.
- Your tests get graded too: `bugx test-verify` mutates your code to check whether the tests actually catch the bugs.

**Fixes and outputs**
- Generate AAA test cases or self-contained PoCs (`bugx test-generate`).
- Build patch bundles with static checks, delivered for your review — never committed for you (`bugx patch-forge`).
- Write reports (`bugx report`).

**Long term**
- **A lessons base**: sync verified lessons and experience with `bugx cloud sync` in the authorized scope. Use `bugx lesson retrieve` to look up a symptom or module in the next review.
- **Unattended runs**: follow your host (`bugx autonomous`), run toward a goal (`bugx goal`), or keep a resident daemon (`bugx daemon`).
- **Diagnostics**: `bugx doctor` / `bugx log` / `bugx stats` for installation, logs, and usage.

## FAQ

**What is BugX?**
An autonomous code-audit agent that lives inside AI coding assistants: it runs via MCP in Claude Code, Codex, Cline, OpenCode, and ZCode, or as a standalone CLI, tracks audit coverage per unit, and verifies every finding with real executions.

**Which AI coding tools does BugX support?**
BugX currently supports **Claude Code, OpenAI Codex, Cursor, Trae, Windsurf, Kimi Code, MiniMax Code, Cline / Roo Code, OpenCode, ZCode, DeepSeek Harness (DSH), WorkBuddy, and Qoder**, with continuous adaptations across the Model Context Protocol (MCP) ecosystem.

**How does BugX verify a vulnerability?**
Rather than trusting the proposing model's self-report, findings are independently blind-reviewed: an attack input must reliably trigger the vulnerability twice, and a clean input must trigger zero times (two positive runs, one negative control) recorded with command, exit code, and artifact hashes, verified by AST syntax assertions. Refuted findings are recorded in the FP Ledger.

**Does BugX trigger AI model cyber-security risk blocks?**
BugX includes a declarative Prompt Policy Engine with adaptive prompt profiles (e.g., defensive QA phrasing for OpenAI Codex / GPT-4o) that prevents models from triggering platform Daybreak / Cyber Risk safety blocks, while preserving 100% of the underlying code and CWE details.

**Does BugX send my code to the cloud?**
The audit itself runs locally. Enabling lesson and experience sync can trigger transfers at startup and after eligible ledger writes. Check the authorized scope and sensitive-data policy before enabling sync.

**Does BugX replace human review?**
No. It gives you a reviewable ledger — what was checked, what wasn't, and reproducible evidence per finding — so human review starts from facts instead of the model's self-report.

**What languages does BugX audit?**
Multi-language static and behavioral analysis (Python, JavaScript/TypeScript, Java, Go, Rust, and more via tree-sitter), plus dedicated memory checks for native, Python, JVM, Electron, Android, and Apple targets.

## Supported AI coding tools

| AI coding tool | Integration | Install command |
| :--- | :--- | :--- |
| **Claude Code** | Native MCP / autonomous lifecycle | `bugx install --agent claude` |
| **OpenAI Codex** | Workspace subagents / native plugin | `bugx install --agent codex` |
| **Cursor / Trae / Windsurf** | Native project-level MCP integration | `bugx install --agent cursor` |
| **Kimi Code** | Managed plugin / project MCP | `bugx install --agent kimi` |
| **MiniMax Code** | Plugin V1 package / agent & MCP | `bugx install --agent minimax` |
| **OpenCode** | Headless ACP / native MCP | `bugx install --agent opencode` |
| **Cline / Roo Code** | MCP chat sessions / multi-process concurrency | `bugx install --agent cline` |
| **ZCode** | Native MCP / user-level MCP registration | `bugx install --agent zcode` |
| **DeepSeek Harness (DSH)** | Desktop launcher / managed plugin & MCP | `bugx install --agent dsh` |
| **WorkBuddy / Qoder** | Connector meta / native extension | `bugx install --agent workbuddy` |

Run `bugx hosts` to see all environments detected on your machine.

## Data and security

- Project state lives in the project's `.bugx/` directory; the global engine, credentials, knowledge base, and caches live in a user-level BugX directory.
- Credentials are written to a controlled sandbox when needed (readable only by the current user by default). `bugx auth status` reports whether credentials exist — it never prints them. A model name is not a credential.
- Knowledge sync uses the configured scope and sensitive-data policy. Bundle uploads, diagnostic log reports, and feedback have separate commands and data boundaries.
- `reset` / `uninstall` / `purge` / `--no-backup` delete data. Check each command's dry-run and backup behavior first.

## Documentation

The documentation is written in Chinese:

- [User guide](docs/01_guides/01_USER_GUIDE.md): installation, configuration, host capabilities, lifecycle, MCP/ACP, cloud scope, results, and recovery.
- [Command reference](docs/01_guides/02_COMMANDS.md): every command, argument, default, scope, side effect, and exit behavior.
- [Troubleshooting](docs/01_guides/03_TROUBLESHOOTING.md) covers common symptoms and recovery steps.
- The [BugX user skill](skills/platform/bugx-user/SKILL.md) covers setup, cloud sync, evidence review, and lesson reuse.
- [UI audit (probe ui)](docs/01_guides/02_COMMANDS.md#63-probe): responsive layout, contrast, keyboard paths, and design tokens on a running web UI.
- [AI reference](docs/03_ai/01_AI_REFERENCE.md): MCP, lifecycle, state, evidence, and failure contracts for hosts and agents.
- [Docs index](docs/README.md): architecture and documentation navigation.

## Development

Repository conventions (docs sync, version contract, verification gates) live in [AGENTS.md](AGENTS.md); architecture and internal design live in [docs/02_architecture/](docs/02_architecture/). Before committing, run `python3 scripts/bump_version.py --check`.

## Keywords

vibe coding · vibe coding security · pr review · pull request review · zero false positive · poc generator · mcp security · automated code audit · ai code audit · automated code review · code review agent · ai agent · security audit · vulnerability scanner · vulnerability mining · sast · static analysis · mcp server · claude code · openai codex · opencode · cline · zcode · coding agent · llm code review · autonomous audit · behavior diff · memory leak detection · python security · javascript security · devsecops

## License

[Business Source License 1.1](LICENSE).
