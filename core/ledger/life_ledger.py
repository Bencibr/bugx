#!/usr/bin/env python3
"""结算完整性与全局发现去重 ledger。

这个模块是一个小而独立的持久化组件，不依赖 ``verify_life.py`` 的运行时
文件。它只记录发现的身份、计分和审计元数据，适合由外部 supervisor 在
``parallel execute, serial submit`` 的提交边界调用。

核心保证：

* fingerprint 由结构化 ``finding_id`` 优先，其次 ``root_cause``，最后才
  使用文本归一化结果生成；证据、worker 和轮次不会改变发现身份。
* ledger 文件通过 ``fcntl.flock``（POSIX）或 ``msvcrt.locking``（Windows）
  跨进程锁保护；锁由操作系统在进程崩溃时自动释放，文件内写入使用同目录
  临时文件加 ``os.replace``。两种原语都缺失时显式阻断，绝不静默无锁写。
* ``expected_version`` 在独占锁内比较，失败时返回 CAS conflict 且不写入。
* 重复 fingerprint 只追加审计，不增加 ``total_score``；reset 只记录事件，
  不清除全局 discoveries。

模块不执行外部命令，也不读取环境变量、密钥或其他隐式凭据。
"""

from __future__ import annotations

import copy
import datetime as _datetime
import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]
from typing import Any, Iterable, Mapping, TypeAlias

SCHEMA_VERSION = 1
FINGERPRINT_VERSION = "v1"

Finding: TypeAlias = str | Mapping[str, Any]

_LOCAL_LOCKS: dict[Path, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()

_FINDING_ID_RE = re.compile(
    r"(?:finding[\s_-]*id|findingid)\s*[:=]\s*"
    r"[\[\(\{\s\"'`]*([A-Za-z0-9][A-Za-z0-9_.:/-]*)",
    re.IGNORECASE,
)
_ROOT_CAUSE_RE = re.compile(
    r"root[\s_-]*cause\s*[:=]\s*[\[\(\{\s\"'`]*(.+?)"
    r"(?=\s*[\]\)\};|\n]|$)",
    re.IGNORECASE | re.DOTALL,
)
_EVIDENCE_REF_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|js|ts|jsx|tsx|go|rs|java|kt|"
    r"c|cpp|h|sh|md|json|yaml|yml|toml|xml|html|css|sql):\d+",
    re.IGNORECASE,
)
_VOLATILE_TEXT_RE = re.compile(
    r"(?:evidence[_ -]*refs?|evidence|证据|复现|观察|预期|observed|expected|repro)"
    r"\s*[:=].*?(?=;|\||\n|$)",
    re.IGNORECASE | re.DOTALL,
)


class LedgerCorruptionError(ValueError):
    """Raised when an existing ledger is not valid JSON in the known schema."""


