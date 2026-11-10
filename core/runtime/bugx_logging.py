#!/usr/bin/env python3
"""BugX 统一诊断日志。

日志是排查安装、宿主调用和运行异常的辅助记录，不替代生命周期事件、结论或
证据文件。已初始化的项目写入项目 ``.bugx/logs/bugx.jsonl``；尚未初始化的
目录回退到用户级 ``~/.bugx/logs/projects/<project_id>/bugx.jsonl``。

回退是必需的：``.bugx/logs/`` 由项目布局初始化器创建（DOC-001 §5.8），而
诊断日志是**每个命令**（包括 ``doctor``/``hosts`` 这类纯只读命令）都会产生
的副作用。若由日志引导来创建 ``<root>/.bugx``，任何在普通目录里的读操作都会
凭空造出一个"有 .bugx 但没有 manifest"的半初始化状态，并把该目录的
``git status`` 弄脏（BugX 不管理 ``.gitignore``）。采用标准库 logging，支持
JSONL、等级过滤、有限轮转和敏感字段脱敏。
"""

from __future__ import annotations

import contextvars
import datetime as dt
from contextlib import contextmanager
import hashlib
import json
import logging
import time
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl.
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX has no msvcrt.
    msvcrt = None  # type: ignore[assignment]

LOG_SCHEMA_VERSION = 2
_CORRELATION_CACHE: dict[str, Any] = {}
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "bugx.jsonl"
# 未初始化目录的日志回退根。默认 ``~/.bugx/logs``，与 ``BUGX_GLOBAL_CACHE_ROOT``
# / ``BUGX_GLOBAL_TELEMETRY_ROOT`` 同属"用户级、可被测试隔离"的一类覆盖点。
GLOBAL_LOG_ROOT_ENV = "BUGX_GLOBAL_LOG_ROOT"
GLOBAL_LOG_PROJECTS_DIR = "projects"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
LOGGER_NAME = "bugx"
_SCOPED_SYNC_EXPORT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "bugx_scoped_sync_export", default=False,
)
_CONTEXT_ENV = {
    "trace_id": "BUGX_TRACE_ID",
    "run_id": "BUGX_RUN_ID",
    "lifecycle_id": "BUGX_LIFECYCLE_ID",
    "round": "BUGX_ROUND",
    "worker_id": "BUGX_WORKER_ID",
    "finding_id": "BUGX_FINDING_ID",
    "incident_id": "BUGX_INCIDENT_ID",
}

_SECRET_KEY = re.compile(
    r"(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|cookie|authorization"
    r"|credential|private[_-]?key|auth)",
    re.IGNORECASE,
)
# LLM 阶段统计与预算字段排除在敏感鉴权 Token 脱敏之外 (DOC-042)
_METRIC_TOKEN_KEY = re.compile(
    r"(?:^|[_-])(?:tokens?_consumed|tokens?_budget|tokens?_breakdown|tokens?_count"
    r"|prompt_tokens|completion_tokens|total_tokens|diff_tokens|actual_tokens"
    r"|budget_tokens|tokens)$",
    re.IGNORECASE,
)
# 值侧匹配允许引号包裹（token="abc" / secret='xyz'），引号计入前缀组，
# 结束引号保留在输出中，脱敏后仍是合法的引用片段。
_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+|basic\s+|(?:token|password|passwd|secret|api[_-]?key|cookie"
    r"|credential|private[_-]?key|auth)\s*[=:]\s*[\"']?)[^\s,;\"']+",
)
# 无 key 上下文的超长疑似 token（JWT / base64 私钥片段等）整段脱敏。
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_-]{80,}")


def is_initialized_project(root: Path) -> bool:
    """Return whether ``root`` already carries a BugX project manifest.

    ``.bugx/logs/`` belongs to the project layout and is created by the layout
    initializer (``bugx init`` / ``bugx install``, DOC-001 §5.8).  A directory
    without ``.bugx/manifest.json`` has no project runtime to record evidence
    in, so the diagnostic logger must not be the thing that creates one.
    """
    manifest = Path(root).resolve() / ".bugx" / "manifest.json"
    return manifest.is_file() and not manifest.is_symlink()


def global_log_root() -> Path:
    """Return the per-user fallback log root without creating it."""
    override = os.environ.get(GLOBAL_LOG_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bugx" / LOG_DIR_NAME


def project_log_id(root: Path) -> str:
    """Return the stable per-project key used to isolate fallback logs.

    Mirrors ``bugx_paths._project_id`` so a fallback log can be traced back to
    its directory without reading that directory's (absent) manifest.
    """
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]


def global_log_path(root: Path) -> Path:
    """Return the fallback log path for a directory that is not a project."""
    return (
        global_log_root()
        / GLOBAL_LOG_PROJECTS_DIR
        / project_log_id(root)
        / LOG_FILE_NAME
    )


def log_path(root: Path, *, project_scoped: bool | None = None) -> Path:
    """Return the diagnostic log path without creating anything.

    Initialized projects keep the documented ``<root>/.bugx/logs/bugx.jsonl``
    location.  Anything else falls back to the per-user store so that running a
    read-only command from an arbitrary directory stays observationally
    read-only instead of manufacturing project state.
    """
    root = Path(root).resolve()
    if project_scoped is None:
        project_scoped = is_initialized_project(root)
    if project_scoped:
        return root / ".bugx" / LOG_DIR_NAME / LOG_FILE_NAME
    return global_log_path(root)


