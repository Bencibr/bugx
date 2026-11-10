"""Sessions, models, docs and filesystem search service for WebUI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

try:
    from core.webui.dispatcher import sanitize_chat_text
except ImportError:
    try:
        from core.webui.dispatcher import sanitize_chat_text
    except ImportError:
        sanitize_chat_text = lambda t: t

def _get_workspace_sessions(self, proj_hash: Optional[str] = None) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    target_path = self._resolve_target_project_dir(proj_hash)
    target_dir = str(target_path)
    eff_hash = proj_hash or self._compute_hash(target_dir)

    # 1. Persisted sessions from .bugx/sessions/index.json (REQ-02)
    persisted = self.dispatcher.list_sessions(target_path)
    seen_ids = {str(s.get("id")) for s in persisted if s.get("id")}
    merged: list[dict[str, Any]] = list(persisted)

    # 2. Pollings from .bugx/pollings/polling-*
    pollings_dir = target_path / ".bugx" / "pollings"
    if pollings_dir.is_dir():
        try:
            for pdir in sorted(pollings_dir.iterdir(), reverse=True):
                if not pdir.is_dir() or not pdir.name.startswith("polling-"):
                    continue
                sid = pdir.name
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                state_file = pdir / "state.json"
                sname = f"安全审计与自主挖掘 ({sid})"
                ts = pdir.stat().st_mtime * 1000
                status_str = "completed"
                if state_file.is_file():
                    try:
                        sdata = json.loads(state_file.read_text(encoding="utf-8"))
                        status_str = str(sdata.get("state") or "completed")
                        sess_list = sdata.get("sessions") or []
                        if sess_list and isinstance(sess_list[-1], dict) and sess_list[-1].get("reason"):
                            r_text = sess_list[-1]["reason"].split("；")[0]
                            sname = r_text[:50]
                    except Exception:
                        pass
                merged.append({
                    "id": sid,
                    "session_id": sid,
                    "name": sname,
                    "provider": "bugx",
                    "mode": "mine",
                    "status": status_str,
                    "created_at": int(ts),
                    "updated_at": int(ts),
                    "project_hash": eff_hash,
                    "working_dir": target_dir,
                })
        except Exception:
            pass

    if merged:
        merged.sort(key=lambda x: int(x.get("updated_at") or x.get("created_at") or 0), reverse=True)
        return merged

    return [
        {
            "id": "session-default-01",
            "session_id": "session-default-01",
            "name": "当前项目自主安全审计",
            "provider": "codex",
            "mode": "chat",
            "status": "idle",
            "created_at": now_ms - 3600000,
            "updated_at": now_ms - 60000,
            "project_hash": eff_hash,
            "working_dir": target_dir,
        },
    ]

def _get_session_detail(
    self,
    sid: str,
    *,
    proj_hash: Optional[str] = None,
    since_seq: int = 0,
) -> dict[str, Any]:
    target_path = self._resolve_target_project_dir(proj_hash)
    detail = self.dispatcher.get_session_detail(target_path, sid, since_seq=since_seq)
    if detail.get("messages"):
        return detail

    welcome_text = f"欢迎使用 BugX Studio！当前工作区: `{target_path}`\n你可以选择任意受支持宿主（如 Cline / Claude / Codex / OpenCode / BugX）开展对话，或点击「🚀 启动挖掘」调用 BugX 插件。"
    sname = detail.get("name") or ("当前项目自主安全审计" if "default" in sid else f"会话 ({sid})")

    state_file = target_path / ".bugx" / "pollings" / sid / "state.json"
    if state_file.is_file():
        try:
            sdata = json.loads(state_file.read_text(encoding="utf-8"))
            sname = f"安全审计与自主挖掘 ({sid})"
            sess_list = sdata.get("sessions") or []
            reasons = [s["reason"] for s in sess_list if isinstance(s, dict) and s.get("reason")]
            reason_str = "\n".join(f"- {r}" for r in reasons) if reasons else "审计已完成。"
            welcome_text = (
                f"**历史自主审计会话: `{sid}`**\n\n"
                f"状态: `{sdata.get('state', 'completed')}`\n"
                f"耗时: {sdata.get('total_elapsed_seconds', 0):.1f}s\n\n"
                f"**结算说明**:\n{reason_str}"
            )
        except Exception:
            pass

    detail["name"] = sname
    detail["messages"] = [
        {
            "role": "assistant",
            "content": sanitize_chat_text(welcome_text),
            "parts": [{"kind": "text", "text": sanitize_chat_text(welcome_text)}],
            "created_at": int(time.time() * 1000) - 3600000,
            "ts": int(time.time() * 1000) - 3600000,
        }
    ]
    return detail

def _discover_host_real_models(self) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Probe real local host configurations for actually configured models (Zero-Fictional Models)."""
    try:
        from .adapters import get_adapter_registry
    except ImportError:
        from core.webui.adapters import get_adapter_registry

    reg = get_adapter_registry()
    discovered: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for adapter in reg.list_adapters():
        discovered[adapter.host_id] = adapter.discover_models()
    return discovered

