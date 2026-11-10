#!/usr/bin/env python3
"""Free model candidates, capability scoring, and subscription inspection for host providers."""
from __future__ import annotations

import base64
import enum
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

class ProviderTier(enum.IntEnum):
    """通道阶梯优先级（数值越小，优先级越高）。

    五级阶梯精细化序列：
    - Tier 1: 真·免费通道/免费模型 (如 :free 模型, opencode/beam-free)
    - Tier 2A: 宿主官方原生订阅 (如 ClinePass)
    - Tier 2B: 第三方跨宿主订阅 (如 openai-codex / ChatGPT Plus, Copilot)
    - Tier 3: 官方原生直接 API Key (OpenAI/Anthropic 直连)
    - Tier 4: 第三方中转代理 Router (9router, OpenRouter 等)
    - Tier 5: 按量点数代扣通道 (Cline Credits 等，需 balance > 0)
    - Tier 9: 无效/已过期/缺少凭据
    """

    TIER_1_FREE = 1                # 零成本真免费通道/免额度免费模型
    TIER_2_NATIVE_SUB = 2          # 宿主官方原生订阅 (如 ClinePass)
    TIER_2_THIRD_PARTY_SUB = 3     # 第三方跨宿主订阅 (如 openai-codex / ChatGPT Plus, Copilot)
    TIER_3_OFFICIAL_KEY = 4        # 官方原生直连 API Key
    TIER_4_THIRD_PARTY = 5         # 第三方代理 / Router / 自建中转
    TIER_5_CREDITS = 6             # 预付费按量代扣点数通道 (Cline Credits 等)
    TIER_9_UNAVAILABLE = 9         # 无效 / 已过期 / 缺少凭据

    # 兼容历史别名
    TIER_2_SUBSCRIPTION = 2


class FreeModelCategory(str, enum.Enum):
    """免费模型梯队能力类别。"""

    FRONTIER_LARGE = "FRONTIER_LARGE"  # 头部超大/满血推理旗舰大模型 (671B MoE, 70B+, 32B Coder, Gemini 2.0 Flash)
    MID_SCALE = "MID_SCALE"            # 中坚规模大模型 (14B ~ 35B, 如 29B, 14B)
    SMALL_SCALE = "SMALL_SCALE"        # 小型/轻量玩具模型 (< 10B, 如 8B, 3B, 1B, Lite)


#: 已知常驻/权威推荐的开源免费头部大模型白名单池
KNOWN_FREE_FRONTIER_MODELS: Sequence[str] = (
    "Nemotron 3.5 Super VL (free)",
    "deepseek/deepseek-r1:free",
    "deepseek/deepseek-chat:free",
    "qwen/qwen-2.5-coder-32b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-thinking-exp:free",
    "google/gemini-2.0-flash-exp:free",
)


@dataclass
class FreeModelCandidate:
    """免费模型候选对象，记录参数规模、能力分级与推荐分析。"""

    model_id: str
    provider_id: str
    name: str = ""
    category: str = FreeModelCategory.MID_SCALE.value
    capability_score: int = 50
    parameter_scale: str = ""
    reason: str = ""
    source: str = "configured"  # configured, models_catalog, history_cache, frontier_pool, ai_decision
    supports_reasoning: bool = False
    supports_tools: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def rank_tuple(self) -> tuple[int, int, str]:
        """按能力评分倒序排序：能力最高者最优先。"""
        prov_pref = 0
        if self.provider_id in {"cline", "opencode"}:
            prov_pref = -1
        return (-self.capability_score, prov_pref, self.model_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "name": self.name,
            "category": self.category,
            "capability_score": self.capability_score,
            "parameter_scale": self.parameter_scale,
            "reason": self.reason,
            "source": self.source,
            "supports_reasoning": self.supports_reasoning,
            "supports_tools": self.supports_tools,
        }


