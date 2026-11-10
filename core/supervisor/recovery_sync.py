#!/usr/bin/env python3
"""Supervisor 恢复流程的主备选择与状态收口边界。

该模块只编排恢复时序；文件锁、状态存储、事件和持久化仍由显式的
``audit_supervisor.Supervisor`` 窄边界提供。
"""

from __future__ import annotations

import copy
from typing import Any

try:
    from core.supervisor.state_store import reanchor_fence
    from .supervisor_models import (
        NoRunError,
        PersistenceError,
        Status,
        WorkspacePathError,
        has_lexical_symlink,
    )
except ImportError:  # pragma: no cover
    from core.supervisor.state_store import reanchor_fence
    from .supervisor_models import (
        NoRunError,
        PersistenceError,
        Status,
        WorkspacePathError,
        has_lexical_symlink,
    )


def recover(supervisor: Any, reset_worker_attempts: bool = False) -> Any:
    """Recover one Supervisor run from the primary snapshot or its backup."""

    def reset_attempt_history(recovered: Any) -> None:
        if not reset_worker_attempts:
            return
        reset_at = supervisor._clock()
        for claim in recovered.claims.values():
            if getattr(claim, "state", "") in {"expired", "released", "submitted"}:
                claim.fenced_from_attempts = True
        supervisor._append_event(
            recovered,
            "worker_attempts_reset",
            reset_at,
            reset_at=reset_at,
        )

    with supervisor._file_lock():
        if (
            supervisor.state_path.is_symlink()
            or supervisor.backup_path.is_symlink()
            or has_lexical_symlink(
                supervisor._lexical_root, supervisor._lexical_state_path,
            )
            or has_lexical_symlink(
                supervisor._lexical_root, supervisor._lexical_backup_path,
            )
        ):
            raise WorkspacePathError(
                f"拒绝恢复符号链接: {supervisor.state_path}"
            )
        primary_error: BaseException | None = None
        recovered: Any | None = None
        primary_valid = False
        # D1：带内自愈的唯一入口。栅栏失配（RECOVERY_REQUIRED）时主备未必损坏
        # ——最常见的是「主备已写、栅栏未写」的崩溃窗口。此时主备互为佐证，
        # 允许在写锁内重锚后重读；其余损坏（JSON 撕裂/快照缺失）一律不重锚。
        reanchored_revision: int | None = None
        try:
            recovered = supervisor._state_store.read_primary()
            primary_valid = True
        except (NoRunError, PersistenceError) as exc:
            primary_error = exc
            if isinstance(exc, PersistenceError) and str(exc).startswith("RECOVERY_REQUIRED"):
                try:
                    reanchored_revision = reanchor_fence(
                        supervisor.state_path, supervisor.backup_path,
                    )
                except (PersistenceError, WorkspacePathError, OSError):
                    reanchored_revision = None
                if reanchored_revision is not None:
                    try:
                        recovered = supervisor._state_store.read_primary()
                        primary_valid = True
                        primary_error = None
                    except (NoRunError, PersistenceError) as reread_error:
                        primary_error = reread_error
                        reanchored_revision = None

        if recovered is not None and primary_valid:
            recovered.dirty = True
            if reanchored_revision is not None:
                supervisor._append_event(
                    recovered,
                    "revision_fence_reanchored",
                    supervisor._clock(),
                    source="primary",
                    revision=reanchored_revision,
                )
            supervisor._release_active_claims(
                recovered, supervisor._clock(), reason="state_recovered",
            )
            reset_attempt_history(recovered)
            recovered.scheduler_epoch += 1
            if recovered.status in {Status.STALLED, Status.CHECKPOINT, Status.NEXT_ROUND}:
                recovered.status = Status.RUNNING
            supervisor._append_event(
                recovered,
                "state_recovered",
                supervisor._clock(),
                source="primary",
                recovered_from="primary",
                scheduler_epoch=recovered.scheduler_epoch,
            )
            supervisor._persist_locked(recovered)
            return copy.deepcopy(recovered)

        if not supervisor.backup_path.exists():
            if isinstance(primary_error, NoRunError):
                raise NoRunError(
                    f"run state 不存在: {supervisor.state_path}"
                )
            if isinstance(primary_error, PersistenceError):
                raise primary_error
            raise PersistenceError(
                "state 与 backup 均不可恢复: "
                f"state={primary_error}; backup=不存在"
            ) from primary_error

        if not supervisor.backup_path.is_file():
            raise WorkspacePathError(
                f"backup 不是普通文件: {supervisor.backup_path}"
            )

        try:
            recovered = supervisor._state_store.read_backup()
        except (NoRunError, PersistenceError, WorkspacePathError) as backup_error:
            raise PersistenceError(
                "state 与 backup 均不可恢复: "
                f"state={primary_error}; backup={backup_error}"
            ) from backup_error

        recovered.dirty = True
        supervisor._release_active_claims(
            recovered, supervisor._clock(), reason="state_recovered",
        )
        reset_attempt_history(recovered)
        recovered.scheduler_epoch += 1
        if recovered.status in {Status.STALLED, Status.CHECKPOINT, Status.NEXT_ROUND}:
            recovered.status = Status.RUNNING
        supervisor._append_event(
            recovered,
            "state_recovered",
            supervisor._clock(),
            source="backup",
            recovered_from="backup",
            scheduler_epoch=recovered.scheduler_epoch,
        )
        supervisor._persist_locked(recovered)
        return copy.deepcopy(recovered)


__all__ = ["recover"]
