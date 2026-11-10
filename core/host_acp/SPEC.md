# host_acp - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Agent Control Protocol (ACP) Gateway
- **Package Path**: `core/host_acp`
- **Core Mission**: Bidirectional communication gateway implementing the ACP contract between BugX supervisor and host agents.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by supervisor host and worker dispatch orchestrator.
- **Outbound Dependencies**: ACP socket / stdio transports for target host agents (Claude, OpenCode, Codex).

## 3. System Invariants & Failure Modes
- **Safety Invariant**: ACP state transitions must be monotonic: handshake -> active -> checkpoint -> terminal. No dangling claim reuse.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.host_acp import AcpSession
# Establish ACP connection and stream worker heartbeats
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/host_acp --write`.
