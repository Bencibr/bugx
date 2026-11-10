#!/usr/bin/env python3
"""BugX WebUI Host Adapter Plugin - OpenCode CLI."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .base import BaseHostAdapter, HostEvent


class OpenCodeHostAdapter(BaseHostAdapter):
    """Adapter for OpenCode CLI (supports --format json)."""

    host_id = "opencode"
    display_name = "OpenCode"
    vendor = "auto"
    default_model = ""
    supports_reasoning = False
    supported_effort_levels = []
    default_effort = ""
    supported_modes = ["build", "accept_edits", "bypass", "plan"]

    @staticmethod
    def _resolve_vendor(pname: str, mid: str = "") -> str:
        p = (pname or "").lower()
        m = (mid or "").lower()
        if "deepseek" in p or "deepseek" in m:
            return "deepseek"
        if any(k in p for k in ("9router", "9r")) or "9dev" in m:
            return "9router"
        if "kimi" in p or "kimi" in m:
            return "kimi"
        if "grok" in p or "grok" in m:
            return "grok"
        if any(k in p for k in ("openai", "codex")) or m.startswith(("gpt-", "o1", "o3", "o4")):
            return "openai"
        if any(k in p for k in ("zhipu", "bigmodel", "glm")) or "glm" in m:
            return "zhipu"
        if "opencode" in p:
            return "opencode"
        return p.strip() or "auto"

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        return False, [], ""

    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        """Discover live models directly from OpenCode config and cache (Zero-Fictional Models)."""
        home = Path.home().resolve()
        opencode_models: list[dict[str, Any]] = []
        chosen_default = ""
        seen_ids: set[str] = set()

        # 1. Parse active model and user configured providers from opencode.json
        p_oc = home / ".config" / "opencode" / "opencode.json"
        if p_oc.is_file():
            try:
                d = json.loads(p_oc.read_text(encoding="utf-8"))
                m = d.get("model")
                if m:
                    chosen_default = str(m).strip()
                providers_map = d.get("provider") or {}
                if isinstance(providers_map, dict):
                    for p_name, p_data in providers_map.items():
                        for m_id in (p_data.get("models") or {}).keys():
                            full_id = m_id if m_id.startswith(f"{p_name}/") else f"{p_name}/{m_id}"
                            if full_id not in seen_ids:
                                seen_ids.add(full_id)
                                badge = "当前配置" if full_id == chosen_default else str(p_name)
                                opencode_models.append({
                                    "id": full_id,
                                    "label": full_id,
                                    "vendor": self._resolve_vendor(p_name, full_id),
                                    "badge": badge,
                                })
            except Exception:
                pass

        # 2. Parse OpenCode official free models from ~/.cache/opencode/models.json
        p_cache = home / ".cache" / "opencode" / "models.json"
        if p_cache.is_file():
            try:
                c_data = json.loads(p_cache.read_text(encoding="utf-8"))
                for prov in ("opencode", "opencode-go"):
                    p_val = c_data.get(prov) or {}
                    if not isinstance(p_val, dict):
                        continue
                    p_dict = p_val.get("models") if isinstance(p_val.get("models"), dict) else p_val
                    for mid, minfo in p_dict.items():
                        if mid == "models" and isinstance(minfo, dict):
                            continue
                        clean_mid = mid[len(prov) + 1:] if mid.startswith(f"{prov}/") else mid
                        full_id = f"{prov}/{clean_mid}"
                        mname = (minfo.get("name") if isinstance(minfo, dict) else str(minfo)) or clean_mid
                        is_free = (
                            "free" in full_id.lower()
                            or "free" in mname.lower()
                            or "pickle" in full_id.lower()
                            or "contributor" in full_id.lower()
                        )
                        if is_free and full_id not in seen_ids:
                            seen_ids.add(full_id)
                            badge = "当前配置" if full_id == chosen_default else "免费"
                            opencode_models.append({
                                "id": full_id,
                                "label": mname,
                                "vendor": "opencode",
                                "badge": badge,
                            })
            except Exception:
                pass

        # Reorder so chosen_default is at the front if present
        if chosen_default and chosen_default in seen_ids:
            def_idx = next(i for i, m in enumerate(opencode_models) if m["id"] == chosen_default)
            if def_idx > 0:
                opencode_models.insert(0, opencode_models.pop(def_idx))
        elif opencode_models:
            chosen_default = opencode_models[0]["id"]

        # ZERO hardcoded fallback: if nothing read on disk, return ("", [])
        return (chosen_default, opencode_models)

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
        bin_path = shutil.which("opencode") or "/opt/homebrew/bin/opencode"
        clean_msg = prompt.strip()

        env = os.environ.copy()
        cmd = [bin_path, "run", "--format", "json"]

        eff_mode = str(mode or "chat").lower()
        if eff_mode == "plan":
            cmd.extend(["--agent", "plan"])
        elif eff_mode in ("bypass", "auto"):
            cmd.append("--auto")
        elif eff_mode == "accept_edits":
            clean_msg = f"[Instruction: Please ONLY perform file editing operations to satisfy this request. Do NOT execute shell commands without approval.]\n{clean_msg}"
        elif eff_mode == "build":
            pass
        elif auto_approve:
            cmd.append("--auto")

        eff_model = model.strip() if model else ""
        if eff_model and eff_model not in ("auto", "default") and " " not in eff_model:
            cmd.extend(["--model", eff_model])

        cmd.append(clean_msg)
        return cmd, env

    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        raw = line.strip()
        if not raw:
            return []

        if raw.startswith("{") and raw.endswith("}"):
            try:
                d = json.loads(raw)
            except Exception:
                return []

            ev_type = d.get("type")
            if ev_type == "text":
                part = d.get("part") or {}
                text = str(part.get("text") or "")
                if text:
                    return [HostEvent("text", content=text, raw_line=raw)]
            elif ev_type in ("step_finish", "turn_finish", "usage"):
                part = d.get("part") or d
                u = part.get("tokens") or part.get("usage") or {}
                if isinstance(u, dict):
                    prompt_toks = int(u.get("input") or u.get("input_tokens") or 0)
                    compl_toks = int(u.get("output") or u.get("output_tokens") or 0)
                    cached_toks = int(u.get("cache_read") or u.get("cached_tokens") or 0)
                    total_toks = int(u.get("total") or u.get("total_tokens") or (prompt_toks + compl_toks))
                    state["usage"] = {
                        "prompt": prompt_toks,
                        "completion": compl_toks,
                        "cached": cached_toks,
                        "total": total_toks,
                    }
                    return [HostEvent("token_usage", usage=state["usage"], raw_line=raw)]
            elif ev_type == "tool_use":
                part = d.get("part") or {}
                return [
                    HostEvent(
                        "tool_start",
                        tool_id=str(part.get("id") or "opencode-tool"),
                        tool_name=str(part.get("name") or "tool"),
                        tool_args=part.get("input") or {},
                        raw_line=raw,
                    )
                ]
            return []

        # Plain text line fallback
        return [HostEvent("text", content=line, raw_line=raw)]

    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        text_parts = [e.content for e in events if e.event_type == "text" and e.content]
        if text_parts:
            return "".join(text_parts).strip()

        clean_lines = [l for l in raw_lines if not l.strip().startswith("{")]
        res = "".join(clean_lines).strip()
        return res if res else "✅ OpenCode 任务执行完成。"