def _private_dir_chain(outer: Path, inner: Path, *, label: str) -> Path:
    """Create every level from ``outer`` down to ``inner`` as a 0700 directory.

    ``outer`` is the BugX-owned root (``<root>/.bugx`` or ``~/.bugx/logs``), so
    an intermediate ``projects/<project_id>`` step cannot be left world
    readable.  Every level is checked for symlinks first: a linked component
    could redirect the log outside the intended tree (SEC-14).
    """
    try:
        relative = inner.relative_to(outer)
    except ValueError:
        raise ValueError(f"{label}日志目录必须位于 {outer} 之内: {inner}") from None
    chain = [outer]
    for part in relative.parts:
        chain.append(chain[-1] / part)
    for candidate in chain:
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise ValueError(f"拒绝写入不安全的{label}日志目录: {candidate}")
    for candidate in chain:
        candidate.mkdir(mode=0o700, exist_ok=True)
        os.chmod(candidate, 0o700)
    return inner


DEFAULT_SLOW_THRESHOLD_SECONDS = 30.0
_TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$", re.IGNORECASE
)


def parse_traceparent(raw: str | None) -> dict[str, str] | None:
    """Parse a W3C traceparent header string into constituent fields.

    Conforms to W3C TraceContext Level 1:
    - Version 'ff' is rejected as invalid;
    - All-zero trace_id or parent_id are rejected as invalid;
    - Returns lower-case hex values if valid, else None.
    """
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    match = _TRACEPARENT_RE.match(raw)
    if not match:
        return None
    version, trace_id, parent_id, flags = match.groups()
    if version.lower() == "ff":
        return None
    if trace_id == "0" * 32:
        return None
    if parent_id == "0" * 16:
        return None
    return {
        "version": version.lower(),
        "trace_id": trace_id.lower(),
        "parent_id": parent_id.lower(),
        "trace_flags": flags.lower(),
    }


def generate_traceparent(trace_id: str | None = None, parent_id: str | None = None,
                         flags: str = "01") -> str:
    """Generate a standard W3C traceparent header string."""
    clean_trace = trace_id.lower() if trace_id and len(trace_id) == 32 else uuid.uuid4().hex
    clean_parent = parent_id.lower() if parent_id and len(parent_id) == 16 else uuid.uuid4().hex[:16]
    return f"00-{clean_trace}-{clean_parent}-{flags}"


def safe_value(value: Any, *, key: str = "") -> Any:
    if key and _SECRET_KEY.search(key) and not _METRIC_TOKEN_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): safe_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_value(item, key=key) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        redacted = _SECRET_VALUE.sub(r"\1[REDACTED]", value)
        return _LONG_TOKEN.sub("[REDACTED]", redacted)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = dt.datetime.fromtimestamp(
            record.created, tz=dt.timezone.utc,
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        payload: dict[str, Any] = {
            "schema_version": LOG_SCHEMA_VERSION,
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "bugx_event", "log"),
            "message": record.getMessage(),
            "pid": record.process,
        }
        try:
            from core.infra import build_info
            payload["build_id"] = build_info.get_build_id()
        except Exception:
            pass
        fields = getattr(record, "bugx_fields", {})
        if isinstance(fields, dict):
            payload.update(safe_value(fields))
        if record.exc_info:
            payload["exception"] = safe_value(self.formatException(record.exc_info))
        return json.dumps(safe_value(payload), ensure_ascii=False, separators=(",", ":"))