def _get_models_list(self, provider: Optional[str] = None) -> list[dict[str, Any]]:
    """Return dynamic multi-host & model catalog based on real host configurations and model capabilities (REQ-01)."""
    try:
        from .adapters import get_adapter_registry
    except ImportError:
        from core.webui.adapters import get_adapter_registry

    reg = get_adapter_registry()
    real_models = self._discover_host_real_models()

    home = Path.home()
    host_specs = [
        ("cline", "Cline", "auto", False, shutil.which("cline") or Path("/opt/homebrew/bin/cline").is_file()),
        ("codex", "Codex", "openai", True, shutil.which("codex") or (home / ".nvm/versions/node/v24.18.1/bin/codex").is_file()),
        ("claude", "Claude Code", "anthropic", False, shutil.which("claude") or (home / ".local/bin/claude").is_file()),
        ("opencode", "OpenCode", "auto", False, shutil.which("opencode") or Path("/opt/homebrew/bin/opencode").is_file()),
        ("pi", "Pi Agent", "auto", False, shutil.which("pi") or (Path.home() / ".pi/agent/bin/pi").is_file() or Path("/opt/homebrew/bin/pi").is_file()),
        ("zcode", "ZCode", "zhipu", False, bool(shutil.which("zcode") or Path("/Applications/ZCode.app").is_dir() or (Path.home() / "Applications/ZCode.app").is_dir() or (Path.home() / ".zcode").is_dir() or Path("/opt/homebrew/bin/zcode").is_file())),
        ("dsh", "DSH Agent", "deepseek", False, shutil.which("dsh") or Path("/opt/homebrew/bin/dsh").is_file()),
        ("bugx", "BugX Engine", "bugx", False, True),
    ]

    catalog: list[dict[str, Any]] = []
    for host_id, host_name, vendor, is_def, installed in host_specs:
        adapter = reg.get_adapter(host_id)
        mod_tuple = real_models.get(host_id) or ("", [])
        raw_options = mod_tuple[1]
        active_model = self._active_host_models.get(host_id) or mod_tuple[0]
        if raw_options and not any(o.get("id") == active_model for o in raw_options):
            active_model = raw_options[0].get("id", "")
        elif not raw_options:
            active_model = ""

        # Model options enriched with model-specific effort capabilities
        raw_options = mod_tuple[1]
        enriched_options: list[dict[str, Any]] = []
        for opt in raw_options:
            opt_id = str(opt.get("id") or "")
            opt_app, opt_levels, opt_def = adapter.get_effort_capabilities(opt_id)
            enriched_opt = dict(opt)
            enriched_opt["effort_applicable"] = opt_app
            enriched_opt["effort_levels"] = opt_levels
            enriched_opt["default_effort"] = opt_def
            enriched_options.append(enriched_opt)

        effort_app, eff_levels, def_effort = adapter.get_effort_capabilities(active_model)

        catalog.append({
            "provider": host_id,
            "host_name": host_name,
            "model": host_name if host_id != "bugx" else "BugX 攻防引擎",
            "model_id": active_model,
            "model_options": enriched_options,
            "installed": bool(installed),
            "is_default": is_def,
            "supports_mine": True,
            "effort_applicable": effort_app,
            "effort_levels": eff_levels,
            "reasoning_effort": def_effort or ("medium" if effort_app else ""),
            "supported_modes": list(adapter.supported_modes),
        })

    if provider:
        p_low = provider.lower().strip()
        filtered = [m for m in catalog if m["provider"] == p_low]
        if filtered:
            return filtered
    return catalog

