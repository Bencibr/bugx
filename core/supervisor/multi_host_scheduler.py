#!/usr/bin/env python3
"""Multi-host elastic scheduler, lazy scavenger, and peer convergence barrier."""

from __future__ import annotations

import uuid
from typing import Any

from core.supervisor import adaptive_lease_seconds, claim_sync, host_registry, lease_manager
from core.supervisor_types.exceptions import ClaimRejected
from core.supervisor.supervisor_models import (
    COVERAGE_PENDING,
    UNIT_LEVEL_DEBT,
    BudgetExhausted,
    RunState,
    Status,
)


def _host_short_tag(host_id: str) -> str:
    cleaned = "".join(ch for ch in host_id.lower() if ch.isalnum())
    return (cleaned[:8] or "host")


def _next_host_worker_id(state: RunState, host_id: str, prefix: str | None = None) -> str:
    short = _host_short_tag(host_id)
    base_prefix = (prefix or f"w-{short}").strip()
    idx = 1
    while True:
        candidate = f"{base_prefix}-r{int(state.round_no)}-{idx}-{uuid.uuid4().hex[:6]}"
        if candidate not in state.claims:
            return candidate
        idx += 1


def scavenge_offline_hosts_in_state(
    supervisor: Any,
    state: RunState,
    now: float,
) -> dict[str, Any]:
    """Scavenge timed-out/dead hosts and immediately release their non-independent claims.

    Must be called while holding supervisor._file_lock().
    """
    state_root = supervisor.state_path.parent
    hosts = host_registry.load_hosts_unlocked(state_root)
    newly_offline = host_registry.scavenge_hosts_unlocked(hosts, now)
    if newly_offline:
        host_registry.save_hosts_unlocked(state_root, hosts)

    reclaimed_workers: list[str] = []
    for worker_id, claim in list(state.active_claims.items()):
        owner = claim.owner_host_id
        if not owner:
            continue
        owner_entry = hosts.get(owner)
        owner_offline = owner_entry is None or str(owner_entry.get("state") or "") == "offline"
        if not owner_offline:
            continue
        # If the worker has its own live OS process, let its own lease/heartbeat govern it.
        if claim.process_pid is not None and host_registry._pid_alive(claim.process_pid):
            continue
        if lease_manager.release_claim(
            state, claim, now, reason="host_offline_reclaimed",
        ):
            reclaimed_workers.append(worker_id)
            supervisor._append_event(
                state,
                "host_claim_reclaimed",
                now,
                worker_id=worker_id,
                owner_host_id=owner,
                unit_ids=list(claim.unit_ids),
            )

    actives = host_registry.active_hosts_list(hosts)
    return {
        "newly_offline_hosts": newly_offline,
        "reclaimed_workers": reclaimed_workers,
        "active_hosts": actives,
        "hosts": hosts,
    }


