#!/usr/bin/env python3
"""可验证的覆盖证据存储。

这个模块只负责保存和验证证据元数据，不会执行 ``command`` 字符串。
worker 可以并发调用 :meth:`EvidenceStore.store`/``submit``；每次写入都在
进程内线程锁和文件锁保护下进行，并通过临时文件 + ``os.replace`` 原子提交。
``merge`` 是 supervisor 使用的串行合并入口：同一 ``unit_id`` 的不同版本、
target、artifact 或命令都会返回 ``conflict``，绝不静默覆盖已有记录。

唯一例外是显式 ``allow_supersede=True``（Supervisor 复验路径）：同一 ``unit``
的旧观察可以被更新一轮的观察取代，但不是静默覆盖 —— 被取代记录逐字留档在
``superseded`` 审计数组中，且已通过的结论永不退化为未通过。

证据记录的必需字段为：
``unit_id``、``command``、``exit_code``、``artifact_path``、
``artifact_sha256``、``target_fingerprint``、``observed_at``、``worker_id``、
``status``。目标漂移收卷还可带 ``target_drifted=true``，用于保留
Supervisor 已接纳但不再代表当前目标的证据语义。
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import core.bugx_paths as bugx_paths
from core.retrieval import symbol_index

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback uses msvcrt below.
    fcntl = None

try:  # pragma: no cover - Windows-only module.
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
VALID_STATUSES = frozenset({"passed", "failed", "error", "skipped", "blocked"})
SHA256_RE = __import__("re").compile(r"[0-9a-fA-F]{64}")
REQUIRED_FIELDS = (
    "unit_id",
    "command",
    "exit_code",
    "artifact_path",
    "artifact_sha256",
    "target_fingerprint",
    "observed_at",
    "worker_id",
    "status",
)


class EvidenceError(ValueError):
    """证据输入或持久化状态无效。"""


class EvidenceValidationError(EvidenceError):
    """尝试存储无效证据时抛出的错误。"""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(str(error) for error in errors)
        super().__init__("证据无效: " + "; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """一条不可变的 worker 覆盖证据记录。"""

    unit_id: str
    command: str
    exit_code: int
    artifact_path: str
    artifact_sha256: str
    target_fingerprint: str
    observed_at: str
    worker_id: str
    status: str
    # 可选版本用于阻止同一 unit 的跨版本静默覆盖；旧调用无需提供它。
    version: str | int | None = None
    # Checkpoint receipt identity is retained in the durable manifest.  These
    # fields bind the stored record back to the Supervisor claim; they are not
    # an assertion that an external command was observed executing.
    execution_id: str | None = None
    lease_id: str | None = None
    execution_receipt_sha256: str | None = None
    # Supervisor admits the artifact but marks it non-current when the target
    # changed while the claimed worker was producing evidence.
    target_drifted: bool = False
    # A failed execution can still be valid coverage when it is a Finding-backed
    # reproduction PoC; retain the Supervisor-owned classification through the
    # durable manifest so post can make the same decision after reload.
    reproduced_crash: bool = False
    evidence_type: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvidenceRecord":
        if not isinstance(payload, Mapping):
            raise EvidenceError("证据记录必须是 JSON 对象")
        missing = [field for field in REQUIRED_FIELDS if field not in payload]
        if missing:
            raise EvidenceError("证据缺少字段: " + ", ".join(missing))
        values = {field: payload[field] for field in REQUIRED_FIELDS}
        values["version"] = payload.get("version")
        values["execution_id"] = payload.get("execution_id")
        values["lease_id"] = payload.get("lease_id")
        values["execution_receipt_sha256"] = payload.get(
            "execution_receipt_sha256"
        )
        values["target_drifted"] = payload.get("target_drifted", False)
        values["reproduced_crash"] = payload.get("reproduced_crash", False)
        values["evidence_type"] = payload.get("evidence_type")
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {field: getattr(self, field) for field in REQUIRED_FIELDS}
        if self.version is not None:
            payload["version"] = self.version
        for field in ("execution_id", "lease_id", "execution_receipt_sha256"):
            value = getattr(self, field)
            if value is not None:
                payload[field] = value
        if self.target_drifted:
            payload["target_drifted"] = True
        if self.reproduced_crash:
            payload["reproduced_crash"] = True
        if self.evidence_type is not None:
            payload["evidence_type"] = self.evidence_type
        return payload


@dataclass(frozen=True, slots=True)
class VerificationResult:
    valid: bool
    errors: tuple[str, ...] = ()
    record: EvidenceRecord | None = None

    def __bool__(self) -> bool:
        return self.valid

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "record": self.record.to_dict() if self.record else None,
        }


@dataclass(frozen=True, slots=True)
class StoreResult:
    """worker 提交结果：``stored``、``duplicate``、``superseded`` 或 ``conflict``。"""

    status: str
    unit_id: str
    record: EvidenceRecord | None = None
    existing: EvidenceRecord | None = None
    reason: str = ""

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "unit_id": self.unit_id,
            "record": self.record.to_dict() if self.record else None,
            "existing": self.existing.to_dict() if self.existing else None,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ManifestReport:
    """清单校验或合并报告。

    ``invalid``、``stale``、``duplicate``、``conflicts`` 均为包含 unit 信息的
    字典列表；``missing`` 是缺失的 unit id 列表。``valid`` 只有在所有门禁均
    为空时才为真。
    """

    missing: tuple[str, ...] = ()
    stale: tuple[dict[str, Any], ...] = ()
    invalid: tuple[dict[str, Any], ...] = ()
    duplicate: tuple[dict[str, Any], ...] = ()
    records: tuple[EvidenceRecord, ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()

    @property
    def duplicates(self) -> tuple[dict[str, Any], ...]:
        """复数别名，方便调用方按自然语言命名访问。"""
        return self.duplicate

    @property
    def valid(self) -> bool:
        return not any((self.missing, self.stale, self.invalid, self.duplicate, self.conflicts))

    def __bool__(self) -> bool:
        return self.valid

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "missing": list(self.missing),
            "stale": [dict(item) for item in self.stale],
            "invalid": [dict(item) for item in self.invalid],
            "duplicate": [dict(item) for item in self.duplicate],
            "duplicates": [dict(item) for item in self.duplicate],
            "conflicts": [dict(item) for item in self.conflicts],
            "records": [record.to_dict() for record in self.records],
        }


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """同时覆盖同进程线程和跨进程 worker。

    LOG-04：Windows 上没有 ``fcntl``，而 Worker 是独立进程，仅有进程内
    ``threading.RLock`` 无法阻止跨进程的 read-modify-write 丢更新。这里补
    ``msvcrt.locking`` 分支（与 ``bugx_mcp_server._ControllerLease``、
    ``audit_supervisor._file_lock`` 同型）；POSIX 路径保持 ``fcntl.flock`` 不变。
    锁文件本身也不能被符号链接替换：先做词法预检，再用 ``O_NOFOLLOW`` 原子打开，
    避免 ``is_symlink`` 与 ``open`` 之间的 TOCTOU 窗口。
    """
    lock = _thread_lock(path)
    with lock:
        bugx_paths.assert_no_symlink_components(path, error_cls=EvidenceError)
        path.parent.mkdir(parents=True, exist_ok=True)
        open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, open_flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise EvidenceError(f"拒绝使用符号链接证据锁: {path}") from exc
            raise EvidenceError(f"无法安全打开证据锁: {path}") from exc
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows 由模拟测试覆盖
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover - unsupported platform
                raise EvidenceError(
                    "证据锁在当前平台不可用（fcntl 和 msvcrt 均缺失）"
                )
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows 由模拟测试覆盖
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    """在目标文件同一目录中完成 fsync + replace，避免半写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # 某些文件系统不允许 fsync 目录；replace 本身仍是原子的。
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"无法读取证据 JSON: {path}: {exc}") from exc


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_artifact(raw_path: str, workspace_root: Path) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise EvidenceError("artifact_path 必须是非空字符串")
    root = workspace_root.resolve()
    raw = Path(raw_path)
    candidate = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(str(candidate)))
    if not _within(lexical, root):
        raise EvidenceError(f"artifact_path 逃逸 workspace_root: {raw_path}")
    relative = lexical.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise EvidenceError(f"artifact_path 不能包含符号链接: {raw_path}")
    try:
        resolved = lexical.resolve(strict=False)
    except OSError as exc:
        raise EvidenceError(f"artifact_path 无法解析: {raw_path}: {exc}") from exc
    if not _within(resolved, root):
        raise EvidenceError(f"artifact_path 逃逸 workspace_root: {raw_path}")
    if not resolved.is_file():
        raise EvidenceError(f"artifact 不存在或不是普通文件: {raw_path}")
    return resolved