class _OtlpHttpHandler(logging.Handler):
    """Asynchronous HTTP exporter to push spans to OTLP endpoint (e.g. Arize Phoenix / Langfuse)."""

    def __init__(self, endpoint: str) -> None:
        super().__init__()
        self.endpoint = endpoint
        import urllib.request
        self._request_mod = urllib.request

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "bugx_fields", {})
        if not isinstance(fields, dict):
            return
        trace_id = fields.get("trace_id")
        span_id = fields.get("span_id")
        if not trace_id or not span_id:
            return

        # A reusable launcher scope restores cwd/env before returning. Finish
        # exports in that scope so their proxy/config reads cannot cross the
        # restoration boundary. The existing request timeout is not a DNS or
        # total wall-clock guarantee.
        if _SCOPED_SYNC_EXPORT.get():
            self._send_otlp(record, fields, trace_id, span_id)
            return

        # 异步线程发送，绝不阻塞主控或工具调用
        import threading
        t = threading.Thread(
            target=self._send_otlp,
            args=(record, fields, trace_id, span_id),
            daemon=True,
        )
        t.start()

    def _send_otlp(self, record: logging.LogRecord, fields: dict[str, Any], trace_id: str, span_id: str) -> None:
        try:
            start_time_ns = int(record.created * 1e9)
            duration_ms = fields.get("duration_ms") or (float(fields.get("duration_seconds", 0)) * 1000)
            end_time_ns = start_time_ns + int(float(duration_ms) * 1e6)
            clean_trace = str(trace_id).lower().replace("-", "").zfill(32)[-32:]
            clean_span = str(span_id).lower().replace("-", "").zfill(16)[-16:]
            parent_id = fields.get("parent_span_id")
            clean_parent = str(parent_id).lower().replace("-", "").zfill(16)[-16:] if parent_id else ""

            attrs = []
            for k, v in fields.items():
                if k in {"trace_id", "span_id", "parent_span_id", "traceparent"}:
                    continue
                attrs.append({
                    "key": str(k),
                    "value": {"stringValue": str(safe_value(v))},
                })

            event_name = getattr(record, "bugx_event", "log")
            span = {
                "traceId": clean_trace,
                "spanId": clean_span,
                "name": f"bugx.{event_name}",
                "kind": 1,  # SPAN_KIND_INTERNAL
                "startTimeUnixNano": str(start_time_ns),
                "endTimeUnixNano": str(end_time_ns),
                "attributes": attrs,
                "status": {"code": 2 if record.levelno >= logging.ERROR else 1},
            }
            if clean_parent:
                span["parentSpanId"] = clean_parent

            otlp_payload = {
                "resourceSpans": [{
                    "resource": {
                        "attributes": [{
                            "key": "service.name",
                            "value": {"stringValue": "bugx"},
                        }],
                    },
                    "scopeSpans": [{
                        "scope": {"name": "bugx.tracer", "version": "1.0.0"},
                        "spans": [span],
                    }],
                }],
            }
            body = json.dumps(otlp_payload).encode("utf-8")
            req = self._request_mod.Request(
                self.endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            kwargs: dict[str, Any] = {"timeout": 2.0}
            if self.endpoint.startswith("https://"):
                try:
                    from core.infra.client_metadata import tls_context
                    kwargs["context"] = tls_context(base_url=self.endpoint)
                except Exception:
                    pass
            with self._request_mod.urlopen(req, **kwargs) as resp:
                pass
        except Exception:
            pass


class _SecureRotatingFileHandler(RotatingFileHandler):
    """Private rotating handler with a process-wide rollover lock."""

    def __init__(self, filename, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(filename, *args, **kwargs)
        self._bugx_lock_path = Path(f"{self.baseFilename}.lock")
        if self._bugx_lock_path.is_symlink():
            raise ValueError(f"拒绝使用符号链接日志锁: {self._bugx_lock_path}")
        deadline = time.monotonic() + 10.0
        while True:
            try:
                self._bugx_lock_path.touch(mode=0o600, exist_ok=True)
                os.chmod(self._bugx_lock_path, 0o600)
                self._bugx_lock_stream = self._bugx_lock_path.open("a+b")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    self._bugx_lock_stream = None  # degrade gracefully
                    break
                time.sleep(0.02)

    @contextmanager
    def _process_lock(self):
        if self._bugx_lock_stream is None:
            yield
            return
        if fcntl is not None:
            deadline = time.monotonic() + 10.0
            lock_mode = fcntl.LOCK_EX | getattr(fcntl, "LOCK_NB", 0)
            while True:
                try:
                    fcntl.flock(self._bugx_lock_stream.fileno(), lock_mode)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(self._bugx_lock_stream.fileno(), fcntl.LOCK_UN)
            return
        if msvcrt is not None:
            # ``LK_LOCK`` can surface a sharing violation as PermissionError
            # on Windows when another BugX process is rotating the same log.
            # Use the non-blocking primitive and own the bounded wait here so
            # a transient peer never crashes the install command.
            deadline = time.monotonic() + 10.0
            lock_mode = getattr(msvcrt, "LK_NBLCK", None)
            if lock_mode is None:  # pragma: no cover - old msvcrt fallback.
                lock_mode = msvcrt.LK_LOCK
            while True:
                self._bugx_lock_stream.seek(0)
                try:
                    msvcrt.locking(
                        self._bugx_lock_stream.fileno(), lock_mode, 1,
                    )
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                self._bugx_lock_stream.seek(0)
                msvcrt.locking(
                    self._bugx_lock_stream.fileno(), msvcrt.LK_UNLCK, 1,
                )
            return
        yield

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream

    def _write_jsonl_record(self, record: logging.LogRecord) -> str | None:
        """Write and flush one record, returning its event ID only on success.

        ``RotatingFileHandler.emit`` intentionally routes formatter and I/O
        errors through ``handleError``.  That behavior is useful for ordinary
        diagnostics, but it cannot serve as an acknowledgement for post gates:
        callers need to distinguish a flushed JSONL row from a silently dropped
        one.  Keep the handler's rollover policy and process lock, while making
        the formatter/write/flush/close result explicit.
        """
        event_fields = getattr(record, "bugx_fields", {})
        event_id = event_fields.get("event_id") if isinstance(event_fields, dict) else None

        # Closing the previous descriptor while holding the process lock keeps
        # another process from rotating a file that this process still has open.
        if self.stream is not None:
            previous_stream = self.stream
            self.stream = None
            previous_stream.close()

        try:
            if self.shouldRollover(record):
                self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            rendered = self.format(record)
            line = rendered + self.terminator
            written = self.stream.write(line)
            if written != len(line):
                raise OSError("short JSONL write")
            self.stream.flush()
            return event_id if isinstance(event_id, str) and event_id else None
        finally:
            if self.stream is not None:
                stream = self.stream
                self.stream = None
                stream.close()

    def emit(self, record: logging.LogRecord) -> None:
        # Handler.lock covers threads. The lock file also covers independent CLI,
        # MCP, and worker processes sharing one project log during rollover.
        # A successful acknowledgement is attached to this LogRecord only after
        # the exact event_id has been formatted, written, flushed, and closed.
        event_fields = getattr(record, "bugx_fields", {})
        event_id = event_fields.get("event_id") if isinstance(event_fields, dict) else None
        try:
            with self._process_lock():
                acknowledged_id = self._write_jsonl_record(record)
        except Exception:
            # Do not call logging.Handler.handleError here: it may print event
            # contents or an I/O traceback to stderr. log_event observes the
            # missing acknowledgement and returns False to its caller.
            setattr(record, "_bugx_jsonl_error_event_id", event_id)
            return
        if isinstance(event_id, str) and acknowledged_id == event_id:
            setattr(record, "_bugx_jsonl_ack_event_id", acknowledged_id)
        else:
            setattr(record, "_bugx_jsonl_error_event_id", event_id)

    def close(self) -> None:
        try:
            self._bugx_lock_stream.close()
        except AttributeError:
            pass
        super().close()


def _is_within(candidate: Path, base: Path) -> bool:
    """Return whether ``candidate`` sits under ``base`` (real-path boundary)."""
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return False
    return True


def close_handlers_for_root(root: Path) -> int:
    """Close the diagnostic handlers owned by ``root`` and return the count.

    One root can own two locations: the project log under ``<root>/.bugx`` and
    the per-user fallback used while the project is not initialized yet.
    Callers that delete or replace either tree (install rollback, reset,
    uninstall) must release these handles first: on Windows an open handle
    makes ``unlink``/``rmtree`` fail with WinError 32, masking the real error.
    POSIX is unaffected but closing early is harmless.
    """
    resolved_root = Path(root).expanduser().resolve()
    bases = (resolved_root / ".bugx", global_log_path(resolved_root).parent)
    closed = 0
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        handler_path = getattr(handler, "_bugx_log_path", None)
        if not handler_path:
            continue
        # 真实路径边界判断：字符串 startswith 会误命中 <root>/.bugx-evil 等同级前缀
        candidate = Path(str(handler_path).replace("/", os.sep)).resolve()
        if not any(_is_within(candidate, base) for base in bases):
            continue
        try:
            handler.close()
        except OSError:
            pass
        finally:
            logger.removeHandler(handler)
        closed += 1
    return closed


@contextmanager
def isolated_logger_scope():
    """Preserve the embedding process's BugX logger for one serialized call.

    ``configure`` replaces the shared logger's context and closes old handlers.
    Detach them before an in-process launcher invocation so its configuration
    cannot close the host's files or send sandbox events to the host's log.
    The caller must serialize calls that configure this process-global logger.
    Nested scopes preserve the outer scope's handlers and context in the same
    way. Handlers created in this scope are closed on every exit path, including
    exporters which do not have a ``_bugx_log_path``. Scope-owned exports finish
    synchronously, before the runtime scope restores cwd/environment; outside
    this context the normal CLI exporter remains asynchronous.
    """
    logger = logging.getLogger(LOGGER_NAME)
    handlers = list(logger.handlers)
    level, propagate, disabled = logger.level, logger.propagate, logger.disabled
    missing = object()
    context = getattr(logger, "_bugx_context", missing)
    export_token = _SCOPED_SYNC_EXPORT.set(True)
    try:
        for handler in handlers:
            logger.removeHandler(handler)
        try:
            yield logger
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                if handler not in handlers:
                    try:
                        handler.close()
                    except OSError:
                        pass
            for handler in handlers:
                logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
            if context is missing:
                if hasattr(logger, "_bugx_context"):
                    delattr(logger, "_bugx_context")
            else:
                logger._bugx_context = context  # type: ignore[attr-defined]
    finally:
        _SCOPED_SYNC_EXPORT.reset(export_token)


def _level(value: str | int | None) -> int:
    if value is None:
        value = os.environ.get("BUGX_LOG_LEVEL", "INFO")
    if isinstance(value, int):
        return value
    normalized = str(value).upper()
    # ``getLevelNamesMapping`` was added after the oldest Python runtime still
    # used by existing BugX projects.  Keep the logger usable on 3.9/3.10 so
    # a compatibility issue cannot hide the actual pre/status failure.
    mapping = getattr(logging, "getLevelNamesMapping", None)
    level_map = mapping() if mapping is not None else {
        name: number for name, number in logging._nameToLevel.items()
    }
    level = level_map.get(normalized)
    if not isinstance(level, int):
        raise ValueError(f"无效日志等级: {value!r}（可选 DEBUG/INFO/WARNING/ERROR）")
    return level


def configure(root: Path, *, command: str | None = None,
              level: str | int | None = None,
              trace_id: str | None = None,
              project_scoped: bool | None = None) -> logging.Logger:
    """Configure and return the BugX logger for one project root.

    ``project_scoped`` forces the log into ``<root>/.bugx/logs`` (``True``) or
    into the per-user fallback (``False``).  The default ``None`` decides by
    project state alone — deliberately the same rule as :func:`log_path`, so a
    reader can never look somewhere the writer did not write.  A directory
    without ``.bugx/manifest.json`` falls back, because logging is a side
    effect of every command and must never be what creates ``<root>/.bugx``.

    Consequence: the invocation that *creates* the project (``bugx init`` /
    ``bugx install``) still logs to the fallback, since the manifest does not
    exist yet when logging is configured.  The project log then starts with the
    next command; ``bugx init`` itself creates ``.bugx/logs/`` via the layout
    initializer, as documented in DOC-001 §5.8.

    Repeated calls in one process reuse the same handler. A later call for a
    different project replaces the old handler, which keeps test runs and
    embedded CLI calls isolated.
    """
    root = root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"日志项目根目录不是目录: {root}")
    if project_scoped is None:
        project_scoped = is_initialized_project(root)
    if project_scoped:
        # Only bootstrap commands (init/install) and already-initialized
        # projects reach this branch: the project layout owns <root>/.bugx.
        root.mkdir(parents=True, exist_ok=True)
        bugx_root = root / ".bugx"
        path = _private_dir_chain(
            bugx_root, bugx_root / LOG_DIR_NAME, label="",
        ) / LOG_FILE_NAME
    else:
        path = global_log_path(root)
        _private_dir_chain(global_log_root(), path.parent, label="回退")
    lock_path = Path(f"{path}.lock")
    if path.is_symlink() or lock_path.is_symlink():
        raise ValueError(f"拒绝使用符号链接诊断日志: {path}")
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(_level(level))
    logger.propagate = False
    context = {"command": command or "unknown"}
    for field, env_name in _CONTEXT_ENV.items():
        value = os.environ.get(env_name)
        if value:
            context[field] = value
    traceparent_env = os.environ.get("TRACEPARENT")
    parsed_tp = parse_traceparent(traceparent_env)
    if parsed_tp is not None and trace_id is None:
        context["trace_id"] = parsed_tp["trace_id"]
        context["parent_span_id"] = parsed_tp["parent_id"]
        span_id = uuid.uuid4().hex[:16]
        context["span_id"] = span_id
        context["traceparent"] = f"00-{parsed_tp['trace_id']}-{span_id}-{parsed_tp['trace_flags']}"
    else:
        assigned_trace = (
            trace_id if trace_id is not None
            else context.get("trace_id") or uuid.uuid4().hex
        )
        context["trace_id"] = assigned_trace
        span_id = uuid.uuid4().hex[:16]
        context["span_id"] = span_id
        clean_32 = assigned_trace.lower().replace("-", "").zfill(32)[-32:]
        context["traceparent"] = f"00-{clean_32}-{span_id}-01"
    logger._bugx_context = context  # type: ignore[attr-defined]
    resolved_path = str(path.resolve())
    existing = next(
        (handler for handler in logger.handlers
         if getattr(handler, "_bugx_log_path", None) == resolved_path),
        None,
    )
    if existing is None:
        for handler in list(logger.handlers):
            if getattr(handler, "_bugx_handler", False):
                logger.removeHandler(handler)
                handler.close()
        handler = _SecureRotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
            encoding="utf-8", delay=True,
        )
        handler._bugx_handler = True  # type: ignore[attr-defined]
        handler._bugx_log_path = resolved_path  # type: ignore[attr-defined]
        handler.setFormatter(_JsonFormatter())
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)

        otlp_endpoint = os.environ.get("BUGX_OTLP_ENDPOINT")
        if otlp_endpoint:
            otlp_handler = _OtlpHttpHandler(otlp_endpoint.strip())
            otlp_handler._bugx_handler = True  # type: ignore[attr-defined]
            logger.addHandler(otlp_handler)

        log_event(
            logger, "logger_configured", "BugX 诊断日志已启用",
            command=command or "unknown", log_file=str(path),
        )
    return logger