@dataclass
class ProviderCandidate:
    """通道与模型候选对象（跨宿主通用定义）。"""

    provider_id: str
    tier: ProviderTier
    model: str | None = None
    account_id: str | None = None
    plan_type: str | None = None
    is_valid: bool = True
    reason: str = ""
    capability_score: int = 50
    source: str = ""
    host: str = "generic"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def rank_tuple(self) -> tuple[int, int, str]:
        """排序元组：(tier, -tier_score, provider_id)。"""
        if not self.is_valid or self.tier == ProviderTier.TIER_9_UNAVAILABLE:
            return (int(ProviderTier.TIER_9_UNAVAILABLE), 0, self.provider_id)

        score = 0
        if self.tier == ProviderTier.TIER_1_FREE:
            # 免费模型中，若为当前宿主原生提供者（如 cline），优先调度
            if self.provider_id in {"cline", "opencode"}:
                score += 25
            else:
                score += 15
            # 将模型的 capability_score 计入 tier_score，头部大模型获得显著优先排序加权
            cap_score = int(self.capability_score or self.metadata.get("capability_score", 0))
            score += cap_score * 10
        elif self.tier == ProviderTier.TIER_2_NATIVE_SUB:
            score += 20
        elif self.tier == ProviderTier.TIER_2_THIRD_PARTY_SUB:
            score += 10
        if self.plan_type in {"plus", "team", "pro", "enterprise", "pass", "coding-plan"}:
            score += 5
        if self.model and not self.model.startswith("unknown"):
            score += 2
        return (int(self.tier), -score, self.provider_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "tier": int(self.tier),
            "tier_name": self.tier.name,
            "model": self.model,
            "account_id": self.account_id,
            "plan_type": self.plan_type,
            "is_valid": self.is_valid,
            "reason": self.reason,
            "capability_score": self.capability_score,
            "source": self.source,
            "host": self.host,
            "metadata": self.metadata,
        }


