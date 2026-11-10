---
title: BugX Documentation
audience: Public users and AI hosts
status: DOCUMENTATION_UNVERIFIED
version: 0.1.0
last-verified: 2026-11-10
---

# BugX Documentation

This public source copy contains the user manuals, AI-host contracts, architecture
overview, environment-variable reference, diagrams, and package distribution notes.
Runtime state is created under the target project's `.bugx/` directory when BugX is
initialized; it is intentionally not part of this source copy.

## Start here

- [User guide](01_guides/01_USER_GUIDE.md): installation, initialization, configuration,
  host setup, lifecycle, cloud scope, reports, cleanup, and recovery.
- [Command reference](01_guides/02_COMMANDS.md): parser-derived commands, options,
  defaults, scope, side effects, and exit behavior.
- [Troubleshooting](01_guides/03_TROUBLESHOOTING.md): symptom-driven diagnosis,
  evidence collection, recovery, and stop conditions.
- [Environment variables](04_reference/01_ENVIRONMENT_VARIABLES.md): shell setup,
  scope, sensitive boundaries, and source-grounded variable inventory.

## AI host integration

- [AI reference](03_ai/01_AI_REFERENCE.md): lifecycle, MCP, state, evidence,
  receipts, and failure contract.
- [AI usage](03_ai/02_AI_USAGE.md): host-facing usage patterns and decision rules.
- [AI commands](03_ai/03_AI_COMMANDS.md): command routing for an AI host.

## Architecture and evidence

- [Architecture](02_architecture/01_ARCHITECTURE.md): system layers and data flow.
- [Audit flow](02_architecture/03_BUGX_AUDIT_FLOW.md): pre, run, post, settlement,
  and evidence closure.
- [Diagrams](06_diagrams/): Mermaid sources and their SVG, PNG, and editable
  Excalidraw exports.

## Distribution

- [NPM distribution guide](07_distribution/01_README_NPM.md)
- [PyPI distribution guide](07_distribution/02_README_PYPI.md)

## Release boundary

This copy deliberately omits Git history, local host configuration, runtime state,
credentials, build output, acceptance evidence, internal development records, and
developer-only validation assets. Real host, ACP, cloud, and cross-platform behavior
remains environment-dependent and must not be inferred from documentation alone.
