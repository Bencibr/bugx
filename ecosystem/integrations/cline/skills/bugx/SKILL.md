---
name: bugx
description: An autonomous agent dedicated to code review, vulnerability hunting, and automated bug fixing. Built for deep investigations, BugX steps in when you are wrapping up a feature, opening a pull request, or hunting down an elusive bug. It analyzes code logic, automatically writes test cases to uncover and reproduce hidden flaws, and delivers verified patches that resolve root causes. Always safeguarding your code quality and security.
---
<!-- bugx-locale: en-US -->

# BugX

This is BugX's Cline project-level Skill entry. Read the global engine protocol first: load the global protocol via `bugx engine path --name bugx-runtime-contract.md` and `bugx engine path --name bugx-quick.md`, and follow the lifetime, Supervisor, evidence, and reporting protocols in them.

This entry only hands the task to the current Cline; the BugX CLI installs resources only — it does not start Cline or run audits. If the project is not trusted by Cline, trust it in Cline first; once in a session this Skill is auto-discovered from the project's `.cline/skills/bugx/SKILL.md`, and can also be triggered explicitly with `/bugx` in the conversation (the Cline CLI has no `--agent` launch argument).
Public agent template source: `bugx engine path --name bugx.md`.


## Cline native interaction adaptation (mandatory)

The launcher must write the host into the current session runtime via `--host cline`; never let a project's `opencode.json` infer the host. Use the `ask_question` provided by the current Cline session (or an equivalent structured user-selection control); execute the option's unique `value` and controlled `action`. This entry is loaded by the Cline project-level Skill (`.cline/skills/bugx/SKILL.md`, discovered automatically by Cline or triggered by `/bugx`); the Cline desktop enters through the current workspace's BugX Skill, while Cline CLI uses `cline <prompt>`, headless autonomy is carried by the CLI's JSON/headless mode for prompts and session recovery, and `cline --acp` is an ACP service endpoint that must not be run directly as a one-shot prompt command. The installer writes Cline MCP into `mcpServers` in the Cline data-directory file `<CLINE_DATA_DIR>/settings/cline_mcp_settings.json` (default `~/.cline/data/settings/cline_mcp_settings.json`); Cline 3.x has no workspace-level MCP scope, so a project `.cline/mcp.json` is never loaded. `bugx-supervisor` and the worker must both exist and be enabled — the Skill file cannot replace MCP. Cline does not support BugX's `Task(subagent_type="bugx-worker")` dispatch contract; the desktop's built-in research sub-agent lacks worker write and MCP permissions, and the CLI's own Agent/Team interface is not equivalent to that Task contract. Interactive single sessions therefore stay serial with one claim at a time: run `bugx engine --root . workers --count 1`, then have the current session complete worker-start, heartbeat/tool_call, and checkpoint through `bugx-supervisor-worker`, and claim the next unit only after verifying a valid checkpoint. Multi-process CLI/autonomous execution uses `bugx autonomous --host cline --workers N`, or the workers' explicit `--execute`; the runtime context atomically writes host/execute/worker_capacity into the current project session and switches to a bounded 2-6 process pool. Each claim runs in `<artifact_root>/.cline_data`, with `CLINE_DATA_DIR` and `--data-dir` injected, loading only `bugx-supervisor-worker` (`--role worker`) and `bugx-docs` — never the controlling `bugx-supervisor`. The pool follows Supervisor adaptive slots using a sliding window and the runtime context's `stagger_seconds` to refill in a staggered way; a claim is reclaimed on timeout, abnormal exit, or missing persisted checkpoint, and only a valid Supervisor checkpoint counts as success. During the mine stage the shared source is read-only, and PoCs and evidence may only be written into the claim's artifact_root.


## Startup interaction adaptation: startup policy (no questionnaire)

Startup policy is resolved in this order: CLI arguments > project `.bugx/config.json` > user `~/.bugx/config.json` > system defaults. In autonomous startup (`--autonomous`) mode, a regular start presents no questionnaire for cloud sync, review mode, polling, fixes, or commits; in non-autonomous interactive mode, the host uses its native question control to present the battle plan for user confirmation (one-click approve, fine-tune, or defer). When optional MCP/probes are unavailable, fall back automatically to Native Protocol Mode and built-in capabilities; never require a restart merely because session proof is missing.
## Host Route (before intent triage)

- **ZCode host**: prefer loading the sibling `bugx-zcode` skill (ZCode-specific handbook: direct-drive path, rpc fallback, field-tested remedies). If it is not registered in the current session (newly installed skills need a host-session restart to appear), apply its core conclusions directly: after `workers` pre-claim, **drive the protocol channel from the host session** (worker MCP tools or `bugx supervisor rpc --role worker`, caller_role=worker); do not default to wrapping the worker in a subagent; `run_sandboxed_probe` must carry the current Task's lease_id/execution_id.
- All other hosts follow the sections below.



