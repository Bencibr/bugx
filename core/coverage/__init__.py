"""core/coverage - 代码覆盖率与分析领域子系统。

负责代码最小单元发现、业务链路发现、覆盖证据链校验、
防恶化门禁、AI覆盖复核及跨模块业务覆盖度量。
"""

from __future__ import annotations

# Sibling module imports
from core.coverage import (
    coverage_ai_review,
    coverage_backfill,
    coverage_bootstrap,
    coverage_business,
    coverage_cli,
    coverage_discovery,
    coverage_evidence,
    coverage_forensics,
    coverage_gate,
    coverage_intelligence,
    coverage_manifest,
    coverage_platform,
    coverage_validate,
    module_coverage,
)

# Export key classes and functions
from core.coverage.coverage_bootstrap import (
    CoverageBootstrapError,
    bootstrap,
    inspect,
    manifest_path,
)
from core.coverage.coverage_evidence import (
    EvidenceStore,
    EvidenceRecord,
    EvidenceError,
    EvidenceValidationError,
    attach_attack_graph_snapshot,
    load_attack_graph_snapshot,
)
from core.coverage.coverage_forensics import (
    converge_forensics,
    coverage_debt_units,
)
from core.coverage.coverage_manifest import (
    ModuleRow,
    parse_manifest,
    default_manifest_for_root,
)
from core.coverage.coverage_discovery import (
    coverage_unit_id,
    discovery_rows,
    extract_feature_surface,
    business_discovery,
)
from core.coverage.coverage_gate import (
    manifest_gate_errors,
    startup_gate_errors,
)
from core.coverage.coverage_intelligence import (
    CoverageError,
    coverage_gaps,
)

__all__ = [
    # Submodules
    "coverage_ai_review",
    "coverage_backfill",
    "coverage_bootstrap",
    "coverage_business",
    "coverage_cli",
    "coverage_discovery",
    "coverage_evidence",
    "coverage_forensics",
    "coverage_gate",
    "coverage_intelligence",
    "coverage_manifest",
    "coverage_platform",
    "coverage_validate",
    "module_coverage",
    # Key classes & functions
    "CoverageBootstrapError",
    "bootstrap",
    "inspect",
    "manifest_path",
    "EvidenceStore",
    "EvidenceRecord",
    "EvidenceError",
    "EvidenceValidationError",
    "attach_attack_graph_snapshot",
    "load_attack_graph_snapshot",
    "converge_forensics",
    "coverage_debt_units",
    "ModuleRow",
    "parse_manifest",
    "default_manifest_for_root",
    "coverage_unit_id",
    "discovery_rows",
    "extract_feature_surface",
    "business_discovery",
    "manifest_gate_errors",
    "startup_gate_errors",
    "CoverageError",
    "coverage_gaps",
]
