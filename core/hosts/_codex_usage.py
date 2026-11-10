"""Read Codex's local thread index and rollout usage; never read credentials.

The SQLite/JSONL layout is a measured host implementation detail, not a public
API. Unsupported or incomplete evidence is reported explicitly. No chat text,
tool output, or provider secrets are copied into the usage ledger.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from ._base_usage import HostUsageError


class CodexUsageError(HostUsageError):
    """Local index or usage evidence is unavailable."""


_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cached_input_tokens",
    "cache_write_tokens": "cache_write_input_tokens",
    "reasoning_tokens": "reasoning_output_tokens",
    "total_tokens": "total_tokens",
}


def _epoch(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _usage(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
        for key, source in _FIELDS.items()
        for raw in [value.get(source)]
    }
    if result["total_tokens"] is None:
        if result["input_tokens"] is None or result["output_tokens"] is None:
            return None
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _spawn(source: Any) -> Mapping[str, Any]:
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            return {}
    if not isinstance(source, dict):
        return {}
    sub = source.get("subagent")
    spawn = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
    return spawn if isinstance(spawn, dict) else {}


def _sessions(home: Path, root: Path, since: float, until: float) -> list[dict[str, Any]]:
    # One level of known host index files; never walk the sessions archive.
    databases = sorted(
        home.glob("state_*.sqlite"),
        key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1,
        reverse=True,
    )
    if not databases:
        raise CodexUsageError("Codex local thread index not found")
    try:
        with closing(sqlite3.connect(databases[0].resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            rows = conn.execute(
                "SELECT id, rollout_path, source FROM threads "
                "WHERE cwd = ? AND updated_at >= ? AND created_at <= ?",
                (str(root), math.floor(since), math.ceil(until)),
            ).fetchall()
    except sqlite3.Error as exc:
        raise CodexUsageError(f"Unsupported or unreadable Codex thread index: {exc}") from exc
    if not rows:
        raise CodexUsageError("No indexed Codex sessions for the exact project/time window")
    return [
        {"session_id": row[0], "path": Path(row[1]), "parent_thread_id": _spawn(row[2]).get("parent_thread_id")}
        for row in rows
    ]


def _read_session(
    session: dict[str, Any], root: Path, home: Path, since: float, until: float,
    deadline: float, seen: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    sid = session["session_id"]
    path = session["path"].resolve()
    allowed = (home / "sessions", home / "archived_sessions")
    if path.suffix != ".jsonl" or not any(path.is_relative_to(p.resolve()) for p in allowed):
        raise CodexUsageError("Indexed rollout path is outside Codex session archives")
    requests: list[dict[str, Any]] = []
    snapshots: list[tuple[float, str, str, Any, Any]] = []
    models: dict[str, str] = {}
    model = "unknown"
    turn = "unknown"
    digest = hashlib.sha256()
    warnings: set[str] = set()
    forked = False
    first_meta = False
    bytes_read = 0
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        while bytes_read < size:
            if time.monotonic() >= deadline:
                warnings.add("read_budget_exhausted")
                break
            line = stream.readline(size - bytes_read)
            digest.update(line)
            bytes_read += len(line)
            try:
                obj = json.loads(line)
            except (ValueError, UnicodeError):
                warnings.add("invalid_or_incomplete_jsonl")
                continue
            if not isinstance(obj, dict):
                warnings.add("invalid_jsonl_record")
                continue
            typ, payload = obj.get("type"), obj.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            ts = _epoch(obj.get("timestamp"))
            if typ == "session_meta" and not first_meta:
                first_meta = True
                if payload.get("id") != sid or Path(payload.get("cwd") or ".").resolve() != root:
                    raise CodexUsageError("Rollout identity/project does not match the local index")
                forked = bool(payload.get("forked_from_id"))
            elif typ == "turn_context":
                turn = str(payload.get("turn_id") or "unknown")
                model = str(payload.get("model") or "unknown")
                models[turn] = model
            elif typ == "token_usage_record":
                # Forked rollouts can contain parent history, including rewritten
                # timestamps. Explicit thread identity, not time alone, owns usage.
                if payload.get("thread_id") != sid:
                    continue
                if ts is None:
                    warnings.add("usage_timestamp_missing")
                    continue
                if not since <= ts < until:
                    continue
                usage = _usage(payload.get("usage"))
                response = payload.get("response_id")
                if usage is None or not isinstance(response, str) or not response:
                    warnings.add("invalid_request_usage_or_response_id")
                    continue
                identity = (sid, response)
                if identity in seen:
                    if seen[identity] != usage:
                        warnings.add("conflicting_response_usage")
                    continue
                seen[identity] = usage
                request_turn = str(payload.get("turn_id") or turn)
                requests.append({"timestamp_epoch": ts, "turn_id": request_turn,
                                 "response_id": response, "model": models.get(request_turn, model),
                                 "method": "token_usage_record", **usage})
            elif typ == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                if ts is not None and isinstance(info, dict):
                    snapshots.append((ts, turn, model, _usage(info.get("total_token_usage")),
                                      _usage(info.get("last_token_usage"))))
    if not first_meta:
        raise CodexUsageError("Rollout session_meta missing")
    request_turns = {r["turn_id"] for r in requests}
    previous = None
    for ts, snap_turn, snap_model, cumulative, last in snapshots:
        if cumulative is None:
            if since <= ts < until and snap_turn not in request_turns:
                warnings.add("cumulative_usage_missing")
            continue
        delta = None
        if previous is not None:
            if cumulative["total_tokens"] < previous["total_tokens"]:
                if since <= ts < until:
                    warnings.add("cumulative_usage_reset")
            elif cumulative["total_tokens"] != previous["total_tokens"]:
                delta = {k: cumulative[k] - previous[k]
                         if cumulative[k] is not None and previous[k] is not None
                         and cumulative[k] >= previous[k] else None for k in _FIELDS}
        elif last is not None:
            delta = last
            if cumulative["total_tokens"] != last["total_tokens"] and since <= ts < until:
                warnings.add("cumulative_baseline_missing")
        previous = cumulative
        if not since <= ts < until or snap_turn in request_turns:
            continue
        if forked:
            warnings.add("forked_legacy_usage_unattributable")
            continue
        if delta is not None:
            requests.append({"timestamp_epoch": ts, "turn_id": snap_turn,
                             "response_id": None, "model": snap_model,
                             "method": "cumulative_delta", **delta})
    return {"session_id": sid, "parent_thread_id": session.get("parent_thread_id"),
            "rollout_path": str(path), "source_bytes": bytes_read,
            "source_sha256": digest.hexdigest(), "source_complete": bytes_read == size,
            "requests": requests, "warnings": sorted(warnings)}


def collect_lifecycle_usage(
    project_root: Path | str, runtime: Mapping[str, Any], *,
    until_epoch: float | None = None, session_id: str | None = None,
    persist: bool = True, budget_seconds: float = 5.0,
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    since = _epoch(runtime.get("lifecycle_started_epoch") or runtime.get("started_epoch"))
    until = _epoch(until_epoch if until_epoch is not None else runtime.get("stopped_epoch") or time.time())
    if since is None or until is None or since <= 0 or until <= since:
        raise CodexUsageError("Invalid lifecycle usage time window")
    sessions = _sessions(home, root, since, until)
    anchor = session_id or runtime.get("host_session_id") or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if anchor:
        family = {str(anchor)}
        while True:
            children = {s["session_id"] for s in sessions if s["parent_thread_id"] in family}
            expanded = family | children
            if expanded == family:
                break
            family = expanded
        sessions = [s for s in sessions if s["session_id"] in family]
        if not any(s["session_id"] == anchor for s in sessions):
            raise CodexUsageError("Requested Codex thread is not indexed for this project/window")
    deadline = time.monotonic() + max(0.01, budget_seconds)
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    details = []
    warnings = []
    for session in sessions:
        try:
            details.append(_read_session(session, root, home, since, until, deadline, seen))
        except (OSError, ValueError, CodexUsageError) as exc:
            warnings.append({"session_id": session["session_id"], "reason": str(exc)})
    main = [s for s in details if not s["parent_thread_id"] or s["session_id"] == anchor]
    workers = [s for s in details if s not in main]
    requests = [r for s in details for r in s["requests"]]
    models = []
    for name in sorted({r["model"] for r in requests}):
        entries = [r for r in requests if r["model"] == name]
        models.append({"model": name, "calls": len(entries), "duration_ms": None,
                       **{k: sum(r[k] for r in entries) if all(r[k] is not None for r in entries)
                          else None for k in _FIELDS}})
    def total(group):
        values = [r["total_tokens"] for s in group for r in s["requests"]]
        return sum(values) if values else None
    status = "measured" if requests else "unmeasured"
    if warnings or any(s["warnings"] for s in details):
        status = "partial" if requests else "unmeasured"
    result = {
        "schema_version": 1, "measurement_status": status,
        "project_root": str(root), "window": {"since_epoch": since, "until_epoch": until},
        "attribution_scope": "thread_family" if anchor else "project_time_window",
        "host_session_id": anchor, "scheduler_tokens": total(main),
        "worker_sessions_tokens": total(workers), "total_tokens": total(details),
        "main_session_count": len(main), "worker_session_count": len(workers),
        "models": models, "duration_ms_total": None,
        "main_sessions": main, "worker_sessions": workers, "warnings": warnings,
        "limitations": ["Recorded local usage is not a billing statement.",
                        "Cache/reasoning tokens are subsets of input/output, not additional totals.",
                        "Native child sessions are not proof of BugX claim attribution.",
                        "Ephemeral/remote sessions without indexed rollouts are not included.",
                        "Model latency is unavailable; no duration is inferred from event gaps."],
        "detail_path": None,
    }
    if persist:
        try:
            import core.bugx_paths as bugx_paths
            import core.secure_atomic as secure_atomic

            path = bugx_paths.require(root).state_root / "codex-usage.json"
            result["detail_path"] = str(path)
            secure_atomic.atomic_write_json(path, result, mode=0o600)
        except (OSError, ValueError, RuntimeError, ImportError):
            result["detail_path"] = None
            result["warnings"].append({"reason": "detail_persistence_failed"})
            if requests:
                result["measurement_status"] = "partial"
    return result
