#!/usr/bin/env python3
"""core/host_provider_arbitrator.py - 宿主模型与通道动态阶梯裁决引擎 (DOC-243)。

负责多态宿主（Cline、OpenCode 等）在上游模型通道（Providers / Channels）的：
1. 凭据多态感知与活性探查 (HostProviderLivenessProbe)；
2. 五级通道动态阶梯裁决 (TieredProviderRankingEngine)；
3. 运行时阻断就地降级自愈 (ProviderFallbackSelfHealer)。

阶梯裁决优先序列（用户未显式指定时）：
- Tier 1: 真·免费通道 (Free Tier) - 零成本免充值通道
- Tier 2: 官方已认证订阅通道 (Official Subscription) - 如 ChatGPT Plus OAuth, Copilot
- Tier 3: 官方原生直接 API Key (Official Direct Key) - 如 OpenAI/Anthropic/Google 直连
- Tier 4: 第三方代理与中转路由 (Third-Party Router) - 如 9router, OpenRouter
- Tier 5: 按量扣费点数代扣通道 (Usage-Billing / Credits) - 如 Cline Credits
"""

from __future__ import annotations

import base64
import enum
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ProviderTier",
    "ProviderCandidate",
    "FreeModelCategory",
    "FreeModelCandidate",
    "HostProviderArbitrator",
    "arbitrate_cline_provider",
    "self_heal_cline_provider",
    "arbitrate_zcode_provider",
    "self_heal_zcode_provider",
    "arbitrate_opencode_provider",
    "self_heal_opencode_provider",
    "arbitrate_pi_provider",
    "self_heal_pi_provider",
    "arbitrate_host_provider",
    "self_heal_host_provider",
    "format_provider_manifest",
    "is_subscription_token",
    "is_free_model",
    "is_cline_pass_active",
    "score_free_model_capability",
    "list_candidate_free_models",
    "format_free_models_manifest",
    "evaluate_and_select_free_model",
    "list_available_free_models",
    "KNOWN_FREE_FRONTIER_MODELS",
]


from .free_models import (
    KNOWN_FREE_FRONTIER_MODELS,
    FreeModelCandidate,
    FreeModelCategory,
    ProviderCandidate,
    ProviderTier,
    evaluate_and_select_free_model,
    format_free_models_manifest,
    format_provider_manifest,
    is_cline_pass_active,
    is_free_model,
    is_subscription_token,
    list_available_free_models,
    list_candidate_free_models,
    score_free_model_capability,
)
from .host_evaluators import (
    DEFAULT_MODELS_BY_PROVIDER,
    OFFICIAL_DOMAINS,
    OFFICIAL_PROVIDER_NAMES,
    evaluate_cline_provider,
    evaluate_opencode_candidates,
    evaluate_pi_candidates,
    evaluate_zcode_candidates,
)