def _correlation_ids() -> dict[str, Any]:
    """DOC-204 L2：关联 ID 注入（runtime.json 权威值优先）。

    env（BUGX_RUN_ID 等）与 runtime 不一致 → 丢弃 env 值并记
    log_correlation_mismatch（嫁接尝试成为可检安全信号）；runtime 不可读
    时 env 缺失省略（不伪造）。
    """
    ids: dict[str, Any] = {}
    data: dict[str, Any] | None = None
    # 会话 runtime 的真实位置由 .bugx/current.json（bugx_paths 布局）指向；
    # 读不到时按 cwd 相对 bugx-runtime.json 回退（独立协议测试布局）。
    for cand in (Path(".bugx/current.json"), Path("bugx-runtime.json")):
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
            break
        except Exception:
            continue
    if isinstance(data, dict) and "lifecycle_id" in data and "run_id" not in data:
        # current.json 只有 polling/lifecycle 指针——顺着找 lifecycle runtime
        try:
            life_rt = (
                Path(".bugx/pollings") / str(data.get("polling_id") or "polling-unknown")
                / "lifecycles" / str(data.get("lifecycle_id") or "unknown")
                / "state" / "runtime.json"
            )
            merged = json.loads(life_rt.read_text(encoding="utf-8"))
            merged.setdefault("polling_id", data.get("polling_id"))
            merged.setdefault("lifecycle_id", data.get("lifecycle_id"))
            data = merged
        except Exception:
            pass
    env_map = {
        "run_id": "BUGX_RUN_ID", "lifecycle_id": "BUGX_LIFECYCLE_ID",
        "round": "BUGX_ROUND",
    }
    for field, env_name in env_map.items():
        env_val = os.environ.get(env_name)
        rt_val = (data or {}).get(field if field != "round" else "round")
        if data is not None and rt_val is not None:
            ids[field] = rt_val
        elif env_val:
            ids[field] = env_val
    return ids


