#!/usr/bin/env python3
"""Per-connection MCP proof and cooperative retirement protocol.

The MCP server used to publish one mutable ``mcp-session-proof-<role>.json``
file.  A second server could overwrite that file and a stale PID could then
look like the current connection.  This module keeps the protocol deliberately
small: one UUID-scoped directory per server instance, bounded JSON, and a
retirement request that the target process must acknowledge itself.

Only the standard library is used here.  The module is intentionally below the
server/host layers so readers and writers share exactly the same validation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 2
MAX_PROOF_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 16 * 1024
PHASE_ACTIVE = "ACTIVE"
PHASE_RETIRING = "RETIRING"
PHASE_CLOSED = "CLOSED"
VALID_PHASES = frozenset({PHASE_ACTIVE, PHASE_RETIRING, PHASE_CLOSED})
VALID_ROLES = frozenset({"host", "worker"})
#: Darwin ``proc_pidinfo`` flavor: ``PROC_PIDTBSDINFO``（进程启动时间所在结构）。
_PROC_PIDTBSDINFO = 3


class SessionProofError(ValueError):
    """A proof or retirement request failed closed validation."""


class RetirementBusy(SessionProofError):
    """The target cannot retire while work or in-flight requests exist."""


class RetirementUnknown(SessionProofError):
    """A proof/request could not be tied to a known live instance."""


def _finite_timestamp(value: object, field: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionProofError(f"{field} 必须是有限时间戳或 null")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise SessionProofError(f"{field} 必须是正有限时间戳")
    return value


def _text(value: object, field: str, *, max_length: int = 1024, required: bool = True) -> str | None:
    if not isinstance(value, str):
        if value is None and not required:
            return None
        raise SessionProofError(f"{field} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise SessionProofError(f"{field} 不能为空")
    if len(value) > max_length:
        raise SessionProofError(f"{field} 超过长度限制")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SessionProofError(f"{field} 必须是 >= {minimum} 的整数")
    return value


def new_instance_id() -> str:
    """Return a fresh, non-reused connection identity."""
    return str(uuid.uuid4())


def instance_id() -> str:
    """Compatibility alias used by server bootstrap code."""
    return new_instance_id()


def _darwin_start_token(pid: int) -> str | None:
    """macOS: 进程启动时间（libproc 系统调用，不 fork ``ps``）。

    为什么必须要这条路径：宿主沙箱可以**禁止执行** ``/bin/ps``（Codex
    ``--approve-for-me`` 实测 ``/bin/ps: Operation not permitted``）。旧实现只有
    ``/proc``（Linux）与 ``ps``（其它 POSIX）两条路，于是在该宿主里**任何** pid 都
    拿不到 start token ⇒ 会话证明校验恒定失败 ⇒ supervisor MCP 门禁永远报「已配置
    但当前宿主未加载」。

    ``proc_pidinfo`` 是同一份内核信息的系统调用入口，不涉及 exec，也不写盘；
    拿不到（跨用户、进程已退出、结构体尺寸不符）一律返回 ``None`` —— 校验层会把
    ``None`` 当作「无法比对」而不是「比对通过」。
    """

    try:
        import ctypes
        import ctypes.util
    except ImportError:  # pragma: no cover - 标准库缺失属环境异常
        return None
    try:
        library = ctypes.CDLL(
            ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        )
    except OSError:
        return None

    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    info = ProcBsdInfo()
    try:
        function = library.proc_pidinfo
        function.restype = ctypes.c_int
        function.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        ]
        written = function(
            pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
    except (AttributeError, ctypes.ArgumentError, OSError):
        return None
    if written != ctypes.sizeof(info):
        return None
    seconds = int(info.pbi_start_tvsec)
    micros = int(info.pbi_start_tvusec)
    if seconds <= 0:
        return None
    return f"libproc:{seconds}.{micros}"


def process_start_token(pid: int | None) -> str | None:
    """Return a PID-reuse-resistant process start token where available."""
    if pid is None or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        fields = proc_stat.read_text(encoding="utf-8").split()
        if len(fields) > 21:
            return f"proc:{fields[21]}"
    except (OSError, ValueError):
        pass
    if sys.platform == "darwin":
        # 顺序有意：libproc 必须在 ``ps`` 之前——宿主沙箱禁 exec 时 ``ps`` 恒失败，
        # 而证明的**写入方**（MCP 服务）与**校验方**（引擎）必须得到同一种 token 格式。
        token = _darwin_start_token(pid)
        if token is not None:
            return token
    if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000 | 0x0400, False, pid)
            if handle:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                try:
                    ok = kernel32.GetProcessTimes(
                        handle, ctypes.byref(creation), ctypes.byref(exit_time),
                        ctypes.byref(kernel_time), ctypes.byref(user_time),
                    )
                    if ok:
                        stamp = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                        return f"win:{stamp}"
                finally:
                    kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    try:
        import subprocess

        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=2,
        )
        value = " ".join(result.stdout.split())
        if result.returncode == 0 and value:
            return f"ps:{value}"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def start_token_seconds(token: str | None) -> float | None:
    """把 start token 解析成 epoch 秒（无法解析返回 ``None``）。

    支持三种形态：
    * ``libproc:<sec>.<usec>``（macOS libproc）
    * ``ps:<ctime 串>``（``ps -o lstart=``，**本地时区**）
    * ``proc:<ticks>``（Linux ``/proc/<pid>/stat`` 的第 22 字段）：单位是内核时钟滴答，
      与 epoch 无关，因此**不做**跨格式换算，只用于同格式等值比较。
    """

    if not isinstance(token, str) or ":" not in token:
        return None
    prefix, _, value = token.partition(":")
    if prefix == "libproc":
        seconds, _, micros = value.partition(".")
        try:
            return float(int(seconds)) + (int(micros or 0) / 1_000_000.0)
        except ValueError:
            return None
    if prefix == "ps":
        try:
            parsed = time.strptime(" ".join(value.split()), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            return None
        return time.mktime(parsed)
    return None


def start_tokens_match(recorded: object, observed: object, *, tolerance: float = 1.0) -> bool:
    """两个 start token 是否指向同一次进程启动。

    为什么需要跨格式比较：token 的**写入方**是 MCP 服务进程、**校验方**是引擎进程，
    两者可能是不同代际的引擎（宿主用全局引擎起 MCP，项目用本地引擎跑引擎命令）。
    macOS 上 ``ps`` 不可用时新旧实现分别写下 ``ps:`` 与 ``libproc:``；只做字符串相等
    会把「同一个进程」误判为 PID 复用，反向导致门禁永久要求重启宿主（真机缺陷）。
    因此：能解析成 epoch 秒时按时间容差比较（1s），不能解析时退回严格相等。
    """

    if not isinstance(recorded, str) or not isinstance(observed, str):
        return False
    if recorded == observed:
        return True
    left = start_token_seconds(recorded)
    right = start_token_seconds(observed)
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def instance_root(state_root: Path, instance: str) -> Path:
    """Resolve the UUID-scoped proof directory without following symlinks."""
    state_root = Path(state_root)
    instance = _text(instance, "instance_id", max_length=64) or ""
    try:
        uuid.UUID(instance)
    except (ValueError, AttributeError) as exc:
        raise SessionProofError("instance_id 必须是 UUID") from exc
    if state_root.is_symlink() or not state_root.is_dir():
        raise SessionProofError(f"state_root 不是安全目录: {state_root}")
    root = state_root / "mcp-sessions" / instance
    if root.exists() and root.is_symlink():
        raise SessionProofError(f"拒绝符号链接 session 目录: {root}")
    return root


def proof_path(state_root: Path, instance: str) -> Path:
    return instance_root(state_root, instance) / "proof.json"


def retire_request_path(state_root: Path, instance: str) -> Path:
    return instance_root(state_root, instance) / "retire-request.json"


def _atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    if len(payload) > MAX_PROOF_BYTES:
        raise SessionProofError(f"proof 超过 {MAX_PROOF_BYTES} bytes")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _tool_names(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise SessionProofError("tool_names 必须是数组")
    values = []
    for item in list(value)[:256]:
        if not isinstance(item, str) or not item.strip():
            raise SessionProofError("tool_names 只能包含非空字符串")
        values.append(item[:256])
    return values


def normalize_proof(payload: Mapping[str, object], *, now: float | None = None) -> dict[str, object]:
    """Validate and normalize one schema-2 proof without changing its meaning."""
    if not isinstance(payload, Mapping):
        raise SessionProofError("proof 必须是对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SessionProofError("只支持 MCP proof schema=2")
    role = _text(payload.get("role"), "role", max_length=16)
    if role not in VALID_ROLES:
        raise SessionProofError("role 必须是 host 或 worker")
    root = _text(payload.get("root"), "root", max_length=4096)
    root = str(Path(root).resolve())
    state_scope = _text(payload.get("scope"), "scope", max_length=512, required=False)
    lifecycle_id = _text(payload.get("lifecycle_id"), "lifecycle_id", max_length=512, required=False)
    if state_scope is None and lifecycle_id is not None:
        state_scope = lifecycle_id
    scope_generation = _integer(payload.get("scope_generation", 0), "scope_generation", minimum=0)
    instance = _text(payload.get("instance_id"), "instance_id", max_length=64)
    try:
        uuid.UUID(instance or "")
    except (ValueError, AttributeError) as exc:
        raise SessionProofError("instance_id 必须是 UUID") from exc
    pid = _integer(payload.get("pid"), "pid", minimum=1)
    process_token = _text(
        payload.get("process_start_token"), "process_start_token",
        max_length=512, required=False,
    )
    engine_build = _text(payload.get("engine_build"), "engine_build", max_length=256)
    entrypoint_hash = _text(payload.get("entrypoint_hash"), "entrypoint_hash", max_length=128)
    if entrypoint_hash and not all(ch in "0123456789abcdefABCDEF" for ch in entrypoint_hash):
        raise SessionProofError("entrypoint_hash 必须是十六进制摘要")
    transport = _text(payload.get("transport"), "transport", max_length=64)
    phase = _text(payload.get("phase", PHASE_ACTIVE), "phase", max_length=16)
    if phase not in VALID_PHASES:
        raise SessionProofError("phase 无效")
    written = payload.get("written_at", payload.get("proof_written_at"))
    written_at = _finite_timestamp(written, "written_at")
    # Keep the two names during migration; readers use ``written_at`` and
    # ``proof_written_at`` is retained as a descriptive alias in receipts.
    last_request = _finite_timestamp(payload.get("last_request_at"), "last_request_at", allow_none=True)
    last_progress = _finite_timestamp(payload.get("last_progress_at"), "last_progress_at", allow_none=True)
    # A completed request records progress after the request was received, so
    # these clocks intentionally have no ordering relationship.  They are
    # separate liveness signals; collapsing them makes a slow but progressing
    # connection look stale.
    if last_request is not None and last_request > float(written_at) + 86400:
        raise SessionProofError("last_request_at 与 proof 写入时间差异异常")
    inflight = _integer(payload.get("inflight", 0), "inflight", minimum=0)
    tool_names = _tool_names(payload.get("tool_names", []))
    normalized = dict(payload)
    normalized.update({
        "schema_version": SCHEMA_VERSION,
        "root": root,
        "scope": state_scope,
        "lifecycle_id": lifecycle_id,
        "scope_generation": scope_generation,
        "role": role,
        "instance_id": instance,
        "pid": pid,
        "process_start_token": process_token,
        "engine_build": engine_build,
        "entrypoint_hash": entrypoint_hash,
        "transport": transport,
        "phase": phase,
        "written_at": written_at,
        "proof_written_at": written_at,
        "last_request_at": last_request,
        "last_progress_at": last_progress,
        "inflight": inflight,
        "tool_names": tool_names,
    })
    current = float(time.time() if now is None else now)
    if not math.isfinite(current):
        raise SessionProofError("当前时间无效")
    if written_at > current + 300:
        raise SessionProofError("written_at 不能明显位于未来")
    normalized["idle_age"] = None if last_request is None else max(0.0, current - float(last_request))
    normalized["progress_age"] = None if last_progress is None else max(0.0, current - float(last_progress))
    return normalized


def validate_proof(payload: Mapping[str, object], **kwargs: object) -> dict[str, object]:
    """Public validator; aliases make host and server call sites explicit."""
    result = normalize_proof(payload, now=kwargs.get("now"))
    expected_role = kwargs.get("expected_role")
    expected_instance = kwargs.get("expected_instance_id")
    if expected_role is not None and result.get("role") != expected_role:
        raise SessionProofError("proof role 与目标连接不一致")
    if expected_instance is not None and result.get("instance_id") != expected_instance:
        raise SessionProofError("proof instance_id 与目标连接不一致")
    expected_root = kwargs.get("expected_root")
    if expected_root is not None and result.get("root") != str(Path(expected_root).resolve()):
        raise SessionProofError("proof root 与目标项目不一致")
    return result


def read_proof(path: Path, *, expected_root: Path | None = None, expected_role: str | None = None,
               expected_instance_id: str | None = None, now: float | None = None) -> dict[str, object]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise SessionProofError(f"proof 不是普通文件: {path}")
    try:
        if path.stat().st_size > MAX_PROOF_BYTES:
            raise SessionProofError("proof 超过大小限制")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except SessionProofError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SessionProofError(f"proof 无法解析: {exc}") from exc
    return validate_proof(
        payload, expected_root=expected_root, expected_role=expected_role,
        expected_instance_id=expected_instance_id, now=now,
    )


def write_proof(state_root: Path, payload: Mapping[str, object], *, instance: str | None = None,
                now: float | None = None) -> Path:
    """Atomically publish a proof under its own instance directory."""
    data = dict(payload)
    if instance is not None:
        data["instance_id"] = instance
    if "schema_version" not in data:
        data["schema_version"] = SCHEMA_VERSION
    if "written_at" not in data and "proof_written_at" not in data:
        data["written_at"] = float(time.time() if now is None else now)
    normalized = normalize_proof(data, now=now)
    path = proof_path(Path(state_root), str(normalized["instance_id"]))
    if path.is_file() and not path.is_symlink():
        try:
            previous = read_proof(path, expected_instance_id=str(normalized["instance_id"]), now=now)
            if previous.get("phase") == PHASE_CLOSED and normalized.get("phase") != PHASE_CLOSED:
                raise SessionProofError("CLOSED proof 不能重新激活")
        except SessionProofError:
            raise
    _atomic_bytes(path, _canonical(normalized))
    return path


def update_proof(state_root: Path, instance: str, *, now: float | None = None, **changes: object) -> Path:
    """Update one connection while preserving the three independent clocks."""
    path = proof_path(Path(state_root), instance)
    current = read_proof(path, expected_instance_id=instance, now=now)
    if current.get("phase") == PHASE_CLOSED:
        raise SessionProofError("CLOSED proof 不能接受新的请求/进度")
    for name, value in changes.items():
        if name in {"written_at", "proof_written_at"}:
            raise SessionProofError("written_at 只能由 proof 发布器更新")
        current[name] = value
    current["written_at"] = float(time.time() if now is None else now)
    return write_proof(state_root, current, instance=instance, now=now)


def request_retirement(state_root: Path, *, instance: str, root: str | Path,
                       scope: str | None, scope_generation: int, role: str,
                       older_than: float, request_id: str | None = None,
                       now: float | None = None) -> Path:
    """Publish a target-bound cooperative retirement request."""
    if role not in VALID_ROLES:
        raise SessionProofError("role 无效")
    request = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id or str(uuid.uuid4()),
        "instance_id": instance,
        "root": str(Path(root).resolve()),
        "scope": scope,
        "scope_generation": _integer(scope_generation, "scope_generation"),
        "role": role,
        "older_than": _finite_timestamp(older_than, "older_than"),
        "requested_at": float(time.time() if now is None else now),
    }
    _text(request["request_id"], "request_id", max_length=128)
    path = retire_request_path(Path(state_root), instance)
    _atomic_bytes(path, _canonical(request))
    return path


def read_retirement_request(path: Path, *, expected_root: Path | None = None,
                            expected_instance_id: str | None = None,
                            expected_scope_generation: int | None = None,
                            now: float | None = None) -> dict[str, object]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RetirementUnknown(f"retire request 不是普通文件: {path}")
    try:
        if path.stat().st_size > MAX_REQUEST_BYTES:
            raise RetirementUnknown("retire request 超过大小限制")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except RetirementUnknown:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RetirementUnknown(f"retire request 无法解析: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RetirementUnknown("retire request schema 不支持")
    instance = _text(payload.get("instance_id"), "instance_id", max_length=64)
    if expected_instance_id is not None and instance != expected_instance_id:
        raise RetirementUnknown("retire request instance 不匹配")
    if expected_root is not None and payload.get("root") != str(Path(expected_root).resolve()):
        raise RetirementUnknown("retire request root 不匹配")
    if expected_scope_generation is not None and payload.get("scope_generation") != expected_scope_generation:
        raise RetirementUnknown("retire request scope generation 不匹配")
    _text(payload.get("request_id"), "request_id", max_length=128)
    _text(payload.get("role"), "role", max_length=16)
    _integer(payload.get("scope_generation"), "scope_generation")
    _finite_timestamp(payload.get("older_than"), "older_than")
    _finite_timestamp(payload.get("requested_at"), "requested_at")
    current = float(time.time() if now is None else now)
    if float(payload["requested_at"]) > current + 300:
        raise RetirementUnknown("retire request 位于未来")
    return dict(payload)


def set_phase(state_root: Path, instance: str, phase: str, *, now: float | None = None) -> Path:
    """Durably move one instance through ACTIVE -> RETIRING -> CLOSED."""
    if phase not in VALID_PHASES:
        raise SessionProofError("phase 无效")
    current = read_proof(proof_path(Path(state_root), instance), expected_instance_id=instance, now=now)
    previous = str(current.get("phase", PHASE_ACTIVE))
    if previous == PHASE_CLOSED and phase != PHASE_CLOSED:
        raise SessionProofError("CLOSED proof 不能重新激活")
    if previous == PHASE_RETIRING and phase == PHASE_ACTIVE:
        raise SessionProofError("RETIRING proof 不能重新激活")
    if previous == PHASE_ACTIVE and phase == PHASE_CLOSED:
        raise SessionProofError("ACTIVE 必须先进入 RETIRING")
    current["phase"] = phase
    return write_proof(state_root, current, instance=instance, now=now)


def retire_target(state_root: Path, instance: str, *, has_inflight: bool = False,
                  has_bound_work: bool = False, now: float | None = None) -> dict[str, object]:
    """Apply a validated request at the target; never signal a bare PID."""
    proof = read_proof(proof_path(Path(state_root), instance), expected_instance_id=instance, now=now)
    request = read_retirement_request(
        retire_request_path(Path(state_root), instance),
        expected_root=Path(str(proof["root"])),
        expected_instance_id=instance,
        expected_scope_generation=int(proof.get("scope_generation", 0)),
        now=now,
    )
    if request.get("role") != proof.get("role"):
        raise RetirementUnknown("retire request role 与 proof 不一致")
    last_request = proof.get("last_request_at")
    if isinstance(last_request, (int, float)) and last_request > float(request["older_than"]):
        return {"status": "BUSY", "instance_id": instance, "phase": proof.get("phase", PHASE_ACTIVE)}
    if proof.get("phase") == PHASE_CLOSED:
        return {"status": "EXITED", "instance_id": instance, "phase": PHASE_CLOSED}
    if has_inflight or has_bound_work:
        return {"status": "BUSY", "instance_id": instance, "phase": proof.get("phase", PHASE_ACTIVE)}
    if proof.get("phase") == PHASE_ACTIVE:
        set_phase(state_root, instance, PHASE_RETIRING, now=now)
    set_phase(state_root, instance, PHASE_CLOSED, now=now)
    return {"status": "CLOSED", "instance_id": instance, "phase": PHASE_CLOSED}


def proof_digest(payload: Mapping[str, object]) -> str:
    """Return a stable digest for audit receipts, excluding volatile ages."""
    body = {key: value for key, value in payload.items() if key not in {"idle_age", "progress_age"}}
    return hashlib.sha256(_canonical(body)).hexdigest()


__all__ = [
    "MAX_PROOF_BYTES", "MAX_REQUEST_BYTES", "PHASE_ACTIVE", "PHASE_RETIRING", "PHASE_CLOSED",
    "SCHEMA_VERSION", "SessionProofError", "RetirementBusy", "RetirementUnknown",
    "new_instance_id", "instance_id", "process_start_token", "instance_root", "proof_path",
    "retire_request_path", "normalize_proof", "validate_proof", "read_proof", "write_proof",
    "update_proof", "request_retirement", "read_retirement_request", "set_phase", "retire_target",
    "proof_digest",
]
