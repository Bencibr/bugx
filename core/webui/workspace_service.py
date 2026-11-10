"""Workspace discovery and project registry service for WebUI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

def _get_projects_file() -> Path:
    return Path.home() / ".bugx" / "projects.json"


def _load_registered_projects() -> list[dict[str, Any]]:
    pfile = _get_projects_file()
    if not pfile.is_file():
        return []
    try:
        data = json.loads(pfile.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "projects" in data and isinstance(data["projects"], list):
            items = data["projects"]
        elif isinstance(data, list):
            items = data
        else:
            items = []
        valid_items: list[dict[str, Any]] = []
        home_res = Path.home().resolve()
        root_res = Path("/").resolve()
        for item in items:
            if not isinstance(item, dict):
                continue
            wdir = item.get("working_dir")
            if not wdir:
                continue
            p = Path(wdir).resolve()
            if p == home_res or p == root_res:
                continue
            if not p.is_dir():
                continue
            valid_items.append(item)
        return valid_items
    except Exception:
        return []


def _save_registered_projects(
    projects: list[dict[str, Any]],
    *,
    unhide_hashes: set[str] | None = None,
) -> None:
    try:
        pfile = _get_projects_file()
        pfile.parent.mkdir(parents=True, exist_ok=True)
        hidden_hashes: set[str] = set()
        if pfile.is_file():
            try:
                raw = json.loads(pfile.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("hidden_hashes"), list):
                    hidden_hashes = {str(h) for h in raw["hidden_hashes"] if h}
            except Exception:
                pass
        if unhide_hashes:
            hidden_hashes -= unhide_hashes

        home_res = Path.home().resolve()
        root_res = Path("/").resolve()
        clean: list[dict[str, Any]] = []
        for p in projects:
            wdir = p.get("working_dir")
            if wdir and Path(wdir).resolve() not in (home_res, root_res):
                clean.append(p)
        payload = {"version": 1, "projects": clean, "hidden_hashes": sorted(hidden_hashes)}
        tmp_file = pfile.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_file.replace(pfile)
    except Exception:
        pass


def _compute_hash(path_str: str) -> str:
    name = Path(path_str).name
    h = hashlib.sha256(path_str.encode("utf-8")).hexdigest()[:8]
    return f"{name}-{h}"

def _is_valid_project_dir(self, path_obj: Path, is_registered: bool = False, name: str | None = None) -> bool:
    """Check if path_obj is a valid BugX project directory (never home or root, never ephemeral test trash)."""
    try:
        resolved = path_obj.resolve()
        home = Path.home().resolve()
        root = Path("/").resolve()
        if resolved == home or resolved == root:
            return False
        if not resolved.is_dir():
            return False
        eff_name = name or resolved.name
        try:
            import core.bugx_paths as bugx_paths
            if getattr(bugx_paths, "is_ephemeral_test_name", lambda n: False)(eff_name):
                return False
        except Exception:
            pass
        if resolved == self.workspace.resolve():
            return True
        if is_registered:
            return True
        # Any non-home, non-root directory with .bugx is an initialized BugX project
        dot_bugx = resolved / ".bugx"
        if dot_bugx.is_dir():
            return True
        if (resolved / ".bugx-project.json").is_file():
            return True
        return False
    except Exception:
        return False

def _discover_candidate_workspaces(self) -> list[Path]:
    """Discover candidate workspace directories from sibling dirs, recent paths, and cloud records."""
    candidates: list[Path] = []
    home = Path.home().resolve()
    root = Path("/").resolve()

    try:
        import core.bugx_paths as bugx_paths
        is_temp_fn = getattr(bugx_paths, "is_system_temp_dir", lambda p: False)
    except Exception:
        is_temp_fn = lambda p: False

    # 1. Sibling directories under the same workspace container
    try:
        ws_parent = self.workspace.parent.resolve()
        if ws_parent not in (home, root) and ws_parent.is_dir() and not is_temp_fn(ws_parent):
            for sub in sorted(ws_parent.iterdir()):
                if sub.is_dir() and sub.resolve() != self.workspace.resolve():
                    if self._is_valid_project_dir(sub):
                        candidates.append(sub.resolve())
    except Exception:
        pass

    # 2. Check ~/.atomcode/recent_dirs.txt if present (strictly filtering only valid BugX projects)
    recent_file = Path.home() / ".atomcode" / "recent_dirs.txt"
    if recent_file.is_file():
        try:
            for line in recent_file.read_text(encoding="utf-8").splitlines():
                p_str = line.strip()
                if not p_str:
                    continue
                p = Path(p_str).resolve()
                if p.is_dir() and p not in candidates and p != self.workspace.resolve():
                    if self._is_valid_project_dir(p):
                        candidates.append(p)
        except Exception:
            pass

    return candidates

def _count_project_sessions(self, path_obj: Path) -> int:
    count = 0
    try:
        pollings_dir = path_obj / ".bugx" / "pollings"
        if pollings_dir.is_dir():
            count += len([p for p in pollings_dir.iterdir() if p.name.startswith("polling-")])
        sessions_dir = path_obj / ".bugx" / "sessions"
        if sessions_dir.is_dir():
            count += len([s for s in sessions_dir.iterdir() if not s.name.startswith(".")])
    except Exception:
        pass
    if count == 0:
        if path_obj.resolve() == self.workspace.resolve():
            count = len(self._get_workspace_sessions())
        else:
            count = 1
    return count

def _resolve_target_project_dir(self, proj_hash: str | None) -> Path:
    if not proj_hash or proj_hash == self._active_project_hash:
        return self.workspace
    try:
        try:
            from core.runtime import project_settings
        except ImportError:
            from core.runtime import project_settings as project_settings
        resolved = project_settings.resolve_project_dir_by_hash(proj_hash)
        if resolved and resolved.is_dir():
            return resolved
    except Exception:
        pass
    for item in self.get_known_workspaces():
        if item.get("hash") == proj_hash and item.get("working_dir"):
            cand = Path(item["working_dir"]).resolve()
            if cand.is_dir():
                return cand
    return self.workspace

def get_known_workspaces(self) -> list[dict[str, Any]]:
    """Return list of valid BugX projects (current workspace + discovered + registered projects)."""
    try:
        import core.bugx_paths as bugx_paths
        projects = bugx_paths.sync_global_projects(workspace=self.workspace)
        results: list[dict[str, Any]] = []
        for p in projects:
            entry: dict[str, Any] = {
                "hash": p.get("hash", ""),
                "name": p.get("name", ""),
                "working_dir": p.get("working_dir", ""),
                "session_count": p.get("session_count", 1),
                "archived": bool(p.get("archived", False)),
            }
            if p.get("archived_at"):
                entry["archived_at"] = p["archived_at"]
            if p.get("cloud_project_id"):
                entry["cloud_project_id"] = p["cloud_project_id"]
            if p.get("cloud_account_id"):
                entry["cloud_account_id"] = p["cloud_account_id"]
            results.append(entry)
        return results
    except Exception:
        return []

