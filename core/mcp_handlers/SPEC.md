# mcp_handlers - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Model Context Protocol (MCP) Request Handlers
- **Package Path**: `core/mcp_handlers`
- **Core Mission**: Routes, validates, and dispatches MCP tool calls (code generation, db queries, logs, server execution).
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by BugX MCP Server daemon.
- **Outbound Dependencies**: Executes domain tools and queries state/ledger.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: All tool parameters must be schema-validated before dispatch. Failures return structured MCP error responses.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.mcp_handlers import dispatch_tool_call
result = dispatch_tool_call('health_snapshot', {})
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/mcp_handlers --write`.
