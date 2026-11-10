"""core/repair - 缺陷修复、RSI 闭环与自愈领域子系统。

收拢缺陷自动修复、递归自我提升 (RSI)、测试用例生成、质量量化审计、
最小化 PoC 精简、补丁合成与运行时就地自愈。
"""

from __future__ import annotations

from .fix_watchlist import WatchError, check as check_watchlist
from .l7_dynamic import (
    check_cli_runs,
    check_engine_protocol_loads,
    check_install_writes_declared_paths,
    check_state_readback,
    run_dynamic_checks,
)
from .l7_repair_loop import apply_candidate, build_work_order as build_l7_work_order, run_once as run_l7_repair_once
from .l7_selfcheck import (
    check_contract_semantic_depth,
    check_dangling_asset_references,
    check_error_messages_self_healing,
    check_guard_bypass_reachability,
    check_host_capability_parity,
    check_mirror_consistency,
    check_silent_skip_visibility,
    run_all as run_l7_selfcheck,
)
from .minimize_repro import Minimizer, minimize_repro_input, synthesize_standalone_repro
from .patch_synthesizer import PatchSynthesizer, PatchVerificationResult
from .regression_pack import PackError, build as build_regression_pack, run as run_regression_pack
from .rsi_fixer import attempt_fix, build_fix_prompt
from .rsi_loop import repair_then_recheck, run_loop as run_rsi_loop, solidify_round
from .rsi_repair import Diagnosis, build_work_order as build_rsi_work_order, classify as classify_rsi, repair_noise
from .rsi_rounds import diff_rounds
from .rsi_scope import (
    ScopeViolation,
    assert_allowed_paths,
    check_assertion_change,
    current_mode,
    mode_description,
    record_decision,
    snapshot,
)
from .self_heal import (
    Attempt,
    Budget,
    HealReport,
    Plan,
    append_receipt,
    classify_failure,
    heal_tool,
    plans_for,
    receipt_path,
    resolve_budget,
)
from .test_generator import (
    ContextualTestSynthesizer,
    GeneratorError,
    ParameterInfo,
    TargetSymbol,
    TypeDescriptor,
    extract_target_symbol,
    generate_standalone_poc,
    generate_unittest_suite,
    parse_type_annotation,
    verify_code_syntax,
)
from .test_quality_auditor import (
    AuditReport,
    AuditorError,
    DimensionScore,
    TestQualityAuditor,
)
from .test_strategy import (
    StrategyError,
    build_plan as build_test_plan,
    calculate_unit_risk_score,
    risk_score_to_difficulty,
)

__all__ = [
    # fix_watchlist
    "WatchError",
    "check_watchlist",
    # l7_dynamic
    "check_cli_runs",
    "check_engine_protocol_loads",
    "check_install_writes_declared_paths",
    "check_state_readback",
    "run_dynamic_checks",
    # l7_repair_loop
    "apply_candidate",
    "build_l7_work_order",
    "run_l7_repair_once",
    # l7_selfcheck
    "check_contract_semantic_depth",
    "check_dangling_asset_references",
    "check_error_messages_self_healing",
    "check_guard_bypass_reachability",
    "check_host_capability_parity",
    "check_mirror_consistency",
    "check_silent_skip_visibility",
    "run_l7_selfcheck",
    # minimize_repro
    "Minimizer",
    "minimize_repro_input",
    "synthesize_standalone_repro",
    # patch_synthesizer
    "PatchSynthesizer",
    "PatchVerificationResult",
    # regression_pack
    "PackError",
    "build_regression_pack",
    "run_regression_pack",
    # rsi_fixer
    "attempt_fix",
    "build_fix_prompt",
    # rsi_loop
    "repair_then_recheck",
    "run_rsi_loop",
    "solidify_round",
    # rsi_repair
    "Diagnosis",
    "build_rsi_work_order",
    "classify_rsi",
    "repair_noise",
    # rsi_rounds
    "diff_rounds",
    # rsi_scope
    "ScopeViolation",
    "assert_allowed_paths",
    "check_assertion_change",
    "current_mode",
    "mode_description",
    "record_decision",
    "snapshot",
    # self_heal
    "Attempt",
    "Budget",
    "HealReport",
    "Plan",
    "append_receipt",
    "classify_failure",
    "heal_tool",
    "plans_for",
    "receipt_path",
    "resolve_budget",
    # test_generator
    "ContextualTestSynthesizer",
    "GeneratorError",
    "ParameterInfo",
    "TargetSymbol",
    "TypeDescriptor",
    "extract_target_symbol",
    "generate_standalone_poc",
    "generate_unittest_suite",
    "parse_type_annotation",
    "verify_code_syntax",
    # test_quality_auditor
    "AuditReport",
    "AuditorError",
    "DimensionScore",
    "TestQualityAuditor",
    # test_strategy
    "StrategyError",
    "build_test_plan",
    "calculate_unit_risk_score",
    "risk_score_to_difficulty",
]
