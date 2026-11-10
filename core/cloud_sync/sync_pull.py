#!/usr/bin/env python3
"""cloud_sync/sync_pull.py - 云端 V2 pull 协议、inbox 校验、快照暂存与原子导入 (DOC-072)."""

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
    landing_path,
    landing_store,
    layer_ledger,
    resolve_store_path,
    ensure_ledger,
    response_version_field,
)


def _fail_pull(store, account_id: str, layer: str, project_id: str,
               snapshot_id: str, code: str, message: str) -> dict[str, Any]:
    if snapshot_id:
        try:
            store.inbox_mark_rejected(snapshot_id, code)
        except Exception:  # noqa: BLE001
            pass
    try:
        store.record_scope_error(account_id, layer, project_id, error_code=code)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": False, "error": f"{layer} pull failed: [{code}] {message}"}


def pull_layer(client, ns, root: Path, layer: str, project_id: str,
                account_id: str, *,
                resolve_converged_conflicts: bool = False,
                force_baseline: bool = False) -> dict[str, Any]:
    # F1 通用硬闸门：与 _push_layer 同语义，撤销态下禁止任何 pull。
    from core.cloud_sync.consent import require_sharing
    require_sharing(f"pull {layer}")
    store = ns["SyncStore"](resolve_store_path(ns, root, layer))
    try:
        state = store.get_scope_state(account_id, layer, project_id) or {}
        cursor_before = str(state.get("committed_cursor") or "")
        cursor = cursor_before
        snapshot_id = ""
        snapshot_manifest_hash = ""
        snapshot_version = 0
        last_non_empty_cursor = ""
        merged: dict[str, list[dict[str, Any]]] = {kind: [] for kind in RECORD_TYPES}
        seen_cursors: set[str] = set()
        pages = 0
        max_pages = getattr(client, "max_pull_pages", 1000)
        resync_retried = False
        scope_status = "active"
        # 服务端可能对同一 committed cursor 重放同一 snapshot（幂等收敛）；
        # 已完成校验的 inbox 快照不可改写，只能走幂等导入/提交路径。
        stageable = True

        while True:
            res = client.pull_knowledge(
                layer, project_id=project_id, cursor=cursor,
                device_id="" if force_baseline and not cursor else None,
            )
            if not res.ok:
                err = client.sync_error(res)
                code = err.code or f"HTTP_{res.status}"
                message = err.message or str(res.error)
                cursor_reset_required = (
                    code == "SYNC_RESYNC_REQUIRED"
                    or (
                        code == "SYNC_INVALID_ENVELOPE"
                        and bool(cursor)
                        and "cursor" in message.casefold()
                    )
                )
                if cursor_reset_required and not resync_retried:
                    scope_missing = "服务端 scope 状态缺失" in message
                    try:
                        # 当服务端明确提示 scope 状态缺失时，保护本地已有离线事实；
                        # 其他常规 cursor_reset_required（如 tombstone floor/游标校验失败）执行标准 scope reset
                        if not scope_missing:
                            ledger = ensure_ledger(ns, layer_ledger(ns, root, layer))
                            ns["reset_cloud_scope"](ledger, reason=code)
                        store.reset_scope(
                            account_id, layer, project_id,
                            error_code=code, request_id=err.request_id or None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        return _fail_pull(
                            store, account_id, layer, project_id, snapshot_id,
                            code, f"scope reset 失败: {type(exc).__name__}: {exc}",
                        )
                    resync_retried = True
                    state = store.get_scope_state(account_id, layer, project_id) or {}
                    cursor_before = ""
                    cursor = ""
                    snapshot_id = ""
                    snapshot_manifest_hash = ""
                    snapshot_version = 0
                    last_non_empty_cursor = ""
                    merged = {kind: [] for kind in RECORD_TYPES}
                    seen_cursors = set()
                    pages = 0
                    stageable = True
                    scope_status = "active"
                    continue
                if res.status == 426 or err.code == "SYNC_PROTOCOL_UPGRADE_REQUIRED":
                    message = (
                        f"协议过期 (426 {err.code}) {message}；"
                        f"请升级 BugX 客户端至 {err.min_client_version or '0.6.1'}+ 并重建全局引擎 "
                        f"(required_schema={err.required_schema_version or 2})。"
                        f"\n  推荐执行：bugx update"
                    )
                return _fail_pull(store, account_id, layer, project_id,
                                   snapshot_id, code, message)
            data = client._data(res)
            page_scope_status = data.get("scope_status", "active")
            if page_scope_status not in {"active", "deleted"}:
                return _fail_pull(
                    store, account_id, layer, project_id, snapshot_id,
                    "SYNC_INVALID_ENVELOPE", "响应 scope_status 无效",
                )
            if pages and page_scope_status != scope_status:
                return _fail_pull(
                    store, account_id, layer, project_id, snapshot_id,
                    "SYNC_INVALID_ENVELOPE", "同一 pull 会话内 scope_status 发生变化",
                )
            scope_status = page_scope_status
            sid = str(data.get("snapshot_id") or "")
            if not sid:
                return _fail_pull(store, account_id, layer, project_id,
                                  snapshot_id, "SYNC_INVALID_ENVELOPE",
                                  "响应缺少 snapshot_id")
            if snapshot_id and sid != snapshot_id:
                return _fail_pull(store, account_id, layer, project_id,
                                  snapshot_id, "SYNC_INVALID_ENVELOPE",
                                  "同一 pull 会话内 snapshot_id 发生变化")
            snapshot_id = sid
            snap_state = (store.get_snapshot(snapshot_id) or {}).get("state")
            # validated/imported snapshots are crash-recovery artifacts.  Do
            # not call inbox_stage_snapshot again: it would downgrade a
            # validated snapshot to rejected and strand the old cursor.
            stageable = stageable and snap_state in (None, "staged")
            page_records = {kind: list((data.get("records") or {}).get(kind) or [])
                            for kind in RECORD_TYPES}
            for kind in RECORD_TYPES:
                merged[kind].extend(page_records[kind])
            try:
                # snapshot_version 与 push 响应共用同一类型守卫；该值最终
                # 进入 inbox_commit_import 的 remote_version，浮点/字符串
                # 数字不得被 int() 静默截断后推进本地基线。
                page_version = response_version_field(data, ("snapshot_version",))
            except CloudOnboardError as exc:
                return _fail_pull(store, account_id, layer, project_id,
                                  snapshot_id, "SYNC_INVALID_ENVELOPE", str(exc))
            if page_version is not None:
                snapshot_version = max(snapshot_version, page_version)
            # 每页即暂存 inbox（staged 可追加），中断后可用于诊断或安全清理；
            # 已 finalized 的快照重放时跳过暂存，仅走幂等收敛。
            if stageable:
                try:
                    store.inbox_stage_snapshot(
                        snapshot_id=snapshot_id, account_id=account_id, scope=layer,
                        project_id=project_id,
                        # Some deployed servers expose a page hash rather than the
                        # complete snapshot hash.  The page hash is still checked
                        # by validate_records/manifest_hash after all pages merge;
                        # do not treat different page hashes as an inbox overwrite.
                        manifest_hash="",
                        cursor_before=cursor_before,
                        cursor_after=str(data.get("next_cursor") or ""),
                        records=page_records)
                except Exception as exc:  # noqa: BLE001
                    code = (
                        "SYNC_DUPLICATE_RECORD_CONFLICT"
                        if "记录" in str(exc) and "内容冲突" in str(exc)
                        else "SYNC_INVALID_SNAPSHOT"
                    )
                    return _fail_pull(
                        store, account_id, layer, project_id, snapshot_id,
                        code, str(exc),
                    )
            pages += 1
            next_cursor = str(data.get("next_cursor") or "")
            if next_cursor:
                last_non_empty_cursor = next_cursor
            if not data.get("has_more"):
                break
            if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
                return _fail_pull(store, account_id, layer, project_id,
                                  snapshot_id, "SYNC_INVALID_ENVELOPE",
                                  "分页 cursor 重复或为空")
            if pages >= max_pages:
                return _fail_pull(store, account_id, layer, project_id,
                                  snapshot_id, "SYNC_INVALID_ENVELOPE",
                                  "超过最大分页数")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        # ---- 全量校验后过滤：跨页去重、tombstone 优先、版本感知导入 ----
        tomb_index: dict[tuple[str, str], int] = {}
        for tomb in merged["tombstones"]:
            key = (str(tomb.get("record_type") or ""), str(tomb.get("record_id") or ""))
            try:
                version = int(tomb.get("version") or 0)
            except (TypeError, ValueError):
                return _fail_pull(
                    store, account_id, layer, project_id, snapshot_id,
                    "SYNC_INVALID_RECORD", "tombstone version 无效",
                )
            tomb_index[key] = max(tomb_index.get(key, 0), version)

        def dedupe(record_type: str, items: list[dict[str, Any]], id_keys: tuple[str, ...]):
            """Deduplicate pages without letting arrival order lose updates."""
            def comparable(record: dict[str, Any]) -> dict[str, Any]:
                # event_id is the idempotency key.  Producers may timestamp the
                # same event on different devices at slightly different times;
                # that must not turn a retry into a pull conflict.
                if record_type == "consumption_events":
                    return {key: value for key, value in record.items()
                            if key != "created_at"}
                return record

            seen: dict[str, dict[str, Any]] = {}
            duplicates = 0
            conflicts: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
            for item in items:
                if not isinstance(item, dict):
                    raise CloudOnboardError("云端记录必须是对象")
                rid = "|".join(str(item.get(k) or "") for k in id_keys)
                if not rid.strip("|"):
                    raise CloudOnboardError("云端记录缺少稳定主键")
                previous = seen.get(rid)
                if previous is None:
                    seen[rid] = item
                    continue
                previous_hash = _content_hash(ns, comparable(previous))
                item_hash = _content_hash(ns, comparable(item))
                try:
                    previous_version = int(previous.get("version", previous.get("record_version", 0)) or 0)
                    item_version = int(item.get("version", item.get("record_version", 0)) or 0)
                except (TypeError, ValueError) as exc:
                    raise CloudOnboardError(f"云端记录 {rid} version 无效") from exc
                if previous_hash == item_hash:
                    duplicates += 1
                elif item_version > previous_version:
                    seen[rid] = item
                    duplicates += 1
                elif item_version < previous_version:
                    duplicates += 1
                else:
                    conflicts.append((record_type, rid, previous, item))
            return seen, duplicates, conflicts

        patterns, dup_p, conflicts_p = dedupe("patterns", merged["patterns"], ("lesson_id",))
        incidents, dup_i, conflicts_i = dedupe("incidents", merged["incidents"], ("incident_id",))
        tools, dup_t, conflicts_t = dedupe(
            "tool_experience", merged["tool_experience"], ("tool_name", "category"),
        )
        consumption_events, dup_e, conflicts_e = dedupe(
            "consumption_events", merged["consumption_events"], ("event_id",),
        )
        duplicate_conflicts = conflicts_p + conflicts_i + conflicts_t + conflicts_e
        if duplicate_conflicts:
            for record_type, rid, previous, incoming in duplicate_conflicts:
                store.record_conflict(
                    account_id=account_id, scope=layer, project_id=project_id,
                    record_type=record_type, record_id=rid,
                    local_version=int(previous.get("version", previous.get("record_version", 0)) or 0),
                    remote_version=int(incoming.get("version", incoming.get("record_version", 0)) or 0),
                    local_hash=_content_hash(ns, previous),
                    remote_hash=_content_hash(ns, incoming),
                    reason="duplicate_same_version_different_content",
                )
            return _fail_pull(
                store, account_id, layer, project_id, snapshot_id,
                "SYNC_DUPLICATE_RECORD_CONFLICT",
                f"发现 {len(duplicate_conflicts)} 条同版本不同内容的重复云端记录",
            )
        dup_total = dup_p + dup_i + dup_t + dup_e

        try:
            # Pull responses are page envelopes.  The receipt must bind the
            # complete, ordered-as-received records rather than a single page
            # hash, otherwise a partial/tampered landing file can be replayed
            # as if it were the full snapshot.
            snapshot_manifest_hash = ns["manifest_hash"](merged)
        except Exception as exc:  # noqa: BLE001
            return _fail_pull(
                store, account_id, layer, project_id, snapshot_id,
                "SYNC_MANIFEST_MISMATCH", str(exc),
            )

        current_remote_version = int(state.get("remote_version") or 0)
        current_snapshot_id = str(state.get("committed_snapshot_id") or "")
        # Cursors are opaque and cannot be ordered locally.  A lower remote
        # version may only be the exact replay of the already committed
        # snapshot/cursor; any different cursor would roll the scope back.
        if snapshot_version < current_remote_version and not (
            snapshot_id == current_snapshot_id
            and (last_non_empty_cursor or cursor_before) == cursor_before
        ):
            return _fail_pull(
                store, account_id, layer, project_id, snapshot_id,
                "SYNC_CURSOR_REGRESSION",
                "snapshot_version 低于已提交版本且不是同一 snapshot/cursor 的幂等重放",
            )

        def tombstoned(wire_type: str, rid: str, version: Any) -> bool:
            aliases = {
                "patterns": ("pattern", "patterns"),
                "incidents": ("incident", "incidents"),
                "tool_experience": ("tool_experience", "tool"),
            }.get(wire_type, (wire_type,))
            try:
                record_version = int(version or 0)
            except (TypeError, ValueError):
                return True
            return any(
                tomb_index.get((alias, rid), -1) >= record_version
                for alias in aliases
            )

        # ---- 冲突感知的幂等导入（私有同步保留远端 active 状态）----
        ledger = ensure_ledger(ns, layer_ledger(ns, root, layer))
        local_payload = ns["read_ledger"](ledger)
        local_patterns = (local_payload.get("lessons") or {})
        local_incidents = {str(i.get("incident_id")): i
                           for i in _safe_incidents(ns, ledger)}
        remote_states = store.get_record_states(account_id, layer, project_id)
        metadata_updates: dict[tuple[str, str], dict[str, Any]] = {}
        pending_patterns: list[dict[str, Any]] = []
        pending_incidents: list[dict[str, Any]] = []
        pending_tools: list[dict[str, Any]] = []
        pending_consumption_events: list[dict[str, Any]] = []
        pending_tombstones: list[dict[str, Any]] = []
        conflicts = skipped_tomb = skipped_deleted = skipped_invalid = 0
        imported_p = imported_i = imported_tools = dup_local = 0
        converged_records: list[dict[str, Any]] = []
        # 与 import_synced_records 同源的校验器：远端单条脏数据就地隔离，
        # 不再让整层导入被一条记录卡死（与 bulk 导入的容错口径一致）。
        _validate_synced_pattern = ns.get("synced_pattern")
        _validate_synced_incident = ns.get("synced_incident")

        def remember(record_type: str, record_id: str, item: Mapping[str, Any], *, deleted: bool = False) -> None:
            raw_version = item.get("version", item.get("record_version", 0))
            try:
                version = int(raw_version or 0)
            except (TypeError, ValueError):
                raise CloudOnboardError(
                    f"云端 {record_type}/{record_id} version 无效"
                )
            metadata_updates[(record_type, record_id)] = {
                "record_type": record_type, "record_id": record_id,
                "remote_version": version,
                "remote_hash": _content_hash(ns, item) if not deleted else "",
                "remote_deleted": deleted,
            }

        def record_conflict(record_type: str, record_id: str,
                            local: Mapping[str, Any] | None,
                            remote: Mapping[str, Any], reason: str) -> None:
            nonlocal conflicts
            previous = remote_states.get((record_type, record_id)) or {}
            store.record_conflict(
                account_id=account_id, scope=layer, project_id=project_id,
                record_type=record_type, record_id=record_id,
                local_version=int(previous.get("remote_version") or 0),
                remote_version=int(remote.get("version", remote.get("record_version", 0)) or 0),
                local_hash=_content_hash(ns, local or {}),
                remote_hash=_content_hash(ns, remote), reason=reason,
            )
            conflicts += 1

        def mark_converged(record_type: str, record_id: str) -> None:
            observed = metadata_updates.get((record_type, record_id))
            if observed:
                converged_records.append(dict(observed))

        def _core_projection(rec: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "category": str(rec.get("category") or "").strip().lower(),
                "title": str(rec.get("title") or "").strip(),
                "root_cause": str(rec.get("root_cause") or "").strip(),
                "status": str(rec.get("status") or "active").strip().lower(),
            }

        def same_content_pattern(local: Mapping[str, Any], remote: Mapping[str, Any]) -> bool:
            local_wire = ns["lesson_to_wire"](dict(local))
            if _content_projection(local_wire) == _content_projection(remote):
                return True
            # Summary / Full 投影等价性检查：
            # 若核心字段（category, title, root_cause, status 等）完全一致，
            # 说明两端属于同一知识记录的不同模式投影（Full 详情 vs Summary 摘要），非内容分歧！
            return _core_projection(local_wire) == _core_projection(remote)

        def same_content_incident(local: Mapping[str, Any], remote: Mapping[str, Any]) -> bool:
            local_wire = ns["incident_to_wire"](dict(local))
            if _content_projection(local_wire) == _content_projection(remote):
                return True
            return _core_projection(local_wire) == _core_projection(remote)

        for rid, item in patterns.items():
            remember("patterns", rid, item, deleted=bool(item.get("deleted")))
            if tombstoned("patterns", rid, item.get("version")):
                skipped_tomb += 1
                continue
            if item.get("deleted"):
                pending_tombstones.append({
                    "record_type": "pattern", "record_id": rid,
                    "version": int(item.get("version") or 0),
                    "final_status": item.get("final_status") or "revoked",
                })
                skipped_deleted += 1
                continue
            if _validate_synced_pattern is not None:
                try:
                    _validate_synced_pattern(dict(item), allow_remote_active=True)
                except Exception as exc:  # noqa: BLE001
                    skipped_invalid += 1
                    continue
            local = local_patterns.get(rid)
            if local is not None:
                if same_content_pattern(local, item):
                    merged_item = dict(item)
                    if local.get("before_code") and not merged_item.get("before_code"):
                        merged_item["before_code"] = local.get("before_code") or ""
                        merged_item["after_code"] = local.get("after_code") or ""
                        merged_item["reflection"] = local.get("reflection") or ""
                        merged_item["probes"] = local.get("probes") or ""
                    pending_patterns.append(merged_item)
                    mark_converged("patterns", rid)
                    dup_local += 1
                    continue
                record_conflict("patterns", rid, local, item, "content_diverged")
                continue
            pending_patterns.append(item)
            mark_converged("patterns", rid)

        for rid, item in incidents.items():
            remember("incidents", rid, item, deleted=bool(item.get("deleted")))
            if tombstoned("incidents", rid, item.get("version")):
                skipped_tomb += 1
                continue
            if item.get("deleted"):
                pending_tombstones.append({
                    "record_type": "incident", "record_id": rid,
                    "version": int(item.get("version") or 0),
                    "final_status": item.get("final_status") or "revoked",
                })
                skipped_deleted += 1
                continue
            if _validate_synced_incident is not None:
                try:
                    _validate_synced_incident(dict(item), allow_remote_active=True)
                except Exception:  # noqa: BLE001
                    skipped_invalid += 1
                    continue
            local = local_incidents.get(rid)
            if local is not None:
                if same_content_incident(local, item):
                    merged_item = dict(item)
                    if local.get("before_code") and not merged_item.get("before_code"):
                        merged_item["before_code"] = local.get("before_code") or ""
                        merged_item["after_code"] = local.get("after_code") or ""
                        merged_item["decision_chain"] = local.get("decision_chain") or ""
                    pending_incidents.append(merged_item)
                    mark_converged("incidents", rid)
                    dup_local += 1
                    continue
                record_conflict("incidents", rid, local, item, "content_diverged")
                continue
            pending_incidents.append(item)
            mark_converged("incidents", rid)

        for item in tools.values():
            rid = f"{item.get('tool_name') or ''}/{item.get('category') or ''}"
            remember("tool_experience", rid, item, deleted=bool(item.get("deleted")))
            if tombstoned("tool_experience", rid,
                          item.get("record_version")):
                skipped_tomb += 1
                continue
            pending_tools.append(item)
            if not item.get("deleted"):
                mark_converged("tool_experience", rid)

        pending_consumption_events.extend(consumption_events.values())

        # 对独立 tombstone 做保守删除：只有本地内容仍等于上次远端快照时才
        # 应用；陌生本地记录必须进入冲突队列，不能被另一设备静默抹掉。
        for tomb in merged["tombstones"]:
            wire_type = str(tomb.get("record_type") or "")
            rid = str(tomb.get("record_id") or "")
            record_type = {"pattern": "patterns", "incident": "incidents"}.get(
                wire_type, wire_type,
            )
            if wire_type == "tool_experience":
                tool_name, separator, category = rid.partition("/")
                if not separator or not tool_name or not category:
                    return _fail_pull(
                        store, account_id, layer, project_id, snapshot_id,
                        "SYNC_INVALID_RECORD",
                        "tool_experience tombstone.record_id 必须为 tool_name/category",
                    )
                version = int(tomb.get("version") or 0)
                previous = remote_states.get(("tool_experience", rid)) or {}
                if version < int(previous.get("remote_version") or 0):
                    skipped_tomb += 1
                    continue
                pending_tools.append({
                    "tool_name": tool_name, "category": category,
                    "experience_level": "candidate", "use_count": 0,
                    "hit_count": 0, "miss_count": 0,
                    "false_positive_count": 0, "rejected_count": 0,
                    "independent_lifecycle_count": 0,
                    "first_observed_at": "", "last_observed_at": "",
                    "tool_version": "", "record_version": version,
                    "device_id": str(tomb.get("device_id") or ""),
                    "deleted": True,
                    "deleted_at": str(tomb.get("deleted_at") or ""),
                })
                remember("tool_experience", rid, tomb, deleted=True)
                continue
            if record_type not in {"patterns", "incidents"} or not rid:
                continue
            local = local_patterns.get(rid) if record_type == "patterns" \
                else local_incidents.get(rid)
            remember(record_type, rid, tomb, deleted=True)
            if local is None:
                continue
            previous = remote_states.get((record_type, rid)) or {}
            if record_type == "patterns":
                local_wire = ns["lesson_to_wire"](dict(local))
            else:
                local_wire = ns["incident_to_wire"](dict(local))
            local_hash = _content_hash(ns, local_wire)
            if not previous.get("remote_hash"):
                record_conflict(record_type, rid, local, tomb, "tombstone_on_unknown_local")
                continue
            if local_hash != previous.get("remote_hash"):
                record_conflict(record_type, rid, local, tomb, "local_changed_before_tombstone")
                continue
            pending_tombstones.append({
                "record_type": wire_type, "record_id": rid,
                "version": int(tomb.get("version") or 0),
                "final_status": tomb.get("final_status") or "revoked",
            })
            mark_converged(record_type, rid)

        # 一次 Ledger 事务导入所有 record 并写入 import receipt；任何校验或
        # 版本冲突都会回滚全部知识变更。cursor 在另一数据库随后推进，崩溃
        # 时由 Ledger receipt 识别并幂等重放，不宣称跨库分布式原子性。
        try:
            # A server snapshot may be observed again after the committed
            # cursor advances (for example a delta pull reusing its snapshot
            # id).  The Ledger receipt is therefore scoped to this pull
            # boundary, not only to the opaque server snapshot id.
            # Some deployed servers may reuse a snapshot id while the
            # contents of a baseline change (for example after a server-side
            # cursor reset). Bind the ledger receipt to the validated
            # manifest as well as the cursor, otherwise an old receipt can
            # falsely reject a legitimate new baseline as a replay conflict.
            receipt_key = f"{cursor_before}\x00{snapshot_manifest_hash}"
            import_id = f"{snapshot_id}:receipt:{hashlib.sha256(receipt_key.encode('utf-8')).hexdigest()[:16]}"
            imported = ns["import_synced_records"](
                ledger, pending_patterns, pending_incidents, pending_tools,
                pending_tombstones, pending_consumption_events,
                allow_remote_active=True, actor="supervisor",
                snapshot_id=import_id,
                manifest_hash=snapshot_manifest_hash,
            )
        except Exception as exc:  # noqa: BLE001
            raise CloudOnboardError(
                f"本地知识导入失败，cursor 保持 {cursor_before!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        pattern_result = imported.get("patterns") or {}
        incident_result = imported.get("incidents") or {}
        imported_p = int(pattern_result.get("created", 0)) + int(pattern_result.get("activated", 0))
        imported_i = int(incident_result.get("created", 0)) + int(incident_result.get("activated", 0))
        dup_local += int(pattern_result.get("duplicate", 0)) + int(incident_result.get("duplicate", 0))
        imported_tools = len((imported.get("tools") or {}).get("imported") or [])
        imported_consumption = int(imported.get("consumption_events", 0) or 0)

        committed_cursor = last_non_empty_cursor or cursor_before
        if stageable:
            store.inbox_mark_validated(snapshot_id)
            # 单事务原子提交：snapshot imported ⇄ committed cursor（AD-008-02）。
            store.inbox_commit_import(
                snapshot_id, account_id=account_id, scope=layer, project_id=project_id,
                cursor=committed_cursor,
                remote_version=max(snapshot_version,
                                   int(state.get("remote_version") or 0)),
                record_states=list(metadata_updates.values()))
        else:
            # 重放已导入快照：cursor 不回退，只推进 remote_version/时间戳。
            store.commit_scope_sync(
                account_id, layer, project_id, cursor=committed_cursor,
                snapshot_id=snapshot_id,
                remote_version=max(snapshot_version,
                                   int(state.get("remote_version") or 0)),
                record_states=list(metadata_updates.values()))
        acked = False
        if committed_cursor and hasattr(client, "ack_knowledge"):
            try:
                ack = client.ack_knowledge(layer, project_id, committed_cursor)
            except Exception as exc:  # noqa: BLE001
                ack = None
                ack_error = f"{type(exc).__name__}: {exc}"
            else:
                ack_error = ""
            if ack is None or not ack.ok:
                ack_status = getattr(ack, "status", None)
                ack_detail = ack_error or str(getattr(ack, "error", "ACK rejected"))
                try:
                    ack_err = client.sync_error(ack) if ack is not None else None
                    ack_code = (ack_err.code if ack_err else "") or "SYNC_ACK_FAILED"
                    ack_request_id = ack_err.request_id if ack_err else None
                except Exception:  # noqa: BLE001
                    ack_code, ack_request_id = "SYNC_ACK_FAILED", None
                store.record_scope_error(
                    account_id, layer, project_id,
                    error_code=ack_code, request_id=ack_request_id,
                )
                landed = landing_store(root, layer, merged, committed_cursor,
                                       metadata={"snapshot_id": snapshot_id,
                                                 "manifest_hash": ""})
                return {
                    "ok": False, "committed": True, "ack_pending": True,
                    "scope_status": scope_status,
                    "error": (f"{layer} pull committed but ACK failed "
                               f"code={ack_status}: {ack_detail}"),
                    "landing": landed,
                }
            acked = True
        resolved_conflicts = 0
        if resolve_converged_conflicts:
            resolved_conflicts = store.resolve_remote_conflicts(
                account_id, layer, project_id, converged_records,
                resolver_identity="sync-pull",
            )
            # 自动修剪与关闭已收敛的历史冲突
            pruned_conflicts = store.prune_converged_conflicts(
                account_id, layer, project_id, resolver_identity="sync-pull",
            )
            resolved_conflicts += pruned_conflicts
        # quarantine 落地仅诊断；失败不影响已提交状态。
        landed = landing_store(root, layer, merged, committed_cursor, metadata={
            "snapshot_id": snapshot_id, "manifest_hash": "", })
        detail = (f"landed patterns={imported_p} incidents={imported_i} "
                  f"tools={imported_tools} consumption_events={imported_consumption} "
                  f"tombstones={len(merged['tombstones'])} "
                  f"pages={pages} conflicts={conflicts} "
                  f"resolved_conflicts={resolved_conflicts} "
                  f"duplicates={dup_total + dup_local} skipped={skipped_tomb + skipped_deleted} "
                  f"invalid_skipped={skipped_invalid} "
                  f"cursor={committed_cursor} scope_status={scope_status} "
                  f"ack={'ok' if acked else 'not-needed'} "
                  f"quarantine={'ok' if landed else 'failed'}")
        return {"ok": True, "scope_status": scope_status, "detail": detail,
                "records_skipped_invalid": skipped_invalid}
    finally:
        store.close()


def _safe_incidents(ns, ledger: Path) -> list:
    try:
        return ns["list_incidents"](ledger) or []
    except Exception as exc:  # noqa: BLE001
        raise CloudOnboardError(f"本地错题集读取失败，拒绝推进 cursor: {ledger}") from exc


def _content_hash(ns, value: Any) -> str:
    projected = _content_projection(value)
    return hashlib.sha256(json.dumps(projected, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _content_projection(value: Any) -> Any:
    """比较知识内容时忽略同步元数据，不忽略 status/deleted 事实。"""
    if isinstance(value, dict):
        return {
            key: _content_projection(item)
            for key, item in value.items()
            if key not in {"version", "record_version", "device_id"}
        }
    if isinstance(value, list):
        return [_content_projection(item) for item in value]
    return value
