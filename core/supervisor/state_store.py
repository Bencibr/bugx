#!/usr/bin/env python3
"""Supervisor 持久化快照 I/O 边界。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .supervisor_models import (
    NoRunError,
    PersistenceError,
    RunAlreadyStarted,
    RunState,
    WorkspacePathError,
    atomic_write_bytes,
    has_lexical_symlink,
)


def reanchor_fence(state_path: Path, backup_path: Path) -> int:
    """按 primary/backup **一致**的 revision 重建持久化栅栏（BX-ENGINE-22）。

    背景：revision 栅栏由 DOC-229 引入，而**同时存活的老引擎写入者**（例如升级前
    启动、仍在运行的 host 会话）会继续推进 ``supervisor.json`` 而不更新栅栏，造成
    「状态 revision 1904 vs 栅栏 1899」。此时新引擎读取即 fail-closed，且
    ``recover`` 同样需要先读 → **没有任何修复路径**，项目永久不可读，唯一出路是
    「重置项目运行时」＝丢数据。

    本函数提供显式、可审计的带外修复：只有当 primary 与 backup 的 revision
    **完全一致**时才重建栅栏（互为佐证，排除撕裂写）；不一致则拒绝并要求人工
    介入。返回重建后的 revision。

    D1：除 revision 一致外还要求两份快照**字节内容一致**。同一 revision 却内容
    分叉，说明写入撕裂或人工拷贝污染；此时重锚会把分叉固化成权威栅栏，读侧
    再也无法发现它。正常写入路径（``persist`` 与 finding 2PC）都用同一份
    payload 写主备，因此字节一致是既有不变量，不会误杀。
    """
    def _snapshot(path: Path) -> tuple[int, bytes]:
        if not path.is_file() or path.is_symlink():
            raise PersistenceError(f"快照不可用: {path}")
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PersistenceError(f"快照无法解析: {path}: {exc}") from exc
        revision = payload.get("revision") if isinstance(payload, dict) else None
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise PersistenceError(f"快照缺少合法 revision: {path}")
        return revision, raw

    primary_revision, primary_bytes = _snapshot(state_path)
    backup_revision, backup_bytes = _snapshot(backup_path)
    if primary_revision != backup_revision:
        raise PersistenceError(
            "primary/backup revision 不一致，拒绝重建栅栏（需人工核对备份）: "
            f"primary={primary_revision} backup={backup_revision}"
        )
    if hashlib.sha256(primary_bytes).digest() != hashlib.sha256(backup_bytes).digest():
        raise PersistenceError(
            "primary/backup 内容不一致（同一 revision 分叉），"
            "拒绝重建栅栏（需人工核对备份）: "
            f"revision={primary_revision}"
        )
    fence_path = state_path.with_name(state_path.name + ".revision")
    if fence_path.is_symlink():
        raise WorkspacePathError(f"拒绝写入符号链接: {fence_path}")
    atomic_write_bytes(fence_path, f"{primary_revision}\n".encode("ascii"))
    return primary_revision


@dataclass(frozen=True)
class StateStore:
    """受信任 state/backup 快照的读写器；不持有锁，也不改变状态机。"""

    state_path: Path
    backup_path: Path
    lexical_root: Path
    lexical_state_path: Path
    lexical_backup_path: Path

    @property
    def revision_path(self) -> Path:
        return self.state_path.with_name(self.state_path.name + ".revision")

    def _guard_paths(self) -> None:
        if (
            self.state_path.is_symlink()
            or self.backup_path.is_symlink()
            or self.revision_path.is_symlink()
            or has_lexical_symlink(self.lexical_root, self.lexical_state_path)
            or has_lexical_symlink(self.lexical_root, self.lexical_backup_path)
            or has_lexical_symlink(
                self.lexical_root,
                self.lexical_state_path.with_name(self.lexical_state_path.name + ".revision"),
            )
        ):
            raise WorkspacePathError(f"拒绝读取符号链接: {self.state_path}")

    def _revision_fence(
        self, state: RunState, source: str, *, migrate_legacy: bool = False,
    ) -> None:
        """校验持久化 revision 栅栏。

        BX-ENGINE-20：栅栏由 DOC-229（build 98）引入，此前写入的快照**没有**
        栅栏文件。若把「缺失」一律视为损坏，则升级引擎后所有既有项目的 run
        立即不可读，唯一出路是「显式重置项目运行时」＝丢弃整个 run。缺失栅栏
        本身不携带任何信息，因此：

        * 缺失 + ``migrate_legacy=True`` → 按快照自身 revision 一次性回填（恢复不变式）；
        * 缺失 + ``migrate_legacy=False`` → 兼容放行（不写盘），用于备份恢复路径：
          恢复场景下不得因旧版缺栅栏而拒绝可用备份；
        * **栅栏存在但与快照不符/格式非法 → 仍然 fail-closed**，对分叉与撕裂写的
          检测强度不变。
        """
        if not self.revision_path.exists():
            if migrate_legacy:
                atomic_write_bytes(
                    self.revision_path, f"{state.revision}\n".encode("ascii"),
                )
            return
        if not self.revision_path.is_file():
            raise PersistenceError("RECOVERY_REQUIRED: state revision fence is not a regular file")
        try:
            raw = self.revision_path.read_text(encoding="ascii")
            if not raw.endswith("\n") or not raw[:-1].isdigit():
                raise ValueError("invalid revision fence format")
            expected = int(raw[:-1])
        except (OSError, UnicodeError, ValueError) as exc:
            raise PersistenceError(f"RECOVERY_REQUIRED: state revision fence is invalid: {exc}") from exc
        if state.revision != expected:
            raise PersistenceError(
                f"RECOVERY_REQUIRED: {source} revision {state.revision} does not match "
                f"durable revision fence {expected}"
            )

    def read_primary(self) -> RunState:
        """读取主快照；恢复备份由 Supervisor 显式控制。"""
        self._guard_paths()
        if not self.state_path.exists():
            raise NoRunError(f"run state 不存在: {self.state_path}")
        if not self.state_path.is_file():
            raise PersistenceError(f"state 不是普通文件: {self.state_path}")
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = RunState.from_dict(payload)
            self._revision_fence(state, "primary", migrate_legacy=True)
            return state
        except (OSError, json.JSONDecodeError, PersistenceError) as exc:
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"state 无法读取: {exc}") from exc

    def read_backup(self) -> RunState:
        """读取 backup 候选；是否采用由 Supervisor 显式决定。"""
        self._guard_paths()
        if not self.backup_path.exists():
            raise NoRunError(f"backup 不存在: {self.backup_path}")
        if not self.backup_path.is_file():
            raise WorkspacePathError(f"backup 不是普通文件: {self.backup_path}")
        try:
            payload = json.loads(self.backup_path.read_text(encoding="utf-8"))
            state = RunState.from_dict(payload)
            # 恢复路径：旧版快照缺栅栏时放行且不写盘（写盘会把 backup 的 revision
            # 固化为权威栅栏，可能掩盖 primary 的分叉）。
            self._revision_fence(state, "backup", migrate_legacy=False)
            return state
        except (OSError, json.JSONDecodeError, PersistenceError) as exc:
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"backup 无法读取: {exc}") from exc

    def read(self) -> RunState:
        """兼容入口：读取主快照。"""
        return self.read_primary()

    def ensure_new_run_allowed(self) -> None:
        """拒绝覆盖现有或可恢复快照；新 run 必须显式清理/恢复。"""
        self._guard_paths()
        if not (
            self.state_path.exists() or self.backup_path.exists()
            or self.revision_path.exists() or self.revision_path.is_symlink()
            or self.lexical_state_path.exists() or self.lexical_backup_path.exists()
        ):
            return
        if self.revision_path.exists() and not (
            self.state_path.exists() or self.backup_path.exists()
        ):
            raise PersistenceError(
                "RECOVERY_REQUIRED: revision fence exists without a state snapshot"
            )
        if self.state_path.exists() and not self.state_path.is_file():
            raise PersistenceError(f"state 不是普通文件: {self.state_path}")
        if self.backup_path.exists() and not self.backup_path.is_file():
            raise PersistenceError(f"backup 不是普通文件: {self.backup_path}")
        if self.state_path.exists():
            try:
                self.read_primary()
                raise RunAlreadyStarted(
                    f"run state 已存在，拒绝覆盖: {self.state_path}"
                )
            except RunAlreadyStarted:
                raise
            except (NoRunError, PersistenceError) as primary_exc:
                if self.backup_path.exists():
                    try:
                        self.read_backup()
                    except (NoRunError, PersistenceError, WorkspacePathError) as backup_exc2:
                        raise PersistenceError(
                            f"state 与 backup 均损坏，需显式 recover 或清理后重试: state={primary_exc}; backup={backup_exc2}"
                        ) from backup_exc2
                    raise PersistenceError(
                        f"现有 state 已损坏但 backup 可恢复，需显式 recover 后才能创建新 run: {primary_exc}"
                    ) from primary_exc
                raise PersistenceError(
                    f"现有 state 已损坏，需显式 recover 后才能创建新 run: {primary_exc}"
                ) from primary_exc
        if self.backup_path.exists():
            try:
                self.read_backup()
            except (NoRunError, PersistenceError, WorkspacePathError) as backup_exc:
                raise PersistenceError(
                    f"backup 已损坏，需显式 recover 或清理后重试: {backup_exc}"
                ) from backup_exc
            raise PersistenceError(
                f"run state 缺失但 backup 存在，需显式 recover: {self.state_path}"
            )

    def persist(self, state: RunState) -> None:
        """按 backup → primary → revision 栅栏（提交标记）顺序写入紧凑快照。

        D1：旧实现先写栅栏。进程若在「栅栏已写、主文件未写」窗口崩溃，主备与
        栅栏三者互相失配：``read_primary`` 与 ``read_backup`` 双路皆 fail-closed，
        连 ``recover`` 都读不动，只能靠带外 ``repair-fence`` 人工介入（自动模式
        无法收敛终态）。把栅栏改为**最后写**的提交标记后，崩溃窗口只剩两类：

        1. 备份已写、主文件/栅栏未写 → primary 与栅栏一致，**照常可读**；
        2. 主备已写、栅栏未写 → 读侧 fail-closed，但主备互为佐证，
           ``recover`` 可在写锁内自动重锚前滚（见 ``recovery_sync.recover``）。
        """
        self._guard_paths()
        payload = json.dumps(
            state.to_dict(), ensure_ascii=False, separators=(",", ":"),
            sort_keys=True, allow_nan=False,
        ).encode("utf-8") + b"\n"
        atomic_write_bytes(self.backup_path, payload)
        atomic_write_bytes(self.state_path, payload)
        atomic_write_bytes(self.revision_path, f"{state.revision}\n".encode("ascii"))
