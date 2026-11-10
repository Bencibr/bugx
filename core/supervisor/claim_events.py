#!/usr/bin/env python3
"""审计事件链原语（hash-chain event append）。

为什么独立成模块（架构收敛，2026-10-26）：``supervisor.budget_governor`` 与
``supervisor.lease_manager`` 此前互为依赖——``lease_manager`` 在
``mark_claim_expired`` 里 `from supervisor import budget_governor`，而
``budget_governor`` 顶层从 ``lease_manager`` 导入 ``append_event`` 等原语。
双向依赖让模块方向无法静态判定（`tests/test_import_graph_acyclic.py` 禁止）。

``append_event`` 只依赖 ``supervisor_models``（事件序列 / hash-chain / 裁剪归档），
与租约、预算语义无关，因此下沉到本模块：

    lease_manager ──┐
                    ├──> claim_events ──> supervisor_models
    budget_governor ┘
"""

from __future__ import annotations

from pathlib import Path

from .supervisor_models import MAX_HISTORY_EVENTS, RunState, event_hash


def append_event(
    state: RunState,
    event_type: str,
    now: float,
    **fields: object,
) -> dict[str, object]:
    """向审计历史中追加一条经 hash-chain 保护的不可变事件。"""

    next_seq = (
        int(state.history[-1]["seq"]) + 1
        if state.history else state.history_base_seq
    )
    event: dict[str, object] = {
        "seq": next_seq,
        # _persist_locked increments state.revision after the operation; this
        # is the revision the event will become durable with.
        "revision": state.revision + 1,
        "type": event_type,
        "at": now,
        "prev_hash": state.audit_head,
        **fields,
    }
    event["hash"] = event_hash(event)
    state.history.append(event)
    if len(state.history) > MAX_HISTORY_EVENTS:
        trim_count = len(state.history) - MAX_HISTORY_EVENTS
        # DOC-204 L6：裁剪旁路归档——被裁事件 append 到
        # supervisor.history.archive.jsonl（链字段原样保留），消除
        # "无登记≠未登记"歧义；best-effort 旁证，失败不阻断事件链。
        try:
            import json as _json

            archive_path = getattr(state, "_history_archive_path", None)
            if archive_path:
                archive = Path(archive_path)
                archive.parent.mkdir(parents=True, exist_ok=True)
                with archive.open("a", encoding="utf-8") as fh:
                    for ev in state.history[:trim_count]:
                        fh.write(_json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass
        state.trimmed_count = getattr(state, "trimmed_count", 0) + trim_count
        retained = state.history[trim_count:]
        first = retained[0]
        state.history_base_seq = int(first["seq"])
        state.history_base_hash = str(first.get("prev_hash", ""))
        state.history = retained
    state.audit_head = str(event["hash"])
    state.dirty = True
    return event