def log_event(logger: logging.Logger, event: str, message: str = "", *,
              level: int = logging.INFO, exc_info: Any = None,
              **fields: Any) -> bool:
    """Write one structured event and report local JSONL flush success.

    ``True`` means the configured local JSONL handler acknowledged this exact
    event ID after formatter, write, flush, and close succeeded.  Logger/handler
    level filters, a missing local JSONL handler, or any local I/O failure return
    ``False``. Other handlers such as asynchronous OTLP do not affect this ack.
    """
    context = dict(getattr(logger, "_bugx_context", {}))
    # DOC-204 L2：关联 ID 注入（runtime 权威值优先；缺失省略不伪造）。
    # 节流：_correlation_ids 的文件读取每次调用有开销，按 pid 缓存 60s。
    global _CORRELATION_CACHE
    now_mono = time.monotonic()
    cached = _CORRELATION_CACHE.get("v")
    if cached is None or now_mono - cached[0] > 60.0:
        try:
            cached = (now_mono, _correlation_ids())
        except Exception:
            cached = (now_mono, {})
        _CORRELATION_CACHE["v"] = cached
    for cid_key, cid_val in cached[1].items():
        context.setdefault(cid_key, cid_val)
    context.update(fields)
    event_id = uuid.uuid4().hex
    context["event_id"] = event_id

    if logger.disabled or not logger.isEnabledFor(level):
        return False
    record_exc_info = exc_info
    if not isinstance(record_exc_info, tuple):
        record_exc_info = sys.exc_info() if record_exc_info else None
    try:
        pathname, lineno, function, _ = logger.findCaller(stack_info=False, stacklevel=2)
        record = logger.makeRecord(
            logger.name, level, pathname, lineno, message or event, (), record_exc_info,
            function, extra={"bugx_event": event, "bugx_fields": context},
        )
        logger.handle(record)
    except Exception:
        # A non-JSONL observer must not turn an already-flushed local event into
        # a false negative ack. If dispatch failed before a local ack, the event
        # remains unavailable.
        return (
            getattr(locals().get("record"), "_bugx_jsonl_ack_event_id", None)
            == event_id
        )
    return getattr(record, "_bugx_jsonl_ack_event_id", None) == event_id


def log_phase(logger: logging.Logger, phase: str, *,
              status: str = "ok", duration_seconds: float = 0.0,
              tokens_consumed: int | None = None,
              decision_reason: str = "", **fields: Any) -> None:
    """Log a named execution phase with elapsed duration, tokens consumed, and decision reason.

    ``phase`` is used for grouping and searching (e.g. "tools_kb", "pre_check",
    "worker_execution", "settlement"). ``duration_seconds`` represents elapsed wall time.
    ``tokens_consumed`` represents model tokens used in this phase if available.
    """
    if duration_seconds < 0:
        duration_seconds = 0.0
    tok_str = f" tokens={tokens_consumed}" if tokens_consumed is not None else ""
    log_fields = dict(fields)
    if tokens_consumed is not None:
        log_fields["tokens_consumed"] = tokens_consumed
    level = logging.ERROR if status == "failed" else logging.INFO
    log_event(logger, f"phase.{phase}.finished",
              f"phase={phase} duration={duration_seconds:.3f}s{tok_str} status={status}",
              level=level,
              phase=phase, status=status,
              duration_seconds=round(duration_seconds, 3),
              decision_reason=decision_reason,
              **log_fields)


