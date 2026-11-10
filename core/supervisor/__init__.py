#!/usr/bin/env python3
"""core/supervisor - BugX 外部 Supervisor 领域服务子包 (DOC-070)。

分层解耦 Supervisor 核心职责：
- lease_manager: Worker 租约凭证、心跳、超时与资源争用管理；
- budget_governor: 预算软硬门禁、耗尽处理、并发对齐与僵尸检测；
- checkpoint_pipeline: Checkpoint 验证、签名提取、证据沉淀与发布。
- finish_policy: finish 终态门禁的纯分类规则。
- audit_supervisor: 会话生命周期守护主进程。
- task_scheduler: 任务调度与敏感攻击面编排。
- supervisor_host: 宿主与 MCP 会话生命周期治理。
- supervisor_models: Supervisor 统一领域数据模型与状态定义。
- worker_checkpoint: Worker 检查点快照提交与前置校验。
- worker_execution: Worker 进程执行沙箱与有界终止。
- worker_handoff: 智能体 Worker 移交与任务装配。
- worker_runtime: Worker 运行时调度与环境隔离。
"""

from __future__ import annotations

from .lease_manager import (
    active_claim,
    active_resource_keys,
    append_event,
    claimed_units_this_round,
    drain_dangling_claims,
    eligible_units,
    normalize_checkpoint_id,
    normalize_ids,
    normalize_worker_id,
    mark_claim_expired,
    mark_worker_attempts_exhausted,
    release_claim,
    release_active_claims,
    renew_claim,
)
from .budget_governor import (
    ROUND_HEARTBEAT_TIMEOUT_SECONDS,
    close_parallel_round,
    deadline_reasons,
    ensure_live_for_work,
    observe_clock,
    parallel_gate_result,
    parallel_required,
    recalibrate_round_budget,
    reconcile_parallel,
    refresh,
    adaptive_lease_seconds,
    apply_status_budget,
    refresh_status_budget,
    set_budget_exhausted,
    soft_limit_reasons,
    validate_parallel_capacity,
    target_gate_result,
)
from .checkpoint_pipeline import (
    assert_target_fingerprint,
    confirmation_gate,
    coverage_evidence_path,
    finding_key,
    attach_evidence_receipts,
    normalize_coverage,
    normalize_evidence,
    enrich_gate_finding,
    normalize_findings,
    normalize_notes,
    normalize_signatures,
    publish_checkpoint_evidence,
    refresh_confirmation_gate,
    resolve_session_coverage_ledger_path,
    session_coverage_complete,
)
from .finish_policy import FinishDecision, classify_finish
from .state_store import StateStore
from .serial_authorization import (
    EVENT_TYPE as SERIAL_AUTHORIZATION_EVENT_TYPE,
    SCHEMA_VERSION as SERIAL_AUTHORIZATION_SCHEMA_VERSION,
    SerialAuthorizationError,
    issue_serial_authorization,
    validate_receipt_schema,
    validate_serial_authorization,
)
from .audit_supervisor import Supervisor, SupervisorError, RunBudget
from .supervisor_models import RunState, Status, WorkerClaim, resolve_system_concurrency
from . import (
    audit_supervisor,
    task_scheduler,
    supervisor_host,
    supervisor_models,
    worker_checkpoint,
    worker_execution,
    worker_handoff,
    worker_runtime,
)

__all__ = [
    # lease_manager
    "active_claim",
    "active_resource_keys",
    "append_event",
    "claimed_units_this_round",
    "drain_dangling_claims",
    "eligible_units",
    "normalize_checkpoint_id",
    "normalize_ids",
    "normalize_worker_id",
    "mark_claim_expired",
    "mark_worker_attempts_exhausted",
    "release_claim",
    "release_active_claims",
    "renew_claim",
    # budget_governor
    "close_parallel_round",
    "deadline_reasons",
    "ensure_live_for_work",
    "observe_clock",
    "parallel_gate_result",
    "parallel_required",
    "recalibrate_round_budget",
    "reconcile_parallel",
    "refresh",
    "adaptive_lease_seconds",
    "apply_status_budget",
    "refresh_status_budget",
    "ROUND_HEARTBEAT_TIMEOUT_SECONDS",
    "set_budget_exhausted",
    "soft_limit_reasons",
    "validate_parallel_capacity",
    "target_gate_result",
    # checkpoint_pipeline
    "assert_target_fingerprint",
    "confirmation_gate",
    "coverage_evidence_path",
    "finding_key",
    "normalize_coverage",
    "normalize_evidence",
    "enrich_gate_finding",
    "attach_evidence_receipts",
    "normalize_findings",
    "normalize_notes",
    "normalize_signatures",
    "publish_checkpoint_evidence",
    "refresh_confirmation_gate",
    "resolve_session_coverage_ledger_path",
    "session_coverage_complete",
    # finish_policy
    "FinishDecision",
    "classify_finish",
    # state_store
    "StateStore",
    # serial_authorization
    "SERIAL_AUTHORIZATION_EVENT_TYPE",
    "SERIAL_AUTHORIZATION_SCHEMA_VERSION",
    "SerialAuthorizationError",
    "issue_serial_authorization",
    "validate_receipt_schema",
    "validate_serial_authorization",
    # audit_supervisor & supervisor_models
    "Supervisor",
    "SupervisorError",
    "RunBudget",
    "RunState",
    "Status",
    "WorkerClaim",
    "resolve_system_concurrency",
    # modules
    "audit_supervisor",
    "task_scheduler",
    "supervisor_host",
    "supervisor_models",
    "worker_checkpoint",
    "worker_execution",
    "worker_handoff",
    "worker_runtime",
]
