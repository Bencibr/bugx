---
title: BugX Documentation Hub
audience: Public users
status: DOCUMENTATION_UNVERIFIED
version: 0.1.0
last-verified: 2026-11-10
---

# BugX Documentation Hub

BugX is an evidence-driven code-audit and defect-mining agent for AI-assisted development. The current documentation follows the source tree and distinguishes implementation facts from environment-dependent verification.

## Quick start

Requirements:

- Python 3.11 or newer for the Python package and CLI.
- Node.js 18 or newer for the npm wrapper.

Declared package entry points:

    python -m pip install bugx-cli
    npm install -g bugx-cli

Initialize a project before starting the engine:

    bugx init --root . --cloud-sync local
    bugx install --root . --agent opencode
    bugx doctor --root . --strict
    bugx engine --root . status

## Host capability boundary

There are 12 install/detection adapter names, 7 ready engine plugins, 2 ACP-ready hosts, and 6 autonomous CLI choices. These sets are intentionally different. The current ACP-ready hosts are Cline and OpenCode. The autonomous CLI choices are Claude, Codex, OpenCode, DSH, Cline, and ZCode. An install adapter is not proof of ACP or autonomous support.

## Public manuals

- [Chinese user guide](01_guides/01_USER_GUIDE.md): installation, configuration, lifecycle, MCP/ACP, cloud scope, reports, and recovery.
- [CLI command reference](01_guides/02_COMMANDS.md): the current 43-command parser surface, options, defaults, scopes, side effects, and exit behavior.
- Topic cookbooks ([Cloud Auth](01_guides/04_CLOUD_AUTH_AND_SYNC_COOKBOOK.md), [Host Matrix](01_guides/05_HOST_MATRIX_AND_DISPATCH.md), [Probes & PoC](01_guides/06_PROBES_AND_POC_COOKBOOK.md), [Lifecycle States](01_guides/07_LIFECYCLE_AND_RECOVERY_STATES.md), [Lessons & Privacy](01_guides/08_LESSONS_AND_PRIVACY_BOUNDARY.md)): indexed offline in `core/bundled_docs/` and hot-synced via `bugx-docs`.
- [Troubleshooting and recovery](01_guides/03_TROUBLESHOOTING.md): symptom-driven evidence collection and safe next steps.
- [Environment variables](04_reference/01_ENVIRONMENT_VARIABLES.md): shell setup, scope, sensitive boundaries, and source inventory.
- [AI reference](03_ai/01_AI_REFERENCE.md): machine-oriented MCP, lifecycle, evidence, state, and failure contract.
- [Architecture](02_architecture/README.md): system design and data-flow documentation.
- [Release notes](05_release-notes/README.md): release documentation.

## Completion boundary

Model text, a parser result, an MCP schema, or an audit state of ready does not prove completion. A completed lifecycle requires real Host/Worker identity, claim, worker-start, tool calls or heartbeats, checkpoint receipts, Finding decisions, coverage, finish, post, and integrity evidence. Real ACP, autonomous, remote-sync, and cross-platform acceptance remains environment-specific.
