#!/usr/bin/env python3
"""core/ledger/fp_ledger.py — 负样本误报账本与源码哈希防误杀护栏（P1 / T-005）。

核心安全边界：
1. 仅沉淀经引擎确认驳回的误报（允许 ``not_reproducible`` / ``false_positive`` / ``wont_fix``），
   严禁把 ``timeout`` / ``env_blocked`` / ``source_tampered`` 误记为“代码安全”；
2. 每条负样本必须绑定目标文件的 ``source_sha256``；
3. 查询时实时重算目标文件 SHA-256：
   - 哈希一致 → ``active_guard``（有效降噪护栏）；
   - 哈希漂移或文件消失 → 自动降级为 ``stale_guard`` 并失效，严防修改代码引入新漏洞后被旧误报掩盖！
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

LEDGER_SCHEMA_VERSION = 1
ALLOWED_FP_REJECTION_CLASSES = frozenset({
    "not_reproducible",
    "false_positive",
    "wont_fix",
})


class FalsePositiveLedgerError(ValueError):
    """Raised on invalid false-positive ledger path or payload."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _confined_file(root: Path, rel_path: str) -> tuple[str, Path]:
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise FalsePositiveLedgerError("file_path 必须是非空相对路径")
    candidate = Path(rel_path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise FalsePositiveLedgerError(f"file_path 越界或非相对路径: {rel_path}")
    resolved_root = Path(root).resolve()
    cursor = resolved_root
    for part in candidate.parts:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            raise FalsePositiveLedgerError(f"file_path 禁止穿过符号链接: {rel_path}")
    resolved = (resolved_root / candidate).resolve()
    try:
        rel_norm = resolved.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise FalsePositiveLedgerError(f"file_path 越界: {rel_path}") from exc
    return rel_norm, resolved


def _file_sha256(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def ledger_path(root: Path) -> Path:
    return Path(root).resolve() / ".bugx" / "knowledge" / "fp-ledger.json"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_fp_ledger(root: Path) -> dict[str, Any]:
    path = ledger_path(root)
    if not path.is_file():
        return {"schema_version": LEDGER_SCHEMA_VERSION, "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": LEDGER_SCHEMA_VERSION, "entries": []}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"schema_version": LEDGER_SCHEMA_VERSION, "entries": []}
    return data


def record_false_positive(
    root: Path,
    *,
    file_path: str,
    rejection_class: str,
    guard_evidence: str,
    category: str = "",
    cwe: str = "",
    symbol: str = "",
    fingerprint: str = "",
    languages: Sequence[str] = (),
    frameworks: Sequence[str] = (),
) -> dict[str, Any]:
    """Persist a source-hash-bound false-positive guard into .bugx/knowledge/fp-ledger.json."""
    if rejection_class not in ALLOWED_FP_REJECTION_CLASSES:
        raise FalsePositiveLedgerError(
            f"不允许将 rejection_class={rejection_class!r} 写入负样本误报账本（仅允许 {sorted(ALLOWED_FP_REJECTION_CLASSES)}）"
        )
    evidence_text = (guard_evidence or "").strip()
    if not evidence_text:
        raise FalsePositiveLedgerError("guard_evidence 不能为空")

    rel_norm, abs_path = _confined_file(root, file_path)
    src_hash = _file_sha256(abs_path)
    if src_hash is None:
        raise FalsePositiveLedgerError(f"目标文件不存在或不可读，拒绝写入无哈希绑定的误报记录: {rel_norm}")

    dedup_key = _sha256_bytes(
        f"{rel_norm}|{category.strip().lower()}|{cwe.strip().upper()}|{symbol.strip()}|{src_hash}".encode("utf-8")
    )[:16]
    fp_id = f"FP-{dedup_key}"
    now = time.time()

    data = load_fp_ledger(root)
    entries: list[dict[str, Any]] = [e for e in data.get("entries", []) if isinstance(e, dict)]

    existing = next((e for e in entries if e.get("fp_id") == fp_id), None)
    if existing is not None:
        existing["hit_count"] = int(existing.get("hit_count") or 1) + 1
        existing["guard_evidence"] = evidence_text[:500]
        existing["updated_at"] = now
        record = existing
    else:
        record = {
            "fp_id": fp_id,
            "file_path": rel_norm,
            "source_sha256": src_hash,
            "category": category.strip().lower(),
            "cwe": cwe.strip().upper(),
            "symbol": symbol.strip(),
            "fingerprint": fingerprint.strip(),
            "rejection_class": rejection_class,
            "guard_evidence": evidence_text[:500],
            "languages": sorted({s.strip().lower() for s in languages if isinstance(s, str) and s.strip()}),
            "frameworks": sorted({s.strip().lower() for s in frameworks if isinstance(s, str) and s.strip()}),
            "hit_count": 1,
            "created_at": now,
            "updated_at": now,
        }
        entries.append(record)

    payload = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "updated_at": now,
        "entries": entries,
    }
    _atomic_write_json(ledger_path(root), payload)
    return record


def match_false_positive_guards(
    root: Path,
    *,
    file_path: str,
    category: str = "",
    cwe: str = "",
    symbol: str = "",
    include_stale: bool = False,
) -> list[dict[str, Any]]:
    """Return active (hash-verified) false-positive guards for a file; stale entries auto-invalidate."""
    try:
        rel_norm, abs_path = _confined_file(root, file_path)
    except FalsePositiveLedgerError:
        return []
    current_hash = _file_sha256(abs_path)
    data = load_fp_ledger(root)
    cat_norm = category.strip().lower()
    cwe_norm = cwe.strip().upper()
    sym_norm = symbol.strip()

    matched: list[dict[str, Any]] = []
    for entry in data.get("entries", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("file_path") != rel_norm:
            continue
        if cat_norm and entry.get("category") and entry.get("category") != cat_norm:
            continue
        if cwe_norm and entry.get("cwe") and entry.get("cwe") != cwe_norm:
            continue
        if sym_norm and entry.get("symbol") and entry.get("symbol") != sym_norm:
            continue

        is_active = bool(current_hash and entry.get("source_sha256") == current_hash)
        guard_status = "active_guard" if is_active else "stale_guard"
        if not is_active and not include_stale:
            continue
        item = dict(entry)
        item["guard_status"] = guard_status
        matched.append(item)
    return matched


def render_fp_guard_guidance(
    guards: Sequence[Mapping[str, Any]],
    *,
    char_budget: int = 600,
) -> str:
    """Render compact active false-positive guard hints for Worker noise reduction."""
    active = [g for g in guards if g.get("guard_status") == "active_guard"]
    if not active or char_budget <= 0:
        return ""
    lines = ["[历史已证伪误报护栏 (Active FP Guards — 源码未变更，避免重复无效验证)]"]
    used = len(lines[0]) + 1
    for g in active:
        block = (
            f"- [{g.get('fp_id')} | {g.get('file_path')} | {g.get('rejection_class')}] "
            f"{g.get('guard_evidence')}"
        )
        if used + len(block) + 1 > char_budget:
            break
        lines.append(block)
        used += len(block) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "ALLOWED_FP_REJECTION_CLASSES",
    "FalsePositiveLedgerError",
    "ledger_path",
    "load_fp_ledger",
    "record_false_positive",
    "match_false_positive_guards",
    "render_fp_guard_guidance",
]
