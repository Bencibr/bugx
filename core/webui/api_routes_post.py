"""POST route handlers for WebUIHandler."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import core.webui.workspace_service as _ws

_load_registered_projects = _ws._load_registered_projects
_save_registered_projects = _ws._save_registered_projects

try:
    from core.webui.mcp_inspector import (
        auto_match_project_capabilities,
        install_remote_skill,
        install_skill,
        save_sandbox_mcp_env,
        toggle_project_mcp,
        toggle_project_skill,
        uninstall_skill,
    )
except ImportError:
    try:
        from core.webui.mcp_inspector import (
            auto_match_project_capabilities,
            install_remote_skill,
            install_skill,
            save_sandbox_mcp_env,
            toggle_project_mcp,
            toggle_project_skill,
            uninstall_skill,
        )
    except ImportError:
        auto_match_project_capabilities = None
        install_remote_skill = None
        install_skill = None
        save_sandbox_mcp_env = None
        toggle_project_mcp = None
        toggle_project_skill = None
        uninstall_skill = None

_SHARED_APPROVAL_MODE = "build"

def handle_api_post(self, path: str, req_handler: Any, body_bytes: bytes) -> bool:
    """Handle POST API endpoints."""
    parsed = urlparse(path)
    p = parsed.path

    data: dict[str, Any] = {}
    if body_bytes:
        try:
            data = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            pass

    if p == "/approval_mode":
        global _SHARED_APPROVAL_MODE
        mode = str(data.get("mode") or "build").strip().lower()
        valid_modes = {"build", "accept_edits", "bypass", "auto", "plan"}
        if mode not in valid_modes:
            mode = "build"
        self._approval_mode = mode
        mod = sys.modules.get("core.webui.handler") or sys.modules.get("webui.handler")
        if mod is not None:
            mod._SHARED_APPROVAL_MODE = mode
        self._send_json(req_handler, {"ok": True, "mode": mode})
        return True

    if p == "/chat":
        msg = data.get("message", "")
        provider = data.get("provider", "codex")
        sid = data.get("session_id", "session-default-01")
        appr_mode = str(data.get("approval_mode") or data.get("mode") or getattr(self, "_approval_mode", _SHARED_APPROVAL_MODE) or "build").strip().lower()
        model = data.get("model", "")
        effort = data.get("reasoning_effort", "")
        target_files = data.get("target_files")
        images = data.get("images") if isinstance(data.get("images"), list) else None

        req_handler.send_response(200)
        req_handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        req_handler.send_header("Cache-Control", "no-cache")
        req_handler.send_header("Connection", "close")
        req_handler.send_header("Access-Control-Allow-Origin", "*")
        req_handler.end_headers()

        q = self.dispatcher.register_client(sid)
        self._write_sse(req_handler, {"type": "runtime_info", "provider": provider, "mode": appr_mode})
        self._write_sse(req_handler, {"type": "session_assigned", "session_id": sid})

        receipt = self.dispatcher.dispatch(
            session_id=sid,
            message=msg,
            provider=provider,
            working_dir=data.get("working_dir") or str(self.workspace),
            mode=appr_mode,
            model=model,
            reasoning_effort=effort,
            target_files=target_files if isinstance(target_files, list) else None,
            images=images,
        )
        if not receipt.get("accepted"):
            self._write_sse(req_handler, {"type": "error", "message": receipt.get("error", "Rejected")})
            self.dispatcher.unregister_client(sid, q)
            return True

        try:
            while True:
                try:
                    event = q.get(timeout=1.0)
                    self._write_sse(req_handler, event)
                    if event.get("type") in ("done", "stopped", "error"):
                        break
                except queue.Empty:
                    req_handler.wfile.write(b": ping\n\n")
                    req_handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.dispatcher.unregister_client(sid, q)
        return True

    if p == "/live/message":
        msg = data.get("message", "")
        provider = data.get("provider", "codex")
        sid = data.get("session_id", "session-default-01")
        proj_hash = data.get("project_hash")
        target_dir = Path(data["working_dir"]).resolve() if data.get("working_dir") else self._resolve_target_project_dir(proj_hash)
        images = data.get("images") if isinstance(data.get("images"), list) else None
        appr_mode = str(data.get("approval_mode") or data.get("mode") or getattr(self, "_approval_mode", _SHARED_APPROVAL_MODE) or "build").strip().lower()
        receipt = self.dispatcher.dispatch(
            session_id=sid,
            message=msg,
            provider=provider,
            working_dir=str(target_dir),
            mode=appr_mode,
            model=str(data.get("model") or ""),
            reasoning_effort=str(data.get("reasoning_effort") or ""),
            target_files=data.get("target_files") if isinstance(data.get("target_files"), list) else None,
            images=images,
        )
        self._send_json(req_handler, receipt, status=200 if receipt.get("accepted") else 409)
        return True

    if p in ("/live/stop", "/chat/stop"):
        sid = data.get("session_id", "session-default-01")
        proj_hash = data.get("project_hash")
        target_dir = self._resolve_target_project_dir(proj_hash)
        stopped = self.dispatcher.stop_session(sid, cwd=target_dir)
        self._send_json(req_handler, {"accepted": True, "stopped": stopped, "session_id": sid})
        return True

    if p == "/live/switch_session":
        sid = str(data.get("session_id") or "session-default-01")
        proj_hash = data.get("project_hash")
        target_dir = self._resolve_target_project_dir(proj_hash)
        detail = self.dispatcher.get_session_detail(target_dir, sid)
        active_turn = sid in self.dispatcher._active_processes

        switch_event = {
            "type": "session_switched",
            "session_id": sid,
        }
        with self.dispatcher._lock:
            for qlist in self.dispatcher._queues.values():
                for q in qlist:
                    try:
                        q.put_nowait(switch_event)
                    except Exception:
                        pass

        if detail.get("stats") and not active_turn:
            state_event = {
                "type": "state",
                "running": False,
                "stop_reason": "stopped",
                "stats": detail["stats"],
            }
            with self.dispatcher._lock:
                for qlist in self.dispatcher._queues.values():
                    for q in qlist:
                        try:
                            q.put_nowait(state_event)
                        except Exception:
                            pass

        self._send_json(req_handler, {
            "ok": True,
            "session_id": sid,
            "active_turn": active_turn,
        })
        return True

    if p == "/auth/login/start":
        res = self._start_cloud_login(data)
        self._send_json(req_handler, res)
        return True

    if p.startswith("/auth/login/") and p.endswith("/poll"):
        login_id = p.split("/auth/login/")[1].split("/poll")[0]
        res = self._poll_cloud_login(login_id)
        self._send_json(req_handler, res)
        return True

    if p == "/auth/logout":
        res = self._logout_cloud()
        self._send_json(req_handler, res)
        return True

    if p == "/auth/bind":
        res = self._bind_cloud_key(data)
        self._send_json(req_handler, res)
        return True

    if p == "/cloud/sync":
        res = self._trigger_cloud_sync(data)
        self._send_json(req_handler, res)
        return True

    if p == "/project/settings":
        try:
            from core.runtime import project_settings
        except ImportError:
            from core.runtime import project_settings as project_settings
        try:
            res = project_settings.update_project_settings(self.workspace, data)
            self._send_json(req_handler, res)
        except ValueError as exc:
            self._send_json(req_handler, {"error": str(exc)}, status=400)
        return True

    if p.startswith("/projects/") and p.endswith("/settings"):
        proj_hash = p.split("/projects/")[1].split("/settings")[0]
        try:
            from core.runtime import project_settings
        except ImportError:
            from core.runtime import project_settings as project_settings
        target_dir = project_settings.resolve_project_dir_by_hash(proj_hash)
        if not target_dir and proj_hash == self._active_project_hash:
            target_dir = self.workspace
        if not target_dir:
            self._send_json(req_handler, {"error": "Project not found"}, status=404)
            return True
        try:
            res = project_settings.update_project_settings(target_dir, data)
            res["is_active_workspace"] = (target_dir.resolve() == self.workspace.resolve())
            self._send_json(req_handler, res)
        except ValueError as exc:
            self._send_json(req_handler, {"error": str(exc)}, status=400)
        return True

    if p.startswith("/projects/") and p.endswith("/archive"):
        proj_hash = p.split("/projects/")[1].split("/archive")[0]
        archived_flag = bool(data.get("archived", True))
        try:
            import core.bugx_paths as bugx_paths
            updated = bugx_paths.archive_global_project(
                proj_hash, archived=archived_flag, workspace=self.workspace
            )
        except Exception:
            updated = None
        if not updated:
            self._send_json(req_handler, {"error": "Project not found"}, status=404)
            return True
        self._send_json(req_handler, {"ok": True, "archived": archived_flag, "project": updated})
        return True

    if p == "/api/ide/open":
        res, status_code = self._open_in_ide(data, req_handler)
        self._send_json(req_handler, res, status=status_code)
        return True

    if p == "/api/mcp/toggle":
        target_dir = self._resolve_target_project_dir(data.get("project_hash"))
        srv_name = str(data.get("name") or data.get("server") or data.get("mcp_id") or "").strip()
        if not srv_name:
            self._send_json(req_handler, {"error": "Missing server name"}, status=400)
            return True
        enabled = bool(data.get("enabled", True))
        mount = bool(data.get("mount", enabled))
        cmd = data.get("command")
        args = data.get("args")
        res = toggle_project_mcp(
            target_dir,
            srv_name,
            enabled=enabled,
            mount=mount,
            command=str(cmd) if cmd else None,
            args=[str(a) for a in args] if isinstance(args, list) else None,
        )
        self._send_json(req_handler, {"ok": True, "mcps": res})
        return True

    if p == "/api/mcp/configure":
        target_dir = self._resolve_target_project_dir(data.get("project_hash"))
        srv_name = str(data.get("name") or data.get("server") or data.get("mcp_id") or "").strip()
        env_vars = data.get("env") if isinstance(data.get("env"), dict) else {}
        if not srv_name:
            self._send_json(req_handler, {"error": "Missing server name"}, status=400)
            return True
        res = save_sandbox_mcp_env(target_dir, srv_name, env_vars)
        self._send_json(req_handler, res)
        return True

    if p == "/api/skills/toggle":
        target_dir = self._resolve_target_project_dir(data.get("project_hash"))
        sk_name = str(data.get("name") or data.get("skill") or data.get("skill_name") or "").strip()
        if not sk_name:
            self._send_json(req_handler, {"error": "Missing skill name"}, status=400)
            return True
        enabled = bool(data.get("enabled", True))
        mount = bool(data.get("mount", enabled))
        res = toggle_project_skill(target_dir, sk_name, enabled=enabled, mount=mount)
        self._send_json(req_handler, {"ok": True, "skills": res})
        return True

    if p == "/api/skills/install":
        sk_name = str(data.get("name") or data.get("skill") or data.get("skill_name") or "").strip()
        target = str(data.get("target") or "project").strip()
        target_path_str = data.get("target_path") or data.get("project_path")
        target_dir = self._resolve_target_project_dir(data.get("project_hash"))
        if target_path_str:
            target_dir = Path(target_path_str).resolve()
        if not sk_name:
            self._send_json(req_handler, {"error": "Missing skill name"}, status=400)
            return True
        res = install_skill(sk_name, target=target, target_workspace=target_dir)
        self._send_json(req_handler, res)
        return True

    if p == "/api/skills/install-remote":
        url = str(data.get("url") or data.get("source_url") or "").strip()
        sk_name = str(data.get("name") or data.get("skill_name") or "").strip() or None
        target = str(data.get("target") or "project").strip()
        target_path_str = data.get("target_path") or data.get("project_path")
        target_dir = self._resolve_target_project_dir(data.get("project_hash"))
        if target_path_str:
            target_dir = Path(target_path_str).resolve()
        if not url:
            self._send_json(req_handler, {"error": "Missing remote URL"}, status=400)
            return True
        res = install_remote_skill(url, skill_name=sk_name, target=target, target_workspace=target_dir)
        self._send_json(req_handler, res)
        return True

    if p == "/api/skills/uninstall":
        sk_name = str(data.get("name") or data.get("skill") or data.get("skill_name") or "").strip()
        target = str(data.get("target") or "project").strip()
        target_path_str = data.get("target_path") or data.get("project_path")
        target_dir = self._resolve_target_project_dir(data.get("project_hash"))
        if target_path_str:
            target_dir = Path(target_path_str).resolve()
        if not sk_name:
            self._send_json(req_handler, {"error": "Missing skill name"}, status=400)
            return True
        res = uninstall_skill(sk_name, target=target, target_workspace=target_dir)
        self._send_json(req_handler, res)
        return True

    if p == "/api/capabilities/auto-match":
        target_dir = self._resolve_target_project_dir(data.get("project_hash"))
        apply_flag = bool(data.get("apply", False))
        res = auto_match_project_capabilities(target_dir, apply=apply_flag)
        self._send_json(req_handler, res)
        return True

    if p == "/projects":
        target_path_str = data.get("working_dir") or data.get("path")
        if not target_path_str:
            self._send_json(req_handler, {"error": "Missing working_dir or path"}, status=400)
            return True
        target_path = Path(target_path_str).resolve()
        proj_name = data.get("name") or target_path.name
        if not self._is_valid_project_dir(target_path, is_registered=True, name=proj_name):
            self._send_json(
                req_handler,
                {"error": f"Invalid project directory: {target_path_str} (home or non-existent directories not allowed)"},
                status=400,
            )
            return True

        dot_bugx = target_path / ".bugx"
        dot_bugx.mkdir(parents=True, exist_ok=True)

        h = self._compute_hash(str(target_path))

        registered = _load_registered_projects()
        now_ms = int(time.time() * 1000)
        existing = [r for r in registered if Path(r.get("working_dir", "")).resolve() == target_path]
        if existing:
            existing[0]["last_seen_at"] = now_ms
            existing[0]["name"] = proj_name
            existing[0]["archived"] = False
        else:
            registered.append({
                "hash": h,
                "name": proj_name,
                "working_dir": str(target_path),
                "added_at": now_ms,
                "last_seen_at": now_ms,
                "archived": False,
            })
        _save_registered_projects(registered, unhide_hashes={h})

        session_count = self._count_project_sessions(target_path)
        self._send_json(req_handler, {
            "ok": True,
            "project": {
                "hash": h,
                "name": proj_name,
                "working_dir": str(target_path),
                "session_count": session_count,
                "archived": False,
            },
        })
        return True

    if p == "/projects/delete" or (p.startswith("/projects/") and p.endswith("/delete")):
        proj_hash = None
        if p.startswith("/projects/") and p != "/projects/delete":
            clean_sub = p.removeprefix("/projects/")
            if clean_sub.endswith("/delete"):
                proj_hash = clean_sub.removesuffix("/delete").strip("/")
        if not proj_hash:
            proj_hash = data.get("hash") or data.get("project_hash")
        if not proj_hash and data.get("working_dir"):
            proj_hash = self._compute_hash(data["working_dir"])
        if not proj_hash:
            self._send_json(req_handler, {"error": "Missing project hash or working_dir"}, status=400)
            return True
        mode = (data.get("mode") or "remove").lower()
        if mode not in ("remove", "purge"):
            self._send_json(req_handler, {"error": f"Invalid delete mode: {mode}"}, status=400)
            return True
        target_dir = self._resolve_target_project_dir(proj_hash)
        self.dispatcher.stop_project_sessions(proj_hash, cwd=target_dir)
        try:
            import core.bugx_paths as bugx_paths
            res = bugx_paths.remove_global_project(
                proj_hash, mode=mode, workspace=self.workspace
            )
        except Exception as exc:
            self._send_json(req_handler, {"error": str(exc)}, status=500)
            return True
        self._send_json(req_handler, res)
        return True

    if p == "/live/provider":
        sid = data.get("session_id", "session-default-01")
        raw_prov = str(data.get("provider") or "codex")
        raw_mod = str(data.get("model") or "")
        eff_prov, eff_mod = self.dispatcher._split_provider_and_model(raw_prov, raw_mod)
        if eff_mod:
            self._active_host_models[eff_prov] = eff_mod
        prefs = self.dispatcher.set_session_preference(
            sid,
            provider=eff_prov,
            model=eff_mod,
            mode=data.get("mode"),
        )
        try:
            from .adapters import get_adapter_registry
        except ImportError:
            from core.webui.adapters import get_adapter_registry
        adapter = get_adapter_registry().get_adapter(eff_prov)
        eff_app, eff_levels, def_effort = adapter.get_effort_capabilities(eff_mod)
        self._send_json(req_handler, {
            "ok": True,
            "provider": eff_prov,
            "model": eff_mod,
            "effort_applicable": eff_app,
            "effort_levels": eff_levels,
            "reasoning_effort": def_effort,
            "preferences": prefs,
        })
        return True

    if p == "/live/reasoning_effort":
        sid = data.get("session_id", "session-default-01")
        effort = str(data.get("reasoning_effort") or data.get("effort") or "medium").lower()
        if effort not in ("none", "off", "minimal", "low", "medium", "high", "xhigh", "max"):
            self._send_json(req_handler, {"error": f"Invalid reasoning_effort: {effort}"}, status=400)
            return True
        prefs = self.dispatcher.set_session_preference(sid, reasoning_effort=effort)
        self._send_json(req_handler, {"ok": True, "reasoning_effort": effort, "preferences": prefs})
        return True

    if p in ("/live/permission", "/chat/permission"):
        sid = data.get("session_id", "session-default-01")
        approved = bool(data.get("approved", data.get("allow", True)))
        resolved = self.dispatcher.resolve_permission(sid, approved=approved)
        self._send_json(req_handler, {"ok": True, "resolved": resolved, "approved": approved})
        return True

    if p == "/cd":
        raw_path = str(data.get("path") or "").strip()
        if not raw_path:
            self._send_json(req_handler, {"success": False, "message": "工作目录路径不能为空"}, status=400)
            return True

        if raw_path == "~":
            target_dir = Path.home()
        elif raw_path.startswith("~/"):
            target_dir = Path.home() / raw_path[2:]
        else:
            p_cand = Path(raw_path)
            target_dir = p_cand if p_cand.is_absolute() else (self.workspace / p_cand)

        try:
            target_dir = target_dir.resolve()
        except Exception as exc:
            self._send_json(req_handler, {"success": False, "message": f"无效的路径: {exc}"}, status=400)
            return True

        if not target_dir.exists():
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self._send_json(req_handler, {"success": False, "message": f"目录不存在且创建失败: {exc}"}, status=400)
                return True
        elif not target_dir.is_dir():
            target_dir = target_dir.parent

        # Update active workspace
        self.workspace = target_dir

        # Ensure .bugx initialized
        (target_dir / ".bugx").mkdir(parents=True, exist_ok=True)

        # Auto-register to global projects list
        proj_hash = ""
        try:
            import core.bugx_paths as bugx_paths
            bugx_paths.record_global_project(target_dir, name=target_dir.name)
            proj_hash = bugx_paths.get_project_hash(target_dir)
            self._active_project_hash = proj_hash
        except Exception:
            pass

        # Find or create a session in target_dir
        sessions = self.dispatcher.list_sessions(target_dir)
        if sessions:
            sid = sessions[0]["id"]
        else:
            sid = f"sess-{int(time.time() * 1000)}"
            self.dispatcher._upsert_session_meta(
                target_dir,
                sid,
                name=f"安全审计 ({target_dir.name})",
                mode="chat",
                provider="codex",
                status="idle",
            )

        self._send_json(req_handler, {
            "success": True,
            "ok": True,
            "current_dir": str(target_dir),
            "target": str(target_dir),
            "session_id": sid,
            "project_hash": proj_hash or self._active_project_hash,
        })
        return True

    if p == "/fs/list":
        sub_path = str(data.get("path") or ".").strip()
        entries = self._list_fs(sub_path)
        self._send_json(req_handler, entries)
        return True

    if p == "/fs/mkdir":
        raw_path = str(data.get("path") or "").strip()
        if not raw_path:
            self._send_json(req_handler, {"error": "路径不能为空"}, status=400)
            return True
        if raw_path == "~":
            target_dir = Path.home()
        elif raw_path.startswith("~/"):
            target_dir = Path.home() / raw_path[2:]
        else:
            p_cand = Path(raw_path)
            target_dir = p_cand if p_cand.is_absolute() else (self.workspace / p_cand)
        try:
            target_dir = target_dir.resolve()
            target_dir.mkdir(parents=True, exist_ok=True)
            self._send_json(req_handler, {"ok": True, "path": str(target_dir)})
        except Exception as exc:
            self._send_json(req_handler, {"error": f"创建文件夹失败: {exc}"}, status=400)
        return True

    if p in ("/fs/delete", "/api/docs/delete"):
        raw_path = str(data.get("path") or "").strip()
        if not raw_path:
            self._send_json(req_handler, {"error": "路径不能为空"}, status=400)
            return True
        if raw_path == "~":
            target = Path.home()
        elif raw_path.startswith("~/"):
            target = Path.home() / raw_path[2:]
        else:
            p_cand = Path(raw_path)
            target = p_cand if p_cand.is_absolute() else (self.workspace / p_cand)
        try:
            target = target.resolve()
        except Exception:
            self._send_json(req_handler, {"error": "无法解析目标路径"}, status=400)
            return True

        home_dir = Path.home().resolve()
        ws_dir = self.workspace.resolve()
        root_dir = Path("/").resolve()

        if target in (root_dir, home_dir, ws_dir):
            self._send_json(req_handler, {"error": "系统根目录、主目录或工作区根目录受保护，禁止直接删除"}, status=403)
            return True

        for sys_name in ("/System", "/Library", "/usr", "/bin", "/sbin"):
            try:
                sys_dir = Path(sys_name).resolve()
                if target == sys_dir or sys_dir in target.parents:
                    self._send_json(req_handler, {"error": "系统核心受保护路径禁止删除"}, status=403)
                    return True
            except Exception:
                pass

        if not target.exists():
            self._send_json(req_handler, {"error": f"指定文件或目录不存在: {target.name}"}, status=404)
            return True

        try:
            is_dir = target.is_dir()
            if is_dir:
                shutil.rmtree(target)
            else:
                target.unlink()
            self._send_json(req_handler, {
                "ok": True,
                "success": True,
                "deleted": str(target),
                "name": target.name,
                "is_dir": is_dir,
                "parent": str(target.parent),
            })
        except Exception as exc:
            self._send_json(req_handler, {"error": f"删除失败: {exc}"}, status=400)
        return True

    if p in (
        "/live/mcp/trust",
        "/live/policy-intervention",
        "/live/compact",
        "/live/user-input",
        "/chat/user-input",
        "/config/reload",
    ):
        self._send_json(req_handler, {"ok": True})
        return True

    return False

