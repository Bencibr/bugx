#!/usr/bin/env python3
"""Supervisor-owned serial authorization capabilities (DOC-208 D5).

The runtime stores only the receipt returned here.  Authorization itself is
owned by a versioned event in Supervisor's hash-chained history.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from typing import Any

from core.host_runtime import host_registry

from .supervisor_models import RunState, event_hash

from .lease_manager import append_event


SCHEMA_VERSION = 1
EVENT_TYPE = "serial_authorization_issued"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_FIELDS = frozenset({
    "schema_version",
    "authorization_id",
    "runtime_run_uuid",
    "supervisor_run_id",
    "session_id",
    "lifecycle_id",
    "round_id",
    "life_id",
    "life_kind",
    "round_no",
    "allow_serial",
    "serial_reason_sha256",
    "budget_epoch",
    "budget_snapshot_sha256",
    "policy_hash",
    "authorization_source",
})
_ENVELOPE_FIELDS = frozenset({
    "seq", "revision", "type", "at", "prev_hash", "hash",
})
_RECEIPT_FIELDS = frozenset({
    "schema_version", "authorization_id", "seq", "revision", "hash",
})
_IDENTITY_FIELDS = (
    "runtime_run_uuid", "supervisor_run_id", "session_id", "lifecycle_id",
    "round_id", "life_id", "life_kind", "round_no",
)
_SERIAL_LIFE_KINDS = frozenset({"fix", "mine"})


class SerialAuthorizationError(ValueError):
    """A serial capability is malformed, stale, ambiguous, or not authorized."""


def _validate_serial_context(life_kind: object, host: object) -> str:
    """Allow mine-life serial execution only on a host that declares 串行 Worker.

    DOC-239：串行性来自宿主插件 ``worker_runtime.mode``，这里不再写宿主名。
    """
    normalized = _require_text(life_kind, "life_kind").strip().casefold()
    if normalized not in _SERIAL_LIFE_KINDS:
        raise SerialAuthorizationError(
            "serial authorization only supports fix Life or mine Life on a serial-worker host"
        )
    if normalized == "mine" and not host_registry.host_serial_worker(host):
        raise SerialAuthorizationError(
            "mine Life serial authorization requires a trusted serial-worker host"
        )
    return normalized


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise SerialAuthorizationError(f"{name} must be a lowercase SHA-256")
    return value


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SerialAuthorizationError(f"{name} must be a non-empty string")
    return value


def _require_runtime_uuid(value: object) -> str:
    raw = _require_text(value, "runtime_run_uuid")
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise SerialAuthorizationError("runtime_run_uuid must be a canonical UUID") from exc
    # launch_bugx runtime ids are uuid4().hex (32 lowercase hexadecimal
    # characters); accept that real on-disk form and canonical dashed UUIDs.
    # Reject uppercase and other UUID spellings so identity hashing stays stable.
    if raw not in {str(parsed), parsed.hex}:
        raise SerialAuthorizationError("runtime_run_uuid must be a canonical UUID")
    return raw


def _require_int(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SerialAuthorizationError(f"{name} must be an integer >= {minimum}")
    return value


def _identity_payload(
    *, runtime_run_uuid: str, supervisor_run_id: str, session_id: str,
    lifecycle_id: str, round_id: str, life_id: str, life_kind: str,
    round_no: int,
) -> dict[str, object]:
    return {
        "runtime_run_uuid": _require_runtime_uuid(runtime_run_uuid),
        "supervisor_run_id": _require_text(supervisor_run_id, "supervisor_run_id"),
        "session_id": _require_text(session_id, "session_id"),
        "lifecycle_id": _require_text(lifecycle_id, "lifecycle_id"),
        "round_id": _require_text(round_id, "round_id"),
        "life_id": _require_text(life_id, "life_id"),
        "life_kind": _require_text(life_kind, "life_kind"),
        "round_no": _require_int(round_no, "round_no", minimum=1),
    }


def _identity_scope(identity: Mapping[str, Any]) -> tuple[Any, ...]:
    """授权事件的代际作用域：``_IDENTITY_FIELDS`` 全字段元组。

    partial scope（只用 runtime uuid 或 supervisor run id）会让跨轮授权互相误判为
    孤儿/重复；全字段才等于「这一轮、这一会话、这一 Life」的代际身份。
    """
    return tuple(identity.get(field) for field in _IDENTITY_FIELDS)


def derive_serial_policy_hash(
    *,
    identity: Mapping[str, object],
    base_policy_hash: str | None,
    serial_reason_sha256: str,
) -> str:
    """Bind an explicit serial opt-in to the runtime policy and identity."""
    if base_policy_hash is not None:
        base_policy_hash = _require_digest(base_policy_hash, "base_policy_hash")
    reason_digest = _require_digest(serial_reason_sha256, "serial_reason_sha256")
    normalized = _identity_payload(
        runtime_run_uuid=identity.get("runtime_run_uuid"),  # type: ignore[arg-type]
        supervisor_run_id=identity.get("supervisor_run_id"),  # type: ignore[arg-type]
        session_id=identity.get("session_id"),  # type: ignore[arg-type]
        lifecycle_id=identity.get("lifecycle_id"),  # type: ignore[arg-type]
        round_id=identity.get("round_id"),  # type: ignore[arg-type]
        life_id=identity.get("life_id"),  # type: ignore[arg-type]
        life_kind=identity.get("life_kind"),  # type: ignore[arg-type]
        round_no=identity.get("round_no"),  # type: ignore[arg-type]
    )
    return _sha256(canonical_json({
        "schema_version": SCHEMA_VERSION,
        "allow_serial": True,
        "authorization_source": "pre_cli",
        "base_policy_hash": base_policy_hash,
        "identity": normalized,
        "serial_reason_sha256": reason_digest,
    }))


def _authorization_id(
    identity: Mapping[str, object], budget_epoch: int,
    budget_snapshot_sha256: str,
) -> str:
    identity_domain = {
        key: identity[key]
        for key in _IDENTITY_FIELDS
    }
    identity_domain.update({
        "budget_epoch": budget_epoch,
        "budget_snapshot_sha256": budget_snapshot_sha256,
    })
    return _sha256(canonical_json(identity_domain))


def _budget_snapshot(state: RunState) -> tuple[str, str]:
    budget = state.budget.to_dict()
    reason = budget.get("serial_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise SerialAuthorizationError("serial authorization requires serial_reason")
    return _sha256(canonical_json(budget)), _sha256(reason.encode("utf-8"))


def _validate_event(event: Mapping[str, object]) -> dict[str, object]:
    if event.get("type") != EVENT_TYPE:
        raise SerialAuthorizationError("not a serial_authorization_issued event")
    if set(event) != _ENVELOPE_FIELDS | _EVENT_FIELDS:
        raise SerialAuthorizationError("serial authorization event fields do not match schema v1")
    if event.get("schema_version") != SCHEMA_VERSION or isinstance(
        event.get("schema_version"), bool
    ):
        raise SerialAuthorizationError("unsupported serial authorization schema")
    if event.get("authorization_source") != "pre_cli":
        raise SerialAuthorizationError("authorization_source must be pre_cli")
    if event.get("allow_serial") is not True or event.get("life_kind") not in _SERIAL_LIFE_KINDS:
        raise SerialAuthorizationError(
            "serial authorization has an unsupported Life kind"
        )
    _require_digest(event.get("authorization_id"), "authorization_id")
    _require_digest(event.get("serial_reason_sha256"), "serial_reason_sha256")
    _require_digest(event.get("budget_snapshot_sha256"), "budget_snapshot_sha256")
    _require_digest(event.get("policy_hash"), "policy_hash")
    _identity_payload(
        runtime_run_uuid=event.get("runtime_run_uuid"),  # type: ignore[arg-type]
        supervisor_run_id=event.get("supervisor_run_id"),  # type: ignore[arg-type]
        session_id=event.get("session_id"),  # type: ignore[arg-type]
        lifecycle_id=event.get("lifecycle_id"),  # type: ignore[arg-type]
        round_id=event.get("round_id"),  # type: ignore[arg-type]
        life_id=event.get("life_id"),  # type: ignore[arg-type]
        life_kind=event.get("life_kind"),  # type: ignore[arg-type]
        round_no=event.get("round_no"),  # type: ignore[arg-type]
    )
    _require_int(event.get("budget_epoch"), "budget_epoch", minimum=0)
    _require_int(event.get("seq"), "event seq", minimum=1)
    _require_int(event.get("revision"), "event revision", minimum=1)
    at = event.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        raise SerialAuthorizationError("event at must be finite")
    if not isinstance(event.get("prev_hash"), str):
        raise SerialAuthorizationError("event prev_hash must be a string")
    if event.get("prev_hash") and not _HASH_RE.fullmatch(str(event["prev_hash"])):
        raise SerialAuthorizationError("event prev_hash is invalid")
    digest = _require_digest(event.get("hash"), "event hash")
    if digest != event_hash(event):
        raise SerialAuthorizationError("serial authorization event hash is invalid")
    expected_id = _authorization_id(
        event,
        int(event["budget_epoch"]),
        str(event["budget_snapshot_sha256"]),
    )
    if event["authorization_id"] != expected_id:
        raise SerialAuthorizationError("authorization_id does not match the identity snapshot")
    return dict(event)


def validate_receipt_schema(receipt: object) -> dict[str, object]:
    """Validate the bounded runtime receipt schema without trusting it as authority."""
    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
        raise SerialAuthorizationError("serial authorization receipt fields do not match schema v1")
    if receipt.get("schema_version") != SCHEMA_VERSION or isinstance(
        receipt.get("schema_version"), bool
    ):
        raise SerialAuthorizationError("unsupported serial authorization receipt schema")
    _require_digest(receipt.get("authorization_id"), "receipt authorization_id")
    _require_digest(receipt.get("hash"), "receipt hash")
    _require_int(receipt.get("seq"), "receipt seq", minimum=1)
    _require_int(receipt.get("revision"), "receipt revision", minimum=1)
    return dict(receipt)


def _full_chain_state(state: RunState) -> RunState:
    """Reparse the retained chain without the persisted prefix-validation shortcut."""
    payload = state.to_dict()
    payload.pop("history_anchor", None)
    try:
        return RunState.from_dict(payload)
    except Exception as exc:
        raise SerialAuthorizationError("Supervisor history chain is invalid") from exc


def _state_matches(
    state: RunState,
    identity: Mapping[str, object],
    *,
    budget_epoch: int,
    budget_snapshot_sha256: str,
    serial_reason_sha256: str,
) -> None:
    if (
        state.run_id != identity["supervisor_run_id"]
        or state.session_id != identity["session_id"]
        or state.round_id != identity["round_id"]
        or state.life_id != identity["life_id"]
        or state.life_kind != identity["life_kind"]
        or state.round_no != identity["round_no"]
    ):
        raise SerialAuthorizationError("Supervisor state identity does not match capability")
    if state.revision < 1:
        raise SerialAuthorizationError("Supervisor state revision is invalid")
    if state.budget.require_parallel is not False:
        raise SerialAuthorizationError("Supervisor budget does not authorize serial execution")
    current_budget_hash, current_reason_hash = _budget_snapshot(state)
    if (
        state.budget_epoch != budget_epoch
        or current_budget_hash != budget_snapshot_sha256
        or current_reason_hash != serial_reason_sha256
    ):
        raise SerialAuthorizationError("Supervisor budget snapshot does not match capability")


def _receipt(event: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "authorization_id": event["authorization_id"],
        "seq": event["seq"],
        "revision": event["revision"],
        "hash": event["hash"],
    }


def issue_serial_authorization(
    supervisor: Any,
    *,
    runtime_run_uuid: str,
    supervisor_run_id: str,
    session_id: str,
    lifecycle_id: str,
    round_id: str,
    life_id: str,
    round_no: int,
    policy_hash: str,
    expected_revision: int,
    life_kind: str = "fix",
    host: str | None = None,
) -> dict[str, object]:
    """Issue or recover the unique pre-authorized serial capability.

    ``supervisor._mutate`` owns the state lock and reloads state inside that
    lock.  The mandatory expected revision is checked before either returning
    an orphan event receipt or appending a new event.
    """
    expected_revision = _require_int(
        expected_revision, "expected_revision", minimum=0,
    )
    policy_digest = _require_digest(policy_hash, "policy_hash")
    authorized_life_kind = _validate_serial_context(life_kind, host)
    identity = _identity_payload(
        runtime_run_uuid=runtime_run_uuid,
        supervisor_run_id=supervisor_run_id,
        session_id=session_id,
        lifecycle_id=lifecycle_id,
        round_id=round_id,
        life_id=life_id,
        life_kind=authorized_life_kind,
        round_no=round_no,
    )

    def operation(state: RunState, now: float) -> dict[str, object]:
        supervisor._check_revision(state, expected_revision)
        if state.life_kind != identity["life_kind"]:
            raise SerialAuthorizationError(
                "serial authorization Life kind does not match locked state"
            )
        if state.run_id != identity["supervisor_run_id"]:
            raise SerialAuthorizationError("supervisor_run_id does not match locked state")
        if (
            state.session_id != identity["session_id"]
            or state.round_id != identity["round_id"]
            or state.life_id != identity["life_id"]
            or state.round_no != identity["round_no"]
        ):
            raise SerialAuthorizationError("pre identity does not match locked Supervisor state")
        if state.budget.require_parallel is not False:
            raise SerialAuthorizationError("serial authorization requires require_parallel=false")
        budget_hash, reason_hash = _budget_snapshot(state)
        authorization_id = _authorization_id(identity, state.budget_epoch, budget_hash)
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "authorization_id": authorization_id,
            **identity,
            "allow_serial": True,
            "serial_reason_sha256": reason_hash,
            "budget_epoch": state.budget_epoch,
            "budget_snapshot_sha256": budget_hash,
            "policy_hash": policy_digest,
            "authorization_source": "pre_cli",
        }

        # 作用域必须是**完整代际身份**（runtime/session/lifecycle/round/life/round_no），
        # 不能只用 runtime_run_uuid 或 supervisor_run_id：runtime 会话跨轮存活，
        # 用 runtime uuid 做作用域会把上一轮的授权当成本轮的「孤儿」，于是
        # finish 后重新 pre 必抛 orphan serial authorization does not match
        # explicit pre inputs（缺陷 D2，recover 也清不掉历史事件）。
        scope = _identity_scope(identity)
        same_generation = [
            event for event in state.history
            if isinstance(event, Mapping)
            and event.get("type") == EVENT_TYPE
            and _identity_scope(event) == scope
        ]
        if same_generation:
            if len(same_generation) != 1:
                raise SerialAuthorizationError("duplicate serial authorization events exist")
            existing = _validate_event(same_generation[0])
            actual_payload = {
                key: existing[key]
                for key in _EVENT_FIELDS
            }
            if actual_payload != payload:
                raise SerialAuthorizationError(
                    "orphan serial authorization does not match explicit pre inputs"
                )
            return _receipt(existing)
        if state.history_base_seq != 1:
            raise SerialAuthorizationError(
                "compacted Supervisor history cannot prove authorization uniqueness"
            )

        event = append_event(
            state, EVENT_TYPE, now, **payload,
        )
        validated = _validate_event(event)
        return _receipt(validated)

    return supervisor._mutate(operation)  # type: ignore[return-value]


def validate_serial_authorization(
    state: RunState,
    receipt: object,
    *,
    runtime_run_uuid: str,
    supervisor_run_id: str,
    session_id: str,
    lifecycle_id: str,
    round_id: str,
    life_id: str,
    round_no: int,
    policy_hash: str,
    life_kind: str = "fix",
    host: str | None = None,
) -> dict[str, object]:
    """Validate receipt against the complete retained chain and current state."""
    runtime_receipt = validate_receipt_schema(receipt)
    policy_digest = _require_digest(policy_hash, "policy_hash")
    authorized_life_kind = _validate_serial_context(life_kind, host)
    identity = _identity_payload(
        runtime_run_uuid=runtime_run_uuid,
        supervisor_run_id=supervisor_run_id,
        session_id=session_id,
        lifecycle_id=lifecycle_id,
        round_id=round_id,
        life_id=life_id,
        life_kind=authorized_life_kind,
        round_no=round_no,
    )
    checked_state = _full_chain_state(state)
    # 与 issue 侧同一作用域：同一代际只能有一条事件，跨代际（新轮次/新会话）各自一条。
    scope = _identity_scope(identity)
    same_identity_events = [
        event for event in checked_state.history
        if isinstance(event, Mapping)
        and event.get("type") == EVENT_TYPE
        and _identity_scope(event) == scope
    ]
    if len(same_identity_events) != 1:
        raise SerialAuthorizationError("identity must have exactly one Supervisor authorization event")
    candidates = [
        event for event in checked_state.history
        if isinstance(event, Mapping)
        and event.get("type") == EVENT_TYPE
        and event.get("authorization_id") == runtime_receipt["authorization_id"]
    ]
    if len(candidates) != 1:
        raise SerialAuthorizationError("receipt must identify exactly one Supervisor event")
    event = _validate_event(candidates[0])
    if _receipt(event) != runtime_receipt:
        raise SerialAuthorizationError("runtime receipt does not match Supervisor event envelope")
    if int(event["revision"]) > checked_state.revision:
        raise SerialAuthorizationError("authorization event revision exceeds Supervisor state")
    if any(event.get(key) != identity[key] for key in _IDENTITY_FIELDS):
        raise SerialAuthorizationError("runtime identity does not match Supervisor event")
    if event.get("policy_hash") != policy_digest:
        raise SerialAuthorizationError("policy_hash does not match Supervisor event")
    _state_matches(
        checked_state, identity,
        budget_epoch=int(event["budget_epoch"]),
        budget_snapshot_sha256=str(event["budget_snapshot_sha256"]),
        serial_reason_sha256=str(event["serial_reason_sha256"]),
    )
    return _receipt(event)


__all__ = [
    "EVENT_TYPE",
    "SCHEMA_VERSION",
    "SerialAuthorizationError",
    "derive_serial_policy_hash",
    "issue_serial_authorization",
    "validate_receipt_schema",
    "validate_serial_authorization",
]