def score_free_model_capability(
    model_id: str,
    description: str | None = None,
) -> tuple[int, str, str, str, bool]:
    """量化评估免费模型的能力分级与综合得分。

    返回 (score, category, parameter_scale, reason, supports_reasoning)。
    - score: 0 ~ 100
    - category: "FRONTIER_LARGE" | "MID_SCALE" | "SMALL_SCALE"
    - parameter_scale: 如 "671B MoE", "120B", "70B", "35B MoE", "32B", "14B", "8B", "3B", "1B"
    - reason: 推荐原因及特性说明
    - supports_reasoning: 是否具备原生思考/推理链
    """
    if not model_id or not isinstance(model_id, str):
        return 50, FreeModelCategory.MID_SCALE.value, "Unknown", "未指定模型标识", False

    m = model_id.strip().lower()
    combined = (m + " " + (description or "").strip().lower()).strip()

    # 1. 头部超大满血推理模型 (Frontier Reasoning Flagship, 96~100)
    if "r1" in m and ("deepseek" in m or m.startswith("r1") or "-r1" in m or "/r1" in m):
        return (
            100,
            FreeModelCategory.FRONTIER_LARGE.value,
            "671B MoE",
            "DeepSeek R1 头部开源满血推理大模型 (顶尖逻辑推理与代码生成)",
            True,
        )
    if "flash-thinking" in m or ("gemini" in m and "thinking" in m):
        return (
            96,
            FreeModelCategory.FRONTIER_LARGE.value,
            "Frontier",
            "Google Gemini 2.0 思考型头部推理大模型",
            True,
        )

    # 2. 头部代码专家大模型 (Frontier Coding Flagship, 95)
    if "qwen" in m and "coder" in m and any(k in m for k in ("32b", "72b", "max", "plus")):
        scale = "72B" if "72b" in m else "32B"
        return (
            95,
            FreeModelCategory.FRONTIER_LARGE.value,
            f"{scale} Dense",
            f"Qwen 2.5 Coder 头部开源代码大模型 (顶尖软件工程与代码能力)",
            False,
        )

    # 3. 头部全能旗舰大模型 (Frontier General Flagship, 94)
    if "deepseek" in m and any(k in m for k in ("v3", "chat", "v4")):
        return (
            94,
            FreeModelCategory.FRONTIER_LARGE.value,
            "671B MoE",
            "DeepSeek V3/V4 头部全能通用大模型 (综合能力顶尖)",
            False,
        )
    if "glm" in m and any(k in m for k in ("5.3", "5.2", "4", "plus", "max")):
        return (
            94,
            FreeModelCategory.FRONTIER_LARGE.value,
            "Flagship",
            "GLM 头部全能通用大模型",
            False,
        )

    # 4. 超大规模开源模型 (550B / 405B / 120B / 70B+ / 72B / Gemini Flash, 90~95)
    if "550b" in combined or "nemotron-3-ultra" in combined:
        return (
            95,
            FreeModelCategory.FRONTIER_LARGE.value,
            "550B MoE",
            "Nemotron 3 Ultra 550B 超大规模开源大模型",
            False,
        )
    if "405b" in combined:
        return (
            94,
            FreeModelCategory.FRONTIER_LARGE.value,
            "405B Dense",
            "LLaMA 3.1 405B 超大规模开源大模型",
            False,
        )
    if "super vl" in combined or "nemotron 3.5 super" in combined or "nemotron-3.5-super" in combined:
        return (
            94,
            FreeModelCategory.FRONTIER_LARGE.value,
            "120B VL",
            "NVIDIA Nemotron 3.5 Super VL 120B 顶级开源大模型 (超快输出与视觉多模态能力)",
            False,
        )
    if "120b" in combined or "nemotron-3-super" in combined:
        return (
            93,
            FreeModelCategory.FRONTIER_LARGE.value,
            "120B MoE",
            "Nemotron 3 Super 120B 超大规模开源模型",
            False,
        )
    if any(k in m for k in ("70b", "72b")):
        scale = "72B" if "72b" in m else "70B"
        return (
            92,
            FreeModelCategory.FRONTIER_LARGE.value,
            f"{scale} Dense",
            f"{scale} 级别头部通用开源大模型",
            False,
        )
    if ("gemini-2.0-flash" in m or ("gemini" in m and "flash" in m)) and "lite" not in m:
        return (
            90,
            FreeModelCategory.FRONTIER_LARGE.value,
            "Frontier",
            "Google Gemini 2.0 Flash 头部快思考/长上下文大模型",
            False,
        )

    # 参数规模正则提取 (边界安全匹配: 支持 35-billion, 120b 等，避免 31b 误中 1b)
    import re
    b_matches = [float(val) for val in re.findall(r'(?<![0-9.])(\d+(?:\.\d+)?)\s*(?:b\b|-billion\b|billion\b)', combined)]
    param_b = max(b_matches) if b_matches else None

    # 特色中坚规模大模型优先判定
    if "solar mini" in combined or "solar-mini" in combined:
        scale = f"{int(param_b)}B MoE" if param_b else "35B MoE"
        return (
            85,
            FreeModelCategory.MID_SCALE.value,
            scale,
            "Solar Mini 4 35B 混合专家架构大模型 (紧凑高效与高质量输出)",
            False,
        )

    # 5. 中坚规模大模型 (Mid-Scale: 20B ~ 45B, 78~85)
    if param_b is not None and param_b >= 35:
        return (
            85,
            FreeModelCategory.MID_SCALE.value,
            f"{int(param_b)}B",
            f"{int(param_b)}B 准大模型",
            False,
        )
    if (param_b is not None and param_b >= 28) or any(k in combined for k in ("29b", "30b", "31b", "32b", "34b")):
        scale = f"{int(param_b)}B" if param_b else "30B"
        is_reasoning = "reasoning" in combined
        return (
            82 if is_reasoning else 80,
            FreeModelCategory.MID_SCALE.value,
            scale,
            f"中坚规模通用大模型 ({scale} 参数，性能与资源均衡)",
            is_reasoning,
        )
    if (param_b is not None and param_b >= 14) or any(k in combined for k in ("14b", "15b", "26b")):
        scale = f"{int(param_b)}B" if param_b else "14B"
        return (
            78,
            FreeModelCategory.MID_SCALE.value,
            scale,
            f"{scale} 中坚规模模型",
            False,
        )
    if (param_b is not None and param_b >= 10) or any(k in combined for k in ("12b", "13b")):
        scale = f"{int(param_b)}B" if param_b else "12B~13B"
        return (
            75,
            FreeModelCategory.MID_SCALE.value,
            scale,
            f"{scale} 中等规模模型",
            False,
        )
    if "beam" in combined or "cline-free" in combined:
        return (
            70,
            FreeModelCategory.MID_SCALE.value,
            "Native",
            "宿主原生默认免费通道模型",
            False,
        )

    # 6. 小型与轻量模型 (Small Scale: < 10B, 40~65)
    if (param_b is not None and param_b >= 6) or any(k in combined for k in ("8b", "7b", "9b")):
        scale = f"{int(param_b)}B" if param_b else "8B"
        return (
            65,
            FreeModelCategory.SMALL_SCALE.value,
            scale,
            f"小型边缘模型 ({scale} 参数，仅供轻量任务)",
            False,
        )
    if (param_b is not None and param_b >= 3) or any(k in combined for k in ("3b", "4b")):
        scale = f"{int(param_b)}B" if param_b else "3B"
        return (
            55,
            FreeModelCategory.SMALL_SCALE.value,
            scale,
            f"极小轻量模型 ({scale} 参数，长上下文与复杂逻辑较弱)",
            False,
        )
    if (param_b is not None and param_b < 3) or any(k in m for k in ("1b", "0.5b", "1.5b", "2b", "2.6b")):
        scale = f"{param_b}B" if param_b else "1B"
        return (
            45,
            FreeModelCategory.SMALL_SCALE.value,
            scale,
            f"超轻玩具模型 ({scale} 参数，极易出现代码幻觉与逻辑截断)",
            False,
        )
    if any(k in m for k in ("lite", "tiny", "nano", "micro")):
        return (
            40,
            FreeModelCategory.SMALL_SCALE.value,
            "Lite",
            "极轻精简模型",
            False,
        )

    # 兜底
    return (
        50,
        FreeModelCategory.MID_SCALE.value,
        "Unknown",
        "未标明参数规模的免费模型",
        False,
    )


