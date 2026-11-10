"""Attempt-local, fail-closed telemetry for post settlement (DOC-208 D3)."""

from __future__ import annotations

import contextvars
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


POST_EVENT_SCHEMA = 1
_EVENT_FIELDS = {
    "exit_code", "status", "phase", "gate_id", "gate_name", "reason_code",
    "exception_type", "context_status", "duration_ms", "duration_seconds",
    "runtime_run_id", "supervisor_run_id", "lifecycle_id", "polling_id",
    "observability_status", "detail",
}

# The list describes real post/dispatcher checkpoints. It intentionally does
# not encode a fixed gate count: paths only emit gates they actually execute.
GATE_REGISTRY = {
    name: {
        "gate_id": f"post.v1.{name}",
        "reason_codes": {
            "ok": f"{name}_passed",
            "blocked": f"{name}_blocked",
            "error": f"{name}_error",
        },
    }
    for name in (
        "attempt_token_claim", "post_exception", "runtime_load", "runtime_active", "lifecycle_open", "git_diff",
        "checkpoint_reconcile", "module_coverage", "target_reconcile", "prep_validate",
        "supervisor_parallel", "serial_authorization", "finding_open",
        "findings_snapshot", "finding_confirmation",
        "lesson_review_pre_settle", "finding_binding", "fix_mutation",
        "settlement", "auto_review", "knowledge_materialization",
        "lesson_review_post_settle", "lifecycle_finalize", "next_life_start",
        "acceptance_receipt",
        "dispatch_layout", "dispatch_version", "dispatch_runtime_paths",
    )
}
_FAILURE_REASON_CODES = {
    "post_failed",
    "attempt_token_reused",
    "unhandled_exception",
    "post_return_nonzero",
    "dispatch_failed",
    *(f"{name}_blocked" for name in GATE_REGISTRY),
    *(f"{name}_error" for name in GATE_REGISTRY),
}

_CURRENT: contextvars.ContextVar["PostAttempt | None"] = contextvars.ContextVar(
    "bugx_post_attempt", default=None,
)
_LAST_RESULT: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "bugx_last_post_attempt_result", default=None,
)


@dataclass
class PostAttempt:
    post_attempt_id: str
    source: str
    attempt_token: object = field(default_factory=object, repr=False)
    started: float = field(default_factory=time.monotonic)
    active_gate_id: str | None = None
    active_gate_started: float | None = None
    finished_gate_ids: set[str] = field(default_factory=set)
    failure_recorded: bool = False
    observability_status: str = "available"
    gates: list[dict[str, object]] = field(default_factory=list)
    failure_reason_code: str | None = None
    identity: dict[str, str] = field(default_factory=dict)
    instrumentation_errors: list[str] = field(default_factory=list)
    token_claimed: bool = False
    token_claim_rejected: bool = False
    _token_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def claim_token(self, token: object) -> bool:
        with self._token_lock:
            if token is not self.attempt_token or self.token_claimed:
                return False
            self.token_claimed = True
            return True

    def result(self) -> dict[str, object]:
        return {
            "post_event_schema": POST_EVENT_SCHEMA,
            "post_attempt_id": self.post_attempt_id,
            "source": self.source,
            "status": "failed" if self.failure_recorded else "finished",
            "failure_reason_code": self.failure_reason_code,
            "observability_status": self.observability_status,
            "gates": [dict(item) for item in self.gates],
            "instrumentation_errors": list(self.instrumentation_errors),
        }


def _logger():
    try:
        from core.launch_phases.common import DIAGNOSTIC_LOGGER
        return DIAGNOSTIC_LOGGER
    except Exception:
        return None


