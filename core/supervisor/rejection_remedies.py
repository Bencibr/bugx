#!/usr/bin/env python3
"""Gate rejection remedies and bounded recovery decisions.

The gate is a read-only observation.  This module keeps the contract for what
each rejection means and which role/input can clear it next.  It deliberately
does not execute recovery: callers must re-read the gate after every action and
stop after one no-progress retry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CATEGORY_WAIT = "wait"
CATEGORY_HOST = "host_action"
CATEGORY_WORKER = "worker_action"
CATEGORY_SCHEDULER = "scheduler_config"
CATEGORY_TERMINAL = "terminal"
UNREGISTERED_CATEGORY = "unregistered"

MAX_NO_PROGRESS_RETRIES = 1
RECOVERY_RETRY = "RETRY"
RECOVERY_FAILED = "FAILED"
RECOVERY_REQUIRED_ACTION = "RECOVERY_REQUIRED"


def _entry(
    *,
    meaning: str,
    category: str,
    trigger: Mapping[str, object],
    required_role: str,
    required_inputs: tuple[str, ...],
    exit_conditions: tuple[str, ...],
    next_actions: tuple[str, ...],
    steps: tuple[str, ...],
    cli: list[list[str]] | None = None,
    mcp_tool: str | None = None,
    terminal: bool = False,
    fallback_actions: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "meaning": meaning,
        "category": category,
        "trigger": dict(trigger),
        "required_role": required_role,
        "required_inputs": required_inputs,
        "exit_conditions": exit_conditions,
        "next_actions": next_actions,
        "fallback_actions": fallback_actions,
        "steps": list(steps),
        "cli": cli,
        "mcp_tool": mcp_tool,
        "requires_host": required_role == "host",
        "terminal": terminal,
        "max_no_progress_retries": MAX_NO_PROGRESS_RETRIES,
    }


REJECTION_REMEDIES: dict[str, dict[str, Any]] = {
    "active_workers_remaining": _entry(
        meaning="仍有 active worker 未提交 checkpoint，必须等待其收敛。",
        category=CATEGORY_WAIT,
        trigger={"gate_reason": "active_workers_remaining", "gate_field": "active_workers"},
        required_role="worker",
        required_inputs=("worker_id", "heartbeat_nonce"),
        exit_conditions=("active_workers 为空", "worker 已提交 checkpoint 或租约已终止"),
        next_actions=("WAIT_OR_HEARTBEAT_WORKERS",),
        steps=("读取 active_claims 与租约剩余时间", "等待 checkpoint 或发送带身份的 heartbeat"),
        cli=[["bugx", "supervisor", "status"]],
    ),
    "expired_workers_require_retry": _entry(
        meaning="存在 expired claim 形成的重试债，必须先接手原任务包。",
        category=CATEGORY_WORKER,
        trigger={"gate_reason": "expired_workers_require_retry", "gate_field": "expired_workers"},
        required_role="worker",
        required_inputs=("worker_id", "recover_expired=true", "expected_revision"),
        exit_conditions=("expired_workers 为空", "新 claim active 且 unit_ids/resource_keys 与原包一致"),
        next_actions=("RECLAIM_EXPIRED_WORKERS",),
        fallback_actions=("STALLED_OR_RECOVER",),
        steps=(
            "以 recover_expired=true 接手同一 expired/released claim",
            "接手失败时先确认没有 active claim，再由 host 显式 recover",
            "recover 后重新读取 gate，再按原单元重新派发",
        ),
        cli=[["bugx", "supervisor", "status"]],
        mcp_tool="bugx_supervisor_claim",
    ),
    "parallel_peak_below_two": _entry(
        meaning="要求并行但本轮未观察到两个真实运行 worker。",
        category=CATEGORY_SCHEDULER,
        trigger={"gate_reason": "parallel_peak_below_two"},
        required_role="host",
        required_inputs=("coverage_units", "max_concurrency"),
        exit_conditions=("本轮真实峰值至少为 2", "或显式记录 serial_reason"),
        next_actions=("CLAIM_WORKERS",),
        steps=("派发两个不同单元并确认 worker-start 窗口重叠",),
        cli=[["bugx", "supervisor", "status"]],
    ),
    "parallel_overlap_missing": _entry(
        meaning="峰值达到两个但执行窗口没有实际重叠。",
        category=CATEGORY_SCHEDULER,
        trigger={"gate_reason": "parallel_overlap_missing"},
        required_role="host",
        required_inputs=("worker_ids", "execution_started_at"),
        exit_conditions=("overlap_seconds 大于 0",),
        next_actions=("CLAIM_WORKERS", "WAIT_OR_HEARTBEAT_WORKERS"),
        steps=("同时启动两个 worker，重新读取持久化执行窗口",),
    ),
    "parallel_workers_missing_checkpoint": _entry(
        meaning="并发窗口存在但缺少两个非空 checkpoint。",
        category=CATEGORY_WORKER,
        trigger={"gate_reason": "parallel_workers_missing_checkpoint"},
        required_role="worker",
        required_inputs=("worker_id", "checkpoint_id", "coverage", "evidence"),
        exit_conditions=("两个 worker 均有非空 checkpoint digest",),
        next_actions=("WAIT_OR_HEARTBEAT_WORKERS",),
        steps=("每个 worker 提交带 coverage/evidence 的 checkpoint",),
        mcp_tool="bugx_supervisor_checkpoint",
    ),
    "max_wall_seconds": _entry(
        meaning="run 级墙钟预算耗尽。",
        category=CATEGORY_HOST,
        trigger={"budget_reason": "max_wall_seconds"},
        required_role="host",
        required_inputs=("expected_revision", "renew_wall_clock"),
        exit_conditions=("新预算 epoch 建立", "或 finish 进入明确终态"),
        next_actions=("RESET_BUDGET",),
        steps=("host 以 expected_revision 显式续期预算，随后重读 gate",),
        cli=[["bugx", "supervisor", "reset-budget", "--renew-wall-clock"]],
        mcp_tool="bugx_supervisor_reset_budget",
    ),
    "max_round_wall_seconds": _entry(
        meaning="当前 Round 墙钟预算耗尽。",
        category=CATEGORY_HOST,
        trigger={"budget_reason": "max_round_wall_seconds"},
        required_role="host",
        required_inputs=("expected_revision",),
        exit_conditions=("Round 预算恢复", "或 next-round/finish 返回明确结果"),
        next_actions=("RESET_BUDGET", "FINISH_OR_NEXT_ROUND"),
        steps=("重读活性后再续期或推进 Round，不得无条件循环",),
        cli=[["bugx", "supervisor", "next-round"]],
        mcp_tool="bugx_supervisor_next_round",
    ),
    "max_tool_calls": _entry(
        meaning="工具调用预算耗尽。",
        category=CATEGORY_HOST,
        trigger={"budget_reason": "max_tool_calls"},
        required_role="host",
        required_inputs=("expected_revision", "max_tool_calls"),
        exit_conditions=("预算 epoch 重置", "或明确终止"),
        next_actions=("RESET_BUDGET",),
        steps=("host 显式设置新上限并在一次无进展重试后停止",),
        cli=[["bugx", "supervisor", "reset-budget", "--max-tool-calls", "2000"]],
        mcp_tool="bugx_supervisor_reset_budget",
    ),
    "max_cost_units": _entry(
        meaning="成本预算耗尽。",
        category=CATEGORY_HOST,
        trigger={"budget_reason": "max_cost_units"},
        required_role="host",
        required_inputs=("expected_revision", "max_cost_units"),
        exit_conditions=("预算 epoch 重置", "或明确终止"),
        next_actions=("RESET_BUDGET",),
        steps=("host 显式设置新成本上限并重读 gate",),
        cli=[["bugx", "supervisor", "reset-budget", "--max-cost-units", "20000"]],
        mcp_tool="bugx_supervisor_reset_budget",
    ),
    "max_rounds": _entry(
        meaning="Round 数量上限耗尽。",
        category=CATEGORY_HOST,
        trigger={"budget_reason": "max_rounds"},
        required_role="host",
        required_inputs=("expected_revision", "max_rounds"),
        exit_conditions=("预算 epoch 重置", "或 finish 保留未覆盖债务"),
        next_actions=("RESET_BUDGET", "FINISH_OR_NEXT_ROUND"),
        steps=("提高上限并开启新 epoch，或如实 finish",),
        cli=[["bugx", "supervisor", "reset-budget", "--max-rounds", "20"]],
        mcp_tool="bugx_supervisor_reset_budget",
    ),
    "target_drift": _entry(
        meaning="目标工作区指纹漂移，当前 claim 不能继续写入。",
        category=CATEGORY_HOST,
        trigger={"gate_field": "target_drift", "equals": True},
        required_role="host",
        required_inputs=("reason", "expected_revision"),
        exit_conditions=("target_drift=false", "且新 gate 已重读"),
        next_actions=("REBUILD_PLAN",),
        steps=("重建计划并显式 rebind；rebind 后重读 gate",),
        cli=[["bugx", "supervisor", "rebind-target", "--reason", "<reason>"]],
        mcp_tool="bugx_supervisor_rebind_target",
    ),
}


def remedy_for(reason: str) -> dict[str, Any]:
    """Return a fail-closed remedy declaration for one gate reason."""
    entry = REJECTION_REMEDIES.get(str(reason))
    if entry is None:
        return {
            "reason": str(reason),
            "registered": False,
            "category": UNREGISTERED_CATEGORY,
            "meaning": "未登记的拒绝原因：不能猜测恢复动作。",
            "trigger": {"unknown_reason": str(reason)},
            "required_role": "host",
            "required_inputs": ("reason", "current_gate"),
            "exit_conditions": ("人工/版本一致性检查完成",),
            "next_actions": (RECOVERY_REQUIRED_ACTION,),
            "fallback_actions": (),
            "steps": ["停止自动写操作，登记原因并返回 RECOVERY_REQUIRED"],
            "cli": None,
            "mcp_tool": None,
            "requires_host": True,
            "terminal": True,
            "max_no_progress_retries": MAX_NO_PROGRESS_RETRIES,
        }
    return {"reason": str(reason), "registered": True, **entry}


def remedies_for(reasons: Any) -> dict[str, dict[str, Any]]:
    """Map a reason collection to declarations; malformed input is empty."""
    if not isinstance(reasons, (list, tuple, set, frozenset)):
        return {}
    normalized = sorted({str(reason) for reason in reasons})
    return {reason: remedy_for(reason) for reason in normalized}


def unregistered_reasons(reasons: Any) -> list[str]:
    return [
        reason for reason, entry in remedies_for(reasons).items()
        if not entry["registered"]
    ]


def recovery_fingerprint(state: Mapping[str, object], gate: Mapping[str, object]) -> tuple[object, ...]:
    """Stable state/reason key used to detect a retry that made no progress."""
    return (
        str(state.get("status", "")),
        state.get("round_no"),
        state.get("scheduler_epoch"),
        tuple(sorted(str(item) for item in gate.get("reasons", ()) or ())),
        tuple(sorted(str(item) for item in gate.get("expired_workers", ()) or ())),
        tuple(sorted(str(item) for item in gate.get("active_workers", ()) or ())),
    )


def bounded_recovery_decision(
    attempts: int,
    progressed: bool,
) -> dict[str, object]:
    """Return one retry at most, then a terminal recovery-required result.

    ``attempts`` is the count of prior attempts for the same fingerprint.  The
    helper is pure so a host can persist the decision with its own receipt while
    keeping the gate read-only.
    """
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("attempts must be a non-negative integer")
    if progressed:
        return {
            "status": "PROGRESS",
            "next_action": None,
            "attempts": 0,
            "max_no_progress_retries": MAX_NO_PROGRESS_RETRIES,
            "terminal": False,
        }
    if attempts < MAX_NO_PROGRESS_RETRIES:
        return {
            "status": RECOVERY_RETRY,
            "next_action": "RETRY_RECOVERY",
            "attempts": attempts + 1,
            "max_no_progress_retries": MAX_NO_PROGRESS_RETRIES,
            "terminal": False,
        }
    return {
        "status": RECOVERY_FAILED,
        "next_action": RECOVERY_REQUIRED_ACTION,
        "reason": "recovery_no_progress",
        "attempts": attempts,
        "max_no_progress_retries": MAX_NO_PROGRESS_RETRIES,
        "terminal": True,
    }


# Explicit alias for callers that describe the operation as a retry guard.
recovery_attempt = bounded_recovery_decision


__all__ = [
    "CATEGORY_HOST",
    "CATEGORY_SCHEDULER",
    "CATEGORY_TERMINAL",
    "CATEGORY_WAIT",
    "CATEGORY_WORKER",
    "MAX_NO_PROGRESS_RETRIES",
    "RECOVERY_FAILED",
    "RECOVERY_REQUIRED_ACTION",
    "RECOVERY_RETRY",
    "REJECTION_REMEDIES",
    "bounded_recovery_decision",
    "recovery_attempt",
    "recovery_fingerprint",
    "remedies_for",
    "remedy_for",
    "unregistered_reasons",
]
