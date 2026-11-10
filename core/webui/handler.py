"""BugX Studio WebUI Request Handler.

Handles serving the AtomCode/DSH-derived WebUI SPA and all its API endpoints:
- Projects & Workspaces (/projects, /project)
- MCP Server Inspection (/mcp/status)
- Skills & Commands (/skills)
- Models & CLI Hosts (/models)
- Real-time Task Streaming & Execution (/live, /live/message, /live/stop)
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .dispatcher import LiveSessionDispatcher, sanitize_chat_text
from .mcp_inspector import (
    auto_match_project_capabilities,
    get_mcp_detail,
    get_mcp_marketplace,
    get_online_mcp_catalog,
    get_online_skills_catalog,
    get_skills_marketplace,
    inspect_workspace_mcp,
    install_remote_skill,
    install_skill,
    read_skill_markdown,
    save_sandbox_mcp_env,
    toggle_project_mcp,
    toggle_project_skill,
    uninstall_skill,
)

import sys

_core_dir = str(Path(__file__).resolve().parent.parent)
if _core_dir not in sys.path:
    sys.path.insert(0, _core_dir)

try:
    from core.cloud_sync.cloud_client import (
        DEVICE_TOKEN_PATH,
        CloudClient,
        CloudCredentials,
        configured_base_url,
        get_device_id,
        get_device_label,
    )
except ImportError:
    try:
        from core.cloud_sync.cloud_client import (
            DEVICE_TOKEN_PATH,
            CloudClient,
            CloudCredentials,
            configured_base_url,
            get_device_id,
            get_device_label,
        )
    except ImportError:
        DEVICE_TOKEN_PATH = "/api/v1/agent/auth/device-token"
        CloudClient = None
        CloudCredentials = None
        get_device_id = lambda: "unknown-device"
        get_device_label = lambda: "BugX Device"
        configured_base_url = lambda: "https://bugx.dev"

STATIC_DIR = Path(__file__).resolve().parent / "static"
DISPATCHER = LiveSessionDispatcher()
_SHARED_PENDING_LOGINS: dict[str, dict[str, Any]] = {}
_SHARED_APPROVAL_MODE: str = "build"



try:
    import core.webui.workspace_service as _ws
    import core.webui.ide_cloud_service as _ics
    import core.webui.docs_session_service as _dss
    import core.webui.api_routes_post as _arp
except ImportError:
    import core.webui.workspace_service as _ws
    import core.webui.ide_cloud_service as _ics
    import core.webui.docs_session_service as _dss
    import core.webui.api_routes_post as _arp

_get_projects_file = _ws._get_projects_file
_load_registered_projects = _ws._load_registered_projects
_save_registered_projects = _ws._save_registered_projects

class WebUIHandler:
    """Dispatches HTTP requests for BugX Studio WebUI."""

    def __init__(self, current_workspace: Optional[Path] = None):
        self.workspace = (current_workspace or Path.cwd()).resolve()
        self.dispatcher = DISPATCHER
        self._active_project_hash = self._compute_hash(str(self.workspace))
        self._pending_logins = _SHARED_PENDING_LOGINS
        self._approval_mode = _SHARED_APPROVAL_MODE
        self._active_host_models: dict[str, str] = {}

    _compute_hash = staticmethod(_ws._compute_hash)
    _is_valid_project_dir = _ws._is_valid_project_dir
    _discover_candidate_workspaces = _ws._discover_candidate_workspaces
    _count_project_sessions = _ws._count_project_sessions
    _resolve_target_project_dir = _ws._resolve_target_project_dir
    get_known_workspaces = _ws.get_known_workspaces

    _detect_installed_ides = _ics._detect_installed_ides
    _open_in_ide = _ics._open_in_ide
    _get_auth_status = _ics._get_auth_status
    _get_cloud_status = _ics._get_cloud_status
    _start_cloud_login = _ics._start_cloud_login
    _poll_cloud_login = _ics._poll_cloud_login
    _cancel_cloud_login = _ics._cancel_cloud_login
    _logout_cloud = _ics._logout_cloud
    _bind_cloud_key = _ics._bind_cloud_key
    _trigger_cloud_sync = _ics._trigger_cloud_sync

    _get_workspace_sessions = _dss._get_workspace_sessions
    _get_session_detail = _dss._get_session_detail
    _discover_host_real_models = _dss._discover_host_real_models
    _get_models_list = _dss._get_models_list
    _get_docs_tree = _dss._get_docs_tree
    _get_doc_content_readonly = _dss._get_doc_content_readonly
    _search_docs_fts5 = _dss._search_docs_fts5
    _list_fs = _dss._list_fs
    _search_fs = _dss._search_fs

    handle_api_post = _arp.handle_api_post

    def handle_static(self, path: str, req_handler: Any) -> bool:
        """Serve static files if matched. Returns True if handled."""
        clean_path = path.split("?")[0].lstrip("/")
        if not clean_path:
            clean_path = "index.html"

        target_file = (STATIC_DIR / clean_path).resolve()
        # Security check: must reside inside STATIC_DIR
        try:
            target_file.relative_to(STATIC_DIR)
        except ValueError:
            return False

        if not target_file.is_file():
            # If requesting subpath without extension, fallback to index.html (SPA routing)
            if "." not in clean_path:
                target_file = STATIC_DIR / "index.html"
            else:
                return False

        content_type = "text/html; charset=utf-8"
        if clean_path.endswith(".js"):
            content_type = "text/javascript; charset=utf-8"
        elif clean_path.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif clean_path.endswith(".json") or clean_path.endswith(".webmanifest"):
            content_type = "application/json; charset=utf-8"
        elif clean_path.endswith(".png"):
            content_type = "image/png"
        elif clean_path.endswith(".svg"):
            content_type = "image/svg+xml"
        elif clean_path.endswith(".woff2"):
            content_type = "font/woff2"
        elif clean_path.endswith(".woff"):
            content_type = "font/woff"

        try:
            data = target_file.read_bytes()
            req_handler.send_response(200)
            req_handler.send_header("Content-Type", content_type)
            req_handler.send_header("Content-Length", str(len(data)))
            req_handler.send_header("Cache-Control", "no-cache")
            req_handler.end_headers()
            req_handler.wfile.write(data)
            return True
        except Exception:
            return False

    def handle_api_get(self, path: str, req_handler: Any) -> bool:
        """Handle GET API endpoints."""
        parsed = urlparse(path)
        p = parsed.path
        qs = parse_qs(parsed.query)

        if p == "/projects":
            workspaces = self.get_known_workspaces()
            archived_filter = qs.get("archived", [None])[0]
            inc_archived = str(qs.get("include_archived", [""])[0]).lower() in ("1", "true", "yes")
            if archived_filter == "only":
                workspaces = [w for w in workspaces if w.get("archived")]
            elif inc_archived or archived_filter in ("all", "true"):
                pass
            else:
                workspaces = [w for w in workspaces if not w.get("archived")]
            self._send_json(req_handler, workspaces)
            return True

        if p == "/project":
            info = {
                "project_hash": self._active_project_hash,
                "name": self.workspace.name,
                "working_dir": str(self.workspace),
            }
            self._send_json(req_handler, info)
            return True

        if p == "/project/settings":
            try:
                from core.runtime import project_settings
            except ImportError:
                from core.runtime import project_settings as project_settings
            settings_data = project_settings.get_project_settings(self.workspace)
            self._send_json(req_handler, settings_data)
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
            settings_data = project_settings.get_project_settings(target_dir)
            settings_data["is_active_workspace"] = (target_dir.resolve() == self.workspace.resolve())
            self._send_json(req_handler, settings_data)
            return True

        if p == "/mcp/status":
            proj_hash = qs.get("project_hash", [None])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            mcp_data = inspect_workspace_mcp(target_dir)
            self._send_json(req_handler, mcp_data)
            return True

        if p == "/api/mcp/marketplace":
            proj_hash = qs.get("project_hash", [None])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            self._send_json(req_handler, get_mcp_marketplace(target_dir))
            return True

        if p == "/api/mcp/online":
            proj_hash = qs.get("project_hash", [None])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            self._send_json(req_handler, get_online_mcp_catalog(target_dir))
            return True

        if p == "/api/mcp/content":
            proj_hash = qs.get("project_hash", [None])[0]
            server_name = qs.get("name", [""])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            res = get_mcp_detail(target_dir, server_name)
            self._send_json(req_handler, res, status=200 if res.get("ok") else 404)
            return True

        if p == "/api/skills/marketplace":
            proj_hash = qs.get("project_hash", [None])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            self._send_json(req_handler, get_skills_marketplace(target_dir))
            return True

        if p == "/api/skills/online":
            proj_hash = qs.get("project_hash", [None])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            self._send_json(req_handler, get_online_skills_catalog(target_dir))
            return True

        if p == "/api/skills/content":
            proj_hash = qs.get("project_hash", [None])[0]
            skill_name = qs.get("name", [""])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            res = read_skill_markdown(target_dir, skill_name)
            self._send_json(req_handler, res, status=200 if res.get("ok") else 404)
            return True

        if p == "/api/ide/list":
            self._send_json(req_handler, self._detect_installed_ides())
            return True

        if p == "/api/docs/tree":
            proj_hash = qs.get("project_hash", [None])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            self._send_json(req_handler, self._get_docs_tree(target_dir))
            return True

        if p == "/api/docs/content":
            proj_hash = qs.get("project_hash", [None])[0]
            rel_path = qs.get("path", [""])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            res, status_code = self._get_doc_content_readonly(target_dir, rel_path)
            self._send_json(req_handler, res, status=status_code)
            return True

        if p == "/api/docs/search":
            proj_hash = qs.get("project_hash", [None])[0]
            query = qs.get("q", [""])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            self._send_json(req_handler, self._search_docs_fts5(target_dir, query))
            return True

        if p == "/skills":
            proj_hash = qs.get("project_hash", [None])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            mkt = get_skills_marketplace(target_dir)
            skills = [
                {"name": it["name"], "description": it["description"], "enabled": it.get("enabled", True)}
                for it in mkt.get("items", [])
                if it.get("mounted") and it.get("enabled")
            ]
            self._send_json(req_handler, skills)
            return True

        if p in ("/models", "/providers/discover-models"):
            prov_filter = qs.get("provider", [None])[0]
            models = self._get_models_list(provider=prov_filter)
            self._send_json(req_handler, models)
            return True

        if p == "/config":
            cfg = {
                "notifications": {"enabled": True},
                "ui": {"brand_name": "BugX Studio"},
            }
            self._send_json(req_handler, cfg)
            return True

        if p == "/auth/status":
            self._send_json(req_handler, self._get_auth_status())
            return True

        if p == "/cloud/status":
            self._send_json(req_handler, self._get_cloud_status())
            return True

        if p == "/tunnel/status":
            self._send_json(req_handler, {"running": False})
            return True

        if p == "/approval_mode":
            mode = getattr(self, "_approval_mode", _SHARED_APPROVAL_MODE)
            self._send_json(req_handler, {"ok": True, "mode": mode})
            return True

        if p == "/chat/active":
            proj_hash = qs.get("project_hash", [None])[0]
            target_dir = self._resolve_target_project_dir(proj_hash)
            active = self.dispatcher.get_active_sessions(workspace=target_dir)
            self._send_json(req_handler, active)
            return True

        if p.startswith("/fs/search"):
            base_path = qs.get("path", ["."])[0]
            q_str = (qs.get("q", [""])[0] or "").lower().strip()
            matches = self._search_fs(base_path, q_str)
            self._send_json(req_handler, {"matches": matches})
            return True

        if p.startswith("/sessions/search"):
            q_str = (qs.get("q", [""])[0] or "").lower().strip()
            sessions = self._get_workspace_sessions()
            if q_str:
                sessions = [s for s in sessions if q_str in str(s.get("name", "")).lower()]
            self._send_json(req_handler, sessions)
            return True

        if p == "/sessions":
            proj_hash = qs.get("project_hash", [None])[0]
            sessions = self._get_workspace_sessions(proj_hash)
            self._send_json(req_handler, sessions)
            return True

        if p.startswith("/projects/") and p.endswith("/sessions"):
            parts = p.strip("/").split("/")
            proj_hash = parts[1]
            sessions = self._get_workspace_sessions(proj_hash)
            self._send_json(req_handler, sessions)
            return True

        if p.startswith("/projects/") and "/sessions/" in p:
            parts = p.strip("/").split("/")
            proj_hash = parts[1]
            sid = parts[3]
            since_seq = int(qs.get("since_seq", ["0"])[0] or 0)
            detail = self._get_session_detail(sid, proj_hash=proj_hash, since_seq=since_seq)
            self._send_json(req_handler, detail)
            return True

        if p.startswith("/sessions/resolve/"):
            sid = p.split("/sessions/resolve/")[1]
            since_seq = int(qs.get("since_seq", ["0"])[0] or 0)
            detail = self._get_session_detail(sid, since_seq=since_seq)
            self._send_json(req_handler, detail)
            return True

        if p == "/live":
            self._handle_sse_stream(req_handler, qs)
            return True

        if p == "/fs/list":
            sub_path = qs.get("path", ["."])[0]
            entries = self._list_fs(sub_path)
            self._send_json(req_handler, entries)
            return True

        return False

    def handle_api_delete(self, path: str, req_handler: Any) -> bool:
        """Handle DELETE API endpoints."""
        parsed = urlparse(path)
        p = parsed.path
        qs = parse_qs(parsed.query)

        if p in ("/fs/file", "/fs/delete", "/api/docs/content"):
            raw_path = str(qs.get("path", [""])[0] or "").strip()
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

        if p.startswith("/auth/login/"):
            login_id = p.split("/auth/login/")[1].split("?")[0]
            self._cancel_cloud_login(login_id)
            self._send_json(req_handler, {"ok": True})
            return True

        if p.startswith("/sessions/"):
            sid = p.split("/sessions/")[1].split("/")[0]
            res = self.dispatcher.delete_session(self.workspace, sid)
            self._send_json(req_handler, res)
            return True

        if p.startswith("/projects/"):
            parts = p.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "projects":
                proj_hash = parts[1]
                if len(parts) == 2:
                    # DELETE /projects/:hash?mode=remove|purge (REQ-06)
                    mode = (qs.get("mode", ["remove"])[0] or "remove").lower()
                    if mode not in ("remove", "purge"):
                        self._send_json(req_handler, {"error": f"Invalid delete mode: {mode}"}, status=400)
                        return True
                    target_dir = self._resolve_target_project_dir(proj_hash)
                    # Stop any active tasks on this project before deleting/purging
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
                elif len(parts) >= 4 and parts[2] == "sessions":
                    # DELETE /projects/:hash/sessions/:sid (REQ-02)
                    sid = parts[3]
                    target_dir = self._resolve_target_project_dir(proj_hash)
                    res = self.dispatcher.delete_session(target_dir, sid)
                    self._send_json(req_handler, res)
                    return True
            self._send_json(req_handler, {"ok": True, "deleted": True})
            return True

        if p.startswith("/provider-accounts/") or p.startswith("/model-profiles/"):
            self._send_json(req_handler, {"ok": True, "deleted": True})
            return True

        return False

    def handle_api_patch(self, path: str, req_handler: Any, body_bytes: bytes) -> bool:
        """Handle PATCH API endpoints."""
        parsed = urlparse(path)
        p = parsed.path

        data: dict[str, Any] = {}
        if body_bytes:
            try:
                data = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                pass

        if ((p.startswith("/projects/") and p.endswith("/rename") and "/sessions/" not in p) or p == "/projects/rename"):
            parts = p.strip("/").split("/")
            proj_hash = parts[1] if (len(parts) >= 3 and parts[0] == "projects") else data.get("project_hash")
            working_dir = data.get("working_dir")
            new_name = str(data.get("name") or data.get("title") or "").strip()
            if not new_name:
                self._send_json(req_handler, {"error": "Missing new project name"}, status=400)
                return True
            registered = _load_registered_projects()
            updated = False
            for it in registered:
                if (proj_hash and it.get("hash") == proj_hash) or (working_dir and it.get("working_dir") == working_dir):
                    it["name"] = new_name
                    updated = True
                    try:
                        p_dir = Path(it.get("working_dir", "")).resolve()
                        cfg_file = p_dir / ".bugx" / "config.json"
                        if cfg_file.is_file():
                            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                            cfg["project_name"] = new_name
                            cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
            if updated:
                _save_registered_projects(registered)
            self._send_json(req_handler, {"ok": True, "renamed": True, "name": new_name, "hash": proj_hash})
            return True

        if "/rename" in p or (p.startswith("/projects/") and "/sessions/" in p):
            parts = p.strip("/").split("/")
            new_name = str(data.get("name") or data.get("title") or "").strip()
            if len(parts) >= 4 and parts[0] == "projects" and parts[2] == "sessions":
                proj_hash = parts[1]
                sid = parts[3]
                target_dir = self._resolve_target_project_dir(proj_hash)
                res = self.dispatcher.rename_session(target_dir, sid, new_name)
                if isinstance(res, dict) and "title" not in res and res.get("name"):
                    res["title"] = res["name"]
                self._send_json(req_handler, res)
                return True
            if len(parts) >= 3 and parts[0] == "sessions":
                sid = parts[1]
                res = self.dispatcher.rename_session(self.workspace, sid, new_name)
                if isinstance(res, dict) and "title" not in res and res.get("name"):
                    res["title"] = res["name"]
                self._send_json(req_handler, res)
                return True
            self._send_json(req_handler, {"ok": True, "renamed": True, "name": new_name, "title": new_name})
            return True

        if p.startswith("/provider-accounts/") or p.startswith("/model-profiles/"):
            self._send_json(req_handler, {"ok": True, "updated": True})
            return True

        return False

    def _handle_sse_stream(self, req_handler: Any, qs: dict[str, list[str]]):
        """Handle Server-Sent Events (SSE) streaming on /live with reconnect state hydration (REQ-08)."""
        sid = qs.get("session_id", ["session-default-01"])[0]
        since_seq = int(qs.get("since_seq", ["0"])[0] or 0)
        proj_hash = qs.get("project_hash", [None])[0]
        target_dir = self._resolve_target_project_dir(proj_hash)
        q = self.dispatcher.register_client(sid)

        req_handler.send_response(200)
        req_handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        req_handler.send_header("Cache-Control", "no-cache")
        req_handler.send_header("Connection", "keep-alive")
        req_handler.send_header("Access-Control-Allow-Origin", "*")
        req_handler.end_headers()

        detail = self.dispatcher.get_session_detail(target_dir, sid, since_seq=since_seq)
        initial_event = {
            "type": "snapshot",
            "session_id": sid,
            "status": detail.get("status", "idle"),
            "messages": detail.get("messages", []),
            "events": detail.get("events", []),
            "last_seq": detail.get("last_seq", since_seq),
            "provider": detail.get("provider", "codex"),
            "mode": detail.get("mode", "chat"),
            "reasoning_effort": detail.get("reasoning_effort", "medium"),
            "working_dir": str(target_dir),
            "stats": detail.get("stats"),
        }
        self._write_sse(req_handler, initial_event)

        if detail.get("stats") and sid not in self.dispatcher._active_processes:
            self._write_sse(
                req_handler,
                {
                    "type": "state",
                    "running": False,
                    "stop_reason": "stopped",
                    "stats": detail["stats"],
                },
            )

        try:
            while True:
                try:
                    event = q.get(timeout=10.0)
                    self._write_sse(req_handler, event)
                except queue.Empty:
                    req_handler.wfile.write(b": ping\n\n")
                    req_handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.dispatcher.unregister_client(sid, q)

    @staticmethod
    def _write_sse(req_handler: Any, event: dict[str, Any]):
        line = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
        req_handler.wfile.write(line)
        req_handler.wfile.flush()

    @staticmethod
    def _send_json(req_handler: Any, payload: Any, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            req_handler.send_response(status)
            req_handler.send_header("Content-Type", "application/json; charset=utf-8")
            req_handler.send_header("Content-Length", str(len(body)))
            req_handler.send_header("Access-Control-Allow-Origin", "*")
            req_handler.end_headers()
            req_handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