def sha256_file(path: Path) -> str:
    """计算文件内容的 SHA-256。

    实现只保留一份，位于 ``symbol_index.file_sha256``（QUA-03）；本模块只做
    转发，避免三份分块读取互相漂移。
    """
    return symbol_index.file_sha256(path)


# 兼容模块内既有的私有调用点；实现只此一份。
_sha256 = sha256_file


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError("observed_at 必须是非空 ISO-8601 字符串")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceError(f"observed_at 无法解析: {value}") from exc


def _record_identity(record: EvidenceRecord) -> tuple[Any, ...]:
    """忽略时间和 worker，识别同一份实际证据。"""
    return (
        record.unit_id,
        record.version,
        record.target_fingerprint,
        record.command,
        record.exit_code,
        record.artifact_path,
        record.artifact_sha256.lower(),
        record.status,
        record.reproduced_crash,
        record.evidence_type,
    )


def _supersede_allowed(
    existing: EvidenceRecord,
    incoming: EvidenceRecord,
    *,
    allow_target_rebind: bool = False,
) -> bool:
    """Permit re-verification replacements.

    BX-ENGINE-21：默认仍要求 ``target_fingerprint`` 一致（既有契约）。但在 target
    重绑（workspace 变更后的显式 rebind）之后，旧记录的目标已不是当前目标，若继续
    要求一致，旧证据就成为**不可超越的墓碑**——同一 unit 的任何新证据（含
    ``blocked``）都被判 conflict，该单元永久无法 covered，也无法如实报告受阻。
    ``allow_target_rebind=True`` 由**复验路径**（Supervisor checkpoint pipeline，
    已通过 ``_assert_target_fingerprint`` 校验当前目标）显式开启；
    「已通过证据不得被未通过记录退化」的护栏在任何模式下都不变。
    """
    if existing.version != incoming.version:
        return False
    if (
        existing.target_fingerprint != incoming.target_fingerprint
        and not allow_target_rebind
    ):
        return False
    return not (existing.status == "passed" and incoming.status != "passed")


