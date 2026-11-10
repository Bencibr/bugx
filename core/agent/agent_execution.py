"""Controlled execution of target-project CheckPlans.

Plans are data, never shell strings.  The executor resolves a working-copy ID
through an explicit registry, applies a small environment allowlist, captures
bounded output, and reaps the complete POSIX process group before returning.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


DEFAULT_OUTPUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_ENV_ALLOWLIST = frozenset({
    "HOME", "LANG", "LC_ALL", "NODE_PATH", "PATH", "PYTHONPATH",
    "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "VIRTUAL_ENV", "WINDIR",
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHELL_NAMES = frozenset({
    "ash", "bash", "cmd", "csh", "dash", "fish", "ksh", "powershell",
    "pwsh", "sh", "tcsh", "zsh",
})
_TERM_GRACE_SECONDS = 0.25
_KILL_VERIFY_SECONDS = 1.0


class CheckPlanError(ValueError):
    """A CheckPlan violates the executor contract."""


class ExecutionError(RuntimeError):
    """A CheckPlan could not be launched or observed reliably."""


class ExecutionOutcomeUnknown(ExecutionError):
    """Process containment or reaping could not be confirmed."""

    def __init__(self, message: str, observation: "ExecutionObservation") -> None:
        super().__init__(message)
        self.observation = observation


@dataclass(frozen=True, slots=True)
class TargetCallTarget:
    """A source module expected to appear in a real runtime stack frame."""

    module_path: str
    function_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckPlan:
    """Validated, shell-free process invocation for one target work copy."""

    plan_id: str
    argv: tuple[str, ...]
    workcopy_id: str
    cwd: str = "."
    env_allowlist: tuple[str, ...] = tuple(sorted(DEFAULT_ENV_ALLOWLIST))
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES
    target_call_targets: tuple[TargetCallTarget, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["argv"] = list(self.argv)
        value["env_allowlist"] = list(self.env_allowlist)
        value["target_call_targets"] = [asdict(item) for item in self.target_call_targets]
        value["env"] = dict(self.env)
        return value


@dataclass(frozen=True, slots=True)
class TargetCallEvidence:
    """Evidence from an interpreter-generated traceback/runner stack frame."""

    target_path: str
    frame_path: str
    function_name: str | None
    frame: str
    stream: str


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """Raw process result plus bounded output summaries and lifecycle flags."""

    plan_id: str
    workcopy_id: str
    argv: tuple[str, ...]
    exit_code: int | None
    stdout_summary: str
    stderr_summary: str
    stdout_bytes_captured: int
    stderr_bytes_captured: int
    stdout_bytes_observed: int
    stderr_bytes_observed: int
    started_at: str
    finished_at: str
    timed_out: bool = False
    output_limit: bool = False
    cancelled: bool = False
    outcome_unknown: bool = False
    target_call_evidence: tuple[TargetCallEvidence, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["argv"] = list(self.argv)
        value["target_call_evidence"] = [asdict(item) for item in self.target_call_evidence]
        return value


class WindowsJobObjectBackend:
    """Reserved Windows containment boundary.

    A Windows implementation must create/assign a Job Object with kill-on-close
    semantics before process launch.  Until such a backend exists, execution is
    rejected before starting a process instead of falling back to parent-only
    termination.
    """

    @staticmethod
    def ensure_available() -> None:  # pragma: no cover - Windows-only boundary
        raise ExecutionError("Windows Job Object process containment is not implemented")


def validate_check_plan(
    plan: CheckPlan,
    *,
    allowed_env_keys: Sequence[str] = tuple(DEFAULT_ENV_ALLOWLIST),
) -> None:
    """Validate every field before resolving or launching a command."""
    if not isinstance(plan, CheckPlan):
        raise CheckPlanError("executor accepts only a CheckPlan instance")
    if not isinstance(plan.plan_id, str) or not _SAFE_ID.fullmatch(plan.plan_id):
        raise CheckPlanError("plan_id is invalid")
    if not isinstance(plan.workcopy_id, str) or not _SAFE_ID.fullmatch(plan.workcopy_id):
        raise CheckPlanError("workcopy_id is invalid")
    if isinstance(plan.argv, (str, bytes)) or not isinstance(plan.argv, (tuple, list)):
        raise CheckPlanError("argv must be a non-empty sequence of strings, never a command string")
    if not plan.argv or len(plan.argv) > 256:
        raise CheckPlanError("argv must contain between 1 and 256 arguments")
    if any(not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 32768 for arg in plan.argv):
        raise CheckPlanError("argv contains an empty, non-string, NUL, or oversized argument")
    executable = Path(plan.argv[0]).name.casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    if executable in _SHELL_NAMES:
        raise CheckPlanError("shell interpreters are not accepted as CheckPlan executables")
    if executable == "env" and any(Path(item).name.casefold() in _SHELL_NAMES for item in plan.argv[1:]):
        raise CheckPlanError("env may not be used to launch a shell interpreter")

    if not isinstance(plan.cwd, str) or not plan.cwd or "\\" in plan.cwd or "\x00" in plan.cwd:
        raise CheckPlanError("cwd must be a POSIX-style working-copy-relative path")
    relative_cwd = PurePosixPath(plan.cwd)
    if relative_cwd.is_absolute() or any(part in {"..", ""} for part in relative_cwd.parts):
        raise CheckPlanError("cwd must not escape the working copy")
    if not isinstance(plan.env_allowlist, (tuple, list)):
        raise CheckPlanError("env_allowlist must be a sequence of names")
    globally_allowed = set(allowed_env_keys)
    for key in plan.env_allowlist:
        if not isinstance(key, str) or not _SAFE_ENV_KEY.fullmatch(key) or key not in globally_allowed:
            raise CheckPlanError(f"environment key is not allowlisted: {key!r}")
    if not isinstance(plan.env, Mapping):
        raise CheckPlanError("env must be a mapping")
    for key, value in plan.env.items():
        if key not in plan.env_allowlist:
            raise CheckPlanError(f"environment value is not permitted by the plan: {key!r}")
        if not isinstance(value, str) or "\x00" in value or len(value) > 65536:
            raise CheckPlanError(f"environment value is invalid: {key!r}")
    if sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in plan.env.items()) > 1024 * 1024:
        raise CheckPlanError("environment values exceed the supported aggregate size")
    if isinstance(plan.timeout_seconds, bool) or not isinstance(plan.timeout_seconds, (int, float)):
        raise CheckPlanError("timeout_seconds must be a positive number")
    if not 0 < float(plan.timeout_seconds) <= 24 * 60 * 60:
        raise CheckPlanError("timeout_seconds is outside the supported range")
    if isinstance(plan.output_limit_bytes, bool) or not isinstance(plan.output_limit_bytes, int):
        raise CheckPlanError("output_limit_bytes must be a positive integer")
    if not 1 <= plan.output_limit_bytes <= 64 * 1024 * 1024:
        raise CheckPlanError("output_limit_bytes is outside the supported range")
    if not isinstance(plan.target_call_targets, (tuple, list)):
        raise CheckPlanError("target_call_targets must be a sequence")
    for target in plan.target_call_targets:
        if not isinstance(target, TargetCallTarget) or not _valid_target_path(target.module_path):
            raise CheckPlanError("target call evidence target is invalid")
        if isinstance(target.function_names, (str, bytes)) or not isinstance(target.function_names, (tuple, list)):
            raise CheckPlanError("target function names must be a sequence")
        if any(not isinstance(name, str) or not name for name in target.function_names):
            raise CheckPlanError("target function names must be non-empty strings")


def extract_target_call_evidence(
    stdout: str,
    stderr: str,
    targets: Sequence[TargetCallTarget],
) -> tuple[TargetCallEvidence, ...]:
    """Accept target evidence only from recognizable runtime stack sections.

    A printed word such as ``BUG``, an exit code, or a bare path is not a call
    trace.  Python frames must be inside a traceback block; Node frames must be
    in a TAP failure stack or a V8 error stack.
    """
    found: list[TargetCallEvidence] = []
    for stream_name, output in (("stdout", stdout), ("stderr", stderr)):
        lines = output.splitlines()
        in_python_traceback = False
        in_node_stack = False
        for line in lines:
            stripped = line.strip()
            if stripped == "Traceback (most recent call last):":
                in_python_traceback = True
                in_node_stack = False
                continue
            if stripped.startswith("not ok ") or stripped == "stack: |-" or stripped.startswith("Error:") or "AssertionError" in stripped:
                in_node_stack = True
            py_match = re.search(r'File ["\'](.+?)["\'], line \d+, in ([^\s]+)', line) if in_python_traceback else None
            node_match = re.search(r"\bat\s+(.+?)\s+\((.+?):\d+:\d+\)$|\bat\s+(.+?):\d+:\d+$", stripped) if in_node_stack else None
            if py_match:
                frame_path, function_name = py_match.group(1), py_match.group(2)
                evidence = _match_target_frame(frame_path, function_name, line, stream_name, targets)
                if evidence:
                    found.append(evidence)
            elif node_match:
                function_name = node_match.group(1) or None
                frame_path = node_match.group(2) or node_match.group(3)
                evidence = _match_target_frame(frame_path, function_name, line, stream_name, targets)
                if evidence:
                    found.append(evidence)
            if in_python_traceback and stripped and not (stripped.startswith("File ") or stripped.startswith("During handling") or stripped.startswith("The above exception")):
                if not line.startswith((" ", "\t")) and not stripped.startswith("Traceback"):
                    in_python_traceback = False
    unique: dict[tuple[str, str, str | None], TargetCallEvidence] = {}
    for item in found:
        unique.setdefault((item.target_path, item.frame_path, item.function_name), item)
    return tuple(unique.values())


def execute_check_plan(
    plan: CheckPlan,
    workcopies: Mapping[str, str | os.PathLike[str]],
    *,
    cancel_event: threading.Event | None = None,
    allowed_env_keys: Sequence[str] = tuple(DEFAULT_ENV_ALLOWLIST),
) -> ExecutionObservation:
    """Execute one validated CheckPlan without shell interpretation."""
    validate_check_plan(plan, allowed_env_keys=allowed_env_keys)
    if os.name == "nt":  # pragma: no cover - Windows-only boundary
        WindowsJobObjectBackend.ensure_available()
    if os.name != "posix":
        raise ExecutionError("process-tree containment is available only on POSIX or a Job Object backend")
    if plan.workcopy_id not in workcopies:
        raise CheckPlanError(f"unknown workcopy_id: {plan.workcopy_id}")
    root_value = workcopies[plan.workcopy_id]
    try:
        root = Path(root_value).resolve(strict=True)
        cwd = (root / Path(*PurePosixPath(plan.cwd).parts)).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise CheckPlanError("working copy or cwd cannot be resolved") from exc
    if not root.is_dir() or not cwd.is_dir():
        raise CheckPlanError("working copy and cwd must be directories")
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise CheckPlanError("cwd resolves outside the working copy") from exc

    process_env = {
        key: value for key, value in os.environ.items()
        if key in plan.env_allowlist and key in set(allowed_env_keys)
    }
    process_env.update(dict(plan.env))
    capture = _BoundedCapture(plan.output_limit_bytes)
    started_at = _timestamp()
    start_clock = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    threads: list[threading.Thread] = []
    timed_out = False
    output_limit = False
    cancelled = False
    cleanup_error: str | None = None
    try:
        process = subprocess.Popen(
            list(plan.argv),
            cwd=str(cwd),
            env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise ExecutionError(f"CheckPlan process could not be started: {exc}") from exc

    assert process.stdout is not None and process.stderr is not None
    for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
        thread = threading.Thread(target=capture.drain, args=(name, pipe), daemon=True)
        thread.start()
        threads.append(thread)
    deadline = start_clock + float(plan.timeout_seconds)
    while process.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        if capture.exceeded:
            output_limit = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.01)
    # Cleanup also runs after a normal parent exit: descendants are not allowed
    # to outlive the CheckPlan or keep captured pipes open.
    try:
        _terminate_and_reap_group(process)
    except (ExecutionError, OSError) as exc:
        cleanup_error = str(exc)
    for thread in threads:
        thread.join(timeout=1.5)
    if any(thread.is_alive() for thread in threads):
        cleanup_error = cleanup_error or "output capture threads did not finish after process-group cleanup"
    for pipe in (process.stdout, process.stderr):
        try:
            pipe.close()
        except OSError:
            pass

    stdout_raw, stderr_raw, stdout_seen, stderr_seen, exceeded = capture.snapshot()
    output_limit = output_limit or exceeded
    finished_at = _timestamp()
    exit_code = process.returncode
    evidence = extract_target_call_evidence(
        stdout_raw.decode("utf-8", errors="replace"),
        stderr_raw.decode("utf-8", errors="replace"),
        plan.target_call_targets,
    )
    observation = ExecutionObservation(
        plan_id=plan.plan_id,
        workcopy_id=plan.workcopy_id,
        argv=tuple(plan.argv),
        exit_code=exit_code,
        stdout_summary=stdout_raw.decode("utf-8", errors="replace"),
        stderr_summary=stderr_raw.decode("utf-8", errors="replace"),
        stdout_bytes_captured=len(stdout_raw),
        stderr_bytes_captured=len(stderr_raw),
        stdout_bytes_observed=stdout_seen,
        stderr_bytes_observed=stderr_seen,
        started_at=started_at,
        finished_at=finished_at,
        timed_out=timed_out,
        output_limit=output_limit,
        cancelled=cancelled,
        outcome_unknown=cleanup_error is not None or exit_code is None,
        target_call_evidence=evidence,
    )
    if observation.outcome_unknown:
        raise ExecutionOutcomeUnknown(cleanup_error or "process exit status is unknown", observation)
    return observation


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self._seen = {"stdout": 0, "stderr": 0}
        self._stored = 0
        self.exceeded = False

    def drain(self, stream: str, pipe: object) -> None:
        reader = pipe
        try:
            while True:
                chunk = reader.read(8192)  # type: ignore[attr-defined]
                if not chunk:
                    return
                with self._lock:
                    self._seen[stream] += len(chunk)
                    available = max(0, self.limit - self._stored)
                    if available:
                        kept = chunk[:available]
                        self._buffers[stream].extend(kept)
                        self._stored += len(kept)
                    if len(chunk) > available:
                        self.exceeded = True
        except (OSError, ValueError):
            return

    def snapshot(self) -> tuple[bytes, bytes, int, int, bool]:
        with self._lock:
            return (
                bytes(self._buffers["stdout"]),
                bytes(self._buffers["stderr"]),
                self._seen["stdout"],
                self._seen["stderr"],
                self.exceeded,
            )


def _terminate_and_reap_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate descendants before reaping the leader, then verify cleanup."""
    pgid = process.pid
    # Signal before poll()/wait(): those calls reap an exited leader and release
    # its PID. Keeping the leader unreaped until this first group signal avoids
    # a fast PID reuse racing cleanup after a short-lived command.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError as exc:
        raise ExecutionOutcomeUnknown(
            f"could not signal process group {pgid}: {exc}", _unknown_observation(process)
        ) from exc

    # Do not reap the leader before the last group signal.  Its PID is also the
    # process-group ID; after wait() another process could reuse that ID and a
    # late killpg(SIGKILL) would target an unrelated group.
    if not _wait_group_gone(pgid, _TERM_GRACE_SECONDS):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            # On POSIX hosts a terminated group leader may remain as a zombie
            # until its parent reaps it. Some kernels report EPERM for both
            # killpg(0) and SIGKILL while that zombie is the only group member.
            # Reap the leader, then only observe the numeric PGID: never signal
            # it again after reaping, since the identifier could be reused.
            try:
                process.wait(timeout=_KILL_VERIFY_SECONDS)
            except subprocess.TimeoutExpired as wait_exc:
                raise ExecutionOutcomeUnknown(
                    f"process leader {process.pid} could not be reaped",
                    _unknown_observation(process),
                ) from wait_exc
            if not _wait_group_gone(pgid, _KILL_VERIFY_SECONDS):
                raise ExecutionOutcomeUnknown(
                    f"process group {pgid} remains after SIGKILL was denied: {exc}",
                    _unknown_observation(process),
                ) from exc
            return
        except OSError as exc:
            raise ExecutionOutcomeUnknown(
                f"could not force-kill process group {pgid}: {exc}",
                _unknown_observation(process),
            ) from exc
    try:
        process.wait(timeout=_KILL_VERIFY_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ExecutionOutcomeUnknown(
            f"process leader {process.pid} could not be reaped",
            _unknown_observation(process),
        ) from exc
    if not _wait_group_gone(pgid, _KILL_VERIFY_SECONDS):
        raise ExecutionOutcomeUnknown(
            f"process group {pgid} remains after termination",
            _unknown_observation(process),
        )


def _wait_group_gone(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
            continue
        except OSError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _unknown_observation(process: subprocess.Popen[bytes]) -> ExecutionObservation:
    now = _timestamp()
    return ExecutionObservation(
        plan_id="unknown",
        workcopy_id="unknown",
        argv=(),
        exit_code=process.returncode,
        stdout_summary="",
        stderr_summary="",
        stdout_bytes_captured=0,
        stderr_bytes_captured=0,
        stdout_bytes_observed=0,
        stderr_bytes_observed=0,
        started_at=now,
        finished_at=now,
        outcome_unknown=True,
    )


def _match_target_frame(
    frame_path: str,
    function_name: str | None,
    frame: str,
    stream: str,
    targets: Sequence[TargetCallTarget],
) -> TargetCallEvidence | None:
    normalized_frame = frame_path.replace("\\", "/")
    for target in targets:
        suffix = target.module_path.replace("\\", "/").lstrip("./")
        if suffix.endswith(".py") or re.search(r"\.(?:[cm]?js|tsx?)$", suffix):
            expected = suffix
        elif "." in suffix and "/" not in suffix:
            expected = suffix.replace(".", "/") + ".py"
        else:
            expected = suffix
        if not normalized_frame.endswith(expected):
            continue
        if target.function_names and function_name not in target.function_names:
            continue
        return TargetCallEvidence(
            target_path=target.module_path,
            frame_path=normalized_frame,
            function_name=function_name,
            frame=frame,
            stream=stream,
        )
    return None


def _valid_target_path(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value and not PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "CheckPlan", "CheckPlanError", "DEFAULT_ENV_ALLOWLIST", "DEFAULT_OUTPUT_LIMIT_BYTES",
    "ExecutionError", "ExecutionObservation", "ExecutionOutcomeUnknown", "TargetCallEvidence",
    "TargetCallTarget", "WindowsJobObjectBackend", "execute_check_plan",
    "extract_target_call_evidence", "validate_check_plan",
]
