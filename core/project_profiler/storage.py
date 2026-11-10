#!/usr/bin/env python3
"""Ledger persistence, snapshot export, and cloud wire projection for project profiles."""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .constants import (
    CLIENT_ONLY_PROFILE_KEYS,
    PROFILE_ROW_KEY,
    PROFILE_SCHEMA_VERSION,
    WIRE_COMPONENTS_LIMIT,
    WIRE_DROP_ORDER,
    WIRE_EVIDENCE_LIMIT,
    WIRE_FACTS_LIMIT,
    WIRE_PAYLOAD_BUDGET_BYTES,
    WIRE_RISK_SIGNAL_KEYS,
    WIRE_RISK_SIGNALS_LIMIT,
)
from .extractors import DETECTOR_VERSION
from .strategy import classify_profile


def _get_detector_version() -> str:
    # 只查真实模块名（扁平名 ``core.project_profile``/``project_profile`` 已不存在）。
    for mod_name in ("core.runtime.project_profile",):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "DETECTOR_VERSION"):
            return str(getattr(mod, "DETECTOR_VERSION"))
    return DETECTOR_VERSION


def ledger_module():
    try:
        from core.ledger import lesson_ledger
        return lesson_ledger
    except ImportError:
        try:
            from .. import lesson_ledger  # type: ignore
            return lesson_ledger
        except ImportError:
            from core import lesson_ledger
            return lesson_ledger


