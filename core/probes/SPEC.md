# probes - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Domain Dynamic Probes System
- **Package Path**: `core/probes`
- **Core Mission**: Domain-specialized dynamic black-box probes (Web, Android, WeChat Mini Program) with static fallback.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by autonomous CVE pipeline, audit workers, and supervisor verification.
- **Outbound Dependencies**: External runtimes (Playwright, adb, mini-program devtools).

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Graceful degradation: if dynamic runtime is unavailable, probe must report static degradation cleanly.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.probes import WebDynamicProbe
probe = WebDynamicProbe()
res = probe.execute_probe('http://localhost:8080')
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/probes --write`.
