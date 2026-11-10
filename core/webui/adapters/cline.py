#!/usr/bin/env python3
"""BugX WebUI Host Adapter Plugin - Cline CLI."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .base import BaseHostAdapter, HostEvent


class ClineHostAdapter(BaseHostAdapter):
    """Adapter for Cline CLI."""

    host_id = "cline"
    display_name = "Cline"
    vendor = "auto"
    default_model = ""
    supports_reasoning = True
    supported_effort_levels = ["low", "medium", "high", "xhigh"]
    default_effort = "high"
    supported_modes = ["build", "accept_edits", "bypass", "plan"]

    @staticmethod
    def _get_cline_home() -> Path:
        env_dir = os.environ.get("CLINE_HOME")
        if env_dir:
            p = Path(env_dir).expanduser().resolve()
            if p.exists():
                return p
        return Path.home() / ".cline"

    @staticmethod
    def _get_cline_binary_path() -> Path | None:
        cand_paths: list[Path] = []
        w = shutil.which("cline")
        if w:
            cand_paths.append(Path(w).resolve().parent / ".cline")
        cand_paths.extend([
            Path.home() / ".nvm/versions/node/v24.18.1/lib/node_modules/cline/bin/.cline",
            Path("/usr/local/lib/node_modules/cline/bin/.cline"),
            Path("/opt/homebrew/lib/node_modules/cline/bin/.cline"),
        ])
        for p in cand_paths:
            if p.is_file():
                return p
        return None

    @staticmethod
    def _resolve_vendor(pname: str, mid: str = "") -> str:
        p = (pname or "").lower()
        m = (mid or "").lower()
        if "glm" in m or "bigmodel" in m or any(k in p for k in ("zhipu", "bigmodel", "glm")):
            return "zhipu"
        if "deepseek" in m or "dpf" in m or "deepseek" in p:
            return "deepseek"
        if "9dev" in m or any(k in p for k in ("9router", "9r")):
            return "9router"
        if "kimi" in m or "kimi" in p:
            return "kimi"
        if "grok" in m or "grok" in p:
            return "grok"
        if m.startswith(("gpt-", "o1", "o3", "o4")) or any(k in p for k in ("openai", "codex")):
            return "openai"
        if "cline" in p or "poolside" in m:
            return "cline"
        return p.strip() or "auto"

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        cline_home = self._get_cline_home()
        p_cline_prov = cline_home / "data" / "settings" / "providers.json"
        p_cline_models = cline_home / "data" / "settings" / "models.json"
        target_model = (model or self.default_model).strip().lower()

        if p_cline_prov.is_file():
            try:
                d = json.loads(p_cline_prov.read_text(encoding="utf-8"))
                for p_name, p_entry in d.get("providers", {}).items():
                    m = str(p_entry.get("settings", {}).get("model") or "").lower()
                    if m == target_model:
                        r = p_entry.get("settings", {}).get("reasoning")
                        if not r or not isinstance(r, dict) or not r.get("enabled", True):
                            return False, [], ""
                        if "xhs" in m:
                            return False, [], ""
                        eff = str(r.get("effort") or "xhigh")
                        if eff not in ("low", "medium", "high", "xhigh"):
                            eff = "high"
                        return True, ["low", "medium", "high", "xhigh"], eff
            except Exception:
                pass

        if p_cline_models.is_file():
            try:
                m_data = json.loads(p_cline_models.read_text(encoding="utf-8"))
                for p_name, p_entry in m_data.get("providers", {}).items():
                    m_dict = p_entry.get("models", {})
                    if isinstance(m_dict, dict) and target_model in {k.lower(): v for k, v in m_dict.items()}:
                        minfo = next(v for k, v in m_dict.items() if k.lower() == target_model)
                        if isinstance(minfo, dict) and minfo.get("supportsReasoning") is False:
                            return False, [], ""
            except Exception:
                pass

        if any(k in target_model for k in ("flash-1017", "r1", "reasoning", "think")):
            return True, ["low", "medium", "high", "xhigh"], "high"
        return False, [], ""

    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        """Discover models directly from Cline settings and package on disk (Zero-Fictional Models)."""
        cline_home = self._get_cline_home()
        prov_file = cline_home / "data" / "settings" / "providers.json"
        models_file = cline_home / "data" / "settings" / "models.json"

        prov_data: dict[str, Any] = {}
        if prov_file.is_file():
            try:
                prov_data = json.loads(prov_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        models_data: dict[str, Any] = {}
        if models_file.is_file():
            try:
                models_data = json.loads(models_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        last_used = str(prov_data.get("lastUsedProvider") or "")
        providers = prov_data.get("providers", {}) if isinstance(prov_data, dict) else {}

        cline_models: list[dict[str, Any]] = []
        seen: set[str] = set()
        chosen_default = ""

        # 1. Model of lastUsedProvider from providers.json
        if last_used and last_used in providers:
            p_entry = providers[last_used]
            cur_m = (
                p_entry.get("settings", {}).get("model")
                if isinstance(p_entry.get("settings"), dict) else None
            ) or p_entry.get("model")
            if not cur_m and isinstance(p_entry.get("models"), dict) and p_entry["models"]:
                cur_m = next(iter(p_entry["models"].keys()))
            if cur_m:
                m_str = str(cur_m).strip()
                chosen_default = m_str
                seen.add(m_str.lower())
                r_info = p_entry.get("settings", {}).get("reasoning") if isinstance(p_entry.get("settings"), dict) else None
                has_r = bool(r_info.get("enabled", False)) if isinstance(r_info, dict) else False
                cline_models.append({
                    "id": m_str,
                    "label": m_str,
                    "vendor": self._resolve_vendor(last_used, m_str),
                    "badge": "当前配置",
                    "reasoning": has_r,
                })

        # 2. Configured active models in providers.json
        for pname, pdata in providers.items():
            if not isinstance(pdata, dict):
                continue
            settings = pdata.get("settings", {}) if isinstance(pdata.get("settings"), dict) else pdata
            m_candidates: list[str] = []
            m = settings.get("model")
            if m:
                m_candidates.append(str(m).strip())
            if isinstance(pdata.get("models"), dict):
                m_candidates.extend(str(k).strip() for k in pdata["models"].keys())
            for m_str in m_candidates:
                if not m_str or m_str.lower() in seen:
                    continue
                seen.add(m_str.lower())
                r_info = settings.get("reasoning") if isinstance(settings, dict) else None
                has_r = bool(r_info.get("enabled", False)) if isinstance(r_info, dict) else False
                is_free = "free" in m_str.lower()
                badge = "免费" if is_free else pname
                cline_models.append({
                    "id": m_str,
                    "label": m_str,
                    "vendor": self._resolve_vendor(pname, m_str),
                    "badge": badge,
                    "reasoning": has_r,
                })

        # 3. Built-in official models from Cline CLI binary (free models and cline-pass models)
        bin_p = self._get_cline_binary_path()
        if bin_p and bin_p.is_file():
            try:
                import re
                content = bin_p.read_bytes()
                # If provider 'cline' is active/configured, extract official free models
                if "cline" in providers:
                    idx = content.find(b'"reflection/beam-free"')
                    if idx != -1:
                        raw = content[max(0, idx - 500):idx + 4000].decode("utf-8", errors="ignore")
                        cline_m = re.findall(r'id:"([^"]+)",name:"([^"]+)"', raw)
                        for mid, mname in cline_m:
                            if mid.lower() not in seen:
                                seen.add(mid.lower())
                                is_free = "free" in mid.lower() or "free" in mname.lower()
                                cline_models.append({
                                    "id": mid,
                                    "label": mname,
                                    "vendor": "cline",
                                    "badge": "免费" if is_free else "cline",
                                    "reasoning": True,
                                })
                # If provider 'cline-pass' is active/configured, extract pass models
                if "cline-pass" in providers:
                    idx_p = content.find(b'"cline-pass":{')
                    if idx_p != -1:
                        raw_p = content[idx_p:idx_p + 15000].decode("utf-8", errors="ignore")
                        pass_m = re.findall(r'name:"([^"]+)",id:"(cline-pass/[^"]+)"', raw_p)
                        for mname, mid in pass_m:
                            if mid.lower() not in seen:
                                seen.add(mid.lower())
                                cline_models.append({
                                    "id": mid,
                                    "label": mname,
                                    "vendor": "deepseek" if "deepseek" in mid.lower() else "cline-pass",
                                    "badge": "cline-pass",
                                    "reasoning": True,
                                })
            except Exception:
                pass

        # 4. Discovered models in models.json
        for pname, pdata in models_data.get("providers", {}).items():
            if not isinstance(pdata, dict):
                continue
            m_coll = pdata.get("models", {})
            if isinstance(m_coll, dict):
                for mid, minfo in m_coll.items():
                    m_str = str(mid).strip()
                    if not m_str or m_str.lower() in seen:
                        continue
                    seen.add(m_str.lower())
                    has_r = bool(minfo.get("supportsReasoning", False)) if isinstance(minfo, dict) else False
                    cline_models.append({
                        "id": m_str,
                        "label": m_str,
                        "vendor": self._resolve_vendor(pname, m_str),
                        "badge": pname,
                        "reasoning": has_r,
                    })
            elif isinstance(m_coll, list):
                for item in m_coll:
                    m_str = str(item).strip()
                    if not m_str or m_str.lower() in seen:
                        continue
                    seen.add(m_str.lower())
                    cline_models.append({
                        "id": m_str,
                        "label": m_str,
                        "vendor": self._resolve_vendor(pname, m_str),
                        "badge": pname,
                        "reasoning": False,
                    })

        # Reorder so chosen_default is at the front if present
        if chosen_default and chosen_default.lower() in seen:
            def_idx = next(i for i, m in enumerate(cline_models) if m["id"].lower() == chosen_default.lower())
            if def_idx > 0:
                cline_models.insert(0, cline_models.pop(def_idx))
        elif cline_models:
            chosen_default = cline_models[0]["id"]
        else:
            chosen_default = ""

        # ZERO hardcoded fallback: if nothing read on disk, return ("", [])
        return (chosen_default, cline_models)

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        *,
        mode: str = "chat",
        model: str = "",
        reasoning_effort: str = "medium",
        auto_approve: bool = True,
        target_files: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        bin_path = shutil.which("cline") or str(
            Path.home() / ".nvm/versions/node/v24.18.1/bin/cline"
        )
        clean_msg = prompt.strip()

        env = os.environ.copy()
        cmd = [bin_path, "--json"]

        eff_mode = str(mode or "chat").lower()
        if eff_mode == "plan":
            cmd.append("-p")
        elif eff_mode == "accept_edits":
            cmd.extend(["--auto-approve", "true"])
        elif eff_mode == "build":
            cmd.extend(["--auto-approve", "false"])
        elif eff_mode in ("bypass", "auto") or auto_approve:
            cmd.append("-y")

        eff_model = model.strip() if model else ""
        if eff_model and eff_model not in ("auto", "default") and " " not in eff_model:
            cmd.extend(["--model", eff_model])

        effort_app, eff_levels, _ = self.get_effort_capabilities(eff_model)
        if effort_app:
            eff_effort = str(reasoning_effort or "high").lower()
            if eff_levels and eff_effort not in eff_levels:
                eff_effort = eff_levels[0]
            if eff_effort in ("none", "low", "medium", "high", "xhigh"):
                cmd.extend(["--thinking", eff_effort])
        else:
            cmd.extend(["--thinking", "none"])

        cmd.append(clean_msg)
        return cmd, env

    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        import re
        clean = re.sub(r"\x1b\[[0-9;]*[mK]", "", line)
        raw = clean.strip()
        if not raw:
            return []

        # Structured JSON line
        if raw.startswith("{") and raw.endswith("}"):
            try:
                ev = json.loads(raw)
            except Exception:
                return []

            ev_type = str(ev.get("type") or "")

            # 1. run_result or agent_event usage
            if ev_type in ("run_result", "agent_event"):
                u = ev.get("usage") or (ev.get("event") or {}).get("usage")
                if isinstance(u, dict):
                    prompt_toks = int(u.get("inputTokens") or u.get("input_tokens") or 0)
                    compl_toks = int(u.get("outputTokens") or u.get("output_tokens") or 0)
                    cached_toks = int(u.get("cacheReadTokens") or u.get("cache_read_input_tokens") or 0)
                    total_toks = int(u.get("totalTokens") or (prompt_toks + compl_toks))
                    state["usage"] = {
                        "prompt": prompt_toks,
                        "completion": compl_toks,
                        "cached": cached_toks,
                        "total": total_toks,
                    }
                    txt = str(ev.get("text") or (ev.get("event") or {}).get("text") or "").strip()
                    if txt:
                        state["final_text"] = txt
                    return [HostEvent("token_usage", usage=state["usage"], raw_line=raw)]

            # 2. Error message
            if ev_type == "error":
                msg = str(ev.get("message") or "")
                if "hook dispatch failed" in msg:
                    return []
                return [HostEvent("text", content=f"\n⚠️ {msg}\n", raw_line=raw)]

            # Ignore internal lifecycle frames
            if ev_type in ("hook_event", "system"):
                return []

            return []

        # Plain text fallback
        if "Submission recorded (verified):" in raw:
            summary = raw.split("Submission recorded (verified):", 1)[1].strip()
            state["last_submission"] = summary
            return [HostEvent("text", content=f"{summary}\n", raw_line=clean)]

        if "Too small: expected string to have >=10 characters" in raw or "[submit_and_exit]" in raw:
            return []

        return [HostEvent("text", content=clean, raw_line=clean)]

    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        if state_text := [e.content for e in events if e.event_type == "text" and e.content]:
            return "".join(state_text).strip()

        # If we captured a verified submission summary
        for e in reversed(events):
            if e.event_type == "text" and e.content.strip():
                import re
                c = re.sub(r"\x1b\[[0-9;]*[mK]", "", e.content).strip()
                if c.startswith("Submission recorded"):
                    c = c.split(":", 1)[1].strip()
                if c and not c.startswith(("[submit_and_exit]", "{")):
                    return c

        clean_lines = [
            l for l in raw_lines
            if not l.strip().startswith(("{", "[submit_and_exit]"))
        ]
        res = "".join(clean_lines).strip()
        return res if res else "✅ Cline 任务执行完成。"
