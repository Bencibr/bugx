# supervisor - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: External Supervisor & Worker Governance
- **Package Path**: `core/supervisor`
- **Core Mission**: Out-of-process supervisor governing worker leases, budgets, finish policy gates, and finding settlement.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by worker orchestrators and CLI supervisor runner.
- **Outbound Dependencies**: Governs workers via ACP and heartbeat monitors.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Fail-safe lease reclamation: dead workers are evicted upon lease expiration without leaving zombie locks.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.supervisor import claim_unit, submit_checkpoint
# Supervise worker execution lease and verify checkpoint
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/supervisor --write`.
