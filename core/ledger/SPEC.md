# ledger - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Immutable Audit Ledger & Receipt Store
- **Package Path**: `core/ledger`
- **Core Mission**: Append-only storage for findings, evidence checkpoints, audit trails, and execution receipts.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by supervisor, worker heartbeats, and report engine.
- **Outbound Dependencies**: Local SQLite / JSONL state persistence in .bugx/ directory.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Append-only integrity: completed records cannot be mutated. Schema migrations must be monotonic.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.ledger import record_finding, load_session_ledger
# Persist verifiable finding with evidence hash
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/ledger --write`.
