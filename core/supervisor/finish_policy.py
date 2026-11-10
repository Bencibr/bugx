"""Pure finish classification for the Supervisor.

This module decides which terminal result a finish attempt should produce from
already-collected inputs.  It does not read files, acquire locks, mutate state,
or emit events; those effects remain owned by ``audit_supervisor.Supervisor``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class FinishDecision:
    """The side-effect-free result of evaluating finish gates."""

    status: str
    reason: str | None = None
    last_error: str | None = None
    budget_reasons: tuple[str, ...] = ()


def graph_completion(graph: object) -> tuple[bool, str | None]:
    """Return whether a graph goal has durable, goal-specific evidence.

    All Recon/Repro/Review tasks are treated as required when present, and a
    graph must contain at least one completed task for each stage plus a
    completed, revisioned report. Confirmed findings are valid adjudications
    for ``find_bugs``; their remediation is deliberately not consulted there.
    """
    goal = getattr(graph, "goal", None)
    tasks = getattr(graph, "tasks", {})
    bugs = getattr(graph, "bug_index", {})
    report_revision = getattr(graph, "report_revision", 0)
    if not isinstance(tasks, Mapping) or not isinstance(bugs, Mapping):
        return False, "invalid_graph_state"
    task_values = list(tasks.values())
    for required_stage in ("recon", "repro", "review"):
        stage_tasks = [task for task in task_values if getattr(task, "stage", None) == required_stage]
        if not stage_tasks or any(
            getattr(task, "state", None) != "completed"
            or not getattr(task, "result_digest", None)
            for task in stage_tasks
        ):
            return False, f"required_{required_stage}_incomplete"
    if any(getattr(bug, "assessment", "candidate") == "candidate" for bug in bugs.values()):
        return False, "bug_adjudication_pending"
    report_tasks = [task for task in task_values if getattr(task, "stage", None) == "report"]
    if (
        not isinstance(report_revision, int)
        or report_revision < 1
        or not report_tasks
        or not any(
            getattr(task, "state", None) == "completed"
            and bool(getattr(task, "result_digest", None))
            for task in report_tasks
        )
    ):
        return False, "report_not_persisted"
    if goal == "find_bugs":
        return True, None
    if goal != "repair_preview":
        return False, "unknown_graph_goal"

    confirmed = [bug for bug in bugs.values() if getattr(bug, "assessment", None) == "confirmed"]
    if not confirmed:
        return False, "no_confirmed_bug_to_repair"
    patch_tasks = [task for task in task_values if getattr(task, "stage", None) == "patch"]
    check_tasks = [task for task in task_values if getattr(task, "stage", None) == "check"]
    if not patch_tasks or not check_tasks:
        return False, "repair_or_regression_task_missing"
    observations = getattr(graph, "observations", [])
    for bug in confirmed:
        bug_id = getattr(bug, "bug_id", None)
        if getattr(bug, "remediation", None) != "fix_verified" or getattr(
            bug, "evidence_level", None,
        ) != "regression_verified":
            return False, f"fix_not_verified:{bug_id}"
        if not any(
            getattr(task, "subject_id", None) == bug_id
            and getattr(task, "state", None) == "completed"
            and bool(getattr(task, "result_digest", None))
            for task in patch_tasks
        ):
            return False, f"patch_not_completed:{bug_id}"
        passed_regressions = [
            task for task in check_tasks
            if getattr(task, "subject_id", None) == bug_id
            and getattr(task, "state", None) == "completed"
            and bool(getattr(task, "result_digest", None))
            and any(
                isinstance(observation, Mapping)
                and observation.get("task_id") == getattr(task, "task_id", None)
                and observation.get("artifact_digest") == getattr(task, "result_digest", None)
                and observation.get("status") == "passed"
                and observation.get("exit_code") == 0
                for observation in observations
            )
        ]
        if not passed_regressions:
            return False, f"regression_not_passed:{bug_id}"
    return True, None


def classify_graph_finish(
    *,
    ready: bool,
    reason: str | None,
    budget_exhausted: bool,
    budget_reasons: Sequence[str],
    deadline_reasons: Sequence[str],
) -> FinishDecision:
    """Classify graph completion while retaining hard budget/deadline gates."""
    hard_reasons = [*budget_reasons, *deadline_reasons]
    if hard_reasons or budget_exhausted:
        return FinishDecision(
            status="BUDGET_EXHAUSTED",
            budget_reasons=tuple(str(item) for item in hard_reasons),
        )
    if ready:
        return FinishDecision(status="COMPLETE")
    detail = reason or "graph_goal_incomplete"
    return FinishDecision(
        status="INCOMPLETE",
        reason="graph_goal_incomplete",
        last_error=f"agent graph 完成门禁未满足: {detail}",
    )


def classify_finish(
    *,
    actual_complete: bool,
    requested_coverage_complete: bool | None,
    life_kind: str,
    parallel_satisfied: bool,
    parallel_reasons: Sequence[object],
    open_findings: Sequence[Mapping[str, object]],
    require_confirmation: bool,
    confirmation_ready: bool,
    confirmation_findings: Sequence[object],
    confirmation_error: object | None,
    budget_exhausted: bool,
    budget_reasons: Sequence[str],
    deadline_reasons: Sequence[str],
    soft_limit_reasons: Sequence[str],
) -> FinishDecision:
    """Classify a finish attempt without mutating any caller-owned object.

    The precedence intentionally mirrors the historical Supervisor behavior:
    budget/deadline reasons override otherwise actionable incomplete gates;
    open findings, concurrency, and confirmation are reported in that order.
    """
    complete = (
        actual_complete
        and requested_coverage_complete is not False
        and (life_kind == "fix" or parallel_satisfied)
        and not open_findings
        and (not require_confirmation or confirmation_ready)
    )
    reasons = [*budget_reasons, *deadline_reasons]
    if not complete:
        reasons.extend(soft_limit_reasons)

    if complete and not budget_exhausted and not deadline_reasons:
        return FinishDecision(status="COMPLETE")

    if actual_complete and open_findings and not reasons:
        error = "仍有未关闭 Finding: " + ", ".join(
            f"{item['id']}({item['reason']})" for item in open_findings[:20]
        )
        return FinishDecision(
            status="INCOMPLETE", reason="open_findings", last_error=error,
        )

    if life_kind != "fix" and actual_complete and not parallel_satisfied and not reasons:
        error = "并发门禁未满足: " + ", ".join(
            str(item) for item in parallel_reasons
        )
        return FinishDecision(
            status="INCOMPLETE",
            reason="concurrency_not_satisfied",
            last_error=error,
        )

    if (
        actual_complete
        and require_confirmation
        and not confirmation_ready
        and not reasons
    ):
        error = (
            ", ".join(str(item) for item in confirmation_findings)
            if confirmation_findings
            else str(confirmation_error or "存在未确认 Finding")
        )
        return FinishDecision(
            status="INCOMPLETE",
            reason="finding_confirmation_pending",
            last_error="Finding 确认门禁未满足: " + error,
        )

    if reasons:
        budget_reasons = tuple(str(item) for item in reasons)
        if open_findings:
            error = "预算已耗尽，且仍有未关闭 Finding: " + ", ".join(
                f"{item['id']}({item['reason']})" for item in open_findings[:20]
            )
            return FinishDecision(
                status="BUDGET_EXHAUSTED",
                reason="budget_exhausted_with_open_findings",
                last_error=error,
                budget_reasons=budget_reasons,
            )
        return FinishDecision(
            status="BUDGET_EXHAUSTED",
            budget_reasons=budget_reasons,
        )
    return FinishDecision(status="INCOMPLETE")


__all__ = [
    "FinishDecision", "classify_finish", "graph_completion", "classify_graph_finish",
]
