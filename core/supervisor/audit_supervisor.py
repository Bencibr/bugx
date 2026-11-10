#!/usr/bin/env python3
"""外部 Supervisor：用独立预算协调并发 bugx worker。

这个模块故意不执行 worker 命令，也不访问网络。worker 只提交结构化的
checkpoint，Supervisor 在文件锁内串行合并结果，并把每次状态变化写入
带 hash-chain 的不可变审计历史。

设计约束：

* ``life_score`` 只是评分字段，永远不参与预算计算；
* 所有状态写入都经过 workspace 内的文件锁和 ``os.replace`` 原子替换；
* claim 是一次性的 worker 身份，过期 claim 只能被标记为 expired，不能重用；
* reset_budget 只创建新的预算 epoch，不删除 findings、coverage 或 history；
* 覆盖未完成时，finish 只能产生 INCOMPLETE 或 BUDGET_EXHAUSTED。
"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import sys
from pathlib import Path

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent

import copy
from core.ledger import finding_lifecycle
import argparse
import errno
import json
import math
import os
import re
import core.secure_atomic as secure_atomic
import subprocess
import threading
import time
import uuid
from core.planning import unit_kind
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

try:  # pragma: no cover - the fallback is for non-POSIX importability.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
try:  # pragma: no cover - Windows-only module.
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]


@contextmanager
def _bounded_os_file_lock(
    descriptor: int, lock_path: Path, deadline: float, timeout: float, *, shared: bool,
):
    if fcntl is not None:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        delay = 0.05
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Supervisor lock acquisition exceeded {timeout:.3g}s: {lock_path}"
                    ) from exc
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.5)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows-only
        delay = 0.05
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Supervisor lock acquisition exceeded {timeout:.3g}s: {lock_path}"
                    ) from exc
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.5)
        try:
            yield
        finally:
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - unsupported platform
        raise OSError("Supervisor lock 在当前平台不可用（fcntl 和 msvcrt 均缺失）")


from core.supervisor import (
    checkpoint_handler,
    finding_workflow,
    lifecycle_blocks,
    round_governor,
    worker_lifecycle,
)
from core.supervisor.supervisor_models import (
    SCHEMA_VERSION,
    DEFAULT_STATE_RELATIVE_PATH,
    LEGACY_STATE_RELATIVE_PATH,
    COVERAGE_PENDING,
    COVERAGE_COVERED,
    COVERAGE_BLOCKED,
    COVERAGE_UNVERIFIED,
    COVERAGE_PARTIAL,
    COVERAGE_NOT_APPLICABLE,
    COVERAGE_STALE,
    COVERAGE_REMOVED,
    COVERAGE_SUPERSEDED,
    COVERAGE_CORRUPTED,
    VALID_COVERAGE_STATES,
    UNIT_LEVEL_DEBT,
    GLOBAL_BLOCKED,
    TERMINAL_COVERED,
    SHA256_RE,
    MAX_SIGNATURES_PER_CHECKPOINT,
    MAX_RECENT_SIGNATURES,
    MAX_SIGNATURE_LENGTH,
    MAX_UNIT_NOTES,
    MAX_UNIT_NOTE_LENGTH,
    note_requests_deep_verification,
    MAX_FINDINGS_PER_CHECKPOINT,
    MAX_COVERAGE_PER_CHECKPOINT,
    MAX_EVIDENCE_PER_CHECKPOINT,
    MAX_COMMAND_LENGTH,
    MAX_ARTIFACT_PATH_LENGTH,
    # Compatibility export: tests and older callers use the chunk-size constant.
    ARTIFACT_HASH_CHUNK_BYTES,
    MAX_FINDING_FINGERPRINT_LENGTH,
    MAX_FINDING_FIELD_LENGTH,
    MAX_JSON_DEPTH,
    MAX_JSON_KEYS,
    MAX_LEASE_SECONDS,
    MAX_WORKER_ATTEMPTS,
    MAX_LIFECYCLE_BLOCKED_PER_GATE,
    MAX_LONG_CALL_SECONDS,
    Status,
    SupervisorError,
    NoRunError,
    RunAlreadyStarted,
    InvalidTransition,
    BudgetExhausted,
    ClaimRejected,
    LeaseExpired,
    RevisionConflict,
    InvalidCheckpoint,
    PersistenceError,
    WorkspacePathError,
    ConcurrencyNotSatisfied,
    canonical_json,
    workspace_fingerprint,
    target_identity,
    invalidate_git_head_cache,
    git_target_fingerprint,
    finite_number,
    check_depth_and_keys,
    RunBudget,
    WorkerClaim,
    GraphRun,
    GraphTask,
    AgentBinding,
    BugRecord,
    load_unit_notes,
    RunState,
    event_hash,
    validate_history,
    confined_path,
    has_lexical_symlink,
    confined_regular_file,
    worker_artifact_root,
    default_coverage_manifest,
    atomic_write_bytes,
    resolve_system_concurrency,
    process_start_token,
)


def _normalize_lexical_system_alias(path: Path) -> Path:
    """Normalize macOS /tmp and /var aliases without following user links."""
    absolute = Path(os.path.abspath(str(path)))
    if sys.platform != "darwin":
        return absolute
    for name in ("tmp", "var"):
        alias = Path("/") / name
        try:
            if alias.resolve(strict=True) != Path("/private") / name:
                continue
            return (Path("/private") / name) / absolute.relative_to(alias)
        except (OSError, ValueError):
            continue
    return absolute

from core.supervisor import (
    active_claim,
    active_resource_keys,
    append_event,
    assert_target_fingerprint,
    claimed_units_this_round,
    close_parallel_round,
    confirmation_gate,
    coverage_evidence_path,
    deadline_reasons,
    drain_dangling_claims,
    eligible_units,
    ensure_live_for_work,
    finding_key,
    normalize_checkpoint_id,
    normalize_coverage,
    normalize_evidence,
    enrich_gate_finding,
    normalize_findings,
    normalize_ids,
    normalize_notes,
    normalize_signatures,
    normalize_worker_id,
    mark_worker_attempts_exhausted,
    observe_clock,
    parallel_gate_result,
    parallel_required,
    publish_checkpoint_evidence,
    recalibrate_round_budget,
    reconcile_parallel,
    refresh,
    adaptive_lease_seconds,
    apply_status_budget,
    refresh_confirmation_gate,
    resolve_session_coverage_ledger_path,
    release_claim,
    release_active_claims,
    renew_claim,
    mark_claim_expired,
    session_coverage_complete,
    ROUND_HEARTBEAT_TIMEOUT_SECONDS,
    set_budget_exhausted,
    soft_limit_reasons,
    validate_parallel_capacity,
    target_gate_result,
)

from core.supervisor.lease_manager import reclaim_orphan_leases

from core.supervisor import finding_sync

from core.supervisor import finish_sync

from core.supervisor import recovery_sync

from core.supervisor import start_sync

from core.supervisor import checkpoint_sync

from core.supervisor import claim_sync

from core.supervisor import next_round_sync

from core.supervisor import context_sync

from core.supervisor import worker_sync

from core.supervisor import graph_sync

from core.supervisor import coverage_sync

from core.supervisor.checkpoint_pipeline import (
    attach_evidence_receipts,
    checkpoint_payload_digest,
    load_checkpoint_payload,
    load_manifest_units,
    merge_coverage_updates,
    migrate_coverage,
    validate_worker_artifacts,
)

from core.supervisor.observability import (
    build_decision_snapshot,
    build_parallel_gate_snapshot,
)

from core.supervisor.state_store import StateStore

import core.supervisor.checkpoint_handler as _ch
_assert_reproducible_contract = _ch._assert_reproducible_contract
_validate_incoming_findings = _ch._validate_incoming_findings

class Supervisor:
    """线程/进程安全的、只追加审计的外部 run Supervisor。"""

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        budget: RunBudget | None = None,
        *,
        state_path: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] | None = None,
        mono_clock: Callable[[], float] | None = None,
        coverage_ledger_path: str | os.PathLike[str] | None = None,
    ) -> None:
        root = Path(workspace_root).resolve()
        if not root.is_dir():
            raise WorkspacePathError(f"workspace_root 不是目录: {root}")
        self.workspace_root = root
        self.coverage_ledger_path = Path(coverage_ledger_path).resolve() if coverage_ledger_path else None
        if state_path is not None:
            raw_state = Path(state_path)
            raw_rel = Path(state_path)
        else:
            canonical = root / DEFAULT_STATE_RELATIVE_PATH
            legacy = root / LEGACY_STATE_RELATIVE_PATH
            if not canonical.exists() and legacy.exists():
                raw_state = legacy
                raw_rel = LEGACY_STATE_RELATIVE_PATH
            else:
                raw_state = canonical
                raw_rel = DEFAULT_STATE_RELATIVE_PATH

        self.state_path = confined_path(root, raw_state)
        self.lock_path = confined_path(
            root, self.state_path.with_name(self.state_path.name + ".lock"),
        )
        self.backup_path = confined_path(
            root, self.state_path.with_name(self.state_path.name + ".bak"),
        )
        # Keep lexical forms to detect inner symlinks when /var -> /private alias
        # makes the canonical check miss the lexical symlink component.
        try:
            lexical_root = _normalize_lexical_system_alias(Path(workspace_root))
            if state_path is None:
                lexical_raw = lexical_root / raw_rel
            else:
                candidate = Path(state_path)
                lexical_raw = (
                    _normalize_lexical_system_alias(candidate)
                    if candidate.is_absolute() else lexical_root / candidate
                )
            self._lexical_root = lexical_root
            self._lexical_state_path = lexical_raw
            self._lexical_backup_path = Path(str(lexical_raw) + ".bak")
            self._lexical_lock_path = Path(str(lexical_raw) + ".lock")
        except Exception:
            # Fallback to canonical paths if lexical computation fails.
            self._lexical_root = self.workspace_root
            self._lexical_state_path = self.state_path
            self._lexical_backup_path = self.backup_path
            self._lexical_lock_path = self.lock_path
        self._state_store = StateStore(
            self.state_path, self.backup_path, self._lexical_root,
            self._lexical_state_path, self._lexical_backup_path,
        )
        invalidate_git_head_cache(root)
        self.target_fingerprint = workspace_fingerprint(root)
        self._configured_budget = budget or RunBudget()
        self._clock = clock or time.time
        # A7 之后「租约是否过期」只读单调时钟（墙钟跳变不得改变结论），因此
        # 过期语义的可测性要求单调时钟**同样可注入**。此前只有墙钟可注入，
        # 假时钟驱动的用例在真实 monotonic 下永不过期 —— run_fault_matrix 的
        # 「心跳丢失与租约回收」因此长期 reclaimed=False（门 5 无法闭合）。
        # 只注入 ``clock`` 时它就是唯一权威时钟（两者同源），否则既有的假时钟
        # 用例会出现「墙钟前进、单调钟不动」的自相矛盾。
        # late binding：``mock.patch("time.monotonic")`` 必须能改变默认源。
        self._mono_clock = mono_clock or (lambda: time.monotonic())
        self._thread_lock = threading.RLock()

    @property
    def budget(self) -> RunBudget:
        """返回创建新 run 或 reset 使用的默认预算。"""

        return self._configured_budget

    @contextmanager
    def _file_lock(self, timeout: float = 10.0):
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Supervisor lock timeout must be a finite non-negative number")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        if not self._thread_lock.acquire(timeout=timeout):
            raise TimeoutError(
                f"Supervisor lock acquisition exceeded {timeout:.3g}s: {self.lock_path}"
            )
        try:
            with self._thread_lock:
                if has_lexical_symlink(self._lexical_root, self._lexical_lock_path):
                    raise WorkspacePathError(f"拒绝使用符号链接 Supervisor 锁: {self.lock_path}")
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(self.lock_path, flags, 0o600)
                except OSError as exc:
                    raise WorkspacePathError(f"无法安全打开 Supervisor 锁: {self.lock_path}") from exc
                with os.fdopen(descriptor, "a+", encoding="utf-8") as lock_handle:
                    with _bounded_os_file_lock(
                        lock_handle.fileno(), self.lock_path, deadline, timeout, shared=False,
                    ):
                        yield
        finally:
            self._thread_lock.release()

    @contextmanager
    def _read_lock(self, timeout: float = 10.0):
        """共享读锁通道 (DOC-138 / S1-5)：允许多个只读查询并发执行，消除读写竞争。"""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Supervisor lock timeout must be a finite non-negative number")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        if not self._thread_lock.acquire(timeout=timeout):
            raise TimeoutError(
                f"Supervisor lock acquisition exceeded {timeout:.3g}s: {self.lock_path}"
            )
        try:
            with self._thread_lock:
                if has_lexical_symlink(self._lexical_root, self._lexical_lock_path):
                    raise WorkspacePathError(f"拒绝使用符号链接 Supervisor 锁: {self.lock_path}")
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(self.lock_path, flags, 0o600)
                except OSError as exc:
                    raise WorkspacePathError(f"无法安全打开 Supervisor 锁: {self.lock_path}") from exc
                with os.fdopen(descriptor, "a+", encoding="utf-8") as lock_handle:
                    with _bounded_os_file_lock(
                        lock_handle.fileno(), self.lock_path, deadline, timeout, shared=True,
                    ):
                        yield
        finally:
            self._thread_lock.release()

    def _read_state_locked(self) -> RunState:
        """Fail-closed read through the dedicated snapshot store."""
        # A5 recovery is forward-only and runs before any reader can pair a
        # partially applied state with a report or seal.
        from core.supervisor.finding_commit import recover_finding_commit
        commit_root = self.state_path.parent.parent
        if commit_root.resolve() == self.workspace_root.resolve():
            commit_root = self.state_path.parent
        if commit_root.is_dir():
            recover_finding_commit(commit_root)
        state = self._state_store.read()
        # DOC-204 L6：裁剪旁路归档路径（lease_manager.append_event 消费）
        try:
            state._history_archive_path = str(
                Path(self.state_path).parent / "supervisor.history.archive.jsonl"
            )
        except (AttributeError, TypeError):
            pass
        return state

    def recover(self, reset_worker_attempts: bool = False) -> RunState:
        """Explicit, auditable recovery from the primary or its backup."""
        return recovery_sync.recover(
            self, reset_worker_attempts=reset_worker_attempts,
        )  # type: ignore[return-value]

    def fix_finding_severity(
        self,
        *,
        finding: str,
        severity: str,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, object]:
        """DOC-230 §3.8 第 4 项：历史 severity 脏数据的修复通道（host / CLI）。

        与 ``recover`` 同构——一次锁内、可审计的状态变更。边界：**只改 severity**，
        绝不触碰 ``confirmation``（修复脏数据 ≠ 改裁决结论）；值必须命中写侧
        词表；**合法值之间改级必须给 `reason`**（severity 参与 Life 奖励与
        minimum_severity 评分，静默改级会变成刷分/藏缺陷工具，见 C1/A12）；
        真实变更写 ``finding_severity_repaired`` 事件；幂等调用不推进 revision。
        """
        return finding_sync.repair_finding_severity(
            self,
            finding=finding,
            severity=severity,
            reason=reason,
            expected_revision=expected_revision,
        )

    def _persist_locked(self, state: RunState) -> None:
        if not state.dirty:
            return
        snapshots: list[tuple[Path, bool, bytes | None]] = []
        for path in (
            self.state_path, self.backup_path, self._state_store.revision_path,
        ):
            if path.is_symlink():
                raise WorkspacePathError(f"拒绝回滚符号链接状态文件: {path}")
            exists = path.exists()
            if exists and not path.is_file():
                raise PersistenceError(f"状态文件不是普通文件: {path}")
            snapshots.append((path, exists, path.read_bytes() if exists else None))

        state.revision += 1
        pending = getattr(state, "_finding_commit_pending", None)
        if isinstance(pending, Mapping):
            from core.supervisor.finding_commit import commit_finding_transaction
            state_bytes = json.dumps(
                state.to_dict(), ensure_ascii=False, separators=(",", ":"),
                sort_keys=True, allow_nan=False,
            ).encode("utf-8") + b"\n"
            commit_root = self.state_path.parent.parent
            if commit_root.resolve() == self.workspace_root.resolve():
                commit_root = self.state_path.parent
            commit_finding_transaction(
                commit_root,
                state_path=self.state_path,
                backup_path=self.backup_path,
                report_path=Path(str(pending["report_path"])),
                seal_path=Path(str(pending["seal_path"])),
                state_bytes=state_bytes,
                report_bytes=bytes(pending["report_bytes"]),
                seal_bytes=bytes(pending["seal_bytes"]),
                request_id=str(pending.get("request_id") or uuid.uuid4()),
                expected_revision=state.revision,
            )
            atomic_write_bytes(
                self._state_store.revision_path,
                f"{state.revision}\n".encode("ascii"),
            )
            try:
                delattr(state, "_finding_commit_pending")
            except AttributeError:
                pass
            state.dirty = False
            return
        try:
            self._state_store.persist(state)
        except BaseException as persist_error:
            state.revision -= 1
            state.dirty = True
            try:
                for path, existed, payload in snapshots:
                    if existed:
                        if path.is_symlink():
                            raise WorkspacePathError(
                                f"拒绝回滚符号链接状态文件: {path}"
                            )
                        atomic_write_bytes(path, payload or b"")
                    elif path.exists() or path.is_symlink():
                        if path.is_symlink() or not path.is_file():
                            raise WorkspacePathError(
                                f"拒绝删除异常状态文件: {path}"
                            )
                        path.unlink()
            except BaseException as rollback_error:
                raise PersistenceError(
                    "run-state 持久化失败且回滚失败，拒绝继续"
                ) from rollback_error
            raise persist_error
        state.dirty = False

    def _mutate(
        self,
        operation: Callable[[RunState, float], object],
        *,
        before_persist: Callable[[], Callable[[], None] | None] | None = None,
    ) -> object:
        """执行一次锁内状态变更；可选 ``before_persist`` 建立可恢复边界。

        传入 ``before_persist``（checkpoint 证据发布路径）时，``operation`` 与
        ``before_persist`` 先准备证据，再持久化 run-state；状态持久化失败时调用
        证据回滚钩子。它是可恢复边界，不伪称跨文件原子提交；回滚失败则 fail-closed。
        不传时保持既有语义 —— operation 抛出的拒绝/预算/租约事件仍会落盘（失败留痕）。
        """
        atomic_pair = before_persist is not None
        with self._file_lock():
            state = self._read_state_locked()
            now = float(self._clock())
            rollback: Callable[[], None] | None = None
            try:
                result = operation(state, now)
                if before_persist is not None:
                    rollback = before_persist()
            except BaseException:
                if not atomic_pair:
                    self._persist_locked(state)
                raise
            try:
                self._persist_locked(state)
            except BaseException as persist_error:
                if rollback is not None:
                    try:
                        rollback()
                    except BaseException as rollback_error:
                        raise PersistenceError(
                            "checkpoint 事务回滚失败，拒绝继续"
                        ) from rollback_error
                raise persist_error
            if isinstance(result, (RunState, WorkerClaim)):
                return copy.deepcopy(result)
            return result

    def _query_readonly(self, operation: Callable[[RunState, float], object]) -> object:
        """查询入口：refresh 可能变更状态，因此读改写全程持有排他锁。

        Clean queries still avoid disk writes and revision changes. Keep the
        lock across the single operation pass so refresh side effects aren't
        replayed against a newly read state.
        """
        with self._file_lock():
            state = self._read_state_locked()
            now = float(self._clock())
            result = operation(state, now)
            if not state.dirty:
                if isinstance(result, (RunState, WorkerClaim)):
                    return copy.deepcopy(result)
                return result
            # refresh can update counters/events or terminate a worker; don't
            # release the lock and replay those effects on a second snapshot.
            self._persist_locked(state)
            if isinstance(result, (RunState, WorkerClaim)):
                return copy.deepcopy(result)
            return result

    _release_active_claims = staticmethod(release_active_claims)

    def _new_mutate(self, operation: Callable[[float], RunState]) -> RunState:
        with self._file_lock():
            # Fail closed on any existing persisted artifact; explicit recover
            # is required before a new run can replace historical evidence.
            self._state_store.ensure_new_run_allowed()
            now = float(self._clock())
            state = operation(now)
            self._persist_locked(state)
            return copy.deepcopy(state)

    _append_event = staticmethod(append_event)

    @staticmethod
    def _check_revision(state: RunState, expected_revision: int | None) -> None:
        if expected_revision is not None and expected_revision != state.revision:
            raise RevisionConflict(
                f"revision mismatch: expected={expected_revision}, actual={state.revision}"
            )

    def _active_claim(
        self, state: RunState, worker_id: str, now: float, *,
        require_running: bool = False,
    ) -> WorkerClaim:
        """实例包装：把可注入的单调时钟传给 ``active_claim``。

        过期判定只读单调时钟，因此这里的 ``now_mono`` 必须与 ``_refresh``
        同源；否则注入假时钟的用例会在真实的 ``time.monotonic()`` 下永不过期。
        """
        return active_claim(
            state, worker_id, now,
            require_running=require_running,
            now_mono=float(self._mono_clock()),
        )
    _apply_heartbeat_renewal = staticmethod(renew_claim)
    _normalize_worker_id = staticmethod(normalize_worker_id)
    _normalize_checkpoint_id = staticmethod(normalize_checkpoint_id)
    _normalize_ids = staticmethod(normalize_ids)
    _claimed_units_this_round = staticmethod(claimed_units_this_round)
    _eligible_units = staticmethod(eligible_units)
    _active_resource_keys = staticmethod(active_resource_keys)
    _reconcile_parallel = staticmethod(reconcile_parallel)
    _parallel_required = staticmethod(parallel_required)
    _parallel_gate_result = staticmethod(parallel_gate_result)
    _close_parallel_round = staticmethod(close_parallel_round)
    _recalibrate_round_budget = staticmethod(recalibrate_round_budget)
    _deadline_reasons = staticmethod(deadline_reasons)
    _soft_limit_reasons = staticmethod(soft_limit_reasons)
    _validate_parallel_capacity = staticmethod(validate_parallel_capacity)
    _set_budget_exhausted = staticmethod(set_budget_exhausted)

    def _refresh(self, state: RunState, now: float) -> None:
        refresh(
            state, now,
            workspace_root=self.workspace_root,
            now_mono=float(self._mono_clock()),
        )

    def _crash_drain(self, state: RunState, now: float) -> list[str]:
        """Reclaim only leases proven dead or already past their deadline."""
        return reclaim_orphan_leases(state, now)

    def _refresh_status_budget(self, state: RunState, now: float) -> None:
        """Refresh live leases, then drain crash orphans before gate evaluation."""
        self._refresh(state, now)
        # Refresh runs first so a fresh live heartbeat can renew its lease.  The
        # drain then only releases claims proven dead or already lease-expired;
        # submitted claims are not active and cannot be mistaken for crashes.
        self._crash_drain(state, now)
        apply_status_budget(state, now)
    _ensure_live_for_work = staticmethod(ensure_live_for_work)
    _finding_key = staticmethod(finding_key)
    _normalize_findings = staticmethod(normalize_findings)
    _enrich_gate_finding = staticmethod(enrich_gate_finding)
    _normalize_notes = staticmethod(normalize_notes)
    _normalize_signatures = staticmethod(normalize_signatures)
    _confirmation_gate = staticmethod(confirmation_gate)
    _refresh_confirmation_gate = staticmethod(refresh_confirmation_gate)
    _normalize_coverage = staticmethod(normalize_coverage)
    _normalize_evidence = staticmethod(normalize_evidence)

    def _target_gate_result(self, state: RunState) -> dict[str, object]:
        return target_gate_result(self.workspace_root, state)

    def start(
        self,
        *,
        life_score: float = 1.0,
        coverage_units: Sequence[str] = (),
        run_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        round_id: str | None = None,
        life_id: str | None = None,
        plan_hash: str | None = None,
        life_kind: str = "mine",
    ) -> RunState:
        """创建并持久化一个 run；不会覆盖已有 state。"""

        if plan_hash is not None and (
            not isinstance(plan_hash, str) or not SHA256_RE.fullmatch(plan_hash)
        ):
            raise InvalidCheckpoint(
            "plan_hash 必须是 64 位十六进制 SHA-256，"
            "形如 " + "a" * 8 + "..." + "（共 64 字符，用 "
            "`shasum -a 256 <plan-file>` 生成）；不要传 None 或短哈希"
        )
        if life_kind not in {"mine", "fix"}:
            raise InvalidCheckpoint("life_kind 必须是 mine 或 fix")
        score = finite_number(life_score, "life_score")
        raw_units = list(coverage_units)
        if any(not isinstance(unit, str) or not unit.strip() for unit in raw_units):
            raise InvalidCheckpoint(
            "coverage_units 必须是非空且唯一的字符串，"
            '形如 ["1-内部-ROOT", "2-外部-API"]；'
            "空数组会被视为未覆盖任何单元，重复项会导致统计失真"
        )
        units = [unit.strip() for unit in raw_units]
        if len(set(units)) != len(units):
            raise InvalidCheckpoint("coverage_units 不能包含仅空白差异的重复单元")
        initial_eligible = [unit for unit in units if not unit_kind.is_business(unit)]
        if not initial_eligible:
            initial_eligible = units
        self._validate_parallel_capacity(
            self._configured_budget, len(initial_eligible),
        )

        invalidate_git_head_cache(self.workspace_root)

        def create(now: float) -> RunState:
            return start_sync.create_run(
                self,
                now,
                score=score,
                units=units,
                run_id=run_id,
                project_id=project_id,
                session_id=session_id,
                round_id=round_id,
                life_id=life_id,
                plan_hash=plan_hash,
                life_kind=life_kind,
                workspace_fingerprint=workspace_fingerprint,
            )

        return self._new_mutate(create)

    def start_graph(
        self,
        *,
        graph_id: str,
        goal: str,
        origin_root: str,
        snapshot_digest: str,
        tasks: Sequence[GraphTask | Mapping[str, object]],
        expected_revision: int | None = None,
    ) -> GraphRun:
        """Attach a versioned agent graph to this run in the state transaction."""

        return self._mutate(
            lambda state, now: graph_sync.graph_start_sync(
                self, state, now,
                graph_id=graph_id,
                goal=goal,
                origin_root=origin_root,
                snapshot_digest=snapshot_digest,
                tasks=tasks,
                expected_revision=expected_revision,
            )
        )  # type: ignore[return-value]

    def _coverage_evidence_path(self) -> Path | None:
        return coverage_evidence_path(self.workspace_root, self.state_path)

    def _assert_target_fingerprint(self, state: RunState, now: float) -> None:
        assert_target_fingerprint(self.workspace_root, self.target_fingerprint, state, now)

    def _publish_checkpoint_evidence(
        self, evidence: Mapping[str, Mapping[str, object]],
    ) -> Callable[[], None] | None:
        return publish_checkpoint_evidence(
            self.workspace_root, self.state_path, evidence,
        )

    # DES-03：以下方法是 claim_worker 的状态变更体，由原先的嵌套闭包
    # 提升而来；claim_worker 只保留归一化 + 适配调用。
    # ---- Worker Lifecycle (core/supervisor/worker_lifecycle.py) ----
    _apply_claim_operation = worker_lifecycle._apply_claim_operation
    claim_worker = worker_lifecycle.claim_worker
    start_worker = worker_lifecycle.start_worker
    record_worker_failure = worker_lifecycle.record_worker_failure
    heartbeat = worker_lifecycle.heartbeat
    begin_long_call = worker_lifecycle.begin_long_call
    record_tool_call = worker_lifecycle.record_tool_call

    # ---- Checkpoint Pipeline (core/supervisor/checkpoint_handler.py) ----
    _apply_checkpoint_operation = checkpoint_handler._apply_checkpoint_operation
    _record_checkpoint_rejection = checkpoint_handler._record_checkpoint_rejection
    _verify_readonly_claim = checkpoint_handler._verify_readonly_claim
    submit_checkpoint = checkpoint_handler.submit_checkpoint
    _commit_checkpoint_transaction = checkpoint_handler._commit_checkpoint_transaction
    _commit_expired_claims = checkpoint_handler._commit_expired_claims
    submit_graph_checkpoint = checkpoint_handler.submit_graph_checkpoint

    # ---- Round Governor (core/supervisor/round_governor.py) ----
    recalibrate_round_budget = round_governor.recalibrate_round_budget
    _apply_next_round_operation = round_governor._apply_next_round_operation
    next_round = round_governor.next_round
    bind_context = round_governor.bind_context
    _check_session_coverage_ledger = round_governor._check_session_coverage_ledger
    finish = round_governor.finish
    parallel_gate = round_governor.parallel_gate
    _apply_reset_budget_operation = round_governor._apply_reset_budget_operation
    reset_budget = round_governor.reset_budget
    rebind_target = finding_workflow.rebind_target

    # ---- Lifecycle Blocks (core/supervisor/lifecycle_blocks.py) ----
    _count_lifecycle_blocks = staticmethod(lifecycle_blocks._count_lifecycle_blocks)
    _lifecycle_block_fields = staticmethod(lifecycle_blocks._lifecycle_block_fields)
    _release_owner_cancelled_debris = staticmethod(lifecycle_blocks._release_owner_cancelled_debris)
    record_lifecycle_block = lifecycle_blocks.record_lifecycle_block

    # ---- Finding Workflow (core/supervisor/finding_workflow.py) ----
    reopen_finding = finding_workflow.reopen_finding
    admit_business_unit = finding_workflow.admit_business_unit
    _reinstate_covered_by_evidence = finding_workflow._reinstate_covered_by_evidence
    _rescan_changed_scopes = finding_workflow._rescan_changed_scopes
    _units_in_scopes = finding_workflow._units_in_scopes
    _restore_not_applicable = finding_workflow._restore_not_applicable
    _retarget_evidence = finding_workflow._retarget_evidence
    sync_finding_decisions = finding_workflow.sync_finding_decisions
    sync_finding_decisions_audited = finding_workflow.sync_finding_decisions_audited
    freeze_report_snapshot = finding_workflow.freeze_report_snapshot
    settle_report = finding_workflow.settle_report
    _verification_recipe_digest = finding_workflow._verification_recipe_digest
    _verification_precheck = finding_workflow._verification_precheck
    _record_verification = finding_workflow._record_verification
    confirm_finding = finding_workflow.confirm_finding
    invalidate_coverage = finding_workflow.invalidate_coverage
    verify_finding = finding_workflow.verify_finding

    def fail(self, reason: str, *, expected_revision: int | None = None) -> RunState:
        """将 run 置为 FAILED；仍保留所有审计数据。"""

        if not isinstance(reason, str) or not reason.strip():
            raise InvalidCheckpoint("FAILED 必须提供 reason")
        normalized_reason = reason.strip()

        def operation(state: RunState, now: float) -> RunState:
            # A caller may retry after the FAILED response was lost.  The
            # terminal result is already durable, so the same reason is an
            # acknowledgement and must remain idempotent even with the
            # revision observed before the first request.
            if state.status == Status.FAILED and state.last_error == normalized_reason:
                return state
            self._check_revision(state, expected_revision)
            if state.status == Status.COMPLETE:
                raise InvalidTransition("COMPLETE run 不能改为 FAILED")
            if state.status == Status.INCOMPLETE:
                # D2：INCOMPLETE 是 finish 已收尾的终态（completed_at 已写、
                # run_finished 已发），与 COMPLETE 同级，不得被 fail 改写。
                raise InvalidTransition("INCOMPLETE run 不能改为 FAILED")
            if state.status == Status.FAILED:
                raise InvalidTransition("FAILED run 已经记录了不同的 reason")
            self._refresh(state, now)
            state.last_error = normalized_reason
            state.status = Status.FAILED
            # FAILED is terminal just like COMPLETE.  Persist the terminal
            # timestamp before the event is written so a subsequent process
            # can reload/status the run without violating RunState invariants.
            state.completed_at = now
            self._release_active_claims(state, now, reason="run_failed")
            self._append_event(
                state, "run_failed", now, reason=state.last_error,
            )
            return state

        return self._mutate(operation)  # type: ignore[return-value]

    def status(self) -> RunState:
        """重新从磁盘读取状态，并标记已过期 lease/硬时间预算。"""

        def operation(state: RunState, now: float) -> RunState:
            self._refresh_status_budget(state, now)
            self._refresh_confirmation_gate(state)
            return state

        return self._query_readonly(operation)  # type: ignore[return-value]

    def status_with_gate(self) -> dict[str, object]:
        """在同一个文件锁快照中返回状态与并发门禁。

        MCP 宿主通常需要同时读取状态和 gate。分两次调用会让 worker 在两次
        读取之间完成 checkpoint，导致 UI 展示一组互不对应的快照；该方法把
        回收、预算刷新和 gate 计算放进同一个原子读取窗口。
        """

        def operation(state: RunState, now: float) -> dict[str, object]:
            self._refresh_status_budget(state, now)
            self._refresh_confirmation_gate(state)
            gate = build_parallel_gate_snapshot(
                state,
                self._parallel_gate_result(state, now=now),
                self._target_gate_result(state),
            )
            return {"state": state.to_dict(), "parallel_gate": gate}

        return self._query_readonly(operation)  # type: ignore[return-value]

    def decision_snapshot(self) -> dict[str, object]:
        """决策专用只读快照 (DOC-154)：在共享读锁内读取纯内存决策字段。

        不执行任何写操作，不升级写事务，不更新 revision，返回用于收敛与停机判定的不可变数据字典。
        """
        with self._read_lock():
            state = self._read_state_locked()
            now = float(self._clock())
            return build_decision_snapshot(state, now)


__all__ = [
    "BudgetExhausted",
    "AgentBinding",
    "BugRecord",
    "COVERAGE_BLOCKED",
    "COVERAGE_COVERED",
    "COVERAGE_PENDING",
    "ClaimRejected",
    "ConcurrencyNotSatisfied",
    "InvalidCheckpoint",
    "InvalidTransition",
    "LeaseExpired",
    "NoRunError",
    "PersistenceError",
    "RevisionConflict",
    "RunAlreadyStarted",
    "RunBudget",
    "GraphRun",
    "GraphTask",
    "RunState",
    "Status",
    "Supervisor",
    "SupervisorError",
    "WorkerClaim",
    "WorkspacePathError",
    "resolve_system_concurrency",
]


def _cli_budget(args: argparse.Namespace) -> RunBudget:
    from core.supervisor.cli import cli_budget as impl
    return impl(args)


def _cli_supervisor(args: argparse.Namespace) -> Supervisor:
    from core.supervisor.cli import cli_supervisor as impl
    return impl(args)


_manifest_units = load_manifest_units
_cli_payload = load_checkpoint_payload


def _cli_print(value: RunState | WorkerClaim | Mapping[str, object]) -> None:
    from core.supervisor.cli import cli_print as impl
    return impl(value)


def _incident_hints(supervisor: "Supervisor", ledger: Path, limit: int) -> dict[str, object]:
    from core.supervisor.cli import incident_hints as impl
    return impl(supervisor, ledger, limit)


def main(argv: Sequence[str] | None = None) -> int:
    """兼容入口；CLI 解析和业务分发位于 ``supervisor.cli``。"""
    from core.supervisor import cli as cli_module
    return cli_module.main(argv, api=sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
