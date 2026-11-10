"""Read-only, bounded snapshot capture for ``post --dry-run``.

This module deliberately does not import or call post-phase helpers.  Its only
filesystem access is rooted at an already-open project directory and every
component is opened with ``O_NOFOLLOW``.  The returned snapshot retains the raw
bytes used for its hashes so callers can evaluate that exact capture.

A declared source that simply does not exist yet is *not* the same failure as a
source that exists but is unsafe to read.  Missing paths get ``source_missing``
while permission/IO failures keep ``source_unavailable_or_unsafe``; sources
declared optional (``OPTIONAL_SOURCES``) are recorded as ``watched_absent``
instead of aborting the preflight, and remain fail-closed when a caller
promotes them through ``capture_snapshot(required_sources=...)``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Collection, Mapping
from urllib.parse import quote


JSON_LIMIT = 16 * 1024 * 1024
# Keep the post snapshot reader aligned with Supervisor's findings sync
# contract.  A smaller private cap would reject reports that sync accepts.
FINDINGS_LIMIT = 16 * 1024 * 1024
ARTIFACT_LIMIT = 8 * 1024 * 1024
SNAPSHOT_LIMIT = 64 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
NOT_EVALUATED = (
    "lease_refresh", "checkpoint_apply", "test_execution", "settlement",
    "network", "git_hook", "report_publication", "next_round_start",
)
GUARANTEE = "仅表示 observed_at 时的读取结果；不锁定后续状态，也不保证最终 post 成功。"

# "文件不存在"与"文件存在但不安全"是两种不同事实，必须给出不同原因码。
SOURCE_MISSING = "source_missing"

# 快照源的必需性声明。``findings.json`` 由结算阶段落盘，post 预检往往早于它，
# 因此默认按可选源处理：缺失时记为 ``watched_absent`` 而不是中止预检；下游阶段
# 真正需要它时用 ``capture_snapshot(required_sources={"findings"})`` 提升为必需，
# 缺失即按 ``source_missing`` fail-closed。其余具名源仍恒为必需。
OPTIONAL_SOURCES = frozenset({"findings"})

# ENOENT/ENOTDIR（父目录缺失或不是目录）都只说明路径当前没有可读文件，属于"尚未
# 落盘"的正常状态；其余 errno 一律按不可用处理。O_NOFOLLOW 命中 symlink 时 open
# 以 ELOOP 失败，那也不是 IO 故障，而是"目标不是普通文件"的安全判定。
_MISSING_ERRNOS = frozenset(
    code for code in (getattr(errno, "ENOENT", None), getattr(errno, "ENOTDIR", None))
    if code is not None
)
_SYMLINK_ERRNOS = frozenset(
    code for code in (getattr(errno, "ELOOP", None),) if code is not None
)


def _read_failure_reason(exc: OSError) -> str:
    """把 ``OSError`` 归一为稳定原因码；原因码绝不回显主机绝对路径。"""
    if isinstance(exc, FileNotFoundError) or exc.errno in _MISSING_ERRNOS:
        return SOURCE_MISSING
    if exc.errno in _SYMLINK_ERRNOS:
        return "source_not_regular"
    return "source_unavailable_or_unsafe"


class SnapshotInvalid(ValueError):
    """A fail-closed snapshot read failure with a stable reason code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class SourceSnapshot:
    path: str
    raw_bytes: bytes = field(repr=False)
    sha256: str
    device: int
    inode: int
    size: int
    limit: int
    kind: str


@dataclass(frozen=True)
class FrozenSnapshot:
    root: Path
    observed_at: str
    snapshot_id: str
    sources: Mapping[str, SourceSnapshot] = field(repr=False)
    documents: Mapping[str, Any] = field(repr=False)
    coherence: str
    blockers: tuple[Mapping[str, str], ...]
    watched_absent: tuple[str, ...] = ()
    not_evaluated: tuple[str, ...] = NOT_EVALUATED