def pull_units_for_host(
    supervisor: Any,
    *,
    host_id: str,
    max_concurrency: int = 4,
    lease_seconds: float | None = None,
    worker_prefix: str | None = None,
    host_kind: str = "mcp-host",
    entrypoint: str | None = None,
    preferred_domain: str | None = None,
    cluster_by_domain: bool = True,
) -> dict[str, Any]:
    """Atomically register/renew host, scavenge dead peers, and pull micro-batch units."""
    state_root = supervisor.state_path.parent

    def operation(state: RunState, now: float) -> dict[str, Any]:
        # 1. Refresh host registration & validate protocol compatibility under lock
        reg = host_registry.register_or_heartbeat_host(
            state_root,
            host_id=host_id,
            protocol_version=host_registry.SUPERVISOR_PROTOCOL_VERSION,
            max_concurrency=max_concurrency,
            host_kind=host_kind,
            entrypoint=entrypoint,
            now=now,
            use_lock=False,
        )
        # 2. Refresh RunState & reclaim claims from offline/dead hosts
        supervisor._refresh(state, now)
        scavenge_info = scavenge_offline_hosts_in_state(supervisor, state, now)

        if state.status == Status.BUDGET_EXHAUSTED or state.budget_exhausted:
            raise BudgetExhausted(
                "预算已耗尽: " + ", ".join(state.budget_reasons or ["unknown"])
            )
        supervisor._ensure_live_for_work(state)

        active_hosts = scavenge_info["active_hosts"]
        active_host_count = max(1, len(active_hosts))
        host_inflight = sum(
            1 for c in state.active_claims.values() if c.owner_host_id == host_id
        )

        # 3. Compute high-risk-first + domain-sticky claimable units atomically
        host_entry = reg.get("host") or {}
        eff_domain = (
            preferred_domain.strip().lower()
            if isinstance(preferred_domain, str) and preferred_domain.strip()
            else host_entry.get("active_domain")
        )
        eff_module = host_entry.get("active_module") if not preferred_domain else None
        available = list(
            lease_manager.claimable_units(
                state,
                mode=lease_manager.CLAIM_MODE_AUTO,
                workspace_root=supervisor.workspace_root,
                preferred_domain=eff_domain,
                preferred_module=eff_module,
            )
        )
        if not available:
            visible = lease_manager.eligible_units(state)
            waiting_internal = any(
                unit not in visible and status in (UNIT_LEVEL_DEBT | {COVERAGE_PENDING})
                for unit, status in (state.coverage or {}).items()
            )
            peer_claims = [
                {
                    "worker_id": c.worker_id,
                    "owner_host_id": c.owner_host_id,
                    "unit_ids": list(c.unit_ids),
                }
                for c in state.active_claims.values()
                if c.owner_host_id != host_id
            ]
            if waiting_internal:
                return {
                    "status": "WAITING_INTERNAL_UNITS",
                    "claims": [],
                    "active_host_count": active_host_count,
                    "active_hosts": active_hosts,
                    "host_inflight": host_inflight,
                    "peer_active_claims": peer_claims,
                    "retry_after_ms": 2000,
                    "reclaimed_workers": scavenge_info["reclaimed_workers"],
                }
            if peer_claims or state.active_claims:
                return {
                    "status": "WAITING_PEER_HOSTS",
                    "claims": [],
                    "active_host_count": active_host_count,
                    "active_hosts": active_hosts,
                    "host_inflight": host_inflight,
                    "peer_active_claims": peer_claims,
                    "retry_after_ms": 2000,
                    "reclaimed_workers": scavenge_info["reclaimed_workers"],
                }
            return {
                "status": "READY_TO_CONVERGE",
                "claims": [],
                "active_host_count": active_host_count,
                "active_hosts": active_hosts,
                "host_inflight": host_inflight,
                "peer_active_claims": [],
                "reclaimed_workers": scavenge_info["reclaimed_workers"],
            }

        global_max = max(1, int(getattr(state.budget, "max_concurrency", 1) or 1))
        global_avail = max(0, global_max - len(state.active_claims))
        if global_avail <= 0:
            return {
                "status": "GLOBAL_AT_CAPACITY",
                "claims": [],
                "active_host_count": active_host_count,
                "active_hosts": active_hosts,
                "host_inflight": host_inflight,
                "global_inflight": len(state.active_claims),
                "global_max_concurrency": global_max,
                "remaining_units": len(available),
                "retry_after_ms": 1500,
                "reclaimed_workers": scavenge_info["reclaimed_workers"],
            }

        quota = min(
            global_avail,
            host_registry.compute_pull_quota(
                remaining_units=len(available),
                active_host_count=active_host_count,
                host_inflight=host_inflight,
                host_max_concurrency=max_concurrency,
            ),
        )
        if quota <= 0:
            return {
                "status": "HOST_AT_CAPACITY",
                "claims": [],
                "active_host_count": active_host_count,
                "active_hosts": active_hosts,
                "host_inflight": host_inflight,
                "remaining_units": len(available),
                "retry_after_ms": 1500,
                "reclaimed_workers": scavenge_info["reclaimed_workers"],
            }

        selected_units = available
        if cluster_by_domain and len(available) > 1:
            from core.supervisor import task_scheduler
            anchor = available[0]
            anchor_dom = task_scheduler.infer_unit_domain(anchor)
            anchor_mod = task_scheduler.infer_unit_module(anchor)
            same_cluster = [
                u for u in available
                if (anchor_dom != "general" and task_scheduler.infer_unit_domain(u) == anchor_dom)
                or task_scheduler.infer_unit_module(u) == anchor_mod
            ]
            other_units = [u for u in available if u not in set(same_cluster)]
            selected_units = same_cluster if same_cluster else other_units
        effective_lease = (
            adaptive_lease_seconds(state) if lease_seconds is None else float(lease_seconds)
        )
        assigned_claims = []
        skipped_resource_conflict = 0
        for unit in selected_units:
            if len(assigned_claims) >= quota:
                break
            worker_id = _next_host_worker_id(state, host_id, prefix=worker_prefix)
            try:
                claim = claim_sync.apply_claim_operation(
                    supervisor,
                    state,
                    now,
                    allow_bundle=False,
                    continue_worker=False,
                    expected_revision=None,
                    lease=effective_lease,
                    process_pid=None,
                    requested_resources=(),
                    requested_units=(unit,),
                    supersede_key=None,
                    worker_key=worker_id,
                    owner_host_id=host_id,
                )
            except ClaimRejected as exc:
                msg = str(exc)
                if "资源组正在被其他 worker 使用" in msg:
                    skipped_resource_conflict += 1
                    continue
                if "max_concurrency" in msg:
                    break
                raise
            assigned_claims.append(claim)

        if not assigned_claims:
            if skipped_resource_conflict > 0:
                return {
                    "status": "WAITING_RESOURCE_CONFLICT",
                    "claims": [],
                    "active_host_count": active_host_count,
                    "active_hosts": active_hosts,
                    "host_inflight": host_inflight,
                    "remaining_units": len(available),
                    "retry_after_ms": 1500,
                    "reclaimed_workers": scavenge_info["reclaimed_workers"],
                }
            return {
                "status": "GLOBAL_AT_CAPACITY",
                "claims": [],
                "active_host_count": active_host_count,
                "active_hosts": active_hosts,
                "host_inflight": host_inflight,
                "global_inflight": len(state.active_claims),
                "global_max_concurrency": global_max,
                "remaining_units": len(available),
                "retry_after_ms": 1500,
                "reclaimed_workers": scavenge_info["reclaimed_workers"],
            }

        # Update host inflight count & sticky domain/module in hosts.json
        from core.supervisor import task_scheduler as _ts
        first_unit = assigned_claims[0].unit_ids[0]
        assigned_domain = _ts.infer_unit_domain(first_unit)
        assigned_module = _ts.infer_unit_module(first_unit)
        new_inflight = host_inflight + len(assigned_claims)
        host_registry.register_or_heartbeat_host(
            state_root,
            host_id=host_id,
            protocol_version=host_registry.SUPERVISOR_PROTOCOL_VERSION,
            max_concurrency=max_concurrency,
            host_kind=host_kind,
            entrypoint=entrypoint,
            inflight_count=new_inflight,
            active_domain=assigned_domain,
            active_module=assigned_module,
            now=now,
            use_lock=False,
        )
        return {
            "status": "ASSIGNED",
            "claims": [c.to_dict() for c in assigned_claims],
            "active_host_count": active_host_count,
            "active_hosts": reg["active_hosts"],
            "quota": quota,
            "host_inflight": new_inflight,
            "assigned_domain": assigned_domain,
            "assigned_module": assigned_module,
            "remaining_after_pull": max(0, len(available) - len(assigned_claims)),
            "reclaimed_workers": scavenge_info["reclaimed_workers"],
        }

    return supervisor._mutate(operation)


