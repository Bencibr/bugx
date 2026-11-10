#!/usr/bin/env python3
"""launch_phases/lesson_context.py - 经验事实装配、提示词卡片压缩与检索上下文 (DOC-036)。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .common import (
    AGENT_DIR,
    LESSON_LEDGER_SCRIPT,
    LESSON_LEDGER_FILE,
    GLOBAL_LESSON_LEDGER_FILE,
    LESSON_CONTEXT_MAX_CARDS,
    LESSON_CONTEXT_BYTE_BUDGET,
    LESSON_CONTEXT_TOKEN_BUDGET,
    DEEP_CARD_LIMIT,
    DEEP_CARD_BYTE_BUDGET,
    DEEP_CARD_TOKEN_BUDGET,
    resolve_project_root,
)

try:
    from core.runtime import project_profile
except ImportError:
    project_profile = None
from .session_runtime import (
    load_runtime,
    save_runtime,
    runtime_lock,
)
from .supervisor_bridge import supervisor_units

try:
    from core.ledger import lesson_ledger as _ll
except ImportError:
    _ll = None
from core.retrieval import knowledge_sources

try:
    from core.governance.token_budget import estimate_tokens
except ImportError:
    def estimate_tokens(value: str | bytes, model: str | None = None) -> tuple[int, str]:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        return max(1, len(raw) // 4), "fallback"


def _lesson_module():
    global _ll
    if _ll is None:
        from core.ledger import lesson_ledger as _ll
    return _ll


def _lesson_priority_key(card: dict) -> tuple:
    # 辅助排序键
    return (
        0 if card.get('hit_count', 0) > 0 else 1,
        -card.get('hit_count', 0),
        card.get('pattern_id', ''),
    )


def _cloud_prefetch_incidents(profile: dict | None, routing_tags: dict | None,
                              limit: int) -> list[dict]:
    """读取云端定向经验；只返回有界、未落盘的候选卡片输入。"""
    if limit <= 0:
        return []
    try:
        from core.cloud_sync.cloud_client import CloudClient
        from core.cloud_sync.cloud_onboard import load_project_binding, get_state

        root = resolve_project_root()
        if get_state(root) == "local":
            return []
        client = CloudClient()
        if not client.cred.is_authenticated():
            return []
        project_id = load_project_binding(root)
        result = client.prefetch_knowledge(
            profile or {}, routing_tags=routing_tags, limit=limit,
            project_id=project_id,
        )
        if not result.ok:
            return []
        data = client._data(result) if hasattr(client, "_data") else result.data
        if not isinstance(data, dict):
            return []
        rows = data.get("incidents") or []
        return [row for row in rows if isinstance(row, dict)][:limit]
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
        # 云端预拉是加速器；正式同步失败或旧服务端不支持时继续使用本地账本。
        return []


def _incident_card(item: dict, source: str) -> tuple[dict, bytes]:
    """把本地/云端 incident 压缩为同一 deep-card 结构 (DOC-203)。"""
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    incident_key = str(item.get("incident_id") or "").strip()
    # 提取并截断 repro_oracle (硬限制 300 字符)
    raw_oracle = item.get("repro_oracle")
    if not raw_oracle and "复现：" in str(item.get("context", "")):
        parts = str(item["context"]).split("复现：", 1)
        if len(parts) > 1 and "confirmed finding" not in parts[1]:
            raw_oracle = parts[1].strip()
    repro_oracle = str(raw_oracle or "")[:300].strip()

    # 过滤占位符
    before = item.get("before_snippet") or item.get("before_code") or ""
    if "未提供" in before:
        before = ""
    after = item.get("after_snippet") or item.get("after_code") or ""
    if "未提供" in after:
        after = ""
    decision = item.get("decision") or item.get("decision_chain") or ""
    if "自动沉淀" in decision:
        decision = ""

    card = {
        "kind": "incident",
        "incident_id": incident_key,
        "signature": item.get("signature") or item.get("error_signature"),
        "summary": item.get("summary") or item.get("root_cause") or item.get("title"),
        "repro_oracle": repro_oracle,
        "before_snippet": before,
        "after_snippet": after,
        "decision": decision,
        "diff_ref": item.get("diff_ref") or details.get("diff_ref"),
        "commit_ref": item.get("commit_ref") or details.get("commit_ref"),
        "duration_seconds": item.get("duration_seconds") or item.get("duration"),
        "match_score": item.get("match_score", 0),
        "routing_tags": item.get("routing_tags", {}),
        "match_reasons": item.get("match_reasons", []),
        "knowledge_source": source,
    }
    return card, json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

def lesson_context(query: str = "", category: str | None = None,
                    profile: dict | None = None) -> dict:
    """Validate and retrieve a compact, bounded lesson plan.

    This is deliberately read-only.  An initialized project missing its
    ledger must stop here instead of silently replacing historical experience
    with an empty file.  The ledger backend may additionally enforce the same
    budgets; the card compaction below is the final defense at the context
    boundary so old backends cannot inflate the prompt.
    """
    if not LESSON_LEDGER_SCRIPT.is_file():
        raise ValueError(f"缺少复盘账本脚本: {LESSON_LEDGER_SCRIPT}")
    project_db = LESSON_LEDGER_FILE.with_suffix(".sqlite3")
    if not project_db.is_file() or project_db.is_symlink():
        raise ValueError(
            f"缺少或不安全的项目错题集 SQLite 运行库: {project_db}；"
            "请从备份恢复，禁止从 JSON 种子静默重建"
        )
    ledger = _lesson_module()
    if profile is None and project_profile is not None:
        try:
            profile = project_profile.load_project_profile(LESSON_LEDGER_FILE)
        except (OSError, ValueError, TypeError):
            profile = None
    requested_tags = (
        project_profile.routing_tags(profile) if profile and project_profile else None
    )
    audit_strategy = (
        project_profile.compile_audit_strategy(profile)
        if profile and project_profile else None
    )
    # read() is validation-only; initialize() is intentionally forbidden here.
    ledger.read(LESSON_LEDGER_FILE)
    summary = ledger.metrics(LESSON_LEDGER_FILE)
    delayed_ids = {
        str(item) for item in summary.get("delayed_hit_pattern_ids", [])
        if isinstance(item, str)
    }
    global_summary = None
    if GLOBAL_LESSON_LEDGER_FILE.with_suffix(".sqlite3").is_file():
        try:
            ledger.read(GLOBAL_LESSON_LEDGER_FILE)
            global_summary = ledger.metrics(GLOBAL_LESSON_LEDGER_FILE)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Global knowledge is an optional read-only accelerator.  A bad
            # global store must not make a project's authoritative ledger
            # unusable; doctor reports the degraded global source separately.
            global_summary = {"status": "invalid", "error": str(exc)}
    runtime = load_runtime()
    used_ids = set(runtime.get("lifecycle_lesson_ids", []))
    source_errors = []
    recommendations = knowledge_sources.retrieve_patterns(
        ledger,
        LESSON_LEDGER_FILE,
        GLOBAL_LESSON_LEDGER_FILE,
        query=query,
        category=category,
        limit=LESSON_CONTEXT_MAX_CARDS,
        routing_tags=requested_tags,
        include_pattern_ids=delayed_ids,
        runtime_only=True,
    )
    # A project pattern is authoritative only for the same pattern_id. Across
    # different IDs, relevance must win; otherwise a weak project card could
    # crowd out a highly relevant global card merely because of its source.
    by_pattern_id: dict[str, dict] = {}
    for item in recommendations:
        key = item.get("pattern_id") or item.get("lesson_id")
        if not isinstance(key, str) or not key:
            continue
        existing = by_pattern_id.get(key)
        if existing is None or (
            item.get("knowledge_source") == "project"
            and existing.get("knowledge_source") != "project"
        ):
            by_pattern_id[key] = item
    recommendations = list(by_pattern_id.values())
    recommendations.sort(key=_lesson_priority_key)
    seen_pattern_ids: set[str] = set()
    cards: list[dict] = []
    categories: set[str] = set()
    total_bytes = 0
    total_tokens = 0
    suppressed = 0
    category_counts: dict[str, int] = {}
    for item in recommendations:
        lesson_key = item.get("pattern_id") or item.get("lesson_id")
        if not isinstance(lesson_key, str) or not lesson_key or lesson_key in used_ids:
            suppressed += 1
            continue
        if lesson_key in seen_pattern_ids:
            suppressed += 1
            continue
        category_value = item.get("category") or item.get("scope") or "uncategorized"
        probes = item.get("probes") or item.get("checks") or item.get("active_probes") or []
        if not isinstance(probes, list):
            probes = [str(probes)]
        card = {
            "pattern_id": lesson_key,
            "category": str(category_value),
            "title": str(item.get("title") or item.get("name") or lesson_key),
            "checks": [str(value) for value in probes[:4]],
            "negative_controls": [str(value) for value in (
                item.get("negative_controls") or item.get("counter_checks") or []
            )][:3],
            "evidence": [str(value) for value in (
                item.get("evidence_requirements") or item.get("evidence") or []
            )][:3],
            "match_score": item.get("match_score", 0),
            "knowledge_source": item.get("knowledge_source", "project"),
        }
        encoded = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        card_bytes = len(encoded)
        card_tokens, token_method = estimate_tokens(encoded)
        if total_bytes + card_bytes > LESSON_CONTEXT_BYTE_BUDGET \
                or total_tokens + card_tokens > LESSON_CONTEXT_TOKEN_BUDGET:
            suppressed += 1
            continue
        # Diversity is intentional, but a narrow query must still receive related cards.
        normalized_category = str(category_value).casefold()
        max_per_category = 1 if category is None and len(categories) < 3 else 2
        if category_counts.get(normalized_category, 0) >= max_per_category:
            suppressed += 1
            continue
        cards.append(card)
        seen_pattern_ids.add(lesson_key)
        categories.add(normalized_category)
        category_counts[normalized_category] = category_counts.get(normalized_category, 0) + 1
        total_bytes += card_bytes
        total_tokens += card_tokens
    used_incidents = set(runtime.get("lifecycle_incident_ids", []))
    deep_cards: list[dict] = []
    deep_bytes = 0
    deep_tokens = 0
    # DOC-203: 自适应弹性预算与深度错题卡放宽 (最大 2 张高质量 repro_oracle，上限 2400 Tokens)
    effective_deep_limit = 2
    effective_token_budget = min(2400, max(DEEP_CARD_TOKEN_BUDGET, 1600))

    def append_incident(item: dict, source: str) -> bool:
        nonlocal deep_bytes, deep_tokens
        incident_key = str(item.get("incident_id") or "").strip()
        if not incident_key or incident_key in used_incidents:
            return False
        card, encoded = _incident_card(item, source)
        card_tokens, _ = estimate_tokens(encoded)
        if (deep_bytes + len(encoded) > DEEP_CARD_BYTE_BUDGET or
                deep_tokens + card_tokens > effective_token_budget):
            return False
        deep_cards.append(card)
        used_incidents.add(incident_key)
        deep_bytes += len(encoded)
        deep_tokens += card_tokens
        return len(deep_cards) >= effective_deep_limit

    try:
        recent_signatures = [
            str(value) for value in runtime.get("lesson_recent_signatures", [])
            if isinstance(value, str)
        ][-5:]
        incidents = knowledge_sources.lookup_incidents(
            ledger,
            LESSON_LEDGER_FILE,
            GLOBAL_LESSON_LEDGER_FILE,
            query=query, signatures=recent_signatures,
            limit=effective_deep_limit, token_budget=effective_token_budget,
            byte_budget=DEEP_CARD_BYTE_BUDGET, runtime_only=True,
            routing_tags=requested_tags,
        )
        for item in incidents:
            if append_incident(item, str(item.get("knowledge_source") or "project")):
                if len(deep_cards) >= effective_deep_limit:
                    break
    except (AttributeError, TypeError, ValueError):
        deep_cards = []
        deep_bytes = 0
        deep_tokens = 0
    if len(deep_cards) < effective_deep_limit:
        for item in _cloud_prefetch_incidents(profile, requested_tags, effective_deep_limit):
            if append_incident(item, "cloud"):
                if len(deep_cards) >= effective_deep_limit:
                    break
    # 工具经验卡片：项目优先、全局只读补充，按 tool_name 去重后取前 3。
    # 与 pattern 卡片分开预算，失败只降级为空列表，不阻断启动。
    tool_cards: list[dict] = []
    try:
        tool_pool = {
            str(item["tool_name"]): item
            for item in knowledge_sources.lookup_tools(
                ledger, LESSON_LEDGER_FILE, GLOBAL_LESSON_LEDGER_FILE,
                query=query, limit=3,
            )
            if isinstance(item.get("tool_name"), str) and item.get("tool_name")
        }
        tool_cards = sorted(
            tool_pool.values(),
            key=lambda x: (-x.get("match_score", 0), str(x.get("tool_name"))),
        )[:3]
    except (AttributeError, TypeError, ValueError, KeyError, OSError):
        tool_cards = []
    return {
        "revision": summary["revision"],
        "head_hash": summary["head_hash"],
        "state_hash": summary["state_hash"],
        "lessons_total": summary["lessons_total"],
        "lessons_active": summary["lessons_active"],
        "query": query,
        "category": category,
        "project_profile_hash": (profile or {}).get("profile_hash"),
        "project_classification": (profile or {}).get("classification", {}),
        "project_routing_tags": requested_tags or {},
        "audit_strategy": audit_strategy,
        "retrieval_policy": {
            "backend": "sqlite-fts5-plus-keyword-fallback",
            "sources": ["project", "global-read-only"],
            "selection": "routing-tags-then-relevance-confidence-source-freshness-category-diversity",
            "old_pattern_suppression": "lifecycle_lesson_ids",
        },
        "recommendations": cards,
        "deep_cards": deep_cards,
        "tool_cards": tool_cards,
        "context_budget": {
            "max_cards": LESSON_CONTEXT_MAX_CARDS,
            "max_tokens": LESSON_CONTEXT_TOKEN_BUDGET,
            "max_bytes": LESSON_CONTEXT_BYTE_BUDGET,
            "tokens": total_tokens,
            "bytes": total_bytes,
            "token_method": token_method if cards else "not-applicable",
            "suppressed": suppressed,
            "source_errors": source_errors,
            "deep_cards": {
                "max": DEEP_CARD_LIMIT,
                "max_tokens": DEEP_CARD_TOKEN_BUDGET,
                "max_bytes": DEEP_CARD_BYTE_BUDGET,
                "prefetched": len(deep_cards),
                "bytes": deep_bytes,
                "tokens": deep_tokens,
            },
        },
        "knowledge_sources": {
            "project": {
                "revision": summary.get("revision"),
                "state_hash": summary.get("state_hash"),
            },
            "global": global_summary if global_summary is not None else {"status": "missing"},
        },
    }

def derive_lesson_query(explicit_query: str, category: str | None = None) -> tuple[str, str]:
    """从当前待覆盖单元生成短查询；显式用户查询始终优先。"""
    if explicit_query.strip():
        return explicit_query.strip(), "user"
    try:
        units = supervisor_units()
    except (OSError, ValueError, RuntimeError):
        units = []
    # 只取稳定单元 ID 的最后一段，避免把源码路径、日志和大段上下文注入账本。
    terms = []
    for unit in units[:4]:
        value = str(unit).rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        value = value.split("::", 1)[0].strip()
        if value and value not in terms:
            terms.append(value[:80])
    if category:
        terms.insert(0, category[:80])
    return " ".join(terms), "coverage-derived" if terms else "empty"

def set_lesson_context(context: dict) -> dict:
    with runtime_lock():
        state = load_runtime()
        state["lesson_context"] = context
        seen = list(state.get("lifecycle_lesson_ids", []))
        for item in context.get("recommendations", []):
            lesson_key = item.get("pattern_id") or item.get("lesson_id")
            if isinstance(lesson_key, str) and lesson_key and lesson_key not in seen:
                seen.append(lesson_key)
        state["lifecycle_lesson_ids"] = seen[-100:]
        incident_ids = list(state.get("lifecycle_incident_ids", []))
        for item in context.get("deep_cards", []):
            incident_key = item.get("incident_id")
            if isinstance(incident_key, str) and incident_key and incident_key not in incident_ids:
                incident_ids.append(incident_key)
        state["lifecycle_incident_ids"] = incident_ids[-100:]
        save_runtime(state)
        # Workers receive retrieval capability, not these card bodies. Record
        # prefetch selection only; actual retrieval/usage is recorded elsewhere.
        ledger = _lesson_module()
        lifecycle_id = str(state.get("lifecycle_id") or "unknown")
        run_id = str(state.get("run_id") or "unknown")
        for items, key_name in (
            (context.get("recommendations", []), "pattern_id"),
            (context.get("deep_cards", []), "incident_id"),
            (context.get("tool_cards", []), "tool_name"),
        ):
            for item in items:
                subject_id = item.get(key_name)
                if not isinstance(subject_id, str) or not subject_id:
                    continue
                subject_type = "incident" if key_name == "incident_id" else "pattern"
                if key_name == "tool_name":
                    category = "aggregate"
                    top_categories = item.get("top_categories")
                    if isinstance(top_categories, list) and top_categories:
                        first = top_categories[0]
                        if isinstance(first, Mapping) and first.get("category"):
                            category = str(first["category"])
                    subject_id = f"{subject_id}/{category}"
                    subject_type = "tool_experience"
                source = str(item.get("knowledge_source") or "project")
                source_path = knowledge_sources.path_for_source(
                    source, LESSON_LEDGER_FILE, GLOBAL_LESSON_LEDGER_FILE,
                )
                if source_path is None:
                    continue
                ledger.record_consumption_event(
                    source_path,
                    {
                        "event_id": f"selected:{source}:{run_id}:{subject_id}",
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "event_type": "selected",
                        "lifecycle_id": lifecycle_id,
                        "run_id": run_id,
                    },
                )
    return state

def lesson_review_matches(round_result: dict,
                           lifecycle_id: str | None,
                           polling_id: str | None = None) -> tuple[bool, str]:
    """验证复盘事实与本次结算结果一致，而不只检查一行是否存在。

    R6 复盘 S4：账本三表按 polling_id 隔离。优先精确匹配当前 polling 的
    review 行；精确匹配不到时回退读遗留 '' 行（外部 Supervisor CLI 的
    ``lesson_ledger.py review`` 不携带 polling 维度，落库即 ''），避免把
    合法的外部复盘判成缺失。
    """
    if not isinstance(lifecycle_id, str) or not lifecycle_id:
        return False, "缺少 lifecycle_id"
    scope = polling_id or ""
    try:
        round_no = int(round_result["round"])
        ledger = _lesson_module()
        details = ledger.review_details(
            LESSON_LEDGER_FILE, lifecycle_id, round_no, scope,
        )
        if details is None and scope:
            details = ledger.review_details(
                LESSON_LEDGER_FILE, lifecycle_id, round_no, "",
            )
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return False, f"复盘读取失败: {exc}"
    if not details:
        return False, "缺少当前 Life 的 cycle-review"
    expected_findings = int(round_result.get("findings", 0) or 0)
    expected_credited = int(round_result.get("credited", 0) or 0)
    if int(details.get("finding_count", -1)) != expected_findings:
        return False, (
            f"finding_count={details.get('finding_count')}，"
            f"结算结果={expected_findings}"
        )
    if int(details.get("credited_count", -1)) != expected_credited:
        return False, (
            f"credited_count={details.get('credited_count')}，"
            f"结算结果={expected_credited}"
        )
    fingerprints = details.get("finding_fingerprints") or []
    if len(fingerprints) != expected_credited:
        return False, "finding_fingerprints 数量与 credited_count 不一致"
    if len(fingerprints) != len(set(fingerprints)):
        return False, "finding_fingerprints 存在重复值"
    return True, "ok"

def lesson_status() -> dict:
    if not LESSON_LEDGER_SCRIPT.is_file():
        return {"project": {"status": "missing", "source": "project"},
                "global": {"status": "missing", "source": "global"}}
    ledger = _lesson_module()

    def read_one(path: Path, source: str) -> dict:
        db = path.with_suffix(".sqlite3")
        if not db.is_file():
            return {"status": "missing", "source": source, "database": str(db)}
        try:
            return {"source": source, **ledger.metrics(path)}
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            # Project and global stores have different authority: a malformed
            # optional global store must not paint the project ledger invalid,
            # and every failure must remain visible to status/diagnostics.
            return {
                "status": "invalid", "source": source,
                "database": str(db), "error_type": type(exc).__name__,
                "error": str(exc),
            }

    project = read_one(LESSON_LEDGER_FILE, "project")
    global_metrics = read_one(GLOBAL_LESSON_LEDGER_FILE, "global")
    return {"project": project, "global": global_metrics}
