#!/usr/bin/env python3
"""core/mcp_handlers/channel_lifecycle.py - 宿主信道租约、空闲退出与心跳维持。"""

from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅注解
    from core.skills.bugx_mcp_server import BugxMcpService

import atexit
import errno
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None

import core.bugx_paths as bugx_paths
import core.secure_atomic as secure_atomic
from core.mcp_handlers import McpProtocolError

logger = logging.getLogger(__name__)

HOST_SESSION_HEARTBEAT_INTERVAL_SECONDS = 5.0

# i18n 辅助
def _i18n_exc(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        import core.i18n as i18n
        return i18n.exception_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def _i18n_prompt(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        import core.i18n as i18n
        return i18n.prompt_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback

def _pid_alive(pid: int) -> bool:
    """跨平台 PID 存活检测：存活 True，死亡/无权限确认死亡 False。

    Unix 用 kill(pid, 0)；Windows 用 OpenProcess 轮询（无 psutil 依赖）。
    ESRCH=死亡；EPERM=进程存在但无权限（视为存活，避免误杀）。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True  # 查不到退出码，保守视为存活
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True  # 检测本身失败，保守视为存活，不自愈
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 存在但无权限，视为存活
        except OSError:
            return True  # 其他 OS 错误，保守视为存活


_HOST_CHANNEL_STATE_TTL_SECONDS = 3600.0

# ---------------------------------------------------------------------------
# DOC-230 3.1 层 2/层 3 与 3.2：MCP 通道生命周期自清（原则 P-E：进程生命周期
# 必须有自终止条件，**不依赖宿主正确回收**）。
#
# 背景（真实事故）：被遗弃的 host MCP 进程（实测存活 1d16h）长期占着
# `controller.lock`，使新会话永远拿不到 host 通道；且进程本身不退出，形成泄漏。
# 层 2 让持有者**自己让位**；层 3 提供仅显式的兜底接管；3.2 让 worker 通道同样
# 具备空闲自退，并给出 `mcp prune` 运维入口与 doctor 卫生检查。
# ---------------------------------------------------------------------------
_CHANNEL_IDLE_ENV = {
    "host": "BUGX_HOST_IDLE_SELF_EXIT",
    "worker": "BUGX_MCP_IDLE_EXIT_SECONDS",
}
_DEFAULT_CHANNEL_IDLE_SELF_EXIT_SECONDS = 1800.0
_CHANNEL_IDLE_CHECK_INTERVAL_SECONDS = 30.0
_TAKEOVER_GRACE_SECONDS = 5.0


def _idle_self_exit_seconds(role: str) -> float:
    """通道空闲自退阈值（秒）。``<=0`` 显式禁用；非法/非有限值回退默认。"""
    raw = (os.environ.get(_CHANNEL_IDLE_ENV.get(role, "")) or "").strip()
    if not raw:
        return _DEFAULT_CHANNEL_IDLE_SELF_EXIT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CHANNEL_IDLE_SELF_EXIT_SECONDS
    if value != value or value in (float("inf"), float("-inf")):
        return _DEFAULT_CHANNEL_IDLE_SELF_EXIT_SECONDS
    return value


class _ChannelActivity:
    """本进程的通道活动记录：空闲自退的**唯一判据**（原则 P-C 闲置语义）。

    用**活动**而非进程存活判定"被遗弃"：任何一次工具调用都刷新
    ``last_activity_at``；``inflight`` 计数保证"正在执行的工具调用"不会被误退。
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.started_at = time.time()
        self.last_activity_at = self.started_at
        self._last_monotonic = time.monotonic()
        self._inflight = 0
        self._lock = threading.Lock()
        self.worker_ids: set[str] = set()

    def touch(self, *, worker_id: object = None) -> None:
        with self._lock:
            self.last_activity_at = time.time()
            self._last_monotonic = time.monotonic()
            if isinstance(worker_id, str) and worker_id:
                self.worker_ids.add(worker_id)

    def enter(self) -> None:
        with self._lock:
            self._inflight += 1
            self.last_activity_at = time.time()
            self._last_monotonic = time.monotonic()

    def leave(self) -> None:
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def idle_seconds(self) -> float:
        with self._lock:
            return max(0.0, time.monotonic() - self._last_monotonic)



def _channel_active_claim_keys(service: "BugxMcpService") -> list[str] | None:
    """读取项目内活跃 claim 的键；**读不到返回 None**（不确定即不自退，fail-safe）。"""
    try:
        state = service.get_supervisor().status()
        claims = getattr(state, "active_claims", None)
        if not isinstance(claims, Mapping):
            return None
        return [str(key) for key in claims]
    except Exception:  # noqa: BLE001 - 只读探测，任何失败都按"不确定"处理
        return None


def _channel_idle_exit_reason(
    service: "BugxMcpService", activity: "_ChannelActivity",
) -> str | None:
    """返回自退理由；``None`` 表示不满足自退前置条件。

    前置条件（DOC-230 3.1 层 2 / 3.2，含审计 R2-Q3/R2-Q6 裁定）：
    1. 阈值启用且空闲超阈值；
    2. **无进行中的工具调用**；
    3. 无会被本次退出影响的活跃 claim——
       * worker 通道：本通道用过的 worker_id 名下不得有活跃 claim
         （含"已 claim 未 start"的 R2-Q6 窗口）；
       * host 通道：**项目内任何活跃 claim 都不得存在**，否则可能让等待宿主
         决策的 worker 被永久挂起（R2-Q3）。
    读不到状态时**不自退**（fail-safe：宁可多活一轮，不可误退）。
    """
    threshold = _idle_self_exit_seconds(activity.role)
    if threshold <= 0:
        return None
    idle = activity.idle_seconds()
    if idle < threshold:
        return None
    if activity.inflight:
        return None
    claims = _channel_active_claim_keys(service)
    if claims is None:
        return None
    if activity.role == "worker":
        relevant = [
            key for key in claims
            if any(worker_id and worker_id in key for worker_id in activity.worker_ids)
        ]
        if relevant:
            return None
    elif claims:
        return None
    return f"idle {idle:.0f}s >= {threshold:.0f}s"


def _record_lease_event(service: "BugxMcpService", event_type: str, **fields: object) -> None:
    """把通道生命周期事件写入项目审计链；best-effort（失败不阻断自退）。"""
    try:
        supervisor = service.get_supervisor()
        supervisor._mutate(
            lambda state, now: supervisor._append_event(state, event_type, now, **fields)
        )
    except Exception:  # noqa: BLE001 - 审计写入失败不得阻断生命周期动作
        return


def _channel_self_retire(
    service: "BugxMcpService",
    activity: "_ChannelActivity",
    reason: str,
    *,
    lease: "_ControllerLease | None" = None,
    exit_hook: "Callable[[int], None] | None" = None,
) -> None:
    """释放租约 → 写审计事件 → 退出（层 2 / 3.2 的实际让位动作）。"""
    _record_lease_event(
        service, "controller_lease_self_released",
        role=activity.role, pid=os.getpid(),
        idle_seconds=round(activity.idle_seconds(), 3), reason=reason,
    )
    if lease is not None:
        try:
            lease.release()
        except (OSError, ValueError, RuntimeError):
            pass
    try:
        service.clear_session_proof()
    except (OSError, ValueError, RuntimeError):
        pass
    hook = exit_hook or os._exit
    hook(0)


def _channel_idle_monitor_loop(
    service: "BugxMcpService",
    activity: "_ChannelActivity",
    stop: threading.Event,
    *,
    lease: "_ControllerLease | None" = None,
    interval: float | None = None,
    exit_hook: "Callable[[int], None] | None" = None,
) -> None:
    """后台检查线程：每 ``interval`` 秒判定一次是否应自退。"""
    period = _CHANNEL_IDLE_CHECK_INTERVAL_SECONDS if interval is None else interval
    while not stop.is_set():
        if stop.wait(period):
            return
        reason = _channel_idle_exit_reason(service, activity)
        if reason is not None:
            _channel_self_retire(
                service, activity, reason, lease=lease, exit_hook=exit_hook,
            )
            return


def _read_lease_holder(lock_path: Path) -> int | None:
    """读取主控租约文件里记录的持有者 PID（只读、不取锁）。"""
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            return None
        content = lock_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return int(content) if content.isdigit() and int(content) > 0 else None


def _holder_idle_seconds(state_root: Path, holder_pid: int) -> float | None:
    """由持有者自己发布的 session proof 推算其闲置时长；拿不到返回 None。

    session proof 由 host 心跳线程每 5s 刷新（``refresh_session_proof``），
    因此 ``updated_at`` 是**活动**证据而非进程存活证据（原则 P-C）。
    """
    path = state_root / "mcp-session-proof-host.json"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping) or payload.get("pid") != holder_pid:
        return None
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        return None
    return max(0.0, time.time() - float(updated_at))


def _takeover_holder(
    state_root: Path, *, grace: float = _TAKEOVER_GRACE_SECONDS,
) -> tuple[bool, dict[str, object]]:
    """层 3：仅显式 ``--takeover`` 时调用；返回 (是否应重试获取租约, 证据)。

    安全前置（默认关闭，避免 v1 被裁定的"误杀长任务"）：
    1. 必须读到持有者 PID；
    2. 持有者闲置**必须超阈值**（与层 2 同一阈值），否则拒绝；
    3. SIGTERM 后 ≤ ``grace`` 秒内必须退出；否则拒绝并给可操作错误。
    """
    lock_path = state_root / "controller.lock"
    holder_pid = _read_lease_holder(lock_path)
    evidence: dict[str, object] = {"holder_pid": holder_pid, "taken_over": False}
    if holder_pid is None:
        return (True, evidence)  # 无持有者记录：交给常规获取路径（含陈旧锁自愈）
    if holder_pid == os.getpid():
        return (True, evidence)
    idle = _holder_idle_seconds(state_root, holder_pid)
    evidence["holder_idle_seconds"] = None if idle is None else round(idle, 3)
    threshold = _idle_self_exit_seconds("host")
    if not _pid_alive(holder_pid):
        return (True, evidence)  # 已死：常规路径会自愈陈旧锁
    if threshold <= 0:
        evidence["reason"] = "takeover_disabled_by_zero_threshold"
        return (False, evidence)
    if idle is None or idle < threshold:
        evidence["reason"] = "holder_not_idle_enough"
        evidence["threshold_seconds"] = threshold
        return (False, evidence)
    try:
        os.kill(holder_pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        evidence["reason"] = f"sigterm_failed: {type(exc).__name__}"
        return (False, evidence)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not _pid_alive(holder_pid):
            break
        time.sleep(0.1)
    if _pid_alive(holder_pid):
        evidence["reason"] = "holder_still_alive_after_grace"
        return (False, evidence)
    evidence["taken_over"] = True
    return (True, evidence)


def _host_channel_state_path(state_root: Path) -> Path:
    """DOC-230 3.3：host 通道可用性状态文件（引擎目录外，运行态）。"""
    return state_root / "host-channel-unavailable.json"


def _clear_host_channel_unavailable(state_root: Path) -> None:
    """host 通道成功启动时清除冲突记录（只删普通文件，不穿透符号链接）。"""
    path = _host_channel_state_path(state_root)
    try:
        if path.is_symlink() or not path.is_file():
            return
        path.unlink()
    except OSError:
        return


def _run_external(argv: list[str], *, timeout: float = 2.0) -> tuple[str | None, str | None]:
    """执行外部命令，返回 ``(stdout, failure_reason)``——三态而非"成功/空"两态。

    A11（架构审计）：此前调用 `ps` 只读 stdout、忽略退出码与 stderr，于是
    **参数不可移植时静默降级为"空结果"**（本会话实测：macOS 的 BSD ps 不认识
    `-o etimes`，报 `keyword not found` 并返回空表——调用方看到的却是"没有进程"）。

    三态语义：
      * ``(非空文本, None)`` —— 成功且有输出；
      * ``("", None)``      —— 成功但输出为空（**真的**空，不是失败）；
      * ``(None, 原因)``    —— 失败（非零退出 / OSError / 超时），原因含 stderr 摘要。

    调用方**不得**把 ``(None, ...)`` 当作空结果处理。
    """
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{argv[0]} 无法执行: {exc}"
    stdout = result.stdout or ""
    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"exit={result.returncode}"
        return None, f"{argv[0]} 退出码 {result.returncode}: {detail[:200]}"
    return stdout, None


def _holder_started_at(holder_pid: int | None) -> tuple[str | None, str | None]:
    """尽力取得持有者启动时间；返回 ``(时间文本或 None, 失败原因或 None)``。

    失败**不再**静默等同于"未知时间"：调用方必须能把"ps 不可用"与"ps 说没有
    这个进程"区分开（A11）。本函数仍不因取证失败阻断错误上报。
    """
    if not isinstance(holder_pid, int) or holder_pid <= 0:
        return None, None
    stdout, failure = _run_external(["ps", "-o", "lstart=", "-p", str(holder_pid)])
    if failure is not None:
        return None, failure
    value = (stdout or "").strip()
    return (value or None), None


def _record_host_channel_unavailable(state_root: Path, *, holder_pid: int | None) -> None:
    """DOC-230 3.3：host 通道启动失败必须**显式可见**。

    静默降级为 worker-only 会让 Agent 反复试错才发现自己不具备恢复能力
    （审计 V10）；此处落盘一条带 TTL 的记录，供 worker 通道内联与 doctor 读取。
    """
    holder_started_at, holder_started_at_error = _holder_started_at(holder_pid)
    payload = {
        "schema_version": 1,
        "reason": "controller_lease_occupied",
        "holder_pid": holder_pid,
        "holder_started_at": holder_started_at,
        # A11：取证失败必须**可诊断**，不能与"成功但无输出"混同。
        "holder_started_at_error": holder_started_at_error,
        "holder_idle_seconds": None,
        "observed_at": time.time(),
        "ttl_seconds": _HOST_CHANNEL_STATE_TTL_SECONDS,
        "how_to_unblock": (
            f"释放被占用的 host 通道：kill {holder_pid}（或关闭占用它的 IDE 会话）"
            if isinstance(holder_pid, int) and holder_pid > 0
            else "关闭占用 host 通道的 IDE 会话，或 bugx mcp serve --role host --takeover"
        ),
    }
    try:
        secure_atomic.atomic_write_json(
            _host_channel_state_path(state_root), payload, mode=0o600, parent_mode=0o700,
        )
    except (OSError, ValueError, RuntimeError):
        return


def _get_fcntl() -> Any:
    mod = sys.modules.get("core.skills.bugx_mcp_server")
    if mod is not None and hasattr(mod, "fcntl"):
        return getattr(mod, "fcntl")
    return fcntl


def _get_msvcrt() -> Any:
    mod = sys.modules.get("core.skills.bugx_mcp_server")
    if mod is not None and hasattr(mod, "msvcrt"):
        return getattr(mod, "msvcrt")
    return msvcrt


class _ControllerLease:
    """Hold one host MCP control lease for the lifetime of its stdio process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None
        self._released = False

    def acquire(self) -> None:
        cur_fcntl = _get_fcntl()
        cur_msvcrt = _get_msvcrt()
        if cur_fcntl is None and cur_msvcrt is None:  # pragma: no cover - rare platform fallback
            raise McpProtocolError("当前平台不支持 Supervisor 主控租约")
        if self.path.is_symlink():
            raise McpProtocolError(f"拒绝使用符号链接主控租约: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # P3-7：open("a+") 在检查与打开之间存在 TOCTOU 窗口，符号链接可在
        # is_symlink 之后被换回。改用 O_NOFOLLOW 原子打开（与
        # audit_supervisor._file_lock 同型），ELOOP 即符号链接竞争，按原错误拒绝。
        open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, open_flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise McpProtocolError(
                    f"拒绝使用符号链接主控租约: {self.path}"
                ) from exc
            raise McpProtocolError(
                f"无法安全打开主控租约: {self.path}"
            ) from exc
        self._handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        if cur_fcntl is not None:
            try:
                cur_fcntl.flock(self._handle.fileno(), cur_fcntl.LOCK_EX | cur_fcntl.LOCK_NB)
                self._handle.seek(0)
                self._handle.truncate()
                self._handle.write(f"{os.getpid()}\n")
                self._handle.flush()
            except (BlockingIOError, OSError) as exc:
                holder = ""
                holder_pid: int | None = None
                try:
                    self._handle.seek(0)
                    content = self._handle.read().strip()
                    if content.isdigit():
                        holder = f" (PID {content})"
                        holder_pid = int(content)
                except (OSError, ValueError):
                    pass
                # BX-R4-03：锁持有者已死 → 陈旧锁自愈。flock 是内核态锁，
                # 持有进程死亡后锁已释放；文件里残留的 PID 只是墓碑。
                # 确认死亡后截断墓碑、重试一次独占获取；重试仍失败才报错。
                if holder_pid is not None and not _pid_alive(holder_pid):
                    try:
                        self._handle.seek(0)
                        self._handle.truncate()
                        self._handle.flush()
                        cur_fcntl.flock(self._handle.fileno(), cur_fcntl.LOCK_EX | cur_fcntl.LOCK_NB)
                        self._handle.seek(0)
                        self._handle.truncate()
                        self._handle.write(f"{os.getpid()}\n")
                        self._handle.flush()
                        atexit.register(self.release)
                        _clear_host_channel_unavailable(self.path.parent)
                        return
                    except (BlockingIOError, OSError):
                        pass  # 有活锁竞争者接管，落到下面的报错分支
                self._handle.close()
                self._handle = None
                _record_host_channel_unavailable(self.path.parent, holder_pid=holder_pid)
                raise McpProtocolError(
                    _i18n_exc(
                        "mcp.session_occupied",
                        f"当前项目已有 IDE 占用 BugX Session{holder}；请先结束原会话或等待其退出。若需多宿主并发挖掘，请使用 Git Worktree（git worktree add）为各宿主创建独立工作区分支",
                        holder=holder,
                    )
                ) from exc
        elif cur_msvcrt is not None:
            try:
                self._handle.seek(0)
                cur_msvcrt.locking(self._handle.fileno(), cur_msvcrt.LK_NBLCK, 1)
                self._handle.seek(0)
                self._handle.truncate()
                self._handle.write(f"{os.getpid()}\n")
                self._handle.flush()
            except (BlockingIOError, OSError) as exc:
                holder = ""
                holder_pid = None
                try:
                    self._handle.seek(0)
                    content = self._handle.read().strip()
                    if content.isdigit():
                        holder = f" (PID {content})"
                        holder_pid = int(content)
                except (OSError, ValueError):
                    pass
                # Windows 分支同理：持有者已死则解锁后重试一次。
                if holder_pid is not None and not _pid_alive(holder_pid):
                    try:
                        self._handle.seek(0)
                        cur_msvcrt.locking(self._handle.fileno(), cur_msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                    try:
                        self._handle.seek(0)
                        cur_msvcrt.locking(self._handle.fileno(), cur_msvcrt.LK_NBLCK, 1)
                        self._handle.seek(0)
                        self._handle.truncate()
                        self._handle.write(f"{os.getpid()}\n")
                        self._handle.flush()
                        atexit.register(self.release)
                        _clear_host_channel_unavailable(self.path.parent)
                        return
                    except (BlockingIOError, OSError):
                        pass
                self._handle.close()
                self._handle = None
                _record_host_channel_unavailable(self.path.parent, holder_pid=holder_pid)
                raise McpProtocolError(
                    _i18n_exc(
                        "mcp.session_occupied",
                        f"当前项目已有 IDE 占用 BugX Session{holder}；请先结束原会话或等待其退出。若需多宿主并发挖掘，请使用 Git Worktree（git worktree add）为各宿主创建独立工作区分支",
                        holder=holder,
                    )
                ) from exc
        atexit.register(self.release)
        _clear_host_channel_unavailable(self.path.parent)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        cur_fcntl = _get_fcntl()
        cur_msvcrt = _get_msvcrt()
        if self._handle is not None:
            try:
                try:
                    self._handle.seek(0)
                    self._handle.truncate()
                    self._handle.flush()
                except OSError:
                    pass
                if cur_fcntl is not None:
                    cur_fcntl.flock(self._handle.fileno(), cur_fcntl.LOCK_UN)
                elif cur_msvcrt is not None:
                    try:
                        self._handle.seek(0)
                        cur_msvcrt.locking(self._handle.fileno(), cur_msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            finally:
                self._handle.close()
                self._handle = None

    @property
    def is_locked(self) -> bool:
        return self._handle is not None and not self._released

    def __del__(self) -> None:  # pragma: no cover - best-effort process cleanup.
        self.release()


def _host_session_heartbeat_once(root: Path) -> bool:
    """Refresh the host Session runtime without bypassing its state machine."""
    try:
        from core.launch_phases import session_runtime

        scope = bugx_paths.require(root)
        # 不能直接赋 session_runtime.RUNTIME_FILE：runtime_lock() 与
        # load_runtime() 都会按 _project_root() 重算并覆盖它，root 参数随之
        # 静默失效（MCP 服务不 chdir，cwd 未必是绑定项目）。必须钉住路径。
        with session_runtime._pinned_runtime_paths(scope.runtime, scope.runtime_lock):
            _, updated = session_runtime.heartbeat_runtime(owner_pid=os.getpid())
        return updated
    except (ImportError, OSError, TimeoutError, ValueError, RuntimeError):
        # MCP must remain available when a project has no active Session or
        # its runtime is already in a terminal/corrupt state; post will report
        # the authoritative failure through its normal gate.
        return False


def _host_session_heartbeat_loop(service: "BugxMcpService", stop: threading.Event) -> None:
    """Keep the host Session alive while its MCP stdio channel is open."""
    while not stop.is_set():
        _host_session_heartbeat_once(service.root)
        service.refresh_session_proof()
        if stop.wait(HOST_SESSION_HEARTBEAT_INTERVAL_SECONDS):
            return


# (Argument validation helpers imported from mcp_handlers)


# (compact_json, format_result, serialize_value imported from mcp_handlers)


def _error_payload(error: BaseException) -> dict[str, object]:
    return {
        "error_type": type(error).__name__,
        "message": str(error) or type(error).__name__,
        "error_code": "MCP_CALL_FAILED",
        "interactive_required": True,
        "next_action": "PROMPT_USER",
        "prompt": (
            "MCP 调用失败，请立即询问用户：1) 立即重试 2) 本轮切换手动/回退 "
            "3) 下次再说（停止本轮）"
        ),
        "choices": ["retry", "fallback", "later"],
    }


def _call_failure_data(operation: object, error: BaseException) -> dict[str, object]:
    """为 tools/call 协议错误提供可被宿主直接呈现的交互门禁。"""
    payload = _error_payload(error)
    payload["operation"] = str(operation or "unknown")
    return payload


# (next_action imported from mcp_handlers)


