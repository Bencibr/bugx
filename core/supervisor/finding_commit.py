#!/usr/bin/env python3
"""Crash-safe state/report/seal commit log for Finding publication.

The Supervisor owns the business lock; this module owns the durable PREPARE /
APPLY / COMMIT record underneath it.  A pending transaction is always replayed
forward from the recorded after bytes.  It is never repaired by guessing which
directory is newest or by copying an unrelated backup over a newer state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


MAX_PARTICIPANTS = 4
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
COMMIT_SCHEMA_VERSION = 1
PHASE_PRECHECK = "PRECHECK"
PHASE_PREPARE = "PREPARE"
PHASE_APPLY = "APPLY"
PHASE_COMMIT = "COMMIT"
PHASE_CLEANUP = "CLEANUP"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
# Short names mirror the protocol text and keep test/host callers from
# hard-coding the wire values.
PRECHECK = PHASE_PRECHECK
PREPARE = PHASE_PREPARE
APPLY = PHASE_APPLY
COMMIT = PHASE_COMMIT
CLEANUP = PHASE_CLEANUP


class FindingCommitError(RuntimeError):
    """Commit validation or I/O failure."""


class RecoveryRequired(FindingCommitError):
    """The on-disk transaction cannot be safely replayed."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _atomic_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _safe_participant(root: Path, path: Path) -> Path:
    root = Path(root).resolve()
    path = Path(path)
    if not path.is_absolute():
        path = root / path
    # macOS exposes /var and /tmp as lexical aliases of /private/var and
    # /private/tmp.  Resolve only for the containment comparison; retain the
    # original path for the no-follow final-component check.
    lexical = path.absolute()
    path = Path(os.path.realpath(str(lexical)))
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FindingCommitError(f"participant 路径越界: {path}") from exc
    lexical_for_check = lexical
    # macOS exposes /var and /tmp as lexical aliases of /private/*; normalize
    # only that platform alias before checking user-controlled components.
    for alias in ("var", "tmp"):
        alias_path = Path("/") / alias
        canonical_alias = Path("/private") / alias
        if lexical.parts[:2] == alias_path.parts and root.parts[:3] == canonical_alias.parts:
            try:
                lexical_for_check = canonical_alias / lexical.relative_to(alias_path)
            except ValueError:
                pass
            break
    cursor = lexical_for_check
    lexical_symlink = False
    try:
        relative = lexical_for_check.relative_to(root)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                lexical_symlink = True
                break
    except ValueError:
        lexical_symlink = True
    if lexical_symlink or lexical.is_symlink() or path.is_symlink():
        raise FindingCommitError(f"participant 不能是符号链接: {path}")
    if path.exists():
        try:
            if path.stat().st_nlink != 1:
                raise FindingCommitError(f"participant 不能是硬链接别名: {path}")
        except OSError as exc:
            raise FindingCommitError(f"participant 无法 stat: {path}") from exc
        if not path.is_file():
            raise FindingCommitError(f"participant 不是普通文件: {path}")
    return path


def _read_existing(path: Path) -> tuple[bool, bytes]:
    if path.is_symlink():
        raise FindingCommitError(f"participant 不能是符号链接: {path}")
    if not path.exists():
        return False, b""
    if not path.is_file():
        raise FindingCommitError(f"participant 不是普通文件: {path}")
    return True, path.read_bytes()


def _write_participant(path: Path, data: bytes, *, exists: bool) -> None:
    if not exists:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_bytes(path, data)


