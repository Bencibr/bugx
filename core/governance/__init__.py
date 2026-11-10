#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/governance - BugX 治理、预算、容量规划与安全守卫领域子包。

集中管理 BugX 运行时的资源限制、安全隔离与治理门禁：
- token_budget: LLM Token 预算分配与估算
- capacity_planner: 系统资源感知与 Worker 并发容量动态规划
- goal_governor: 目标状态收敛判定、故障分类与隔离自愈
- guide_budget: 审计引导与上下文切片预算守卫
- dependency_lock: 依赖锁定、版本校验与可信安装隔离
- secret_redactor: 敏感凭据/密钥/个人信息检测与语法感知脱敏
- readonly_enforcer: 源码只读挂载与工作区未篡改一致性校验
- runtime_tool_gate: 活跃阶段必选运行时工具门禁校验
"""

from __future__ import annotations

import sys
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from .capacity_planner import effective_worker_concurrency
from .dependency_lock import (
    DependencyLockError,
    LockedRequirement,
    canonical_name,
    install_locked,
    load_trusted_lock,
    parse_lock_text,
    sanitized_pip_env,
    select_installable,
)
from .goal_governor import (
    ClassificationResult,
    FailureKind,
    GoalFailureClassifier,
    GoalOracle,
    GoalProgress,
    GoalSpec,
    QuarantineManager,
    SelfHealingController,
)
from .guide_budget import (
    check_budget_compliance,
    measure,
    modal_density_report,
    recommend_budget,
    slice_guide_context,
)
from .readonly_enforcer import (
    prepare_readonly_view,
    release_readonly_view,
    snapshot_source_state,
    verify_untouched,
)
from .runtime_tool_gate import (
    enforce_required_runtime_tools,
    required_tool_ids_for_active_stages,
)
from .secret_redactor import (
    SecretRedactionError,
    SecretRedactor,
    contains_unredacted_secret,
    contains_unredacted_secrets,
    find_unredacted_secrets,
    is_valid_chinese_id,
    is_valid_luhn,
    load_redact_config,
    redact,
    redact_payload,
    redact_records,
    redact_text,
)
from .token_budget import budget_summary, estimate_tokens

__all__ = [
    # token_budget
    "estimate_tokens",
    "budget_summary",
    # capacity_planner
    "effective_worker_concurrency",
    # goal_governor
    "GoalSpec",
    "GoalProgress",
    "FailureKind",
    "ClassificationResult",
    "QuarantineManager",
    "GoalOracle",
    "GoalFailureClassifier",
    "SelfHealingController",
    # guide_budget
    "slice_guide_context",
    "recommend_budget",
    "modal_density_report",
    "measure",
    "check_budget_compliance",
    # dependency_lock
    "DependencyLockError",
    "LockedRequirement",
    "canonical_name",
    "parse_lock_text",
    "load_trusted_lock",
    "select_installable",
    "sanitized_pip_env",
    "install_locked",
    # secret_redactor
    "SecretRedactionError",
    "SecretRedactor",
    "is_valid_chinese_id",
    "is_valid_luhn",
    "load_redact_config",
    "redact_text",
    "redact",
    "redact_payload",
    "find_unredacted_secrets",
    "contains_unredacted_secrets",
    "contains_unredacted_secret",
    "redact_records",
    # readonly_enforcer
    "snapshot_source_state",
    "verify_untouched",
    "prepare_readonly_view",
    "release_readonly_view",
    # runtime_tool_gate
    "required_tool_ids_for_active_stages",
    "enforce_required_runtime_tools",
]
