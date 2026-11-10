#!/usr/bin/env python3
"""BugX WebUI Host Adapter Plugin - DeepSeek DSH CLI."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .base import BaseHostAdapter, HostEvent


class DshHostAdapter(BaseHostAdapter):
    """Adapter for DeepSeek DSH CLI."""

    host_id = "dsh"
    display_name = "DeepSeek Shell (DSH)"
    vendor = "deepseek"
    default_model = ""
    supports_reasoning = False
    supported_effort_levels = []
    default_effort = ""
    supported_modes = ["build", "accept_edits", "bypass", "plan"]

    @staticmethod
    def _get_dsh_home() -> Path:
        env_dir = os.environ.get("DSH_HOME")
        if env_dir:
            p = Path(env_dir).expanduser().resolve()
            if p.exists():
                return p
        return Path.home() / ".dsh"

    @staticmethod
    def _resolve_vendor(prov: str, mid: str = "") -> str:
        p = (prov or "").lower()
        m = (mid or "").lower()
        if "deepseek" in p or "deepseek" in m:
            return "deepseek"
        if any(k in p for k in ("9router", "frouter")) or "9dev" in m:
            return "9router"
        if "kimi" in p or "kimi" in m:
            return "kimi"
        if "openai" in p or "gpt" in m:
            return "openai"
        if any(k in p for k in ("zhipu", "bigmodel", "glm")) or "glm" in m:
            return "zhipu"
        return p.strip() or "deepseek"

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        return False, [], ""

    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        """Discover models directly from DSH settings.yaml on disk."""
        dsh_home = self._get_dsh_home()
        settings_file = dsh_home / "settings.yaml"

        default_dsh = ""
        default_provider = ""
        dsh_models: list[dict[str, Any]] = []
        seen: set[str] = set()

        if settings_file.is_file():
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}
            except Exception:
                try:
                    import re
                    txt = settings_file.read_text(encoding="utf-8")
                    data = {}
                    m = re.search(r"agent-default-model:\s*\n\s*provider:\s*([^\s\r\n]+)\s*\n\s*model:\s*([^\s\r\n]+)", txt)
                    if m:
                        data["agent-default-model"] = {"provider": m.group(1).strip(), "model": m.group(2).strip()}
                except Exception:
                    data = {}

            adm = data.get("agent-default-model", {})
            if isinstance(adm, dict):
                if adm.get("model"):
                    default_dsh = str(adm["model"]).strip()
                if adm.get("provider"):
                    default_provider = str(adm["provider"]).strip()

            # 1. Models from llm-pi-ai.providers
            pi_prov = data.get("llm-pi-ai", {}).get("providers", {})
            if isinstance(pi_prov, dict):
                for pname, pinfo in pi_prov.items():
                    if not isinstance(pinfo, dict):
                        continue
                    for m in pinfo.get("models", []):
                        mid = m.get("id") if isinstance(m, dict) else str(m)
                        if not mid:
                            continue
                        m_str = str(mid).strip()
                        m_name = m.get("name", m_str) if isinstance(m, dict) else m_str
                        if m_str.lower() not in seen:
                            seen.add(m_str.lower())
                            dsh_models.append({
                                "id": m_str,
                                "label": str(m_name).strip(),
                                "vendor": self._resolve_vendor(pname, m_str),
                                "badge": "当前配置" if m_str == default_dsh else pname,
                            })

            # 2. Models from generic providers section
            gen_prov = data.get("providers", {})
            if isinstance(gen_prov, dict):
                for pname, pinfo in gen_prov.items():
                    if not isinstance(pinfo, dict):
                        continue
                    for m in pinfo.get("models", []):
                        mid = m.get("id") if isinstance(m, dict) else str(m)
                        if not mid:
                            continue
                        m_str = str(mid).strip()
                        if m_str.lower() not in seen:
                            seen.add(m_str.lower())
                            dsh_models.append({
                                "id": m_str,
                                "label": m_str,
                                "vendor": self._resolve_vendor(pname, m_str),
                                "badge": "当前配置" if m_str == default_dsh else pname,
                            })

        if default_dsh and default_dsh.lower() in seen:
            def_idx = next(i for i, m in enumerate(dsh_models) if m["id"].lower() == default_dsh.lower())
            if def_idx > 0:
                dsh_models.insert(0, dsh_models.pop(def_idx))
        elif dsh_models:
            default_dsh = dsh_models[0]["id"]
        else:
            default_dsh = ""

        # ZERO hardcoded fallback: if nothing read on disk, return ("", [])
        return (default_dsh, dsh_models)

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
        bin_path = shutil.which("dsh") or str(
            Path.home() / ".nvm/versions/node/v24.18.1/bin/dsh"
        )
        clean_msg = prompt.strip()

        env = os.environ.copy()
        cmd = [bin_path, "--profile", "headless", clean_msg]
        return cmd, env

    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        raw = line.strip()
        if not raw:
            return []
        if raw.startswith(("bugx-supervisor", "dsh:", "warning:")):
            return []
        return [HostEvent("text", content=line, raw_line=raw)]

    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        text_parts = [e.content for e in events if e.event_type == "text" and e.content]
        if text_parts:
            return "".join(text_parts).strip()
        clean_lines = [
            l for l in raw_lines
            if not l.strip().startswith(("bugx-supervisor", "dsh:", "warning:"))
        ]
        return "".join(clean_lines).strip() or "✅ DSH 任务执行完成。"