def _call_failpoint(callback: Callable[[str], object] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


class FindingCommitLog:
    """One bounded transaction log rooted at ``state_root/finding-commit``."""

    def __init__(self, state_root: Path, *, failpoint: Callable[[str], object] | None = None) -> None:
        self.state_root = Path(state_root).absolute()
        if self.state_root.is_symlink() or not self.state_root.is_dir():
            raise FindingCommitError(f"state_root 不是安全目录: {self.state_root}")
        self.root = self.state_root / "finding-commit"
        self.pending_path = self.root / "pending.json"
        self.lock_path = self.root / "commit.lock"
        self.failpoint = failpoint

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "a+b")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _transaction_dir(self, txid: str) -> Path:
        if not isinstance(txid, str) or not txid or "/" in txid or "\\" in txid or len(txid) > 96:
            raise FindingCommitError("txid 无效")
        return self.root / txid

    def _load_manifest(self, txid: str) -> dict[str, object]:
        directory = self._transaction_dir(txid)
        path = directory / "manifest.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
                raise RecoveryRequired("manifest 不存在或超过大小限制")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except RecoveryRequired:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryRequired(f"manifest 无法读取: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != COMMIT_SCHEMA_VERSION:
            raise RecoveryRequired("manifest schema 不支持")
        if payload.get("txid") != txid:
            raise RecoveryRequired("manifest txid 不匹配")
        participants = payload.get("participants")
        if not isinstance(participants, dict) or not participants or len(participants) > MAX_PARTICIPANTS:
            raise RecoveryRequired("manifest participants 无效")
        return payload

    def _validate_manifest_paths(self, manifest: Mapping[str, object]) -> list[tuple[Path, dict[str, object]]]:
        txid = str(manifest.get("txid"))
        directory = self._transaction_dir(txid)
        participants = manifest.get("participants")
        if not isinstance(participants, Mapping):
            raise RecoveryRequired("manifest participants 无效")
        result: list[tuple[Path, dict[str, object]]] = []
        for name, raw in participants.items():
            if not isinstance(name, str) or not isinstance(raw, Mapping):
                raise RecoveryRequired("manifest participant 条目无效")
            relative = raw.get("path")
            if not isinstance(relative, str) or relative.startswith("/") or "/" in relative or "\\" in relative:
                raise RecoveryRequired("manifest participant path 非固定路径")
            if relative not in {"state", "backup", "report", "seal"}:
                raise RecoveryRequired("manifest 含未知 participant")
            target = _safe_participant(self.state_root, str(raw.get("target")))
            if target.is_symlink():
                raise RecoveryRequired("participant 目标是符号链接")
            for field in ("before_sha256", "after_sha256"):
                value = raw.get(field)
                if not isinstance(value, str) or len(value) != 64:
                    raise RecoveryRequired(f"manifest {field} 无效")
            before_exists = raw.get("before_exists")
            after_exists = raw.get("after_exists")
            if not isinstance(before_exists, bool) or not isinstance(after_exists, bool):
                raise RecoveryRequired("manifest 存在性字段无效")
            result.append((target, dict(raw)))
        return result

    def _payload(self, directory: Path, relative: str, *, after: bool) -> bytes:
        path = directory / f"{relative}.{'after' if after else 'before'}"
        if path.is_symlink() or not path.is_file():
            raise RecoveryRequired(f"缺少 transaction payload: {path}")
        data = path.read_bytes()
        if len(data) > MAX_PAYLOAD_BYTES:
            raise RecoveryRequired("transaction payload 超过大小限制")
        return data

    def _verify_bytes(self, data: bytes, expected: str, label: str) -> None:
        if _sha256(data) != expected:
            raise RecoveryRequired(f"{label} hash 不匹配")

    def _recover_locked(self) -> dict[str, object] | None:
        """Replay a pending transaction; caller must hold ``_lock``."""
        if not self.pending_path.exists():
            return None
        if self.pending_path.is_symlink():
            raise RecoveryRequired("pending pointer 是符号链接")
        try:
            pointer = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryRequired(f"pending pointer 无法解析: {exc}") from exc
        if not isinstance(pointer, Mapping) or not isinstance(pointer.get("txid"), str):
            raise RecoveryRequired("pending pointer 无效")
        txid = str(pointer["txid"])
        manifest = self._load_manifest(txid)
        participants = self._validate_manifest_paths(manifest)
        directory = self._transaction_dir(txid)
        for target, item in participants:
            relative = str(item["path"])
            after = self._payload(directory, relative, after=True)
            self._verify_bytes(after, str(item["after_sha256"]), f"{relative}.after")
            before_exists = bool(item["before_exists"])
            before = self._payload(directory, relative, after=False) if before_exists else b""
            if before_exists:
                self._verify_bytes(before, str(item["before_sha256"]), f"{relative}.before")
            current_exists, current = _read_existing(target)
            current_sha = _sha256(current) if current_exists else _sha256(b"")
            if current_exists != before_exists and current_sha != str(item["after_sha256"]):
                raise RecoveryRequired(f"{relative} 当前存在性与 before/after 均不匹配")
            if current_exists and current_sha not in {str(item["before_sha256"]), str(item["after_sha256"])}:
                raise RecoveryRequired(f"{relative} 当前字节不是记录的 before/after")
            if not current_exists and current_sha not in {str(item["before_sha256"]), str(item["after_sha256"])}:
                raise RecoveryRequired(f"{relative} 当前缺失且不是记录的 before")
            if current_sha != str(item["after_sha256"]) or current_exists != bool(item["after_exists"]):
                _write_participant(target, after, exists=bool(item["after_exists"]))
        receipt = self._commit_marker(manifest, recovered=True)
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass
        _fsync_dir(self.root)
        return receipt

    def recover(self) -> dict[str, object] | None:
        """Replay the one pending transaction, if any, to COMMITTED."""
        with self._lock():
            return self._recover_locked()

    def _commit_marker(self, manifest: Mapping[str, object], *, recovered: bool = False) -> dict[str, object]:
        txid = str(manifest["txid"])
        receipt = {
            "schema_version": COMMIT_SCHEMA_VERSION,
            "txid": txid,
            "phase": PHASE_COMMIT,
            "committed_at": time.time(),
            "recovered": bool(recovered),
            "participants": list((manifest.get("participants") or {}).keys()),
        }
        _atomic_bytes(self._transaction_dir(txid) / "COMMITTED.json", _canonical(receipt))
        return receipt

    def commit(self, participants: Mapping[str, Path | str | None], after: Mapping[str, bytes], *,
               txid: str | None = None, request_id: str | None = None,
               expected_revision: int | None = None) -> dict[str, object]:
        """Commit fixed participant paths with PRECHECK -> PREPARE -> APPLY -> COMMIT."""
        names = set(participants)
        if not names or len(names) > MAX_PARTICIPANTS or not names.issubset({"state", "backup", "report", "seal"}):
            raise FindingCommitError("participants 必须是固定且不超过四个路径")
        if set(after) != names:
            raise FindingCommitError("after 必须覆盖全部 participants")
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
                raise FindingCommitError("expected_revision 无效")
            try:
                state_payload = json.loads(after["state"].decode("utf-8"))
            except (AttributeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise FindingCommitError("state after 无法校验 revision") from exc
            if not isinstance(state_payload, Mapping) or state_payload.get("revision") != expected_revision:
                raise FindingCommitError("state after revision 与 expected_revision 不一致")
        txid = txid or str(uuid.uuid4())
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
            raise FindingCommitError("request_id 无效")
        with self._lock():
            recovered = self._recover_locked()
            if recovered is not None and self.pending_path.exists():
                raise RecoveryRequired("无法在已有 pending transaction 后继续提交")
            targets: dict[str, Path] = {}
            entries: dict[str, dict[str, object]] = {}
            for name, raw_path in participants.items():
                if raw_path is None:
                    raise FindingCommitError(f"participant {name} 路径缺失")
                target = _safe_participant(self.state_root, Path(raw_path))
                existing, before = _read_existing(target)
                payload = after[name]
                if not isinstance(payload, bytes):
                    raise FindingCommitError(f"participant {name} after 必须是 bytes")
                if len(payload) > MAX_PAYLOAD_BYTES:
                    raise FindingCommitError(f"participant {name} 超过大小限制")
                targets[name] = target
                entries[name] = {
                    "path": name,
                    "target": str(target),
                    "before_exists": existing,
                    "after_exists": True,
                    "before_sha256": _sha256(before),
                    "after_sha256": _sha256(payload),
                }
            directory = self._transaction_dir(txid)
            if directory.exists():
                raise FindingCommitError(f"txid 已存在: {txid}")
            directory.mkdir(parents=True, mode=0o700)
            for name, target in targets.items():
                existing, before = _read_existing(target)
                _atomic_bytes(directory / f"{name}.before", before)
                _atomic_bytes(directory / f"{name}.after", after[name])
            manifest = {
                "schema_version": COMMIT_SCHEMA_VERSION,
                "txid": txid,
                "request_id": request_id,
                "expected_revision": expected_revision,
                "phase": PHASE_PREPARE,
                "created_at": time.time(),
                "participants": entries,
            }
            _atomic_bytes(directory / "manifest.json", _canonical(manifest))
            _call_failpoint(self.failpoint, "after-prepare")
            _atomic_bytes(self.pending_path, _canonical({"schema_version": COMMIT_SCHEMA_VERSION, "txid": txid}))
            _call_failpoint(self.failpoint, "after-prepared")
            manifest["phase"] = PHASE_APPLY
            _atomic_bytes(directory / "manifest.json", _canonical(manifest))
            for name, target in targets.items():
                _write_participant(target, after[name], exists=True)
                _call_failpoint(self.failpoint, f"after-apply-{name}")
            _call_failpoint(self.failpoint, "after-apply")
            manifest["phase"] = PHASE_COMMIT
            _atomic_bytes(directory / "manifest.json", _canonical(manifest))
            receipt = self._commit_marker(manifest)
            try:
                self.pending_path.unlink()
            except FileNotFoundError:
                pass
            _fsync_dir(self.root)
            _call_failpoint(self.failpoint, "after-commit")
            return receipt


def commit_finding_transaction(state_root: Path, *, state_path: Path, backup_path: Path | None = None,
                               report_path: Path | None = None, seal_path: Path | None = None,
                               state_bytes: bytes, report_bytes: bytes | None = None,
                               seal_bytes: bytes | None = None, failpoint: Callable[[str], object] | None = None,
                               txid: str | None = None, request_id: str | None = None,
                               expected_revision: int | None = None) -> dict[str, object]:
    """Convenience entry point for the Supervisor state/report/seal trio."""
    participants: dict[str, Path] = {"state": Path(state_path)}
    after: dict[str, bytes] = {"state": state_bytes}
    if backup_path is not None:
        participants["backup"] = Path(backup_path)
        after["backup"] = state_bytes
    if report_path is not None and report_bytes is not None:
        participants["report"] = Path(report_path)
        after["report"] = report_bytes
    if seal_path is not None and seal_bytes is not None:
        participants["seal"] = Path(seal_path)
        after["seal"] = seal_bytes
    if report_bytes is not None and seal_bytes is not None:
        try:
            seal = json.loads(seal_bytes.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise FindingCommitError("seal after 无法解析") from exc
        if not isinstance(seal, Mapping) or seal.get("report_sha256") != _sha256(report_bytes):
            raise FindingCommitError("seal.report_sha256 与 report after 不一致")
        try:
            state_payload = json.loads(state_bytes.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise FindingCommitError("state after 无法解析") from exc
        if isinstance(state_payload, Mapping) and state_payload.get("report_seal") not in (None, dict(seal)):
            raise FindingCommitError("state.report_seal 与 seal after 不一致")
    return FindingCommitLog(state_root, failpoint=failpoint).commit(
        participants, after, txid=txid, request_id=request_id,
        expected_revision=expected_revision,
    )


def recover_finding_commit(state_root: Path) -> dict[str, object] | None:
    return FindingCommitLog(state_root).recover()


# The log object is the protocol entry point; retain a descriptive alias for
# callers that name it after the transaction rather than its storage medium.
FindingCommit = FindingCommitLog


__all__ = [
    "MAX_PARTICIPANTS", "MAX_PAYLOAD_BYTES", "MAX_MANIFEST_BYTES", "COMMIT_SCHEMA_VERSION",
    "PHASE_PRECHECK", "PHASE_PREPARE", "PHASE_APPLY", "PHASE_COMMIT", "PHASE_CLEANUP",
    "PRECHECK", "PREPARE", "APPLY", "COMMIT", "CLEANUP",
    "RECOVERY_REQUIRED", "FindingCommitError", "RecoveryRequired", "FindingCommitLog", "FindingCommit",
    "commit_finding_transaction", "recover_finding_commit",
]
