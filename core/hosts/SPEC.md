# hosts - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Host Adapter Registry & Implementations
- **Package Path**: `core/hosts`
- **Core Mission**: Extensible plugin registry and concrete adapters for IDE/Agent hosts (OpenCode, Claude, Codex, DSH, etc.).
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by supervisor host arbitration and CLI worker launcher.
- **Outbound Dependencies**: Standard host plugin contracts (HostPlugin, HostCapability).

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Zero-configuration discovery: adding a module in core/hosts/ registers the host. No hardcoded host switches.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.hosts import get_host_plugin
plugin = get_host_plugin('claude')
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/hosts --write`.