@dataclass(frozen=True)
class DryRunResult:
    report: Mapping[str, Any]
    snapshot: FrozenSnapshot | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.report)


def _relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SnapshotInvalid("source_outside_project_root") from exc
    value = relative.as_posix()
    if not value or value == "." or "\\" in value:
        raise SnapshotInvalid("source_path_invalid")
    return value


class RootReader:
    """Open project-relative paths without following any path component."""

    def __init__(self, root: Path):
        self.root = Path(os.path.abspath(os.fspath(root)))
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.fd = os.open(self.root, flags)
        except OSError as exc:
            raise SnapshotInvalid("project_root_unavailable") from exc

    def close(self) -> None:
        os.close(self.fd)

    @staticmethod
    def _parts(relative: str) -> tuple[str, ...]:
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", ".."} or "\\" in part for part in path.parts
        ):
            raise SnapshotInvalid("source_path_invalid")
        return path.parts

    def _open(self, relative: str) -> int:
        parts = self._parts(relative)
        parent = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                child = os.open(part, flags, dir_fd=parent)
                os.close(parent)
                parent = child
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            return os.open(parts[-1], flags, dir_fd=parent)
        except OSError as exc:
            raise SnapshotInvalid(_read_failure_reason(exc)) from exc
        finally:
            os.close(parent)

    def exists(self, relative: str) -> bool:
        """报告普通文件是否存在；缺失返回 False，其余不安全事实仍 fail-closed。"""
        parts = self._parts(relative)
        parent = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                child = os.open(part, flags, dir_fd=parent)
                os.close(parent)
                parent = child
            try:
                item = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
                raise SnapshotInvalid("source_not_regular")
            return True
        except OSError as exc:
            # 父目录缺失同样只是"还没有文件"，不能让可选源把它变成安全事件。
            reason = _read_failure_reason(exc)
            if reason == SOURCE_MISSING:
                return False
            raise SnapshotInvalid(reason) from exc
        finally:
            os.close(parent)

    def read(self, relative: str, limit: int, kind: str) -> SourceSnapshot:
        fd = self._open(relative)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise SnapshotInvalid("source_not_regular")
            if before.st_size > limit:
                raise SnapshotInvalid("source_size_limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise SnapshotInvalid("source_size_limit")
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            ) or total != after.st_size:
                raise SnapshotInvalid("source_changed_during_read")
            raw = b"".join(chunks)
            return SourceSnapshot(
                path=relative,
                raw_bytes=raw,
                sha256=hashlib.sha256(raw).hexdigest(),
                device=before.st_dev,
                inode=before.st_ino,
                size=total,
                limit=limit,
                kind=kind,
            )
        finally:
            os.close(fd)


