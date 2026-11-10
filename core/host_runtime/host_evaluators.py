#!/usr/bin/env python3
"""Provider candidate evaluation logic for Cline, ZCode, OpenCode, and Pi hosts."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .free_models import (
    FreeModelCandidate,
    ProviderCandidate,
    ProviderTier,
    evaluate_and_select_free_model,
    is_cline_pass_active,
    is_free_model,
    is_subscription_token,
    score_free_model_capability,
)

logger = logging.getLogger(__name__)

OFFICIAL_PROVIDER_NAMES = frozenset({
    "openai",
    "openai-native",
    "openai-codex",
    "anthropic",
    "google",
    "gemini",
    "github-copilot",
    "copilot",
    "deepseek",
    "bigmodel",
    "zai",
    "z.ai",
    "qwen",
    "kimi",
    "moonshot",
    "minimax",
})

OFFICIAL_DOMAINS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.deepseek.com",
    "open.bigmodel.cn",
    "api.bigmodel.cn",
    "api.z.ai",
    "dashscope.aliyuncs.com",
    "api.moonshot.cn",
    "api.kimi.com",
    "api.minimax.chat",
)

DEFAULT_MODELS_BY_PROVIDER = {
    "openai-codex": "gpt-4o",
    "openai-native": "gpt-4o",
    "openai": "gpt-4o",
    "anthropic": "claude-3-5-sonnet-20241022",
    "google": "gemini-1.5-pro",
    "gemini": "gemini-1.5-pro",
    "deepseek": "deepseek-chat",
    "bigmodel": "GLM-5.3",
    "zai": "glm-4.7",
    "cline": "claude-3-5-sonnet-20241022",
    "9router": "9dev",
    "ocsg": "deepseek-v4.1-flash-1017",
}

def evaluate_cline_provider(
    cls,
    name: str,
    info: Mapping[str, Any],
    models_data: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
    data_dir: Path | None = None,
    *,
    preferred_free_model: str | None = None,
) -> ProviderCandidate:
    """评估 Cline 单个 Provider 候选条目并评定梯队。"""
    now = now_ts if now_ts is not None else time.time()
    st = info.get("settings", {}) if isinstance(info, Mapping) else {}
    auth = st.get("auth") if isinstance(st, Mapping) else None
    api_key = str(st.get("apiKey") or "").strip()
    base_url = str(st.get("baseUrl") or "").strip()
    configured_model = st.get("model") or None

    # 尝试从 models_data 中寻找明确配置的默认模型
    if not configured_model and models_data and isinstance(models_data, Mapping):
        prov_m = models_data.get("providers", {}).get(name, {})
        def_m = prov_m.get("provider", {}).get("defaultModelId")
        if def_m:
            configured_model = str(def_m).strip()

    model = configured_model or DEFAULT_MODELS_BY_PROVIDER.get(name)

    # 0. 凭据真实性前置校验 (《红队对抗宪法》：排除虚假凭据与未认证状态，严禁仅凭模型名放行)
    if name == "cline":
        has_cline_pass = is_cline_pass_active(data_dir=data_dir, auth=auth)
        has_cline_auth = bool(
            auth and isinstance(auth, Mapping) and (
                str(auth.get("accessToken") or "").strip() or str(auth.get("refreshToken") or "").strip()
            )
        )
        has_cline_key = bool(api_key)
        if not has_cline_pass and not has_cline_auth and not has_cline_key:
            return ProviderCandidate(
                provider_id=name,
                tier=ProviderTier.TIER_9_UNAVAILABLE,
                model=model,
                is_valid=False,
                reason="Cline 配置缺少登录凭据 (auth/token/pass)",
            )
        if has_cline_auth and not has_cline_pass and not has_cline_key:
            expires_at_ms = auth.get("expiresAt") or 0
            expires_at = (expires_at_ms / 1000.0) if expires_at_ms > 1e11 else float(expires_at_ms or 0)
            refresh_token = str(auth.get("refreshToken") or "").strip() or None
            if not refresh_token and expires_at > 0 and (now - expires_at) > 300:
                account_id = auth.get("accountId")
                return ProviderCandidate(
                    provider_id=name,
                    tier=ProviderTier.TIER_9_UNAVAILABLE,
                    model=model,
                    account_id=account_id,
                    is_valid=False,
                    reason=f"OAuth Token 已过期且无 RefreshToken (expired_at={expires_at})",
                )
    else:
        has_oauth = bool(
            auth and isinstance(auth, Mapping) and (
                str(auth.get("accessToken") or "").strip() or str(auth.get("refreshToken") or "").strip()
            )
        )
        if has_oauth and not api_key and not base_url:
            expires_at_ms = auth.get("expiresAt") or 0
            expires_at = (expires_at_ms / 1000.0) if expires_at_ms > 1e11 else float(expires_at_ms or 0)
            refresh_token = str(auth.get("refreshToken") or "").strip() or None
            if not refresh_token and expires_at > 0 and (now - expires_at) > 300:
                account_id = auth.get("accountId")
                return ProviderCandidate(
                    provider_id=name,
                    tier=ProviderTier.TIER_9_UNAVAILABLE,
                    model=model,
                    account_id=account_id,
                    is_valid=False,
                    reason=f"OAuth Token 已过期且无 RefreshToken (expired_at={expires_at})",
                )
        has_creds = bool(api_key or base_url or has_oauth)
        if not has_creds:
            return ProviderCandidate(
                provider_id=name,
                tier=ProviderTier.TIER_9_UNAVAILABLE,
                model=model,
                is_valid=False,
                reason=f"第三方 Provider [{name}] 缺少有效凭据 (API Key/BaseUrl/Auth)",
            )

    # 1. 优先判定：是否为免额度/免点数的真正免费模型（Tier 1）
    # 无论是 cline、opencode 还是第三方通道，只要存在可用免费模型或明确配置了免费模型，均作为最高优 Tier 1
    prov_free_candidates: list[FreeModelCandidate] = []
    if models_data and isinstance(models_data, Mapping):
        p_models = models_data.get("providers", {}).get(name, {}).get("models", {})
        if isinstance(p_models, Mapping):
            for m_id, m_info in p_models.items():
                m_str = str(m_id).strip()
                if is_free_model(m_str):
                    score, cat, scale, reason, reasoning = score_free_model_capability(m_str)
                    m_name = m_str
                    supports_reason = reasoning
                    if isinstance(m_info, Mapping):
                        m_name = str(m_info.get("name") or m_str)
                        if "supportsReasoning" in m_info:
                            supports_reason = bool(m_info["supportsReasoning"])
                    prov_free_candidates.append(FreeModelCandidate(
                        model_id=m_str,
                        provider_id=name,
                        name=m_name,
                        category=cat,
                        capability_score=score,
                        parameter_scale=scale,
                        reason=reason,
                        source="models_catalog",
                        supports_reasoning=supports_reason,
                    ))

    if configured_model and is_free_model(configured_model, base_url):
        m_conf = str(configured_model).strip()
        if not any(c.model_id == m_conf for c in prov_free_candidates):
            score, cat, scale, reason, reasoning = score_free_model_capability(m_conf)
            prov_free_candidates.append(FreeModelCandidate(
                model_id=m_conf,
                provider_id=name,
                name=m_conf,
                category=cat,
                capability_score=score,
                parameter_scale=scale,
                reason=reason,
                source="configured",
                supports_reasoning=reasoning,
            ))

    if prov_free_candidates:
        prov_free_candidates.sort(key=lambda c: c.rank_tuple)
        best_free, manifest = evaluate_and_select_free_model(
            prov_free_candidates,
            preferred_model=preferred_free_model,
        )
        if best_free:
            return ProviderCandidate(
                provider_id=name,
                tier=ProviderTier.TIER_1_FREE,
                model=best_free.model_id,
                is_valid=True,
                reason=f"免额度/免点数免费大模型 [{best_free.category}] ({best_free.model_id}, 评分: {best_free.capability_score})",
                metadata={
                    "free_model_candidate": best_free,
                    "free_models_manifest": manifest,
                    "capability_score": best_free.capability_score,
                    "parameter_scale": best_free.parameter_scale,
                    "category": best_free.category,
                },
            )

    # 1. 检查宿主官方原生订阅 (ClinePass)
    if name == "cline":
        if is_cline_pass_active(data_dir=data_dir, auth=auth):
            return ProviderCandidate(
                provider_id=name,
                tier=ProviderTier.TIER_2_NATIVE_SUB,
                model=model,
                is_valid=True,
                plan_type="clinepass",
                reason="Cline 官方原生订阅通道 (ClinePass)",
            )

    # 2. 检查 OAuth 订阅认证 (如 openai-codex / copilot)
    if auth and isinstance(auth, Mapping):
        access_token = str(auth.get("accessToken") or "").strip() or None
        refresh_token = str(auth.get("refreshToken") or "").strip() or None
        expires_at_ms = auth.get("expiresAt") or 0
        expires_at = (expires_at_ms / 1000.0) if expires_at_ms > 1e11 else float(expires_at_ms or 0)
        account_id = auth.get("accountId")

        is_sub, plan = is_subscription_token(str(access_token or ""))
        # 如果是已知订阅通道（如 openai-codex / copilot）即使未解析出 payload 也归为订阅
        if not is_sub and name in {"openai-codex", "github-copilot", "copilot"}:
            is_sub = True
            plan = plan or "subscription"

        # 必须至少有 accessToken 或 refreshToken
        if not access_token and not refresh_token:
            return ProviderCandidate(
                provider_id=name,
                tier=ProviderTier.TIER_9_UNAVAILABLE,
                model=model,
                account_id=account_id,
                plan_type=plan,
                is_valid=False,
                reason="OAuth 配置缺少 accessToken 和 refreshToken",
            )

        # 若具备 refreshToken，Cline Hub/CLI 启动时会自动静默刷新；
        # 仅在无 refreshToken 且 accessToken 已过期超过 300s 时判定为过期失效
        is_expired = bool(not refresh_token and expires_at > 0 and (now - expires_at) > 300)
        if is_expired:
            return ProviderCandidate(
                provider_id=name,
                tier=ProviderTier.TIER_9_UNAVAILABLE,
                model=model,
                account_id=account_id,
                plan_type=plan,
                is_valid=False,
                reason=f"OAuth Token 已过期且无 RefreshToken (expired_at={expires_at})",
            )

        if is_sub:
            tier = ProviderTier.TIER_2_NATIVE_SUB if name == "cline" else ProviderTier.TIER_2_THIRD_PARTY_SUB
            return ProviderCandidate(
                provider_id=name,
                tier=tier,
                model=model,
                account_id=account_id,
                plan_type=plan,
                is_valid=True,
                reason=f"有效官方订阅通道 ({plan or 'subscription'})",
            )

    # 3. 检查直连或第三方 API Key 或 BaseUrl
    if api_key or base_url:
        # 官方直接通道判定
        is_official = name in OFFICIAL_PROVIDER_NAMES and not base_url and bool(api_key)
        if is_official:
            return ProviderCandidate(
                provider_id=name,
                tier=ProviderTier.TIER_3_OFFICIAL_KEY,
                model=model,
                is_valid=True,
                reason="官方原生直连 API Key",
            )
        # 第三方中转路由通道
        return ProviderCandidate(
            provider_id=name,
            tier=ProviderTier.TIER_4_THIRD_PARTY,
            model=model,
            is_valid=True,
            reason=f"第三方中转代理通道 ({base_url or name})",
        )

    # 4. 内置按量点数代扣通道 (cline 未订阅 Pass)
    if name == "cline":
        return ProviderCandidate(
            provider_id=name,
            tier=ProviderTier.TIER_5_CREDITS,
            model=model,
            is_valid=True,
            reason="Cline 官方预付费按量代扣通道",
        )

    # 5. 无有效凭据条目
    return ProviderCandidate(
        provider_id=name,
        tier=ProviderTier.TIER_9_UNAVAILABLE,
        model=model,
        is_valid=False,
        reason="缺少有效凭据或配置不完整",
    )


def evaluate_zcode_candidates(
    cls,
    v2_dir: Path | None = None,
    *,
    preferred_domain: str | None = None,
    preferred_model: str | None = None,
) -> list[ProviderCandidate]:
    """全量探查 ZCode 宿主环境中的候选通道（官方订阅、凭据、自定义规则）。"""
    v2 = v2_dir or (Path.home() / ".zcode" / "v2")
    candidates: list[ProviderCandidate] = []
    if not v2.is_dir():
        return candidates

    # 1. 探查 coding-plan-cache.json 中的官方订阅套餐 (Tier 2)
    cache_file = v2 / "coding-plan-cache.json"
    if cache_file.is_file():
        try:
            cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
            items = cache_data.get("entryStatus", {}).get("items", {})
            for plan_key, plan_val in items.items():
                status = plan_val.get("status")
                reason = plan_val.get("reason", "")

                raw_domain = plan_key
                if plan_key.startswith("builtin:"):
                    raw_domain = plan_key[len("builtin:"):]
                domain = raw_domain.split("-coding-plan")[0].split("-start-plan")[0]

                rec_model = DEFAULT_MODELS_BY_PROVIDER.get(
                    domain, "GLM-5.3" if ("bigmodel" in domain or "glm" in domain) else "default"
                )
                score = 94 if ("bigmodel" in domain or "glm" in domain) else 90

                if status == "available":
                    candidates.append(ProviderCandidate(
                        provider_id=domain,
                        tier=ProviderTier.TIER_2_NATIVE_SUB,
                        model=rec_model,
                        plan_type=plan_key,
                        is_valid=True,
                        reason=f"官方已认证有效订阅套餐 [{plan_key}]",
                        capability_score=score,
                        source="coding-plan-cache",
                        host="zcode",
                        metadata={
                            "plan_key": plan_key,
                            "domain": domain,
                            "status": status,
                        },
                    ))
                else:
                    candidates.append(ProviderCandidate(
                        provider_id=domain,
                        tier=ProviderTier.TIER_9_UNAVAILABLE,
                        model=rec_model,
                        plan_type=plan_key,
                        is_valid=False,
                        reason=f"官方套餐不可用 ({reason or 'unavailable'})",
                        capability_score=50,
                        source="coding-plan-cache",
                        host="zcode",
                        metadata={
                            "plan_key": plan_key,
                            "domain": domain,
                            "status": status,
                        },
                    ))
        except Exception:
            pass

    # 2. 探查 credentials.json 中的活跃凭据
    creds_file = v2 / "credentials.json"
    if creds_file.is_file():
        try:
            creds = json.loads(creds_file.read_text(encoding="utf-8"))
            for cred_key in creds.keys():
                if "oauth:" in cred_key and ":access_token" in cred_key:
                    c_domain = cred_key.split("oauth:")[1].split(":access_token")[0]
                    matched = [c for c in candidates if c.provider_id == c_domain and c.is_valid]
                    if not matched:
                        rec_m = DEFAULT_MODELS_BY_PROVIDER.get(c_domain, "default")
                        candidates.append(ProviderCandidate(
                            provider_id=c_domain,
                            tier=ProviderTier.TIER_2_NATIVE_SUB,
                            model=rec_m,
                            is_valid=True,
                            reason=f"凭据库存在有效 OAuth 授权 [{cred_key}]",
                            capability_score=90,
                            source="credentials",
                            host="zcode",
                            metadata={"domain": c_domain},
                        ))
        except Exception:
            pass

    # 3. 探查 provider_config.json 中的自定义 Provider 规则
    prov_file = v2 / "provider_config.json"
    if prov_file.is_file():
        try:
            p_data = json.loads(prov_file.read_text(encoding="utf-8"))
            rules = p_data.get("config", {}).get("providerConfigRules", {}).get("providerRules", [])
            for rule in rules:
                p_id = rule.get("providerId") or "unknown"
                p_name = rule.get("providerName") or p_id
                cfg = rule.get("config", {})
                access = cfg.get("access", {})
                api = cfg.get("api", {})
                api_key = str(access.get("apiKey") or "").strip()
                base_url = str(api.get("baseUrl") or "").strip()
                models = cfg.get("modelOrder") or cfg.get("personalModelIds") or []
                first_m = models[0] if models else None

                if not api_key:
                    candidates.append(ProviderCandidate(
                        provider_id=p_id,
                        tier=ProviderTier.TIER_9_UNAVAILABLE,
                        model=first_m,
                        is_valid=False,
                        reason=f"自定义 Provider [{p_name}] 缺少 API Key",
                        capability_score=40,
                        source="provider_config",
                        host="zcode",
                        metadata={"provider_name": p_name, "rule": rule},
                    ))
                    continue

                # 检查是否有免费模型 (Tier 1)
                has_free = any(is_free_model(str(m), base_url) for m in models)
                if has_free:
                    free_m = next(m for m in models if is_free_model(str(m), base_url))
                    score, cat, scale, r_text, _ = score_free_model_capability(str(free_m))
                    candidates.append(ProviderCandidate(
                        provider_id=p_id,
                        tier=ProviderTier.TIER_1_FREE,
                        model=free_m,
                        is_valid=True,
                        reason=f"自定义免费模型 [{p_name}] ({free_m}, {scale})",
                        capability_score=score,
                        source="provider_config",
                        host="zcode",
                        metadata={"provider_name": p_name, "category": cat, "rule": rule},
                    ))
                    continue

                # 检查是否为官方直连 (Tier 3)
                is_official = any(dom in base_url.lower() for dom in OFFICIAL_DOMAINS)
                if is_official and not ("proxy" in base_url.lower() or "router" in p_name.lower()):
                    candidates.append(ProviderCandidate(
                        provider_id=p_id,
                        tier=ProviderTier.TIER_3_OFFICIAL_KEY,
                        model=first_m,
                        is_valid=True,
                        reason=f"自定义官方原生直连 Key [{p_name}] ({base_url})",
                        capability_score=80,
                        source="provider_config",
                        host="zcode",
                        metadata={"provider_name": p_name, "rule": rule},
                    ))
                else:
                    candidates.append(ProviderCandidate(
                        provider_id=p_id,
                        tier=ProviderTier.TIER_4_THIRD_PARTY,
                        model=first_m,
                        is_valid=True,
                        reason=f"自定义第三方代理/Router [{p_name}] ({base_url or 'proxy'})",
                        capability_score=70,
                        source="provider_config",
                        host="zcode",
                        metadata={"provider_name": p_name, "rule": rule},
                    ))
        except Exception:
            pass

    candidates.sort(key=lambda c: c.rank_tuple)
    return candidates


def evaluate_opencode_candidates(
    cls,
    config_dir_or_file: Path | None = None,
    *,
    preferred_provider: str | None = None,
    preferred_model: str | None = None,
) -> list[ProviderCandidate]:
    """全量探查 OpenCode 宿主环境中的候选通道。"""
    candidates: list[ProviderCandidate] = []
    target_file: Path | None = None
    if config_dir_or_file:
        target_file = config_dir_or_file if config_dir_or_file.is_file() else (config_dir_or_file / "opencode.json")
    else:
        cand_paths = [
            Path.cwd() / "opencode.json",
            Path.home() / ".config" / "opencode" / "opencode.json",
        ]
        for cp in cand_paths:
            if cp.is_file():
                target_file = cp
                break

    if not target_file or not target_file.is_file():
        return candidates

    try:
        data = json.loads(target_file.read_text(encoding="utf-8"))
    except Exception:
        return candidates

    prov_dict = data.get("provider", {}) if isinstance(data, dict) else {}
    for p_name, p_info in prov_dict.items():
        if not isinstance(p_info, dict):
            continue
        opts = p_info.get("options", {}) if isinstance(p_info.get("options"), dict) else {}
        api_key = str(opts.get("apiKey") or p_info.get("apiKey") or p_info.get("api_key") or "").strip()
        base_url = str(opts.get("baseURL") or opts.get("baseUrl") or p_info.get("baseURL") or p_info.get("baseUrl") or "").strip()
        models_dict = p_info.get("models", {})
        model_names = list(models_dict.keys()) if isinstance(models_dict, dict) else (p_info.get("models") or [])

        # 环境变量兜底
        if not api_key:
            api_key = os.environ.get(f"{p_name.upper()}_API_KEY", "").strip() or os.environ.get("OPENCODE_API_KEY", "").strip()

        first_m = model_names[0] if model_names else DEFAULT_MODELS_BY_PROVIDER.get(p_name, "default")

        if not api_key and not base_url:
            candidates.append(ProviderCandidate(
                provider_id=p_name,
                tier=ProviderTier.TIER_9_UNAVAILABLE,
                model=first_m,
                is_valid=False,
                reason=f"OpenCode 通道 [{p_name}] 缺少凭据 (API Key/BaseURL)",
                capability_score=40,
                source="opencode_config",
                host="opencode",
            ))
            continue

        # 免费模型 (Tier 1)
        has_free = any(is_free_model(str(m), base_url) for m in model_names)
        if has_free:
            free_m = next(m for m in model_names if is_free_model(str(m), base_url))
            score, cat, scale, r_text, _ = score_free_model_capability(str(free_m))
            candidates.append(ProviderCandidate(
                provider_id=p_name,
                tier=ProviderTier.TIER_1_FREE,
                model=free_m,
                is_valid=True,
                reason=f"OpenCode 免费模型 [{p_name}] ({free_m})",
                capability_score=score,
                source="opencode_config",
                host="opencode",
            ))
            continue

        # 官方直连 (Tier 3)
        is_official = p_name in OFFICIAL_PROVIDER_NAMES or any(dom in base_url.lower() for dom in OFFICIAL_DOMAINS)
        if is_official and not ("router" in p_name.lower() or "proxy" in base_url.lower()):
            score, cat, scale, r_text, _ = score_free_model_capability(str(first_m))
            candidates.append(ProviderCandidate(
                provider_id=p_name,
                tier=ProviderTier.TIER_3_OFFICIAL_KEY,
                model=first_m,
                is_valid=True,
                reason=f"OpenCode 官方直连通道 [{p_name}]",
                capability_score=max(score, 80),
                source="opencode_config",
                host="opencode",
            ))
        else:
            score, cat, scale, r_text, _ = score_free_model_capability(str(first_m))
            candidates.append(ProviderCandidate(
                provider_id=p_name,
                tier=ProviderTier.TIER_4_THIRD_PARTY,
                model=first_m,
                is_valid=True,
                reason=f"OpenCode 第三方代理通道 [{p_name}] ({base_url or 'proxy'})",
                capability_score=max(score, 70),
                source="opencode_config",
                host="opencode",
            ))

    candidates.sort(key=lambda c: c.rank_tuple)
    return candidates


def evaluate_pi_candidates(
    cls,
    agent_dir: Path | None = None,
    *,
    preferred_provider: str | None = None,
    preferred_model: str | None = None,
) -> list[ProviderCandidate]:
    """全量探查 Pi 宿主环境中的候选通道。"""
    candidates: list[ProviderCandidate] = []
    pi_dir = agent_dir or (Path.home() / ".pi" / "agent")
    if not pi_dir.is_dir():
        return candidates

    auth_file = pi_dir / "auth.json"
    models_file = pi_dir / "models.json"

    auth_data = {}
    if auth_file.is_file():
        try:
            auth_data = json.loads(auth_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    models_data = {}
    if models_file.is_file():
        try:
            models_data = json.loads(models_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    prov_map = models_data.get("providers", {}) if isinstance(models_data, dict) else {}
    all_prov_names = set(prov_map.keys()) | set(auth_data.keys())

    for p_name in all_prov_names:
        p_conf = prov_map.get(p_name, {})
        p_auth = auth_data.get(p_name, {})
        api_key = str(p_auth.get("key") or p_conf.get("apiKey") or "").strip()
        base_url = str(p_conf.get("baseUrl") or "").strip()

        if not api_key:
            api_key = os.environ.get(f"{p_name.upper()}_API_KEY", "").strip() or os.environ.get("PI_API_KEY", "").strip()

        models_list = p_conf.get("models", [])
        model_ids = [m.get("id") for m in models_list if isinstance(m, dict) and m.get("id")]
        first_m = model_ids[0] if model_ids else DEFAULT_MODELS_BY_PROVIDER.get(p_name, "default")

        if not api_key and not base_url:
            candidates.append(ProviderCandidate(
                provider_id=p_name,
                tier=ProviderTier.TIER_9_UNAVAILABLE,
                model=first_m,
                is_valid=False,
                reason=f"Pi 通道 [{p_name}] 缺少凭据 (apiKey/auth)",
                capability_score=40,
                source="pi_config",
                host="pi",
            ))
            continue

        has_free = any(is_free_model(str(m), base_url) for m in model_ids)
        if has_free:
            free_m = next(m for m in model_ids if is_free_model(str(m), base_url))
            score, cat, scale, r_text, _ = score_free_model_capability(str(free_m))
            candidates.append(ProviderCandidate(
                provider_id=p_name,
                tier=ProviderTier.TIER_1_FREE,
                model=free_m,
                is_valid=True,
                reason=f"Pi 免费模型 [{p_name}] ({free_m})",
                capability_score=score,
                source="pi_config",
                host="pi",
            ))
            continue

        is_official = p_name in OFFICIAL_PROVIDER_NAMES or any(dom in base_url.lower() for dom in OFFICIAL_DOMAINS)
        if is_official and not ("router" in p_name.lower() or "proxy" in base_url.lower()):
            score, cat, scale, r_text, _ = score_free_model_capability(str(first_m))
            candidates.append(ProviderCandidate(
                provider_id=p_name,
                tier=ProviderTier.TIER_3_OFFICIAL_KEY,
                model=first_m,
                is_valid=True,
                reason=f"Pi 官方原生直连 Key [{p_name}]",
                capability_score=max(score, 80),
                source="pi_config",
                host="pi",
            ))
        else:
            score, cat, scale, r_text, _ = score_free_model_capability(str(first_m))
            candidates.append(ProviderCandidate(
                provider_id=p_name,
                tier=ProviderTier.TIER_4_THIRD_PARTY,
                model=first_m,
                is_valid=True,
                reason=f"Pi 第三方代理/Router [{p_name}] ({base_url or 'proxy'})",
                capability_score=max(score, 70),
                source="pi_config",
                host="pi",
            ))

    candidates.sort(key=lambda c: c.rank_tuple)
    return candidates

