#!/usr/bin/env python3
"""core/mcp_handlers/knowledge_handlers.py - 知识账本、经验卡与 Harness 证据工具处理器 (DOC-070)。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from core.retrieval import knowledge_sources

from .base import (
    McpProtocolError,
    i18n_exc,
    number_argument,
    optional_string,
    optional_string_array_argument,
    serialize_value,
    string_array_argument,
)


_ROUTE_PROFILES = {
    "auth-flow", "authz", "secret-boundary", "supply-chain", "api-contract",
    "state-transition", "memory-safety", "filesystem-boundary", "process-exit",
}
_ROUTE_KEYWORDS = {
    "auth", "permission", "request", "serialization", "deserialize", "concurrency",
    "memory", "secret", "supply", "validation", "boundary", "injection", "tls",
}


def _route_values(context: Mapping[str, object], field: str) -> list[str]:
    values: list[str] = []
    sources = [context]
    for key in ("project_classification", "project_routing_tags", "audit_strategy"):
        nested = context.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    for source in sources:
        raw = source.get(field)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            continue
        for item in raw:
            if isinstance(item, str) and item.strip() and item.strip() not in values:
                values.append(item.strip()[:96])
    return values[:8]


def handle_knowledge_route(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """Route only risky work to bounded knowledge retrieval."""
    trigger = str(arguments.get("trigger") or "preflight").strip().lower()
    if trigger not in {"preflight", "strategy", "finding", "failure"}:
        raise McpProtocolError("trigger 必须是 preflight/strategy/finding/failure")
    query = optional_string(arguments, "query") or ""
    category = optional_string(arguments, "category")
    if len(query) > 512 or (category and len(category) > 96):
        raise McpProtocolError("query/category 超出长度限制")
    signatures = optional_string_array_argument(arguments, "signatures") or []
    if len(signatures) > 10 or any(len(item) > 512 for item in signatures):
        raise McpProtocolError("signatures 最多 10 项且每项不超过 512 字符")
    context = arguments.get("context") or {}
    if not isinstance(context, Mapping):
        raise McpProtocolError("context 必须是对象")

    profiles = _route_values(context, "check_profiles")
    frameworks = _route_values(context, "frameworks")
    versions = _route_values(context, "version_lines")
    tags = profiles + frameworks + versions + _route_values(context, "languages")
    search_text = " ".join([query, category or "", *tags]).lower()
    reasons: list[str] = []
    if trigger in {"finding", "failure"}:
        reasons.append(f"trigger:{trigger}")
    if signatures:
        reasons.append("error-signature")
    if _ROUTE_PROFILES.intersection(item.lower() for item in profiles):
        reasons.append("high-risk-check-profile")
    if frameworks or versions:
        reasons.append("versioned-stack")
    if any(keyword in search_text for keyword in _ROUTE_KEYWORDS):
        reasons.append("risk-keyword")
    should_retrieve = bool(reasons)
    derived_query = query or " ".join((frameworks + versions + profiles)[:8])
    if should_retrieve and not derived_query:
        derived_query = "security risk"
    selected_category = category or (profiles[0] if profiles else None)
    route = {
        "trigger": trigger,
        "query": derived_query[:512],
        "category": selected_category[:96] if selected_category else None,
        "signatures": signatures[:4],
        "tool_matrix_required": bool(profiles),
    }
    result: dict[str, object] = {
        "status": "ok",
        "should_retrieve": should_retrieve,
        "reasons": reasons[:4],
        "route": route,
        "lessons": [],
        "incidents": [],
    }
    if not should_retrieve:
        return result
    result["lessons"] = handle_lesson_retrieve(
        service,
        {"query": route["query"], "category": route["category"], "limit": 3, "token_budget": 600},
    )["lessons"]
    if signatures:
        result["incidents"] = handle_incident_hints(
            service, {"signatures": signatures[:10], "limit": 1},
        )["incidents"]
    return result


def _collision_lesson(item: Mapping[str, object]) -> dict[str, object]:
    """Return only fields needed for a fast collision with current evidence."""
    return {
        "pattern_id": item.get("pattern_id") or item.get("lesson_id"),
        "category": item.get("category"),
        "checks": list(item.get("checks") or [])[:4],
        "negative_controls": list(item.get("negative_controls") or [])[:3],
        "evidence_requirements": list(item.get("evidence_requirements") or [])[:3],
        "match_score": item.get("match_score", 0),
        "match_reasons": list(item.get("match_reasons") or [])[:4],
        "knowledge_source": item.get("knowledge_source", "project"),
    }


def handle_lesson_retrieve(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    query = arguments.get("query")
    category = arguments.get("category")
    limit = int(number_argument(arguments, "limit", 3))
    token_budget = int(number_argument(arguments, "token_budget", 600))
    if not 1 <= limit <= 3:
        raise McpProtocolError("limit 必须在 1..3")
    if not 100 <= token_budget <= 600:
        raise McpProtocolError("token_budget 必须在 100..600")
    scope = service._scope()
    lessons = knowledge_sources.retrieve_patterns(
        service._knowledge(),
        scope.knowledge_seed,
        scope.global_knowledge_seed,
        query=query if isinstance(query, str) else "",
        category=category if isinstance(category, str) else None,
        limit=limit,
        token_budget=token_budget,
        byte_budget=9000,
        runtime_only=True,
    )
    lifecycle_id, run_id = service._consumption_ids()
    request_hash = hashlib.sha256(
        json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    for item in lessons:
        subject_id = item.get("pattern_id") or item.get("lesson_id")
        if isinstance(subject_id, str) and subject_id:
            source = str(item.get("knowledge_source") or "project")
            source_path = knowledge_sources.path_for_source(
                source, scope.knowledge_seed, scope.global_knowledge_seed,
            )
            if source_path is None:
                continue
            service._knowledge().record_consumption_event(
                source_path,
                {
                    "event_id": f"mcp-retrieved:{source}:{run_id}:pattern:{subject_id}:{request_hash}",
                    "subject_type": "pattern",
                    "subject_id": subject_id,
                    "event_type": "retrieved",
                    "lifecycle_id": lifecycle_id,
                    "run_id": run_id,
                },
            )
    return {
        "status": "ok",
        "lessons": [_collision_lesson(item) for item in lessons],
    }


def handle_tool_matrix(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    scope = service._scope()
    matrix = knowledge_sources.tool_matrix(
        service._knowledge(), scope.knowledge_seed, scope.global_knowledge_seed,
    )
    lifecycle_id, run_id = service._consumption_ids()
    request_hash = hashlib.sha256(
        json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    for category in matrix.get("categories", []):
        category_name = str(category.get("category") or "aggregate")
        for item in category.get("tools", []):
            tool_name = item.get("tool_name")
            source = str(item.get("knowledge_source") or "project")
            source_path = knowledge_sources.path_for_source(
                source, scope.knowledge_seed, scope.global_knowledge_seed,
            )
            if not isinstance(tool_name, str) or not source_path:
                continue
            service._knowledge().record_consumption_event(
                source_path,
                {
                    "event_id": (
                        f"mcp-retrieved:{source}:{run_id}:tool_experience:"
                        f"{tool_name}/{category_name}:{request_hash}"
                    ),
                    "subject_type": "tool_experience",
                    "subject_id": f"{tool_name}/{category_name}",
                    "event_type": "retrieved",
                    "lifecycle_id": lifecycle_id,
                    "run_id": run_id,
                },
            )
    return dict(serialize_value(matrix))


def handle_incident_hints(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    signatures = string_array_argument(arguments, "signatures") or []
    limit = arguments.get("limit", 1)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2:
        raise McpProtocolError("limit 必须是 1..2 的整数")
    scope = service._scope()
    incidents = knowledge_sources.lookup_incidents(
        service._knowledge(),
        scope.knowledge_seed,
        scope.global_knowledge_seed,
        signatures=signatures,
        limit=limit,
        runtime_only=True,
    )
    lifecycle_id, run_id = service._consumption_ids()
    request_hash = hashlib.sha256(
        json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    for item in incidents:
        subject_id = item.get("incident_id")
        if isinstance(subject_id, str) and subject_id:
            source = str(item.get("knowledge_source") or "project")
            source_path = knowledge_sources.path_for_source(
                source, scope.knowledge_seed, scope.global_knowledge_seed,
            )
            if source_path is None:
                continue
            service._knowledge().record_consumption_event(
                source_path,
                {
                    "event_id": f"mcp-retrieved:{source}:{run_id}:incident:{subject_id}:{request_hash}",
                    "subject_type": "incident",
                    "subject_id": subject_id,
                    "event_type": "retrieved",
                    "lifecycle_id": lifecycle_id,
                    "run_id": run_id,
                },
            )
    return {
        "status": "ok",
        "incidents": [
            {
                "incident_id": item.get("incident_id"),
                "signature": item.get("signature"),
                "summary": str(item.get("summary") or "")[:240],
                "match_reasons": list(item.get("match_reasons") or [])[:4],
                "knowledge_source": item.get("knowledge_source", "project"),
                "repro_oracle": item.get("repro_oracle") or "",
            }
            for item in incidents
        ],
    }


def handle_efficiency_card(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    history = service._runtime_intelligence()
    return {
        "status": "ok",
        "rounds_recorded": len(history),
        "recent": history[-5:],
    }


def handle_get_stage_harness(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    stage = str(arguments.get("stage", "")).strip().lower()
    if not stage:
        raise McpProtocolError("缺少必需参数: stage", code=-32602)
    unit_id = arguments.get("unit_id")
    rule_name = arguments.get("rule")
    context = arguments.get("context")
    if context is not None and not isinstance(context, Mapping):
        raise McpProtocolError(i18n_exc("mcp.context_must_be_object", "context 必须是对象"), code=-32602)

    source_code = arguments.get("source_code")
    from core.planning import prompt_harness
    try:
        content = prompt_harness.get_stage_prompt(stage, context=context, root=service.root)
        metadata = prompt_harness.get_stage_metadata(stage)
    except (ValueError, FileNotFoundError) as exc:
        raise McpProtocolError(str(exc), code=-32602)

    rule_slice = None
    if unit_id and isinstance(unit_id, str):
        rule_slice = prompt_harness.detect_unit_rule_slice(
            unit_id, root=service.root, source_code=source_code if isinstance(source_code, str) else None
        )
    elif rule_name and isinstance(rule_name, str):
        try:
            rule_content = prompt_harness.load_rule(rule_name, root=service.root)
            rule_slice = f"【专项规则 - {rule_name}】\n" + rule_content
        except (ValueError, FileNotFoundError):
            rule_slice = None

    return {
        "status": "ok",
        "stage": stage,
        "prompt": content,
        "rule_slice": rule_slice,
        "stage_metadata": metadata,
    }


def handle_distill_evidence(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    raw_output = str(arguments.get("raw_output") or "")
    max_lines = number_argument(arguments, "max_lines", 15)
    fmt = str(arguments.get("format") or "text").strip().lower()
    from core.planning import prompt_harness
    distilled = prompt_harness.distill_evidence(raw_output, max_lines=int(max_lines))
    result_payload: dict[str, object] = {
        "status": "ok",
        "distilled_evidence": distilled,
    }
    if fmt == "json":
        result_payload["structured_evidence"] = prompt_harness.parse_structured_evidence(
            raw_output, distilled=distilled,
        )
    return result_payload


def handle_harness_status(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """Expose the session-scoped harness registry without executing it."""
    from core.contracts.engine_gap_contracts import harness_snapshot
    return {"status": "ok", "harness": harness_snapshot(service.root)}


def handle_oracle_suggest(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """Return advisory repository oracle candidates; never confirms a finding."""
    query = optional_string(arguments, "query") or optional_string(arguments, "unit") or ""
    if len(query) > 512:
        raise McpProtocolError("query/unit 超出长度限制")
    raw_limit = arguments.get("limit", 5)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 1 <= raw_limit <= 5:
        raise McpProtocolError("limit 必须是 1 到 5 的整数")
    from core.contracts.engine_gap_contracts import extract_oracle_candidates, oracle_suggest
    stored = oracle_suggest(service.root, query, limit=raw_limit)
    extracted = extract_oracle_candidates(service.root, query, limit=raw_limit)
    seen = {item.get("oracle_id") for item in stored}
    suggestions = stored + [item for item in extracted if item.get("oracle_id") not in seen]
    return {
        "status": "ok",
        "advisory": True,
        "suggestions": suggestions[:raw_limit],
        "confirmation_required": True,
    }


TOKEN_BASE_BUDGET = 1600
TOKEN_RENEWAL_MAX_GRANT = 400
TOKEN_MAX_RENEWALS = 2
TOKEN_HARD_CEILING = 2400
TOKEN_NEEDED_MIN = 100
TOKEN_NEEDED_MAX = 800
TOKEN_REASON_MAX_LENGTH = 128
TOKEN_RENEWAL_STATE_VERSION = 1


def handle_request_token_renewal(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """活性续命申请机制 (DOC-203)：为长复杂 PoC / 深度链路申请弹性 Token 续期。"""
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise McpProtocolError("缺少必需参数: reason 或 reason 不是非空字符串", code=-32602)
    if len(reason) > TOKEN_REASON_MAX_LENGTH:
        raise McpProtocolError(
            f"reason 长度不能超过 {TOKEN_REASON_MAX_LENGTH} 字符", code=-32602,
        )
    needed = arguments.get("needed", TOKEN_RENEWAL_MAX_GRANT)
    if (
        isinstance(needed, bool)
        or not isinstance(needed, int)
        or not TOKEN_NEEDED_MIN <= needed <= TOKEN_NEEDED_MAX
    ):
        raise McpProtocolError(
            f"needed 必须是 {TOKEN_NEEDED_MIN}..{TOKEN_NEEDED_MAX} 的整数",
            code=-32602,
        )
    if hasattr(service, "_request_token_renewal"):
        res = service._request_token_renewal(reason=reason.strip(), needed=needed)
        if isinstance(res, dict):
            return res

    lifecycle_id, run_id = service._consumption_ids()
    key = f"{lifecycle_id}:{run_id}"
    renewals_map = getattr(service, "_token_renewals", None)
    if not isinstance(renewals_map, dict):
        renewals_map = {}
        service._token_renewals = renewals_map
    record = renewals_map.setdefault(key, {"count": 0, "total_granted": 0, "reasons": []})
    max_renewals = TOKEN_MAX_RENEWALS
    hard_ceiling = TOKEN_HARD_CEILING
    base_budget = TOKEN_BASE_BUDGET
    current_total = base_budget + record["total_granted"]
    if record["count"] >= max_renewals or current_total >= hard_ceiling:
        return {
            "status": "ok",
            "approved": False,
            "granted_tokens": 0,
            "renewals_used": record["count"],
            "renewals_remaining": 0,
            "current_budget": current_total,
            "message": f"已达到单生命周期续命上限 (最大 {max_renewals} 次或上限 {hard_ceiling} Tokens)",
        }
    grant = min(TOKEN_RENEWAL_MAX_GRANT, max(100, needed))
    if current_total + grant > hard_ceiling:
        grant = max(0, hard_ceiling - current_total)
    if grant <= 0:
        return {
            "status": "ok",
            "approved": False,
            "granted_tokens": 0,
            "renewals_used": record["count"],
            "renewals_remaining": 0,
            "current_budget": current_total,
            "message": f"已达物理绝对天花板 ({hard_ceiling} Tokens)，无法继续续期",
        }
    record["count"] += 1
    record["total_granted"] += grant
    record["reasons"].append(reason[:128])
    return {
        "status": "ok",
        "approved": True,
        "granted_tokens": grant,
        "renewals_used": record["count"],
        "renewals_remaining": max_renewals - record["count"],
        "current_budget": base_budget + record["total_granted"],
        "message": f"成功批准动态续期 +{grant} Tokens (原因: {reason[:64]})",
    }


def handle_inspect_source(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    """安全读取并切片源文件（带边界自愈与沙箱隔离，支持透明符号切片路由）。"""
    path_val = arguments.get("path")
    if not isinstance(path_val, str) or not path_val.strip():
        raise McpProtocolError("path 必须是非空字符串", code=-32602)

    symbol = optional_string(arguments, "symbol") or optional_string(arguments, "method") or optional_string(arguments, "function")
    kind = optional_string(arguments, "kind")

    if symbol:
        try:
            try:
                from .retrieval_handlers import handle_retrieve_slice
            except ImportError:
                from core.mcp_handlers.retrieval_handlers import handle_retrieve_slice

            slice_res = handle_retrieve_slice(
                service,
                {
                    "target": path_val.strip(),
                    "symbol": symbol,
                    "kind": kind or ("method" if arguments.get("method") else None),
                    "to_executable": True,
                    "include_context": True,
                },
            )
            matched_count = int(slice_res.get("total_matched") or len(slice_res.get("slices") or []) or 0)
            if slice_res.get("status") == "ok" and matched_count > 0:
                return {
                    "status": "ok",
                    "routed_to": "bugx_retrieve_slice",
                    "symbol": symbol,
                    "count": matched_count,
                    **slice_res,
                }
        except Exception:
            # Fallback smoothly to standard source inspection
            pass

    try:
        start_line = int(arguments.get("start_line", 1) or 1)
    except (ValueError, TypeError):
        start_line = 1

    end_line_arg = arguments.get("end_line")
    end_line = None
    if end_line_arg is not None:
        try:
            end_line = int(end_line_arg)
        except (ValueError, TypeError):
            end_line = None

    numbered = bool(arguments.get("numbered", True))
    try:
        max_lines = int(arguments.get("max_lines", 500) or 500)
    except (ValueError, TypeError):
        max_lines = 500

    root = getattr(service, "root", None)

    try:
        from core.planning import source_inspector
        result = source_inspector.inspect_source(
            path=path_val.strip(),
            start_line=start_line,
            end_line=end_line,
            numbered=numbered,
            max_lines=max_lines,
            root=root,
        )
        return {"status": "ok", **result.to_dict()}
    except (FileNotFoundError, PermissionError, IsADirectoryError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        raise McpProtocolError(f"读取源码失败: {exc}", code=-32603)


KNOWLEDGE_HANDLERS: dict[str, Any] = {
    "bugx_knowledge_route": handle_knowledge_route,
    "bugx_lesson_retrieve": handle_lesson_retrieve,
    "bugx_tool_matrix": handle_tool_matrix,
    "bugx_incident_hints": handle_incident_hints,
    "bugx_efficiency_card": handle_efficiency_card,
    "bugx_get_stage_harness": handle_get_stage_harness,
    "bugx_distill_evidence": handle_distill_evidence,
    "bugx_harness_status": handle_harness_status,
    "bugx_oracle_suggest": handle_oracle_suggest,
    "bugx_request_token_renewal": handle_request_token_renewal,
    "bugx_inspect_source": handle_inspect_source,
}

