# Qoder BugX Connector Guide

This document describes how to connect Qoder to BugX via the Model Context Protocol (MCP).

## Overview

BugX provides two local stdio MCP channels for Qoder:
1. `bugx-supervisor`: Main agent control plane for session lifecycle, gating, unit orchestration, and report generation.
2. `bugx-supervisor-worker`: Dedicated worker channel for concurrent audit tasks, claims, and checkpoints.

## Configuration

### Project-level Configuration (`.qoder/mcp.json` or `.mcp.json`)

When you run `bugx install --agent qoder`, the project configuration is automatically created:

```json
{
  "mcpServers": {
    "bugx-supervisor": {
      "command": "bugx",
      "args": ["mcp", "serve", "--root", ".", "--role", "host"]
    },
    "bugx-supervisor-worker": {
      "command": "bugx",
      "args": ["mcp", "serve", "--root", ".", "--role", "worker"]
    }
  }
}
```

### Global User-level Configuration (`~/.qoder/settings.json`)

To add the BugX connector globally across all projects, add the following to `~/.qoder/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "bugx-supervisor": {
      "command": "bugx",
      "args": ["mcp", "serve", "--root", ".", "--role", "host"]
    },
    "bugx-supervisor-worker": {
      "command": "bugx",
      "args": ["mcp", "serve", "--root", ".", "--role", "worker"]
    }
  }
}
```

## Security & Permissions

- **Local Execution**: Both MCP servers run locally via stdio pipes; no open network ports or credentials are required.
- **Root Enforcement**: All commands and files are scoped to the current project workspace.