def load_project_profile(ledger_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Read the single project-profile row from an already migrated ledger."""
    ledger = ledger_module()
    db = ledger.database_path(ledger_path)
    if not db.is_file() or db.is_symlink():
        return None
    with ledger.connect_ledger(db, readonly=True) as conn:  # type: ignore[attr-defined]
        ledger.validate_pattern(conn, db)  # type: ignore[attr-defined]
        row = conn.execute("SELECT schema_version,profile_json,classification_json,profile_hash,source_fingerprint,revision,status,updated_at,producer_version FROM project_profile WHERE project_key=?", (PROFILE_ROW_KEY,)).fetchone()
        if row is None:
            return None
        try:
            profile = json.loads(row[1])
            classification = json.loads(row[2])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"project_profile JSON 无效: {exc}") from exc
        if not isinstance(profile, dict) or not isinstance(classification, dict):
            raise ValueError("project_profile 记录格式无效")
        profile.setdefault("schema_version", int(row[0]))
        profile.setdefault("runtime_facts", [])
        profile.setdefault("framework_facts", [])
        profile.setdefault("components", [])
        profile.setdefault("build_targets", [])
        profile.setdefault("toolchains", [])
        profile.setdefault("risk_signals", [])
        profile.setdefault("unknowns", [])
        profile.setdefault("conflicts", [])
        profile["classification"] = classification
        profile["profile_hash"] = row[3]
        profile["source_fingerprint"] = row[4]
        profile["revision"] = row[5]
        profile["status"] = row[6]
        profile["updated_at"] = row[7]
        profile["producer_version"] = row[8]
        return profile


def save_project_profile(ledger_path: str | os.PathLike[str], profile: Mapping[str, Any], *, producer_version: str = "unknown", revision: int | None = None) -> dict[str, Any]:
    """Atomically upsert the project profile; knowledge hashes exclude this row."""
    if not isinstance(profile, Mapping) or profile.get("schema_version") not in {1, PROFILE_SCHEMA_VERSION}:
        raise ValueError("project_profile.schema_version 不受支持")
    profile = dict(profile)
    if profile.get("schema_version") == 1:
        profile["schema_version"] = PROFILE_SCHEMA_VERSION
        for key in ("runtime_facts", "framework_facts", "components", "build_targets", "toolchains", "risk_signals", "unknowns", "conflicts"):
            profile.setdefault(key, [])
        profile["classification"] = classify_profile(profile)
    profile_hash = str(profile.get("profile_hash") or "")
    source_fingerprint = str(profile.get("source_fingerprint") or "")
    if not profile_hash or not source_fingerprint:
        raise ValueError("project_profile 缺少稳定 hash")
    if revision is not None and (
        not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
    ):
        raise ValueError("project_profile revision 必须是非负整数")
    ledger = ledger_module()
    db = ledger.database_path(ledger_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    profile_json = dict(profile)
    profile_json.pop("classification", None)
    profile_json.pop("revision", None)
    profile_json.pop("updated_at", None)
    profile_json.pop("producer_version", None)
    classification = dict(profile.get("classification") or {})
    with ledger.connect_ledger(db) as conn:  # type: ignore[attr-defined]
        ledger.validate_pattern(conn, db)  # type: ignore[attr-defined]
        conn.execute("BEGIN IMMEDIATE")
        try:
            old = conn.execute("SELECT revision,profile_hash FROM project_profile WHERE project_key=?", (PROFILE_ROW_KEY,)).fetchone()
            next_revision = revision if revision is not None else (int(old[0]) + 1 if old else 1)
            if old and next_revision < int(old[0]):
                raise ValueError("project_profile revision 不能回退")
            conn.execute("INSERT INTO project_profile(project_key,schema_version,profile_json,classification_json,profile_hash,source_fingerprint,revision,status,updated_at,producer_version) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(project_key) DO UPDATE SET schema_version=excluded.schema_version,profile_json=excluded.profile_json,classification_json=excluded.classification_json,profile_hash=excluded.profile_hash,source_fingerprint=excluded.source_fingerprint,revision=excluded.revision,status=excluded.status,updated_at=excluded.updated_at,producer_version=excluded.producer_version", (PROFILE_ROW_KEY, PROFILE_SCHEMA_VERSION, json.dumps(profile_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")), json.dumps(classification, ensure_ascii=False, sort_keys=True, separators=(",", ":")), profile_hash, source_fingerprint, next_revision, str(profile.get("status") or "unknown"), now, producer_version))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    result = dict(profile)
    result.update({"revision": next_revision, "updated_at": now, "producer_version": producer_version})
    return result


def ensure_project_profile(ledger_path: str | os.PathLike[str], root: str | os.PathLike[str], *, producer_version: str = "unknown", force: bool = False) -> tuple[dict[str, Any], bool]:
    """Reuse a stable profile when inputs did not change; otherwise update it."""
    from .detector import detect_project_profile
    detected = detect_project_profile(root)
    current = load_project_profile(ledger_path)
    det_version = _get_detector_version()
    if (
        current
        and not force
        and current.get("detector_version") == det_version
        and current.get("source_fingerprint") == detected.get("source_fingerprint")
    ):
        return current, False
    return save_project_profile(ledger_path, detected, producer_version=producer_version), True


def export_project_profile(profile: Mapping[str, Any], output: str | os.PathLike[str]) -> None:
    """Write a human/debug snapshot; SQLite remains the source of truth."""
    try:
        from core.secure_atomic import atomic_write_bytes
    except ImportError:
        from ..secure_atomic import atomic_write_bytes  # type: ignore
    target = Path(output)
    export = dict(profile)
    export.pop("generated_at", None)
    atomic_write_bytes(target, json.dumps(export, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n", mode=0o600)


def slim_component(dep: Mapping[str, Any]) -> dict[str, Any]:
    version = dep.get("resolved_version") or dep.get("declared_version") or dep.get("observed_version")
    return {
        "ecosystem": str(dep.get("ecosystem") or ""),
        "name": str(dep.get("name") or dep.get("module") or ""),
        "version": version,
        "status": str(dep.get("status") or "unknown"),
    }


def slim_risk_signal(signal: Mapping[str, Any]) -> dict[str, Any]:
    return {key: signal[key] for key in WIRE_RISK_SIGNAL_KEYS if signal.get(key) is not None}


def confirmed_first(components: list[Any]) -> list[Any]:
    confirmed = [c for c in components if isinstance(c, Mapping) and c.get("status") == "confirmed"]
    others = [c for c in components if not (isinstance(c, Mapping) and c.get("status") == "confirmed")]
    return confirmed + others


def wire_safe_entry(entry: Any) -> bool:
    """服务端拒绝任何以 ``/`` 或 ``~`` 开头的字符串（绝对路径）；
    明细列表里这类值只出现在路径证据里，直接丢弃该条目即可。"""
    if isinstance(entry, str):
        return not entry.startswith(("/", "~"))
    if isinstance(entry, Mapping):
        return all(wire_safe_entry(v) for v in entry.values())
    if isinstance(entry, (list, tuple)):
        return all(wire_safe_entry(v) for v in entry)
    return True


def wire_safe_slice(items: list[Any], limit: int) -> list[Any]:
    kept: list[Any] = []
    for item in items:
        if len(kept) >= limit:
            break
        if wire_safe_entry(item):
            kept.append(item)
    return kept


def wire_project_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """按 DOC-057 设计项 4 生成云端看板用的紧凑投影。"""
    wire: dict[str, Any] = {
        k: v for k, v in profile.items()
        if not isinstance(v, list) and k not in CLIENT_ONLY_PROFILE_KEYS
    }

    components = profile.get("components") if isinstance(profile.get("components"), list) else []
    ordered = confirmed_first(components)
    wire["components"] = [slim_component(c) for c in ordered[:WIRE_COMPONENTS_LIMIT]
                          if isinstance(c, Mapping)]

    risk_signals = profile.get("risk_signals") if isinstance(profile.get("risk_signals"), list) else []
    slim_signals = [slim_risk_signal(s) for s in risk_signals[:WIRE_RISK_SIGNALS_LIMIT]
                    if isinstance(s, Mapping)]
    wire["risk_signals"] = slim_signals

    for name, limit in (("forms", None), ("category", None), ("languages", None),
                        ("runtimes", None), ("frameworks", WIRE_FACTS_LIMIT),
                        ("surfaces", None), ("declared_targets", None),
                        ("declared_commands", WIRE_FACTS_LIMIT),
                        ("flows", WIRE_EVIDENCE_LIMIT), ("evidence", WIRE_EVIDENCE_LIMIT),
                        ("runtime_facts", WIRE_FACTS_LIMIT), ("framework_facts", WIRE_FACTS_LIMIT),
                        ("build_targets", WIRE_FACTS_LIMIT), ("toolchains", WIRE_FACTS_LIMIT),
                        ("unknowns", WIRE_FACTS_LIMIT), ("conflicts", WIRE_FACTS_LIMIT),
                        ("code_languages", WIRE_FACTS_LIMIT), ("code_modules", WIRE_FACTS_LIMIT)):
        value = profile.get(name)
        if not isinstance(value, list):
            continue
        wire[name] = value if limit is None else wire_safe_slice(value, limit)

    classification = profile.get("classification")
    if isinstance(classification, Mapping):
        slim_classification = dict(classification)
        nested_signals = classification.get("risk_signals")
        if isinstance(nested_signals, list):
            slim_classification["risk_signals"] = slim_signals[:4]
        wire["classification"] = slim_classification

    def _wire_size() -> int:
        return len(json.dumps(wire, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    for key in WIRE_DROP_ORDER:
        if _wire_size() <= WIRE_PAYLOAD_BUDGET_BYTES:
            break
        wire.pop(key, None)
    return wire


# Compatibility aliases
_ledger_module = ledger_module
_slim_component = slim_component
_slim_risk_signal = slim_risk_signal
_confirmed_first = confirmed_first
_wire_safe_entry = wire_safe_entry
_wire_safe_slice = wire_safe_slice