## How to Use This Skill (AI Meta-Guidance and Intent Decision Tree)

When you (the AI) trigger and read this Skill, strictly follow this cognitive model and execution order:

### 1. Intent Decision Tree
In the first second of receiving a task, classify which branch it belongs to:
- **Branch A: Focused Quick Triage / Specific Function / Diff / Error Stack**
  - **Indicators**: Single-file inspection, reproducing a specific function, analyzing an exception stack trace.
  - **Action**: Write a lightweight reproduction test (PoC) or run targeted tests locally using `bugx view`, `bugx test-generate`, `bugx test-verify`; **strictly forbid starting heavy full-lifecycle gates!**
- **Branch B: Full-Repository Vulnerability Hunting / Deep Mutation / Parallel Worker Audit**
  - **Indicators**: User explicitly triggers `/bugx`, requests "full codebase audit", or "hunt deep vulnerabilities".
  - **Action**: Follow the canonical lifecycle progression (`engine pre` -> gate confirmation -> Explore -> Mutate -> Repair -> post settlement).
- **Branch C: Environment Diagnosis / Dependency Repair / Host Inspection**
  - **Action**: Run `bugx doctor --root <ROOT> --json` and `bugx engine --root <ROOT> status --json`; never use `init` or `--force` blindly.

### 2. Standard 5-Step Execution Loop
1. **Target Root**: Confirm the target repository's absolute path `<ROOT>`; strictly restrict operations within `<ROOT>`, and forbid broad filesystem scans.
2. **Inspect State**: Run read-only commands to check runtime state first (`bugx engine --root <ROOT> status --json` or `bugx doctor`).
3. **Select Route**: Pick commands based on intent; if parameters are needed, run `bugx help <top-level-cmd>` (e.g. `bugx help cloud`) to read live syntax dynamically; never guess.
4. **Handle Gates**: If `engine pre` exits with code `2`, it signals waiting for host structured gate interaction; present options via the host's native control and execute the controlled action selected by the user. Do not fabricate choices or spin-loop. Exit code `0` confirms gate passage only, not audit completion.
5. **Close the Loop**: Defects require standalone reproduction scripts (PoC) and call-stack evidence; report truthfully using the 6 standard status terms (read-only inspection, project ready, gate ready, test execution, mining launched, mining completed).

### 3. Core Action Matrix (High-Frequency Commands Covering 90% of Calls)
- **Environment Diagnosis**: `bugx doctor --root <ROOT> --json` (`--repair-assets` for repair)
- **Startup and Gates**: Run `bugx engine --root <ROOT> status --json` first. Only when the status is `UNINITIALIZED`, run `bugx engine --root <ROOT> pre` to prepare a fresh project; do not run `bugx init` first when starting mining through a host plugin; it is a separate explicit CLI operation and does not replace the status/pre state machine. If the status is `FATAL_BLOCKED`, another blocked state, or the status check fails, preserve the state and stop; never use `init`, `reset`, or `--force` to bypass the gate.
- **Reproduction and Mutation**: `bugx test-generate --root <ROOT> --scope <PATH::SYM> --out <FILE>` / `bugx test-verify verify --scope <SCOPE>`
- **Supervisor and Concurrency**: `bugx supervisor status --root <ROOT>`
- **Telemetry and Logs**: `bugx telemetry inspect` / `bugx log --root <ROOT> --errors`
- **Dual Reports and Preview**: `bugx report both --eval-dir evals/oss --out evals/oss` / `bugx report serve --port 18991`

### 4. Compact Command Radar (41 Top-level Commands Classified Index)
To invoke any command below, inspect exact parameters dynamically via `bugx help <cmd>`; never guess:
- **Diagnostics and Inspection (9)**: `help`, `doctor`, `hosts`, `completion`, `log`, `stats`, `view`, `project`, `self-audit`
- **Core Lifecycle and Hosts (14)**: `init`, `engine`, `supervisor`, `daemon`, `config`, `reset`, `install`, `uninstall`, `dsh`, `plugin`, `mcp`, `mcp-runtime`, `capability`, `logic-analysis`
- **Deep Audit and Verification (10)**: `audit`, `probe`, `test-generate`, `test-audit`, `test-verify`, `patch-forge`, `test-strategy`, `fuzz-input`, `differential-probe`, `mining-audit`
- **Knowledge Flywheel and Delivery (8)**: `report`, `lesson`, `knowledge`, `skill`, `cloud`, `auth`, `telemetry`, `advisory`

### 5. Mandatory Rules and Deprecated Command Redirection
- **Gate Blockage Forbids Destructive Repairs**: `FATAL_BLOCKED` must never be bypassed using `init`, `reset`, or `--force`; preserve logs and state first.
- **Deprecated Command Redirections**: `matrix` -> `bugx audit`; `console` -> `bugx report serve`; `watch` -> `bugx log --watch`; `test` is an internal script, use `scripts/test_selector.py` instead.
