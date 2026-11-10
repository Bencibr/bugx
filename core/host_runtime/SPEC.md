# host_runtime - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Host Runtime & Environment Detector
- **Package Path**: `core/host_runtime`
- **Core Mission**: Detects host agent installations, checks tool version constraints, and normalizes paths.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called during CLI startup, doctor diagnostics, and host arbitration.
- **Outbound Dependencies**: Inspects host filesystem binaries and environment variables.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Read-only inspection; must fail-closed if required host capability is missing without fallback.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.host_runtime import detect_host_runtime
# Probe host agent runtime environment
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/host_runtime --write`.
