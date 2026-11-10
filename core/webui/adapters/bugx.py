#!/usr/bin/env python3
"""BugX WebUI Host Adapter Plugin - BugX Autonomous Audit/Mining Engine."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .base import BaseHostAdapter, HostEvent


class BugxHostAdapter(BaseHostAdapter):
    """Adapter for native BugX Autonomous Audit and Mining Engine."""

    host_id = "bugx"
    display_name = "BugX Autonomous"
    vendor = "bugx"
    default_model = "bugx-autonomous"
    supports_reasoning = True
    supported_effort_levels = ["low", "medium", "high", "xhigh"]
    default_effort = "high"
    supported_modes = ["mine", "build", "accept_edits", "bypass", "plan"]

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        return True, ["low", "medium", "high", "xhigh"], "high"

    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        bugx_models = [
            {"id": "bugx-autonomous", "label": "BugX Autonomous Mining Engine", "vendor": "bugx", "badge": "原生核心"},
            {"id": "bugx-adversarial", "label": "BugX Red-Team Adversarial Prober", "vendor": "bugx", "badge": "攻防基线"},
        ]
        return (self.default_model, bugx_models)

    def build_command(
        self,
        prompt: str,
        cwd: Path,
        *,
        mode: str = "mine",
        model: str = "",
        reasoning_effort: str = "medium",
        auto_approve: bool = True,
        target_files: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        env = os.environ.copy()
        env["BUGX_HOST_AGENT"] = "codex"
        if target_files:
            env["BUGX_TARGET_FILES"] = ",".join(target_files)

        cmd = [
            sys.executable,
            "-m",
            "bugx_cli.cli",
            "audit",
            "--root",
            str(cwd),
            "--format",
            "both",
            "--json",
        ]
        eff_mode = str(mode or "mine").lower()
        if eff_mode == "plan":
            cmd.extend(["--goal", "find_bugs"])
        elif eff_mode in ("bypass", "auto"):
            cmd.extend(["--goal", "repair_preview", "--concurrency", "6"])
        elif eff_mode in ("build", "accept_edits"):
            cmd.extend(["--goal", "repair_preview"])
        return cmd, env

    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        raw = line.strip()
        if not raw:
            return []
        if raw.startswith("{") and raw.endswith("}"):
            try:
                ev = json.loads(raw)
                if "usage" in ev or "tokens" in ev:
                    u = ev.get("usage") or ev.get("tokens") or {}
                    p_tok = int(u.get("prompt") or u.get("input") or 0)
                    c_tok = int(u.get("completion") or u.get("output") or 0)
                    state["usage"] = {
                        "prompt": p_tok,
                        "completion": c_tok,
                        "cached": int(u.get("cached") or 0),
                        "total": int(u.get("total") or (p_tok + c_tok)),
                    }
                    return [HostEvent("token_usage", usage=state["usage"], raw_line=raw)]
                msg = ev.get("message") or ev.get("summary") or ""
                if msg:
                    return [HostEvent("text", content=f"{msg}\n", raw_line=raw)]
            except Exception:
                pass
        return [HostEvent("text", content=line, raw_line=raw)]

    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        text_parts = [e.content for e in events if e.event_type == "text" and e.content]
        if text_parts:
            return "".join(text_parts).strip()
        return "".join(raw_lines).strip() or "✅ BugX 自主安全挖掘完成。"
