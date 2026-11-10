#!/usr/bin/env python3
"""core/mcp_handlers/telemetry_handlers.py - 调度度量与全景遥测工具处理器 (DOC-070)。"""

from __future__ import annotations

import math
import threading
from typing import Any, Mapping

from core.supervisor import audit_supervisor


# Retrieval telemetry is intentionally process-local.  Retrieval calls are
# short-lived and must not make the MCP telemetry path depend on a database or
# an external service.  The benchmark and the MCP handler share this recorder.
_RETRIEVAL_OPERATIONS = ("rrf", "slice", "memory", "ego")
_retrieval_samples: dict[str, list[dict[str, float]]] = {
    operation: [] for operation in _RETRIEVAL_OPERATIONS
}
_retrieval_lock = threading.RLock()
_MAX_RETRIEVAL_SAMPLES = 10_000


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def record_retrieval_metric(
    operation: str,
    duration_ms: float,
    *,
    tokens: float = 0.0,
    output_bytes: float = 0.0,
) -> None:
    """Record one bounded retrieval sample for the telemetry dashboard.

    ``tokens`` is an estimate of the serialized context size (the benchmark
    uses UTF-8 bytes / 3.8).  ``output_bytes`` allows the dashboard to report
    token density independently of latency.  Invalid samples are rejected so
    the dashboard never fabricates a zero-valued measurement.
    """
    name = str(operation).strip().lower()
    if name not in _RETRIEVAL_OPERATIONS:
        raise ValueError("operation must be one of rrf, slice, memory, ego")
    values = (duration_ms, tokens, output_bytes)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise TypeError("retrieval metrics must be numeric")
    duration = float(duration_ms)
    token_count = float(tokens)
    byte_count = float(output_bytes)
    if not all(math.isfinite(value) and value >= 0.0 for value in (duration, token_count, byte_count)):
        raise ValueError("retrieval metrics must be finite and non-negative")
    with _retrieval_lock:
        samples = _retrieval_samples[name]
        samples.append({"duration_ms": duration, "tokens": token_count, "bytes": byte_count})
        if len(samples) > _MAX_RETRIEVAL_SAMPLES:
            del samples[: len(samples) - _MAX_RETRIEVAL_SAMPLES]


def clear_retrieval_metrics() -> None:
    """Clear process-local retrieval samples (primarily useful for tests)."""
    with _retrieval_lock:
        for samples in _retrieval_samples.values():
            samples.clear()


def retrieval_metrics_snapshot() -> dict[str, object]:
    """Return P50/P99 latency and serialized-context token density."""
    with _retrieval_lock:
        snapshot: dict[str, object] = {}
        for operation in _RETRIEVAL_OPERATIONS:
            samples = list(_retrieval_samples[operation])
            latencies = [sample["duration_ms"] for sample in samples]
            tokens = [sample["tokens"] for sample in samples]
            byte_count = sum(sample["bytes"] for sample in samples)
            token_count = sum(tokens)
            snapshot[operation] = {
                "count": len(samples),
                "p50_ms": round(_percentile(latencies, 0.50), 4),
                "p99_ms": round(_percentile(latencies, 0.99), 4),
                "avg_ms": round(sum(latencies) / len(latencies), 4) if latencies else 0.0,
                "token_count": round(token_count, 3),
                "output_bytes": round(byte_count, 3),
                "token_density": round(token_count / byte_count, 6) if byte_count else None,
            }
        return snapshot