class TokenPhaseTracker:
    """阶段 Token 累计器，支持多分类记录与归因分析 (DOC-042)。"""

    def __init__(self, phase: str, budget: int | None = None):
        self.phase = phase
        self.budget = budget
        self._tokens_by_category: dict[str, int] = {}
        self.status: str = "ok"
        self.decision_reason: str = ""
        self.error: str = ""

    def mark_failed(self, reason: str = "", error: str = "") -> None:
        """标记该阶段执行失败并记录原因。"""
        self.status = "failed"
        if reason:
            self.decision_reason = reason
        if error or reason:
            self.error = error or reason

    def set_decision(self, reason: str) -> None:
        """记录阶段决策原因。"""
        self.decision_reason = reason

    def record(self, count: int, category: str = "other") -> None:
        """记录指定分类的 token 消耗。"""
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return
        cat = str(category).strip().lower() or "other"
        self._tokens_by_category[cat] = self._tokens_by_category.get(cat, 0) + count

    @property
    def total_tokens(self) -> int:
        """返回阶段累计消耗的总 Token 数。"""
        return sum(self._tokens_by_category.values())

    @property
    def breakdown(self) -> dict[str, int]:
        """返回各分类的 Token 归因字典。"""
        return dict(self._tokens_by_category)


@contextmanager
def metered_phase(logger: logging.Logger | None, phase: str, *,
                  token_budget: int | None = None,
                  slow_threshold_seconds: float | None = None,
                  decision_reason: str = "", **fields: Any):
    """Context manager to measure phase duration and token consumption.

    Yields a TokenPhaseTracker to record token usage within the phase.
    Triggers slow_operation_warning and token_anomaly_warning when thresholds are exceeded.
    """
    tracker = TokenPhaseTracker(phase=phase, budget=token_budget)
    if decision_reason:
        tracker.set_decision(decision_reason)
    if logger is None:
        yield tracker
        return

    started = time.monotonic()
    status = "ok"
    error_msg = ""
    try:
        yield tracker
    except Exception as exc:
        status = "failed"
        error_msg = str(exc)
        raise
    finally:
        duration = max(0.0, time.monotonic() - started)
        tokens = tracker.total_tokens
        log_fields = dict(fields)
        if tracker.status != "ok":
            status = tracker.status
        if tracker.decision_reason:
            decision_reason = tracker.decision_reason
        if tracker.error:
            error_msg = tracker.error
        if error_msg:
            log_fields["error"] = error_msg

        # 计算 Token 预算
        budget = token_budget
        if budget is None:
            env_budget = os.environ.get(f"BUGX_TOKEN_BUDGET_{phase.upper()}")
            if env_budget:
                try:
                    budget = int(env_budget)
                except ValueError:
                    budget = None
        if budget is not None:
            log_fields["token_budget"] = budget
        if tracker.breakdown:
            log_fields["token_breakdown"] = tracker.breakdown

        log_phase(
            logger, phase, status=status, duration_seconds=duration,
            tokens_consumed=tokens if tokens > 0 else None,
            decision_reason=decision_reason, **log_fields,
        )

        # 慢耗时检测
        threshold = slow_threshold_seconds
        if threshold is None:
            env_thresh = os.environ.get("BUGX_SLOW_THRESHOLD_SECONDS")
            if env_thresh:
                try:
                    threshold = float(env_thresh)
                except ValueError:
                    threshold = DEFAULT_SLOW_THRESHOLD_SECONDS
            else:
                threshold = DEFAULT_SLOW_THRESHOLD_SECONDS
        if threshold > 0 and duration >= threshold:
            log_event(
                logger, "slow_operation_warning",
                f"阶段 {phase} 执行耗时 ({duration:.2f}s) 超过阈值 ({threshold:.2f}s)",
                level=logging.WARNING,
                phase=phase,
                duration_seconds=round(duration, 3),
                threshold_seconds=round(threshold, 3),
                status=status,
                **fields,
            )

        # 异常 Token 消耗检测
        if budget is not None and budget > 0 and tokens > budget:
            diff = tokens - budget
            log_event(
                logger, "token_anomaly_warning",
                f"阶段 {phase} 消耗 {tokens} tokens，超出预算 {budget}（超标 +{diff}）",
                level=logging.WARNING,
                phase=phase,
                actual_tokens=tokens,
                budget_tokens=budget,
                diff_tokens=diff,
                token_breakdown=tracker.breakdown,
                status=status,
                **fields,
            )


@contextmanager
def timed_phase(logger: logging.Logger | None, phase: str, *,
                token_budget: int | None = None,
                slow_threshold_seconds: float | None = None,
                decision_reason: str = "", **fields: Any):
    """Context manager to measure phase duration and tokens (wraps metered_phase)."""
    with metered_phase(
        logger, phase, token_budget=token_budget, slow_threshold_seconds=slow_threshold_seconds,
        decision_reason=decision_reason, **fields,
    ) as tracker:
        yield tracker


