"""core/planning - 能力规划、领域路由、代码切片、规格采集与仲裁流水线领域子系统。

本子系统整合了 BugX 针对目标系统的全生命周期能力规划与上下文编排：
1. 能力规划与验证: capability_plan, capability_planner, test_capability_verifier
2. 领域路由与原型: domain_router, domain_archetype
3. 源码探测与切片: source_inspector, prompt_harness, dynamic_flow_analyzer, impact_pruner
4. 意图采集与裁决: spec_harvester, intent_adjudicator, judge_pipeline, logic_analysis
5. 执行靶场与单元契约: target_sandbox_manager, unit_kind
"""

from __future__ import annotations

# Sibling module imports
from core.planning import (
    capability_plan,
    capability_planner,
    domain_archetype,
    domain_router,
    dynamic_flow_analyzer,
    impact_pruner,
    intent_adjudicator,
    judge_pipeline,
    logic_analysis,
    prompt_harness,
    source_inspector,
    spec_harvester,
    target_sandbox_manager,
    test_capability_verifier,
    unit_kind,
)

# Export key classes and functions
from core.planning.capability_plan import build_plan
from core.planning.capability_planner import (
    optional_mcp_names,
    plan_runtime_capabilities,
)
from core.planning.domain_archetype import (
    SpecGap,
    analyze_domain_completeness,
    generate_domain_fixtures,
    inject_test_fixture,
    load_spec_gaps,
    provide_prerequisite_fixture,
    save_spec_gaps,
)
from core.planning.domain_router import DomainSpecializedRouter
from core.planning.dynamic_flow_analyzer import DynamicFlowAnalyzer, FlowVariable
from core.planning.impact_pruner import (
    ChangeEntry,
    compute_impact_network,
    extract_symbols_from_file,
    filter_paths_with_manifest,
    git_changed_files,
)
from core.planning.intent_adjudicator import adjudicate_finding_against_spec
from core.planning.judge_pipeline import (
    classify_unit,
    default_rules,
    dynamic_clean,
    harvest_feedback,
    load_rules,
    sentinel_fired,
    static_profile,
    symbol_source,
    unit_parts,
)
from core.planning.logic_analysis import (
    LogicAnalysisError,
    bind_finding,
    bind_task,
    build_bundle,
    build_evidence_packet,
    build_intent_hypothesis,
    build_logic_chain,
    build_oracle_spec,
    evidence_status,
    validate_bundle,
)
from core.planning.prompt_harness import (
    BudgetAllocationError,
    analyze_source_rules,
    build_domain_specialized_prompt,
    build_worker_prompt,
    detect_unit_rule_slice,
    enforce_global_budget,
    enforce_section_budget,
    get_prompts_dir,
    get_stage_metadata,
    get_stage_prompt,
    list_available_stages,
    load_rule,
    load_stage,
    route_source_inspection,
    stage_sections,
)
from core.planning.source_inspector import InspectResult, inspect_source
from core.planning.spec_harvester import (
    BusinessSpec,
    ExplicitInvariant,
    export_feature_inventory,
    harvest_specification,
    load_business_spec,
    save_business_spec,
)
from core.planning.target_sandbox_manager import (
    HealthStatus,
    SnapshotInfo,
    SnapshotRollbackError,
    TargetSandboxManager,
)
from core.planning.test_capability_verifier import (
    CapabilitySummary,
    Mutant,
    MutantExecutionResult,
    MutationSandboxRunner,
    SemanticMutator,
    TestCapabilityEvaluator,
    normalize_test_command,
    to_coverage_intelligence_format,
    verify_test_capability,
)
from core.planning.unit_kind import (
    PREFIXES,
    VALID_PHASES,
    UnitKind,
    format_unit_id,
    is_business,
    is_internal,
    parse_unit_id,
    strip_unit_prefix,
)

__all__ = [
    # Submodules
    "capability_plan",
    "capability_planner",
    "domain_archetype",
    "domain_router",
    "dynamic_flow_analyzer",
    "impact_pruner",
    "intent_adjudicator",
    "judge_pipeline",
    "logic_analysis",
    "prompt_harness",
    "source_inspector",
    "spec_harvester",
    "target_sandbox_manager",
    "test_capability_verifier",
    "unit_kind",
    # capability_plan & capability_planner
    "build_plan",
    "plan_runtime_capabilities",
    "optional_mcp_names",
    # domain_router & domain_archetype
    "DomainSpecializedRouter",
    "SpecGap",
    "analyze_domain_completeness",
    "generate_domain_fixtures",
    "inject_test_fixture",
    "load_spec_gaps",
    "provide_prerequisite_fixture",
    "save_spec_gaps",
    # dynamic_flow_analyzer & impact_pruner
    "DynamicFlowAnalyzer",
    "FlowVariable",
    "ChangeEntry",
    "compute_impact_network",
    "extract_symbols_from_file",
    "filter_paths_with_manifest",
    "git_changed_files",
    # intent_adjudicator & judge_pipeline & logic_analysis
    "adjudicate_finding_against_spec",
    "classify_unit",
    "default_rules",
    "dynamic_clean",
    "harvest_feedback",
    "load_rules",
    "sentinel_fired",
    "static_profile",
    "symbol_source",
    "unit_parts",
    "LogicAnalysisError",
    "bind_finding",
    "bind_task",
    "build_bundle",
    "build_evidence_packet",
    "build_intent_hypothesis",
    "build_logic_chain",
    "build_oracle_spec",
    "evidence_status",
    "validate_bundle",
    # prompt_harness & source_inspector
    "BudgetAllocationError",
    "analyze_source_rules",
    "build_domain_specialized_prompt",
    "build_worker_prompt",
    "detect_unit_rule_slice",
    "enforce_global_budget",
    "enforce_section_budget",
    "get_prompts_dir",
    "get_stage_metadata",
    "get_stage_prompt",
    "list_available_stages",
    "load_rule",
    "load_stage",
    "route_source_inspection",
    "stage_sections",
    "InspectResult",
    "inspect_source",
    # spec_harvester
    "BusinessSpec",
    "ExplicitInvariant",
    "export_feature_inventory",
    "harvest_specification",
    "load_business_spec",
    "save_business_spec",
    # target_sandbox_manager
    "HealthStatus",
    "SnapshotInfo",
    "SnapshotRollbackError",
    "TargetSandboxManager",
    # test_capability_verifier
    "CapabilitySummary",
    "Mutant",
    "MutantExecutionResult",
    "MutationSandboxRunner",
    "SemanticMutator",
    "TestCapabilityEvaluator",
    "normalize_test_command",
    "to_coverage_intelligence_format",
    "verify_test_capability",
    # unit_kind
    "PREFIXES",
    "VALID_PHASES",
    "UnitKind",
    "format_unit_id",
    "is_business",
    "is_internal",
    "parse_unit_id",
    "strip_unit_prefix",
]
