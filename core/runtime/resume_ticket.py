"""One-use, short-lived handoff state for resuming after project setup."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import core.secure_atomic as secure_atomic

TICKET_TTL_SECONDS = 600
_TICKET_NAME = "resume_ticket.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def create_resume_ticket(
    root: Path,
    *,
    session_id: str | None = None,
    intent: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Atomically write a ticket that expires after ten minutes."""
    root = root.expanduser().resolve()
    current = time.time() if now is None else float(now)
    payload = {
        "schema_version": 1,
        "resume_id": uuid.uuid4().hex,
        "session_id": session_id or f"sess-{uuid.uuid4().hex[:8]}",
        "intent": intent or "对当前项目进行漏洞排查",
        "project_root": str(root),
        "status": "pending_wakeup",
        "created_at": current,
        "expires_at": current + TICKET_TTL_SECONDS,
        "action": "resume_after_project_init",
    }
    target = root / ".bugx" / _TICKET_NAME
    secure_atomic.atomic_write_json(target, payload, mode=0o600, parent_mode=0o700)
    return payload


def consume_resume_ticket(root: Path, *, now: float | None = None) -> dict[str, Any] | None:
    """Consume a valid ticket at most once, including concurrent callers."""
    root = root.expanduser().resolve()
    target = root / ".bugx" / _TICKET_NAME
    claim = target.with_suffix(".claim")
    secure_atomic.reject_symlink_components(target)
    claimed = False
    try:
        if target.is_symlink() or not target.is_file():
            return None
        if claim.is_symlink():
            return None
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            claimed = True
        except FileExistsError:
            try:
                state = json.loads(claim.read_text(encoding="utf-8"))
                pid = int(state.get("pid", 0)) if isinstance(state, dict) else 0
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pid = 0
            if _pid_alive(pid):
                return None
            claim.unlink(missing_ok=True)
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            claimed = True
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": time.time()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        payload = json.loads(target.read_text(encoding="utf-8"))
        current = time.time() if now is None else float(now)
        if (not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or payload.get("project_root") != str(root)
                or not isinstance(payload.get("expires_at"), (int, float))
                or float(payload["expires_at"]) <= current):
            target.unlink(missing_ok=True)
            return None
        target.unlink()
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    finally:
        if claimed:
            try:
                claim.unlink(missing_ok=True)
            except OSError:
                pass


def resume_card(ticket: dict[str, Any]) -> dict[str, Any]:
    """Return the stable card payload used by GUI hosts after setup."""
    return {
        "type": "bugx.resume_ticket",
        "resume_id": ticket.get("resume_id"),
        "action": "reload_workspace_and_resume",
        "expires_at": ticket.get("expires_at"),
    }