def list_candidate_free_models(
    providers_data: Mapping[str, Any] | None = None,
    models_data: Mapping[str, Any] | None = None,
    *,
    data_dir: Path | None = None,
    include_frontier_pool: bool = False,
    scan_home_history: bool = False,
) -> list[FreeModelCandidate]:
    """全面扫描宿主环境，聚合所有可用的免费模型候选对象，并按能力评分由高到低排序。"""
    candidates: list[FreeModelCandidate] = []
    seen: set[tuple[str, str]] = set()

    # 1. 从 providers_data 中提取
    if providers_data and isinstance(providers_data, Mapping):
        prov_map = providers_data.get("providers", {})
        if isinstance(prov_map, Mapping):
            for p_name, p_info in prov_map.items():
                if isinstance(p_info, Mapping):
                    st = p_info.get("settings", {})
                    if isinstance(st, Mapping):
                        model_name = st.get("model")
                        base_url = st.get("baseUrl")
                        if model_name and is_free_model(str(model_name), str(base_url or "")):
                            m_str = str(model_name).strip()
                            key = (p_name, m_str)
                            if key not in seen:
                                seen.add(key)
                                score, cat, scale, reason, reasoning = score_free_model_capability(m_str)
                                candidates.append(FreeModelCandidate(
                                    model_id=m_str,
                                    provider_id=p_name,
                                    name=m_str,
                                    category=cat,
                                    capability_score=score,
                                    parameter_scale=scale,
                                    reason=reason,
                                    source="configured",
                                    supports_reasoning=reasoning,
                                ))

    # 2. 从 models_data 中提取
    if models_data and isinstance(models_data, Mapping):
        provs = models_data.get("providers", {})
        if isinstance(provs, Mapping):
            for p_name, p_body in provs.items():
                if isinstance(p_body, Mapping):
                    models_dict = p_body.get("models", {})
                    if isinstance(models_dict, Mapping):
                        for m_id, m_info in models_dict.items():
                            m_str = str(m_id).strip()
                            if is_free_model(m_str):
                                key = (p_name, m_str)
                                if key not in seen:
                                    seen.add(key)
                                    score, cat, scale, reason, reasoning = score_free_model_capability(m_str)
                                    m_name = m_str
                                    supports_reason = reasoning
                                    if isinstance(m_info, Mapping):
                                        m_name = str(m_info.get("name") or m_str)
                                        if "supportsReasoning" in m_info:
                                            supports_reason = bool(m_info["supportsReasoning"])
                                    candidates.append(FreeModelCandidate(
                                        model_id=m_str,
                                        provider_id=p_name,
                                        name=m_name,
                                        category=cat,
                                        capability_score=score,
                                        parameter_scale=scale,
                                        reason=reason,
                                        source="models_catalog",
                                        supports_reasoning=supports_reason,
                                    ))

    # 3. 从 data_dir 或历史备份中提取
    search_dirs: list[Path] = []
    if data_dir:
        search_dirs.append(data_dir / "settings")
    elif scan_home_history:
        home_cline = Path.home() / ".cline" / "data" / "settings"
        if home_cline.is_dir():
            search_dirs.append(home_cline)

    for sdir in search_dirs:
        if not sdir.is_dir():
            continue
        for bak in sdir.glob("providers.json.bak*"):
            try:
                content = json.loads(bak.read_text(encoding="utf-8"))
                if isinstance(content, Mapping):
                    p_map = content.get("providers", {})
                    if isinstance(p_map, Mapping):
                        for p_name, p_info in p_map.items():
                            if isinstance(p_info, Mapping):
                                m_val = p_info.get("settings", {}).get("model")
                                if m_val and is_free_model(str(m_val)):
                                    m_str = str(m_val).strip()
                                    key = (p_name, m_str)
                                    if key not in seen:
                                        seen.add(key)
                                        score, cat, scale, reason, reasoning = score_free_model_capability(m_str)
                                        candidates.append(FreeModelCandidate(
                                            model_id=m_str,
                                            provider_id=p_name,
                                            name=m_str,
                                            category=cat,
                                            capability_score=score,
                                            parameter_scale=scale,
                                            reason=reason,
                                            source="history_cache",
                                            supports_reasoning=reasoning,
                                        ))
            except Exception:
                pass

    # 4. 可选：包含已知头部免费大模型池
    if include_frontier_pool:
        target_p = "cline"
        if providers_data and isinstance(providers_data, Mapping) and "opencode" in providers_data.get("providers", {}):
            target_p = "opencode"
        for m_str in KNOWN_FREE_FRONTIER_MODELS:
            key = (target_p, m_str)
            if key not in seen:
                seen.add(key)
                score, cat, scale, reason, reasoning = score_free_model_capability(m_str)
                candidates.append(FreeModelCandidate(
                    model_id=m_str,
                    provider_id=target_p,
                    name=m_str,
                    category=cat,
                    capability_score=score,
                    parameter_scale=scale,
                    reason=reason,
                    source="frontier_pool",
                    supports_reasoning=reasoning,
                ))

    # 按能力评分倒序排序（头部大模型绝对优先）
    candidates.sort(key=lambda c: c.rank_tuple)
    return candidates


