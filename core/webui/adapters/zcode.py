#!/usr/bin/env python3
"""BugX WebUI Host Adapter Plugin - ZCode Assistant CLI."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .base import BaseHostAdapter, HostEvent


class ZCodeHostAdapter(BaseHostAdapter):
    """Adapter for ZCode Assistant CLI."""

    host_id = "zcode"
    display_name = "ZCode"
    vendor = "zhipu"
    default_model = ""
    supports_reasoning = False
    supported_effort_levels = []
    default_effort = ""
    supported_modes = ["build", "accept_edits", "bypass", "plan"]

    @staticmethod
    def _get_zcode_dir() -> Path:
        env_dir = os.environ.get("ZCODE_HOME")
        if env_dir:
            p = Path(env_dir).expanduser().resolve()
            if p.exists():
                return p
        return Path.home() / ".zcode"

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        return False, [], ""

    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        """Discover models directly from ZCode configuration files based on active package entitlement and user configuration."""
        zcode_dir = self._get_zcode_dir()
        config_file = zcode_dir / "v2" / "config.json"
        provider_config_file = zcode_dir / "v2" / "provider_config.json"
        setting_file = zcode_dir / "v2" / "setting.json"
        plan_cache_file = zcode_dir / "v2" / "coding-plan-cache.json"

        chosen_default = self.default_model

        # 1. Identify disabled model rules from provider_config.json
        disabled_models: set[str] = set()
        if provider_config_file.is_file():
            try:
                p_cfg = json.loads(provider_config_file.read_text(encoding="utf-8"))
                rules = p_cfg.get("config", {}).get("modelConfigRules", {}).get("providerModelRules", [])
                if isinstance(rules, list):
                    for r in rules:
                        if isinstance(r, dict) and r.get("config", {}).get("enabled") is False:
                            mid = str(r.get("modelId", "")).strip().lower()
                            if mid:
                                disabled_models.add(mid)
            except Exception:
                pass

        # 2. Identify available / entitled coding plans from coding-plan-cache.json
        available_plan_keys: set[str] = set()
        if plan_cache_file.is_file():
            try:
                c_plan = json.loads(plan_cache_file.read_text(encoding="utf-8"))
                items = c_plan.get("entryStatus", {}).get("items", {})
                if isinstance(items, dict):
                    for pkey, pstatus in items.items():
                        if isinstance(pstatus, dict) and pstatus.get("status") == "available":
                            available_plan_keys.add(pkey)
            except Exception:
                pass

        # 3. Identify active domain and selected key from setting.json
        active_domain = "bigmodel"
        active_selected_key = ""
        if setting_file.is_file():
            try:
                s_data = json.loads(setting_file.read_text(encoding="utf-8"))
                if isinstance(s_data, dict):
                    active_domain = s_data.get("providerFamilyDomain", "bigmodel")
                    sel_keys = s_data.get("modelProviderFamilySelectedKeys", {})
                    if isinstance(sel_keys, dict):
                        active_selected_key = str(sel_keys.get(active_domain) or "")
                    sel_model = s_data.get("selectedModel") or s_data.get("defaultModel")
                    if isinstance(sel_model, str) and sel_model.strip():
                        chosen_default = sel_model.strip()
            except Exception:
                pass

        def _resolve_vendor_key(pname: str, mid: str = "") -> str:
            p = pname.lower()
            m = mid.lower()
            if any(k in p for k in ("deepseek", "dpf")) or "deepseek" in m:
                return "deepseek"
            if "kimi" in p or "kimi" in m:
                return "kimi"
            if "grok" in p or "grok" in m:
                return "grok"
            if "openai" in p or m.startswith("gpt-"):
                return "openai"
            if any(k in p for k in ("商汤", "sensenova", "sense")) or "sensenova" in m:
                return "sensenova"
            if "9router" in p or "9dev" in m:
                return "9router"
            if any(k in p for k in ("bigmodel", "z.ai", "zhipu", "glm")) or "glm" in m:
                return "zhipu"
            return p.strip() or "custom"

        canonical_glm_models: list[dict[str, Any]] = []
        custom_models: list[dict[str, Any]] = []
        seen_lower_ids: set[str] = set()

        # 4. Discover authentic GLM models from config.json based on actual entitlement
        if config_file.is_file():
            try:
                c_data = json.loads(config_file.read_text(encoding="utf-8"))
                providers = c_data.get("provider", {}) if isinstance(c_data, dict) else {}
                if not providers and isinstance(c_data, dict) and "models" in c_data:
                    providers = {"builtin:bigmodel": {"models": c_data["models"]}}
                if not chosen_default and isinstance(c_data, dict):
                    plan_sel = c_data.get("codingPlan", {}).get("selectedModel") or c_data.get("selectedModel")
                    if isinstance(plan_sel, str) and plan_sel.strip():
                        chosen_default = plan_sel.strip()

                # Builtin providers: strictly verify entitlement and active domain
                for p_id, p_info in providers.items():
                    if not isinstance(p_info, dict):
                        continue
                    if not any(k in p_id.lower() for k in ("builtin:bigmodel", "builtin:zai")):
                        continue
                    if p_info.get("enabled") is False or p_info.get("systemDisabledReason"):
                        continue
                    if available_plan_keys and p_id not in available_plan_keys:
                        continue
                    if active_selected_key:
                        clean_key = active_selected_key.split(":")[-1]
                        if clean_key not in p_id and p_id not in active_selected_key:
                            continue

                    p_models = p_info.get("models", {})
                    if not isinstance(p_models, dict):
                        continue

                    sorted_m = sorted(
                        p_models.items(),
                        key=lambda item: item[1].get("zcode", {}).get("priority", 50) if isinstance(item[1], dict) else 0,
                        reverse=True,
                    )
                    for m_id, m_info in sorted_m:
                        if not isinstance(m_info, dict):
                            continue
                        m_id_str = str(m_id).strip()
                        if not m_id_str or m_id_str.lower() in seen_lower_ids or m_id_str.lower() in disabled_models:
                            continue
                        seen_lower_ids.add(m_id_str.lower())
                        z_info = m_info.get("zcode", {}) if isinstance(m_info.get("zcode"), dict) else {}
                        prio = z_info.get("priority", 50)
                        r_info = m_info.get("reasoning", {}) if isinstance(m_info.get("reasoning"), dict) else {}
                        has_r = bool(r_info.get("enabled", False))

                        badge = "当前配置" if prio == 100 else ("高精度" if prio == 99 else "GLM")
                        canonical_glm_models.append({
                            "id": m_id_str,
                            "label": m_id_str,
                            "vendor": "zhipu",
                            "badge": badge,
                            "reasoning": has_r,
                        })

                # Custom user providers from config.json
                for p_id, p_info in providers.items():
                    if not isinstance(p_info, dict):
                        continue
                    if any(k in p_id.lower() for k in ("builtin:bigmodel", "builtin:zai")):
                        continue
                    if p_info.get("enabled") is False or p_info.get("systemDisabledReason"):
                        continue
                    p_name = str(p_info.get("name") or p_id.split(":")[-1]).strip()
                    p_models = p_info.get("models", {})
                    if isinstance(p_models, dict):
                        for m_id, m_info in p_models.items():
                            if not isinstance(m_info, dict):
                                continue
                            m_id_str = str(m_id).strip()
                            if not m_id_str or "bigmodel" in m_id_str.lower():
                                continue
                            m_label = str(m_info.get("name") or m_id_str).strip()
                            eff_id = m_label if m_label and m_label != "None" and m_label != m_id_str else m_id_str
                            if eff_id.lower() in seen_lower_ids or eff_id.lower() in disabled_models:
                                continue
                            seen_lower_ids.add(eff_id.lower())
                            r_info = m_info.get("reasoning", {})
                            has_r = bool(r_info.get("enabled", False)) if isinstance(r_info, dict) else False
                            custom_models.append({
                                "id": eff_id,
                                "label": eff_id,
                                "vendor": _resolve_vendor_key(p_name, eff_id),
                                "badge": p_name,
                                "reasoning": has_r,
                            })
            except Exception:
                pass

        # 5. Dynamic custom models configured in ~/.zcode/v2/provider_config.json
        if provider_config_file.is_file():
            try:
                p_data = json.loads(provider_config_file.read_text(encoding="utf-8"))
                rules = p_data.get("config", {}).get("providerConfigRules", {}).get("providerRules", [])
                if isinstance(rules, list):
                    for r in rules:
                        if not isinstance(r, dict):
                            continue
                        p_name = str(r.get("providerName") or "custom").strip()
                        c = r.get("config", {}) if isinstance(r.get("config"), dict) else {}
                        m_ids = c.get("modelOrder") or c.get("personalModelIds") or []
                        if isinstance(m_ids, list):
                            for mid in m_ids:
                                mid_str = str(mid).strip()
                                if not mid_str or "bigmodel" in mid_str.lower():
                                    continue
                                if mid_str.lower() in seen_lower_ids or mid_str.lower() in disabled_models:
                                    continue
                                seen_lower_ids.add(mid_str.lower())
                                has_r = any(k in mid_str.lower() for k in ("flash", "pro", "r1", "reasoning", "think"))
                                custom_models.append({
                                    "id": mid_str,
                                    "label": mid_str,
                                    "vendor": _resolve_vendor_key(p_name, mid_str),
                                    "badge": p_name,
                                    "reasoning": has_r,
                                })
            except Exception:
                pass

        all_models = canonical_glm_models + custom_models
        if not all_models:
            return ("", [])

        # Ensure chosen default is valid
        if not any(m["id"].lower() == chosen_default.lower() for m in all_models):
            chosen_default = all_models[0]["id"] if all_models else ""

        # 6. Harmonize badges based on chosen_default
        for m in all_models:
            if m["id"].lower() == chosen_default.lower():
                m["badge"] = "当前配置"
            elif m["id"] == "GLM-5.3-Flash" and chosen_default != "GLM-5.3-Flash":
                m["badge"] = "极速"

        return (chosen_default, all_models)

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
        home = Path.home()
        bin_path = (
            shutil.which("zcode")
            or (home / ".local/bin/zcode" if (home / ".local/bin/zcode").is_file() else None)
            or (Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs") if Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs").is_file() else None)
            or (home / "Applications/ZCode.app/Contents/Resources/glm/zcode.cjs" if (home / "Applications/ZCode.app/Contents/Resources/glm/zcode.cjs").is_file() else None)
            or "/opt/homebrew/bin/zcode"
        )
        clean_msg = prompt.strip()

        mode_map = {
            "build": "build",
            "accept_edits": "edit",
            "bypass": "yolo",
            "plan": "plan",
            "chat": "yolo",
            "mine": "yolo",
        }
        zcode_mode = mode_map.get(mode, "yolo")

        env = os.environ.copy()
        eff_model = (model or "").strip()
        # Suppress fictional models and pass real model via environment if specified
        if eff_model and eff_model not in ("auto", "default", "zcode-default", "zcode-pro"):
            env["ZCODE_MODEL"] = eff_model

        # Build execution command with proper headless arguments (zcode -p <prompt> --mode <mode> --cwd <cwd>)
        if str(bin_path).endswith(".cjs"):
            node_bin = shutil.which("node") or "/usr/local/bin/node" or "/opt/homebrew/bin/node"
            cmd = [node_bin, str(bin_path), "-p", clean_msg, "--mode", zcode_mode, "--cwd", str(cwd)]
        else:
            cmd = [str(bin_path), "-p", clean_msg, "--mode", zcode_mode, "--cwd", str(cwd)]

        return cmd, env

    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        raw = line.strip()
        if not raw:
            return []
        return [HostEvent("text", content=line, raw_line=raw)]

    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        text_parts = [e.content for e in events if e.event_type == "text" and e.content]
        if text_parts:
            return "".join(text_parts).strip()
        return "".join(raw_lines).strip() or "✅ ZCode 任务执行完成。"
