#!/usr/bin/env python3
"""BugX WebUI Host Adapter Plugin - OpenAI Codex CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .base import BaseHostAdapter, HostEvent


class CodexHostAdapter(BaseHostAdapter):
    """Adapter for OpenAI Codex CLI (supports --json streaming and review)."""

    host_id = "codex"
    display_name = "Codex"
    vendor = "openai"
    default_model = ""
    supports_reasoning = True
    supported_effort_levels = ["low", "medium", "high", "xhigh", "max"]
    default_effort = "medium"
    supported_modes = ["build", "accept_edits", "bypass", "plan"]

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        home = Path.home().resolve()
        p_cache = home / ".codex" / "models_cache.json"
        target_model = (model or self.default_model).strip().lower()

        if p_cache.is_file():
            try:
                d = json.loads(p_cache.read_text(encoding="utf-8"))
                for item in d.get("models", []):
                    slug = str(item.get("slug") or "").lower()
                    if slug == target_model:
                        levels = item.get("supported_reasoning_levels") or []
                        if levels:
                            effs = [str(lvl.get("effort") or "") for lvl in levels if lvl.get("effort")]
                            effs = [e for e in effs if e in ("low", "medium", "high", "xhigh", "max")]
                            def_lvl = str(item.get("default_reasoning_level") or "medium")
                            return True, effs, def_lvl
                        return False, [], ""
            except Exception:
                pass

        # Heuristic for known OpenAI reasoning families
        if any(target_model.startswith(prefix) for prefix in ("o1", "o3", "o4", "gpt-6", "gpt-5.6")):
            return True, ["low", "medium", "high", "xhigh", "max"], "medium"
        return False, [], ""

    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        home = Path.home().resolve()
        codex_models: list[dict[str, Any]] = []
        chosen_default = ""

        # 1. Read ~/.codex/config.toml
        p_cfg = home / ".codex" / "config.toml"
        if p_cfg.is_file():
            try:
                txt = p_cfg.read_text(encoding="utf-8")
                match = re.search(r"^model\s*=\s*[\"']([^'\"]+)[\"']", txt, re.M)
                if match:
                    chosen_default = match.group(1).strip()
            except Exception:
                pass

        # 2. Read ~/.codex/models_cache.json
        p_cache = home / ".codex" / "models_cache.json"
        if p_cache.is_file():
            try:
                d = json.loads(p_cache.read_text(encoding="utf-8"))
                for item in d.get("models", []):
                    slug = str(item.get("slug") or "")
                    name = str(item.get("display_name") or slug)
                    if slug:
                        badge = "当前配置" if slug == chosen_default else ("推荐" if "astra" in slug or "sol" in slug else "")
                        codex_models.append({"id": slug, "label": name, "vendor": "openai", "badge": badge})
            except Exception:
                pass

        if chosen_default and any(m["id"] == chosen_default for m in codex_models):
            codex_models.sort(key=lambda x: 0 if x["id"] == chosen_default else 1)
        elif codex_models:
            chosen_default = codex_models[0]["id"]
        else:
            chosen_default = ""

        # ZERO hardcoded fallback: if nothing read on disk, return ("", [])
        return (chosen_default, codex_models)

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
        bin_path = shutil.which("codex") or str(
            Path.home() / ".nvm/versions/node/v24.18.1/bin/codex"
        )
        clean_msg = prompt.strip()

        env = os.environ.copy()

        if clean_msg.startswith("/review"):
            cmd = [bin_path, "review", "--uncommitted"]
            return cmd, env

        cmd = [bin_path, "exec", "--json"]

        # Mode-aware sandbox & permission policy
        eff_mode = str(mode or "chat").lower()
        if eff_mode == "plan":
            cmd.extend(["-s", "read-only"])
        elif eff_mode == "accept_edits":
            cmd.extend(["-s", "workspace-write", "--approve-for-me"])
        elif eff_mode == "build":
            cmd.extend(["-s", "workspace-write"])
        elif eff_mode in ("bypass", "auto"):
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif auto_approve:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")

        eff_model = model.strip() if model else ""
        # Filter out cross-host non-OpenAI models (e.g. 9dev from claude)
        if (
            eff_model
            and eff_model not in ("auto", "default")
            and not eff_model.startswith("claude")
            and eff_model != "9dev"
            and " " not in eff_model
        ):
            cmd.extend(["-m", eff_model])

        # Inject reasoning effort ONLY if model supports it
        effort_app, eff_levels, _ = self.get_effort_capabilities(eff_model)
        if effort_app:
            eff_effort = str(reasoning_effort or "medium").lower()
            if eff_levels and eff_effort not in eff_levels:
                eff_effort = eff_levels[0]
            if eff_effort in ("low", "medium", "high", "xhigh", "max"):
                cmd.extend(["-c", f"model_reasoning_effort=\"{eff_effort}\""])

        cmd.append(clean_msg)
        return cmd, env

    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        raw = line.strip()
        if not raw:
            return []

        # When running codex exec --json, each event is a JSON object on its own line
        if raw.startswith("{") and raw.endswith("}"):
            try:
                ev = json.loads(raw)
            except Exception:
                return []

            ev_type = str(ev.get("type") or "")

            # 1. Agent message text chunk
            if ev_type == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    txt = str(item.get("text") or "")
                    if txt:
                        return [HostEvent("text", content=txt, raw_line=raw)]
                elif item.get("type") == "command_execution":
                    # Tool completion
                    cmd_id = str(item.get("id") or "cmd")
                    output = str(item.get("aggregated_output") or "")
                    code = item.get("exit_code")
                    success = code == 0 if code is not None else True
                    return [
                        HostEvent(
                            "tool_result",
                            tool_id=cmd_id,
                            tool_name="bash",
                            tool_output=output,
                            tool_success=success,
                            raw_line=raw,
                        )
                    ]
                elif item.get("type") == "error":
                    msg = str(item.get("message") or "")
                    # Suppress non-fatal config deprecation warnings
                    if "is ignored" in msg or "deprecated" in msg:
                        return []
                    return [HostEvent("text", content=f"\n⚠️ {msg}\n", raw_line=raw)]

            # 2. Tool command start
            if ev_type == "item.started":
                item = ev.get("item") or {}
                if item.get("type") == "command_execution":
                    cmd_id = str(item.get("id") or "cmd")
                    command_str = str(item.get("command") or "")
                    return [
                        HostEvent(
                            "tool_start",
                            tool_id=cmd_id,
                            tool_name="bash",
                            tool_args={"command": command_str},
                            raw_line=raw,
                        )
                    ]

            # 3. Token usage
            if ev_type == "turn.completed":
                usage = ev.get("usage")
                if isinstance(usage, dict):
                    prompt_toks = int(usage.get("input_tokens") or 0)
                    compl_toks = int(usage.get("output_tokens") or 0)
                    cached_toks = int(
                        usage.get("cached_tokens")
                        or usage.get("cache_read_input_tokens")
                        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                        or 0
                    )
                    total_toks = int(usage.get("total_tokens") or (prompt_toks + compl_toks))
                    state["usage"] = {
                        "prompt": prompt_toks,
                        "completion": compl_toks,
                        "cached": cached_toks,
                        "total": total_toks,
                    }
                    return [HostEvent("token_usage", usage=state["usage"], raw_line=raw)]

            # Other structured JSON events (thread.started, turn.started) are internal
            return []

        # Lines not starting with '{' are banners, warnings, or pipe messages -> filter out from UI
        return []

    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        # Collect all assistant message text chunks
        text_parts = [e.content for e in events if e.event_type == "text" and e.content]
        if text_parts:
            return "".join(text_parts).strip()

        # If no agent message was emitted (e.g. review mode or raw fallback)
        filtered_lines: list[str] = []
        for line in raw_lines:
            s = line.strip()
            if not s or s.startswith(("{", "OpenAI Codex", "workdir:", "model:", "provider:", "approval:", "sandbox:", "reasoning", "session id:", "--------", "user", "tokens used", "warning:")):
                continue
            if s.startswith("<CONVERSATION_HISTORY") or s.startswith("请基于以上历史"):
                continue
            filtered_lines.append(line)

        cleaned = "".join(filtered_lines).strip()
        return cleaned if cleaned else "✅ Codex 任务执行完成。"
