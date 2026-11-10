# retrieval - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Context Retrieval & Knowledge Search
- **Package Path**: `core/retrieval`
- **Core Mission**: AST code navigation, codebase-memory graph indexing, keyword search, and contextual evidence assembly.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by worker prompts, MCP tools, and autonomous triage.
- **Outbound Dependencies**: Reads repository files, AST nodes, and graph databases.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Search must respect repository boundaries and .gitignore ignore rules.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.retrieval import search_context
# Retrieve contextual code snippets by semantic query
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/retrieval --write`.