def format_free_models_manifest(candidates: Sequence[FreeModelCandidate]) -> str:
    """生成供 AI 审计与决策的结构化免费模型清单 Markdown 表格。"""
    if not candidates:
        return "*(未发现可用免费模型候选)*"

    lines = [
        "### 免费大模型候选清单与能力分级 (Free Model Manifest)",
        "",
        "| 序号 | 模型标识 (Model ID) | 通道 | 梯队类别 | 预估规模 | 评分 | 推理支持 | 来源 | 推荐分析 |",
        "|:---:|---|:---:|:---:|:---:|:---:|:---:|:---:|---|",
    ]
    for idx, c in enumerate(candidates, start=1):
        reasoning_tag = "✓ 是" if c.supports_reasoning else "—"
        lines.append(
            f"| {idx} | `{c.model_id}` | `{c.provider_id}` | **{c.category}** | {c.parameter_scale or '—'} | "
            f"**{c.capability_score}** | {reasoning_tag} | {c.source} | {c.reason} |"
        )
    lines.append("")
    best = candidates[0]
    lines.append(
        f"> **AI 择优决策推荐**：首选头部大模型 `{best.model_id}` (评分: {best.capability_score}, 规模: {best.parameter_scale})，{best.reason}。"
    )
    return "\n".join(lines)


