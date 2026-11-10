"""Pinned top-level package specs used by optional-tool probes and self-heal."""

OPTIONAL_TOOL_PACKAGE_SPECS = {
    "codebase-memory": "codebase-memory-mcp==0.12.1",
    "ast-grep": "ast-grep-cli==0.45.1",
    "semgrep": "semgrep==1.182.0",
    "bandit": "bandit==1.9.4",
}

# Only these probe capabilities may trigger self-healing. Values are trusted
# registry keys, never package names copied from probe output.
PROBE_REPAIR_TARGETS = {
    "codebase-memory": ("codebase-memory",),
    "efficiency": ("ast-grep",),
    "sast": ("bandit", "semgrep"),
    "bandit": ("bandit",),
    "semgrep": ("semgrep",),
}