# ------------------------------------------------------------------
def _get_docs_tree(self, proj_root: Path) -> dict[str, Any]:
    root = proj_root.resolve()
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".pytest_cache", ".mypy_cache"}
    allowed_exts = {".md", ".txt", ".mermaid", ".json", ".yaml", ".yml", ".toml"}

    core_docs: list[dict[str, Any]] = []
    design_docs: list[dict[str, Any]] = []
    audit_reports: list[dict[str, Any]] = []
    config_files: list[dict[str, Any]] = []

    def _make_item(fpath: Path, category: str) -> dict[str, Any] | None:
        try:
            if fpath.is_symlink():
                if not fpath.resolve().is_relative_to(root):
                    return None
            rel = fpath.relative_to(root).as_posix()
            st = fpath.stat()
            return {
                "path": rel,
                "name": fpath.name,
                "category": category,
                "ext": fpath.suffix.lower(),
                "size": st.st_size,
                "updated_at": int(st.st_mtime * 1000),
                "readonly": True,
            }
        except Exception:
            return None

    # 1. Top-level core docs & configs
    try:
        for item in sorted(root.iterdir()):
            if item.is_file() and item.suffix.lower() in allowed_exts:
                if item.suffix.lower() in {".md", ".txt", ".mermaid"}:
                    entry = _make_item(item, "core_docs")
                    if entry:
                        core_docs.append(entry)
                else:
                    entry = _make_item(item, "config_files")
                    if entry:
                        config_files.append(entry)
    except Exception:
        pass

    # 2. docs/** directory
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        try:
            for sub in sorted(docs_dir.rglob("*")):
                if any(part in skip_dirs for part in sub.parts):
                    continue
                if sub.is_file() and sub.suffix.lower() in allowed_exts:
                    entry = _make_item(sub, "design_docs")
                    if entry:
                        design_docs.append(entry)
                        if len(design_docs) >= 300:
                            break
        except Exception:
            pass

    # 3. .bugx/reports/** and .bugx/pollings/**/report*.md
    dot_bugx = root / ".bugx"
    if dot_bugx.is_dir():
        try:
            for sub in sorted(dot_bugx.rglob("*")):
                if sub.is_file() and sub.suffix.lower() in {".md", ".json"} and (
                    "report" in sub.name.lower() or "finding" in sub.name.lower() or "summary" in sub.name.lower()
                ):
                    entry = _make_item(sub, "audit_reports")
                    if entry:
                        audit_reports.append(entry)
                        if len(audit_reports) >= 100:
                            break
        except Exception:
            pass

    all_files = core_docs + design_docs + audit_reports + config_files
    return {
        "project_root": str(root),
        "readonly": True,
        "read_only": True,
        "files": all_files,
        "groups": [
            {"id": "core_docs", "title": "📌 项目核心文档", "items": core_docs},
            {"id": "design_docs", "title": "📚 设计与规范文档 (docs/)", "items": design_docs},
            {"id": "audit_reports", "title": "🛡️ BugX 审计报告 (.bugx/)", "items": audit_reports},
            {"id": "config_files", "title": "⚙️ 项目配置文件", "items": config_files},
        ],
        "total": len(all_files),
    }

def _get_doc_content_readonly(self, proj_root: Path, rel_path: str) -> tuple[dict[str, Any], int]:
    root = proj_root.resolve()
    clean_rel = str(rel_path or "").strip()
    if not clean_rel:
        return {"ok": False, "error": "Missing document path"}, 400
    cand = root / clean_rel
    try:
        resolved = cand.resolve()
        if not resolved.is_relative_to(root):
            return {"ok": False, "error": "Forbidden: path traversal outside project root"}, 403
        if cand.is_symlink() and not resolved.is_relative_to(root):
            return {"ok": False, "error": "Forbidden: external symlink"}, 403
    except Exception:
        return {"ok": False, "error": "Forbidden path"}, 403

    if not resolved.is_file():
        return {"ok": False, "error": f"Document not found: {clean_rel}"}, 404

    try:
        st = resolved.stat()
        max_preview_bytes = 256 * 1024
        size_limit = 2 * 1024 * 1024
        with resolved.open("rb") as fh:
            raw_bytes = fh.read(max_preview_bytes if st.st_size > size_limit else size_limit)
        if b"\x00" in raw_bytes[:4096]:
            return {"ok": False, "error": "Binary file cannot be rendered as document"}, 415
        text = raw_bytes.decode("utf-8", errors="replace")
        truncated = st.st_size > len(raw_bytes)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        return {
            "ok": True,
            "readonly": True,
            "read_only": True,
            "path": resolved.relative_to(root).as_posix(),
            "abs_path": str(resolved),
            "name": resolved.name,
            "ext": resolved.suffix.lower(),
            "size": st.st_size,
            "truncated": truncated,
            "sha256": digest,
            "content": text,
        }, 200
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 500