def format_provider_manifest(
    candidates: Sequence[ProviderCandidate],
    *,
    host: str = "",
    selected: ProviderCandidate | None = None,
) -> str:
    """生成供 AI 审计与决策的结构化宿主通道清单 Markdown 表格。"""
    if not candidates:
        return "*(未发现可用通道候选)*"

    h_upper = host.upper() if host else "HOST"
    lines = [
        f"### 宿主通道与模型智能裁决清单 ({h_upper} Provider Manifest)",
        "",
        "| 序号 | 宿主 | 通道标识 (Provider) | 阶梯梯队 (Tier) | 推荐模型 | 能力评分 | 凭据状态 | 推荐与裁决分析 |",
        "|:---:|:---:|---|:---:|---|:---:|:---:|---|",
    ]
    for idx, c in enumerate(candidates, start=1):
        tier_label = f"**Tier {int(c.tier)}**" if c.is_valid else f"Tier {int(c.tier)}"
        status_tag = "✓ 有效" if c.is_valid else "✗ 异常"
        h_name = c.host or host or "generic"
        lines.append(
            f"| {idx} | `{h_name}` | `{c.provider_id}` | {tier_label} | `{c.model or '—'}` | "
            f"**{c.capability_score}** | {status_tag} | {c.reason} |"
        )
    lines.append("")
    best = selected or candidates[0]
    lines.append(
        f"> **AI 择优决策推荐**：首选宿主 `{best.host or host}` 通道 `{best.provider_id}` "
        f"(阶梯: Tier {int(best.tier)}, 模型: `{best.model or 'default'}`, 评分: {best.capability_score})，{best.reason}。"
    )
    return "\n".join(lines)


def evaluate_and_select_free_model(
    candidates: Sequence[FreeModelCandidate],
    *,
    preferred_model: str | None = None,
    ai_judgment_reason: str | None = None,
) -> tuple[FreeModelCandidate | None, str]:
    """供 AI 决策或自主择优选择最佳免费大模型。

    返回 (selected_candidate, manifest_markdown)。
    """
    manifest = format_free_models_manifest(candidates)
    if not candidates:
        return None, manifest

    # 1. 检查环境变量或显式指定
    env_pref = os.environ.get("BUGX_FREE_MODEL") or os.environ.get("PREFERRED_FREE_MODEL")
    target_pref = (str(preferred_model or "").strip() or str(env_pref or "").strip()) or None

    if target_pref:
        # 在候选集寻找精确或前缀/后缀匹配
        for c in candidates:
            if c.model_id.lower() == target_pref.lower() or c.model_id.lower().endswith(target_pref.lower()):
                c_chosen = FreeModelCandidate(
                    model_id=c.model_id,
                    provider_id=c.provider_id,
                    name=c.name,
                    category=c.category,
                    capability_score=c.capability_score,
                    parameter_scale=c.parameter_scale,
                    reason=ai_judgment_reason or f"采纳当前 AI 明确指定决策: {c.model_id}",
                    source="ai_decision",
                    supports_reasoning=c.supports_reasoning,
                    supports_tools=c.supports_tools,
                    metadata=c.metadata,
                )
                return c_chosen, manifest

        # 候选集未找到匹配项，直接按 AI 指定创建候选
        score, cat, scale, reason, reasoning = score_free_model_capability(target_pref)
        synth_candidate = FreeModelCandidate(
            model_id=target_pref,
            provider_id=candidates[0].provider_id,
            name=target_pref,
            category=cat,
            capability_score=score,
            parameter_scale=scale,
            reason=ai_judgment_reason or f"采纳当前 AI 显式指定外部免费模型: {target_pref}",
            source="ai_decision",
            supports_reasoning=reasoning,
        )
        return synth_candidate, manifest

    # 2. 未显式指定：智能自适应选择能力评分最高的头部大模型
    best = candidates[0]
    return best, manifest


def list_available_free_models(
    providers_data: Mapping[str, Any] | None = None,
    models_data: Mapping[str, Any] | None = None,
    *,
    data_dir: Path | None = None,
    include_frontier_pool: bool = False,
    scan_home_history: bool = False,
) -> tuple[list[FreeModelCandidate], str]:
    """便捷接口：列出当前所有可用免费模型清单及格式化 Markdown 表格。

    返回 (candidates, manifest_text)。
    """
    cands = list_candidate_free_models(
        providers_data=providers_data,
        models_data=models_data,
        data_dir=data_dir,
        include_frontier_pool=include_frontier_pool,
        scan_home_history=scan_home_history,
    )
    manifest = format_free_models_manifest(cands)
    return cands, manifest