def _emit(event: str, message: str = "", *, level: int = 20, **fields: object) -> bool:
    attempt = _CURRENT.get()
    logger = _logger()
    if attempt is None or logger is None:
        if attempt is not None:
            attempt.observability_status = "unavailable"
            if not getattr(attempt, "_warning_emitted", False):
                attempt._warning_emitted = True  # type: ignore[attr-defined]
                print("[post] telemetry unavailable", file=sys.stderr)
        return False
    try:
        from core.runtime import bugx_logging
        safe_fields = {
            key: value for key, value in fields.items()
            if key in _EVENT_FIELDS
        }
        if len(safe_fields) != len(fields):
            attempt.instrumentation_errors.append("event_field_not_allowed")
        acknowledged = bugx_logging.log_event(
            logger, event, level=level,
            post_event_schema=POST_EVENT_SCHEMA,
            post_attempt_id=attempt.post_attempt_id,
            command="post",
            **attempt.identity,
            **safe_fields,
        )
        if acknowledged is not True:
            attempt.observability_status = "unavailable"
            if not getattr(attempt, "_warning_emitted", False):
                attempt._warning_emitted = True  # type: ignore[attr-defined]
                print("[post] telemetry unavailable", file=sys.stderr)
            return False
        return True
    except Exception:
        attempt.observability_status = "unavailable"
        if not getattr(attempt, "_warning_emitted", False):
            attempt._warning_emitted = True  # type: ignore[attr-defined]
            print("[post] telemetry unavailable", file=sys.stderr)
        return False


def emit_attempt_event(event: str, *, level: int = 20, **fields: object) -> bool:
    """Emit a command/phase event under the active attempt and local-write ack."""
    return _emit(event, level=level, **fields)


@contextmanager
def attempt_scope(
    source: str = "direct", attempt_id: str | None = None, *,
    attempt_token: object | None = None, reuse_current: bool = False,
) -> Iterator[PostAttempt]:
    """Create an attempt unless a one-time internal token proves same-call ownership."""
    existing = _CURRENT.get()
    if (
        attempt_token is not None and existing is not None
        and existing.claim_token(attempt_token)
    ):
        yield existing
        return
    if reuse_current and existing is not None:
        yield existing
        return
    token_rejected = attempt_token is not None
    attempt = PostAttempt(
        post_attempt_id=str(uuid.uuid4()) if token_rejected else (attempt_id or str(uuid.uuid4())),
        source="token_rejected" if token_rejected else source,
        token_claim_rejected=token_rejected,
    )
    token = _CURRENT.set(attempt)
    try:
        if token_rejected:
            begin_gate("attempt_token_claim")
            finish_gate(status="blocked")
            record_failure("attempt_token_reused", gate_status="blocked")
        yield attempt
    finally:
        if attempt.active_gate_id is not None:
            finish_gate(status="error", detail="attempt exited with active gate")
        _LAST_RESULT.set(attempt.result())
        _CURRENT.reset(token)


def current_attempt() -> PostAttempt | None:
    return _CURRENT.get()


def get_post_attempt_result() -> dict[str, object] | None:
    attempt = _CURRENT.get()
    if attempt is not None:
        return attempt.result()
    result = _LAST_RESULT.get()
    return dict(result) if result is not None else None


def set_identity(**values: object) -> None:
    attempt = _CURRENT.get()
    if attempt is None:
        return
    allowed = {
        "runtime_run_id", "supervisor_run_id", "lifecycle_id", "polling_id",
    }
    for key, value in values.items():
        if key in allowed and isinstance(value, str) and value:
            attempt.identity[key] = value[:128]
        elif (
            key == "context_status" and isinstance(value, str)
            and value in {"bound", "unbound", "unknown"}
        ):
            attempt.identity[key] = str(value)


def begin_gate(name: str) -> bool:
    attempt = _CURRENT.get()
    entry = GATE_REGISTRY.get(name)
    if attempt is None or entry is None:
        return False
    if attempt.active_gate_id is not None:
        attempt.instrumentation_errors.append("nested_gate")
        finish_gate(status="error", detail="nested gate instrumentation")
    gate_id = str(entry["gate_id"])
    if gate_id in attempt.finished_gate_ids:
        attempt.instrumentation_errors.append("duplicate_gate_begin")
        return False
    attempt.active_gate_id = gate_id
    attempt.active_gate_started = time.monotonic()
    return True


