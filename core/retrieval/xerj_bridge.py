"""Small Elasticsearch-compatible client for a local Xerj node.

The bridge only accepts loopback endpoints.  It verifies the product identity
before issuing search requests, batches concurrent-style lookups with Xerj's
``_msearch`` endpoint, and records an optional native fallback as a degraded
backend state without writing warnings or printing to the caller's terminal.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import socket
import ssl
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit


DEFAULT_XERJ_URL = "http://127.0.0.1:9200"
HEALTH_PROBE_TIMEOUT = 0.5
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class XerjBridgeError(RuntimeError):
    """Base class for a failed or invalid Xerj HTTP interaction."""


class XerjUnavailableError(XerjBridgeError):
    """The local Xerj service is unavailable or failed its identity probe."""


class XerjResponseError(XerjBridgeError):
    """Xerj returned an invalid or unsuccessful HTTP response."""


Fallback = Any
SearchRequest = Tuple[str, Mapping[str, Any]]


class XerjBridge:
    """Query a local Xerj node through its Elasticsearch-compatible API.

    ``required=False`` allows an explicitly supplied internal engine to serve
    queries if Xerj is unavailable.  The fallback is quiet at the output
    boundary, but ``degradation`` and ``last_backend`` retain the actual route
    taken so callers cannot mistake native results for Xerj results.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_XERJ_URL,
        *,
        fallback: Optional[Fallback] = None,
        required: bool = False,
        request_timeout: float = 5.0,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        circuit_failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 60.0,
    ) -> None:
        if (
            isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(float(request_timeout))
            or request_timeout <= 0
        ):
            raise ValueError("request_timeout must be positive")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        if (
            isinstance(circuit_failure_threshold, bool)
            or not isinstance(circuit_failure_threshold, int)
            or circuit_failure_threshold < 1
        ):
            raise ValueError("circuit_failure_threshold must be a positive integer")
        if (
            isinstance(circuit_cooldown_seconds, bool)
            or not isinstance(circuit_cooldown_seconds, (int, float))
            or circuit_cooldown_seconds < 0
        ):
            raise ValueError("circuit_cooldown_seconds must be a non-negative number")
        self.endpoint = self._validated_endpoint(endpoint)
        self.fallback = fallback
        self.required = bool(required)
        self.request_timeout = float(request_timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.circuit_failure_threshold = int(circuit_failure_threshold)
        self.circuit_cooldown_seconds = float(circuit_cooldown_seconds)

        self._lock = threading.RLock()
        self._connection: Optional[http.client.HTTPConnection] = None
        self._healthy = False
        self._closed = False
        self.last_backend = "xerj"
        self.degradation: Optional[Dict[str, Any]] = None
        self._consecutive_failures = 0
        self._circuit_tripped_at = 0.0

    @staticmethod
    def _validated_endpoint(endpoint: str) -> str:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty loopback URL")
        parsed = urlsplit(endpoint.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("endpoint must use http or https and include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint must not include a query or fragment")

        host = parsed.hostname.casefold()
        is_loopback = host == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise ValueError("Xerj endpoint must be loopback; remote endpoints are refused")

        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError("endpoint port must be between 1 and 65535")
        path = parsed.path.rstrip("/")
        return "%s://%s%s%s" % (
            parsed.scheme,
            "[%s]" % parsed.hostname if ":" in parsed.hostname else parsed.hostname,
            ":%d" % port,
            path,
        )

    @property
    def is_healthy(self) -> bool:
        return self._healthy and not self._closed and not self.is_circuit_open

    @property
    def circuit_state(self) -> str:
        with self._lock:
            if self._consecutive_failures < self.circuit_failure_threshold:
                return "closed"
            elapsed = time.monotonic() - self._circuit_tripped_at
            if elapsed < self.circuit_cooldown_seconds:
                return "open"
            return "half_open"

    @property
    def is_circuit_open(self) -> bool:
        return self.circuit_state == "open"

    def reset_circuit_breaker(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._circuit_tripped_at = 0.0

    def _record_probe_success(self) -> None:
        with self._lock:
            self._healthy = True
            self._consecutive_failures = 0
            self._circuit_tripped_at = 0.0

    def _record_probe_failure(self) -> None:
        with self._lock:
            self._healthy = False
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.circuit_failure_threshold:
                self._circuit_tripped_at = time.monotonic()

    @property
    def capability_status(self) -> Dict[str, Any]:
        """Return a copy of the observable active-backend/degradation state."""
        return {
            "backend": self.last_backend,
            "degraded": self.degradation is not None,
            "degradation": dict(self.degradation) if self.degradation else None,
            "circuit_state": self.circuit_state,
            "consecutive_failures": self._consecutive_failures,
        }

    def health_probe(self, timeout: float = HEALTH_PROBE_TIMEOUT) -> bool:
        """Probe ``GET /`` once, with a hard 500 ms timeout ceiling.

        A generic Elasticsearch-compatible server is not accepted as Xerj.
        The node must expose an ``x-xerj-*`` response header or identify Xerj
        in its root response.  Redirects are treated as failures and are never
        followed.
        """
        if self.is_circuit_open:
            self._healthy = False
            return False
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            self._record_probe_failure()
            return False
        budget = min(float(timeout), HEALTH_PROBE_TIMEOUT)
        try:
            status, headers, payload = self._request_json("GET", "/", None, budget)
            if status != 200:
                self._record_probe_failure()
                return False
            header_identity = any(
                key.casefold().startswith("x-xerj-") for key in headers
            )
            body_identity = False
            if isinstance(payload, Mapping):
                for key in ("name", "tagline", "product", "version"):
                    value = payload.get(key)
                    if isinstance(value, str) and "xerj" in value.casefold():
                        body_identity = True
                        break
                    if isinstance(value, Mapping):
                        text = " ".join(str(item) for item in value.values())
                        if "xerj" in text.casefold():
                            body_identity = True
                            break
            is_valid = header_identity or body_identity
            if is_valid:
                self._record_probe_success()
                return True
            else:
                self._record_probe_failure()
                return False
        except XerjBridgeError:
            self._record_probe_failure()
            return False

    # Common spellings kept as method aliases, not parallel implementations.
    probe = health_probe
    health_check = health_probe

    def search(
        self,
        index: str,
        query: Mapping[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Run an Elasticsearch ``_search`` request for one index."""
        self._validate_search(index, query)
        try:
            self._ensure_healthy()
            _, _, result = self._request_json(
                "POST", self._index_path(index, "_search"), query,
                self.request_timeout if timeout is None else timeout,
            )
            self.last_backend = "xerj"
            self.degradation = None
            return result
        except XerjBridgeError as exc:
            self._healthy = False
            self._drop_connection()
            return self._fallback_search(index, query, exc)

    def msearch(
        self,
        requests: Sequence[SearchRequest],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Run a batch of ``(index, body)`` searches using ``/_msearch``."""
        normalized = self._validate_msearch(requests)
        lines: List[str] = []
        for index, body in normalized:
            lines.append(json.dumps({"index": index}, ensure_ascii=False))
            lines.append(json.dumps(body, ensure_ascii=False))
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            self._ensure_healthy()
            _, _, result = self._request_json(
                "POST", "/_msearch", payload,
                self.request_timeout if timeout is None else timeout,
                content_type="application/x-ndjson",
            )
            self.last_backend = "xerj"
            self.degradation = None
            return result
        except XerjBridgeError as exc:
            self._healthy = False
            self._drop_connection()
            return self._fallback_msearch(normalized, exc)

    batch_search = msearch

    def close(self) -> None:
        """Close the reusable HTTP connection.  Safe to call more than once."""
        with self._lock:
            self._closed = True
            self._healthy = False
            self._drop_connection_locked()

    def __enter__(self) -> "XerjBridge":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @staticmethod
    def _validate_search(index: str, query: Mapping[str, Any]) -> None:
        if not isinstance(index, str) or not index or "\n" in index or "\r" in index:
            raise ValueError("index must be a non-empty single-line string")
        if not isinstance(query, Mapping):
            raise TypeError("query must be a mapping")

    @classmethod
    def _validate_msearch(cls, requests: Sequence[SearchRequest]) -> List[SearchRequest]:
        if isinstance(requests, (str, bytes, bytearray)) or not isinstance(requests, Sequence):
            raise TypeError("requests must be a sequence of (index, query) pairs")
        normalized: List[SearchRequest] = []
        for item in requests:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise TypeError("each msearch entry must be an (index, query) pair")
            index, query = item
            cls._validate_search(index, query)
            normalized.append((index, query))
        return normalized

    @staticmethod
    def _index_path(index: str, action: str) -> str:
        return "/%s/%s" % (quote(index, safe=",*._-"), action)

    def _ensure_healthy(self) -> None:
        if self._closed:
            raise XerjUnavailableError("Xerj bridge is closed")
        if self.is_circuit_open:
            raise XerjUnavailableError(
                "Xerj circuit breaker is open (%d consecutive probe failures)" % self._consecutive_failures
            )
        if self._healthy:
            return
        if not self.health_probe():
            raise XerjUnavailableError("local Xerj health probe failed")

    def _request_json(
        self,
        method: str,
        path: str,
        body: Any,
        timeout: float,
        *,
        content_type: str = "application/json",
    ) -> Tuple[int, Dict[str, str], Any]:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise XerjUnavailableError("request timeout must be positive")
        parsed = urlsplit(self.endpoint)
        full_path = (parsed.path.rstrip("/") + path) or "/"
        body_bytes: Optional[bytes]
        if body is None:
            body_bytes = None
        elif isinstance(body, bytes):
            body_bytes = body
        else:
            body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        with self._lock:
            if self._closed:
                raise XerjUnavailableError("Xerj bridge is closed")
            connection = self._get_connection(float(timeout))
            try:
                headers = {"Accept": "application/json"}
                if body_bytes is not None:
                    headers["Content-Type"] = content_type
                connection.timeout = float(timeout)
                if connection.sock is not None:
                    connection.sock.settimeout(float(timeout))
                connection.request(method, full_path, body=body_bytes, headers=headers)
                response = connection.getresponse()
                raw = response.read(self.max_response_bytes + 1)
                status = response.status
                response_headers = {key.casefold(): value for key, value in response.getheaders()}
                if len(raw) > self.max_response_bytes:
                    raise XerjResponseError("Xerj response exceeded the configured size limit")
                if 300 <= status < 400:
                    raise XerjResponseError("Xerj endpoint returned a redirect")
                if status < 200 or status >= 300:
                    detail = raw[:2048].decode("utf-8", errors="replace")
                    raise XerjResponseError("Xerj HTTP %d: %s" % (status, detail))
                try:
                    decoded = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise XerjResponseError("Xerj returned invalid JSON") from exc
                return status, response_headers, decoded
            except XerjBridgeError:
                self._drop_connection_locked()
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError) as exc:
                self._drop_connection_locked()
                raise XerjUnavailableError("Xerj request failed: %s" % exc) from exc

    def _get_connection(self, timeout: float) -> http.client.HTTPConnection:
        if self._connection is not None:
            self._connection.timeout = timeout
            return self._connection
        parsed = urlsplit(self.endpoint)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection_type = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        kwargs: Dict[str, Any] = {"timeout": timeout}
        if parsed.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        self._connection = connection_type(parsed.hostname, port, **kwargs)
        return self._connection

    def _drop_connection(self) -> None:
        with self._lock:
            self._drop_connection_locked()

    def _drop_connection_locked(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _fallback_search(self, index: str, query: Mapping[str, Any], error: XerjBridgeError) -> Any:
        if self.required or self.fallback is None:
            raise XerjUnavailableError(str(error)) from error
        self._record_degradation(error)
        method = getattr(self.fallback, "search", None)
        if callable(method):
            return method(index, query)
        if callable(self.fallback):
            return self.fallback(index, query)
        raise XerjUnavailableError("fallback engine does not provide search(index, query)") from error

    def _fallback_msearch(self, requests: List[SearchRequest], error: XerjBridgeError) -> Any:
        if self.required or self.fallback is None:
            raise XerjUnavailableError(str(error)) from error
        self._record_degradation(error)
        method = getattr(self.fallback, "msearch", None)
        if callable(method):
            return method(requests)
        method = getattr(self.fallback, "search", None)
        if callable(method):
            return [method(index, query) for index, query in requests]
        if callable(self.fallback):
            return [self.fallback(index, query) for index, query in requests]
        raise XerjUnavailableError("fallback engine does not provide msearch or search") from error

    def _record_degradation(self, error: XerjBridgeError) -> None:
        self.last_backend = "native"
        self.degradation = {
            "preferred_backend": "xerj",
            "active_backend": "native",
            "reason": str(error),
            "recorded_at": time.time(),
        }


__all__ = [
    "DEFAULT_XERJ_URL",
    "HEALTH_PROBE_TIMEOUT",
    "XerjBridge",
    "XerjBridgeError",
    "XerjResponseError",
    "XerjUnavailableError",
]
