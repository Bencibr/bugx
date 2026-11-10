#!/usr/bin/env python3
"""Audit strategy compilation and profile classification."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .constants import (
    CHECK_PROFILE_REQUIREMENTS,
    FORMS,
    MAX_COMPONENTS,
    MAX_FACTS,
    PROFILE_SCHEMA_VERSION,
    RUNTIMES,
    SURFACES,
    TARGETS,
)
from .extractors import DETECTOR_VERSION, version_value
from .fingerprint import canon


def routing_tags(profile: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return only the stable tags consumed by knowledge retrieval."""
    classification = profile.get("classification") if isinstance(profile.get("classification"), Mapping) else profile
    return {
        "forms": [str(x) for x in classification.get("forms", []) if str(x) in FORMS],
        "languages": [str(x) for x in classification.get("languages", [])],
        "runtimes": [str(x) for x in classification.get("runtimes", []) if str(x) in RUNTIMES],
        "frameworks": [str(x) for x in classification.get("frameworks", [])][:24],
        "surfaces": [str(x) for x in classification.get("surfaces", []) if str(x) in SURFACES],
        "targets": [str(x) for x in classification.get("targets", []) if str(x) in TARGETS],
        "check_profiles": [str(x) for x in classification.get("check_profiles", [])][:24],
    }


