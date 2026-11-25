"""Pinned top-level package specs used by required and optional tool probes."""

REQUIRED_TOOL_PACKAGE_SPECS = {
    "codebase-memory": "codebase-memory-mcp==0.12.1",
    "tree-sitter": "tree-sitter-language-pack==1.14.3",
}

OPTIONAL_TOOL_PACKAGE_SPECS = {
    "ast-grep": "ast-grep-cli==0.45.1",
    "semgrep": "semgrep==1.182.0",
    "bandit": "bandit==1.9.4",
}

TOOL_PACKAGE_SPECS = {
    **REQUIRED_TOOL_PACKAGE_SPECS,
    **OPTIONAL_TOOL_PACKAGE_SPECS,
}

# Only these probe capabilities may trigger self-healing. Values are trusted
# registry keys, never package names copied from probe output.
PROBE_REPAIR_TARGETS = {
    "codebase-memory": ("codebase-memory",),
    "tree-sitter": ("tree-sitter",),
    "efficiency": ("ast-grep",),
    "sast": ("bandit", "semgrep"),
    "bandit": ("bandit",),
    "semgrep": ("semgrep",),
}
