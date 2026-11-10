#!/usr/bin/env python3
"""cloud_sync/sync_push.py - 云端 V2 push 协议、outbox 幂等入队、重放与冲突处理 (DOC-072)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from core.cloud_sync.cloud_onboard import (
    RECORD_TYPES,
    CloudOnboardError,
    get_incident_list,
    layer_ledger,
    load_project_binding,
    get_pattern_list,
    resolve_store_path,
    response_version_field,
)
from core.runtime.sync_store import DEFAULT_MAX_ATTEMPTS
from core.retrieval.knowledge_sync import is_baseline_seed_pattern, is_baseline_seed_tombstone


def _get_pull_layer():
    from core.cloud_sync.sync_pull import pull_layer
    return pull_layer


# G2'：本地单机计数合并进全设备共享记录时的毒化放大上界（对抗审计条件）。
_TOOL_COUNT_CLAMP = 10 ** 6


def _tool_experience_level_for(lifecycles: int) -> str:
    """独立 lifecycle 数 → 经验等级，与 ledger._tool_experience_level 同口径。"""
    if lifecycles >= 2:
        return "active"
    if lifecycles == 1:
        return "candidate"
    return "unproven"


def _tool_state_version(sync_states: Mapping[tuple[str, str], Mapping[str, Any]],
                        tool_name: str, category: str) -> int:
    """tool_experience 的 sync_states 版本下界：取全部命中键的 max。

    sync_record_state 中 tool 记录并存两个身份域：pull 落地的
    ``tool_name/category`` 与服务端折叠/冲突回写的 ``tool_name``（canonical
    键 ``("tool_experience", tool_name)``）。任一命中都是已知下界，取 max。
    """
    rid_cat = f"{tool_name}/{category}" if category else tool_name
    best = 0
    for key in (("tool_experience", rid_cat), ("tool", rid_cat),
                ("tool_experience", tool_name), ("tool", tool_name)):
        state = sync_states.get(key)
        if not state:
            continue
        try:
            best = max(best, int(state.get("remote_version") or 0))
        except (TypeError, ValueError):
            continue
    return best


def _merge_tool_experience(tools: list[dict[str, Any]],
                           sync_states: Mapping[tuple[str, str], Mapping[str, Any]],
                           ) -> list[dict[str, Any]]:
    """G1'/G2'：tool_experience 版本下界回填 + 信封内按 tool_name 去重合并。

    本地账本按 (tool_name, category) 建模，服务器按 tool_name 折叠；载荷若
    携带重复 record_id，内容哈希永不一致，一条真分歧即拖死整批收敛
    （Round 7 实测 106 条冲突死锁）。合并语义（对抗审计 C 修正版）：
    - use/hit/miss/false_positive/rejected 计数求和，逐项 clamp ≤10^6；
    - independent_lifecycle_count 取 max（COUNT(DISTINCT) 求和会重复计数）；
    - first_observed_at 取 min、last_observed_at 取 max；
    - experience_level 按合并后的独立 lifecycle 数重算，不能任选一行；
    - category 取 use_count 最大行（平局取字典序最小），身份字段同源；
    - record_version = max(各行回填值)，回填值 = max(本地自带, sync_states)；
    - 输入先按 (tool_name, category) 排序：manifest 稳定 → operation_id 幂等。
    """
    rows: list[dict[str, Any]] = []
    for item in tools:
        row = dict(item)
        name = str(row.get("tool_name") or "")
        row["record_version"] = max(
            int(row.get("record_version") or 0),
            _tool_state_version(sync_states, name, str(row.get("category") or "")),
        )
        rows.append(row)
    rows.sort(key=lambda r: (str(r.get("tool_name") or ""),
                             str(r.get("category") or "")))
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("tool_name") or ""), []).append(row)
    merged: list[dict[str, Any]] = []
    for name in sorted(groups):
        members = groups[name]
        head = dict(members[0])
        rep_use = int(head.get("use_count", 0) or 0)
        for row in members[1:]:
            for field in ("use_count", "hit_count", "miss_count",
                          "false_positive_count", "rejected_count"):
                head[field] = min(int(head.get(field, 0) or 0)
                                  + int(row.get(field, 0) or 0),
                                  _TOOL_COUNT_CLAMP)
            head["independent_lifecycle_count"] = max(
                int(head.get("independent_lifecycle_count", 0) or 0),
                int(row.get("independent_lifecycle_count", 0) or 0))
            first = str(head.get("first_observed_at") or "")
            cand = str(row.get("first_observed_at") or "")
            if cand and (not first or cand < first):
                head["first_observed_at"] = cand
            last = str(head.get("last_observed_at") or "")
            cand = str(row.get("last_observed_at") or "")
            if cand > last:
                head["last_observed_at"] = cand
            head["record_version"] = max(int(head.get("record_version") or 0),
                                         int(row.get("record_version") or 0))
            # category（及同源身份字段）取 use_count 最大行；输入已按 category
            # 升序，严格大于才替换 ⇒ 平局保留字典序最小行，结果确定。
            row_use = int(row.get("use_count", 0) or 0)
            if row_use > rep_use:
                rep_use = row_use
                for field in ("category", "tool_version", "device_id",
                              "deleted", "deleted_at"):
                    head[field] = row.get(field)
        head["experience_level"] = _tool_experience_level_for(
            int(head.get("independent_lifecycle_count", 0) or 0))
        merged.append(head)
    return merged


def _private_v2_records(ns, ledger: Path, *, detail_mode: str,
                        sync_states: dict[tuple[str, str], dict[str, Any]] | None = None,
                        layer: str | None = None,
                        policy: str = "allow") -> dict[str, Any]:
    """Apply private-sync policy, then fill the strict V2 wire fields.

    DOC-211：policy 三档（block/redact/allow）。redact 的 span 脱敏必须在
    本函数返回前应用——调用方随后的 chunk manifest/operation_id 都从这里的
    records 派生，脱敏滞后会让幂等键与传输内容错位（审计#1-1）。
    """
    sqlite_path = ledger.with_suffix(".sqlite3")
    if sqlite_path.is_file() and not sqlite_path.is_symlink():
        built = ns["build_private_sync_records"](
            ledger, detail_mode=detail_mode, exclude_baseline=(layer == "project"),
            policy=policy,
        )
        source = built["records"]
        effective_mode = built["detail_mode"]
    else:
        # A freshly bootstrapped test/project can still have only the legacy
        # JSON seed. Preserve the old empty-safe push behavior until the
        # ledger runtime is initialized; real initialized ledgers always use
        # the governed knowledge_sync projection above.
        effective_mode = ns["resolve_detail_mode"](detail_mode)
        raw_patterns = get_pattern_list(ns, ledger)
        raw_incidents = get_incident_list(ns, ledger)
        if layer == "project":
            raw_patterns = [p for p in raw_patterns if not is_baseline_seed_pattern(p)]
        if effective_mode == "summary":
            # summary 降级必须复用 knowledge_sync.strip_to_summary 的单一
            # 字段白名单；legacy JSON 路径此前只清空部分 wire 字段，
            # before_code/after_code/fix_summary/repro_oracle 等详情会漏出网。
            raw_patterns = [ns["strip_to_summary"](item, "pattern")
                            for item in raw_patterns]
            raw_incidents = [ns["strip_to_summary"](item, "incident")
                             for item in raw_incidents]
        source = {"patterns": raw_patterns,
                  "incidents": raw_incidents,
                  "tool_experience": [], "consumption_events": [], "tombstones": []}
    if layer == "project":
        source["patterns"] = [p for p in source["patterns"] if not is_baseline_seed_pattern(p)]
        source["tombstones"] = [t for t in source["tombstones"] if not is_baseline_seed_tombstone(t)]
    patterns = [ns["lesson_to_wire"](item) for item in source["patterns"]]
    pattern_by_id = {
        str(item.get("pattern_id") or item.get("lesson_id")): item
        for item in source["patterns"] if isinstance(item, dict)
    }
    incidents = []
    for raw_incident in source["incidents"]:
        incident = dict(raw_incident)
        parent = pattern_by_id.get(str(
            incident.get("pattern_id") or incident.get("lesson_id") or ""
        ), {})
        for field in ("category", "title", "root_cause", "repro_oracle"):
            if not incident.get(field) and parent.get(field):
                incident[field] = parent[field]
        if not incident.get("error_signature"):
            incident["error_signature"] = incident.get("signature", "")
        incidents.append(ns["incident_to_wire"](incident))
    if effective_mode == "summary":
        for item in patterns:
            for key in ("reflection", "probes", "source_refs", "superseded_by"):
                item[key] = ""
            item["details"] = {}
        for item in incidents:
            for key in ("before_code", "after_code", "decision_chain", "repro_oracle"):
                item[key] = ""
            item["details"] = {}
    tools: list[dict[str, Any]] = []
    for item in source["tool_experience"]:
        tools.append({
            "tool_name": str(item.get("tool_name") or ""),
            "category": str(item.get("category") or ""),
            "experience_level": str(item.get("experience_level") or "unproven"),
            "use_count": int(item.get("use_count", 0) or 0),
            "hit_count": int(item.get("hit_count", 0) or 0),
            "miss_count": int(item.get("miss_count", 0) or 0),
            "false_positive_count": int(item.get("false_positive_count", 0) or 0),
            "rejected_count": int(item.get("rejected_count", 0) or 0),
            "independent_lifecycle_count": int(
                item.get("independent_lifecycle_count", 0) or 0),
            "first_observed_at": str(item.get("first_observed_at") or ""),
            "last_observed_at": str(item.get("last_observed_at") or ""),
            "tool_version": str(item.get("tool_version") or ""),
            "record_version": int(item.get("record_version", 0) or 0),
            "device_id": str(item.get("device_id") or ""),
            "deleted": bool(item.get("deleted", False)),
            "deleted_at": str(item.get("deleted_at") or ""),
        })
    device_id = ns["get_device_id"]()
    tombstones = [{
        "record_type": str(item.get("record_type") or ""),
        "record_id": str(item.get("record_id") or ""),
        "version": int(item.get("version", 0) or 0),
        "deleted_at": str(item.get("deleted_at") or item.get("updated_at") or ""),
        "device_id": str(item.get("device_id") or device_id),
    } for item in source["tombstones"]]
    # patterns/incidents 的版本来自远端同步元数据，而不是把本地事实账本
    # 改造成传输状态表。新记录没有元数据时保持 version=0，由服务端创建 v1。
    sync_states = sync_states or {}
    for record_type, items, id_key in (
        ("patterns", patterns, "lesson_id"),
        ("incidents", incidents, "incident_id"),
    ):
        for item in items:
            state = sync_states.get((record_type, str(item.get(id_key) or ""))) or {}
            version = int(state.get("remote_version") or 0)
            item["version"] = version
    for item in tombstones:
        wire_type = str(item.get("record_type") or "")
        record_type = {
            "pattern": "patterns", "incident": "incidents",
            "tool": "tool_experience",
        }.get(wire_type, wire_type)
        state = sync_states.get((record_type, str(item.get("record_id") or ""))) or {}
        item["version"] = max(int(item.get("version") or 0), int(state.get("remote_version") or 0))
    # G1'/G2'：tool_experience 先做 sync_states 版本下界回填，再按 tool_name
    # 在信封内去重合并（本地 (tool_name, category) 域 → 服务器 tool_name 域）。
    tools = _merge_tool_experience(tools, sync_states)
    records = {"patterns": patterns, "incidents": incidents,
               "tool_experience": tools,
               "consumption_events": [dict(item) for item in source["consumption_events"]],
               "tombstones": tombstones}
    if policy == "block":
        ns["enforce_credential_block"](records, policy="block", hint_exits=True)
    else:
        # DOC-211 redact/allow：span 脱敏+双重复扫+审计在统一入口完成；
        # redact 后的 records 替换必须发生在本函数返回（=manifest 派生）之前。
        from core.cloud_sync.cloud_client import prepare_records_for_sync as _prepare_records
        records = _prepare_records(records, policy=policy)["records"]
    return {"detail_mode": effective_mode, "records": records}


def replay_local_pending_outbox(root: Path, client: Any, ns, account_id: str) -> None:
    """本地模式下仅重放此前已授权的 pending outbox，不拉取新知识、不创建项目。

    每个 scope（global / project）只对已存在绑定的 project_id 检查同账号
    同 scope 同 project 的 pending。成功后幂等退役同域内其他残留 pending，
    失败则保留证据，不伪装成功。
    """
    if not account_id:
        return
    candidates: list[tuple[str, str]] = [("global", "")]
    try:
        pid = load_project_binding(root)
    except CloudOnboardError:
        pid = ""
    if pid:
        candidates.append(("project", pid))
    for layer, proj_id in candidates:
        store_path = resolve_store_path(ns, root, layer)
        store = ns["SyncStore"](store_path)
        try:
            has_pending = store.has_active_operations(
                account_id=account_id, scope=layer, project_id=proj_id,
            )
        finally:
            store.close()
        if not has_pending:
            continue
        try:
            # DOC-211：启动重放是内部自动触发方，不构成"显式参数"——
            # detail_mode/policy 必须吃持久化配置，禁止硬编码 full 旁路
            # summary 承诺（审计#1-5/#2-3）。
            from core.cloud_sync.cloud_client import load_cloud_config as _load_cfg
            try:
                sync_cfg = _load_cfg()
            except ValueError:
                sync_cfg = {}
            result = push_layer(
                client, ns, root, layer, proj_id, account_id,
                detail_mode=sync_cfg.get("sync_detail_mode"),
                sensitive_policy=sync_cfg.get("sensitive_policy") or "allow",
                _conflict_retried=False, _allow_rebase=False,
            )
            if result.get("ok"):
                # 成功后幂等退役同域内其他残留 pending（同一逻辑操作可追踪，旧 pending 不得永久残留）
                retire_store = ns["SyncStore"](store_path)
                try:
                    remaining = retire_store.active_operation_ids(
                        account_id=account_id, scope=layer, project_id=proj_id,
                    )
                    if remaining:
                        scope_state = retire_store.get_scope_state(account_id, layer, proj_id) or {}
                        rv = scope_state.get("remote_version")
                        for oid in remaining:
                            try:
                                retire_store.outbox_ack(oid, remote_version=rv)
                            except Exception:
                                pass
                finally:
                    retire_store.close()
        except Exception:  # noqa: BLE001
            # 保留可恢复证据，不抛错阻断启动
            continue


def _conflict_remote_version(conflict: dict[str, Any]) -> int:
    """conflicts[].remote_version 使用同一类型守卫；缺省视为 0。"""
    value = conflict.get("remote_version")
    if value is None:
        value = 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CloudOnboardError(
            f"响应字段 conflicts[].remote_version 必须是非负整数: {value!r}")
    return value


def _fail_push_envelope(store, operation_id: str, account_id: str, layer: str,
                        project_id: str, detail: str) -> dict[str, Any]:
    """非法响应版本字段按 SYNC_INVALID_ENVELOPE 失败，且绝不推进 remote_version。

    服务端可能已受理该 operation，但信封不可信时本地不能 ack 或写入任何
    版本元数据；operation 保持可重放，等下一次同步与服务端重新对齐。
    """
    try:
        store.outbox_fail(operation_id, "SYNC_INVALID_ENVELOPE", retryable=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        store.record_scope_error(account_id, layer, project_id,
                                 error_code="SYNC_INVALID_ENVELOPE")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": False, "error":
            f"{layer} push failed: code=SYNC_INVALID_ENVELOPE {detail}"}


def _chunk_records(records: Mapping[str, list]) -> list[dict[str, list]]:
    """DOC-210：把 V2 records 确定性分页到服务端单批上限内。

    服务端逐记录 upsert、GC 仅由 tombstone 驱动（快照缺席不删除），多批顺序
    推送的最终状态与单批全量一致。分页保持类别顺序与类别内顺序；批键由
    「批内容 + 批序号」派生，重复同步对已 ack 批幂等跳过。单批（≤上限）不引入
    批序号，与既有 operation_id 逐字节兼容。
    """
    try:
        from core.cloud_sync.cloud_schema import MAX_SYNC_RECORDS as _MAX
    except ImportError:  # pragma: no cover - cloud_schema 与 core 同目录发布
        _MAX = 500
    kinds = ("patterns", "incidents", "tool_experience",
             "consumption_events", "tombstones")
    flat: list[tuple[str, Any]] = []
    for kind in kinds:
        for item in records.get(kind) or []:
            flat.append((kind, item))
    if len(flat) <= _MAX:
        return [dict(records)]
    pages: list[dict[str, list]] = []
    for start in range(0, len(flat), _MAX):
        page = flat[start:start + _MAX]
        chunk: dict[str, list] = {kind: [] for kind in kinds}
        for kind, item in page:
            chunk[kind].append(item)
        pages.append(chunk)
    return pages


def push_layer(client, ns, root: Path, layer: str, project_id: str,
                 account_id: str, *, detail_mode: str,
                 sensitive_policy: str = "allow",
                 _conflict_retried: bool = False,
                 _allow_rebase: bool = True) -> dict[str, Any]:
    # F1 通用硬闸门：任何入口（sync_layers / onboarding outbox 重放 / 未来新增调用）
    # 都必须在网络与 SyncStore 变更之前被拒绝。
    from core.cloud_sync.consent import require_sharing
    require_sharing(f"push {layer}")
    ledger = layer_ledger(ns, root, layer)
    store = ns["SyncStore"](resolve_store_path(ns, root, layer))
    try:
        store.recover_stale_inflight()
        prev = store.get_scope_state(account_id, layer, project_id) or {}
        prepared = _private_v2_records(
            ns, ledger, detail_mode=detail_mode,
            sync_states=store.get_record_states(account_id, layer, project_id),
            layer=layer,
            policy=sensitive_policy,
        )
        records = prepared["records"]
        # DOC-210：服务端单批上限（MAX_SYNC_RECORDS）是硬闸（413）。超限时把
        # V2 records 确定性分批、按序推送：服务端逐记录 upsert 且 GC 仅由
        # tombstone 驱动，多批并集与单批全量等价；批间严格顺序（任一批失败/
        # 退避即停）保证 base 版本栅栏单调推进，不产生自致 409。
        chunks = _chunk_records(records)
        chunked = len(chunks) > 1
        has_pending = store.has_active_operations(
            account_id=account_id, scope=layer, project_id=project_id,
        )
        empty_records = not any((records["patterns"], records["incidents"],
                                 records["tool_experience"],
                                 records["consumption_events"],
                                 records["tombstones"]))
        if empty_records and not has_pending and not _conflict_retried:
            already_acked = store.has_acked_payload(
                account_id=account_id, scope=layer, project_id=project_id,
                payload_hash=ns["manifest_hash"](records),
            )
            if already_acked:
                return {"ok": True, "skipped": True,
                        "detail": f"{layer} push skipped: unchanged empty record set"}
        # operation/snapshot 必须包含最终版本化 wire 内容；版本改变意味着
        # 一个新的 outbox operation，不能复用尚未 ack 的旧 payload。
        state_hash = str(ns["read_ledger"](ledger).get("state_hash") or "")
        chunk_op_ids: set[str] = set()
        chunk_manifests: set[str] = set()
        chunk_notes: list[str] = []
        policy_note = f" policy={sensitive_policy}" if sensitive_policy != "allow" else ""
        for chunk_index, chunk in enumerate(chunks):
            patterns = chunk["patterns"]
            incidents = chunk["incidents"]
            tools = chunk["tool_experience"]
            consumption_events = chunk["consumption_events"]
            tombstones = chunk["tombstones"]
            manifest = ns["manifest_hash"](chunk)
            base_version = int((store.get_scope_state(
                account_id, layer, project_id) or {}).get("remote_version") or 0)
            # operation/snapshot 必须包含最终版本化 wire 内容；版本改变意味着
            # 一个新的 outbox operation，不能复用尚未 ack 的旧 payload。
            state_hash = str(ns["read_ledger"](ledger).get("state_hash") or "")
            chunk_tag = (f"|chunk={chunk_index + 1}/{len(chunks)}"
                         if chunked else "")
            digest = hashlib.sha256(
                f"v2|{account_id}|{layer}|{project_id}|{state_hash}|{manifest}|"
                f"base={base_version}|retry={int(_conflict_retried)}{chunk_tag}".encode("utf-8")
            ).hexdigest()
            operation_id = "op-" + digest[:32]
            snapshot_id = "snap-" + digest[32:]
            chunk_op_ids.add(operation_id)
            chunk_manifests.add(manifest)
            row, _created = store.outbox_enqueue(
                operation_id=operation_id, account_id=account_id, scope=layer,
                project_id=project_id, payload_hash=manifest, base_version=base_version,
                local_version=(len(patterns) + len(incidents) + len(tools)
                               + len(consumption_events)))
            if row["state"] == "acked":
                if chunked:
                    chunk_notes.append(
                        f"chunk {chunk_index + 1}/{len(chunks)} acked skip")
                    continue
                return {"ok": True, "detail": f"idempotent skip op={operation_id} (acked)"}
            if row["state"] == "dead_letter":
                # A dead-letter is a stopped attempt, not a permanent tombstone.
                # Explicit sync after server/auth/tool recovery must be able to
                # retry the same deterministic operation without changing its
                # idempotency identity.
                row = store.outbox_requeue(operation_id)
            if row["state"] == "pending" and row.get("next_retry_at"):
                due = store.due_operations(
                    account_id=account_id, scope=layer, project_id=project_id,
                )
                if not any(item["operation_id"] == operation_id for item in due):
                    code = row.get("last_error_code") or "SYNC_RETRY_BACKOFF"
                    store.record_scope_error(
                        account_id, layer, project_id, error_code=code,
                    )
                    return {
                        "ok": False,
                        "error": f"{layer} push deferred: code={code} "
                                 f"retry_at={row['next_retry_at']}",
                    }
            store.outbox_mark_inflight(operation_id)
            res = client.push_knowledge(
                layer, project_id=project_id, lessons=patterns, incidents=incidents,
                tool_experience=tools, consumption_events=consumption_events,
                tombstones=tombstones, snapshot_id=snapshot_id,
                operation_id=operation_id, base_version=base_version,
                sensitive_policy=sensitive_policy)
            if not res.ok:
                err = client.sync_error(res)
                is_conflict = res.status == 409 or err.code == "SYNC_VERSION_CONFLICT"
                if err.code == "SYNC_SCOPE_DELETED":
                    store.record_scope_error(
                        account_id, layer, project_id,
                        error_code=err.code, request_id=err.request_id or None,
                    )
                    return {
                        "ok": True, "scope_status": "deleted", "skipped": True,
                        "detail": f"{layer} scope 已删除，跳过 push",
                    }
                store.outbox_fail(
                    operation_id, err.code or f"HTTP_{res.status}",
                    # Keep a 409 replayable while the bounded pull/rebase/replay
                    # path is in progress. Network/5xx outages stay pending until
                    # recovery; protocol/conflict retries remain bounded.
                    retryable=err.retryable or is_conflict,
                    retry_after_seconds=err.retry_after_seconds,
                    max_attempts=(None if err.retryable and not is_conflict
                                  else DEFAULT_MAX_ATTEMPTS),
                )
                if not _conflict_retried and is_conflict and _allow_rebase:
                    # A 409 is a version fence, not a transient HTTP retry. Pull
                    # the authoritative remote cursor first, then replay the same
                    # local layer once. The outbox row remains pending if either
                    # the rebase or the single replay fails.
                    rebased = _get_pull_layer()(client, ns, root, layer, project_id, account_id)
                    if rebased.get("ok"):
                        retry = push_layer(
                            client, ns, root, layer, project_id, account_id,
                            detail_mode=detail_mode,
                            sensitive_policy=sensitive_policy,
                            _conflict_retried=True,
                            _allow_rebase=True,
                        )
                        if retry.get("ok"):
                            # 同一逻辑操作可追踪：重放成功后幂等退役/关联原始 pending，不让旧 pending 永久残留
                            try:
                                retire_store = ns["SyncStore"](resolve_store_path(ns, root, layer))
                                try:
                                    orig = retire_store.get_operation(operation_id)
                                    if orig and orig["state"] in ("pending", "inflight"):
                                        cur_state = retire_store.get_scope_state(account_id, layer, project_id) or {}
                                        rv = cur_state.get("remote_version")
                                        retire_store.outbox_ack(operation_id, remote_version=rv)
                                finally:
                                    retire_store.close()
                            except Exception:
                                pass
                        retry["detail"] = (
                            "409 后 pull/rebase/replay："
                            + (retry.get("detail") or retry.get("error", "失败"))
                        )
                        return retry
                # 426：V1 旧协议/V2 schema 缺失，服务端明确要求升级客户端
                if res.status == 426 or err.code == "SYNC_PROTOCOL_UPGRADE_REQUIRED":
                    upgrade_hint = (
                        f"请升级 BugX 客户端至 {err.min_client_version or '0.6.1'}+ 并重建全局引擎。\n"
                        f"  推荐执行：bugx update\n"
                        f"  或手动执行：pip install --upgrade bugx-cli && bugx install --force"
                    )
                    store.record_scope_error(account_id, layer, project_id,
                                             error_code=err.code or "SYNC_PROTOCOL_UPGRADE_REQUIRED",
                                             request_id=err.request_id or None)
                    return {"ok": False, "error":
                            f"{layer} push failed: 协议过期 (426 {err.code}) {err.message or res.error}；{upgrade_hint} (required_schema={err.required_schema_version or 2})"}
                req_id_str = f" (request_id={err.request_id})" if err.request_id else ""
                store.record_scope_error(account_id, layer, project_id,
                                         error_code=err.code or f"HTTP_{res.status}",
                                         request_id=err.request_id or None)
                return {"ok": False, "error":
                        f"{layer} push failed: code={err.code}{req_id_str} {err.message or res.error}"}
            data = client._data(res) if hasattr(client, "_data") else {}
            try:
                push_snapshot_version = response_version_field(data)
            except CloudOnboardError as exc:
                return _fail_push_envelope(
                    store, operation_id, account_id, layer, project_id, str(exc),
                )
            conflicts = data.get("conflicts") if isinstance(data, dict) else None
            if isinstance(conflicts, list) and conflicts:
                def _is_same_content_conflict(c: dict) -> bool:
                    if not isinstance(c, dict):
                        return False
                    reason = str(c.get("reason") or "")
                    lhash = str(c.get("local_hash") or "")
                    rhash = str(c.get("remote_hash") or "")
                    return reason == "stale_version" and bool(lhash) and lhash == rhash

                replayable_same_hash = all(_is_same_content_conflict(c) for c in conflicts)
                stale_updates = []
                for conflict in conflicts:
                    if not isinstance(conflict, dict):
                        continue
                    record_type = str(conflict.get("record_type") or "unknown")
                    canonical_type = {
                        "pattern": "patterns", "patterns": "patterns",
                        "incident": "incidents", "incidents": "incidents",
                        "tool": "tool_experience", "tool_experience": "tool_experience",
                    }.get(record_type, record_type)
                    record_id = str(conflict.get("record_id") or "unknown")
                    try:
                        rv = _conflict_remote_version(conflict)
                    except CloudOnboardError as exc:
                        return _fail_push_envelope(
                            store, operation_id, account_id, layer, project_id, str(exc),
                        )
                    reason = str(conflict.get("reason") or "remote_conflict")
                    if reason == "stale_version" and rv > 0:
                        stale_updates.append({
                            "record_type": canonical_type,
                            "record_id": record_id,
                            "remote_version": rv,
                            "remote_hash": str(conflict.get("remote_hash") or ""),
                        })
                    store.record_conflict(
                        account_id=account_id, scope=layer, project_id=project_id,
                        record_type=canonical_type, record_id=record_id,
                        local_version=int(conflict.get("local_version") or 0),
                        remote_version=rv,
                        local_hash=str(conflict.get("local_hash") or ""),
                        remote_hash=str(conflict.get("remote_hash") or ""),
                        reason=reason,
                    )
                if stale_updates:
                    store.upsert_record_states(account_id, layer, project_id, stale_updates)
                # 服务端已提交该 operation 的结果。确认 outbox，避免每次启动
                # 对同一批次无限重放；冲突本身留在本地队列等待治理。
                store.outbox_ack(
                    operation_id,
                    remote_version=push_snapshot_version or None,
                )
                if not replayable_same_hash and any(_is_same_content_conflict(c) for c in conflicts):
                    store.prune_converged_conflicts(account_id, layer, project_id, resolver_identity="sync-auto-heal")
                if replayable_same_hash and not _conflict_retried:
                    # HTTP 200 partial_conflict 已经提交了本批 operation，不能
                    # 复用旧 operation_id。pull 一次权威版本后，以 retry 标记
                    # 派生新的幂等键，最多自动重放一次，避免死循环。
                    rebased = _get_pull_layer()(client, ns, root, layer, project_id, account_id)
                    if rebased.get("ok"):
                        retry = push_layer(
                            client, ns, root, layer, project_id, account_id,
                            detail_mode=detail_mode,
                            sensitive_policy=sensitive_policy,
                            _conflict_retried=True,
                            _allow_rebase=False,
                        )
                        store.prune_converged_conflicts(account_id, layer, project_id, resolver_identity="sync-auto-replay")
                        if retry.get("ok"):
                            retry["detail"] = (
                                "200 stale_version 同哈希自动 pull/replay："
                                + (retry.get("detail") or "成功")
                            )
                            return retry
                # 若全部冲突均为零分歧的同哈希 stale_version，自动修剪并收敛为成功
                if replayable_same_hash:
                    store.prune_converged_conflicts(account_id, layer, project_id, resolver_identity="sync-auto-heal")
                    return {
                        "ok": True,
                        "detail": f"同内容版本自动对齐: 已自愈收敛 {len(conflicts)} 条记录，冲突已清除",
                    }
                store.record_scope_error(
                    account_id, layer, project_id,
                    error_code="SYNC_PARTIAL_CONFLICT",
                )
                return {
                    "ok": False, "committed": True, "partial_conflict": True,
                    "conflicts": len(conflicts),
                    "error": f"{layer} push 返回 {len(conflicts)} 条记录冲突，已入本地冲突队列",
                }
            # snapshot_version 已通过 response_version_field 类型守卫；
            # 旧网关可能只回 version 字段，二者都没有时保持 None 不推进版本。
            store.outbox_ack(operation_id,
                             remote_version=push_snapshot_version or None)
            if push_snapshot_version:
                # S3：仅 clean-200（零冲突）路径把响应 snapshot_version 以 MAX
                # 语义回写 scope_state，消除"base 永远慢一拍"的 409/rebase 循环；
                # 200-partial（上方 return）/SYNC_INVALID_ENVELOPE/失败路径一律
                # 不回写，committed_cursor/committed_snapshot_id 不受影响
                # （红队 BLOCK 约束 ①②③）。
                store.advance_remote_version(account_id, layer, project_id,
                                             push_snapshot_version)
            if not chunked:
                return {"ok": True, "detail":
                        f"status={res.status} patterns={len(patterns)} incidents={len(incidents)} "
                        f"tool_experience={len(tools)} consumption_events={len(consumption_events)} "
                        f"tombstones={len(tombstones)} "
                        f"detail_mode={prepared['detail_mode']}{policy_note} op={operation_id} base={base_version}"}
            chunk_notes.append(
                f"chunk {chunk_index + 1}/{len(chunks)}: status={res.status} "
                f"patterns={len(patterns)} incidents={len(incidents)} "
                f"tool_experience={len(tools)} "
                f"consumption_events={len(consumption_events)} "
                f"tombstones={len(tombstones)} op={operation_id}")
        # DOC-210：分批全部落账后，把分批改造前遗留的陈旧 pending/inflight
        # operation（旧全量 payload，内容已被本批并集覆盖）幂等退役，避免
        # has_active_operations 永久为真。
        retired_legacy: list[str] = []
        try:
            for legacy_id in store.active_operation_ids(
                    account_id=account_id, scope=layer,
                    project_id=project_id):
                if legacy_id in chunk_op_ids:
                    continue
                legacy = store.get_operation(legacy_id) or {}
                if str(legacy.get("payload_hash") or "") in chunk_manifests:
                    continue
                store.outbox_ack(legacy_id)
                retired_legacy.append(legacy_id)
        except Exception:
            retired_legacy = []
        detail = (f"chunked push ok: batches={len(chunks)} "
                  f"detail_mode={prepared['detail_mode']}{policy_note} " + "; ".join(chunk_notes))
        if retired_legacy:
            detail += f"; retired legacy ops={','.join(retired_legacy)}"
        return {"ok": True, "detail": detail}
    finally:
        store.close()