def finish_gate(
    *, status: str, reason_code: str | None = None, detail: object = "",
    gate_name: str | None = None,
) -> bool:
    attempt = _CURRENT.get()
    if attempt is None:
        return False
    if gate_name is not None:
        entry = GATE_REGISTRY.get(gate_name)
        if entry is None:
            attempt.instrumentation_errors.append("unknown_gate")
            return False
        gate_id = str(entry["gate_id"])
        if attempt.active_gate_id not in {None, gate_id}:
            attempt.instrumentation_errors.append("active_gate_mismatch")
            return False
        started = attempt.active_gate_started or time.monotonic()
    else:
        gate_id = attempt.active_gate_id or ""
        name = gate_id.removeprefix("post.v1.")
        entry = GATE_REGISTRY.get(name)
        started = attempt.active_gate_started or time.monotonic()
    if not gate_id or entry is None:
        attempt.instrumentation_errors.append("finish_without_registered_gate")
        return False
    if gate_id in attempt.finished_gate_ids:
        attempt.instrumentation_errors.append("duplicate_gate_finish")
        return False
    if status not in {"ok", "blocked", "error"}:
        attempt.instrumentation_errors.append("invalid_gate_status")
        status = "error"
    codes = entry["reason_codes"]
    valid_code = codes.get(status)
    if reason_code is not None and reason_code != valid_code:
        attempt.instrumentation_errors.append("invalid_reason_code")
    reason_code = str(valid_code)
    duration_ms = round(max(0.0, time.monotonic() - started) * 1000, 2)
    item: dict[str, object] = {
        "gate_id": gate_id,
        "gate_name": gate_id.removeprefix("post.v1."),
        "phase": "post",
        "status": status,
        "duration_ms": duration_ms,
        "reason_code": reason_code,
    }
    attempt.finished_gate_ids.add(gate_id)
    attempt.gates.append(item)
    attempt.active_gate_id = None
    attempt.active_gate_started = None
    _emit(
        "gate_finished", level=20 if status == "ok" else 30,
        **item,
    )
    return True


def finish_active_gate(status: str, detail: object = "") -> bool:
    return finish_gate(status=status, detail=detail)


def record_failure(
    reason_code: str = "post_failed", detail: object = "", *,
    gate_status: str = "blocked", exit_code: int = 1,
    exception_type: str | None = None,
) -> None:
    attempt = _CURRENT.get()
    if attempt is None or attempt.failure_recorded:
        return
    attempt.failure_recorded = True
    stable_reason = reason_code if reason_code in _FAILURE_REASON_CODES else "post_failed"
    attempt.failure_reason_code = stable_reason
    if attempt.active_gate_id is not None:
        finish_active_gate(gate_status)
    _emit(
        "post_failed", level=40, reason_code=stable_reason,
        exit_code=int(exit_code), status="failed",
        **({"detail": str(detail)[:400]} if detail else {}),
        **({"exception_type": exception_type[:80]} if exception_type else {}),
    )


def record_unhandled_exception(exc: BaseException, *, exit_code: int = 1) -> None:
    attempt = _CURRENT.get()
    if attempt is not None and attempt.active_gate_id is None:
        begin_gate("post_exception")
    record_failure(
        "unhandled_exception", gate_status="error",
        exit_code=exit_code,
        exception_type=type(exc).__name__,
    )


def record_dispatch_failure(
    gate_name: str, detail: object = "", *, exit_code: int = 1,
    gate_status: str = "blocked",
) -> None:
    entry = GATE_REGISTRY.get(gate_name)
    if entry is None:
        record_failure("dispatch_failed", detail, exit_code=exit_code)
        return
    if gate_status not in {"blocked", "error"}:
        gate_status = "error"
    begin_gate(gate_name)
    finish_gate(status=gate_status, detail=detail)
    record_failure(f"{gate_name}_{gate_status}", detail, gate_status=gate_status, exit_code=exit_code)


def log_attempt_finished(exit_code: int) -> None:
    attempt = _CURRENT.get()
    if attempt is None:
        return
    if exit_code != 0 and not attempt.failure_recorded:
        record_failure("post_return_nonzero", gate_status="error", exit_code=exit_code)
    failed = exit_code != 0 or attempt.failure_recorded
    final_exit_code = int(exit_code) if exit_code != 0 else (1 if failed else 0)
    _emit(
        "post_attempt_finished", level=40 if failed else 20,
        exit_code=final_exit_code,
        status="failed" if failed else "ok",
        observability_status=attempt.observability_status,
    )


__all__ = [
    "GATE_REGISTRY", "POST_EVENT_SCHEMA", "PostAttempt", "attempt_scope",
    "begin_gate", "current_attempt", "emit_attempt_event", "finish_active_gate", "finish_gate",
    "get_post_attempt_result", "log_attempt_finished", "record_dispatch_failure",
    "record_failure", "record_unhandled_exception", "set_identity",
]