def is_free_model(model_name: str | None, base_url: str | None = None) -> bool:
    """判定是否为免额度/免点数的真正免费模型或端点。

    兼容 Cline 真实 UI 标识 ``(free)``、``:free``、``-free``、``/free``、`` free`` 以及本地端点。
    """
    if base_url and ("localhost" in base_url.lower() or "127.0.0.1" in base_url):
        return True
    if not model_name or not isinstance(model_name, str):
        return False
    m = model_name.strip().lower()
    if any(marker in m for marker in (":free", "(free)", "-free", "/free", " free")):
        return True
    if "beam-free" in m or "cline-free" in m or m.startswith("free/"):
        return True
    return False


def is_cline_pass_active(
    data_dir: Path | None = None,
    auth: Mapping[str, Any] | None = None,
) -> bool:
    """深度检测当前环境或账户是否激活了 Cline 官方原生订阅 (ClinePass)。"""
    # 0. 未登录账户不可使用 ClinePass
    if not auth or not isinstance(auth, Mapping):
        return False
    acc_token = str(auth.get("accessToken") or "").strip()
    ref_token = str(auth.get("refreshToken") or "").strip()
    acc_id = str(auth.get("accountId") or "").strip()
    if not acc_token and not ref_token and not acc_id:
        return False

    # 1. 显式环境变量覆盖
    for env_var in ("CLINE_PASS", "BUGX_CLINE_PASS", "CLINE_SUBSCRIPTION"):
        val = str(os.environ.get(env_var) or "").strip().lower()
        if val in {"1", "true", "yes", "on"}:
            return True

    # 2. 从 auth 字典中提取订阅标识
    if auth.get("clinePass") is True or auth.get("ext-cline-pass") is True:
        return True
    metadata = auth.get("metadata")
    if isinstance(metadata, Mapping):
        if metadata.get("clinePass") is True or metadata.get("subscription") is True:
            return True

    # 3. 扫描 feature-flags.json（需账户严格匹配，防借用宿主本地缓存）
    account_id = acc_id or None
    if not account_id:
        return False

    candidate_paths: list[Path] = []
    if data_dir:
        candidate_paths.extend([
            data_dir / "cache" / "feature-flags.json",
            data_dir / "cache" / "feature-flags.cline-code.json",
            data_dir / "settings" / "global-settings.json",
        ])
    else:
        home_cline_data = Path.home() / ".cline" / "data"
        candidate_paths.extend([
            home_cline_data / "cache" / "feature-flags.json",
            home_cline_data / "cache" / "feature-flags.cline-code.json",
        ])

    for cand in candidate_paths:
        if cand.is_file():
            try:
                content = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(content, dict):
                    flag_user = content.get("userId")
                    if not flag_user or flag_user != account_id:
                        continue
                    flags = content.get("flagsPayload", {}).get("featureFlags", {})
                    if flags.get("ext-cline-pass") is True or flags.get("cline-pass") is True:
                        return True
            except Exception:
                pass

    return False


def is_subscription_token(token: str | None) -> tuple[bool, str | None]:
    """尝试从 JWT 提取订阅特征（如 chatgpt_plan_type: plus）。

    返回 (is_subscription, plan_type)。
    """
    if not token or not isinstance(token, str) or token.count(".") < 2:
        return False, None
    try:
        parts = token.split(".")
        payload_b64 = parts[1]
        # 补齐 base64 padding
        padded = payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
        payload_bytes = base64.urlsafe_b64decode(padded)
        payload = json.loads(payload_bytes.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            return False, None

        # 检查常见的 OAuth 订阅负载标记
        # 1. OpenAI ChatGPT OAuth
        auth_claim = payload.get("https://api.openai.com/auth")
        if isinstance(auth_claim, dict):
            plan = auth_claim.get("chatgpt_plan_type")
            if plan:
                return True, str(plan).lower()

        # 2. 根级 plan / subscription 字段
        for key in ("plan", "plan_type", "subscription", "tier"):
            if key in payload and isinstance(payload[key], str):
                return True, payload[key].lower()

        # 3. GitHub Copilot token
        if "copilot" in token.lower() or "github" in payload.get("iss", ""):
            return True, "copilot"

    except Exception:
        pass
    return False, None


