#!/usr/bin/env python3
"""受控的进程 Worker 执行器。

所有需要 BugX 代为启动的进程都经过这里：标准流落盘避免孙进程继承
PIPE 导致父进程退出后 ``communicate`` 永久等待；每个进程组独立启动，
超时、输出超限或父进程结束时都会回收整组进程。
"""

from __future__ import annotations

import hashlib
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence


DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
OUTPUT_LIMIT_EXIT_CODE = 125
_POLL_INTERVAL_SECONDS = 0.05
_KILL_GRACE_SECONDS = 0.5

# 全仓唯一的 shell 语法判据。任何「字符串命令 → argv」的转换都必须复用
# ``split_argv``，禁止各模块再各自维护一份运算符清单（否则新增运算符时
# 会出现漏改一处就静默放行 shell 注入的漂移）。
SHELL_PUNCTUATION = ";&|<>`$()"
SHELL_OPERATORS = frozenset({
    ";", "&", "&&", "|", "||", "<", ">", ">>", "<<", "<&", ">&",
    "`", "$(", "(", ")",
})


class WorkerExecutionError(RuntimeError):
    """Worker 进程参数或执行边界无效。"""


def split_argv(
    template: str,
    *,
    error_factory: Callable[[str], Exception] = WorkerExecutionError,
) -> list[str]:
    """把命令模板解析为 argv 数组，并拒绝未授权的 shell 语法。

    这是仓库中「字符串命令 → argv」的单一实现。调用方传入的字符串命令一律
    先经过本函数，绝不交给 ``shell=True``：shell 解释会带来命令拼接、重定向
    与管道注入，而 BugX 的命令入参来自 Worker（其上下文必然携带被审计项目的
    不可信源码）。

    ``error_factory`` 让各领域模块保留自己的异常类型（值错误语义一致）。
    """
    if not isinstance(template, str):
        raise error_factory("命令模板必须是字符串")
    lexer = shlex.shlex(template, posix=True, punctuation_chars=SHELL_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        argv = list(lexer)
    except ValueError as exc:
        raise error_factory(f"命令无法解析: {exc}") from exc
    if not argv:
        raise error_factory("命令不能为空")
    if any(token in SHELL_OPERATORS for token in argv):
        raise error_factory("命令包含 shell 运算符；如确需使用，请显式传入 --allow-shell")
    return argv


def _validate_command(command: Sequence[str]) -> list[str]:
    if isinstance(command, (str, bytes)):
        raise WorkerExecutionError("command 必须是 argv 数组，禁止 shell 字符串")
    argv = list(command)
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise WorkerExecutionError("command 必须是非空字符串数组")
    return argv


def terminate_group(process: subprocess.Popen[bytes]) -> None:
    """终止独立进程组，并确保父进程被回收。"""
    try:
        if os.name == "posix":
            # 即使父进程已经退出，后台孙进程仍可能保留该进程组；必须
            # 继续清理整个组，避免它们脱离 Worker 生命周期。
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:  # pragma: no cover - Windows fallback
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows fallback
                process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        process.kill()
        process.wait(timeout=1)


def _decode(path: Path, limit: int) -> str:
    try:
        # 只读前 ``limit`` 个字节：大文件先用 read_bytes() 全量载入再切片
        # 会把整份输出拉进内存，输出上限形同虚设。
        with path.open("rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def run_bounded(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    stdin_path: Path | None = None,
    timeout: float | None = None,
    capture: bool = True,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    liveness: Callable[[], bool] | None = None,
    stop_when: Callable[[], bool] | None = None,
    max_total_timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """启动一个有界 Worker 进程并回收其整个进程组。

    返回对象保留 ``subprocess.CompletedProcess`` 兼容形状，并增加：
    ``timed_out``、``output_exceeded``、``pid``、``duration_seconds``。

    ``liveness`` 提供「仍有进展」信号时，``timeout`` 语义从硬上限变为
    无进展上限：每次 liveness 为真都会滑动续期 ``timeout`` 窗口；
    ``max_total_timeout`` 是含续期在内的绝对硬上限，防止无限续期。
    ``stop_when`` 是调用方提供的外部终止门；命中后立即回收进程组，
    并在结果上标记 ``stop_requested``。
    """
    argv = _validate_command(command)
    if not cwd.is_dir():
        raise WorkerExecutionError(f"cwd 不是目录: {cwd}")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise WorkerExecutionError("timeout 必须是正数")
    if liveness is not None and not callable(liveness):
        raise WorkerExecutionError("liveness 必须是可调用对象")
    if stop_when is not None and not callable(stop_when):
        raise WorkerExecutionError("stop_when 必须是可调用对象")
    if max_total_timeout is not None and (
        not isinstance(max_total_timeout, (int, float)) or max_total_timeout <= 0
    ):
        raise WorkerExecutionError("max_total_timeout 必须是正数")
    if not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
        raise WorkerExecutionError("max_output_bytes 必须是正整数")
    stdin_handle = None
    if stdin_path is not None:
        if not isinstance(stdin_path, Path):
            stdin_path = Path(stdin_path)
        if stdin_path.is_symlink() or not stdin_path.is_file():
            raise WorkerExecutionError(f"stdin_path 不是普通文件: {stdin_path}")
        try:
            stdin_handle = stdin_path.open("rb")
        except OSError as exc:
            raise WorkerExecutionError(f"stdin_path 无法打开: {stdin_path}") from exc

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="bugx-worker-") as temporary:
        stdout_path = Path(temporary) / "stdout.log"
        stderr_path = Path(temporary) / "stderr.log"
        stdout_handle = stdout_path.open("wb") if capture else subprocess.DEVNULL
        stderr_handle = stderr_path.open("wb") if capture else subprocess.DEVNULL
        process: subprocess.Popen[bytes] | None = None
        timed_out = False
        stop_requested = False
        output_exceeded = False
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=None if env is None else dict(env),
                stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                close_fds=True,
                start_new_session=(os.name == "posix"),
            )
            hard_deadline = (
                None if max_total_timeout is None else started + float(max_total_timeout)
            )
            deadline = None if timeout is None else started + float(timeout)
            while process.poll() is None:
                now = time.monotonic()
                if stop_when is not None and stop_when():
                    stop_requested = True
                    timed_out = True
                    break
                if hard_deadline is not None and now >= hard_deadline:
                    timed_out = True
                    break
                if deadline is not None and now >= deadline:
                    if liveness is not None and liveness():
                        deadline = now + float(timeout)
                    else:
                        timed_out = True
                        terminate_group(process)
                        break
                if capture:
                    try:
                        output_size = stdout_path.stat().st_size + stderr_path.stat().st_size
                    except OSError:
                        output_size = 0
                    if output_size > max_output_bytes:
                        output_exceeded = True
                        break
                time.sleep(_POLL_INTERVAL_SECONDS)
            if capture and not output_exceeded:
                try:
                    output_exceeded = (
                        stdout_path.stat().st_size + stderr_path.stat().st_size
                        > max_output_bytes
                    )
                except OSError:
                    output_exceeded = False
        finally:
            # One cleanup path covers normal completion, timeout, exceptions,
            # and KeyboardInterrupt. The isolated process group prevents a
            # cancelled host from outliving its controller.
            if process is not None:
                terminate_group(process)
            if stdin_handle is not None:
                stdin_handle.close()
            if hasattr(stdout_handle, "close"):
                stdout_handle.close()  # type: ignore[union-attr]
            if hasattr(stderr_handle, "close"):
                stderr_handle.close()  # type: ignore[union-attr]

        returncode = 1 if process is None or process.returncode is None else process.returncode
        if output_exceeded and returncode == 0:
            # 输出被截断时不能把“看似成功但证据不完整”上报为成功。
            returncode = OUTPUT_LIMIT_EXIT_CODE
        stdout = _decode(stdout_path, max_output_bytes) if capture else None
        stderr = _decode(stderr_path, max_output_bytes) if capture else None
        result = subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        result.timed_out = timed_out  # type: ignore[attr-defined]
        result.stop_requested = stop_requested  # type: ignore[attr-defined]
        result.output_exceeded = output_exceeded  # type: ignore[attr-defined]
        result.pid = process.pid if process is not None else None  # type: ignore[attr-defined]
        result.duration_seconds = max(0.0, time.monotonic() - started)  # type: ignore[attr-defined]
        result.stdout_sha256 = (  # type: ignore[attr-defined]
            hashlib.sha256((stdout or "").encode("utf-8")).hexdigest()
        )
        result.stderr_sha256 = (  # type: ignore[attr-defined]
            hashlib.sha256((stderr or "").encode("utf-8")).hexdigest()
        )
        return result


__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES", "OUTPUT_LIMIT_EXIT_CODE",
    "SHELL_OPERATORS", "SHELL_PUNCTUATION",
    "WorkerExecutionError", "run_bounded", "split_argv",
]