def classify_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Compile facts into the bounded action-facing classification object."""
    forms = list(profile.get("forms") or [])
    ai_arb = profile.get("ai_profile_arbitration")
    if isinstance(ai_arb, Mapping) and ai_arb.get("real_forms"):
        for rf in ai_arb["real_forms"]:
            if rf in FORMS and rf not in forms:
                forms.append(rf)
    languages = list(profile.get("languages") or [])
    frameworks = list(profile.get("frameworks") or [])
    surfaces = list(profile.get("surfaces") or [])
    targets = list(profile.get("declared_targets") or [])
    status = str(profile.get("status") or "unknown")
    check_profiles: list[str] = []
    if "web" in forms or "ui" in surfaces:
        check_profiles.extend(["web-input", "browser-state", "auth-flow"])
    if "backend" in forms or any(x in surfaces for x in ("rest", "graphql", "grpc")):
        check_profiles.extend(["api-contract", "authz", "state-transition"])
    if "mobile" in forms:
        check_profiles.extend(["mobile-lifecycle", "offline-sync", "deep-link"])
    if "embedded" in forms:
        check_profiles.extend(["firmware-boundary", "memory-safety", "update-path"])
    if "cli" in forms:
        check_profiles.extend(["argument-boundary", "filesystem-boundary", "process-exit"])
    if "infra" in forms:
        check_profiles.extend(["deployment-config", "secret-boundary", "supply-chain"])
    if profile.get("components") or any(
        isinstance(signal, Mapping) and signal.get("status") in {"candidate", "unknown"}
        for signal in profile.get("risk_signals") or []
    ):
        check_profiles.append("supply-chain")
    if not check_profiles:
        check_profiles.append("generic-code")
    res = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "status": status,
        "forms": forms,
        "languages": languages,
        "runtimes": list(profile.get("runtimes") or []),
        "frameworks": frameworks,
        "surfaces": surfaces,
        "targets": targets,
        "check_profiles": sorted(set(check_profiles)),
        "runtime_facts": list(profile.get("runtime_facts") or [])[:MAX_FACTS],
        "framework_facts": list(profile.get("framework_facts") or [])[:MAX_FACTS],
        "build_targets": list(profile.get("build_targets") or [])[:MAX_FACTS],
        "toolchains": list(profile.get("toolchains") or [])[:MAX_FACTS],
        "risk_signals": list(profile.get("risk_signals") or [])[:MAX_COMPONENTS],
        "unknowns": list(profile.get("unknowns") or [])[:MAX_FACTS],
        "conflicts": list(profile.get("conflicts") or [])[:MAX_FACTS],
    }
    scale = profile.get("scale_estimation") if isinstance(profile.get("scale_estimation"), Mapping) else {}
    code_metrics = profile.get("code_metrics") if isinstance(profile.get("code_metrics"), Mapping) else {}
    risk_metrics = profile.get("risk_metrics") if isinstance(profile.get("risk_metrics"), Mapping) else {}
    if scale:
        res["scale_level"] = str(scale.get("scale_level") or "unknown")
        primary = str(scale.get("primary_language") or "unknown")
        if primary == "unknown" and profile.get("primary_language"):
            primary = str(profile.get("primary_language"))
        res["primary_language"] = primary
    if code_metrics and "test_to_code_ratio" in code_metrics:
        res["test_to_code_ratio"] = float(code_metrics.get("test_to_code_ratio") or 0.0)
    if risk_metrics:
        res["health_grade"] = str(risk_metrics.get("health_grade") or "unknown")
        res["avg_complexity"] = float(risk_metrics.get("avg_cyclomatic_complexity") or 0.0)
        res["god_modules_count"] = int(risk_metrics.get("god_modules_count") or 0)
        res["total_sensitive_sinks"] = int(risk_metrics.get("total_sensitive_sinks") or 0)
        if "maintainability_index" in risk_metrics:
            res["maintainability_index"] = float(risk_metrics.get("maintainability_index") or 0.0)
        if "tech_debt_days" in risk_metrics:
            res["tech_debt_days"] = float(risk_metrics.get("tech_debt_days") or 0.0)
    return res


def compile_audit_strategy(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Compile classification facts into the small plan consumed by workers."""
    classification = profile.get("classification")
    if not isinstance(classification, Mapping):
        classification = classify_profile(profile)
    check_profiles = sorted({
        str(value) for value in classification.get("check_profiles", [])
        if str(value) in CHECK_PROFILE_REQUIREMENTS
    }) or ["generic-code"]
    requirements = []
    capabilities: set[str] = set()
    ev_set: set[str] = set()
    for check_profile in check_profiles:
        baseline = CHECK_PROFILE_REQUIREMENTS[check_profile]
        required_capabilities = sorted(set(baseline["capabilities"]))
        required_evidence = sorted(set(baseline["evidence"]))
        capabilities.update(required_capabilities)
        ev_set.update(required_evidence)
        requirements.append({
            "check_profile": check_profile,
            "capabilities": required_capabilities,
            "evidence_requirements": required_evidence,
        })
    runtime_facts = list(profile.get("runtime_facts") or [])
    framework_facts = list(profile.get("framework_facts") or [])
    toolchain_facts = list(profile.get("toolchains") or [])
    risk_signals = list(profile.get("risk_signals") or [])
    recommended_checks: list[dict[str, Any]] = []

    def recommend(check_profile: str, reason: str, refs: Iterable[str]) -> None:
        recommended_checks.append({
            "check_profile": check_profile,
            "reason": reason,
            "evidence_refs": sorted(set(str(x) for x in refs if x)),
        })

    framework_names = {str(item.get("name") or "") for item in framework_facts if isinstance(item, Mapping)}
    framework_refs = {
        str(item.get("name") or ""): list(item.get("evidence_refs") or [])
        for item in framework_facts if isinstance(item, Mapping)
    }
    if "nextjs" in framework_names and "nodejs" in {str(item.get("name") or "") for item in runtime_facts}:
        recommend("frontend_ssr_dependency_and_request_boundary", "Next.js + Node runtime + server-side rendering", framework_refs.get("nextjs", []))
    elif framework_names & {"react", "vue", "angular", "svelte"}:
        name = sorted(framework_names & {"react", "vue", "angular", "svelte"})[0]
        recommend("frontend_request_and_dom_boundary", f"{name} 前端框架与浏览器请求边界", framework_refs.get(name, []))
    if framework_names & {"spring-boot", "micronaut", "quarkus"}:
        name = sorted(framework_names & {"spring-boot", "micronaut", "quarkus"})[0]
        recommend("jvm_framework_request_and_deserialization", f"JVM {name} 版本相关请求绑定与反序列化检查", framework_refs.get(name, []))
    if framework_names & {"electron", "tauri"}:
        name = sorted(framework_names & {"electron", "tauri"})[0]
        recommend("desktop_webview_ipc_and_update_boundary", f"桌面端 {name} WebView/IPC/更新边界", framework_refs.get(name, []))
    if any(str(item.get("platform") or "") in {"android", "ios", "mobile"} for item in profile.get("build_targets") or [] if isinstance(item, Mapping)):
        recommend("mobile_permission_deeplink_and_lifecycle", "移动端平台目标、权限与生命周期边界", [
            ref for item in profile.get("build_targets") or [] if isinstance(item, Mapping) for ref in item.get("evidence_refs") or []
        ])
    for signal in risk_signals:
        if isinstance(signal, Mapping) and signal.get("status") == "candidate":
            recommend("supply-chain", f"{signal.get('id')} 命中 {signal.get('package')} 版本候选", signal.get("evidence_refs") or [])
            break

    review = None
    root_candidate = profile.get("root") or profile.get("project_root")
    if root_candidate:
        try:
            rev_path = Path(root_candidate) / ".bugx" / "state" / "l1-semantic-review.json"
            if rev_path.is_file():
                review = json.loads(rev_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not review and isinstance(profile.get("semantic_review"), Mapping):
        review = profile.get("semantic_review")

    if review and isinstance(review, Mapping):
        strat_verdict = review.get("audit_strategy_verdict") or {}
        missing_profiles = strat_verdict.get("missing_check_profiles") or []
        for p in missing_profiles:
            p_str = str(p)
            if p_str in CHECK_PROFILE_REQUIREMENTS and p_str not in check_profiles:
                check_profiles.append(p_str)
                baseline = CHECK_PROFILE_REQUIREMENTS[p_str]
                req_caps = sorted(set(baseline["capabilities"]))
                req_ev = sorted(set(baseline["evidence"]))
                capabilities.update(req_caps)
                ev_set.update(req_ev)
                requirements.append({
                    "check_profile": p_str,
                    "capabilities": req_caps,
                    "evidence_requirements": req_ev,
                })
        priority_focus = strat_verdict.get("priority_focus") or []
        if priority_focus:
            recommend("ai-priority-recon", f"AI 语义复核置顶关注模块: {', '.join(str(x) for x in priority_focus[:5])}", [str(x) for x in priority_focus])
        blind_spots = review.get("blind_spots") or []
        for bs in blind_spots[:3]:
            recommend("ai-blind-spot-audit", f"AI 识别盲区审计: {bs}", [])

    version_lines = []
    for f_item in runtime_facts + framework_facts + toolchain_facts:
        if not isinstance(f_item, Mapping):
            continue
        version = f_item.get("resolved_version") or f_item.get("declared_version")
        name = str(f_item.get("name") or "")
        major = version_value(version)
        if name and major:
            version_lines.append(f"{name}@{major.split('.')[0]}")
    payload = {
        "schema_version": 2,
        "profile_hash": str(profile.get("profile_hash") or ""),
        "detector_version": str(profile.get("detector_version") or DETECTOR_VERSION),
        "classification": {
            "forms": list(classification.get("forms") or []),
            "languages": list(classification.get("languages") or []),
            "runtimes": list(classification.get("runtimes") or []),
            "frameworks": list(classification.get("frameworks") or []),
            "surfaces": list(classification.get("surfaces") or []),
            "targets": list(classification.get("targets") or []),
            "toolchains": toolchain_facts[:24],
        },
        "check_profiles": check_profiles,
        "tool_requirements": requirements,
        "capabilities": sorted(capabilities),
        "knowledge_query_tags": routing_tags(profile),
        "evidence_requirements": sorted(ev_set),
        "recommended_checks": recommended_checks[:24],
        "risk_signals": risk_signals[:MAX_COMPONENTS],
        "unknowns": list(profile.get("unknowns") or [])[:MAX_FACTS],
        "conflicts": list(profile.get("conflicts") or [])[:MAX_FACTS],
        "version_lines": sorted(set(version_lines))[:24],
        "ai_review_status": (review.get("audit_strategy_verdict") or {}).get("status", "none") if review else "none",
        "ai_priority_focus": (review.get("audit_strategy_verdict") or {}).get("priority_focus", []) if review else [],
    }
    if profile.get("scale_estimation"):
        payload["scale_estimation"] = dict(profile["scale_estimation"])
    if profile.get("engineering_economics"):
        payload["engineering_economics"] = dict(profile["engineering_economics"])
    if profile.get("risk_metrics"):
        payload["risk_metrics"] = dict(profile["risk_metrics"])
    from core.skills import skill_pack

    if skill_pack is not None:
        matched_packs = skill_pack.find_matching_skill_packs(classification)
        if matched_packs:
            top_pack = matched_packs[0]
            root_candidate = profile.get("root") or profile.get("project_root")
            plan = skill_pack.compile_skill_plan(
                top_pack["skill_id"], classification,
                project_root=root_candidate,
            )
            if plan:
                payload["skill_plan"] = plan
    payload["strategy_hash"] = "sha256:" + hashlib.sha256(canon(payload)).hexdigest()
    return payload