def _validate_record(record: EvidenceRecord, workspace_root: Path) -> VerificationResult:
    errors: list[str] = []
    string_fields = (
        "unit_id",
        "command",
        "artifact_path",
        "target_fingerprint",
        "worker_id",
    )
    for field in string_fields:
        value = getattr(record, field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} 必须是非空字符串")
    if isinstance(record.exit_code, bool) or not isinstance(record.exit_code, int):
        errors.append("exit_code 必须是整数")
    if not isinstance(record.status, str) or record.status not in VALID_STATUSES:
        errors.append(f"status 必须属于: {sorted(VALID_STATUSES)}")
    if not isinstance(record.target_drifted, bool):
        errors.append("target_drifted 必须是布尔值")
    if not isinstance(record.reproduced_crash, bool):
        errors.append("reproduced_crash 必须是布尔值")
    if record.evidence_type is not None and (
        not isinstance(record.evidence_type, str) or not record.evidence_type.strip()
    ):
        errors.append("evidence_type 必须是非空字符串或省略")
    if isinstance(record.exit_code, int) and not isinstance(record.exit_code, bool):
        if record.status == "passed" and record.exit_code != 0:
            errors.append("status=passed 时 exit_code 必须为 0")
        if record.status in {"failed", "error"} and record.exit_code == 0:
            errors.append(f"status={record.status} 时 exit_code 不能为 0")
    if not isinstance(record.artifact_sha256, str) or not SHA256_RE.fullmatch(
        record.artifact_sha256
    ):
        errors.append("artifact_sha256 必须是 64 位十六进制 SHA-256")
    try:
        _parse_timestamp(record.observed_at)
    except EvidenceError as exc:
        errors.append(str(exc))
    artifact: Path | None = None
    if isinstance(record.artifact_path, str) and record.artifact_path.strip():
        try:
            artifact = _resolve_artifact(record.artifact_path, workspace_root)
        except EvidenceError as exc:
            errors.append(str(exc))
    if artifact is not None and SHA256_RE.fullmatch(str(record.artifact_sha256 or "")):
        actual = _sha256(artifact)
        if actual.lower() != record.artifact_sha256.lower():
            errors.append(
                f"artifact_sha256 不匹配: expected={record.artifact_sha256.lower()} actual={actual}"
            )
    if record.version is not None and (
        isinstance(record.version, bool) or not isinstance(record.version, (str, int))
    ):
        errors.append("version 必须是字符串、整数或省略")
    for field in ("execution_id", "lease_id"):
        value = getattr(record, field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{field} 必须是非空字符串或省略")
    if record.execution_receipt_sha256 is not None and not SHA256_RE.fullmatch(
        str(record.execution_receipt_sha256)
    ):
        errors.append("execution_receipt_sha256 必须是 64 位十六进制 SHA-256 或省略")
    return VerificationResult(not errors, tuple(errors), record if not errors else None)


def _coerce_record(value: EvidenceRecord | Mapping[str, Any]) -> EvidenceRecord:
    if isinstance(value, EvidenceRecord):
        return value
    if isinstance(value, Mapping):
        return EvidenceRecord.from_dict(value)
    raise EvidenceError("manifest 条目必须是 EvidenceRecord 或 JSON 对象")


def _manifest_values(manifest: Any) -> list[Any]:
    if isinstance(manifest, (str, Path)):
        payload = _read_json(Path(manifest))
    else:
        payload = manifest
    if isinstance(payload, Mapping):
        if "records" in payload:
            payload = payload["records"]
        elif all(field in payload for field in REQUIRED_FIELDS):
            payload = [payload]
        else:
            raise EvidenceError("manifest 必须包含 records 数组")
    if isinstance(payload, (str, bytes)) or not isinstance(payload, Iterable):
        raise EvidenceError("manifest 必须是 records 数组或 JSON 对象")
    return list(payload)


def _expected_ids(expected_units: Iterable[str] | Mapping[str, Any] | None) -> set[str]:
    if expected_units is None:
        return set()
    if isinstance(expected_units, Mapping):
        return {str(unit_id) for unit_id in expected_units}
    return {str(unit_id) for unit_id in expected_units}


def _validate_manifest_values(
    values: list[Any],
    workspace_root: Path,
    *,
    expected_units: Iterable[str] | Mapping[str, Any] | None = None,
    expected_targets: Mapping[str, str] | None = None,
    stale_after: timedelta | float | int | None = None,
    now: datetime | None = None,
) -> ManifestReport:
    invalid: list[dict[str, Any]] = []
    duplicate: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    records: list[EvidenceRecord] = []
    indexes_by_unit: dict[str, list[int]] = {}

    for index, value in enumerate(values):
        try:
            record = _coerce_record(value)
        except EvidenceError as exc:
            invalid.append({"index": index, "errors": [str(exc)]})
            continue
        indexes_by_unit.setdefault(record.unit_id, []).append(index)
        result = _validate_record(record, workspace_root)
        if not result.valid:
            invalid.append({"index": index, "unit_id": record.unit_id, "errors": list(result.errors)})
            continue
        records.append(record)
        if expected_targets and record.unit_id in expected_targets:
            if record.target_fingerprint != expected_targets[record.unit_id]:
                stale.append({
                    "index": index,
                    "unit_id": record.unit_id,
                    "reason": "target_fingerprint_changed",
                })
        if stale_after is not None:
            if isinstance(stale_after, timedelta):
                age_limit = stale_after
            else:
                age_limit = timedelta(seconds=float(stale_after))
            observed = _parse_timestamp(record.observed_at)
            current = now or datetime.now(timezone.utc)
            if observed.tzinfo is None and current.tzinfo is not None:
                current = current.replace(tzinfo=None)
            if observed < current - age_limit:
                stale.append({
                    "index": index,
                    "unit_id": record.unit_id,
                    "reason": "observed_at_expired",
                    "observed_at": record.observed_at,
                })

    for unit_id, indexes in indexes_by_unit.items():
        if len(indexes) > 1:
            duplicate.append({"unit_id": unit_id, "indexes": indexes})

    expected = _expected_ids(expected_units)
    present = set(indexes_by_unit)
    missing = tuple(sorted(expected - present))
    return ManifestReport(
        missing=missing,
        stale=tuple(stale),
        invalid=tuple(invalid),
        duplicate=tuple(duplicate),
        records=tuple(records),
    )


class EvidenceStore:
    """工作区内的原子证据存储与 supervisor 合并器。"""

    def __init__(self, path: str | Path, *, workspace_root: str | Path | None = None):
        raw_path = Path(path).expanduser()
        root = Path(workspace_root).expanduser() if workspace_root is not None else raw_path.parent
        self.workspace_root = root.resolve(strict=True)
        candidate = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
        self.path = candidate.resolve(strict=False)
        if not _within(self.path, self.workspace_root):
            raise EvidenceError("证据 manifest 路径逃逸 workspace_root")
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _load_records(self) -> list[EvidenceRecord]:
        if not self.path.exists():
            return []
        payload = _read_json(self.path)
        values = payload.get("records", []) if isinstance(payload, Mapping) else payload
        if not isinstance(values, list):
            raise EvidenceError("证据 manifest 的 records 必须是数组")
        records: list[EvidenceRecord] = []
        for value in values:
            records.append(_coerce_record(value))
        return records

    def records(self) -> list[EvidenceRecord]:
        """读取当前记录快照。"""
        with _exclusive_lock(self.lock_path):
            return self._load_records()

    def retarget(self, unit_ids: Iterable[str], target_fingerprint: str) -> list[str]:
        """把已存在记录的 ``target_fingerprint`` 改写到新目标（审计留档）。

        只用于 ``rebind_target`` 中「本轮重扫确认该单元字节未变」的单元：它们的
        证据仍指向同一段源码，只是目标指纹前滚了。不改这条字段，新的 state 指纹
        与旧证据就会长期不一致，下一次 post 的 checkpoint 结算必然判 stale。
        字节已变的单元一律不要调用——那会把旧结论套到新源码上。
        """
        wanted = {str(unit) for unit in unit_ids}
        if not wanted or not target_fingerprint.strip():
            return []
        with _exclusive_lock(self.lock_path):
            records = self._load_records()
            changed: list[str] = []
            rewritten: list[EvidenceRecord] = []
            for record in records:
                if record.unit_id in wanted and record.target_fingerprint != target_fingerprint:
                    rewritten.append(replace(record, target_fingerprint=target_fingerprint))
                    changed.append(record.unit_id)
                else:
                    rewritten.append(record)
            if changed:
                _atomic_json_write(self.path, self._state(rewritten, self._load_superseded()))
            return sorted(changed)

    def verify(
        self,
        record: EvidenceRecord | Mapping[str, Any] | str,
    ) -> VerificationResult:
        """验证传入记录；传入 unit_id 时验证 store 中的当前记录。"""
        if isinstance(record, str):
            current = next((item for item in self.records() if item.unit_id == record), None)
            if current is None:
                return VerificationResult(False, (f"找不到 unit_id: {record}",))
            record = current
        try:
            coerced = _coerce_record(record)
        except EvidenceError as exc:
            return VerificationResult(False, (str(exc),))
        return _validate_record(coerced, self.workspace_root)

    def _normalise(self, record: EvidenceRecord | Mapping[str, Any]) -> EvidenceRecord:
        coerced = _coerce_record(record)
        result = _validate_record(coerced, self.workspace_root)
        if not result.valid:
            raise EvidenceValidationError(result.errors)
        artifact = _resolve_artifact(coerced.artifact_path, self.workspace_root)
        relative = artifact.relative_to(self.workspace_root).as_posix()
        return replace(coerced, artifact_path=relative, artifact_sha256=coerced.artifact_sha256.lower())

    @staticmethod
    def _state(
        records: Iterable[EvidenceRecord],
        superseded: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "records": [record.to_dict() for record in records],
        }
        entries = [dict(entry) for entry in superseded or ()]
        if entries:
            payload["superseded"] = entries
        return payload

    def _load_superseded(self) -> list[dict[str, Any]]:
        """读取被受控替换的历史记录（仅审计留档，不参与门禁判定）。"""
        if not self.path.exists():
            return []
        payload = _read_json(self.path)
        if not isinstance(payload, Mapping):
            return []
        values = payload.get("superseded") or []
        if not isinstance(values, list):
            raise EvidenceError("证据 manifest 的 superseded 必须是数组")
        return [dict(value) for value in values if isinstance(value, Mapping)]

    def superseded(self) -> list[dict[str, Any]]:
        """公开的审计视图：返回所有被取代的旧记录（含 unit_id 与 replaced）。"""
        with _exclusive_lock(self.lock_path):
            return self._load_superseded()

    def store(
        self,
        record: EvidenceRecord | Mapping[str, Any],
        *,
        allow_supersede: bool = False,
        allow_target_rebind: bool = False,
    ) -> StoreResult:
        """并发安全提交一条证据，不覆盖同 unit 的现有版本（除非显式指定 allow_supersede）。

        ``allow_target_rebind``（BX-ENGINE-21）仅在 ``allow_supersede`` 之上放宽
        「target 必须一致」这一条，用于 target 重绑后的复验，避免旧证据成为不可超越
        的墓碑；「已通过不得退化为未通过」的护栏不受影响。
        """
        incoming = self._normalise(record)
        with _exclusive_lock(self.lock_path):
            records = self._load_records()
            existing_index = next(
                (i for i, item in enumerate(records) if item.unit_id == incoming.unit_id),
                None,
            )
            if existing_index is not None:
                existing = records[existing_index]
                if _record_identity(existing) == _record_identity(incoming):
                    return StoreResult("duplicate", incoming.unit_id, incoming, existing)
                if not allow_supersede:
                    return StoreResult(
                        "conflict",
                        incoming.unit_id,
                        incoming,
                        existing,
                        "同一 unit 的版本、target 或证据内容发生变化",
                    )
                if not _supersede_allowed(
                    existing, incoming, allow_target_rebind=allow_target_rebind,
                ):
                    reason = (
                        "已通过的 coverage 证据不能被未通过记录退化"
                        if existing.status == "passed" and incoming.status != "passed"
                        else "同一 unit 的版本、target 或证据内容发生变化"
                    )
                    return StoreResult(
                        "conflict",
                        incoming.unit_id,
                        incoming,
                        existing,
                        reason,
                    )
                superseded = self._load_superseded()
                superseded.append({
                    "unit_id": incoming.unit_id,
                    "reason": "reverification supersedes a prior observation",
                    "at": incoming.observed_at,
                    "replaced": existing.to_dict(),
                })
                records[existing_index] = incoming
                _atomic_json_write(self.path, self._state(records, superseded))
                return StoreResult("superseded", incoming.unit_id, incoming, existing)

            records.append(incoming)
            _atomic_json_write(self.path, self._state(records))
            return StoreResult("stored", incoming.unit_id, incoming)

    def submit(
        self,
        record: EvidenceRecord | Mapping[str, Any],
        *,
        allow_supersede: bool = False,
        allow_target_rebind: bool = False,
    ) -> StoreResult:
        """worker 侧语义别名；实际写入仍由原子 store 完成。"""
        return self.store(
            record,
            allow_supersede=allow_supersede,
            allow_target_rebind=allow_target_rebind,
        )

    def validate_manifest(
        self,
        manifest: Any,
        *,
        expected_units: Iterable[str] | Mapping[str, Any] | None = None,
        expected_targets: Mapping[str, str] | None = None,
        stale_after: timedelta | float | int | None = None,
        now: datetime | None = None,
    ) -> ManifestReport:
        values = _manifest_values(manifest)
        return _validate_manifest_values(
            values,
            self.workspace_root,
            expected_units=expected_units,
            expected_targets=expected_targets,
            stale_after=stale_after,
            now=now,
        )

    def merge(
        self,
        manifest: Any,
        *,
        expected_units: Iterable[str] | Mapping[str, Any] | None = None,
        expected_targets: Mapping[str, str] | None = None,
        stale_after: timedelta | float | int | None = None,
        now: datetime | None = None,
        allow_supersede: bool = False,
    ) -> ManifestReport:
        """supervisor 串行合并 worker manifest，显式报告 duplicate/conflict。"""
        values = _manifest_values(manifest)
        report = _validate_manifest_values(
            values,
            self.workspace_root,
            expected_units=expected_units,
            expected_targets=expected_targets,
            stale_after=stale_after,
            now=now,
        )
        invalid_indexes = {item.get("index") for item in report.invalid}
        valid_candidates: list[EvidenceRecord] = []
        for index, value in enumerate(values):
            if index in invalid_indexes:
                continue
            try:
                valid_candidates.append(self._normalise(_coerce_record(value)))
            except EvidenceError:
                continue

        duplicates = [dict(item) for item in report.duplicate]
        conflicts: list[dict[str, Any]] = []
        seen: dict[str, EvidenceRecord] = {}
        for candidate in valid_candidates:
            previous = seen.get(candidate.unit_id)
            if previous is not None:
                if _record_identity(previous) != _record_identity(candidate):
                    conflicts.append({
                        "unit_id": candidate.unit_id,
                        "reason": "同一 manifest 中同 unit 的版本或 target 变更",
                    })
                continue
            seen[candidate.unit_id] = candidate

        with _exclusive_lock(self.lock_path):
            current_records = self._load_records()
            current_by_unit = {record.unit_id: record for record in current_records}
            superseded = self._load_superseded()
            changed = False
            for candidate in seen.values():
                existing = current_by_unit.get(candidate.unit_id)
                if existing is None:
                    current_records.append(candidate)
                    current_by_unit[candidate.unit_id] = candidate
                    changed = True
                elif _record_identity(existing) == _record_identity(candidate):
                    duplicates.append({"unit_id": candidate.unit_id, "reason": "store 已存在相同证据"})
                elif allow_supersede and _supersede_allowed(existing, candidate):
                    idx = next(i for i, r in enumerate(current_records) if r.unit_id == candidate.unit_id)
                    superseded.append({
                        "unit_id": candidate.unit_id,
                        "reason": "reverification supersedes a prior observation",
                        "at": candidate.observed_at,
                        "replaced": existing.to_dict(),
                    })
                    current_records[idx] = candidate
                    current_by_unit[candidate.unit_id] = candidate
                    changed = True
                elif allow_supersede and existing.status == "passed" and candidate.status != "passed":
                    conflicts.append({
                        "unit_id": candidate.unit_id,
                        "reason": "已通过的 coverage 证据不能被未通过记录退化",
                    })
                else:
                    conflicts.append({
                        "unit_id": candidate.unit_id,
                        "reason": "store 中同 unit 的版本、target 或证据内容发生变化",
                    })
            if changed:
                _atomic_json_write(self.path, self._state(current_records, superseded))

        return ManifestReport(
            missing=report.missing,
            stale=report.stale,
            invalid=report.invalid,
            duplicate=tuple(duplicates),
            records=report.records,
            conflicts=tuple(conflicts),
        )


def validate_manifest(
    manifest: Any,
    *,
    workspace_root: str | Path | None = None,
    expected_units: Iterable[str] | Mapping[str, Any] | None = None,
    expected_targets: Mapping[str, str] | None = None,
    stale_after: timedelta | float | int | None = None,
    now: datetime | None = None,
) -> ManifestReport:
    """无状态清单校验 API。"""
    values = _manifest_values(manifest)
    root = Path(workspace_root or Path.cwd()).expanduser().resolve(strict=True)
    return _validate_manifest_values(
        values,
        root,
        expected_units=expected_units,
        expected_targets=expected_targets,
        stale_after=stale_after,
        now=now,
    )


def merge(
    manifest: Any,
    store_path: str | Path,
    *,
    workspace_root: str | Path | None = None,
    expected_units: Iterable[str] | Mapping[str, Any] | None = None,
    expected_targets: Mapping[str, str] | None = None,
    stale_after: timedelta | float | int | None = None,
    now: datetime | None = None,
) -> ManifestReport:
    """无状态风格的串行 merge 便捷 API。"""
    store = EvidenceStore(store_path, workspace_root=workspace_root)
    return store.merge(
        manifest,
        expected_units=expected_units,
        expected_targets=expected_targets,
        stale_after=stale_after,
        now=now,
    )


def attach_attack_graph_snapshot(
    evidence_dir: str | Path,
    graph: Any,
) -> Path:
    """DOC-125: 在证据目录或存储目录下原子持久化动态攻击面图谱快照。"""
    target_dir = Path(evidence_dir)
    if target_dir.is_file():
        target_dir = target_dir.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = target_dir / "attack-graph-snapshot.json"

    if hasattr(graph, "to_dict"):
        payload = graph.to_dict()
    elif isinstance(graph, Mapping):
        payload = dict(graph)
    else:
        raise EvidenceError("攻击面图谱对象必须提供 to_dict() 或为 Mapping")

    _atomic_json_write(snapshot_path, payload)
    return snapshot_path


def load_attack_graph_snapshot(evidence_dir: str | Path) -> dict[str, Any] | None:
    """DOC-125: 从证据目录中读取持久化的攻击面图谱快照。"""
    target_dir = Path(evidence_dir)
    if target_dir.is_file():
        target_dir = target_dir.parent
    snapshot_path = target_dir / "attack-graph-snapshot.json"
    if not snapshot_path.is_file():
        return None
    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


__all__ = [
    "EvidenceError",
    "EvidenceRecord",
    "EvidenceStore",
    "EvidenceValidationError",
    "ManifestReport",
    "StoreResult",
    "VALID_STATUSES",
    "VerificationResult",
    "attach_attack_graph_snapshot",
    "load_attack_graph_snapshot",
    "merge",
    "validate_manifest",
]