def log_dialogue(
    logger: logging.Logger | None,
    role: str,
    content: str,
    *,
    interaction_type: str = "chat",
    options: list[Any] | None = None,
    selected: Any = None,
    tokens: int | None = None,
    turn_id: int | None = None,
    **fields: Any,
) -> None:
    """记录人机交互与对话消息（自动脱敏与文本有界预算）。
    
    用于记录用户输入、Agent 响应、门禁单选等交互，帮助复盘时对齐人类意图与 Agent 行为。
    """
    if logger is None:
        return
    clean_content = str(content).strip()
    if len(clean_content) > 1000:
        clean_content = clean_content[:997] + "..."
    log_fields = dict(fields)
    log_fields.update({
        "role": role,
        "content_summary": clean_content,
        "interaction_type": interaction_type,
    })
    if options is not None:
        log_fields["options"] = [safe_value(opt) for opt in options]
    if selected is not None:
        log_fields["selected"] = safe_value(selected)
    if tokens is not None:
        log_fields["tokens"] = tokens
    if turn_id is not None:
        log_fields["turn_id"] = turn_id

    log_event(
        logger,
        "dialogue_message",
        f"[{role.upper()}] ({interaction_type}) {clean_content[:120]}",
        level=logging.INFO,
        **log_fields,
    )


@contextmanager
def metered_step(
    logger: logging.Logger | None,
    step_name: str,
    *,
    tool_name: str = "bash",
    tool_input: Any = None,
    step_index: int | None = None,
    slow_threshold_seconds: float = 5.0,
    **fields: Any,
):
    """用于追踪具体操作步骤（Bash 执行、工具调用、命令操作）的上下文管理器。
    
    进入时记录 step_started，退出时记录 step_finished（含毫秒级耗时与状态），
    若耗时超出阈值则发出 slow_command_warning。
    """
    if logger is None:
        yield
        return

    started = time.monotonic()
    status = "ok"
    error_msg = ""
    exit_code: int | None = 0
    safe_input = safe_value(tool_input)
    if isinstance(safe_input, str) and len(safe_input) > 500:
        safe_input = safe_input[:497] + "..."

    start_fields = dict(fields)
    start_fields.update({
        "step_name": step_name,
        "tool_name": tool_name,
        "tool_input": safe_input,
    })
    if step_index is not None:
        start_fields["step_index"] = step_index

    log_event(
        logger,
        "step_started",
        f"开始执行步骤 [{step_name}] ({tool_name})",
        level=logging.INFO,
        **start_fields,
    )

    try:
        yield
    except Exception as exc:
        status = "failed"
        error_msg = str(exc)
        exit_code = getattr(exc, "exit_code", getattr(exc, "returncode", 1))
        raise
    finally:
        duration = max(0.0, time.monotonic() - started)
        duration_ms = round(duration * 1000.0, 2)
        end_fields = dict(fields)
        end_fields.update({
            "step_name": step_name,
            "tool_name": tool_name,
            "duration_ms": duration_ms,
            "duration_seconds": round(duration, 3),
            "status": status,
        })
        if step_index is not None:
            end_fields["step_index"] = step_index
        if exit_code is not None:
            end_fields["exit_code"] = exit_code
        if error_msg:
            end_fields["error"] = error_msg

        log_event(
            logger,
            "step_finished",
            f"步骤 [{step_name}] 完成 ({tool_name}, {duration_ms}ms, status={status})",
            level=logging.INFO if status == "ok" else logging.WARNING,
            **end_fields,
        )

        if duration >= slow_threshold_seconds:
            log_event(
                logger,
                "slow_command_warning",
                f"步骤 [{step_name}] ({tool_name}) 执行耗时 ({duration:.2f}s) 超过阈值 ({slow_threshold_seconds:.2f}s)",
                level=logging.WARNING,
                step_name=step_name,
                tool_name=tool_name,
                tool_input=safe_input,
                duration_seconds=round(duration, 3),
                threshold_seconds=slow_threshold_seconds,
                **fields,
            )


@contextmanager
def metered_gate(
    logger: logging.Logger | None,
    gate_name: str,
    *,
    phase: str = "environment",
    slow_threshold_seconds: float = 2.0,
    **fields: Any,
):
    """用于追踪启动门禁与策略校验子阶段的上下文管理器。
    
    记录 gate_started 与 gate_finished，精准计算各门禁耗时毫秒数。
    """
    if logger is None:
        yield
        return

    started = time.monotonic()
    status = "ok"
    error_msg = ""
    gate_fields = dict(fields)
    gate_fields.update({"gate_name": gate_name, "phase": phase})

    log_event(
        logger,
        "gate_started",
        f"开始评估门禁 [{gate_name}]",
        level=logging.INFO,
        **gate_fields,
    )

    try:
        yield
    except Exception as exc:
        status = "blocked"
        error_msg = str(exc)
        raise
    finally:
        duration = max(0.0, time.monotonic() - started)
        duration_ms = round(duration * 1000.0, 2)
        finish_fields = dict(gate_fields)
        finish_fields.update({
            "duration_ms": duration_ms,
            "duration_seconds": round(duration, 3),
            "status": status,
        })
        if error_msg:
            finish_fields["error"] = error_msg

        log_event(
            logger,
            "gate_finished",
            f"门禁 [{gate_name}] 评估完成 ({duration_ms}ms, status={status})",
            level=logging.INFO if status == "ok" else logging.WARNING,
            **finish_fields,
        )


def compact_output(
    text: str,
    *,
    max_chars: int = 2000,
    max_lines: int = 40,
    head_lines: int = 15,
    tail_lines: int = 10,
    persisted_log_path: Path | str | None = None,
) -> str:
    """输出刚性脱水截断，防止巨幅命令日志灌入宿主大模型导致注意力膨胀与极慢推理 (DOC-170)。

    若文本行数与字符数均未超限，原样返回。
    若超限，提取「头部 N 行 + 中间包含错误/异常的关键行 + 尾部 M 行」，并标明截断行数与全量落盘路径。
    """
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines and len(text) <= max_chars:
        return text

    head = lines[:head_lines]
    tail = lines[-tail_lines:] if len(lines) > head_lines + tail_lines else []
    middle_pool = lines[head_lines:-tail_lines] if tail else []

    # 智能提取中间的错误关键词行（最多 10 行）
    error_keywords = ("error", "fail", "warn", "exception", "traceback", "blocked", "fatal", "denied", "timeout")
    matched_middle: list[str] = []
    for line in middle_pool:
        lower = line.lower()
        if any(kw in lower for kw in error_keywords):
            matched_middle.append(f"  [!] {line}")
            if len(matched_middle) >= 10:
                break

    truncated_count = len(lines) - len(head) - len(tail)
    link_info = f"，全量日志已保存至: {persisted_log_path}" if persisted_log_path else ""
    summary_banner = [
        f"... [已截断 {truncated_count} 行冗余输出以防止上下文膨胀{link_info}] ..."
    ]

    parts = list(head)
    if matched_middle:
        parts.append("--- [关键错误行摘要提取] ---")
        parts.extend(matched_middle)
        parts.append("----------------------------")
    parts.extend(summary_banner)
    parts.extend(tail)

    result = "\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n... [文本超出 {max_chars} 字符已切除] ..."
    return result