def _json(source: SourceSnapshot) -> Any:
    try:
        value = json.loads(source.raw_bytes.decode("utf-8"))
        if not isinstance(value, dict):
            raise SnapshotInvalid("source_json_invalid")
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, SnapshotInvalid):
            raise
        raise SnapshotInvalid("source_json_invalid") from exc


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _is_json_integer(value: Any, minimum: int) -> bool:
    """Reject bool explicitly because Python treats bool as an int."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def verify_supervisor_history(state: Any) -> bool:
    if not isinstance(state, Mapping) or not isinstance(state.get("history"), (list, tuple)):
        return False
    state_revision = state.get("revision")
    if not _is_json_integer(state_revision, 0):
        return False
    history = state["history"]
    base_hash = state.get("history_base_hash", "")
    audit_head = state.get("audit_head", "")
    if (
        not isinstance(base_hash, str)
        or (base_hash and not SHA256_RE.fullmatch(base_hash))
        or not isinstance(audit_head, str)
    ):
        return False
    previous = state.get("history_base_hash", "")
    expected = state.get("history_base_seq", 1)
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        return False
    try:
        previous_revision = 0
        for event in history:
            event_seq = event.get("seq") if isinstance(event, Mapping) else None
            if (
                not isinstance(event, Mapping)
                or isinstance(event_seq, bool)
                or not isinstance(event_seq, int)
                or event_seq != expected
            ):
                return False
            event_revision = event.get("revision")
            if (not _is_json_integer(event_revision, 1)
                    or event_revision < previous_revision
                    or event_revision > state_revision):
                return False
            event_previous = event.get("prev_hash", "")
            if (
                not isinstance(event_previous, str)
                or (event_previous and not SHA256_RE.fullmatch(event_previous))
                or event_previous != previous
            ):
                return False
            event_body = {key: value for key, value in event.items() if key != "hash"}
            digest = hashlib.sha256(_canonical_json(event_body)).hexdigest()
            if event.get("hash") != digest:
                return False
            previous = digest
            previous_revision = event_revision
            expected += 1
    except (TypeError, ValueError, RecursionError):
        return False
    return audit_head == previous


def _verify_findings_seal(runtime: Any, supervisor: Any,
                          findings_source: SourceSnapshot | None) -> str | None:
    """校验收到的 findings 字节是否等于 Supervisor 记录的内容哈希。

    ``findings_source`` 为 ``None`` 表示该源在本阶段合法缺失：此时没有可比对的
    字节，只要 Supervisor 也没有为其登记封存事件就无证明义务；一旦登记了封存
    事件却读不到文件，那才是真正的矛盾，仍要 fail-closed。
    """
    if not isinstance(runtime, Mapping) or not isinstance(supervisor, Mapping):
        return "findings_seal_binding_missing"
    history = supervisor.get("history")
    if not isinstance(history, (list, tuple)):
        return "findings_seal_history_missing"
    runtime_lifecycle = runtime.get("lifecycle_id")
    runtime_round = runtime.get("round")
    if not isinstance(runtime_lifecycle, str) or not runtime_lifecycle.strip():
        return "findings_seal_binding_missing"
    if not _is_json_integer(runtime_round, 1):
        return "findings_seal_binding_missing"
    best: tuple[int, str] | None = None
    for event in history:
        if not isinstance(event, Mapping) or event.get("type") not in {
            "finding_decisions_synced", "findings_report_sealed",
        }:
            continue
        digest = event.get("report_sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            continue
        event_lifecycle = event.get("lifecycle_id")
        if not isinstance(event_lifecycle, str) or not event_lifecycle.strip():
            continue
        event_round = event.get("round_no")
        if not _is_json_integer(event_round, 1):
            continue
        if event_lifecycle != runtime_lifecycle or event_round != runtime_round:
            continue
        seq = event.get("seq")
        order = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
        if best is None or order >= best[0]:
            best = (order, digest.lower())
    if best is None:
        # 没有封存事件：源缺失时无可证明的矛盾，存在时才是缺 seal。
        return None if findings_source is None else "findings_seal_missing"
    if findings_source is None:
        return "findings_seal_mismatch"
    if best[1] != findings_source.sha256:
        return "findings_seal_mismatch"
    return None


def _referenced_artifacts(value: Any):
    """Yield (path, expected hash) pairs from captured claims/evidence/receipts."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            artifact_path = current.get("artifact_path")
            if isinstance(artifact_path, str) and artifact_path.strip():
                expected = current.get("artifact_sha256")
                yield artifact_path, expected if isinstance(expected, str) else None
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _snapshot_id(sources: Mapping[str, SourceSnapshot]) -> str:
    digest = hashlib.sha256()
    for path in sorted(sources, key=lambda item: item.encode("utf-8")):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sources[path].sha256))
    return digest.hexdigest()


