# dev_harness - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Development & E2E Testing Harness
- **Package Path**: `core/dev_harness`
- **Core Mission**: Controlled execution environment, mock fixtures, and protocol-level E2E assertion drivers.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Invoked exclusively by automated test suites and debug runners.
- **Outbound Dependencies**: Mocks host processes and supervisor communication channels.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Harness mocks must not leak into production execution paths.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.dev_harness import MockHostEnvironment
# Spin up hermetic testing harness
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/dev_harness --write`.
