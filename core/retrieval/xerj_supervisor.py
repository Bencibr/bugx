"""Lifecycle management for a local, already-installed Xerj binary.

The supervisor never downloads or installs Xerj.  ``BUGX_XERJ_BIN`` provides
an explicit offline binary path; otherwise an existing ``xerj`` executable
on ``PATH`` may be used.  Only the Popen handle created here is stopped.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .xerj_bridge import XerjBridge


XERJ_BINARY_ENV = "BUGX_XERJ_BIN"
DEFAULT_STARTUP_TIMEOUT = 5.0
DEFAULT_STOP_TIMEOUT = 2.0


class XerjSupervisor:
    """Start and stop one locally owned Xerj process.

    A free loopback ES-compatible port is selected by default, avoiding an
    implicit attachment to another service already listening on port 9200.
    An explicit ``port=9200`` remains available to callers that manage that
    port themselves.  A failed optional startup returns ``False`` and leaves
    the attached bridge able to record a native fallback; ``required=True``
    affects bridge queries and makes them fail closed.
    """

    def __init__(
        self,
        *,
        binary_path: Optional[os.PathLike[str] | str] = None,
        data_dir: Optional[os.PathLike[str] | str] = None,
        host: str = "127.0.0.1",
        port: Optional[int] = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        poll_interval: float = 0.05,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        required: bool = False,
        fallback: Any = None,
        circuit_failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 60.0,
    ) -> None:
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("managed Xerj must bind to a loopback host")
        if port is not None and (isinstance(port, bool) or not 1 <= port <= 65535):
            raise ValueError("port must be between 1 and 65535")
        for name, value in (
            ("startup_timeout", startup_timeout),
            ("poll_interval", poll_interval),
            ("stop_timeout", stop_timeout),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError("%s must be non-negative" % name)
        if poll_interval == 0:
            raise ValueError("poll_interval must be positive")

        self.binary_path = Path(binary_path).expanduser() if binary_path is not None else None
        self._explicit_data_dir = Path(data_dir).expanduser() if data_dir is not None else None
        self._host = "127.0.0.1" if host == "localhost" else host
        self._port = port if port is not None else self._free_loopback_port(self._host)
        self.startup_timeout = float(startup_timeout)
        self.poll_interval = float(poll_interval)
        self.stop_timeout = float(stop_timeout)
        self.required = bool(required)
        self.fallback = fallback
        self.circuit_failure_threshold = int(circuit_failure_threshold)
        self.circuit_cooldown_seconds = float(circuit_cooldown_seconds)

        self.state = "stopped"
        self.last_error: Optional[str] = None
        self._resolved_binary: Optional[Path] = None
        self._process: Optional[subprocess.Popen[Any]] = None
        self._temp_root: Optional[tempfile.TemporaryDirectory[str]] = None
        self._data_dir: Optional[Path] = self._explicit_data_dir
        self._config_path: Optional[Path] = None
        self._bridge = self._new_bridge()

    @property
    def is_circuit_open(self) -> bool:
        return self._bridge.is_circuit_open

    @property
    def circuit_state(self) -> str:
        return self._bridge.circuit_state

    @property
    def binary(self) -> Optional[Path]:
        """The executable selected for the most recent start attempt."""
        return self._resolved_binary

    @property
    def endpoint(self) -> str:
        host = "[%s]" % self._host if ":" in self._host else self._host
        return "http://%s:%d" % (host, self._port)

    @property
    def bridge(self) -> XerjBridge:
        """The bridge configured for this supervisor's loopback endpoint."""
        return self._bridge

    @property
    def process(self) -> Optional[subprocess.Popen[Any]]:
        """Return the owned process handle, if a start attempt created one."""
        return self._process

    @property
    def data_dir(self) -> Optional[Path]:
        return self._data_dir

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        """Start Xerj and wait for one identity-checked readiness response."""
        if self._process is not None:
            if self.is_running:
                if self._bridge.health_probe():
                    self.state = "ready"
                    return True
                self.last_error = "owned Xerj process is running but failed its health probe"
                if not self.stop():
                    self.state = "recovery_required"
                    return False
                self.state = "unavailable"
                return False
            if not self._reap_exited_process():
                self.last_error = "previous Xerj process could not be reaped"
                self.state = "recovery_required"
                return False
            self._cleanup_runtime_files()

        self._reset_bridge()
        if self._bridge.is_circuit_open:
            self.last_error = "circuit breaker is open; skipping Xerj startup to use native fallback"
            self.state = "unavailable"
            return False
        self.state = "starting"
        self.last_error = None
        try:
            binary = self._resolve_binary()
        except OSError as exc:
            self.last_error = str(exc)
            self.state = "unavailable"
            return False
        if binary is None:
            self.last_error = "no Xerj binary found; set BUGX_XERJ_BIN for offline use"
            self.state = "unavailable"
            return False

        try:
            self._prepare_runtime_files()
            command = [str(binary), "--insecure", "--config", str(self._config_path)]
            self._process = subprocess.Popen(
                command,
                cwd=str(self._data_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            self.last_error = "could not start Xerj: %s" % exc
            self.state = "unavailable"
            self._cleanup_runtime_files()
            return False

        deadline = time.monotonic() + self.startup_timeout
        while True:
            process = self._process
            if process is None:
                self.last_error = "Xerj process handle was lost during startup"
                self.state = "recovery_required"
                return False
            return_code = process.poll()
            if return_code is not None:
                self.last_error = "Xerj exited during startup with status %s" % return_code
                if self._reap_exited_process():
                    self.state = "unavailable"
                    self._cleanup_runtime_files()
                else:
                    self.state = "recovery_required"
                return False

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._bridge.health_probe(timeout=min(remaining, 0.5)):
                self.state = "ready"
                return True
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

        self.last_error = "Xerj did not pass its health probe before startup timeout"
        stopped = self.stop()
        if not stopped:
            self.state = "recovery_required"
            return False
        self.state = "unavailable"
        return False

    def stop(self) -> bool:
        """Stop and reap only the process created by this supervisor."""
        process = self._process
        if process is None:
            self._bridge.close()
            self._cleanup_runtime_files()
            self.state = "stopped"
            return True

        self.state = "stopping"
        try:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    # The child can exit between poll() and terminate().
                    pass
                try:
                    process.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        # It can exit naturally between timeout and kill.
                        pass
                    process.wait()
            else:
                # ``poll`` observes exit but wait reaps the child on platforms
                # where the process handle still needs collection.
                process.wait(timeout=0)
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = "could not stop/reap Xerj: %s" % exc
            self.state = "recovery_required"
            return False

        self._process = None
        self._bridge.close()
        self._cleanup_runtime_files()
        self.state = "stopped"
        return True

    close = stop

    def __enter__(self) -> "XerjSupervisor":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    @staticmethod
    def _free_loopback_port(host: str) -> int:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    def _new_bridge(self) -> XerjBridge:
        return XerjBridge(
            self.endpoint,
            required=self.required,
            fallback=self.fallback,
            circuit_failure_threshold=self.circuit_failure_threshold,
            circuit_cooldown_seconds=self.circuit_cooldown_seconds,
        )

    def _reset_bridge(self) -> None:
        if getattr(self._bridge, "_closed", False):
            self._bridge = self._new_bridge()

    def _resolve_binary(self) -> Optional[Path]:
        configured: Optional[str]
        if self.binary_path is not None:
            configured = str(self.binary_path)
        elif XERJ_BINARY_ENV in os.environ:
            configured = os.environ[XERJ_BINARY_ENV]
            if not configured.strip():
                raise FileNotFoundError("BUGX_XERJ_BIN is set but empty")
        else:
            configured = shutil.which("xerj")
            if configured is None:
                return None

        binary = Path(configured).expanduser().resolve()
        if not binary.is_file():
            raise FileNotFoundError("Xerj binary does not exist: %s" % binary)
        if not os.access(str(binary), os.X_OK):
            raise PermissionError("Xerj binary is not executable: %s" % binary)
        self._resolved_binary = binary
        return binary

    def _prepare_runtime_files(self) -> None:
        if self._temp_root is not None:
            self._temp_root.cleanup()
            self._temp_root = None

        self._temp_root = tempfile.TemporaryDirectory(prefix="bugx-xerj-")
        root = Path(self._temp_root.name)
        if self._explicit_data_dir is None:
            self._data_dir = root / "data"
        else:
            self._data_dir = self._explicit_data_dir.resolve()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = root / "xerj.toml"

        # Xerj reads its listener ports from this config.  Reserve the ES
        # compatibility port selected by the supervisor and give the other
        # local listeners their own free ports as well.
        rest_port = self._free_loopback_port(self._host)
        grpc_port = self._free_loopback_port(self._host)
        config = "\n".join(
            (
                "[server]",
                "data_dir = %s" % json.dumps(str(self._data_dir)),
                "rest_port = %d" % rest_port,
                "grpc_port = %d" % grpc_port,
                "es_compat_port = %d" % self._port,
                "",
            )
        )
        self._config_path.write_text(config, encoding="utf-8")

    def _cleanup_runtime_files(self) -> None:
        if self._temp_root is not None:
            self._temp_root.cleanup()
            self._temp_root = None
        self._config_path = None
        if self._explicit_data_dir is None:
            self._data_dir = None

    def _reap_exited_process(self) -> bool:
        process = self._process
        if process is not None:
            try:
                process.wait(timeout=0)
            except (OSError, subprocess.SubprocessError):
                self.state = "recovery_required"
                return False
        self._process = None
        self._bridge.close()
        self._bridge = self._new_bridge()
        return True


__all__ = ["DEFAULT_STARTUP_TIMEOUT", "XERJ_BINARY_ENV", "XerjSupervisor"]