def _identity_coherence(documents: Mapping[str, Any], current: Any,
                        absent_sources: Collection[str] = ()) -> tuple[str, list[dict[str, str]]]:
    """比对身份一致性；``absent_sources`` 是本阶段合法缺失的源名。

    合法缺失的可选源没有任何字节可比：它既不参与 ``run_id``/binding 比对，也不
    承担生成标记的证明义务——否则"产物尚未落盘"会被误判成"身份不可证明"。
    未声明为可缺失的源仍按 ``identity_source_missing`` fail-closed。
    """
    blockers: list[dict[str, str]] = []
    absent = frozenset(absent_sources)
    runtime = documents.get("runtime")
    supervisor = documents.get("supervisor")
    findings = documents.get("findings")
    # runtime/supervisor/current 在捕获阶段恒为必需源；缺失即无法证明身份。
    if not all(isinstance(item, Mapping) for item in (runtime, supervisor, current)):
        return "unproven", [{"gate_id": "identity_coherence", "reason_code": "identity_source_missing"}]
    findings_present = isinstance(findings, Mapping)
    if not findings_present and "findings" not in absent:
        return "unproven", [{"gate_id": "identity_coherence", "reason_code": "identity_source_missing"}]
    lifecycle_id = runtime.get("lifecycle_id")
    current_lifecycle = current.get("lifecycle_id")
    if (not isinstance(lifecycle_id, str) or not lifecycle_id.strip()
            or current_lifecycle != lifecycle_id):
        blockers.append({"gate_id": "identity_coherence", "reason_code": "lifecycle_binding_mismatch"})

    runtime_round = runtime.get("round")
    supervisor_round = supervisor.get("round_no")
    if not _is_json_integer(runtime_round, 1):
        blockers.append({"gate_id": "identity_coherence", "reason_code": "runtime_round_invalid"})
    if not _is_json_integer(supervisor_round, 1):
        blockers.append({"gate_id": "identity_coherence", "reason_code": "supervisor_round_invalid"})
    elif _is_json_integer(runtime_round, 1) and supervisor_round != runtime_round:
        blockers.append({"gate_id": "identity_coherence", "reason_code": "supervisor_round_mismatch"})
    if supervisor.get("round_id") != lifecycle_id:
        blockers.append({"gate_id": "identity_coherence", "reason_code": "supervisor_lifecycle_mismatch"})

    runtime_session_values: list[str] = []
    for field_name in ("polling_id", "session_id"):
        value = runtime.get(field_name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            blockers.append({"gate_id": "identity_coherence", "reason_code": "runtime_session_invalid"})
            continue
        runtime_session_values.append(value)
    if len(runtime_session_values) == 2 and runtime_session_values[0] != runtime_session_values[1]:
        blockers.append({"gate_id": "identity_coherence", "reason_code": "runtime_session_conflict"})
    session_identity = runtime_session_values[0] if runtime_session_values else None
    current_polling = current.get("polling_id")
    supervisor_session = supervisor.get("session_id")
    if session_identity is not None:
        if current_polling != session_identity:
            blockers.append({"gate_id": "identity_coherence", "reason_code": "current_session_mismatch"})
        if supervisor_session != session_identity:
            blockers.append({"gate_id": "identity_coherence", "reason_code": "supervisor_session_mismatch"})
    elif current_polling is not None or supervisor_session is not None:
        blockers.append({"gate_id": "identity_coherence", "reason_code": "legacy_session_context_mismatch"})

    # ``run_id`` is the runtime state's authoritative identity.  A fallback to
    # a similarly named extension field would let a malformed or tampered
    # authoritative value be masked by stale secondary data.
    runtime_uuid = runtime.get("run_id")
    if not isinstance(runtime_uuid, str) or not runtime_uuid.strip():
        blockers.append({"gate_id": "identity_coherence", "reason_code": "runtime_run_id_missing"})
        allowed_run_ids: set[str] = set()
    else:
        allowed_run_ids = {runtime_uuid}
    if isinstance(lifecycle_id, str) and lifecycle_id.strip():
        if session_identity is not None:
            allowed_run_ids.update({
                f"{session_identity}-{lifecycle_id}",
                f"lifecycle-{session_identity}-{lifecycle_id}",
            })
        else:
            allowed_run_ids.add(f"lifecycle-{lifecycle_id}")

    for owner, document, required in (
        ("supervisor", supervisor, True),
        ("findings", findings, False),
        ("current", current, False),
    ):
        if not isinstance(document, Mapping):
            continue
        run_id = document.get("run_id")
        if run_id is None and not required:
            continue
        if not isinstance(run_id, str) or not run_id.strip():
            blockers.append({"gate_id": "identity_coherence", "reason_code": f"{owner}_run_id_invalid"})
        elif run_id not in allowed_run_ids:
            blockers.append({"gate_id": "identity_coherence", "reason_code": f"{owner}_run_mismatch"})

    # findings.json 合法缺失时没有任何 binding 可比对，跳过而非判为缺失。
    if findings_present:
        binding = findings.get("run_binding")
        if not isinstance(binding, dict):
            blockers.append({"gate_id": "identity_coherence", "reason_code": "findings_binding_missing"})
        else:
            binding_run_id = binding.get("run_id")
            if not isinstance(binding_run_id, str) or not binding_run_id.strip():
                blockers.append({"gate_id": "identity_coherence", "reason_code": "findings_run_id_invalid"})
            elif binding_run_id not in allowed_run_ids:
                blockers.append({"gate_id": "identity_coherence", "reason_code": "findings_run_mismatch"})
            if not isinstance(binding.get("lifecycle_id"), str) or binding.get("lifecycle_id") != lifecycle_id:
                blockers.append({"gate_id": "identity_coherence", "reason_code": "findings_lifecycle_mismatch"})
            binding_round = binding.get("round")
            if not _is_json_integer(binding_round, 1):
                blockers.append({"gate_id": "identity_coherence", "reason_code": "findings_round_invalid"})
            elif _is_json_integer(runtime_round, 1) and binding_round != runtime_round:
                blockers.append({"gate_id": "identity_coherence", "reason_code": "findings_round_mismatch"})
    # A shared generation token is the only cross-file indication that the
    # three independently persisted JSON documents belong to one state
    # publication.  It is deliberately required in addition to matching
    # identities: without it, a snapshot can combine files from different
    # writes and must remain unproven.  ``generation_id`` is retained as the
    # explicit alias used by older state writers; if both aliases are present
    # they must agree exactly.  A source that is legitimately absent at this
    # phase contributes no token and therefore no proof obligation.
    generation_names = ("snapshot_generation", "generation_id")
    generations: list[str | None] = []
    for item in (runtime, supervisor, findings):
        if not isinstance(item, Mapping):
            continue
        values = [item[name] for name in generation_names if name in item]
        if not values or any(not isinstance(value, str) or not value.strip() for value in values):
            generations.append(None)
            continue
        if len(set(values)) != 1:
            generations.append(None)
            blockers.append({"gate_id": "identity_coherence", "reason_code": "generation_conflict"})
            continue
        generations.append(values[0])
    if not all(generations):
        blockers.append({"gate_id": "identity_coherence", "reason_code": "generation_unproven"})
    elif len(set(generations)) != 1:
        blockers.append({"gate_id": "identity_coherence", "reason_code": "generation_mismatch"})
    return ("proven" if not blockers else "unproven"), blockers


def _ledger_metadata(reader: RootReader, relative: str, sources: dict[str, SourceSnapshot],
                     total: int, watched_absent: list[str]) -> tuple[dict[str, Any], int]:
    """Capture ledger bytes; only inspect SQLite when it is provably immutable."""
    try:
        present = reader.exists(relative)
    except SnapshotInvalid:
        return {"status": "not_evaluated", "reason_code": "ledger_source_unavailable"}, total
    if not present:
        return {"status": "not_evaluated", "reason_code": "ledger_missing"}, total
    source = reader.read(relative, SNAPSHOT_LIMIT, "sqlite")
    if total + source.size > SNAPSHOT_LIMIT:
        raise SnapshotInvalid("snapshot_size_limit")
    sources[relative] = source
    total += source.size
    sidecars = [relative + suffix for suffix in ("-wal", "-shm", "-journal")]
    for path in sidecars:
        if reader.exists(path):
            return {"status": "not_evaluated", "reason_code": "ledger_sidecar_present"}, total
        watched_absent.append(path)
    # immutable=1 prevents SQLite from creating lock/SHM files. The DB is opened
    # read-only and query_only is enforced before the single read transaction.
    fd = reader._open(relative)
    try:
        fd_path = f"/proc/self/fd/{fd}" if Path("/proc/self/fd").is_dir() else f"/dev/fd/{fd}"
        uri = "file:" + quote(fd_path, safe="/") + "?mode=ro&immutable=1"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT value FROM meta WHERE key='state_hash' LIMIT 1"
                ).fetchone()
                connection.execute("COMMIT")
                return {"status": "captured", "state_hash": row[0] if row else None}, total
            finally:
                connection.close()
        except sqlite3.Error:
            return {"status": "not_evaluated", "reason_code": "ledger_read_unsafe"}, total
    finally:
        os.close(fd)


