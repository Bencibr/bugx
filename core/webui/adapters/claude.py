#!/usr/bin/env python3
"""BugX WebUI Host Adapter Plugin - Anthropic Claude Code CLI."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .base import BaseHostAdapter, HostEvent

REASONING_EFFORT_BUDGETS = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": 65536,
}


class ClaudeHostAdapter(BaseHostAdapter):
    """Adapter for Anthropic Claude Code CLI."""

    host_id = "claude"
    display_name = "Claude Code"
    vendor = "anthropic"
    default_model = ""
    supports_reasoning = True
    supported_effort_levels = ["low", "medium", "high", "xhigh", "max"]
    default_effort = "high"
    supported_modes = ["build", "accept_edits", "bypass", "plan"]

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        target = (model or self.default_model).strip().lower()
        if "9dev" in target or "3-7" in target or "thinking" in target:
            return True, ["low", "medium", "high", "xhigh", "max"], "high"
        if any(k in target for k in ("3-5", "3.5", "haiku", "opus")):
            return False, [], ""
        return True, ["low", "medium", "high", "xhigh", "max"], "medium"

    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        home = Path.home().resolve()
        claude_models: list[dict[str, Any]] = []
        p_settings = home / ".claude" / "settings.json"
        if p_settings.is_file():
            try:
                d = json.loads(p_settings.read_text(encoding="utf-8"))
                env = d.get("env", {}) if isinstance(d, dict) else {}
                m = (
                    env.get("ANTHROPIC_MODEL")
                    or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                    or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
                    or env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
                )
                if m:
                    claude_models.append({"id": str(m), "label": str(m), "vendor": "anthropic", "badge": "当前配置"})
            except Exception:
                pass
        if claude_models:
            return (claude_models[0]["id"], claude_models)
        # ZERO hardcoded fallback: if nothing read on disk, return ("", [])
        return ("", [])

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
        bin_path = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        clean_msg = prompt.strip()

        env = os.environ.copy()
        eff_model = model.strip() if model else ""
        effort_app, eff_levels, _ = self.get_effort_capabilities(eff_model)

        if effort_app:
            eff_effort = str(reasoning_effort or "high").lower()
            if eff_levels and eff_effort not in eff_levels:
                eff_effort = eff_levels[0]
            budget = REASONING_EFFORT_BUDGETS.get(eff_effort, 8192)
            env["CLAUDE_CODE_THINKING_BUDGET"] = str(budget)
        else:
            env.pop("CLAUDE_CODE_THINKING_BUDGET", None)

        cmd = [bin_path]

        # Mode-aware permission flags
        eff_mode = str(mode or "chat").lower()
        if eff_mode == "plan":
            cmd.extend(["--permission-mode", "plan"])
        elif eff_mode == "accept_edits":
            cmd.extend(["--permission-mode", "acceptEdits"])
        elif eff_mode == "build":
            cmd.extend(["--permission-mode", "manual"])
        elif eff_mode in ("bypass", "auto"):
            cmd.extend(["--permission-mode", "bypassPermissions", "--dangerously-skip-permissions"])
        elif auto_approve:
            cmd.append("--dangerously-skip-permissions")

        if (
            eff_model
            and eff_model not in ("auto", "default")
            and not eff_model.startswith(("gpt-", "o1", "o3", "o4"))
            and " " not in eff_model
        ):
            cmd.extend(["--model", eff_model])
        elif not eff_model or eff_model in ("auto", "default"):
            chosen_def = self.default_model
            if not chosen_def:
                chosen_def, _ = self.discover_models()
            if chosen_def:
                cmd.extend(["--model", chosen_def])

        cmd.extend(["--output-format", "stream-json", "--verbose", "-p", clean_msg])
        return cmd, env

    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        raw = line.strip()
        if not raw:
            return []

        # Structured stream-json line
        if raw.startswith("{") and raw.endswith("}"):
            try:
                ev = json.loads(raw)
            except Exception:
                return []

            ev_type = str(ev.get("type") or "")

            # 1. Assistant message (text and tool use)
            if ev_type == "assistant":
                msg = ev.get("message") or {}
                content_list = msg.get("content") or []
                events: list[HostEvent] = []
                for item in content_list:
                    if not isinstance(item, dict):
                        continue
                    i_type = item.get("type")
                    if i_type == "text":
                        txt = str(item.get("text") or "")
                        if txt:
                            events.append(HostEvent("text", content=txt, raw_line=raw))
                    elif i_type == "tool_use":
                        events.append(
                            HostEvent(
                                "tool_start",
                                tool_id=str(item.get("id") or "claude-tool"),
                                tool_name=str(item.get("name") or "tool"),
                                tool_args=item.get("input") or {},
                                raw_line=raw,
                            )
                        )
                return events

            # 2. Tool result from user
            if ev_type == "user":
                msg = ev.get("message") or {}
                content_list = msg.get("content") or []
                events = []
                for item in content_list:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        events.append(
                            HostEvent(
                                "tool_result",
                                tool_id=str(item.get("tool_use_id") or ""),
                                tool_output=str(item.get("content") or ""),
                                tool_success=not bool(item.get("is_error", False)),
                                raw_line=raw,
                            )
                        )
                return events

            # 3. Final result & token usage
            if ev_type == "result":
                usage = ev.get("usage") or {}
                p_tok = int(usage.get("input_tokens") or 0)
                c_tok = int(usage.get("output_tokens") or 0)
                cd_tok = int(usage.get("cache_read_input_tokens") or 0)
                tot_tok = p_tok + c_tok
                state["usage"] = {
                    "prompt": p_tok,
                    "completion": c_tok,
                    "cached": cd_tok,
                    "total": tot_tok,
                }
                res_text = str(ev.get("result") or "").strip()
                if res_text:
                    state["final_result"] = res_text
                return [HostEvent("token_usage", usage=state["usage"], raw_line=raw)]

            # Ignore internal hook/lifecycle events
            return []

        # Plain text fallback (filter internal CLI debug tags)
        if raw.startswith(("[claude-code:", "warning:", "<CONVERSATION_HISTORY")):
            return []
        return [HostEvent("text", content=line, raw_line=raw)]

    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        text_parts = [e.content for e in events if e.event_type == "text" and e.content]
        if text_parts:
            return "".join(text_parts).strip()

        clean_lines = [
            l for l in raw_lines
            if not l.strip().startswith(("{", "[claude-code:", "warning:", "<CONVERSATION_HISTORY"))
        ]
        res = "".join(clean_lines).strip()
        return res if res else "✅ Claude 任务执行完成。"
