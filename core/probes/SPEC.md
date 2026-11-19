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
- Verification is performed with short-lived tests outside the repository; no persistent `tests/` registry is retained.
- Validation runners and fixtures are process artifacts under `/tmp/bugx-tests-*`; they are
  physically removed after the verification window and are not a repository interface.
- `capture_browser_page` is shared by `headless_browser_action` and `bugx probe ui`. The UI
  audit owns its browser contexts, records native zoom, and adds computed style, layout and
  explicit state and keyboard assertions to the same screenshot receipt. The packaged
  axe-core bundle runs in the rendered context. The Web conformance reader verifies the
  run identity, manifest, screenshot hashes and scoped outcomes.
- Storybook inputs bind the index digest and preview ID to each captured story. The
  token importer resolves declared same-file aliases in project JSON and adds its file
  digest and CSS mapping to the design receipt.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/probes --write`.