def capture_snapshot(project_root: str | os.PathLike[str], *,
                     required_sources: Collection[str] = ()) -> FrozenSnapshot:
    """Capture immutable bytes for post preflight without repairing any state.

    ``required_sources`` 把默认可选（``OPTIONAL_SOURCES``）的具名源在本阶段提升
    为必需：可选源缺失时记入 ``watched_absent`` 继续评估，必需源缺失则按
    ``source_missing`` fail-closed。
    """
    required = frozenset(required_sources)
    unknown = required - OPTIONAL_SOURCES
    if unknown:
        raise SnapshotInvalid("source_declaration_invalid")
    root = Path(project_root).expanduser().resolve()
    try:
        import core.bugx_paths as bugx_paths
        scope = bugx_paths.require(root)
    except Exception as exc:
        raise SnapshotInvalid("project_layout_invalid") from exc

    named_paths = {
        "current": root / ".bugx" / "current.json",
        "runtime": scope.runtime,
        "life": scope.life,
        "supervisor": scope.supervisor,
        "coverage_evidence": scope.coverage,
        "coverage_manifest": scope.state_root / "module-coverage.md",
        "prep": scope.prep_record,
        "findings": scope.lifecycle_root / "findings.json",
    }
    paths = {name: _relative_path(path, root) for name, path in named_paths.items()}
    wal_path = _relative_path(
        scope.life.with_name(f".{scope.life.name}.settlement-wal.json"), root,
    )
    ledger_path = _relative_path(scope.knowledge_db, root)
    reader = RootReader(root)
    sources: dict[str, SourceSnapshot] = {}
    documents: dict[str, Any] = {}
    blockers: list[dict[str, str]] = []
    watched_absent: list[str] = []
    absent_names: set[str] = set()
    total = 0
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        limits = {
            "findings": FINDINGS_LIMIT,
            "coverage_manifest": JSON_LIMIT,
            "prep": JSON_LIMIT,
        }
        json_names = {"current", "runtime", "life", "supervisor", "coverage_evidence", "findings"}
        for name, relative in paths.items():
            optional = name in OPTIONAL_SOURCES and name not in required
            try:
                source = reader.read(relative, limits.get(name, JSON_LIMIT), "json" if name in json_names else "text")
            except SnapshotInvalid as exc:
                # 只有"缺失"且本阶段不需要该源时才降级为 watched_absent；
                # 不安全、超限、读到半截等事实一律继续 fail-closed。
                if not optional or exc.reason_code != SOURCE_MISSING:
                    raise
                watched_absent.append(relative)
                absent_names.add(name)
                continue
            total += source.size
            if total > SNAPSHOT_LIMIT:
                raise SnapshotInvalid("snapshot_size_limit")
            sources[relative] = source
            if name in json_names:
                documents[name] = _json(source)

        current = documents["current"]
        if (current.get("polling_id") != scope.polling_root.name
                or current.get("lifecycle_id") != scope.lifecycle_root.name):
            blockers.append({"gate_id": "current_pointer", "reason_code": "current_pointer_mismatch"})

        if reader.exists(wal_path):
            source = reader.read(wal_path, JSON_LIMIT, "json")
            total += source.size
            if total > SNAPSHOT_LIMIT:
                raise SnapshotInvalid("snapshot_size_limit")
            sources[wal_path] = source
            try:
                documents["settlement_wal"] = _json(source)
            except SnapshotInvalid:
                blockers.append({"gate_id": "settlement_wal", "reason_code": "settlement_wal_invalid"})
            blockers.append({"gate_id": "settlement_wal", "reason_code": "pending_settlement_wal"})
        else:
            watched_absent.append(wal_path)

        # Capture every artifact referenced by persisted claims, receipts, or
        # coverage records. Each artifact is hashed from the same bytes retained.
        artifact_expectations: dict[str, set[str]] = {}
        for document in (documents.get("supervisor"), documents.get("coverage_evidence"), documents.get("findings")):
            for artifact, expected in _referenced_artifacts(document):
                candidate = PurePosixPath(artifact)
                if candidate.is_absolute() or "\\" in artifact or any(part in {"", ".", ".."} for part in candidate.parts):
                    blockers.append({"gate_id": "artifact_binding", "reason_code": "artifact_path_invalid"})
                    continue
                relative = candidate.as_posix()
                if expected is not None:
                    artifact_expectations.setdefault(relative, set()).add(expected.lower())
                else:
                    blockers.append({"gate_id": "artifact_binding", "reason_code": "artifact_hash_missing"})
                if relative in sources:
                    continue
                source = reader.read(relative, ARTIFACT_LIMIT, "artifact")
                total += source.size
                if total > SNAPSHOT_LIMIT:
                    raise SnapshotInvalid("snapshot_size_limit")
                sources[relative] = source
        for artifact, expected_hashes in artifact_expectations.items():
            actual = sources.get(artifact)
            if not actual:
                blockers.append({"gate_id": "artifact_binding", "reason_code": "artifact_missing"})
            elif len(expected_hashes) != 1 or actual.sha256 not in expected_hashes:
                blockers.append({"gate_id": "artifact_binding", "reason_code": "artifact_hash_mismatch"})

        try:
            ledger_present = reader.exists(ledger_path)
        except SnapshotInvalid:
            ledger_present = None
        if ledger_present is False:
            watched_absent.append(ledger_path)
        ledger, total = _ledger_metadata(reader, ledger_path, sources, total, watched_absent)
        documents["lesson_ledger"] = ledger
        if total > SNAPSHOT_LIMIT:
            raise SnapshotInvalid("snapshot_size_limit")

        # The captured Supervisor JSON contains claims, receipt history and the
        # authoritative finding seal/history; retain those exact bytes once.
        if not verify_supervisor_history(documents.get("supervisor")):
            blockers.append({"gate_id": "supervisor_history", "reason_code": "supervisor_history_seal_invalid"})
        findings_relative = paths["findings"]
        seal_reason = _verify_findings_seal(
            documents.get("runtime"), documents.get("supervisor"),
            sources.get(findings_relative),
        )
        if seal_reason:
            blockers.append({"gate_id": "findings_seal", "reason_code": seal_reason})
        coherence, identity_blockers = _identity_coherence(
            documents, current, absent_sources=absent_names,
        )
        blockers.extend(identity_blockers)

        # Reopen every captured path and compare both identity and content.
        for relative, captured in sources.items():
            check = reader.read(relative, captured.limit, captured.kind)
            if (check.device, check.inode, check.sha256) != (
                captured.device, captured.inode, captured.sha256,
            ):
                raise SnapshotInvalid("source_changed_after_capture")
        # 缺失判定同样是断言：被观察为缺失的可选源若在重检时出现，快照即不可信。
        for relative in watched_absent:
            if reader.exists(relative):
                raise SnapshotInvalid("source_changed_after_capture")

        snapshot = FrozenSnapshot(
            root=root,
            observed_at=observed_at,
            snapshot_id=_snapshot_id(sources),
            sources=MappingProxyType(dict(sources)),
            documents=_deep_freeze(documents),
            coherence=coherence,
            blockers=tuple(MappingProxyType(dict(item)) for item in blockers),
            watched_absent=tuple(sorted(watched_absent, key=lambda item: item.encode("utf-8"))),
        )
        return snapshot
    finally:
        reader.close()


