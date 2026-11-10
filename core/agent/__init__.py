"""core/agent - 智能体编排与工作区领域子包.

收拢与自主智能体（Agent）生命周期直接相关的核心能力：
- agent_artifacts: 产物规范、校验与哈希封签
- agent_eval: 智能体行为评测与打分体系
- agent_execution: 计划执行、超时守护与进程组回收
- agent_orchestrator: 多智能体协同微内核与图调度
- agent_targets: 项目多语言自适应与攻击面切片
- agent_workspace: 隔离工作空间与文件所有权边界
"""

from __future__ import annotations

from .agent_artifacts import (
    AgentArtifact,
    ArtifactError,
    ArtifactPathError,
    ArtifactReference,
    ArtifactType,
    ArtifactValidationError,
    BugCandidate,
    BugCategory,
    CheckResult,
    PatchCandidate,
    ReproCase,
    ReviewResult,
    RunReport,
    canonical_json_bytes,
    compute_artifact_digest,
    seal_artifact,
    validate_artifact,
)
from .agent_eval import (
    EvalError,
    baseline_failures,
    canonical_digest,
    finding_violations,
    invalid_evidence_ids,
    load_json,
    metrics_record,
    score,
    threshold_failures,
    validate_cases,
    validate_findings,
)
from .agent_execution import (
    CheckPlan,
    CheckPlanError,
    ExecutionError,
    ExecutionObservation,
    ExecutionOutcomeUnknown,
    TargetCallEvidence,
    TargetCallTarget,
    execute_check_plan,
    extract_target_call_evidence,
    validate_check_plan,
)
from .agent_orchestrator import (
    AgentOrchestrator,
    GraphError,
    assemble_task_prompt,
)
from .agent_targets import (
    RegressionSelection,
    TargetDiagnostic,
    TargetProfile,
    TargetRunSummary,
    adapt_node_project,
    adapt_project,
    adapt_python_project,
    analyze_affected_paths,
    parse_node_observation,
    parse_python_observation,
)
from .agent_workspace import (
    AgentWorkspace,
    AgentWorkspaceError,
    OriginWriteViolationError,
    TargetDriftError,
    WorkspaceOwnershipError,
)

__all__ = [
    # agent_artifacts
    "AgentArtifact",
    "ArtifactError",
    "ArtifactPathError",
    "ArtifactReference",
    "ArtifactType",
    "ArtifactValidationError",
    "BugCandidate",
    "BugCategory",
    "CheckResult",
    "PatchCandidate",
    "ReproCase",
    "ReviewResult",
    "RunReport",
    "canonical_json_bytes",
    "compute_artifact_digest",
    "seal_artifact",
    "validate_artifact",
    # agent_eval
    "EvalError",
    "baseline_failures",
    "canonical_digest",
    "finding_violations",
    "invalid_evidence_ids",
    "load_json",
    "metrics_record",
    "score",
    "threshold_failures",
    "validate_cases",
    "validate_findings",
    # agent_execution
    "CheckPlan",
    "CheckPlanError",
    "ExecutionError",
    "ExecutionObservation",
    "ExecutionOutcomeUnknown",
    "TargetCallEvidence",
    "TargetCallTarget",
    "execute_check_plan",
    "extract_target_call_evidence",
    "validate_check_plan",
    # agent_orchestrator
    "AgentOrchestrator",
    "GraphError",
    "assemble_task_prompt",
    # agent_targets
    "RegressionSelection",
    "TargetDiagnostic",
    "TargetProfile",
    "TargetRunSummary",
    "adapt_node_project",
    "adapt_project",
    "adapt_python_project",
    "analyze_affected_paths",
    "parse_node_observation",
    "parse_python_observation",
    # agent_workspace
    "AgentWorkspace",
    "AgentWorkspaceError",
    "OriginWriteViolationError",
    "TargetDriftError",
    "WorkspaceOwnershipError",
]
