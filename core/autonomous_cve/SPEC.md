# autonomous_cve - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Autonomous CVE Pipeline
- **Package Path**: `core/autonomous_cve`
- **Core Mission**: Autonomous CVE reproduction, dynamic target probing, and verifiable PoC execution.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by launch phases and supervisor during autonomous vulnerability discovery runs.
- **Outbound Dependencies**: Calls probes, ledger, and execution sandboxes.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: PoC execution must be sandboxed; no untrusted payload execution outside designated sandbox directory.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.autonomous_cve import AutonomousRunner
# Initialize and execute bounded reproduction
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/autonomous_cve --write`.