class HostProviderArbitrator:
    """宿主模型与通道智能裁决引擎。"""

    OFFICIAL_PROVIDER_NAMES = OFFICIAL_PROVIDER_NAMES
    OFFICIAL_DOMAINS = OFFICIAL_DOMAINS
    DEFAULT_MODELS_BY_PROVIDER = DEFAULT_MODELS_BY_PROVIDER

    @classmethod
    def evaluate_cline_provider(cls, *args, **kwargs) -> ProviderCandidate:
        return evaluate_cline_provider(cls, *args, **kwargs)

    @classmethod
    def evaluate_zcode_candidates(cls, *args, **kwargs) -> list[ProviderCandidate]:
        return evaluate_zcode_candidates(cls, *args, **kwargs)

    @classmethod
    def evaluate_opencode_candidates(cls, *args, **kwargs) -> list[ProviderCandidate]:
        return evaluate_opencode_candidates(cls, *args, **kwargs)

    @classmethod
    def evaluate_pi_candidates(cls, *args, **kwargs) -> list[ProviderCandidate]:
        return evaluate_pi_candidates(cls, *args, **kwargs)

    @classmethod
    def rank_cline_providers(
        cls,
        providers_data: Mapping[str, Any],
        models_data: Mapping[str, Any] | None = None,
        *,
        preferred_free_model: str | None = None,
        excluded_providers: set[str] | None = None,
        data_dir: Path | None = None,
    ) -> list[ProviderCandidate]:
        """扫描并按阶梯优先级排序所有可用通道。"""
        prov_map = providers_data.get("providers", {}) if isinstance(providers_data, Mapping) else {}
        excluded = excluded_providers or set()
        candidates: list[ProviderCandidate] = []

        for p_name, p_info in prov_map.items():
            if p_name in excluded:
                continue
            cand = cls.evaluate_cline_provider(
                p_name,
                p_info,
                models_data,
                data_dir=data_dir,
                preferred_free_model=preferred_free_model,
            )
            if cand.is_valid and cand.tier != ProviderTier.TIER_9_UNAVAILABLE:
                candidates.append(cand)

        # 排序：tier 单调递增（数值越小越优），然后 score 递减
        candidates.sort(key=lambda c: c.rank_tuple)
        return candidates

    @classmethod
    def select_best_cline_provider(
        cls,
        providers_data: Mapping[str, Any],
        models_data: Mapping[str, Any] | None = None,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        preferred_free_model: str | None = None,
        excluded_providers: set[str] | None = None,
        data_dir: Path | None = None,
    ) -> tuple[str | None, str | None, ProviderCandidate | None]:
        """根据五级阶梯裁决规则选出最优通道与模型。

        返回 (effective_provider, effective_model, candidate)。
        """
        # 1. 显式指定：100% 绝对尊重
        req_p = str(requested_provider or "").strip() or None
        req_m = str(requested_model or "").strip() or None
        if req_p:
            model = req_m or cls.DEFAULT_MODELS_BY_PROVIDER.get(req_p)
            return req_p, model, None

        # 2. 未指定：扫描所有候选并按阶梯裁决（优先尊重 AI 决策偏好的免费大模型）
        free_pref = preferred_free_model or (req_m if (req_m and is_free_model(req_m)) else None)
        candidates = cls.rank_cline_providers(
            providers_data,
            models_data,
            preferred_free_model=free_pref,
            excluded_providers=excluded_providers,
            data_dir=data_dir,
        )
        if not candidates:
            return None, req_m, None

        best = candidates[0]
        final_model = req_m or best.model or cls.DEFAULT_MODELS_BY_PROVIDER.get(best.provider_id)
        return best.provider_id, final_model, best

    @classmethod
    def self_heal_cline_provider(
        cls,
        data_dir: Path | None = None,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        preferred_free_model: str | None = None,
    ) -> dict[str, Any]:
        """就地自愈 Cline 宿主配置（DOC-243 规范）。"""
        base_dir = data_dir or (Path.home() / ".cline" / "data")
        settings_dir = base_dir / "settings"
        prov_file = settings_dir / "providers.json"
        models_file = settings_dir / "models.json"
        if not prov_file.is_file():
            return {"healed": False, "reason": "cline_providers_file_not_found"}

        actions: list[str] = []
        best_prov = None
        best_model = None
        best_cand = None
        try:
            prov_data = json.loads(prov_file.read_text(encoding="utf-8"))
            models_data = json.loads(models_file.read_text(encoding="utf-8")) if models_file.is_file() else None

            best_prov, best_model, best_cand = cls.select_best_cline_provider(
                prov_data,
                models_data,
                requested_provider=requested_provider,
                requested_model=requested_model,
                preferred_free_model=preferred_free_model,
                data_dir=base_dir,
            )
            if not best_cand or not best_prov:
                return {"healed": False, "reason": "no_valid_cline_candidate"}

            changed = False
            if prov_data.get("lastUsedProvider") != best_prov:
                prov_data["lastUsedProvider"] = best_prov
                changed = True
                actions.append(f"updated_cline_lastUsedProvider_to_{best_prov}")

            if best_model and best_prov in prov_data.get("providers", {}):
                st_item = prov_data["providers"][best_prov].setdefault("settings", {})
                if isinstance(st_item, dict) and st_item.get("model") != best_model:
                    st_item["model"] = best_model
                    changed = True
                    actions.append(f"updated_cline_model_to_{best_model}")

            if changed:
                bak = prov_file.with_name(prov_file.name + ".bak")
                bak.write_bytes(prov_file.read_bytes())
                prov_file.write_text(json.dumps(prov_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                prov_file.chmod(0o600)
        except Exception as exc:
            actions.append(f"cline_heal_error: {exc}")

        return {
            "healed": bool(actions),
            "actions": actions,
            "active_provider": best_prov if best_cand else None,
            "recommended_model": best_model if best_cand else None,
            "candidate": best_cand.to_dict() if best_cand else None,
        }

    @classmethod
    def select_best_zcode_provider(
        cls,
        v2_dir: Path | None = None,
        *,
        requested_domain: str | None = None,
        requested_model: str | None = None,
        preferred_free_model: str | None = None,
    ) -> tuple[str | None, str | None, ProviderCandidate | None, str]:
        candidates = cls.evaluate_zcode_candidates(
            v2_dir=v2_dir,
            preferred_domain=requested_domain,
            preferred_model=requested_model,
        )
        if not candidates:
            return requested_domain, requested_model, None, "*(未发现 ZCode 候选通道)*"

        # 显式指定判定
        if requested_domain:
            for c in candidates:
                if c.provider_id.lower() == requested_domain.lower() and c.is_valid:
                    manifest = format_provider_manifest(candidates, host="zcode", selected=c)
                    return c.provider_id, requested_model or c.model, c, manifest

        valid_cands = [c for c in candidates if c.is_valid and c.tier != ProviderTier.TIER_9_UNAVAILABLE]
        if not valid_cands:
            manifest = format_provider_manifest(candidates, host="zcode", selected=candidates[0])
            return None, requested_model, None, manifest

        best = valid_cands[0]
        manifest = format_provider_manifest(candidates, host="zcode", selected=best)
        final_model = requested_model or best.model
        return best.provider_id, final_model, best, manifest

    @classmethod
    def self_heal_zcode_provider(
        cls,
        v2_dir: Path | None = None,
        *,
        requested_domain: str | None = None,
        requested_model: str | None = None,
    ) -> dict[str, Any]:
        """就地自愈 ZCode 宿主配置（DOC-243 规范，纯动态无硬编码）：
        
        1. 自动探查官方订阅套餐（Coding Plan / OAuth）与自定义 Provider 规则；
        2. 当检测到高梯队可用通道（Tier 1 免费 / Tier 2 官方订阅），但当前配置指向无效/低梯队通道时，
           自动就地纠偏 setting.json 与 provider_config.json；
        3. 自动将失效/缺少 API Key 的 Provider 从 providerOrder 首位挪开，防请求拦截；
        4. 写入前备份原配置，确保零破坏。
        """
        v2 = v2_dir or (Path.home() / ".zcode" / "v2")
        if not v2.is_dir():
            return {"healed": False, "reason": "v2_dir_not_found"}

        best_domain, best_model, best_cand, manifest = cls.select_best_zcode_provider(
            v2_dir=v2,
            requested_domain=requested_domain,
            requested_model=requested_model,
        )
        if not best_cand:
            return {"healed": False, "reason": "no_valid_provider_candidate", "manifest": manifest}

        actions: list[str] = []

        # 1. 检查并自愈 setting.json
        setting_file = v2 / "setting.json"
        if setting_file.is_file():
            try:
                s_data = json.loads(setting_file.read_text(encoding="utf-8"))
                changed = False

                if best_cand.tier in {ProviderTier.TIER_2_NATIVE_SUB, ProviderTier.TIER_2_THIRD_PARTY_SUB}:
                    target_domain = best_cand.provider_id
                    target_plan = best_cand.metadata.get("plan_key") or f"builtin:{target_domain}-coding-plan"
                    needed_key = f"coding-plan:{target_plan}"

                    keys_map = s_data.setdefault("modelProviderFamilySelectedKeys", {})
                    if keys_map.get(target_domain) != needed_key:
                        keys_map[target_domain] = needed_key
                        changed = True
                        actions.append(f"selected_{target_domain}_coding_plan_{target_plan}")

                    if s_data.get("providerFamilyDomain") != target_domain:
                        s_data["providerFamilyDomain"] = target_domain
                        changed = True
                        actions.append(f"set_provider_family_domain_to_{target_domain}")

                if changed:
                    bak_file = setting_file.with_name("setting.json.bak")
                    bak_file.write_bytes(setting_file.read_bytes())
                    setting_file.write_text(json.dumps(s_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except Exception as exc:
                actions.append(f"setting_heal_error: {exc}")

        # 2. 检查并自愈 provider_config.json
        prov_file = v2 / "provider_config.json"
        if prov_file.is_file():
            try:
                p_data = json.loads(prov_file.read_text(encoding="utf-8"))
                cfg = p_data.get("config", {})
                p_order = list(cfg.get("providerOrder", []))
                rules = {r.get("providerId"): r for r in cfg.get("providerConfigRules", {}).get("providerRules", [])}

                changed_order = False
                if p_order:
                    invalid_ids = set()
                    for pid, rule in rules.items():
                        key = str(rule.get("config", {}).get("access", {}).get("apiKey") or "").strip()
                        if not key:
                            invalid_ids.add(pid)

                    while p_order and p_order[0] in invalid_ids:
                        stale_id = p_order.pop(0)
                        p_order.append(stale_id)
                        changed_order = True
                        actions.append(f"deprioritized_stale_provider_{stale_id}_from_provider_order")

                if changed_order:
                    cfg["providerOrder"] = p_order
                    bak_prov = prov_file.with_name("provider_config.json.bak")
                    bak_prov.write_bytes(prov_file.read_bytes())
                    prov_file.write_text(json.dumps(p_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except Exception as exc:
                actions.append(f"prov_heal_error: {exc}")

        return {
            "healed": bool(actions),
            "actions": actions,
            "active_provider": best_domain,
            "recommended_model": best_model,
            "candidate": best_cand.to_dict(),
            "manifest": manifest,
        }

    @classmethod
    def select_best_opencode_provider(
        cls,
        config_dir_or_file: Path | None = None,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
    ) -> tuple[str | None, str | None, ProviderCandidate | None, str]:
        candidates = cls.evaluate_opencode_candidates(
            config_dir_or_file=config_dir_or_file,
            preferred_provider=requested_provider,
            preferred_model=requested_model,
        )
        if not candidates:
            return requested_provider, requested_model, None, "*(未发现 OpenCode 候选)*"

        if requested_provider:
            for c in candidates:
                if c.provider_id.lower() == requested_provider.lower() and c.is_valid:
                    manifest = format_provider_manifest(candidates, host="opencode", selected=c)
                    return c.provider_id, requested_model or c.model, c, manifest

        valid = [c for c in candidates if c.is_valid and c.tier != ProviderTier.TIER_9_UNAVAILABLE]
        if not valid:
            manifest = format_provider_manifest(candidates, host="opencode", selected=candidates[0])
            return None, requested_model, None, manifest

        best = valid[0]
        manifest = format_provider_manifest(candidates, host="opencode", selected=best)
        final_model = requested_model or best.model
        return best.provider_id, final_model, best, manifest

    @classmethod
    def self_heal_opencode_provider(
        cls,
        config_dir_or_file: Path | None = None,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
    ) -> dict[str, Any]:
        """就地自愈 OpenCode 宿主配置（DOC-243 规范）。"""
        candidates = cls.evaluate_opencode_candidates(config_dir_or_file)
        if not candidates:
            return {"healed": False, "reason": "no_opencode_candidates"}

        valid_cands = [c for c in candidates if c.is_valid and c.tier != ProviderTier.TIER_9_UNAVAILABLE]
        if not valid_cands:
            return {"healed": False, "reason": "no_valid_provider_candidate"}

        best = valid_cands[0]
        target_file = None
        if config_dir_or_file:
            target_file = config_dir_or_file if config_dir_or_file.is_file() else (config_dir_or_file / "opencode.json")
        else:
            for cp in [Path.cwd() / "opencode.json", Path.home() / ".config" / "opencode" / "opencode.json"]:
                if cp.is_file():
                    target_file = cp
                    break

        if not target_file or not target_file.is_file():
            return {"healed": False, "reason": "opencode_file_not_found"}

        actions: list[str] = []
        try:
            data = json.loads(target_file.read_text(encoding="utf-8"))
            curr_model = str(data.get("model") or "").strip()
            target_model_str = f"{best.provider_id}/{best.model}"

            curr_prov = curr_model.split("/")[0] if "/" in curr_model else curr_model
            curr_cand = next((c for c in candidates if c.provider_id == curr_prov), None)

            needs_heal = not curr_model or (curr_cand and not curr_cand.is_valid) or (curr_cand and curr_cand.tier > best.tier)
            if needs_heal and curr_model != target_model_str:
                bak = target_file.with_name(target_file.name + ".bak")
                bak.write_bytes(target_file.read_bytes())
                data["model"] = target_model_str
                target_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                actions.append(f"updated_opencode_model_to_{target_model_str}")
        except Exception as exc:
            actions.append(f"opencode_heal_error: {exc}")

        manifest = format_provider_manifest(candidates, host="opencode", selected=best)
        return {
            "healed": bool(actions),
            "actions": actions,
            "active_provider": best.provider_id,
            "recommended_model": best.model,
            "candidate": best.to_dict(),
            "manifest": manifest,
        }

    @classmethod
    def select_best_pi_provider(
        cls,
        agent_dir: Path | None = None,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
    ) -> tuple[str | None, str | None, ProviderCandidate | None, str]:
        candidates = cls.evaluate_pi_candidates(
            agent_dir=agent_dir,
            preferred_provider=requested_provider,
            preferred_model=requested_model,
        )
        if not candidates:
            return requested_provider, requested_model, None, "*(未发现 Pi 候选)*"

        if requested_provider:
            for c in candidates:
                if c.provider_id.lower() == requested_provider.lower() and c.is_valid:
                    manifest = format_provider_manifest(candidates, host="pi", selected=c)
                    return c.provider_id, requested_model or c.model, c, manifest

        valid = [c for c in candidates if c.is_valid and c.tier != ProviderTier.TIER_9_UNAVAILABLE]
        if not valid:
            manifest = format_provider_manifest(candidates, host="pi", selected=candidates[0])
            return None, requested_model, None, manifest

        best = valid[0]
        manifest = format_provider_manifest(candidates, host="pi", selected=best)
        final_model = requested_model or best.model
        return best.provider_id, final_model, best, manifest

    @classmethod
    def self_heal_pi_provider(
        cls,
        agent_dir: Path | None = None,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
    ) -> dict[str, Any]:
        """就地自愈 Pi 宿主配置（DOC-243 规范）。"""
        candidates = cls.evaluate_pi_candidates(agent_dir)
        if not candidates:
            return {"healed": False, "reason": "no_pi_candidates"}

        valid_cands = [c for c in candidates if c.is_valid and c.tier != ProviderTier.TIER_9_UNAVAILABLE]
        if not valid_cands:
            return {"healed": False, "reason": "no_valid_provider_candidate"}

        best = valid_cands[0]
        pi_dir = agent_dir or (Path.home() / ".pi" / "agent")
        settings_file = pi_dir / "settings.json"
        if not settings_file.is_file():
            return {"healed": False, "reason": "pi_settings_file_not_found"}

        actions: list[str] = []
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
            curr_prov = str(data.get("defaultProvider") or "").strip()
            curr_model = str(data.get("defaultModel") or "").strip()

            curr_cand = next((c for c in candidates if c.provider_id == curr_prov), None)
            needs_heal = not curr_prov or (curr_cand and not curr_cand.is_valid) or (curr_cand and curr_cand.tier > best.tier)
            if needs_heal:
                changed = False
                if curr_prov != best.provider_id:
                    data["defaultProvider"] = best.provider_id
                    changed = True
                    actions.append(f"updated_pi_default_provider_to_{best.provider_id}")
                if best.model and curr_model != best.model:
                    data["defaultModel"] = best.model
                    changed = True
                    actions.append(f"updated_pi_default_model_to_{best.model}")

                if changed:
                    bak = settings_file.with_name(settings_file.name + ".bak")
                    bak.write_bytes(settings_file.read_bytes())
                    settings_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception as exc:
            actions.append(f"pi_heal_error: {exc}")

        manifest = format_provider_manifest(candidates, host="pi", selected=best)
        return {
            "healed": bool(actions),
            "actions": actions,
            "active_provider": best.provider_id,
            "recommended_model": best.model,
            "candidate": best.to_dict(),
            "manifest": manifest,
        }

    @classmethod
    def arbitrate_host(
        cls,
        host_name: str,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        preferred_free_model: str | None = None,
        root: Path | None = None,
    ) -> tuple[str | None, str | None, ProviderCandidate | None, str]:
        """统一多宿主通道与模型智能裁决。"""
        h = (host_name or "").strip().lower()
        if h == "zcode":
            return cls.select_best_zcode_provider(
                requested_domain=requested_provider,
                requested_model=requested_model,
                preferred_free_model=preferred_free_model,
            )
        elif h == "opencode":
            return cls.select_best_opencode_provider(
                config_dir_or_file=root,
                requested_provider=requested_provider,
                requested_model=requested_model,
            )
        elif h == "pi":
            return cls.select_best_pi_provider(
                requested_provider=requested_provider,
                requested_model=requested_model,
            )
        elif h == "cline":
            cline_data = Path.home() / ".cline" / "data"
            prov_file = cline_data / "settings" / "providers.json"
            models_file = cline_data / "settings" / "models.json"
            prov_data = json.loads(prov_file.read_text(encoding="utf-8")) if prov_file.is_file() else {}
            models_data = json.loads(models_file.read_text(encoding="utf-8")) if models_file.is_file() else None
            p, m, cand = cls.select_best_cline_provider(
                prov_data,
                models_data,
                requested_provider=requested_provider,
                requested_model=requested_model,
                preferred_free_model=preferred_free_model,
                data_dir=cline_data,
            )
            cands = cls.rank_cline_providers(prov_data, models_data, data_dir=cline_data)
            manifest = format_provider_manifest(cands, host="cline", selected=cand)
            return p, m, cand, manifest
        else:
            return requested_provider, requested_model, None, f"*(宿主 [{host_name}] 暂无专有裁决配置)*"

    @classmethod
    def self_heal_host(
        cls,
        host_name: str,
        *,
        requested_provider: str | None = None,
        requested_model: str | None = None,
        root: Path | None = None,
    ) -> dict[str, Any]:
        """统一多宿主配置就地自愈分发。"""
        h = (host_name or "").strip().lower()
        if h == "zcode":
            return cls.self_heal_zcode_provider(
                requested_domain=requested_provider,
                requested_model=requested_model,
            )
        elif h == "opencode":
            return cls.self_heal_opencode_provider(
                config_dir_or_file=root,
                requested_provider=requested_provider,
                requested_model=requested_model,
            )
        elif h == "pi":
            return cls.self_heal_pi_provider(
                requested_provider=requested_provider,
                requested_model=requested_model,
            )
        elif h == "cline":
            return cls.self_heal_cline_provider(
                requested_provider=requested_provider,
                requested_model=requested_model,
            )
        return {"healed": False, "reason": f"unsupported_host_provider_heal: {host_name}"}


def arbitrate_cline_provider(
    providers_data: Mapping[str, Any],
    models_data: Mapping[str, Any] | None = None,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    preferred_free_model: str | None = None,
    excluded_providers: set[str] | None = None,
    data_dir: Path | None = None,
) -> tuple[str | None, str | None]:
    """便捷入口：返回 (provider, model)。"""
    p, m, _ = HostProviderArbitrator.select_best_cline_provider(
        providers_data,
        models_data,
        requested_provider=requested_provider,
        requested_model=requested_model,
        preferred_free_model=preferred_free_model,
        excluded_providers=excluded_providers,
        data_dir=data_dir,
    )
    return p, m


def self_heal_cline_provider(data_dir: Path | None = None) -> dict[str, Any]:
    """便捷入口：就地自愈 Cline 配置。"""
    return HostProviderArbitrator.self_heal_cline_provider(data_dir)


def arbitrate_zcode_provider(v2_dir: Path | None = None) -> dict[str, Any]:
    """诊断并裁决 ZCode 当前可用模型提供商（官方订阅套餐优先）。"""
    v2 = v2_dir or (Path.home() / ".zcode" / "v2")
    cands = HostProviderArbitrator.evaluate_zcode_candidates(v2)
    official_avail = [
        c.metadata.get("plan_key") for c in cands
        if c.is_valid and c.tier in {ProviderTier.TIER_2_NATIVE_SUB, ProviderTier.TIER_2_THIRD_PARTY_SUB} and c.metadata.get("plan_key")
    ]

    creds_file = v2 / "credentials.json"
    has_bigmodel_creds = False
    if creds_file.is_file():
        try:
            creds = json.loads(creds_file.read_text(encoding="utf-8"))
            has_bigmodel_creds = any("bigmodel" in k and "coding-plan" in k for k in creds.keys())
        except Exception:
            pass

    current_domain = None
    current_selected = None
    setting_file = v2 / "setting.json"
    if setting_file.is_file():
        try:
            s_data = json.loads(setting_file.read_text(encoding="utf-8"))
            current_domain = s_data.get("providerFamilyDomain")
            current_selected = s_data.get("modelProviderFamilySelectedKeys", {}).get(current_domain)
        except Exception:
            pass

    best_p, best_m, best_cand, manifest = HostProviderArbitrator.select_best_zcode_provider(v2)

    return {
        "official_available": official_avail,
        "has_bigmodel_creds": has_bigmodel_creds,
        "current_domain": current_domain,
        "current_selected": current_selected,
        "v2_dir": str(v2),
        "best_domain": best_p,
        "best_model": best_m,
        "best_candidate": best_cand.to_dict() if best_cand else None,
        "candidates": [c.to_dict() for c in cands],
        "manifest": manifest,
    }


def self_heal_zcode_provider(v2_dir: Path | None = None) -> dict[str, Any]:
    """就地自愈 ZCode 宿主配置（DOC-243 规范，纯动态无硬编码）。"""
    return HostProviderArbitrator.self_heal_zcode_provider(v2_dir)


def arbitrate_opencode_provider(config_dir_or_file: Path | None = None) -> tuple[str | None, str | None]:
    """便捷入口：返回 OpenCode 最优 (provider, model)。"""
    p, m, _, _ = HostProviderArbitrator.select_best_opencode_provider(config_dir_or_file)
    return p, m


def self_heal_opencode_provider(config_dir_or_file: Path | None = None) -> dict[str, Any]:
    """便捷入口：就地自愈 OpenCode 配置。"""
    return HostProviderArbitrator.self_heal_opencode_provider(config_dir_or_file)


def arbitrate_pi_provider(agent_dir: Path | None = None) -> tuple[str | None, str | None]:
    """便捷入口：返回 Pi 最优 (provider, model)。"""
    p, m, _, _ = HostProviderArbitrator.select_best_pi_provider(agent_dir)
    return p, m


def self_heal_pi_provider(agent_dir: Path | None = None) -> dict[str, Any]:
    """便捷入口：就地自愈 Pi 配置。"""
    return HostProviderArbitrator.self_heal_pi_provider(agent_dir)


def arbitrate_host_provider(
    host_name: str,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    preferred_free_model: str | None = None,
    root: Path | None = None,
) -> tuple[str | None, str | None, ProviderCandidate | None, str]:
    """统一多宿主通道与模型智能裁决入口。"""
    return HostProviderArbitrator.arbitrate_host(
        host_name,
        requested_provider=requested_provider,
        requested_model=requested_model,
        preferred_free_model=preferred_free_model,
        root=root,
    )


def self_heal_host_provider(
    host_name: str,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """统一多宿主配置就地自愈分发入口。"""
    return HostProviderArbitrator.self_heal_host(
        host_name,
        requested_provider=requested_provider,
        requested_model=requested_model,
        root=root,
    )
