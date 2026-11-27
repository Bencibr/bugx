# Qoder BugX Connector Guide

This document describes how to connect Qoder to BugX via the Model Context Protocol (MCP).

## Overview

BugX registers three local stdio MCP servers for Qoder.
1. `bugx-supervisor`: Main agent control plane for session lifecycle, gating, unit orchestration, and report generation.
2. `bugx-supervisor-worker`: Dedicated worker channel for concurrent audit tasks, claims, and checkpoints.
3. `bugx-docs`: Project documentation lookup.

## Configuration

### Project-level Configuration (`.mcp.json`)

Run `bugx install --agent qoder` from the project root. The installer merges the BugX servers into `.mcp.json` and retains the other project connectors and settings.

```json
{
  "mcpServers": {
    "bugx-supervisor": {
      "command": "bugx",
      "args": ["mcp", "serve", "--root", "/absolute/project/path", "--role", "host"]
    },
    "bugx-supervisor-worker": {
      "command": "bugx",
      "args": ["mcp", "serve", "--root", "/absolute/project/path", "--role", "worker"]
    },
    "bugx-cloud": {
      "command": "bugx",
      "args": ["mcp", "docs", "serve", "--root", "/absolute/project/path", "--db", ".bugx/knowledge/docs.sqlite3"]
    }
  }
}
```

Reopen the workspace and start a new session after installation. Approve the project servers in Qoder when prompted. The absolute project path binds the servers to the workspace even when Qoder launches them from its application directory. On Windows the installer uses the absolute Python executable and adds `-P -m bugx_cli.cli` before the arguments in the example. npm installs also supply their own module and resource paths. Qoder also reads project settings in `.qoder/settings.json`. When both files define a server, the root `.mcp.json` takes precedence. [Qoder MCP reference](https://docs.qoder.com/cli/mcp-reference)

## Security & Permissions

- **Local execution.** The three MCP servers communicate with Qoder over local stdio pipes.
- **Root Enforcement**: All commands and files are scoped to the current project workspace.