def _search_docs_fts5(self, proj_root: Path, query: str) -> dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        return {"ok": True, "query": "", "results": []}
    root = proj_root.resolve()
    results: list[dict[str, Any]] = []

    # Scan indexed docs tree for fast substring/token match with line numbers
    tree = self._get_docs_tree(root)
    q_lower = q.lower()
    for grp in tree.get("groups", []):
        for item in grp.get("items", []):
            rel = item["path"]
            fpath = root / rel
            try:
                if fpath.stat().st_size > 2 * 1024 * 1024:
                    continue
                lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
                for idx, line in enumerate(lines, 1):
                    if q_lower in line.lower():
                        results.append({
                            "path": rel,
                            "name": item["name"],
                            "category": item["category"],
                            "line": idx,
                            "snippet": line.strip()[:200],
                        })
                        if len(results) >= 50:
                            return {"ok": True, "query": q, "results": results}
                        break
            except Exception:
                continue
    return {"ok": True, "query": q, "results": results}

def _list_fs(self, sub_path: str) -> dict[str, Any]:
    """List subdirectories and files in sub_path (supports ~, absolute, and relative paths)."""
    raw = str(sub_path or "~").strip()
    if raw == "~":
        target_dir = Path.home()
    elif raw.startswith("~/"):
        target_dir = Path.home() / raw[2:]
    else:
        p = Path(raw)
        target_dir = p if p.is_absolute() else (self.workspace / p)
    try:
        target_dir = target_dir.resolve()
    except Exception:
        target_dir = self.workspace.resolve()

    if not target_dir.exists() or not target_dir.is_dir():
        target_dir = self.workspace.resolve()

    dirs: list[str] = []
    files: list[str] = []
    entries: list[dict[str, Any]] = []
    try:
        for item in sorted(target_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name.startswith(".git"):
                continue
            size = 0
            mtime = 0
            try:
                st = item.stat()
                size = st.st_size
                mtime = int(st.st_mtime * 1000)
            except Exception:
                pass
            if item.is_dir():
                dirs.append(item.name)
                entries.append({"name": item.name, "is_dir": True, "full": str(item), "size": size, "mtime": mtime})
            else:
                files.append(item.name)
                entries.append({"name": item.name, "is_dir": False, "full": str(item), "size": size, "mtime": mtime})
    except Exception:
        pass

    return {
        "path": str(target_dir),
        "dirs": dirs,
        "files": files,
        "entries": entries,
    }

def _search_fs(self, sub_path: str, query: str) -> list[dict[str, Any]]:
    """Search entries in sub_path matching query."""
    raw = str(sub_path or ".").strip()
    if raw == "~":
        target_dir = Path.home()
    elif raw.startswith("~/"):
        target_dir = Path.home() / raw[2:]
    else:
        p = Path(raw)
        target_dir = p if p.is_absolute() else (self.workspace / p)
    try:
        target_dir = target_dir.resolve()
    except Exception:
        target_dir = self.workspace.resolve()

    if not target_dir.exists() or not target_dir.is_dir():
        return []

    q_low = query.lower().strip()
    matches: list[dict[str, Any]] = []
    try:
        for item in sorted(target_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name.startswith(".git"):
                continue
            if not q_low or q_low in item.name.lower():
                matches.append({
                    "name": item.name,
                    "path": f"/{item.name}" if not item.name.startswith("/") else item.name,
                    "is_dir": item.is_dir(),
                })
                if len(matches) >= 50:
                    break
    except Exception:
        pass
    return matches

