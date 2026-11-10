# cloud_sync - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Cloud Synchronization Client
- **Package Path**: `core/cloud_sync`
- **Core Mission**: Report synchronization, telemetry, remote licensing, and offline receipt queueing.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by report sharing, launch post-phase, and supervisor settlement.
- **Outbound Dependencies**: Standard HTTP client communicating with BugX cloud endpoints.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Must gracefully queue telemetry when network is unavailable; never raise uncaught exceptions to block local audit.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.cloud_sync import sync_report
# Sync local audit receipt to configured cloud backend
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/cloud_sync --write`.
