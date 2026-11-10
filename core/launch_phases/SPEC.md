# launch_phases - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Core Launch Lifecycle Phases
- **Package Path**: `core/launch_phases`
- **Core Mission**: Orchestrates multi-phase lifecycle: pre-execution gates, worker spawning, monitoring, and post-execution settlement.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by `bugx run` CLI entrypoint and programmatic orchestrators.
- **Outbound Dependencies**: Coordinates supervisor, ledger, probes, and report engine.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Phases execute strictly in sequence: pre -> orchestrate -> settle -> post. Early abort must cleanly finalize receipts.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.launch_phases import run_pre_phase, run_post_phase
# Execute standardized lifecycle phases
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/launch_phases --write`.
