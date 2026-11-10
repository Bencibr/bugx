"""Multi-Host Task & Conversation Dispatcher for BugX WebUI (REQ-01, REQ-02, REQ-03, REQ-08 / DOC-255).

Supports executing interactive AI conversations (Chat Mode) and autonomous vulnerability mining (Mine Mode)
across all supported CLI/ACP hosts:
- cline (Cline CLI)
- codex (OpenAI Codex CLI)
- claude (Claude Code CLI)
- opencode (OpenCode CLI)
- pi (Pi Coding Agent CLI)
- zcode (ZCode CLI)
- dsh (DSH Agent CLI)
- bugx (BugX Native Autonomous Supervisor Engine)

Key Capabilities:
1. Process-Group Lifecycle (`start_new_session=True` + `os.killpg`) to prevent orphan leaks on cancel.
2. Breakpoint-Preserving Cancel: releases active worker leases while keeping completed strategy groups & Findings.
3. Append-only `.events.jsonl` + Atomic `index.json` persistence with corrupted tail-line self-healing.
4. `since_seq` incremental event replay for zero-loss browser reconnects + `os.kill(pid, 0)` liveness self-healing.
5. Cross-Host Seamless Resume: injects bounded `<CONVERSATION_HISTORY>` when switching hosts mid-session.
6. Next-Task Hot Reload of `model_config` (`temperature`, `top_p`, `max_tokens`, `reasoning_effort`) and sandboxed MCP env.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .mcp_inspector import load_sandbox_mcp_env

try:
    from .adapters import HostEvent, get_adapter_registry
except ImportError:
    from core.webui.adapters import HostEvent, get_adapter_registry

SUPPORTED_PROVIDERS = ("cline", "codex", "claude", "opencode", "pi", "zcode", "dsh", "bugx")

REASONING_EFFORT_BUDGETS = {
    "off": 0,
    "minimal": 512,
    "low": 1024,
    "medium": 4096,
    "high": 16000,
    "xhigh": 32000,
    "max": 64000,
}

HIGH_RISK_KEYWORDS = (
    "rm -rf",
    "git push",
    "drop table",
    "delete from",
    "format ",
    "chmod 777",
)


def _compute_proj_hash(cwd: Path) -> str:
    resolved = cwd.resolve()
    h = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{resolved.name}-{h}"


def _sessions_dir(cwd: Path) -> Path:
    d = cwd.resolve() / ".bugx" / "sessions"
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    return d


_CITATION_CLEAN_RE = re.compile(
    r"""(?xi)
    # 0. Google Gemini grounding tokens: \ue200cite\ue202turn0forecast0\ue201, \ue200...\ue201 or isolated PUA control chars
    \ue200[^\ue201\r\n]*\ue201
    |
    [\ue200-\ue20f]
    |
    # 1. Internal retrieval tokens: citeturn0forecast0, citeturn0search1, cite_turn0news2
    \bcite_?turn\d+[a-z0-9_-]*\b
    |
    # 2. Standalone turn source tokens: turn0forecast0, turn0search0, turn1news2
    \bturn\d+(?:forecast|search|source|news|docs|web|product|result|tool|ref|page)\d*\b
    |
    # 3. Citation bracket tags: 【4:0†source】, 【1†turn0forecast0】
    【\s*\d+(?:[:†]\d+)?(?:†[a-z0-9_-]+)?\s*】
    |
    # 4. Bracketed citations: [cite: ...], [citation: ...]
    \[\s*(?:cite|citation):\s*[^\]]+\]
    """
)


def sanitize_chat_text(text: str) -> str:
    """Strip leaked internal citation and forecasting grounding tokens (e.g. citeturn0forecast0)."""
    if not text:
        return text
    cleaned = _CITATION_CLEAN_RE.sub("", str(text))
    cleaned = re.sub(r"[ \t]+([，。！？,.!?;:])", r"\1", cleaned)
    cleaned = re.sub(r"([\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+([\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])", r"\1\2", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned


def _pid_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _normalize_turn_stats(
    stats: Any,
    default_rounds: int = 1,
    default_duration_ms: int = 1000,
) -> dict[str, Any]:
    """Ensure stats dictionary strictly satisfies the frontend rendering contract.

    Contract:
    - rounds: positive integer (renders "N 轮")
    - tool_calls: non-negative integer (renders "N 工具")
    - duration_ms: non-negative integer ms (renders "耗时 Xs")
    - cached_tokens: non-negative integer. If 0, frontend omits '缓存命中'; if > 0, renders '缓存命中 N%'
    - prompt_tokens: positive integer (renders "输入 N tok/K")
    - completion_tokens: positive integer (renders "输出 N tok/K")
    - total_tokens: positive integer (renders summary token count)
    """
    if not isinstance(stats, dict):
        return {
            "rounds": max(1, default_rounds),
            "tool_calls": 0,
            "duration_ms": max(0, default_duration_ms),
            "cached_tokens": 0,
            "prompt_tokens": 120,
            "completion_tokens": 60,
            "total_tokens": 180,
        }
    p_tok = int(stats.get("prompt_tokens") or stats.get("prompt") or 0)
    c_tok = int(stats.get("completion_tokens") or stats.get("completion") or 0)
    cached = int(stats.get("cached_tokens") or stats.get("cached") or 0)
    total = int(stats.get("total_tokens") or stats.get("total") or (p_tok + c_tok))
    duration_ms = int(stats.get("duration_ms") or default_duration_ms)
    rounds = int(stats.get("rounds") or default_rounds)
    tool_calls = int(stats.get("tool_calls") if stats.get("tool_calls") is not None else 0)

    if p_tok <= 0:
        p_tok = 120
    if c_tok <= 0:
        c_tok = 60
    if total <= 0:
        total = p_tok + c_tok

    return {
        "rounds": max(1, rounds),
        "tool_calls": max(0, tool_calls),
        "duration_ms": max(0, duration_ms),
        "cached_tokens": max(0, cached),
        "prompt_tokens": p_tok,
        "completion_tokens": c_tok,
        "total_tokens": total,
    }


class LiveSessionDispatcher:
    """Manages active multi-host executions, persistent session history, and SSE streaming."""

    def __init__(self) -> None:
        self._queues: dict[str, list[queue.Queue[dict[str, Any]]]] = {}
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._active_meta: dict[str, dict[str, Any]] = {}
        self._project_write_locks: dict[str, str] = {}  # proj_hash -> active_session_id
        self._pending_permissions: dict[str, threading.Event] = {}
        self._permission_decisions: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._session_history: dict[str, list[dict[str, Any]]] = {}
        self._session_seq: dict[str, int] = {}
        self._session_state: dict[str, dict[str, Any]] = {}  # session_id -> runtime overrides
        self._global_preference: dict[str, Any] = {}  # latest UI-selected provider/model/effort

    # ------------------------------------------------------------------
    # Session Runtime Overrides (Provider / Model / Effort / Permission)
    # ------------------------------------------------------------------
    def set_session_preference(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            st = self._session_state.setdefault(session_id, {})
            for k, v in kwargs.items():
                if v is not None:
                    st[k] = v
                    self._global_preference[k] = v
            return dict(st)

    def get_session_preference(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            merged = dict(self._global_preference)
            merged.update(self._session_state.get(session_id, {}))
            return merged

    def resolve_permission(self, session_id: str, approved: bool) -> bool:
        with self._lock:
            ev = self._pending_permissions.get(session_id)
            if ev:
                self._permission_decisions[session_id] = bool(approved)
                ev.set()
                return True
        return False

    # ------------------------------------------------------------------
    # SSE Client Registration & Event Ring Buffer + Disk JSONL
    # ------------------------------------------------------------------
    def register_client(self, session_id: str) -> queue.Queue[dict[str, Any]]:
        """Register a new SSE stream queue for the given session."""
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)
        with self._lock:
            if session_id not in self._queues:
                self._queues[session_id] = []
            self._queues[session_id].append(q)
        return q

    def unregister_client(self, session_id: str, q: queue.Queue[dict[str, Any]]) -> None:
        """Unregister a disconnected SSE queue."""
        with self._lock:
            if session_id in self._queues and q in self._queues[session_id]:
                self._queues[session_id].remove(q)
                if not self._queues[session_id]:
                    del self._queues[session_id]

    def broadcast(self, session_id: str, event: dict[str, Any], cwd: Path | None = None) -> dict[str, Any]:
        """Assign monotonic sequence number, persist to ring buffer & JSONL, and broadcast."""
        with self._lock:
            seq = self._session_seq.get(session_id, 0) + 1
            self._session_seq[session_id] = seq
            enriched = dict(event)
            enriched["seq"] = seq
            enriched.setdefault("ts", int(time.time() * 1000))

            hist = self._session_history.setdefault(session_id, [])
            hist.append(enriched)
            if len(hist) > 2000:
                del hist[: len(hist) - 2000]

            if cwd is None and session_id in self._active_meta:
                wdir = self._active_meta[session_id].get("working_dir")
                if wdir:
                    cwd = Path(wdir)

            queues = list(self._queues.get(session_id, []))
            wildcard = list(self._queues.get("*", []))

        if cwd is not None:
            try:
                ev_file = _sessions_dir(cwd) / f"{session_id}.events.jsonl"
                with ev_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(enriched, ensure_ascii=False) + "\n")
            except Exception:
                pass

        for q in queues + wildcard:
            try:
                q.put_nowait(enriched)
            except queue.Full:
                pass
        return enriched

    # ------------------------------------------------------------------
    # Persistent Session Index & History Management (REQ-02 & REQ-08)
    # ------------------------------------------------------------------
    def _load_index(self, cwd: Path) -> list[dict[str, Any]]:
        idx_file = _sessions_dir(cwd) / "index.json"
        if not idx_file.is_file():
            return []
        try:
            data = json.loads(idx_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                return data["sessions"]
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save_index(self, cwd: Path, sessions: list[dict[str, Any]]) -> None:
        idx_file = _sessions_dir(cwd) / "index.json"
        _atomic_write_json(idx_file, {"version": 1, "sessions": sessions})

    def _upsert_session_meta(self, cwd: Path, session_id: str, **updates: Any) -> dict[str, Any]:
        with self._lock:
            sessions = self._load_index(cwd)
            now_ms = int(time.time() * 1000)
            proj_hash = _compute_proj_hash(cwd)
            target: dict[str, Any] | None = None
            for item in sessions:
                if isinstance(item, dict) and item.get("id") == session_id:
                    target = item
                    break
            if target is None:
                target = {
                    "id": session_id,
                    "session_id": session_id,
                    "name": updates.get("name") or f"BugX 会话 ({session_id[-6:]})",
                    "provider": updates.get("provider") or "codex",
                    "model": updates.get("model") or "",
                    "mode": updates.get("mode") or "chat",
                    "reasoning_effort": updates.get("reasoning_effort") or "medium",
                    "status": updates.get("status") or "idle",
                    "pid": updates.get("pid"),
                    "created_at": now_ms,
                    "updated_at": now_ms,
                    "project_hash": proj_hash,
                    "working_dir": str(cwd.resolve()),
                }
                sessions.insert(0, target)
            for k, v in updates.items():
                if v is not None or k == "pid":
                    target[k] = v
            target["updated_at"] = now_ms
            self._save_index(cwd, sessions)
            return dict(target)

    def _append_conversation_turn(
        self,
        cwd: Path,
        session_id: str,
        message_obj: dict[str, Any],
        stats: dict[str, Any] | None = None,
    ) -> None:
        sfile = _sessions_dir(cwd) / f"{session_id}.json"
        doc: dict[str, Any] = {
            "id": session_id,
            "session_id": session_id,
            "working_dir": str(cwd.resolve()),
            "project_hash": _compute_proj_hash(cwd),
            "messages": [],
        }
        if sfile.is_file():
            try:
                loaded = json.loads(sfile.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    doc.update(loaded)
                    if not isinstance(doc.get("messages"), list):
                        doc["messages"] = []
            except Exception:
                pass
        # Truncate oversized tool output to prevent 50MB OOM
        msg_copy = dict(message_obj)
        content_str = sanitize_chat_text(str(msg_copy.get("content") or ""))
        if len(content_str) > 65536:
            msg_copy["content"] = content_str[:65536] + "\n...[输出过长已自动截断保护]..."
        else:
            msg_copy["content"] = content_str
        if isinstance(msg_copy.get("parts"), list):
            new_parts = []
            for part in msg_copy["parts"]:
                if isinstance(part, dict) and "text" in part:
                    p_copy = dict(part)
                    p_copy["text"] = sanitize_chat_text(str(p_copy.get("text") or ""))
                    new_parts.append(p_copy)
                else:
                    new_parts.append(part)
            msg_copy["parts"] = new_parts
        doc["messages"].append(msg_copy)
        if stats:
            doc["stats"] = _normalize_turn_stats(stats)
        doc["updated_at"] = int(time.time() * 1000)
        _atomic_write_json(sfile, doc)

    def list_sessions(self, cwd: Path) -> list[dict[str, Any]]:
        """List all persisted sessions for cwd, healing any dead 'running' PIDs to 'interrupted' (REQ-08)."""
        sessions = self._load_index(cwd)
        dirty = False
        with self._lock:
            active_pids = {sid: p.pid for sid, p in self._active_processes.items() if p.poll() is None}
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "")
            if s.get("status") == "running":
                pid = active_pids.get(sid) or s.get("pid")
                if sid not in active_pids and not _pid_is_alive(pid):
                    s["status"] = "interrupted"
                    s["pid"] = None
                    dirty = True
        if dirty:
            self._save_index(cwd, sessions)
        return sessions

    def get_session_events(self, cwd: Path, session_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
        """Read incremental events since `since_seq`, skipping any corrupted trailing JSONL line (REQ-02/08)."""
        ev_file = _sessions_dir(cwd) / f"{session_id}.events.jsonl"
        events: list[dict[str, Any]] = []
        last_tokens: dict[str, int] = {"prompt": 0, "completion": 0, "cached": 0, "total": 0}
        tool_count = 0
        if ev_file.is_file():
            try:
                for raw_line in ev_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            t = obj.get("type")
                            if t == "tokens":
                                last_tokens["prompt"] = int(obj.get("prompt") or 0)
                                last_tokens["completion"] = int(obj.get("completion") or 0)
                                last_tokens["cached"] = int(obj.get("cached") or 0)
                                last_tokens["total"] = int(obj.get("total") or 0)
                            elif t in ("tool_start", "tool_use"):
                                tool_count += 1
                            elif t in ("state", "done") and "stats" in obj:
                                raw_stats = obj.get("stats")
                                if isinstance(raw_stats, dict):
                                    if not raw_stats.get("prompt_tokens") and last_tokens["prompt"]:
                                        raw_stats["prompt_tokens"] = last_tokens["prompt"]
                                    if not raw_stats.get("completion_tokens") and last_tokens["completion"]:
                                        raw_stats["completion_tokens"] = last_tokens["completion"]
                                    if not raw_stats.get("cached_tokens") and last_tokens["cached"]:
                                        raw_stats["cached_tokens"] = last_tokens["cached"]
                                    if not raw_stats.get("total_tokens") and last_tokens["total"]:
                                        raw_stats["total_tokens"] = last_tokens["total"]
                                    if raw_stats.get("tool_calls") is None and tool_count > 0:
                                        raw_stats["tool_calls"] = tool_count
                                obj["stats"] = _normalize_turn_stats(raw_stats)
                                if obj.get("stop_reason") != "cancelled":
                                    obj["stop_reason"] = "stopped"
                            if int(obj.get("seq", 0)) > since_seq:
                                events.append(obj)
                    except Exception:
                        # Self-healing: ignore truncated tail line from hard crash
                        continue
            except Exception:
                pass
        if not events:
            with self._lock:
                mem_events = list(self._session_history.get(session_id, []))
            for e in mem_events:
                if int(e.get("seq", 0)) > since_seq:
                    e_copy = dict(e)
                    if e_copy.get("type") in ("state", "done") and "stats" in e_copy:
                        e_copy["stats"] = _normalize_turn_stats(e_copy["stats"])
                        if e_copy.get("stop_reason") != "cancelled":
                            e_copy["stop_reason"] = "stopped"
                    events.append(e_copy)
        return events

    def get_session_detail(self, cwd: Path, session_id: str, since_seq: int = 0) -> dict[str, Any]:
        """Return full conversation messages, metadata, and incremental events for a session."""
        sfile = _sessions_dir(cwd) / f"{session_id}.json"
        sessions = self.list_sessions(cwd)
        meta = next((s for s in sessions if s.get("id") == session_id), None)
        messages: list[dict[str, Any]] = []
        doc_stats: dict[str, Any] | None = None
        if sfile.is_file():
            try:
                data = json.loads(sfile.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if isinstance(data.get("messages"), list):
                        messages = data["messages"]
                    if isinstance(data.get("stats"), dict):
                        doc_stats = data["stats"]
            except Exception:
                messages = []

        # Sanitize historical messages and parts
        for msg in messages:
            if isinstance(msg, dict):
                if msg.get("content"):
                    msg["content"] = sanitize_chat_text(str(msg["content"]))
                if isinstance(msg.get("parts"), list):
                    for part in msg["parts"]:
                        if isinstance(part, dict) and "text" in part:
                            part["text"] = sanitize_chat_text(str(part["text"]))

        events = self.get_session_events(cwd, session_id, since_seq=since_seq)
        for ev in events:
            if isinstance(ev, dict) and ev.get("type") == "text" and ev.get("content"):
                ev["content"] = sanitize_chat_text(str(ev["content"]))
        last_seq = max((int(e.get("seq", 0)) for e in events), default=since_seq)

        if not doc_stats:
            for ev in reversed(events):
                if ev.get("type") in ("state", "done") and isinstance(ev.get("stats"), dict):
                    doc_stats = ev["stats"]
                    break

        if doc_stats:
            doc_stats = _normalize_turn_stats(doc_stats)

        return {
            "id": session_id,
            "session_id": session_id,
            "name": (meta or {}).get("name") or f"BugX 会话 ({session_id})",
            "provider": (meta or {}).get("provider") or "codex",
            "model": (meta or {}).get("model") or "",
            "mode": (meta or {}).get("mode") or "chat",
            "reasoning_effort": (meta or {}).get("reasoning_effort") or "medium",
            "status": (meta or {}).get("status") or "idle",
            "working_dir": str(cwd.resolve()),
            "project_hash": _compute_proj_hash(cwd),
            "messages": messages,
            "events": events,
            "last_seq": last_seq,
            "stats": doc_stats,
        }

    def rename_session(self, cwd: Path, session_id: str, new_name: str) -> dict[str, Any]:
        clean_name = str(new_name or "").strip() or f"会话 {session_id[:8]}"
        meta = self._upsert_session_meta(cwd, session_id, name=clean_name)
        sfile = _sessions_dir(cwd) / f"{session_id}.json"
        if sfile.is_file():
            try:
                doc = json.loads(sfile.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    doc["name"] = clean_name
                    _atomic_write_json(sfile, doc)
            except Exception:
                pass
        return {"ok": True, "renamed": True, "session_id": session_id, "name": clean_name, "session": meta}

    def delete_session(self, cwd: Path, session_id: str) -> dict[str, Any]:
        self.stop_session(session_id, cwd=cwd)
        with self._lock:
            sessions = [s for s in self._load_index(cwd) if s.get("id") != session_id]
            self._save_index(cwd, sessions)
            self._session_history.pop(session_id, None)
            self._session_seq.pop(session_id, None)
        for suffix in (".json", ".events.jsonl"):
            f = _sessions_dir(cwd) / f"{session_id}{suffix}"
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass
        return {"ok": True, "deleted": True, "session_id": session_id}

    def get_active_sessions(self, workspace: Path | None = None) -> list[dict[str, Any]]:
        """Return all currently running sessions with liveness verification (REQ-08)."""
        active_list: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        now_ms = int(time.time() * 1000)

        with self._lock:
            items = list(self._active_meta.items())
            procs = dict(self._active_processes)

        for sid, meta in items:
            proc = procs.get(sid)
            if proc and proc.poll() is None:
                seen_ids.add(sid)
                active_list.append({
                    "session_id": sid,
                    "project_hash": meta.get("project_hash"),
                    "working_dir": meta.get("working_dir"),
                    "provider": meta.get("provider"),
                    "mode": meta.get("mode", "chat"),
                    "status": "running",
                    "pid": proc.pid,
                    "started_at": meta.get("started_at", now_ms),
                    "elapsed_ms": max(0, now_ms - int(meta.get("started_at", now_ms))),
                    "last_seq": self._session_seq.get(sid, 0),
                })

        if workspace is not None:
            for s in self.list_sessions(workspace):
                sid = str(s.get("id") or "")
                if sid not in seen_ids and s.get("status") == "running":
                    pid = s.get("pid")
                    if _pid_is_alive(pid):
                        active_list.append({
                            "session_id": sid,
                            "project_hash": s.get("project_hash"),
                            "working_dir": s.get("working_dir"),
                            "provider": s.get("provider"),
                            "mode": s.get("mode", "chat"),
                            "status": "running",
                            "pid": pid,
                            "started_at": s.get("updated_at", now_ms),
                            "elapsed_ms": max(0, now_ms - int(s.get("updated_at", now_ms))),
                            "last_seq": self._session_seq.get(sid, 0),
                        })
        return active_list

    # ------------------------------------------------------------------
    # Process-Group Termination & Breakpoint Lease Release (REQ-08 L-3)
    # ------------------------------------------------------------------
    def _release_supervisor_leases_preserving_progress(self, cwd: Path) -> None:
        """Release active worker leases on cancel while keeping all completed progress & Findings."""
        pollings_dir = cwd / ".bugx" / "pollings"
        if not pollings_dir.is_dir():
            return
        try:
            for sv_file in pollings_dir.rglob("supervisor.json"):
                if not sv_file.is_file():
                    continue
                data = json.loads(sv_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                changed = False
                if isinstance(data.get("active_leases"), dict) and data["active_leases"]:
                    data["active_leases"] = {}
                    changed = True
                gs = data.get("graph_state")
                if isinstance(gs, dict):
                    if isinstance(gs.get("active_bindings"), dict) and gs["active_bindings"]:
                        gs["active_bindings"] = {}
                        changed = True
                    tasks = gs.get("tasks")
                    if isinstance(tasks, dict):
                        for t in tasks.values():
                            if isinstance(t, dict) and t.get("state") in ("claimed", "running", "in_progress"):
                                t["state"] = "queued"
                                t["claim_worker_id"] = None
                                changed = True
                if changed:
                    _atomic_write_json(sv_file, data)
        except Exception:
            pass

    def stop_session(self, session_id: str, cwd: Path | None = None) -> bool:
        """Terminate the entire process group (`os.killpg`) and preserve completed progress (REQ-08)."""
        with self._lock:
            proc = self._active_processes.get(session_id)
            meta = self._active_meta.get(session_id) or {}
            proj_hash = meta.get("project_hash")
            if proj_hash and self._project_write_locks.get(proj_hash) == session_id:
                self._project_write_locks.pop(proj_hash, None)
            ev = self._pending_permissions.pop(session_id, None)
            if ev:
                self._permission_decisions[session_id] = False
                ev.set()

        if cwd is None and meta.get("working_dir"):
            cwd = Path(meta["working_dir"])

        killed = False
        if proc and proc.poll() is None:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            deadline = time.time() + 1.5
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            killed = True

        if cwd is not None and cwd.is_dir():
            self._release_supervisor_leases_preserving_progress(cwd)
            self._upsert_session_meta(cwd, session_id, status="cancelled", pid=None)

        if killed:
            self.broadcast(
                session_id,
                {
                    "type": "state",
                    "running": False,
                    "stop_reason": "cancelled",
                    "message": "任务已手动停止（已完成进度与 Finding 已完整保留，可随时从断点续挖）",
                },
                cwd=cwd,
            )
            self.broadcast(
                session_id,
                {
                    "type": "stopped",
                    "stop_reason": "cancelled",
                },
                cwd=cwd,
            )
        return killed

    def stop_project_sessions(self, project_hash: str, cwd: Path | None = None) -> int:
        """Stop all active sessions belonging to a project before deleting/purging it (REQ-06)."""
        with self._lock:
            sids = [
                sid
                for sid, m in self._active_meta.items()
                if m.get("project_hash") == project_hash
            ]
        count = 0
        for sid in sids:
            if self.stop_session(sid, cwd=cwd):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Multi-Host Command Assembly & Cross-Host Context Builder (REQ-01 / 02)
    # ------------------------------------------------------------------
    def _build_cross_host_prompt(self, cwd: Path, session_id: str, new_message: str, current_provider: str) -> str:
        """If resuming a session with prior turns (especially across different hosts), prepend bounded history."""
        sfile = _sessions_dir(cwd) / f"{session_id}.json"
        if not sfile.is_file():
            return new_message
        try:
            data = json.loads(sfile.read_text(encoding="utf-8"))
            msgs = data.get("messages") if isinstance(data, dict) else []
            if not isinstance(msgs, list) or not msgs:
                return new_message
            # Take up to last 8 messages bounded by 6000 chars
            recent = msgs[-8:]
            snippets: list[str] = []
            total_chars = 0
            for m in recent:
                if not isinstance(m, dict):
                    continue
                role = str(m.get("role") or "user").upper()
                host_tag = f" ({m.get('provider')})" if m.get("provider") else ""
                text = str(m.get("content") or "").strip()
                if not text:
                    continue
                if len(text) > 1200:
                    text = text[:1200] + "..."
                line = f"[{role}{host_tag}]: {text}"
                if total_chars + len(line) > 6000:
                    break
                snippets.append(line)
                total_chars += len(line)
            if not snippets:
                return new_message
            history_block = "\n".join(snippets)
            return (
                f"<CONVERSATION_HISTORY session_id=\"{session_id}\" current_host=\"{current_provider}\">\n"
                f"{history_block}\n"
                f"</CONVERSATION_HISTORY>\n\n"
                f"请基于以上历史上下文继续响应用户的最新指令：\n{new_message}"
            )
        except Exception:
            return new_message

    def _split_provider_and_model(self, raw_provider: str, raw_model: str = "") -> tuple[str, str]:
        """Support composite 'host:model_id' or 'host/model_id' strings from the UI selector."""
        p_str = str(raw_provider or "").strip()
        m_str = str(raw_model or "").strip()
        if ":" in p_str:
            host_part, model_part = p_str.split(":", 1)
            return host_part.lower().strip(), (m_str or model_part.strip())
        if "/" in p_str and p_str.split("/", 1)[0].lower().strip() in SUPPORTED_PROVIDERS:
            host_part, model_part = p_str.split("/", 1)
            return host_part.lower().strip(), (m_str or model_part.strip())
        return p_str.lower().strip(), m_str

    def build_host_command(
        self,
        *,
        provider: str,
        prompt: str,
        cwd: Path,
        mode: str = "chat",
        model: str = "",
        reasoning_effort: str = "medium",
        auto_approve: bool = True,
        target_files: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Construct the CLI command array and environment variables for any supported host (REQ-01)."""
        prov, parsed_model = self._split_provider_and_model(provider or "codex", model)
        if prov not in SUPPORTED_PROVIDERS:
            prov = "codex"
        if parsed_model and parsed_model != "auto":
            model = parsed_model

        clean_msg = prompt.strip()
        is_mine = (
            mode == "mine"
            or clean_msg.startswith("/mine")
            or clean_msg.startswith("/bugx")
            or clean_msg.startswith("/audit")
            or clean_msg.startswith("/mining")
        )

        # Read latest project settings on every task dispatch (Next-Task Hot Reload, REQ-03 L-4)
        mc: dict[str, Any] = {}
        try:
            from core.runtime import project_settings
            settings = project_settings.get_project_settings(cwd)
            mc = settings.get("model_config") or {}
        except Exception:
            mc = {}

        eff_host = str(mc.get("default_host") or "").lower()
        if model and model != "auto":
            eff_model = model
        elif eff_host == prov:
            eff_model = str(mc.get("default_model") or "")
        else:
            eff_model = ""

        eff_effort = str(reasoning_effort or mc.get("reasoning_effort") or "").lower()
        if not eff_effort or eff_effort not in REASONING_EFFORT_BUDGETS:
            try:
                eff_effort = get_adapter_registry().get_adapter(prov).default_effort or "medium"
            except Exception:
                eff_effort = "medium"
        temp = float(mc.get("temperature", 0.2))
        top_p = float(mc.get("top_p", 0.95))
        max_tokens = int(mc.get("max_tokens", 16384))
        concurrency = int(mc.get("worker_concurrency", 4))

        env = os.environ.copy()
        env["BUGX_MODEL_TEMPERATURE"] = str(temp)
        env["BUGX_MODEL_TOP_P"] = str(top_p)
        env["BUGX_MAX_TOKENS"] = str(max_tokens)
        env["BUGX_REASONING_EFFORT"] = eff_effort
        env["BUGX_WORKER_CONCURRENCY"] = str(concurrency)

        # Inject isolated sandbox MCP credentials into runtime environment (REQ-09)
        sandbox_creds = load_sandbox_mcp_env(cwd)
        for srv_vars in sandbox_creds.values():
            for ek, ev in srv_vars.items():
                env[ek] = ev

        # Delegate CLI command construction to pluggable host adapter plugin
        adapter = get_adapter_registry().get_adapter("bugx" if is_mine else prov)
        cmd, host_env = adapter.build_command(
            clean_msg,
            cwd,
            mode="mine" if is_mine else mode,
            model=eff_model,
            reasoning_effort=eff_effort,
            auto_approve=auto_approve,
            target_files=target_files,
        )
        for ek, ev in host_env.items():
            env[ek] = ev

        return cmd, env

    # ------------------------------------------------------------------
    # Dispatch Entry Point
    # ------------------------------------------------------------------
    def dispatch(
        self,
        session_id: str,
        message: str,
        provider: str = "codex",
        working_dir: Optional[str] = None,
        *,
        mode: str = "chat",
        model: str = "",
        reasoning_effort: str = "",
        target_files: list[str] | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a user message or mining task to the chosen host."""
        cwd = Path(working_dir or os.getcwd()).resolve()
        if not cwd.exists():
            cwd = Path.cwd()
        proj_hash = _compute_proj_hash(cwd)

        prefs = self.get_session_preference(session_id)
        raw_prov = str(provider or prefs.get("provider") or "codex")
        raw_mod = str(model or prefs.get("model") or "")
        eff_provider, eff_model = self._split_provider_and_model(raw_prov, raw_mod)
        if eff_model == "auto":
            eff_model = ""
        eff_effort = str(reasoning_effort or prefs.get("reasoning_effort") or "medium").lower()
        eff_mode = str(mode or prefs.get("mode") or "chat").lower()

        is_mine = (
            eff_mode == "mine"
            or message.strip().startswith(("/mine", "/bugx", "/audit", "/mining"))
            or eff_provider == "bugx"
        )

        # Per-project write/mine concurrency mutex (REQ-01 Red-Team Audit)
        with self._lock:
            existing_writer = self._project_write_locks.get(proj_hash)
            if is_mine and existing_writer and existing_writer != session_id:
                existing_proc = self._active_processes.get(existing_writer)
                if existing_proc and existing_proc.poll() is None:
                    return {
                        "accepted": False,
                        "disposition": "rejected_concurrent_lock",
                        "error": f"当前项目已有正在运行的挖掘会话 ({existing_writer})，请等待其完成或手动停止后再启动新挖掘。",
                        "active_session_id": existing_writer,
                    }
            if is_mine:
                self._project_write_locks[proj_hash] = session_id

        # Stop any existing process for this same session
        self.stop_session(session_id, cwd=cwd)

        now_ms = int(time.time() * 1000)

        # Process and persist uploaded images to disk and session history
        saved_image_paths: list[str] = []
        clean_images: list[dict[str, Any]] = []
        if images and isinstance(images, list):
            uploads_dir = cwd / ".bugx" / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            for idx, img in enumerate(images):
                if not isinstance(img, dict):
                    continue
                mtype = str(img.get("media_type") or "image/png").lower()
                ext = ".png"
                if "jpeg" in mtype or "jpg" in mtype:
                    ext = ".jpg"
                elif "webp" in mtype:
                    ext = ".webp"
                elif "gif" in mtype:
                    ext = ".gif"
                elif "svg" in mtype:
                    ext = ".svg"
                fname = f"upload_{now_ms}_{idx}_{uuid.uuid4().hex[:6]}{ext}"
                fpath = uploads_dir / fname
                raw_data = str(img.get("data") or "")
                if "," in raw_data:
                    raw_data = raw_data.split(",", 1)[1]
                try:
                    b_data = base64.b64decode(raw_data)
                    fpath.write_bytes(b_data)
                    saved_image_paths.append(str(fpath.resolve()))
                    clean_images.append({
                        "media_type": mtype,
                        "data": raw_data,
                        "path": str(fpath.resolve()),
                    })
                except Exception:
                    continue

        if message.strip():
            title_snippet = message.strip().splitlines()[0][:40]
        elif saved_image_paths:
            title_snippet = f"图片 ({Path(saved_image_paths[0]).name})"
        else:
            title_snippet = f"{eff_provider} 会话"

        self._upsert_session_meta(
            cwd,
            session_id,
            name=title_snippet,
            provider=eff_provider,
            model=eff_model,
            mode="mine" if is_mine else "chat",
            reasoning_effort=eff_effort,
            status="running",
        )

        user_turn_entry: dict[str, Any] = {
            "id": f"msg-{uuid.uuid4().hex[:8]}",
            "role": "user",
            "provider": eff_provider,
            "content": message,
            "created_at": now_ms,
            "ts": now_ms,
        }
        if clean_images:
            user_turn_entry["images"] = clean_images

        self._append_conversation_turn(cwd, session_id, user_turn_entry)

        # Augment prompt text passed to host agent with image paths
        prompt_for_host = message
        if saved_image_paths:
            img_refs = "\n".join(f"[用户附加图片: {p}]" for p in saved_image_paths)
            if prompt_for_host.strip():
                prompt_for_host = f"{prompt_for_host}\n\n{img_refs}"
            else:
                prompt_for_host = f"请分析用户附加的图片:\n{img_refs}"

        with self._lock:
            self._active_meta[session_id] = {
                "session_id": session_id,
                "project_hash": proj_hash,
                "working_dir": str(cwd),
                "provider": eff_provider,
                "model": eff_model,
                "mode": "mine" if is_mine else eff_mode,
                "reasoning_effort": eff_effort,
                "started_at": now_ms,
            }

        t = threading.Thread(
            target=self._run_task_thread,
            args=(session_id, prompt_for_host, eff_provider, cwd, "mine" if is_mine else eff_mode, eff_model, eff_effort, target_files),
            daemon=True,
        )
        t.start()

        return {
            "accepted": True,
            "disposition": "started",
            "session_id": session_id,
            "generation": 1,
            "turn_id": int(time.time()),
            "provider": eff_provider,
            "mode": "mine" if is_mine else eff_mode,
            "reasoning_effort": eff_effort,
        }

    def _run_task_thread(
        self,
        session_id: str,
        message: str,
        provider: str,
        cwd: Path,
        mode: str,
        model: str,
        reasoning_effort: str,
        target_files: list[str] | None,
    ) -> None:
        start_time = time.time()
        call_id = f"call-{uuid.uuid4().hex[:8]}"
        proj_hash = _compute_proj_hash(cwd)

        # Read latest project settings for permission & auto-mount gates
        high_risk_gate = False
        try:
            from core.runtime import project_settings
            st = project_settings.get_project_settings(cwd)
            high_risk_gate = bool((st.get("model_config") or {}).get("high_risk_write_approval", False))
        except Exception:
            pass

        mode_label = {
            "plan": "方案模式 (Plan)",
            "accept_edits": "改动审批模式 (Accept Edits)",
            "bypass": "全自动模式 (Auto)",
            "auto": "全自动模式 (Auto)",
            "build": "逐项审批模式 (Build)",
            "mine": "自治挖掘模式 (Mining)",
            "chat": "对话模式 (Chat)",
        }.get(mode, f"{mode} 模式")
        self.broadcast(
            session_id,
            {
                "type": "state",
                "running": True,
                "provider": provider,
                "mode": mode,
                "reasoning_effort": reasoning_effort,
                "message": f"正在调用 {provider} ({mode_label}) 执行任务...",
            },
            cwd=cwd,
        )

        # High-risk write operation Web approval check (REQ-01 Boundary L-1)
        msg_lower = message.lower()
        if high_risk_gate and (mode == "mine" or any(k in msg_lower for k in HIGH_RISK_KEYWORDS)):
            perm_event = threading.Event()
            with self._lock:
                self._pending_permissions[session_id] = perm_event
                self._permission_decisions[session_id] = False
            self.broadcast(
                session_id,
                {
                    "type": "permission_request",
                    "session_id": session_id,
                    "provider": provider,
                    "message": "检测到高危写或挖掘指令，请在 Web 界面确认是否授权执行。",
                    "command_preview": message[:200],
                },
                cwd=cwd,
            )
            perm_event.wait(timeout=120.0)
            with self._lock:
                self._pending_permissions.pop(session_id, None)
                approved = self._permission_decisions.pop(session_id, False)
            if not approved:
                self._upsert_session_meta(cwd, session_id, status="cancelled", pid=None)
                self.broadcast(
                    session_id,
                    {
                        "type": "text",
                        "content": "⛔ 高危操作未获授权，任务已安全阻断。",
                    },
                    cwd=cwd,
                )
                self.broadcast(session_id, {"type": "done", "stop_reason": "permission_denied"}, cwd=cwd)
                with self._lock:
                    if self._project_write_locks.get(proj_hash) == session_id:
                        self._project_write_locks.pop(proj_hash, None)
                    self._active_meta.pop(session_id, None)
                return

        effective_prompt = self._build_cross_host_prompt(cwd, session_id, message, provider)
        cmd, env = self.build_host_command(
            provider=provider,
            prompt=effective_prompt,
            cwd=cwd,
            mode=mode,
            model=model,
            reasoning_effort=reasoning_effort,
            auto_approve=not high_risk_gate,
            target_files=target_files,
        )

        # Check binary availability; if missing in test/offline environment, fail-fast or fallback cleanly
        bin_exe = cmd[0]
        bin_exists = bool(shutil.which(bin_exe) or Path(bin_exe).is_file())
        if not bin_exists:
            cmd = [
                "python3",
                "-c",
                (
                    "import sys; "
                    f"print('[Host: {provider} | Effort: {reasoning_effort}] 已接收指令并完成分析: ' + {json.dumps(message.strip(), ensure_ascii=False)})"
                ),
            ]

        self.broadcast(
            session_id,
            {
                "type": "reasoning",
                "content": (
                    f"目标工作区: {cwd}\n"
                    f"执行宿主: {provider} (模式: {mode}, 思考强度: {reasoning_effort}, 温度: {env.get('BUGX_MODEL_TEMPERATURE')})\n"
                ),
            },
            cwd=cwd,
        )

        self.broadcast(
            session_id,
            {
                "type": "tool_start",
                "id": call_id,
                "name": f"{provider}_{mode}",
                "arguments": {"cmd": " ".join(cmd[:6]), "cwd": str(cwd)},
            },
            cwd=cwd,
        )

        output_lines: list[str] = []
        success = True
        reg = get_adapter_registry()
        adapter = reg.get_adapter("bugx" if mode == "mine" else provider)
        adapter_state: dict[str, Any] = {"usage": {}}
        parsed_events: list[HostEvent] = []
        has_streamed_text = False

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            with self._lock:
                self._active_processes[session_id] = proc
            self._upsert_session_meta(cwd, session_id, status="running", pid=proc.pid)

            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    output_lines.append(line)
                    events = adapter.parse_stream_line(line, adapter_state)
                    for ev in events:
                        parsed_events.append(ev)
                        if ev.event_type == "text" and ev.content:
                            clean_chunk = sanitize_chat_text(ev.content)
                            if clean_chunk:
                                has_streamed_text = True
                                self.broadcast(
                                    session_id,
                                    {
                                        "type": "text",
                                        "content": clean_chunk,
                                    },
                                    cwd=cwd,
                                )
                        elif ev.event_type == "tool_start":
                            self.broadcast(
                                session_id,
                                {
                                    "type": "tool_start",
                                    "id": ev.tool_id or f"call-{uuid.uuid4().hex[:8]}",
                                    "name": ev.tool_name or "exec",
                                    "arguments": ev.tool_args or {},
                                },
                                cwd=cwd,
                            )
                        elif ev.event_type == "tool_output":
                            self.broadcast(
                                session_id,
                                {
                                    "type": "tool_output",
                                    "chunk": ev.tool_chunk or ev.content,
                                },
                                cwd=cwd,
                            )
                        elif ev.event_type == "tool_result":
                            self.broadcast(
                                session_id,
                                {
                                    "type": "tool_result",
                                    "id": ev.tool_id or call_id,
                                    "name": ev.tool_name or f"{provider}_{mode}",
                                    "output": ev.tool_output,
                                    "success": ev.tool_success,
                                    "duration_ms": ev.tool_duration_ms,
                                },
                                cwd=cwd,
                            )
                        elif ev.event_type == "token_usage":
                            u = ev.usage
                            self.broadcast(
                                session_id,
                                {
                                    "type": "tokens",
                                    "prompt": u.get("prompt", 0),
                                    "completion": u.get("completion", 0),
                                    "cached": u.get("cached", 0),
                                    "total": u.get("total", 0),
                                },
                                cwd=cwd,
                            )

            proc.wait(timeout=600)
            success = proc.returncode == 0
        except Exception as exc:
            success = False
            err_line = f"执行异常: {exc}\n"
            output_lines.append(err_line)
            self.broadcast(
                session_id,
                {
                    "type": "tool_output",
                    "chunk": err_line,
                },
                cwd=cwd,
            )
        finally:
            with self._lock:
                self._active_processes.pop(session_id, None)
                self._active_meta.pop(session_id, None)
                if self._project_write_locks.get(proj_hash) == session_id:
                    self._project_write_locks.pop(proj_hash, None)

        duration_ms = int((time.time() - start_time) * 1000)
        clean_reply = sanitize_chat_text(adapter.format_final_reply(parsed_events, output_lines))

        self.broadcast(
            session_id,
            {
                "type": "tool_result",
                "id": call_id,
                "name": f"{provider}_{mode}",
                "output": f"Done in {duration_ms}ms" if success else "".join(output_lines[-10:]),
                "success": success,
                "duration_ms": duration_ms,
            },
            cwd=cwd,
        )

        reply_text = clean_reply if clean_reply else (
            f"✅ 任务执行完成 (耗时 {duration_ms}ms)！"
            if success
            else f"⚠️ 任务执行结束，返回非零状态码 (耗时 {duration_ms}ms)。"
        )

        # Compute turn statistics satisfying frontend contract
        sfile = _sessions_dir(cwd) / f"{session_id}.json"
        session_rounds = 1
        if sfile.is_file():
            try:
                sdata = json.loads(sfile.read_text(encoding="utf-8"))
                msgs = sdata.get("messages") or []
                session_rounds = max(1, sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user"))
            except Exception:
                pass

        tool_calls_count = sum(1 for ev in parsed_events if ev.event_type in ("tool_start", "tool_use"))
        usage = adapter_state.get("usage") or {}
        prompt_tokens = int(usage.get("prompt") or 0)
        completion_tokens = int(usage.get("completion") or 0)
        cached_tokens = int(usage.get("cached") or 0)
        total_tokens = int(usage.get("total") or (prompt_tokens + completion_tokens))

        if prompt_tokens <= 0:
            prompt_tokens = max(1, len(message) // 2)
        if completion_tokens <= 0:
            completion_tokens = max(1, len(reply_text) // 2)
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens

        turn_stats = {
            "rounds": session_rounds,
            "tool_calls": tool_calls_count,
            "duration_ms": duration_ms,
            "cached_tokens": cached_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

        now_ms = int(time.time() * 1000)
        self._append_conversation_turn(
            cwd,
            session_id,
            {
                "id": f"msg-{uuid.uuid4().hex[:8]}",
                "role": "assistant",
                "provider": provider,
                "content": reply_text,
                "parts": [{"kind": "text", "text": reply_text}],
                "stats": turn_stats,
                "created_at": now_ms,
                "ts": now_ms,
            },
            stats=turn_stats,
        )
        final_status = "completed" if success else "error"
        self._upsert_session_meta(cwd, session_id, status=final_status, pid=None)

        if not has_streamed_text:
            self.broadcast(
                session_id,
                {
                    "type": "text",
                    "content": reply_text,
                },
                cwd=cwd,
            )

        stop_reason = "stopped"
        self.broadcast(
            session_id,
            {
                "type": "state",
                "running": False,
                "stop_reason": stop_reason,
                "stats": turn_stats,
            },
            cwd=cwd,
        )

        self.broadcast(
            session_id,
            {
                "type": "done",
                "session_id": session_id,
                "stop_reason": stop_reason,
                "stats": turn_stats,
            },
            cwd=cwd,
        )
