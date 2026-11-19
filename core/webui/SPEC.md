# webui - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Interactive WebUI & Dashboard Server
- **Package Path**: `core/webui`
- **Core Mission**: FastAPI/Starlette local web server providing mining status, visual architecture graph, and workbench controls.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Started via `bugx report serve` or background daemon.
- **Outbound Dependencies**: Serves frontend static assets (dist/) and exposes REST/WebSocket APIs.
- **CSS Architecture Invariant (F-023)**: `design-tokens.css` 必须最先加载；分节样式（00-base…12-profile-tab）按序跟随；间距/字号/圆角/色板改动应消费 token（var(--sp-*/--fs-*/--radius-*/--bx-*）而非新增字面值；新增样式进对应分节文件，禁止复活 6458 行单文件模式。!important 属 override 层既有对抗产物（576 处，同 selector 零精确重复——已实测无可机械削减子集），新代码禁止新增。

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Port conflict must fail gracefully; binds locally (127.0.0.1) by default for security.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.webui import create_app
app = create_app()
```

## 5. Maintenance & Verification
- Verification is performed with short-lived tests outside the repository; no persistent `tests/` registry is retained.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/webui --write`.
