# project_profiler - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Deterministic Project Profiling & Classification Engine
- **Package Path**: `core/project_profiler`
- **Core Mission**: Non-intrusive, bounded fact discovery, heuristic root anchoring, AI arbitration, and audit strategy compilation for target codebases.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by `core.project_profile` (thin facade), `launch_phases`, `capability_plan`, `bugx_cli`, and webui inspectors.
- **Outbound Dependencies**: `symbol_index` for scope filtering, `secure_atomic` for cache safety, `code_analyzer` for code volume metrics, `sca_scanner` for dependency signals.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Zero target execution: profiling must never execute target project code. Read budgets and file limits must be bounded.
- **Engine Isolation**: Never write `.bugx` state into the engine directory itself (`_is_engine_dir`).
- **Cache Integrity**: Detection version hash embeds both rule table digests and source digests so rule changes automatically invalidate old caches.

## 4. Usage Patterns & Quickstart
```python
from core.project_profiler import detect_project_profile, compile_audit_strategy

profile = detect_project_profile(project_root)
strategy = compile_audit_strategy(profile)
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/project_profiler --write`.