def evaluate_snapshot(snapshot: FrozenSnapshot) -> dict[str, Any]:
    """Purely evaluate captured state; it does not access the filesystem."""
    coherence_proven = snapshot.coherence == "proven"
    gates: list[dict[str, str]] = [
        {"gate_id": "snapshot_integrity", "status": "passed", "reason_code": "snapshot_stable"},
        {
            "gate_id": "coherence",
            "status": "passed" if coherence_proven else "blocked",
            "reason_code": "coherence_proven" if coherence_proven else "coherence_unproven",
        },
    ]
    for gate_id in (
        "current_pointer", "settlement_wal", "supervisor_history", "findings_seal",
        "identity_coherence", "artifact_binding",
    ):
        found = [item for item in snapshot.blockers if item["gate_id"] == gate_id]
        if found:
            for item in found:
                gates.append({"gate_id": gate_id, "status": "blocked", "reason_code": item["reason_code"]})
        elif gate_id in {"current_pointer", "settlement_wal", "supervisor_history"}:
            gates.append({"gate_id": gate_id, "status": "passed", "reason_code": f"{gate_id}_consistent"})
        else:
            gates.append({"gate_id": gate_id, "status": "not_evaluated", "reason_code": "binding_not_proven"})
    if snapshot.documents.get("lesson_ledger", {}).get("status") == "captured":
        gates.append({"gate_id": "lesson_ledger", "status": "passed", "reason_code": "ledger_metadata_readonly"})
    else:
        gates.append({"gate_id": "lesson_ledger", "status": "not_evaluated", "reason_code": snapshot.documents.get("lesson_ledger", {}).get("reason_code", "ledger_unavailable")})
    known_blockers = [dict(item) for item in snapshot.blockers]
    if not coherence_proven:
        known_blockers.append({"gate_id": "coherence", "reason_code": "coherence_unproven"})
    blocked = any(item["status"] == "blocked" for item in gates)
    status = (
        "passed" if coherence_proven and not blocked
        else ("blocked" if snapshot.blockers else "unproven")
    )
    return {
        "snapshot_id": snapshot.snapshot_id,
        "observed_at": snapshot.observed_at,
        "source_hashes": {path: source.sha256 for path, source in sorted(snapshot.sources.items())},
        "watched_absent": list(snapshot.watched_absent),
        "coherence": snapshot.coherence,
        "status": status,
        "gates": gates,
        "known_blockers": known_blockers,
        "not_evaluated": list(snapshot.not_evaluated),
        "guarantee": GUARANTEE,
    }


def blocked_report(reason_code: str) -> dict[str, Any]:
    """构造 fail-closed 的中止报告；``reason_code`` 只含稳定码，不含主机路径。"""
    return {
        "snapshot_id": None,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_hashes": {},
        "watched_absent": [],
        "coherence": "unproven",
        "status": "blocked",
        "gates": [{"gate_id": "snapshot_integrity", "status": "blocked", "reason_code": reason_code}],
        "known_blockers": [{"gate_id": "snapshot_integrity", "reason_code": reason_code}],
        "not_evaluated": list(NOT_EVALUATED),
        "guarantee": GUARANTEE,
    }


def post_dry_run(project_root: str | os.PathLike[str], *,
                 required_sources: Collection[str] = ()) -> DryRunResult:
    """Capture and evaluate a read-only post preflight snapshot."""
    try:
        snapshot = capture_snapshot(project_root, required_sources=required_sources)
    except SnapshotInvalid as exc:
        return DryRunResult(report=blocked_report(exc.reason_code))
    except Exception:
        return DryRunResult(report=blocked_report("snapshot_capture_error"))
    return DryRunResult(report=evaluate_snapshot(snapshot), snapshot=snapshot)

