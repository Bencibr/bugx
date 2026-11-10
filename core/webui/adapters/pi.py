#!/usr/bin/env python3
"""BugX WebUI Host Adapter Plugin - Pi CLI."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .base import BaseHostAdapter, HostEvent


class PiHostAdapter(BaseHostAdapter):
    """Adapter for Pi AI Assistant CLI."""

    host_id = "pi"
    display_name = "Pi"
    vendor = "auto"
    default_model = ""
    supports_reasoning = True
    supported_effort_levels = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
    default_effort = "high"
    supported_modes = ["build", "accept_edits", "bypass", "plan"]

    @staticmethod
    def _get_pi_agent_dir() -> Path:
        env_dir = os.environ.get("PI_CODING_AGENT_DIR")
        if env_dir:
            p = Path(env_dir).expanduser().resolve()
            if p.exists():
                return p
        home = Path.home().resolve()
        for cand in [home / ".pi" / "agent", home / ".pi"]:
            if cand.exists():
                return cand
        return home / ".pi" / "agent"

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        target = (model or self.default_model).strip().lower()
        if any(k in target for k in ("4o-mini", "gpt-4o", "haiku", "3-5", "3.5")):
            return False, [], ""
        return True, ["off", "minimal", "low", "medium", "high", "xhigh", "max"], self.default_effort

    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        pi_dir = self._get_pi_agent_dir()
        settings_file = pi_dir / "settings.json"
        models_file = pi_dir / "models.json"

        # Fail-closed: If no configuration exists on disk, Pi is unconfigured (Zero Fallback)
        if not settings_file.is_file() and not models_file.is_file():
            return ("", [])

        chosen_default = ""
        chosen_provider = ""
        chosen_effort = self.default_effort

        if settings_file.is_file():
            try:
                s_data = json.loads(settings_file.read_text(encoding="utf-8"))
                if isinstance(s_data, dict):
                    if s_data.get("defaultModel"):
                        chosen_default = str(s_data["defaultModel"]).strip()
                    if s_data.get("defaultProvider"):
                        chosen_provider = str(s_data["defaultProvider"]).strip()
                    if s_data.get("defaultThinkingLevel"):
                        chosen_effort = str(s_data["defaultThinkingLevel"]).strip()
            except Exception:
                pass

        pi_models: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # 1. Parse models from models.json (custom/configured provider models)
        if models_file.is_file():
            try:
                m_data = json.loads(models_file.read_text(encoding="utf-8"))
                providers = m_data.get("providers", {}) if isinstance(m_data, dict) else {}
                for p_name, p_info in providers.items():
                    if not isinstance(p_info, dict):
                        continue
                    p_models = p_info.get("models", [])
                    if isinstance(p_models, dict):
                        for m_id, item in p_models.items():
                            if not m_id or m_id in seen_ids:
                                continue
                            seen_ids.add(m_id)
                            m_name = (item.get("name") if isinstance(item, dict) else m_id) or m_id
                            badge = "当前配置" if m_id == chosen_default else str(p_name)
                            pi_models.append({
                                "id": m_id,
                                "label": str(m_name).strip(),
                                "vendor": str(p_name),
                                "badge": badge,
                                "reasoning": bool(item.get("reasoning", True)) if isinstance(item, dict) else True,
                            })
                    elif isinstance(p_models, list):
                        for item in p_models:
                            if not isinstance(item, dict):
                                continue
                            m_id = str(item.get("id") or "").strip()
                            if not m_id or m_id in seen_ids:
                                continue
                            seen_ids.add(m_id)
                            m_name = str(item.get("name") or m_id).strip()
                            badge = "当前配置" if m_id == chosen_default else str(p_name)
                            pi_models.append({
                                "id": m_id,
                                "label": m_name,
                                "vendor": str(p_name),
                                "badge": badge,
                                "reasoning": bool(item.get("reasoning", True)),
                            })
            except Exception:
                pass

        # 2. If no models parsed from file, try querying `pi --list-models` directly
        if not pi_models:
            bin_path = (
                shutil.which("pi")
                or str(Path.home() / ".pi" / "agent" / "bin" / "pi")
                or "/opt/homebrew/bin/pi"
            )
            if Path(bin_path).exists() or shutil.which("pi"):
                try:
                    import subprocess
                    res = subprocess.run([bin_path, "--list-models"], capture_output=True, text=True, timeout=2.0)
                    if res.returncode == 0:
                        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
                        for ln in lines[1:]:  # skip header
                            parts = ln.split()
                            if len(parts) >= 2:
                                p_name, m_id = parts[0], parts[1]
                                if m_id not in seen_ids:
                                    seen_ids.add(m_id)
                                    badge = "当前配置" if m_id == chosen_default else p_name
                                    pi_models.append({
                                        "id": m_id,
                                        "label": m_id,
                                        "vendor": p_name,
                                        "badge": badge,
                                        "reasoning": (len(parts) > 4 and parts[4].lower() == "yes"),
                                    })
                except Exception:
                    pass

        # Reorder so chosen_default is first item if present
        if chosen_default and chosen_default in seen_ids:
            def_idx = next(i for i, m in enumerate(pi_models) if m["id"] == chosen_default)
            if def_idx > 0:
                pi_models.insert(0, pi_models.pop(def_idx))
        elif pi_models:
            chosen_default = pi_models[0]["id"]
        else:
            chosen_default = ""

        # ZERO hardcoded fallback: if nothing read on disk, return ("", [])
        return (chosen_default, pi_models)

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
        bin_path = (
            shutil.which("pi")
            or str(Path.home() / ".pi" / "agent" / "bin" / "pi")
            or "/opt/homebrew/bin/pi"
        )
        clean_msg = prompt.strip()

        env = os.environ.copy()
        cmd = [bin_path, "--mode", "json"]

        eff_mode = str(mode or "chat").lower()
        if eff_mode == "plan":
            cmd.extend(["--tools", "read,grep,find,ls"])
        if eff_mode in ("bypass", "auto") or auto_approve:
            cmd.append("-a")

        eff_model = model.strip() if model else ""
        if eff_model and eff_model not in ("auto", "default", "pi-default") and " " not in eff_model:
            cmd.extend(["--model", eff_model])

        effort_app, eff_levels, _ = self.get_effort_capabilities(eff_model)
        if effort_app:
            eff_effort = str(reasoning_effort or self.default_effort or "medium").lower()
            if eff_levels and eff_effort not in eff_levels:
                eff_effort = eff_levels[0]
            if eff_effort in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
                cmd.extend(["--thinking", eff_effort])
        else:
            cmd.extend(["--thinking", "off"])

        cmd.extend(["-p", clean_msg])
        return cmd, env

    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        raw = line.strip()
        if not raw:
            return []

        # Structured JSON line
        if raw.startswith("{") and raw.endswith("}"):
            try:
                ev = json.loads(raw)
            except Exception:
                return []

            ev_type = str(ev.get("type") or "")

            # 1. message_update (real-time token usage and delta text)
            if ev_type == "message_update":
                ev_res: list[HostEvent] = []
                u = ev.get("usage")
                if isinstance(u, dict):
                    p_tok = int(u.get("input") or 0)
                    c_tok = int(u.get("output") or 0)
                    cd_tok = int(u.get("cacheRead") or 0)
                    tot_tok = int(u.get("totalTokens") or (p_tok + c_tok))
                    state["usage"] = {"prompt": p_tok, "completion": c_tok, "cached": cd_tok, "total": tot_tok}
                    ev_res.append(HostEvent("token_usage", usage=state["usage"], raw_line=raw))

                ame = ev.get("assistantMessageEvent") or {}
                if ame.get("type") == "text_delta":
                    d = str(ame.get("delta") or "")
                    if d:
                        ev_res.append(HostEvent("text", content=d, raw_line=raw))
                return ev_res

            # 2. Final message or turn end
            if ev_type in ("message_end", "turn_end"):
                msg = ev.get("message") or {}
                u = msg.get("usage")
                if isinstance(u, dict):
                    p_tok = int(u.get("input") or 0)
                    c_tok = int(u.get("output") or 0)
                    cd_tok = int(u.get("cacheRead") or 0)
                    tot_tok = int(u.get("totalTokens") or (p_tok + c_tok))
                    state["usage"] = {"prompt": p_tok, "completion": c_tok, "cached": cd_tok, "total": tot_tok}
                    return [HostEvent("token_usage", usage=state["usage"], raw_line=raw)]

            # 3. Tool use / tool execution
            if ev_type in ("tool_start", "tool_use"):
                return [
                    HostEvent(
                        "tool_start",
                        tool_id=str(ev.get("id") or "pi-tool"),
                        tool_name=str(ev.get("tool") or "tool"),
                        tool_args=ev.get("args") or {},
                        raw_line=raw,
                    )
                ]

            return []

        # Plain text fallback
        return [HostEvent("text", content=line, raw_line=raw)]

    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        text_parts = [e.content for e in events if e.event_type == "text" and e.content]
        if text_parts:
            return "".join(text_parts).strip()
        clean_lines = [l for l in raw_lines if not l.strip().startswith("{")]
        return "".join(clean_lines).strip() or "✅ Pi 任务执行完成。"
