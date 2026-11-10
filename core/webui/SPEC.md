# webui - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Interactive WebUI & Dashboard Server
- **Package Path**: `core/webui`
- **Core Mission**: FastAPI/Starlette local web server providing mining status, visual architecture graph, and workbench controls.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Started via `bugx report serve` or background daemon.
- **Outbound Dependencies**: Serves frontend static assets (dist/) and exposes REST/WebSocket APIs.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Port conflict must fail gracefully; binds locally (127.0.0.1) by default for security.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.webui import create_app
app = create_app()
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/webui --write`.