@dataclass(frozen=True)
class SettlementResult:
    """Result of one serialized settlement attempt.

    ``accepted`` and ``duplicates`` contain small JSON-compatible records with
    ``fingerprint`` and ``audit`` fields. ``conflicts`` is empty on success;
    a CAS mismatch returns one record and performs no write.
    """

    accepted: tuple[dict[str, Any], ...]
    duplicates: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]
    version: int

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible representation."""
        return {
            "accepted": copy.deepcopy(list(self.accepted)),
            "duplicates": copy.deepcopy(list(self.duplicates)),
            "conflicts": copy.deepcopy(list(self.conflicts)),
            "version": self.version,
        }


def _normalize_id(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _normalize_phrase(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    # Punctuation (including '-' versus '_') is presentation noise for a root
    # cause. Keep Unicode word characters so non-English findings remain stable.
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _first_string(mapping: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = mapping.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _text_identity(text: str) -> str:
    """Extract a structured marker from report text, then use normalized text."""
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, Mapping):
        return finding_identity(decoded)

    finding_id = _FINDING_ID_RE.search(text)
    if finding_id:
        return f"finding_id\0{_normalize_id(finding_id.group(1))}"

    root_cause = _ROOT_CAUSE_RE.search(text)
    if root_cause:
        normalized = _normalize_phrase(root_cause.group(1))
        if normalized:
            return f"root_cause\0{normalized}"

    # A free-form report may only differ by severity, evidence location, or
    # repro wording. Remove those fields before hashing; the remaining text is
    # still the caller-supplied identity and is never read from disk.
    text = re.sub(r"^\s*\[[A-Za-z]+\]\s*", " ", text)
    text = _VOLATILE_TEXT_RE.sub(" ", text)
    text = _EVIDENCE_REF_RE.sub(" ", text)
    normalized = _normalize_phrase(text)
    if not normalized:
        raise ValueError("finding text must contain a non-empty identity")
    return f"text\0{normalized}"


def finding_identity(finding: Finding) -> str:
    """Return the canonical identity payload used by :func:`finding_fingerprint`.

    A structured ``finding_id`` is authoritative. If it is absent,
    ``root_cause`` is authoritative. Text reports can use the same markers,
    for example ``finding_id=AUTH-7; root_cause=missing authorization``.
    """
    if isinstance(finding, Mapping):
        finding_id = _first_string(finding, ("finding_id", "findingId", "id"))
        if finding_id:
            return f"finding_id\0{_normalize_id(finding_id)}"

        root_cause = _first_string(finding, ("root_cause", "rootCause"))
        if root_cause:
            normalized = _normalize_phrase(root_cause)
            if normalized:
                return f"root_cause\0{normalized}"

        fallback_values = []
        for key in ("title", "summary", "description", "message"):
            value = finding.get(key)
            if value is not None and str(value).strip():
                fallback_values.append(str(value))
        if fallback_values:
            return _text_identity(" ".join(fallback_values))
        raise ValueError("structured finding requires finding_id or root_cause")

    if not isinstance(finding, str):
        raise TypeError("finding must be text or a mapping")
    if not finding.strip():
        raise ValueError("finding text must not be empty")
    return _text_identity(finding)


def finding_fingerprint(finding: Finding) -> str:
    """Return a stable SHA-256 fingerprint for a structured or text finding."""
    identity = finding_identity(finding)
    digest = hashlib.sha256(
        f"bugx/finding/{FINGERPRINT_VERSION}\0{identity}".encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "version": 0,
        "total_score": 0,
        "findings": {},
        "reset_audits": [],
    }


def _validate_state(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise LedgerCorruptionError(f"ledger root must be an object: {path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise LedgerCorruptionError(f"unsupported ledger schema: {path}")
    if not isinstance(data.get("version"), int) or data["version"] < 0:
        raise LedgerCorruptionError(f"invalid ledger version: {path}")
    if not isinstance(data.get("total_score"), int) or data["total_score"] < 0:
        raise LedgerCorruptionError(f"invalid ledger total_score: {path}")
    if not isinstance(data.get("findings"), dict):
        raise LedgerCorruptionError(f"invalid ledger findings: {path}")
    if not isinstance(data.get("reset_audits"), list):
        raise LedgerCorruptionError(f"invalid ledger reset_audits: {path}")
    return data


def _load_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerCorruptionError(f"cannot read ledger {path}: {exc}") from exc
    return _validate_state(data, path)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def atomic_write_json(path: str | os.PathLike[str], data: Mapping[str, Any]) -> None:
    """Atomically replace *path* with JSON, flushing file and directory metadata."""
    target = Path(path)
    directory = target.parent if target.parent != Path("") else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(directory)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(
                data,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(directory)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _lock_for(path: Path) -> threading.RLock:
    key = path.resolve()
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _ensure_dir(path: Path) -> None:
    """幂等建目录（不依赖 mkdir(exist_ok=True) 在宿主 shim 下的行为）。"""
    if path.is_dir():
        return
    path.mkdir(parents=True, exist_ok=True)


def _lock_handle(handle, *, shared: bool = False) -> None:
    """取得跨进程锁；无可用锁原语时显式阻断。

    ``shared=True`` 请求共享（读）锁；Windows 的 ``msvcrt`` 没有共享锁概念，
    退化为排他锁——语义更强，安全方向正确。
    """
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    raise RuntimeError(
        "账本锁在当前平台不可用（fcntl 与 msvcrt 均缺失），拒绝并发不安全写"
    )


def _unlock_handle(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextmanager
def _ledger_lock(path: Path):
    """Acquire both an in-process lock and an OS advisory lock.

    LOG-05：``fcntl`` 只存在于 POSIX。本模块历史上无条件 ``import fcntl``，
    Windows 上 ``from . import life_ledger`` 直接 ``ImportError``，调用方（``verify_life``
    / ``knowledge_auto`` / ``ledger.cli``）只能降级为「部分可用」。现在与
    ``bugx_paths._project_lock`` 对齐：POSIX 用 ``flock``、Windows 用
    ``msvcrt.locking``；两者都缺失时显式阻断，绝不静默退化为无锁写。

    锁文件另加 ``O_NOFOLLOW`` 与符号链接预检：符号链接会把 ``flock`` 落到链接
    指向的任意文件上，互斥直接失效（DOC-031 锁统一）。
    """
    if path.is_symlink():
        raise ValueError(f"账本不能是符号链接: {path}")
    _ensure_dir(path.parent)
    lock_path = path.with_name(f".{path.name}.lock")
    local_lock = _lock_for(lock_path)
    with local_lock:
        if lock_path.is_symlink():
            raise ValueError(f"拒绝使用符号链接锁文件: {lock_path}")
        try:
            descriptor = os.open(
                str(lock_path),
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise ValueError(f"无法安全打开账本锁: {lock_path}") from exc
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            _lock_handle(handle)
            try:
                yield
            finally:
                _unlock_handle(handle)


def _timestamp(value: str | _datetime.datetime | None) -> str:
    if value is None:
        return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, _datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_datetime.timezone.utc)
        return value.astimezone(_datetime.timezone.utc).isoformat(timespec="seconds")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string or datetime")
    return value


def _round(value: int | None, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("round must be a positive integer")
    return value


def _worker(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("worker must be a non-empty string")
    return value.strip()


def _evidence_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        raise TypeError("evidence_refs must be a string, list, tuple, or None")
    refs = []
    for ref in value:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("each evidence_ref must be a non-empty string")
        refs.append(ref)
    return refs


def _finding_evidence(
    finding: Finding,
    default: list[str],
    *,
    submission_overrides: bool,
) -> list[str]:
    if submission_overrides:
        return list(default)
    if isinstance(finding, Mapping):
        for key in ("evidence_refs", "evidenceRefs", "evidence"):
            if key in finding:
                return _evidence_refs(finding[key])
    return list(default)


def _audit_record(
    *,
    round: int | None,
    worker: str,
    timestamp: str,
    evidence_refs: list[str],
    status: str,
    settlement_id: str | None = None,
) -> dict[str, Any]:
    audit = {
        "round": round,
        "worker": worker,
        "timestamp": timestamp,
        "evidence_refs": list(evidence_refs),
        "status": status,
    }
    if settlement_id is not None:
        audit["settlement_id"] = settlement_id
    return audit


class FindingLedger:
    """Process-safe global finding ledger backed by one JSON file."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        """Read a detached snapshot under a shared OS lock."""
        _ensure_dir(self.path.parent)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        local_lock = _lock_for(lock_path)
        with local_lock:
            if lock_path.is_symlink():
                raise ValueError(f"拒绝使用符号链接锁文件: {lock_path}")
            try:
                descriptor = os.open(
                    str(lock_path),
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except OSError as exc:
                raise ValueError(f"无法安全打开账本锁: {lock_path}") from exc
            with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
                _lock_handle(handle, shared=True)
                try:
                    return copy.deepcopy(_load_unlocked(self.path))
                finally:
                    _unlock_handle(handle)

    def settle(
        self,
        findings: Iterable[Finding] | Finding,
        *,
        round: int,
        worker: str,
        timestamp: str | _datetime.datetime | None = None,
        evidence_refs: Any = None,
        expected_version: int | None = None,
        settlement_id: str | None = None,
    ) -> SettlementResult:
        """Submit findings exactly once with optional compare-and-swap.

        The complete read/compare/classify/write sequence runs under the file
        lock. A successful call increments ``version`` once, even if all
        submitted findings are duplicates, because duplicate audit attempts are
        durable. A CAS conflict increments nothing and writes nothing.
        """
        round_number = _round(round)
        worker_name = _worker(worker)
        event_timestamp = _timestamp(timestamp)
        default_refs = _evidence_refs(evidence_refs)
        submission_has_evidence = evidence_refs is not None
        if settlement_id is not None and (
            not isinstance(settlement_id, str) or not settlement_id.strip()
        ):
            raise ValueError("settlement_id must be a non-empty string or None")
        normalized_settlement_id = settlement_id.strip() if settlement_id is not None else None
        if expected_version is not None and (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version must be a non-negative integer or None")

        if isinstance(findings, (str, Mapping)):
            batch = [findings]
        else:
            batch = list(findings)

        # Compute identities and per-finding metadata before taking the lock so
        # malformed submissions fail without partially changing the ledger.
        prepared = [
            (
                finding,
                finding_fingerprint(finding),
                finding_identity(finding),
                _finding_evidence(
                    finding,
                    default_refs,
                    submission_overrides=submission_has_evidence,
                ),
            )
            for finding in batch
        ]

        with _ledger_lock(self.path):
            state = _load_unlocked(self.path)
            current_version = state["version"]
            if expected_version is not None and expected_version != current_version:
                conflict = {
                    "reason": "version_conflict",
                    "expected_version": expected_version,
                    "actual_version": current_version,
                }
                return SettlementResult((), (), (conflict,), current_version)

            accepted: list[dict[str, Any]] = []
            duplicates: list[dict[str, Any]] = []
            for finding, fingerprint, identity, refs in prepared:
                if fingerprint in state["findings"]:
                    record = state["findings"][fingerprint]
                    audit = _audit_record(
                        round=round_number,
                        worker=worker_name,
                        timestamp=event_timestamp,
                        evidence_refs=refs,
                        status="duplicate",
                        settlement_id=normalized_settlement_id,
                    )
                    record.setdefault("audits", []).append(audit)
                    duplicates.append(
                        {
                            "fingerprint": fingerprint,
                            "identity": record["identity"],
                            "first_seen": copy.deepcopy(record["audits"][0]),
                            "audit": copy.deepcopy(audit),
                        }
                    )
                    continue

                audit = _audit_record(
                    round=round_number,
                    worker=worker_name,
                    timestamp=event_timestamp,
                    evidence_refs=refs,
                    status="accepted",
                    settlement_id=normalized_settlement_id,
                )
                state["findings"][fingerprint] = {
                    "fingerprint": fingerprint,
                    "identity": identity,
                    "score": 1,
                    "audits": [audit],
                }
                state["total_score"] += 1
                accepted.append(
                    {
                        "fingerprint": fingerprint,
                        "identity": identity,
                        "score": 1,
                        "audit": copy.deepcopy(audit),
                    }
                )

            if prepared:
                state["version"] = current_version + 1
                atomic_write_json(self.path, state)
            return SettlementResult(
                tuple(accepted),
                tuple(duplicates),
                (),
                state["version"],
            )

    def reset(
        self,
        *,
        round: int | None = None,
        worker: str = "system",
        timestamp: str | _datetime.datetime | None = None,
        evidence_refs: Any = None,
    ) -> dict[str, Any]:
        """Record a lifecycle reset while retaining every global finding."""
        round_number = _round(round, allow_none=True)
        worker_name = _worker(worker)
        event_timestamp = _timestamp(timestamp)
        refs = _evidence_refs(evidence_refs)
        audit = _audit_record(
            round=round_number,
            worker=worker_name,
            timestamp=event_timestamp,
            evidence_refs=refs,
            status="reset",
        )

        with _ledger_lock(self.path):
            state = _load_unlocked(self.path)
            state.setdefault("reset_audits", []).append(audit)
            state["version"] += 1
            atomic_write_json(self.path, state)
            return copy.deepcopy(state)


def read_ledger(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Convenience wrapper returning a ledger snapshot."""
    return FindingLedger(path).read()


def settle(
    path: str | os.PathLike[str],
    findings: Iterable[Finding] | Finding,
    **kwargs: Any,
) -> SettlementResult:
    """Convenience wrapper for :meth:`FindingLedger.settle`."""
    return FindingLedger(path).settle(findings, **kwargs)


def reset(path: str | os.PathLike[str], **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper for :meth:`FindingLedger.reset`."""
    return FindingLedger(path).reset(**kwargs)


__all__ = [
    "FindingLedger",
    "LedgerCorruptionError",
    "SettlementResult",
    "atomic_write_json",
    "finding_fingerprint",
    "finding_identity",
    "read_ledger",
    "reset",
    "settle",
]