def get_context(logger: logging.Logger) -> dict[str, Any]:
    """Return the current invocation context for child-process propagation."""
    return dict(getattr(logger, "_bugx_context", {}))


def request_context(logger: logging.Logger, request_id: object | None = None) -> dict[str, Any]:
    """Create a fresh span context for one inbound request.

    The configured logger span identifies the process; request spans must be
    unique so records from two MCP requests cannot be mistaken for one span.
    """
    context = get_context(logger)
    parent_span_id = str(context.get("span_id") or uuid.uuid4().hex[:16])
    span_id = uuid.uuid4().hex[:16]
    trace_id = str(context.get("trace_id") or uuid.uuid4().hex)
    return {
        "request_id": request_id if request_id is not None else uuid.uuid4().hex,
        "parent_span_id": parent_span_id,
        "span_id": span_id,
        "traceparent": generate_traceparent(trace_id, span_id),
    }


class McpRequestBatch:
    """Aggregate contiguous low-value MCP request completions.

    This is deliberately a logging-only guard: every request still gets a
    response, but repetitive idempotent reads do not consume one JSONL record
    each.  The first/last request identities and latency totals keep the
    aggregate useful for diagnosing a polling client.
    """

    def __init__(self, method: str) -> None:
        self.method = method
        self._reset()

    def _reset(self) -> None:
        self._count = 0
        self._first: dict[str, Any] = {}
        self._last: dict[str, Any] = {}
        self._first_seen = 0.0
        self._last_seen = 0.0
        self._first_monotonic = 0.0
        self._last_monotonic = 0.0
        self._intervals_ms: list[float] = []
        self._duration_total = 0.0
        self._duration_max = 0.0
        self._counters: dict[str, int] = {}

    @property
    def count(self) -> int:
        return self._count

    def record(self, fields: dict[str, Any], *, duration_ms: float,
               counters: dict[str, int] | None = None) -> None:
        now = time.time()
        if self._count == 0:
            self._first = dict(fields)
            self._first_seen = now
            self._first_monotonic = time.monotonic()
            self._last_monotonic = self._first_monotonic
        else:
            current_monotonic = time.monotonic()
            self._intervals_ms.append(
                max(0.0, current_monotonic - self._last_monotonic) * 1000
            )
            self._last_monotonic = current_monotonic
        self._last = dict(fields)
        self._last_seen = now
        self._count += 1
        self._duration_total += max(0.0, float(duration_ms))
        self._duration_max = max(self._duration_max, float(duration_ms))
        for key, value in (counters or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                self._counters[key] = self._counters.get(key, 0) + value

    def due(self, *, max_count: int = 128,
            max_elapsed_seconds: float = 30.0) -> bool:
        """Bound an open batch so a polling-only connection stays observable."""
        if self._count == 0:
            return False
        return (
            self._count >= max_count
            or time.monotonic() - self._first_monotonic >= max_elapsed_seconds
        )

    def flush(self, logger: logging.Logger, **fields: Any) -> bool:
        """Write one summary for the pending batch and clear it."""
        if self._count == 0:
            return False
        payload: dict[str, Any] = {
            "method": self.method,
            "request_count": self._count,
            "first_request_id": self._first.get("request_id"),
            "last_request_id": self._last.get("request_id"),
            "first_span_id": self._first.get("span_id"),
            "last_span_id": self._last.get("span_id"),
            "first_seen_at": self._first_seen,
            "last_seen_at": self._last_seen,
            "elapsed_ms": round(max(0.0, self._last_seen - self._first_seen) * 1000, 2),
            "duration_ms_total": round(self._duration_total, 2),
            "duration_ms_max": round(self._duration_max, 2),
            "duration_ms_avg": round(self._duration_total / self._count, 2),
            **self._counters,
            **fields,
        }
        if self._intervals_ms:
            payload.update({
                "interval_count": len(self._intervals_ms),
                "interval_ms_min": round(min(self._intervals_ms), 2),
                "interval_ms_max": round(max(self._intervals_ms), 2),
                "interval_ms_avg": round(
                    sum(self._intervals_ms) / len(self._intervals_ms), 2,
                ),
                "interval_ms_p50": round(statistics.median(self._intervals_ms), 2),
            })
        log_event(logger, "mcp_request_batch_finished", **payload)
        self._reset()
        return True


__all__ = [
    "BACKUP_COUNT",
    "DEFAULT_SLOW_THRESHOLD_SECONDS",
    "GLOBAL_LOG_ROOT_ENV",
    "LOG_FILE_NAME",
    "LOG_SCHEMA_VERSION",
    "MAX_BYTES",
    "TokenPhaseTracker",
    "McpRequestBatch",
    "request_context",
    "close_handlers_for_root",
    "compact_output",
    "configure",
    "generate_traceparent",
    "get_context",
    "global_log_path",
    "global_log_root",
    "is_initialized_project",
    "log_dialogue",
    "log_event",
    "log_phase",
    "log_path",
    "metered_gate",
    "metered_phase",
    "metered_step",
    "parse_traceparent",
    "project_log_id",
    "timed_phase",
]