def handle_retrieval_metrics(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """Record optional samples and expose retrieval latency/context metrics.

    A caller may pass one sample (``operation``, ``duration_ms``, ``tokens``,
    ``output_bytes``) or a ``samples`` list of those mappings.  With no sample
    this is a read-only snapshot operation.
    """
    del service
    try:
        raw_samples: object = arguments.get("samples")
        if raw_samples is not None:
            if not isinstance(raw_samples, (list, tuple)):
                raise TypeError("samples must be a list")
            for sample in raw_samples:
                if not isinstance(sample, Mapping):
                    raise TypeError("each retrieval sample must be an object")
                record_retrieval_metric(
                    sample.get("operation", sample.get("backend", "")),
                    sample.get("duration_ms", sample.get("latency_ms", 0.0)),
                    tokens=sample.get("tokens", sample.get("token_count", 0.0)),
                    output_bytes=sample.get("output_bytes", sample.get("bytes", 0.0)),
                )
        elif "operation" in arguments:
            record_retrieval_metric(
                arguments.get("operation", ""),
                arguments.get("duration_ms", arguments.get("latency_ms", 0.0)),
                tokens=arguments.get("tokens", arguments.get("token_count", 0.0)),
                output_bytes=arguments.get("output_bytes", arguments.get("bytes", 0.0)),
            )
        return {"status": "ok", "retrieval_metrics": retrieval_metrics_snapshot()}
    except (TypeError, ValueError) as exc:
        return {"status": "error", "error": str(exc), "retrieval_metrics": retrieval_metrics_snapshot()}


def handle_scheduling_telemetry(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    sup = service.get_supervisor()
    try:
        state = sup.status()
        metrics = state.scheduling_metrics
        active = [
            {
                "worker_id": c.worker_id,
                "unit_ids": list(c.unit_ids),
                "lease_expires_at": c.lease_expires_at,
                "execution_state": c.execution_state,
                "process_pid": c.process_pid,
            }
            for c in state.active_claims
        ]
        from core.supervisor import task_scheduler
        ctrl = task_scheduler.AdaptiveConcurrencyController(
            min_concurrency=getattr(state.budget, "min_concurrency", 1),
            max_concurrency=getattr(
                state.budget, "max_concurrency_ceiling",
                getattr(state.budget, "max_concurrency", 4),
            ),
        )
        effective_capacity = ctrl.compute_capacity(len(active), state.budget, run_state=state)
        return {
            "status": "ok",
            "effective_capacity": effective_capacity,
            "total_claims": metrics.total_claims,
            "total_completed": metrics.total_completed,
            "total_preemptions": metrics.total_preempted,
            "total_expired": metrics.total_expired,
            "total_queue_seconds": round(metrics.total_queue_seconds, 2),
            "max_queue_seconds": round(metrics.max_queue_seconds, 2),
            "total_exec_seconds": round(metrics.total_execution_seconds, 2),
            "max_exec_seconds": round(metrics.max_execution_seconds, 2),
            "active_workers_count": len(active),
            "active_workers": active,
            "budget_concurrency": state.budget.max_concurrency,
            "concurrency_ceiling": state.budget.max_concurrency_ceiling,
            "adaptive_window": state.adaptive_concurrency_window,
            "adaptive_success_streak": state.adaptive_success_streak,
            "adaptive_failure_streak": state.adaptive_failure_streak,
            "adaptive_concurrency": state.budget.adaptive_concurrency,
        }
    except audit_supervisor.NoRunError:
        return {"status": "NO_RUN", "detail": "无正在运行的 Supervisor"}


def handle_global_telemetry(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    days = arguments.get("days", 30)
    if not isinstance(days, int) or days <= 0:
        days = 30
    project_filter = arguments.get("project_filter")
    if project_filter is not None and not isinstance(project_filter, str):
        project_filter = None
    try:
        from core.runtime import global_telemetry
        summary = global_telemetry.query_global_summary(days=days)
        projects = global_telemetry.query_project_breakdown(days=days)
        daily = global_telemetry.query_daily_rollup(days=days)
        if project_filter:
            projects = [p for p in projects if project_filter.lower() in p.get("project_name", "").lower()]
        return {
            "status": "ok",
            "days": days,
            "summary": summary,
            "projects": projects,
            "daily_rollup": daily,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "days": days,
            "summary": {},
            "projects": [],
            "daily_rollup": [],
        }


TELEMETRY_HANDLERS: dict[str, Any] = {
    "bugx_scheduling_telemetry": handle_scheduling_telemetry,
    "bugx_global_telemetry": handle_global_telemetry,
    "bugx_retrieval_metrics": handle_retrieval_metrics,
}


__all__ = [
    "TELEMETRY_HANDLERS",
    "clear_retrieval_metrics",
    "handle_global_telemetry",
    "handle_retrieval_metrics",
    "handle_scheduling_telemetry",
    "record_retrieval_metric",
    "retrieval_metrics_snapshot",
]
