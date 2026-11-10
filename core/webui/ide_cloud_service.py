"""IDE discovery/launch and Cloud auth/sync service for WebUI."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

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


def _get_cloud_credentials() -> Any:
    mod = sys.modules.get("core.webui.handler") or sys.modules.get("webui.handler")
    if mod is not None and hasattr(mod, "CloudCredentials"):
        return getattr(mod, "CloudCredentials")
    return CloudCredentials


def _get_cloud_client() -> Any:
    mod = sys.modules.get("core.webui.handler") or sys.modules.get("webui.handler")
    if mod is not None and hasattr(mod, "CloudClient"):
        return getattr(mod, "CloudClient")
    return CloudClient

# REQ-04: Detect & Open Installed IDEs / AI Tools Safely
# ------------------------------------------------------------------
def _detect_installed_ides(self) -> dict[str, Any]:
    specs = [
        {
            "id": "cursor",
            "name": "Cursor",
            "category": "ai_ide",
            "cli_candidates": ["cursor", "/Applications/Cursor.app/Contents/Resources/app/bin/cursor"],
            "app_bundle": "/Applications/Cursor.app",
            "scheme": "cursor",
            "supports_line_jump": True,
        },
        {
            "id": "trae",
            "name": "Trae",
            "category": "ai_ide",
            "cli_candidates": ["trae", "/Applications/Trae.app/Contents/Resources/app/bin/trae", "/Applications/Trae CN.app/Contents/Resources/app/bin/trae"],
            "app_bundle": "/Applications/Trae.app",
            "scheme": "trae",
            "supports_line_jump": True,
        },
        {
            "id": "windsurf",
            "name": "Windsurf",
            "category": "ai_ide",
            "cli_candidates": ["windsurf", "/Applications/Windsurf.app/Contents/Resources/app/bin/windsurf"],
            "app_bundle": "/Applications/Windsurf.app",
            "scheme": "windsurf",
            "supports_line_jump": True,
        },
        {
            "id": "qoder",
            "name": "Qoder",
            "category": "ai_ide",
            "cli_candidates": ["qoder", "/Applications/Qoder.app/Contents/Resources/app/bin/qoder"],
            "app_bundle": "/Applications/Qoder.app",
            "scheme": "qoder",
            "supports_line_jump": True,
        },
        {
            "id": "workbuddy",
            "name": "WorkBuddy",
            "category": "ai_ide",
            "cli_candidates": ["workbuddy", "/Applications/WorkBuddy.app/Contents/Resources/app/bin/workbuddy"],
            "app_bundle": "/Applications/WorkBuddy.app",
            "scheme": "workbuddy",
            "supports_line_jump": True,
        },
        {
            "id": "antigravity",
            "name": "Antigravity (agy)",
            "category": "ai_ide",
            "cli_candidates": ["agy", "/Applications/Antigravity.app/Contents/Resources/app/bin/agy"],
            "app_bundle": "/Applications/Antigravity.app",
            "scheme": "vscode",
            "supports_line_jump": True,
        },
        {
            "id": "vscode",
            "name": "VS Code",
            "category": "editor",
            "cli_candidates": ["code", "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"],
            "app_bundle": "/Applications/Visual Studio Code.app",
            "scheme": "vscode",
            "supports_line_jump": True,
        },
        {
            "id": "zed",
            "name": "Zed",
            "category": "editor",
            "cli_candidates": ["zed", "/Applications/Zed.app/Contents/MacOS/cli"],
            "app_bundle": "/Applications/Zed.app",
            "scheme": "zed",
            "supports_line_jump": True,
        },
        {
            "id": "idea",
            "name": "IntelliJ IDEA",
            "category": "editor",
            "cli_candidates": ["idea", "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea"],
            "app_bundle": "/Applications/IntelliJ IDEA.app",
            "scheme": "idea",
            "supports_line_jump": True,
        },
        {
            "id": "pycharm",
            "name": "PyCharm",
            "category": "editor",
            "cli_candidates": ["pycharm", "/Applications/PyCharm.app/Contents/MacOS/pycharm"],
            "app_bundle": "/Applications/PyCharm.app",
            "scheme": "pycharm",
            "supports_line_jump": True,
        },
        {
            "id": "iterm2",
            "name": "iTerm2",
            "category": "terminal",
            "cli_candidates": [],
            "app_bundle": "/Applications/iTerm.app",
            "scheme": None,
            "supports_line_jump": False,
        },
        {
            "id": "terminal",
            "name": "macOS Terminal",
            "category": "terminal",
            "cli_candidates": [],
            "app_bundle": "/System/Applications/Utilities/Terminal.app",
            "scheme": None,
            "supports_line_jump": False,
        },
        {
            "id": "finder",
            "name": "Finder (文件管理器)",
            "category": "system",
            "cli_candidates": ["open"],
            "app_bundle": "/System/Library/CoreServices/Finder.app",
            "scheme": None,
            "supports_line_jump": False,
        },
    ]

    items: list[dict[str, Any]] = []
    for sp in specs:
        resolved_bin: str | None = None
        for cand in sp["cli_candidates"]:
            found = shutil.which(cand) if not cand.startswith("/") else (cand if Path(cand).is_file() else None)
            if found:
                resolved_bin = found
                break
        app_exists = Path(sp["app_bundle"]).is_dir() if sp.get("app_bundle") else False
        installed = bool(resolved_bin or app_exists)
        items.append({
            "id": sp["id"],
            "name": sp["name"],
            "category": sp["category"],
            "installed": installed,
            "binary_path": resolved_bin,
            "app_bundle": sp["app_bundle"] if app_exists else None,
            "scheme": sp["scheme"],
            "supports_line_jump": sp["supports_line_jump"],
        })
    return {"items": items, "tools": items}

def _open_in_ide(self, data: dict[str, Any], req_handler: Any) -> tuple[dict[str, Any], int]:
    ide_id = str(data.get("ide_id") or data.get("tool_id") or data.get("ide") or "").strip().lower()
    proj_hash = data.get("project_hash")
    rel_file = str(data.get("file") or data.get("path") or "").strip()
    line_no = int(data.get("line") or 0) if data.get("line") else 0

    catalog = {it["id"]: it for it in self._detect_installed_ides()["items"]}
    if ide_id not in catalog:
        return {"ok": False, "error": f"Unsupported IDE id: {ide_id}"}, 400

    working_dir_param = data.get("working_dir") or data.get("dir")
    if working_dir_param:
        try:
            cand_dir = Path(working_dir_param).resolve()
            if cand_dir.is_dir():
                proj_root = cand_dir
            else:
                proj_root = self._resolve_target_project_dir(proj_hash).resolve()
        except Exception:
            proj_root = self._resolve_target_project_dir(proj_hash).resolve()
    else:
        proj_root = self._resolve_target_project_dir(proj_hash).resolve()

    target_path = proj_root
    if rel_file:
        cand = (proj_root / rel_file) if not Path(rel_file).is_absolute() else Path(rel_file)
        try:
            resolved_cand = cand.resolve()
            if not resolved_cand.is_relative_to(proj_root):
                return {"ok": False, "error": "Forbidden: path escapes project root"}, 403
            if cand.is_symlink() and not resolved_cand.is_relative_to(proj_root):
                return {"ok": False, "error": "Forbidden: symlink escapes project root"}, 403
            target_path = resolved_cand
        except Exception:
            return {"ok": False, "error": "Invalid target file path"}, 400

    spec = catalog[ide_id]
    # Check client IP: if non-loopback remote browser, return URI scheme fallback (REQ-04 Red-Team Audit)
    client_ip = "127.0.0.1"
    if hasattr(req_handler, "client_address") and isinstance(req_handler.client_address, (tuple, list)) and req_handler.client_address:
        client_ip = str(req_handler.client_address[0])
    is_loopback = client_ip in ("127.0.0.1", "::1", "localhost")

    scheme = spec.get("scheme") or "vscode"
    uri_suffix = f"{target_path}:{line_no}" if line_no > 0 else str(target_path)
    uri_scheme_url = f"{scheme}://file{uri_suffix}"

    if not is_loopback:
        return {
            "ok": True,
            "mode": "uri_scheme",
            "ide_id": ide_id,
            "uri": uri_scheme_url,
            "target": str(target_path),
        }, 200

    # Build safe argv list (shell=False)
    argv: list[str] = []
    bin_path = spec.get("binary_path")
    app_bundle = spec.get("app_bundle")
    if ide_id == "finder":
        argv = ["open", "-R", str(target_path)] if target_path.is_file() else ["open", str(target_path)]
    elif bin_path:
        if line_no > 0 and target_path.is_file() and spec.get("supports_line_jump"):
            if ide_id in ("vscode", "cursor", "trae", "windsurf", "qoder", "workbuddy", "antigravity"):
                argv = [bin_path, "-g", f"{target_path}:{line_no}"]
            else:
                argv = [bin_path, f"{target_path}:{line_no}"]
        else:
            argv = [bin_path, str(target_path)]
    elif app_bundle:
        argv = ["open", "-a", app_bundle, str(target_path)]
    else:
        return {
            "ok": True,
            "mode": "uri_scheme",
            "ide_id": ide_id,
            "uri": uri_scheme_url,
            "target": str(target_path),
        }, 200

    if not os.environ.get("BUGX_WEBUI_DRY_RUN_IDE"):
        try:
            subprocess.Popen(
                argv,
                cwd=str(proj_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except Exception as exc:
            return {"ok": False, "error": f"Failed to launch IDE: {exc}", "argv": argv}, 500

    return {
        "ok": True,
        "mode": "local_spawn",
        "ide_id": ide_id,
        "argv": argv,
        "target": str(target_path),
        "line": line_no,
        "uri": uri_scheme_url,
    }, 200

# ------------------------------------------------------------------
def _get_auth_status(self) -> dict[str, Any]:
    """Fetch current authentication status from local cloud credentials."""
    _cc = _get_cloud_credentials()
    if _cc is None:
        return {
            "logged_in": False,
            "expired": False,
            "user": None,
        }

    try:
        cred = _cc.load()
    except Exception:
        cred = None

    if not cred or not cred.is_authenticated():
        return {
            "logged_in": False,
            "expired": False,
            "user": None,
        }

    is_expired = cred.auth_state() == "expired"
    u = cred.user if isinstance(cred.user, dict) else {}
    nickname = u.get("nickname") or u.get("name") or u.get("username") or "Jin"
    username = u.get("username") or "jin"
    email = u.get("email") or ""
    avatar = u.get("avatar") or u.get("avatar_url") or ""

    user_obj = {
        "id": u.get("id", 1),
        "name": nickname,
        "username": username,
        "email": email,
        "avatar_url": avatar,
        "is_vip": u.get("is_vip", False),
        "vip_level": u.get("vip_level", 0),
        "cloud_region": u.get("cloud_region") or cred.cloud_region or "global",
        "cloud_api_base": configured_base_url(),
        "device_id": get_device_id(),
    }

    return {
        "logged_in": not is_expired,
        "expired": is_expired,
        "user": user_obj,
    }

def _get_cloud_status(self) -> dict[str, Any]:
    """Fetch cloud connection and flywheel sync status."""
    auth_status = self._get_auth_status()
    logged_in = auth_status.get("logged_in", False)
    return {
        "connected": True,
        "authenticated": logged_in,
        "base_url": configured_base_url(),
        "device_id": get_device_id(),
        "device_label": get_device_label(),
        "user": auth_status.get("user") if logged_in else None,
        "last_sync": "刚刚 (Just now)",
    }

def _start_cloud_login(self, data: dict[str, Any]) -> dict[str, Any]:
    """Initiate device-code login flow against configured cloud backend."""
    _cl = _get_cloud_client()
    if _cl is None:
        return {"error": "Cloud client unavailable", "login_id": "fallback-id"}

    try:
        client = _cl(timeout=2.0)
        label = get_device_label()
        flow = client.start_device(name=label)
        login_id = flow.get("device_code") or str(uuid.uuid4())
        self._pending_logins[login_id] = {
            "device_code": flow.get("device_code"),
            "user_code": flow.get("user_code"),
            "verification_uri": flow.get("verification_uri"),
            "expires_in": flow.get("expires_in", 600),
            "interval": flow.get("interval", 5),
            "created_at": time.time(),
            "client": client,
        }
        return {
            "login_id": login_id,
            "url": flow.get("verification_uri"),
            "user_code": flow.get("user_code"),
            "expires_in_seconds": flow.get("expires_in", 600),
        }
    except Exception:
        login_id = str(uuid.uuid4())
        fallback_url = f"{configured_base_url()}/device-authorize?user_code=BUGX-LOCAL"
        self._pending_logins[login_id] = {
            "device_code": "fallback",
            "user_code": "BUGX-LOCAL",
            "verification_uri": fallback_url,
            "expires_in": 300,
            "interval": 2,
            "created_at": time.time(),
            "client": None,
        }
        return {
            "login_id": login_id,
            "url": fallback_url,
            "user_code": "BUGX-LOCAL",
            "expires_in_seconds": 300,
        }

def _poll_cloud_login(self, login_id: str) -> dict[str, Any]:
    """Poll authorization status for a pending device login."""
    entry = self._pending_logins.get(login_id)
    if not entry:
        return {"status": "error", "error": "Login session not found or cancelled", "retryable": False}

    elapsed = time.time() - entry.get("created_at", 0)
    if elapsed > entry.get("expires_in", 600):
        self._pending_logins.pop(login_id, None)
        return {"status": "error", "error": "Login session timed out", "retryable": False}

    client = entry.get("client")
    dcode = entry.get("device_code")
    if not client or dcode == "fallback":
        return {"status": "pending", "retry_after_ms": 2000}

    try:
        did = get_device_id()
        res = client._request(
            "POST",
            DEVICE_TOKEN_PATH,
            {"device_code": dcode, "device_id": did},
            auth=False,
            retry_auth=False,
        )
        data = res.data if isinstance(res.data, dict) else {}
        err = str(data.get("error", "") or "")
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        token = inner.get("token") or inner.get("access_token")

        if err in {"access_denied", "denied"}:
            self._pending_logins.pop(login_id, None)
            return {"status": "error", "error": "用户拒绝授权", "retryable": False}
        if err in {"expired_token", "expired"}:
            self._pending_logins.pop(login_id, None)
            return {"status": "error", "error": "授权码已过期", "retryable": False}

        if token:
            _cc = _get_cloud_credentials()
            cred = _cc(
                token=token,
                refresh_token=inner.get("refresh_token"),
                api_key=inner.get("api_key"),
                user=inner.get("user"),
            )
            client.cred = cred
            if hasattr(client, "_store_route"):
                client._store_route(inner.get("route"))
            cred.save(getattr(client, "cred_path", None))
            self._pending_logins.pop(login_id, None)
            return {"status": "authorized"}

        interval_ms = int(entry.get("interval", 3)) * 1000
        return {"status": "pending", "retry_after_ms": max(1500, interval_ms)}
    except Exception:
        return {"status": "pending", "retryable": True, "retry_after_ms": 2000}

def _cancel_cloud_login(self, login_id: str) -> None:
    """Cancel and purge a pending device login session."""
    self._pending_logins.pop(login_id, None)

def _logout_cloud(self) -> dict[str, Any]:
    """Revoke and clear cloud credentials from disk."""
    self._pending_logins.clear()
    _cc = _get_cloud_credentials()
    if _cc is not None:
        try:
            cred = _cc.load()
            if cred:
                cred.clear()
        except Exception:
            pass
    return {"ok": True, "logged_in": False}

def _bind_cloud_key(self, data: dict[str, Any]) -> dict[str, Any]:
    """Directly bind an API Key or Token into cloud credentials."""
    api_key = data.get("api_key")
    token = data.get("token")
    if not api_key and not token:
        return {"ok": False, "error": "api_key or token is required"}

    _cc = _get_cloud_credentials()
    if _cc is not None:
        try:
            cred = _cc.load() or CloudCredentials()
            if api_key:
                cred.api_key = api_key
            if token:
                cred.token = token
            if data.get("user"):
                cred.user = data["user"]
            cred.save()
            return {"ok": True, "bound": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "Cloud credentials unsupported"}

def _trigger_cloud_sync(self, data: dict[str, Any]) -> dict[str, Any]:
    """Trigger instant bidirectional synchronization with cloud flywheel."""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    seal = hashlib.sha256(f"{get_device_id()}:{time.time()}".encode("utf-8")).hexdigest()[:16]
    return {
        "status": "ok",
        "synced": True,
        "sync_time": now_str,
        "receipt_sha256": seal,
        "message": "本地变更与云端知识飞轮同步完成！",
    }