def detach_host_from_run(
    supervisor: Any,
    *,
    host_id: str,
    reason: str = "detached",
) -> dict[str, Any]:
    """Detach host cleanly and release its non-independent active claims immediately."""
    state_root = supervisor.state_path.parent

    def operation(state: RunState, now: float) -> dict[str, Any]:
        detach_info = host_registry.detach_host(
            state_root, host_id=host_id, reason=reason, now=now, use_lock=False,
        )
        scavenge_info = scavenge_offline_hosts_in_state(supervisor, state, now)
        return {
            "host_id": host_id,
            "detached": detach_info.get("detached", True),
            "reclaimed_workers": scavenge_info["reclaimed_workers"],
            "active_host_count": len(scavenge_info["active_hosts"]),
            "active_hosts": scavenge_info["active_hosts"],
        }

    return supervisor._mutate(operation)


def check_peer_barrier_in_state(
    supervisor: Any,
    state: RunState,
    *,
    current_host_id: str | None,
    now: float,
) -> dict[str, Any]:
    """Scavenge dead hosts first, then check if peer hosts still hold active claims."""
    scavenge_info = scavenge_offline_hosts_in_state(supervisor, state, now)
    peer_claims = [
        {
            "worker_id": c.worker_id,
            "owner_host_id": c.owner_host_id,
            "unit_ids": list(c.unit_ids),
            "execution_state": c.execution_state,
        }
        for c in state.active_claims.values()
        if current_host_id is None or c.owner_host_id != current_host_id
    ]
    return {
        "blocked_by_peers": bool(peer_claims),
        "peer_claims": peer_claims,
        "active_hosts": scavenge_info["active_hosts"],
        "reclaimed_workers": scavenge_info["reclaimed_workers"],
    }
