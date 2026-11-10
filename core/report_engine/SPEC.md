# report_engine - Domain Specification (SPEC.md)

## 1. Responsibility & Domain Boundary
- **Title**: Verification & Reporting Engine
- **Package Path**: `core/report_engine`
- **Core Mission**: Aggregates findings, computes code volume, formats human/machine reports (HTML, Markdown, JSON), and hosts local report server.
- **Boundary**: All public capabilities must be formally declared in `INTERFACE.md`. Internal utilities must remain encapsulated.

## 2. Dependency & Interaction Topology
- **Inbound Invocations**: Called by launch post-phase, CLI `bugx report`, and cloud sync.
- **Outbound Dependencies**: Reads ledger and project metadata; outputs report bundles.

## 3. System Invariants & Failure Modes
- **Safety Invariant**: Every reported finding must trace to a Supervisor authority snapshot or a verified ledger entry, with hash evidence where the source provides one.
- **Resilience Policy**: Failures must be isolated within the domain and converted to structured exceptions or error receipts.

## 4. Usage Patterns & Quickstart
```python
from core.report_engine import ReportDataBundle, FormalExecutiveReportBuilder
# 默认读项目运行态 Supervisor 权威 findings（无则 gate_blockers 阻断会签）
bundle = ReportDataBundle.from_project(".")
report = FormalExecutiveReportBuilder(bundle)
open("report.md", "w", encoding="utf-8").write(report.render_markdown())
```

## 5. Maintenance & Verification
- Unit & regression tests for this package are registered in `tests/test_registry.json`.
- Changes to exported interfaces must be synchronized via `python3 scripts/gen_interface_spec.py --package core/report_engine --write`.
