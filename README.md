---
title: BugX
audience: Public users
status: DOCUMENTATION_UNVERIFIED
version: 0.1.0
last-verified: 2026-11-10
description: "Evidence-driven AI code auditing and vulnerability hunting CLI for MCP/ACP coding agents, with local project state, worker orchestration, executable evidence, Findings, coverage, and recovery artifacts."
keywords: "AI code auditing, application security, vulnerability hunting, bug finding, security code review, MCP, ACP, Python CLI, Node.js CLI, npm, PyPI"
---

# BugX

BugX is a local-first Python/Node.js CLI for evidence-driven AI code auditing, application security review, vulnerability hunting, and verified bug reproduction. It keeps project state in a private `.bugx/` directory, coordinates supported MCP/ACP host and worker agents through a local supervisor, and records coverage, execution evidence, Findings, artifacts, and recovery state instead of treating model text as proof.

> **Search summary** — BugX helps developers and AI coding agents audit real code for security defects and deep bugs. Install it as `bugx-cli` from npm or PyPI, connect a supported host such as Claude Code, OpenAI Codex, OpenCode, Cline, or ZCode, and use structured lifecycle receipts, coverage, Findings, and reproducible artifacts to distinguish verified results from model text or a zero exit code.

This repository declares version `0.1.0`. The documentation matches the current source tree, but real host ACP, autonomous end-to-end runs, remote cloud synchronization, and cross-platform installation still require environment-specific verification.

## Requirements and installation

- Python `>=3.11` for the Python package and CLI.
- Node.js `>=18` when using the npm wrapper.

Declared package entry points:

```bash
python -m pip install bugx-cli
# or
npm install -g bugx-cli
```

Optional Python extras are available for selected capabilities: `tui`, `graph`, and `crypto`. They are not implicitly installed by the base package.

## Safe first run

From the project root, initialize local state first, then install only the host you intend to use:

```bash
bugx init --root . --cloud-sync local
bugx install --root . --agent opencode
bugx doctor --root . --strict
bugx engine --root . status
```

The `--agent` value must be one of the current install/detection adapters. Use `bugx hosts` to inspect the local host matrix. In a non-interactive shell, `bugx init` requires an explicit `--cloud-sync local|sync` choice unless a preset already supplies it.

The public host lists have different meanings: 12 names are install/detection adapters; 7 have ready engine plugins; ACP is currently ready only for Cline and OpenCode; the autonomous CLI choices are Claude, Codex, OpenCode, DSH, Cline, and ZCode. Do not infer full ACP or autonomous support from an install name.

## Run an audit

The public engine entry point is namespaced under `engine`:

```bash
bugx engine --root . pre
bugx engine --root . status --json
bugx engine --root . post
```

For goal-driven execution:

```bash
bugx autonomous --root . --host opencode
bugx goal --root . --host opencode "audit the project"
```

An exit code or model response is not, by itself, a completed audit. Completion requires the recorded lifecycle, coverage, evidence receipts, Finding decisions, and finish gate to agree. `bugx audit` may start a graph flow that is waiting for a native agent; `ready` is not the same as `complete`.

## Documents

- [User guide](docs/01_guides/01_USER_GUIDE.md): installation, configuration, host capabilities, lifecycle, MCP/ACP, cloud scope, results, and recovery.
- [Command reference](docs/01_guides/02_COMMANDS.md): parser-derived commands, options, defaults, scopes, side effects, and exit behavior.
- [Troubleshooting](docs/01_guides/03_TROUBLESHOOTING.md): symptom-driven diagnosis and recoverable next steps.
- [AI reference](docs/03_ai/01_AI_REFERENCE.md): machine-oriented MCP, lifecycle, state, evidence, and failure contract.
- [Documentation index](docs/README.md): architecture and document navigation.

## Data and safety boundaries

Project state is stored under `.bugx/`; global engine, credentials, knowledge, cache, telemetry, and cloud configuration use user-level BugX locations unless overridden by the documented environment variables. `auth status` reports credential presence and state, not secret contents. A model name is not a credential.

Cloud upload, feedback, diagnostic log reporting, and sync are explicit operations. Review `--scope`, consent, sensitive-data policy, and detail mode before using them. Reset, uninstall, purge, and `--no-backup` can remove data; use their dry-run and backup behavior deliberately.

## Development policy

Every code commit must update affected documentation (每次提交代码都必须同步更新). The semantic baseline remains `0.1.0`; version transitions require explicit human confirmation with `--confirm-version-transition`.

## License

See [LICENSE](LICENSE) for the repository license terms.
