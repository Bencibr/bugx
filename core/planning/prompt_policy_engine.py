#!/usr/bin/env python3
"""core/planning/prompt_policy_engine.py — 声明式可配置提示词策略引擎（零 if-else 硬编码）。

核心职责：
1. 加载内置 ``core/prompts/policies/prompt_policies.json`` 并支持项目级
   ``<root>/.bugx/prompt-policies.json`` 深度合并与环境变量 ``BUGX_PROMPT_POLICY`` 钉选；
2. 按确定性优先级解析 Profile：
   explicit_policy / BUGX_PROMPT_POLICY → reactive_triggers（如遇 cyber policy / Daybreak 拦截）
   → routing_rules（按 priority 降序匹配 host / provider / model_patterns）→ default_profile (standard)；
3. 代码块保护型变换管线（``apply_policy``）：只改写自然语言 prose 段落，物理保护 Markdown
   围栏代码块（```` ```...``` ````）与行内代码（`` `...` ``）及 CWE/技术机制符号零损坏；
4. 严格 fail-closed：非法 JSON、无效正则、未知 profile、越界 ``stage_overrides`` 路径均抛出
   ``PromptPolicyError``。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_POLICIES_PATH = Path(__file__).resolve().parents[1] / "prompts" / "policies" / "prompt_policies.json"
PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"
ENV_POLICY_KEY = "BUGX_PROMPT_POLICY"

# Split markdown into fenced code blocks, inline code spans, and normal prose segments.
_CODE_SEGMENT_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")


class PromptPolicyError(ValueError):
    """Raised when prompt policy configuration violates schema or safety constraints."""


@dataclass(frozen=True)
class ResolvedPolicy:
    name: str
    reason: str
    description: str
    preamble: str
    stage_overrides: Mapping[str, str]
    lexicon_rewrites: tuple[tuple[re.Pattern[str], str], ...]
    safe_repro_directive: str
    matched_rule_id: str | None = None
    triggered_by_error: bool = False

    def is_passthrough(self) -> bool:
        return (
            not self.preamble
            and not self.stage_overrides
            and not self.lexicon_rewrites
            and not self.safe_repro_directive
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "reason": self.reason,
            "matched_rule_id": self.matched_rule_id,
            "triggered_by_error": self.triggered_by_error,
            "has_stage_overrides": bool(self.stage_overrides),
            "rewrite_count": len(self.lexicon_rewrites),
        }


def _validate_stage_override_path(rel_path: str) -> str:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise PromptPolicyError("stage_overrides 路径必须为非空字符串")
    cleaned = rel_path.replace("\\", "/").strip()
    candidate = Path(cleaned)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PromptPolicyError(f"stage_overrides 禁止绝对路径或目录穿越: {rel_path}")
    resolved_root = PROMPTS_ROOT.resolve()
    cursor = resolved_root
    for part in candidate.parts:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            raise PromptPolicyError(f"stage_overrides 禁止符号链接组件: {rel_path}")
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PromptPolicyError(f"stage_overrides 越界 core/prompts/: {rel_path}") from exc
    return cleaned


def _compile_pattern(pattern: object, label: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise PromptPolicyError(f"{label} 正则必须是非空字符串")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise PromptPolicyError(f"{label} 正则编译失败 ({pattern!r}): {exc}") from exc


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PromptPolicyError(f"无法读取策略配置 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PromptPolicyError(f"策略配置必须是 JSON 对象: {path}")
    return data


def _validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    profiles = config.get("profiles")
    if not isinstance(profiles, Mapping) or not profiles:
        raise PromptPolicyError("策略配置缺少非空 profiles 对象")
    default_profile = str(config.get("default_profile") or "standard").strip()
    if default_profile not in profiles:
        raise PromptPolicyError(f"default_profile={default_profile!r} 未在 profiles 中定义")

    validated_profiles: dict[str, dict[str, Any]] = {}
    for pname, pdef in profiles.items():
        if not isinstance(pname, str) or not pname.strip() or not isinstance(pdef, Mapping):
            raise PromptPolicyError(f"非法 profile 定义: {pname!r}")
        raw_overrides = pdef.get("stage_overrides") or {}
        if not isinstance(raw_overrides, Mapping):
            raise PromptPolicyError(f"profile {pname}.stage_overrides 必须是对象")
        overrides = {
            str(k).strip().lower(): _validate_stage_override_path(str(v))
            for k, v in raw_overrides.items()
            if str(k).strip()
        }
        raw_rewrites = pdef.get("lexicon_rewrites") or []
        if not isinstance(raw_rewrites, list):
            raise PromptPolicyError(f"profile {pname}.lexicon_rewrites 必须是列表")
        rewrites: list[dict[str, str]] = []
        for idx, rw in enumerate(raw_rewrites):
            if not isinstance(rw, Mapping):
                raise PromptPolicyError(f"profile {pname}.lexicon_rewrites[{idx}] 必须是对象")
            _compile_pattern(rw.get("pattern"), f"{pname}.lexicon_rewrites[{idx}]")
            if not isinstance(rw.get("replace"), str):
                raise PromptPolicyError(f"profile {pname}.lexicon_rewrites[{idx}].replace 必须是字符串")
            rewrites.append({"pattern": str(rw["pattern"]), "replace": str(rw["replace"])})
        validated_profiles[pname.strip()] = {
            "description": str(pdef.get("description") or ""),
            "preamble": str(pdef.get("preamble") or ""),
            "stage_overrides": overrides,
            "lexicon_rewrites": rewrites,
            "safe_repro_directive": str(pdef.get("safe_repro_directive") or ""),
        }

    routing_rules: list[dict[str, Any]] = []
    for idx, rule in enumerate(config.get("routing_rules") or ()):
        if not isinstance(rule, Mapping):
            raise PromptPolicyError(f"routing_rules[{idx}] 必须是对象")
        target_profile = str(rule.get("profile") or "").strip()
        if target_profile not in validated_profiles:
            raise PromptPolicyError(f"routing_rules[{idx}] 引用未知 profile: {target_profile!r}")
        match = rule.get("match") or {}
        if not isinstance(match, Mapping):
            raise PromptPolicyError(f"routing_rules[{idx}].match 必须是对象")
        for m_idx, pat in enumerate(match.get("model_patterns") or ()):
            _compile_pattern(pat, f"routing_rules[{idx}].model_patterns[{m_idx}]")
        routing_rules.append({
            "id": str(rule.get("id") or f"rule-{idx}"),
            "priority": int(rule.get("priority") or 0),
            "match": {
                "hosts": [str(h).strip().lower() for h in (match.get("hosts") or ()) if str(h).strip()],
                "providers": [str(p).strip().lower() for p in (match.get("providers") or ()) if str(p).strip()],
                "model_patterns": [str(p) for p in (match.get("model_patterns") or ()) if str(p)],
            },
            "profile": target_profile,
        })

    reactive_triggers: list[dict[str, Any]] = []
    for idx, trig in enumerate(config.get("reactive_triggers") or ()):
        if not isinstance(trig, Mapping):
            raise PromptPolicyError(f"reactive_triggers[{idx}] 必须是对象")
        fb = str(trig.get("fallback_profile") or "").strip()
        if fb not in validated_profiles:
            raise PromptPolicyError(f"reactive_triggers[{idx}] 引用未知 fallback_profile: {fb!r}")
        pats = trig.get("error_patterns") or []
        if not isinstance(pats, list) or not pats:
            raise PromptPolicyError(f"reactive_triggers[{idx}].error_patterns 必须是非空列表")
        for p_idx, pat in enumerate(pats):
            _compile_pattern(pat, f"reactive_triggers[{idx}].error_patterns[{p_idx}]")
        reactive_triggers.append({
            "id": str(trig.get("id") or f"trigger-{idx}"),
            "error_patterns": [str(p) for p in pats],
            "fallback_profile": fb,
        })

    return {
        "schema_version": int(config.get("schema_version") or 1),
        "default_profile": default_profile,
        "profiles": validated_profiles,
        "routing_rules": routing_rules,
        "reactive_triggers": reactive_triggers,
    }


def load_policy_config(
    *,
    root: Path | None = None,
    builtin_path: Path | None = None,
) -> dict[str, Any]:
    """Load built-in policies and optionally merge project-level .bugx/prompt-policies.json."""
    base_path = Path(builtin_path) if builtin_path is not None else DEFAULT_POLICIES_PATH
    merged = _load_json_file(base_path)

    if root is not None:
        override_path = Path(root).resolve() / ".bugx" / "prompt-policies.json"
        if override_path.is_file() and not override_path.is_symlink():
            proj = _load_json_file(override_path)
            if "default_profile" in proj:
                merged["default_profile"] = proj["default_profile"]
            if isinstance(proj.get("profiles"), Mapping):
                combined_profiles = dict(merged.get("profiles") or {})
                combined_profiles.update(proj["profiles"])
                merged["profiles"] = combined_profiles
            if isinstance(proj.get("routing_rules"), list):
                merged["routing_rules"] = [*proj["routing_rules"], *(merged.get("routing_rules") or [])]
            if isinstance(proj.get("reactive_triggers"), list):
                merged["reactive_triggers"] = [*proj["reactive_triggers"], *(merged.get("reactive_triggers") or [])]

    return _validate_config(merged)


def _build_resolved(
    config: Mapping[str, Any],
    profile_name: str,
    *,
    reason: str,
    matched_rule_id: str | None = None,
    triggered_by_error: bool = False,
) -> ResolvedPolicy:
    profiles = config["profiles"]
    if profile_name not in profiles:
        raise PromptPolicyError(f"请求的 prompt policy 未定义: {profile_name!r}")
    pdef = profiles[profile_name]
    compiled_rewrites = tuple(
        (re.compile(rw["pattern"]), rw["replace"])
        for rw in pdef["lexicon_rewrites"]
    )
    return ResolvedPolicy(
        name=profile_name,
        reason=reason,
        description=pdef["description"],
        preamble=pdef["preamble"],
        stage_overrides=dict(pdef["stage_overrides"]),
        lexicon_rewrites=compiled_rewrites,
        safe_repro_directive=pdef["safe_repro_directive"],
        matched_rule_id=matched_rule_id,
        triggered_by_error=triggered_by_error,
    )


def detect_policy_block(
    error_text: str | None,
    *,
    root: Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    """Detect whether an upstream model/host error matches any configured reactive trigger."""
    text = (error_text or "").strip()
    if not text:
        return None
    cfg = _validate_config(config) if config is not None else load_policy_config(root=root)
    for trig in cfg["reactive_triggers"]:
        for pat in trig["error_patterns"]:
            if re.search(pat, text):
                return {
                    "trigger_id": trig["id"],
                    "fallback_profile": trig["fallback_profile"],
                    "matched_pattern": pat,
                }
    return None


def resolve_policy(
    *,
    host: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    last_error: str | None = None,
    explicit_policy: str | None = None,
    root: Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> ResolvedPolicy:
    """Resolve the active prompt policy profile without hardcoded host if-else branches."""
    cfg = _validate_config(config) if config is not None else load_policy_config(root=root)

    # 1. Explicit override (argument or BUGX_PROMPT_POLICY env var) has highest authority
    chosen = (explicit_policy or os.environ.get(ENV_POLICY_KEY) or "").strip()
    if chosen:
        source = "explicit_arg" if explicit_policy else f"env:{ENV_POLICY_KEY}"
        return _build_resolved(cfg, chosen, reason=source)

    # 2. Reactive error trigger (e.g. OpenAI cyber policy / Daybreak block)
    hit = detect_policy_block(last_error, config=cfg)
    if hit is not None:
        return _build_resolved(
            cfg,
            hit["fallback_profile"],
            reason=f"reactive_trigger:{hit['trigger_id']}",
            matched_rule_id=hit["trigger_id"],
            triggered_by_error=True,
        )

    # 3. Declarative routing rules sorted by descending priority
    norm_host = (host or "").strip().lower()
    norm_provider = (provider or "").strip().lower()
    norm_model = (model or "").strip()

    sorted_rules = sorted(cfg["routing_rules"], key=lambda r: (-int(r["priority"]), r["id"]))
    for rule in sorted_rules:
        match = rule["match"]
        host_hit = bool(norm_host and norm_host in match["hosts"])
        prov_hit = bool(norm_provider and norm_provider in match["providers"])
        model_hit = bool(
            norm_model and any(re.search(pat, norm_model) for pat in match["model_patterns"])
        )
        if host_hit or prov_hit or model_hit:
            return _build_resolved(
                cfg,
                rule["profile"],
                reason=f"routing_rule:{rule['id']}",
                matched_rule_id=rule["id"],
            )

    # 4. Default profile (standard -> zero-rewrite passthrough)
    return _build_resolved(cfg, cfg["default_profile"], reason="default_profile")


def _rewrite_prose_preserving_code(
    text: str,
    rewrites: Sequence[tuple[re.Pattern[str], str]],
) -> str:
    """Apply lexicon rewrites strictly to prose segments; preserve fenced & inline code verbatim."""
    if not text or not rewrites:
        return text
    segments = _CODE_SEGMENT_RE.split(text)
    for idx in range(0, len(segments), 2):  # even indices are non-code prose
        seg = segments[idx]
        for pattern, replacement in rewrites:
            seg = pattern.sub(replacement, seg)
        segments[idx] = seg
    return "".join(segments)


def resolve_stage_override_relpath(policy: ResolvedPolicy, stage: str) -> str | None:
    key = (stage or "").strip().lower()
    return policy.stage_overrides.get(key)


def apply_policy(
    text: str,
    policy: ResolvedPolicy,
    *,
    include_preamble: bool = True,
    include_safe_repro: bool = False,
) -> str:
    """Apply a resolved policy to prompt/guidance text with code-span protection."""
    if policy.is_passthrough():
        return text

    transformed = _rewrite_prose_preserving_code(text, policy.lexicon_rewrites)
    parts: list[str] = []
    if include_preamble and policy.preamble:
        if policy.preamble not in transformed:
            parts.append(policy.preamble)
    parts.append(transformed)
    if include_safe_repro and policy.safe_repro_directive:
        if policy.safe_repro_directive not in transformed:
            parts.append(f"\n[Safe Repro Policy] {policy.safe_repro_directive}")
    return "\n\n".join(parts) if len(parts) > 1 else parts[0]


__all__ = [
    "DEFAULT_POLICIES_PATH",
    "ENV_POLICY_KEY",
    "PromptPolicyError",
    "ResolvedPolicy",
    "load_policy_config",
    "detect_policy_block",
    "resolve_policy",
    "resolve_stage_override_relpath",
    "apply_policy",
]
