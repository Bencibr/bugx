#!/usr/bin/env python3
"""target_sandbox_manager.py — 被测目标运行时沙盒环境自愈与生命周期管理器 (DOC-137 2.0 / D137-05)。

用途：
  1. 显式隔离目标目录快照与失败恢复 (Snapshot & Rollback)：
     调用方必须提供独立、单写者目标目录；当前 Worker/Life 主流程未接入，禁止对共享项目根目录调用。
  2. 服务健康探测与受管进程恢复 (Health Probe & Managed Service Lifecycle)：
     针对长时间运行的被测 Web/API 目标服务，提供毫秒级 TCP/HTTP 探活。
     仅可通过同一管理器持有的 Popen 句柄恢复其启动的进程；端口占用本身不授权终止。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence


DEFAULT_IGNORES = frozenset({
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".bugx",
    ".DS_Store",
})


@dataclass
class SnapshotInfo:
    """沙盒快照元数据。"""
    snapshot_id: str
    target_dir: str
    snapshot_dir: str
    created_at: float
    file_count: int
    ignores: list[str] = field(default_factory=list)


class SnapshotRollbackError(RuntimeError):
    """Raised when a transaction cannot restore its baseline snapshot."""

    def __init__(self, snapshot_dir: Path, reason: str) -> None:
        self.snapshot_dir = str(snapshot_dir)
        super().__init__(
            f"快照回滚失败，恢复点已保留在 {snapshot_dir}: {reason}"
        )


@dataclass
class HealthStatus:
    """被测目标健康状况。"""
    is_healthy: bool
    status_code: str  # "HEALTHY", "PORT_CLOSED", "PORT_OCCUPIED_HANG", "PID_DEAD", "TIMEOUT"
    detail: str
    latency_ms: float = 0.0


class TargetSandboxManager:
    """被测目标沙盒环境管理器。"""

    def __init__(
        self,
        base_workdir: Path | str | None = None,
        custom_ignores: Sequence[str] | None = None,
    ) -> None:
        self.workdir = (Path(base_workdir or tempfile.gettempdir()) / "bugx_sandboxes").resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.ignores = set(DEFAULT_IGNORES)
        if custom_ignores:
            self.ignores.update(custom_ignores)
        self._managed_processes: dict[int, subprocess.Popen[bytes]] = {}
        self._snapshot_targets: dict[str, tuple[str, tuple[str, ...]]] = {}

    def _should_ignore(self, path: Path, base_dir: Path) -> bool:
        try:
            rel = path.relative_to(base_dir)
            for part in rel.parts:
                if part in self.ignores:
                    return True
        except ValueError:
            pass
        return False

    def create_snapshot(
        self,
        target_dir: Path | str,
        snapshot_id: str | None = None,
    ) -> SnapshotInfo:
        """建立被测目标的轻量级快照。"""
        src = Path(target_dir).resolve()
        if not src.exists() or not src.is_dir():
            raise ValueError(f"目标目录不存在或非目录: {src}")
        try:
            workdir_relative = self.workdir.relative_to(src)
        except ValueError:
            workdir_relative = None
        if workdir_relative is not None and (
            not workdir_relative.parts or workdir_relative.parts[0] not in self.ignores
        ):
            raise ValueError("快照工作目录不能位于目标目录的非忽略子树")

        if snapshot_id is not None and (
            not isinstance(snapshot_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", snapshot_id)
        ):
            raise ValueError("snapshot_id 只能包含 1 到 128 个字母、数字、下划线或连字符")
        s_id = snapshot_id if snapshot_id is not None else f"snap_{uuid.uuid4().hex}"
        dest = self.workdir / s_id
        try:
            dest.resolve().relative_to(self.workdir.resolve())
        except ValueError as exc:
            raise ValueError("snapshot_id 超出沙盒目录") from exc
        # mkdir(exist_ok=False) is the atomic collision gate: never destroy a
        # previous recovery point when an explicit ID is reused or raced.
        dest.mkdir(parents=True, exist_ok=False)

        file_count = 0
        try:
            for root, dirs, files in os.walk(src, topdown=True, followlinks=False):
                current_path = Path(root)
                if self._should_ignore(current_path, src):
                    dirs.clear()
                    continue
                walk_dirs: list[str] = []
                for dir_name in dirs:
                    if dir_name in self.ignores:
                        continue
                    source_dir = current_path / dir_name
                    relative = source_dir.relative_to(src)
                    target_dir = dest / relative
                    target_dir.parent.mkdir(parents=True, exist_ok=True)
                    if source_dir.is_symlink():
                        target_dir.symlink_to(os.readlink(source_dir), target_is_directory=True)
                        file_count += 1
                        continue
                    target_dir.mkdir(exist_ok=True)
                    shutil.copystat(source_dir, target_dir, follow_symlinks=False)
                    walk_dirs.append(dir_name)
                dirs[:] = walk_dirs
                for file_name in files:
                    if file_name in self.ignores:
                        continue
                    file_path = current_path / file_name
                    rel = file_path.relative_to(src)
                    target_file = dest / rel
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    if file_path.is_symlink():
                        target_file.symlink_to(os.readlink(file_path))
                    else:
                        shutil.copy2(file_path, target_file)
                    file_count += 1
            shutil.copystat(src, dest, follow_symlinks=False)
        except BaseException:
            # This path was created exclusively by this call. Refuse to follow
            # a substituted symlink if another local process races the cleanup.
            if dest.is_dir() and not dest.is_symlink() and dest.parent == self.workdir:
                shutil.rmtree(dest, ignore_errors=True)
            raise

        snapshot = SnapshotInfo(
            snapshot_id=s_id,
            target_dir=str(src),
            snapshot_dir=str(dest),
            created_at=time.time(),
            file_count=file_count,
            ignores=sorted(list(self.ignores)),
        )
        self._snapshot_targets[s_id] = (str(src), tuple(sorted(self.ignores)))
        return snapshot

    def rollback(self, snapshot: SnapshotInfo) -> bool:
        """根据快照将目标目录回滚恢复至基准状态。"""
        target = Path(snapshot.target_dir)
        snap_dir = self._snapshot_dir(snapshot)

        if not snap_dir.is_dir() or snap_dir.is_symlink():
            return False
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError("回滚目标必须是普通目录")

        target.parent.mkdir(parents=True, exist_ok=True)
        self._recover_orphaned_swap(target)
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.bugx-stage-", dir=target.parent))
        backup: Path | None = None
        moved_old = False
        preserve_names = set(snapshot.ignores) if snapshot.ignores else self.ignores
        try:
            shutil.copytree(snap_dir, stage, dirs_exist_ok=True, symlinks=True)

            # Snapshot intentionally omits large ignored trees; retain those
            # entries in the staged replacement before switching directories.
            if target.is_dir():
                for root, dirs, files in os.walk(target, topdown=True, followlinks=False):
                    current = Path(root)
                    relative = current.relative_to(target)
                    for name in list(dirs):
                        source = current / name
                        if name not in preserve_names:
                            continue
                        destination = stage / relative / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if source.is_symlink():
                            destination.symlink_to(os.readlink(source))
                        else:
                            shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
                        dirs.remove(name)
                    for name in files:
                        if name not in preserve_names:
                            continue
                        source = current / name
                        destination = stage / relative / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if source.is_symlink():
                            destination.symlink_to(os.readlink(source))
                        else:
                            shutil.copy2(source, destination)
                shutil.copystat(target, stage, follow_symlinks=False)
            else:
                shutil.copystat(snap_dir, stage, follow_symlinks=False)

            if target.exists():
                backup = Path(tempfile.mkdtemp(prefix=f".{target.name}.bugx-backup-", dir=target.parent))
                backup.rmdir()
                os.replace(target, backup)
                moved_old = True
            try:
                os.replace(stage, target)
            except OSError:
                if moved_old and backup is not None:
                    os.replace(backup, target)
                    moved_old = False
                raise
            if moved_old and backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
                moved_old = False
            return True
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if moved_old and backup is not None:
                if not target.exists():
                    os.replace(backup, target)
                else:
                    shutil.rmtree(backup, ignore_errors=True)

    @staticmethod
    def _recover_orphaned_swap(target: Path) -> None:
        """Recover a swap interrupted after moving the old target aside."""
        prefix_backup = f".{target.name}.bugx-backup-"
        prefix_stage = f".{target.name}.bugx-stage-"
        backups = sorted(target.parent.glob(prefix_backup + "*"))
        stages = sorted(target.parent.glob(prefix_stage + "*"))
        candidates = [*backups, *stages]
        # A live target means another rollback may still own these temporary
        # directories; leave them alone and let that operation finish.
        if target.exists():
            return
        if any(path.is_symlink() or not path.is_dir() for path in candidates):
            raise OSError("检测到不安全的回滚临时目录，拒绝自动恢复")
        if not target.exists() and backups:
            if len(backups) != 1:
                raise OSError("存在多个回滚备份，无法安全选择恢复源")
            os.replace(backups[0], target)
            backups = []
        for path in [*backups, *stages]:
            if path.exists():
                shutil.rmtree(path)

    def cleanup_snapshot(self, snapshot: SnapshotInfo) -> None:
        """销毁快照临时目录。"""
        snap_dir = self._snapshot_dir(snapshot)
        if snap_dir.exists():
            if not snap_dir.is_dir():
                raise ValueError("snapshot 路径必须是管理器创建的目录")
            shutil.rmtree(snap_dir)
        self._snapshot_targets.pop(snapshot.snapshot_id, None)

    def _snapshot_dir(self, snapshot: SnapshotInfo) -> Path:
        """Validate that snapshot metadata points to one direct managed child."""
        if not isinstance(snapshot, SnapshotInfo):
            raise ValueError("snapshot 元数据类型无效")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", snapshot.snapshot_id):
            raise ValueError("snapshot_id 格式无效")
        expected = self.workdir / snapshot.snapshot_id
        supplied = Path(snapshot.snapshot_dir)
        if supplied.is_symlink():
            raise ValueError("snapshot 目录不能是符号链接")
        resolved = supplied.resolve(strict=False)
        if resolved != expected or resolved.parent != self.workdir:
            raise ValueError("snapshot 路径不属于当前沙盒管理器")
        binding = self._snapshot_targets.get(snapshot.snapshot_id)
        if binding is None:
            raise ValueError("snapshot 不属于当前管理器实例")
        target_dir, ignores = binding
        if Path(snapshot.target_dir).resolve(strict=False) != Path(target_dir):
            raise ValueError("snapshot target_dir 与创建时目标不匹配")
        if not isinstance(snapshot.ignores, list) or any(
            not isinstance(name, str) for name in snapshot.ignores
        ):
            raise ValueError("snapshot 忽略规则元数据无效")
        if tuple(snapshot.ignores) != ignores:
            raise ValueError("snapshot 忽略规则与创建时元数据不匹配")
        return resolved

    @contextmanager
    def sandbox_transaction(
        self,
        target_dir: Path | str,
        snapshot_id: str | None = None,
    ) -> Iterator[SnapshotInfo]:
        """Run against an isolated target and restore its baseline on every exit.

        The target must have a single writer for the transaction lifetime. A
        shared project root is not a safe target when workers run concurrently.
        """
        snap = self.create_snapshot(target_dir, snapshot_id=snapshot_id)

        def restore_or_raise() -> None:
            try:
                restored = self.rollback(snap)
                if not restored:
                    raise RuntimeError("rollback returned false")
            except BaseException as exc:
                raise SnapshotRollbackError(
                    Path(snap.snapshot_dir), f"{type(exc).__name__}: {exc}"
                ) from exc

        try:
            yield snap
        except BaseException as operation_error:
            try:
                restore_or_raise()
            except SnapshotRollbackError as rollback_error:
                rollback_error.add_note(
                    f"原始操作异常: {type(operation_error).__name__}: {operation_error}"
                )
                raise
            self.cleanup_snapshot(snap)
            raise
        else:
            restore_or_raise()
            self.cleanup_snapshot(snap)

    @staticmethod
    def check_health(
        host: str = "127.0.0.1",
        port: int | None = None,
        pid: int | None = None,
        timeout: float = 2.0,
    ) -> HealthStatus:
        """检查目标服务端口或进程健康状况。"""
        start = time.perf_counter()

        # 1. 进程检查
        if pid is not None:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return HealthStatus(
                    is_healthy=False,
                    status_code="PID_DEAD",
                    detail=f"目标进程 PID {pid} 不存在或无权访问",
                    latency_ms=(time.perf_counter() - start) * 1000,
                )

        # 2. 端口探活
        if port is not None:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            try:
                res = s.connect_ex((host, port))
                latency = (time.perf_counter() - start) * 1000
                if res == 0:
                    return HealthStatus(
                        is_healthy=True,
                        status_code="HEALTHY",
                        detail=f"服务在 {host}:{port} 正常响应",
                        latency_ms=latency,
                    )
                return HealthStatus(
                    is_healthy=False,
                    status_code="PORT_CLOSED",
                    detail=f"无法连接到 {host}:{port} (errno={res})",
                    latency_ms=latency,
                )
            except socket.timeout:
                return HealthStatus(
                    is_healthy=False,
                    status_code="TIMEOUT",
                    detail=f"探活连接 {host}:{port} 发生超时 ({timeout}s)",
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
            except Exception as e:
                return HealthStatus(
                    is_healthy=False,
                    status_code="PORT_CLOSED",
                    detail=f"探活异常: {e}",
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
            finally:
                s.close()

        return HealthStatus(
            is_healthy=True,
            status_code="HEALTHY",
            detail="服务就绪",
            latency_ms=(time.perf_counter() - start) * 1000,
        )

    def recover_service(
        self,
        cmd: Sequence[str] | str,
        port: int | None = None,
        pid: int | None = None,
        cwd: Path | str | None = None,
        startup_timeout: float = 10.0,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, int | None, str]:
        """重启当前管理器启动的服务；拒绝终止未知进程或端口所有者。

        返回: (是否成功, 新PID, 详细说明)
        """
        if isinstance(cmd, str):
            try:
                command = shlex.split(cmd)
            except ValueError as exc:
                return False, None, f"启动命令解析失败: {exc}"
        else:
            command = list(cmd)
        if not command or any(not isinstance(part, str) or not part for part in command):
            return False, None, "启动命令必须是非空 argv"

        managed_proc: subprocess.Popen[bytes] | None = None
        if pid is not None:
            managed_proc = self._managed_processes.get(pid)
            if managed_proc is None:
                return False, None, f"拒绝终止非当前管理器启动的进程 PID {pid}"
            if managed_proc.poll() is not None:
                self._managed_processes.pop(pid, None)
                # 进程已退出：不再把 PID 当作终止授权；允许下方端口检查
                # 决定是否可启动替代进程，避免 daemon 永久重试陈旧 PID。
                managed_proc = None
                pid = None

        # 端口扫描只作为拒绝未知所有者的依据，绝不授权终止端口占用者。
        if port is not None:
            owners = self._port_pids(port)
            if owners is None:
                return False, None, f"无法核实端口 {port} 的占用者，已阻断服务恢复"
            foreign = owners - ({pid} if managed_proc is not None else set())
            if foreign:
                return False, None, (
                    f"拒绝释放未证明归属的端口 {port}，占用 PID: "
                    + ",".join(str(item) for item in sorted(foreign))
                )

        # 只通过本管理器保留的 Popen 句柄停止原进程，不把裸 PID 当作授权。
        if pid is not None:
            stopped, detail = self._terminate_managed_process(pid)
            if not stopped:
                return False, None, detail

        # 2. 启动新服务；字符串命令也已解析为 argv，禁止 shell=True。
        try:
            p_env = os.environ.copy()
            if env:
                p_env.update(env)

            proc = subprocess.Popen(
                command,
                shell=False,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=p_env,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            new_pid = proc.pid
            self._managed_processes[new_pid] = proc
        except Exception as exc:
            return False, None, f"启动被测服务进程失败: {exc}"

        # 3. 轮询等待端口就绪
        if port is not None:
            deadline = time.time() + startup_timeout
            while time.time() < deadline:
                if proc.poll() is not None:
                    self._managed_processes.pop(new_pid, None)
                    return False, None, f"被测服务在启动期间退出 (exit code {proc.returncode})"
                h = self.check_health(port=port, timeout=0.5)
                if h.is_healthy:
                    if proc.poll() is not None:
                        self._managed_processes.pop(new_pid, None)
                        return False, None, "端口探活成功，但本次启动的受管进程已退出"
                    return True, new_pid, f"服务重启成功并在端口 {port} 上就绪"
                time.sleep(0.3)

            # 超时仅清理本次创建的 Popen；无法确认退出时不报告已清理。
            stopped, cleanup_detail = self._terminate_managed_process(new_pid)
            if not stopped:
                return False, None, (
                    f"等待服务端口 {port} 就绪超时 ({startup_timeout}s)，"
                    f"且无法确认新进程已退出: {cleanup_detail}"
                )
            return False, None, f"等待服务端口 {port} 就绪超时 ({startup_timeout}s)"

        return True, new_pid, "服务重启完成 (无端口探活)"

    def _terminate_managed_process(self, pid: int) -> tuple[bool, str]:
        """通过同一管理器保存的子进程句柄停止进程。"""
        proc = self._managed_processes.get(pid)
        if proc is None:
            return False, f"拒绝终止非当前管理器启动的进程 PID {pid}"
        if proc.poll() is not None:
            self._managed_processes.pop(pid, None)
            return True, f"受管进程 PID {pid} 已退出"

        try:
            proc.terminate()
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        except ProcessLookupError:
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                return False, f"无法确认受管进程 PID {pid} 已退出"
        except Exception as exc:
            return False, f"停止受管进程失败 ({type(exc).__name__})"

        if proc.poll() is None:
            return False, f"无法确认受管进程 PID {pid} 已退出"
        self._managed_processes.pop(pid, None)
        return True, f"已停止受管进程 PID {pid}"

    @staticmethod
    def _port_pids(port: int) -> set[int] | None:
        """读取 TCP 监听者；查询失败与“无人监听”分开返回。"""
        try:
            result = subprocess.run(
                ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
                return set()
            if result.returncode != 0 or result.stderr.strip():
                return None
            pids = {int(line.strip()) for line in result.stdout.splitlines() if line.strip()}
            if any(item <= 0 for item in pids):
                return None
            return pids
        except (OSError, ValueError):
            return None
