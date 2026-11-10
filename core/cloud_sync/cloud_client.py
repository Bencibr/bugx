#!/usr/bin/env python3
"""bugx 云端同步客户端 —— 对接后端 /api/v1/agent/*（DOC-008 V2-only）。

覆盖：device-code 登录（发码+轮询）、api-key、设备登记、project 登记、
global/project knowledge 的 push/pull、sync/status。复用 report_client 的
结构化结果（ReportResult）与解析/错误归一。

协议口径（DOC-008 §5，V2-only，无任何 V1 fallback）：
  wire envelope 严格 schema_version=2；records 五个数组恒在；
  manifest_hash 为 canonical hash（见 core/cloud_schema.py）。
  POST /api/v1/agent/auth/device        {device_id, name} -> data{device_code,user_code,verification_uri,expires_in,interval}
  POST /api/v1/agent/auth/device-token  {device_code, device_id} -> 成功 data{token,refresh_token,user}；pending/denied 用 code=403+error 字段
  POST /api/v1/agent/auth/refresh       {refresh_token} -> data{token,refresh_token,user}
  POST /api/v1/agent/devices            {device_id, name}（Bearer）
  POST /api/v1/agent/projects           {project_key,name,slug,owner}（Bearer）-> data{project_id,project_key,slug,name,owner}
  POST /api/v1/agent/global/knowledge   V2 push envelope（Bearer）
  GET  /api/v1/agent/global/knowledge   ?schema_version=2&cursor=<opaque>&page_size&device_id（query-only，禁止 body）
  POST /api/v1/agent/global/knowledge/ack   post-import cursor ACK（Bearer）
  GET  /api/v1/agent/sync/status        -> data{...}
旧客户端/旧响应一律明确拒绝（缺 schema、schema=1、legacy lessons/incidents 字段）。
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _datetime
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core.report_engine.report_client import (
    ReportResult,
    atomic_write_nofollow,
    load_installation_secret,  # 设备 ID 文件丢失时的同机稳定派生源
    message_from_response,
    parse_response_json,
)
from core.infra.client_metadata import client_headers, client_marker, client_user_agent, client_version, tls_context
from core.secure_atomic import AtomicWriteError, reject_symlink_components
from core.cloud_sync.cloud_schema import (
    MAX_CURSOR_LENGTH,
    SCHEMA_VERSION,
    SchemaError,
    SyncError,
    build_push_envelope,
    parse_sync_error,
    validate_pull_response,
)

# 发布包的默认正式云端端点；可由 BUGX_CLOUD_API_BASE 或全局 cloud-config.json 覆盖。
DEFAULT_BASE = "https://bugx.dev"
DEFAULT_BASE_CANDIDATES: tuple[str, ...] = ("https://bugx.com.cn", "https://bugx.dev")
CLOUD_ROUTE_CACHE_PATH = os.environ.get(
    "BUGX_CLOUD_ROUTE_CACHE", str(Path.home() / ".bugx" / "cloud-route-cache.json")
)
DEFAULT_ROUTE_TTL = float(os.environ.get("BUGX_CLOUD_ROUTE_TTL", "86400"))  # 24 小时
DEFAULT_PROBE_TIMEOUT = float(os.environ.get("BUGX_PROBE_TIMEOUT", "0.8"))
_MEMOIZED_BASE_URL: str | None = None
CRED_PATH = os.environ.get("BUGX_CLOUD_CREDENTIALS", str(Path.home() / ".bugx" / "cloud-credentials.json"))
DEVICE_ID_PATH = os.environ.get("BUGX_CLOUD_DEVICE_ID", str(Path.home() / ".bugx" / "cloud-device-id"))
CLOUD_CONFIG_PATH = os.environ.get(
    "BUGX_CLOUD_CONFIG", str(Path.home() / ".bugx" / "cloud-config.json")
)
DEFAULT_TIMEOUT = float(os.environ.get("BUGX_CLOUD_TIMEOUT", "8"))
DEFAULT_RETRIES = int(os.environ.get("BUGX_CLOUD_RETRIES", "1"))
DEFAULT_PAGE = int(os.environ.get("BUGX_CLOUD_PAGE_SIZE", "100"))
MAX_PULL_PAGES = int(os.environ.get("BUGX_CLOUD_MAX_PULL_PAGES", "1000"))
MAX_AGENT_PACKAGE_BYTES = 128 * 1024 * 1024
AGENT_PACKAGE_READ_CHUNK = 1024 * 1024


from core.cloud_sync.network_transport import (
    CurlResponse,
    NoRedirectHandler,
    build_no_redirect_open,
    build_redirect_opener,
    diagnose_network_error,
    is_private_target_host,
    parse_curl_response_headers,
    tls_context,
    trusted_curl_request,
)

# Compatibility aliases
_is_private_target_host = is_private_target_host
_NoRedirectHandler = NoRedirectHandler
_tls_context = tls_context
_build_no_redirect_open = build_no_redirect_open
_build_redirect_opener = build_redirect_opener
_CurlResponse = CurlResponse
_parse_curl_response_headers = parse_curl_response_headers
_trusted_curl_request = trusted_curl_request
from core.cloud_sync.sensitive_filter import (
    SENSITIVE_POLICIES,
    SYNC_DETAIL_MODES,
    SYNC_POLICY_VOCABS,
    SYNC_SCOPE_DEFAULTS,
    SensitiveSyncError,
    emit_policy_audit,
    iter_record_strings,
    prepare_records_for_sync,
    redact_records_for_sync,
    redact_sensitive_text,
    scan_kind,
    scan_sensitive_text,
    scan_wire_value,
    validate_knowledge_for_sync,
    validate_pull_envelope,
    validate_records_for_sync,
    validate_secret_shapes,
    wire_project_id,
)
from core.cloud_sync.endpoint_router import (
    clear_cloud_base_url,
    clear_route_cache,
    cloud_base_url_status,
    cloud_config_path,
    cloud_route_cache_path,
    configured_base_url,
    load_cloud_config,
    load_route_cache,
    probe_candidate_latency,
    read_cloud_config_raw,
    save_route_cache,
    select_lowest_latency_endpoint,
    set_cloud_base_url,
    update_cloud_config,
    validate_base_url,
)
from core.cloud_sync.device_identity import (
    CloudCredentials,
    LESSON_FEEDBACK_MAX_ITEMS,
    LESSON_FEEDBACK_OUTCOMES,
    get_device_id,
    get_device_label,
    incident_to_wire,
    lesson_to_wire,
    token_expires_in,
)

# Compatibility aliases
_scan_wire_value = scan_wire_value
_scan_kind = scan_kind
_iter_record_strings = iter_record_strings
_validate_secret_shapes = validate_secret_shapes
_emit_policy_audit = emit_policy_audit
_wire_project_id = wire_project_id
_validate_base_url = validate_base_url
_cloud_config_path = cloud_config_path
_cloud_route_cache_path = cloud_route_cache_path
_read_cloud_config_raw = read_cloud_config_raw
_token_expires_in = token_expires_in

AGENT_API_PREFIX = "/api/v1/agent"
DEVICE_PATH = f"{AGENT_API_PREFIX}/auth/device"
DEVICE_TOKEN_PATH = f"{AGENT_API_PREFIX}/auth/device-token"
AUTH_REFRESH_PATH = f"{AGENT_API_PREFIX}/auth/refresh"
ROUTING_PATH = f"{AGENT_API_PREFIX}/routing"
DEVICES_PATH = f"{AGENT_API_PREFIX}/devices"
PROJECTS_PATH = f"{AGENT_API_PREFIX}/projects"
SYNC_STATUS_PATH = f"{AGENT_API_PREFIX}/sync/status"
VERSION_CHECK_PATH = f"{AGENT_API_PREFIX}/version"
DOWNLOAD_PATH = f"{AGENT_API_PREFIX}/download"
KNOWLEDGE_PREFETCH_PATH = f"{AGENT_API_PREFIX}/knowledge/prefetch"
VULNERABILITY_QUERY_PATH = f"{AGENT_API_PREFIX}/vulnerabilities/query"
PATCH_VERIFICATION_PATH = f"{AGENT_API_PREFIX}/patch-verifications"
PROJECT_PROFILE_MAX_BYTES = 256 * 1024
VULNERABILITY_QUERY_MAX_COMPONENTS = 100


class CloudClient:
    """带认证的云端客户端（device-code / api-key），网络封装单一入口。"""

    def __init__(
        self,
        base_url: str | None = None,
        credentials: CloudCredentials | None = None,
        cred_path: str | Path | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        self.cred_path = cred_path or CRED_PATH
        self.cred = credentials if credentials is not None else CloudCredentials.load(self.cred_path)
        if base_url:
            # An explicit endpoint is authoritative. Do not inspect user sync
            # configuration or device routing before honoring it.
            self._explicit_base = True
            self._server_route_enabled = False
            self.base = _validate_base_url(base_url)
        else:
            configured = cloud_base_url_status()
            self._explicit_base = configured["source"] != "default"
            self._server_route_enabled = not self._explicit_base and client_marker() not in {"dev", "uat"}
            route_base = (
                self.cred.cloud_api_base
                if self._server_route_enabled and self._route_matches_client() else None
            )
            self.base = route_base or configured_base_url()
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self.retries = retries if retries is not None else DEFAULT_RETRIES
        self.max_pull_pages = MAX_PULL_PAGES
        self._custom_urlopen = urlopen is not None
        self._urlopen = urlopen or _build_no_redirect_open(self.base)

    @staticmethod
    def _expected_environment() -> str:
        return {"dev": "dev", "uat": "uat"}.get(client_marker(), "prod")

    def _route_matches_client(self) -> bool:
        return bool(self.cred.cloud_api_base and self.cred.cloud_region
                    and self.cred.cloud_route_environment == self._expected_environment())

    def _route_expired(self) -> bool:
        if self._server_route_enabled and self.cred.is_authenticated() and not self._route_matches_client():
            return True
        value = self.cred.cloud_route_expires_at
        if not value:
            return False
        try:
            expires = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_datetime.timezone.utc)
        return expires.timestamp() <= time.time()

    def _store_route(self, route: object) -> bool:
        if not isinstance(route, dict):
            return False
        region = str(route.get("region") or "").strip().lower()
        environment = str(route.get("environment") or "").strip().lower()
        api_base = str(route.get("api_base") or "").strip()
        if region not in {"cn", "global"} or environment != self._expected_environment() or not api_base:
            return False
        try:
            normalized = _validate_base_url(api_base)
        except ValueError:
            return False
        self.cred.cloud_region = region
        self.cred.cloud_api_base = normalized
        self.cred.cloud_route_environment = environment
        self.cred.cloud_route_expires_at = str(route.get("expires_at") or "")
        self.cred.cloud_route_version = str(route.get("config_version") or "")
        if self._server_route_enabled:
            self.base = normalized
            if not self._custom_urlopen:
                self._urlopen = _build_no_redirect_open(self.base)
        return True

    def _refresh_route(self) -> bool:
        if not self._server_route_enabled or not self.cred.is_authenticated():
            return False
        result = self._request("GET", ROUTING_PATH, auth=True, retry_auth=False)
        if not result.ok:
            return False
        stored = self._store_route(self._data(result))
        if stored:
            self.cred.save(self.cred_path)
        return stored

    # ---- 内部请求 ----
    def _auth_header(self) -> str | None:
        key = self.cred.api_key or self.cred.token
        return f"Bearer {key}" if key else None

    def _mark_auth_expired(self) -> None:
        """Keep a server-confirmed 401 visible to the next CLI process."""
        self.cred.auth_state_override = "expired"
        try:
            self.cred.save(self.cred_path)
        except (OSError, ValueError):
            # An unwritable cache must not hide the authoritative 401 result.
            pass

    def _open_request(self, req: urllib.request.Request) -> Any:
        try:
            return self._urlopen(req, timeout=self.timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if self._custom_urlopen or not diagnose_network_error(
                exc, req.full_url
            )["is_local_network_blocked"]:
                raise
            try:
                return _trusted_curl_request(req, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                    ValueError) as fallback_error:
                raise fallback_error from exc

    def _request(self, method: str, path: str, payload: dict | None = None,
                 *, auth: bool = True, retry_auth: bool = True,
                 params: dict | None = None,
                 extra_headers: dict | None = None) -> ReportResult:
        if auth and path not in {ROUTING_PATH, AUTH_REFRESH_PATH} and self._route_expired():
            self._refresh_route()
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/json",
            **client_headers(),
        }
        if extra_headers:
            headers.update(extra_headers)
        if auth:
            # 客户端到期前 2 天自动静默续期（API Key 为独立体系，不触发 refresh）
            if retry_auth and not self.cred.api_key and self.cred.token and self.cred.refresh_token:
                expires_in = _token_expires_in(self.cred.token)
                if expires_in is not None and expires_in <= 2 * 86400:
                    self._refresh()
            header = self._auth_header()
            if header:
                headers["Authorization"] = header
        body = None
        if payload is not None:
            # V2-only：pull 一律 query 参数（DOC-008 §5.2），GET 禁止携带 body。
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        for attempt in range(self.retries + 1):
            try:
                resp = self._open_request(req)
                try:
                    status = getattr(resp, "status", 200)
                    raw = resp.read() if hasattr(resp, "read") else b""
                finally:
                    resp.close()
                data = parse_response_json(raw)
                # V2 errors use their real HTTP status; older handlers may also
                # return a 200 envelope with code>=400, so handle both forms.
                code_marker = data.get("code") if isinstance(data, dict) else None
                biz_401 = (status == 401) or (code_marker == 401)
                if (biz_401 and auth and retry_auth and self.cred.refresh_token
                        and self._refresh()):
                    return self._request(method, path, payload, auth=auth, retry_auth=False,
                                        params=params, extra_headers=extra_headers)
                if biz_401 and auth:
                    self._mark_auth_expired()
                retryable = (
                    status >= 500
                    or (isinstance(code_marker, int) and code_marker in (429, 503))
                )
                if retryable and attempt < self.retries:
                    time.sleep(self._retry_delay(data, attempt))
                    continue
                biz_bad = isinstance(code_marker, int) and code_marker >= 400
                return ReportResult(ok=200 <= status < 300 and not biz_bad,
                                    status=status, data=data, error=None,
                                    )
            except urllib.error.HTTPError as exc:
                # 401 → 用 refresh_token 续期后重试一次（覆盖真实 HTTP 401）
                if (exc.code == 401 and auth and retry_auth and self.cred.refresh_token
                        and self._refresh()):
                    try:
                        exc.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return self._request(method, path, payload, auth=auth, retry_auth=False,
                                        params=params, extra_headers=extra_headers)
                if exc.code == 401 and auth:
                    self._mark_auth_expired()
                if exc.code >= 500 and attempt < self.retries:
                    try:
                        exc.close()
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raw = exc.read()
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
                return ReportResult(ok=False, status=exc.code, data=parse_response_json(raw), error=message_from_response(raw, exc))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                diag = diagnose_network_error(exc, url)
                if diag["is_local_network_blocked"]:
                    return ReportResult(
                        ok=False, status=None,
                        data={"code": diag["code"], "message": diag["message"]},
                        error=diag["message"],
                    )
                message = diag.get("message", str(exc))
                return ReportResult(
                    ok=False, status=None,
                    data={"code": "SYNC_NETWORK_UNAVAILABLE", "message": message},
                    error=message,
                )
            except ValueError as exc:
                return ReportResult(
                    ok=False, status=None,
                    data={"code": "SYNC_INVALID_RESPONSE", "message": str(exc)},
                    error=str(exc),
                )
        return ReportResult(ok=False, status=None, data=None, error="unknown")

    def _data(self, res: ReportResult) -> dict:
        return (res.data or {}).get("data") if isinstance(res.data, dict) and isinstance(res.data.get("data"), dict) else (res.data or {})

    def submit_patch_package(self, package: bytes, filename: str, *,
                             source_commit: str = "") -> ReportResult:
        """Submit an opaque patch ZIP; the server owns static verification."""
        if not isinstance(package, (bytes, bytearray)) or not package:
            raise ValueError("补丁包不能为空")
        if not filename or "/" in filename or "\\" in filename:
            raise ValueError("补丁包文件名无效")
        boundary = "----bugx-patch-" + uuid.uuid4().hex
        chunks = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"package\"; filename=\"{filename}\"\r\nContent-Type: application/zip\r\n\r\n".encode(),
            bytes(package), b"\r\n",
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"source_commit\"\r\n\r\n{source_commit.strip()}\r\n".encode(),
            f"--{boundary}--\r\n".encode(),
        ]
        return self._request_raw(
            "POST", PATCH_VERIFICATION_PATH, b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def get_patch_verification(self, verification_id: str) -> ReportResult:
        value = str(verification_id or "").strip()
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ValueError("verification_id 必须是正整数")
        return self._request("GET", f"{PATCH_VERIFICATION_PATH}/{value}")

    def _request_raw(self, method: str, path: str, body: bytes,
                     *, content_type: str) -> ReportResult:
        url = self.base + path
        headers = {"Accept": "application/json", **client_headers(),
                   "Content-Type": content_type, "Content-Length": str(len(body))}
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = self._open_request(req)
            try:
                status = getattr(resp, "status", 200)
                raw = resp.read() if hasattr(resp, "read") else b""
            finally:
                resp.close()
            data = parse_response_json(raw)
            code = data.get("code") if isinstance(data, dict) else None
            return ReportResult(ok=200 <= status < 300 and not (isinstance(code, int) and code >= 400),
                                status=status, data=data, error=None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
            return ReportResult(ok=False, status=exc.code, data=parse_response_json(raw), error=message_from_response(raw, exc))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            diag = diagnose_network_error(exc, url)
            if diag["is_local_network_blocked"]:
                return ReportResult(
                    ok=False, status=None,
                    data={"code": diag["code"], "message": diag["message"]},
                    error=diag["message"],
                )
            message = diag.get("message", str(exc))
            return ReportResult(ok=False, status=None,
                                data={"code": "PATCH_NETWORK_UNAVAILABLE", "message": message},
                                error=message)
        except ValueError as exc:
            return ReportResult(ok=False, status=None,
                                data={"code": "PATCH_NETWORK_UNAVAILABLE", "message": str(exc)},
                                error=str(exc))

    @staticmethod
    def _retry_delay(data: dict | None, attempt: int) -> float:
        err = parse_sync_error(None, data)
        if err.retry_after_seconds is not None:
            return float(min(err.retry_after_seconds, 30))
        return 0.5 * (attempt + 1)

    def sync_error(self, res: ReportResult) -> SyncError:
        """把失败响应解析为结构化同步错误（DOC-008 §5.3）。"""
        status = res.status
        body = res.data if isinstance(res.data, dict) else {}
        wrapped = body.get("code") if isinstance(body, dict) else None
        if isinstance(wrapped, int) and not isinstance(wrapped, bool):
            status = status if (status is not None and status >= 400) else wrapped
        return parse_sync_error(status, body)

    # ---- 认证 ----
    def start_device(self, name: str = "", device_id: str | None = None) -> dict:
        """发起 device-code，返回 {device_code, user_code, verification_uri, expires_in, interval}。"""
        did = device_id or get_device_id()
        res = self._request("POST", DEVICE_PATH, {"device_id": did, "name": name}, auth=False, retry_auth=False)
        if not res.ok:
            raise RuntimeError(f"发起设备授权失败: code={res.status} {res.error}")
        return self._data(res)

    def poll_device_token(self, device_code: str, device_id: str | None = None,
                          max_tries: int = 120, interval: int = 5) -> ReportResult:
        """轮询 device-token：pending/无 token 继续，denied/expired 报错，拿到 token 存凭据。"""
        code_to_msg = {
            "access_denied": "用户已拒绝授权",
            "expired_token": "授权码已过期，请重新登录",
            "expired": "授权码已过期，请重新登录",
        }
        did = device_id or get_device_id()
        backoff = 0.0
        for _ in range(max_tries):
            res = self._request("POST", DEVICE_TOKEN_PATH,
                                {"device_code": device_code, "device_id": did},
                                auth=False, retry_auth=False)
            data = res.data if isinstance(res.data, dict) else {}
            err = str(data.get("error", "") or "")
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            token = inner.get("token") or inner.get("access_token")
            if err in {"access_denied", "expired_token", "expired"}:
                return ReportResult(ok=False, status=getattr(res, "status", None),
                                    data=res.data, error=code_to_msg.get(err, err))
            if token:
                self.cred = CloudCredentials(
                    token=token, refresh_token=inner.get("refresh_token"),
                    api_key=inner.get("api_key"), user=inner.get("user"),
                )
                self._store_route(inner.get("route"))
                self.cred.save(self.cred_path)
                return ReportResult(ok=True, status=getattr(res, "status", 200),
                                    data=res.data, error=None)
            # 限流（联调 I-2）：429 不消耗等待授权的轮次（device_code 有效期
            # 远大于 max_tries*interval），按加压退避后重试同一轮询，避免
            # 登录因服务端 RateLimitStrict 凭空失败。
            if getattr(res, "status", None) == 429 or data.get("code") == 429:
                backoff = min(backoff + interval, 30.0)
                time.sleep(backoff)
                continue
            backoff = 0.0
            # 仍在等待授权 -> 继续轮询
            time.sleep(interval)
        return ReportResult(ok=False, status=None, data=None, error="授权超时")

    def login(self, name: str = "", device_id: str | None = None) -> ReportResult:
        """完整 device-code 登录：发码→打印 URL→尝试自动打开浏览器→轮询→存凭据。"""
        flow = self.start_device(name=name, device_id=device_id)
        uri = flow.get("verification_uri") or ""
        # 授权页必须是普通 http(s) 地址：被劫持/被替换的服务端可能借
        # file:、自定义 scheme 等把宿主浏览器导向本地文件或钓鱼协议，
        # 这里 fail-closed，绝不打印和打开非 http(s) 链接。
        if uri and not str(uri).lower().startswith(("http://", "https://")):
            return ReportResult(ok=False, status=None, data=None,
                                error=f"服务端返回的 verification_uri 不是 http(s) 地址: {uri!r}")
        # flush：stdout 可能是管道（自动化/CI），不刷会卡在缓冲里抓不到 user_code
        print(f"请打开授权页完成登录：{uri}", flush=True)
        print(f"（用户码 {flow.get('user_code')}；有效期 {flow.get('expires_in')}s）", flush=True)
        # 后台 device-code 需人工在浏览器确认；有桌面环境时自动拉起，失败仅回退到手动打开
        if uri:
            opened = False
            try:
                import webbrowser
                opened = bool(webbrowser.open(uri, new=2))
            except Exception:
                opened = False
            # macOS 的 TUI/子进程有时没有可用的 webbrowser 注册器；
            # 使用系统 opener 再尝试一次，但仍不把打开失败当成授权成功。
            if not opened and sys.platform == "darwin":
                try:
                    opened = subprocess.run(
                        ["open", uri], check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5,
                    ).returncode == 0
                except (OSError, subprocess.SubprocessError):
                    opened = False
            if opened:
                print(f"已打开浏览器授权页：{uri}", flush=True)
            else:
                print(f"自动打开浏览器失败，请手动打开授权页：{uri}", flush=True)
        # 轮询间隔来自服务端，属不可信输入：限幅到 [1,30] 秒，拒绝 0/负值
        # 造成的忙轮询，也拒绝异常大值把登录挂死。非法类型回退默认 5s。
        try:
            interval = int(flow.get("interval", 5))
        except (TypeError, ValueError):
            interval = 5
        interval = min(30, max(1, interval))
        return self.poll_device_token(flow.get("device_code", ""),
                                       device_id=device_id, interval=interval)

    def set_api_key(self, api_key: str) -> None:
        self.cred.api_key = api_key
        self.cred.auth_state_override = None
        self.cred.save(self.cred_path)

    def logout(self) -> None:
        self.cred.clear(self.cred_path)
        self.cred = CloudCredentials()

    def _refresh(self) -> bool:
        res = self._request("POST", AUTH_REFRESH_PATH,
                            {"refresh_token": self.cred.refresh_token}, auth=False, retry_auth=False)
        data = self._data(res)
        new_tok = data.get("token") or data.get("access_token")
        if res.ok and new_tok:
            self.cred.token = new_tok
            self.cred.refresh_token = data.get("refresh_token", self.cred.refresh_token)
            self._store_route(data.get("route"))
            self.cred.auth_state_override = None
            self.cred.save(self.cred_path)
            return True
        return False

    def verify_auth(self) -> ReportResult:
        """用只读同步状态接口验证当前凭据是否被服务端接受。

        ``CloudCredentials.auth_state`` 只能判断本地 JWT 过期或凭据是否
        存在；opaque token 必须由服务端确认。该方法不创建项目、不写知识，
        401 会通过统一请求路径触发 refresh，并在最终失败时标记 expired。
        """
        if not self.cred.is_authenticated():
            return ReportResult(ok=False, status=None, data=None, error="云端凭据未配置")
        result = self._request("GET", SYNC_STATUS_PATH)
        if result.ok:
            # 成功核验清除曾经持久化的拒绝；不持久化 valid，避免 opaque
            # token 在未来过期后仍被永久视为有效。
            if self.cred.auth_state_override == "expired":
                self.cred.auth_state_override = None
                try:
                    self.cred.save(self.cred_path)
                except (OSError, ValueError):
                    pass
        elif result.status == 401:
            self._mark_auth_expired()
        return result

    # ---- 设备 / 项目 ----
    def register_device(self, name: str = "", device_id: str | None = None) -> ReportResult:
        return self._request("POST", DEVICES_PATH,
                             {"device_id": device_id or get_device_id(), "name": name})

    def register_project(self, slug: str, name: str, owner: str = "", *,
                         project_key: str | None = None) -> ReportResult:
        """Resolve/create a project using its stable repository identity."""
        if not isinstance(project_key, str) or not project_key.strip():
            raise ValueError("project_key 必须提供，禁止使用路径或目录名推导项目身份")
        return self._request(
            "POST", PROJECTS_PATH,
            {"project_key": project_key.strip(), "slug": slug, "name": name, "owner": owner},
        )

    def list_projects(self) -> ReportResult:
        return self._request("GET", PROJECTS_PATH)

    def project_id(self, res: ReportResult) -> str:
        """从 register_project 的稳定 DTO 响应提取正整数 project_id。"""
        d = self._data(res)
        value = d.get("project_id")
        if isinstance(value, bool):
            return ""
        if isinstance(value, int) and value > 0:
            return str(value)
        if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
            return value
        return ""

    def project_key(self, res: ReportResult) -> str:
        """从项目登记 DTO 提取服务端回显的稳定身份。"""
        value = self._data(res).get("project_key")
        return value.strip() if isinstance(value, str) else ""

    # ---- 项目画像元数据（与知识 V2 通道分离）----
    def get_project_profile(self, project_id: str) -> ReportResult:
        """读取当前账号项目的 ProjectProfile 元数据。"""
        return self._request("GET", self._profile_path(project_id))

    def put_project_profile(self, project_id: str, profile: dict[str, Any], *,
                            base_revision: int = 0,
                            operation_id: str | None = None) -> ReportResult:
        """幂等写入项目画像；服务端以 revision 做轻量乐观并发控制。"""
        if not isinstance(profile, dict):
            raise ValueError("project_profile 必须是对象")
        if profile.get("schema_version") != 2:
            raise ValueError("project_profile.schema_version 必须为 2")
        if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0:
            raise ValueError("base_revision 必须是非负整数")
        sanitized_profile = dict(profile)
        sanitized_profile.pop("observed_at", None)
        body = {
            "schema_version": 2,
            "profile_revision": int(sanitized_profile.get("revision", 0) or 0),
            "base_revision": base_revision,
            "operation_id": operation_id or f"profile-{uuid.uuid4().hex}",
            "profile": sanitized_profile,
        }
        raw_size = len(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if raw_size > PROJECT_PROFILE_MAX_BYTES:
            raise ValueError("project_profile 超过 256 KiB 限制")
        hits: set[str] = set()
        _scan_wire_value(profile, path="project_profile", hits=hits)
        if hits:
            raise SensitiveSyncError(
                "project_profile 命中敏感信息规则(" + ", ".join(sorted(hits)) + ")，拒绝上传"
            )
        return self._request("PUT", self._profile_path(project_id), body)

    def query_vulnerabilities(self, components: list[dict[str, Any]]) -> ReportResult:
        """Warm/read the server's public version-specific advisory cache.

        The response is advisory-only: callers keep local SCA candidates as
        the authoritative offline path and must not mutate ProjectProfile from
        this endpoint.
        """
        if not isinstance(components, list) or len(components) > VULNERABILITY_QUERY_MAX_COMPONENTS:
            raise ValueError("漏洞查询组件数不能超过 100")
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in components:
            if not isinstance(item, dict):
                raise ValueError("漏洞查询组件必须是对象")
            ecosystem = str(item.get("ecosystem") or "").strip().lower()
            name = str(item.get("name") or item.get("package") or "").strip()
            version = str(
                item.get("resolved_version")
                or item.get("declared_version")
                or item.get("observed_version")
                or ""
            ).strip()
            if not ecosystem or not name or not version:
                continue
            key = (ecosystem, name, version)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"ecosystem": ecosystem, "name": name, "version": version})
        return self._request(
            "POST", VULNERABILITY_QUERY_PATH,
            {"schema_version": 1, "components": normalized},
        )

    # ---- 知识同步（V2-only）----
    def push_knowledge(self, scope: str, project_id: str = "",
                       lessons: list[dict] | None = None,
                       incidents: list[dict] | None = None, *,
                       tool_experience: list[dict] | None = None,
                       consumption_events: list[dict] | None = None,
                       tombstones: list[dict] | None = None,
                       snapshot_id: str | None = None,
                       operation_id: str | None = None,
                       base_version: int | None = None,
                       device_id: str | None = None,
                       sensitive_policy: str = "allow") -> ReportResult:
        """V2 push：完整 envelope（records 四数组恒在，无 legacy 顶层字段）。

        DOC-211：sensitive_policy=allow 跳过出网敏感扫描（策略应用已在构建侧
        prepare_records_for_sync 完成）；block/redact 在此做最终复核。
        """
        if scope not in {"global", "project"}:
            raise ValueError("scope 必须是 global 或 project")
        if scope == "project" and not str(project_id).strip():
            raise ValueError("project scope 必须显式提供 project_id")
        if base_version is not None and (not isinstance(base_version, int)
                                         or isinstance(base_version, bool) or base_version < 0):
            raise ValueError("base_version 必须是非负整数")
        filtered_lessons = list(lessons or [])
        filtered_tombstones = list(tombstones or [])
        if scope == "project":
            filtered_lessons = [
                p for p in filtered_lessons
                if not (str(p.get("pattern_id") or p.get("lesson_id") or "").startswith("L-00000000000")
                        or ("mistake-book.md" in str(p.get("source_refs") or "")))
            ]
            filtered_tombstones = [
                t for t in filtered_tombstones
                if not (str(t.get("record_type") or "") in ("pattern", "patterns")
                        and str(t.get("record_id") or "").startswith("L-00000000000"))
            ]
        records = {
            "patterns": filtered_lessons,
            "incidents": list(incidents or []),
            "tool_experience": list(tool_experience or []),
            "consumption_events": list(consumption_events or []),
            "tombstones": filtered_tombstones,
        }
        validate_records_for_sync(records, policy=sensitive_policy)
        if sensitive_policy != "allow":
            # DOC-211：与 import 档同源——三正则（16+ 值长门槛）之外的裸凭据
            # 形状（短值赋值/sk-/JWT/DSN）由 secret_redactor 形状扫描兜底。
            _validate_secret_shapes(records, policy=sensitive_policy)
        envelope = build_push_envelope(
            scope, _wire_project_id(project_id), records,
            snapshot_id=snapshot_id or uuid.uuid4().hex,
            operation_id=operation_id or f"op-{uuid.uuid4().hex}",
            base_version=0 if base_version is None else base_version,
            device_id=device_id or get_device_id(),
            client_version=current_version_str(),
        )
        # QUA-05：V2-only 传输（DOC-008 §5）。原先这里保留「base64 容器 ->
        # 裸 envelope -> 去除 client_version 的裸 envelope」三级降级级联，
        # 用于适配旧代服务端。**这是破坏性变更**：降级层已移除，服务端若不
        # 接受容器形态就直接失败并暴露错误，不再静默重试旧形态。
        # 移除理由：降级会掩盖服务端不兼容（表现为偶发成功而非明确报错），
        # 且最坏情况下一次 push 会产生 3 次写请求。
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        container = {
            "transport": "base64-json",
            "payload_b64": base64.b64encode(raw).decode("ascii"),
        }
        return self._request("POST", self.scope_path(scope, project_id), container)

    def import_knowledge_bundle(self, bundle: dict[str, Any], *, mode: str = "merge",
                                sensitive_policy: str = "allow") -> ReportResult:
        """Import one validated V2 JSON bundle through the explicit import API.

        DOC-211：upload 与 sync 同吃一档策略——block 补凭据形状扫描（堵旧
        3 正则弱闸放行 bare sk-/JWT/DSN 的旁路）；redact 脱敏后重算并回写
        manifest_hash 再发网；allow 原样放行。
        """
        if mode not in {"merge", "local_authoritative"}:
            raise ValueError("mode 必须是 merge 或 local_authoritative")
        if sensitive_policy not in SENSITIVE_POLICIES:
            raise ValueError(
                f"未知敏感策略: {sensitive_policy!r}（允许 {'/'.join(SENSITIVE_POLICIES)}）")
        from core.cloud_sync.cloud_schema import validate_push_envelope
        validate_push_envelope(bundle)
        payload = dict(bundle)
        payload["mode"] = mode
        scope = str(bundle.get("scope") or "")
        project_id = str(bundle.get("project_id") or "") if scope == "project" else ""
        if sensitive_policy == "allow":
            return self._request("POST", self._import_path(scope, project_id), payload)
        if sensitive_policy == "redact":
            prepared = prepare_records_for_sync(
                bundle["records"], policy="redact")
            payload["records"] = prepared["records"]
            # redact 改内容后 envelope 的 manifest_hash 必须重算回写，
            # 否则服务端按 manifest==hash(records) 直接拒绝（审计#1-1）。
            from core.cloud_sync.cloud_schema import manifest_hash as _manifest_hash
            payload["manifest_hash"] = _manifest_hash(prepared["records"])
            return self._request("POST", self._import_path(scope, project_id), payload)
        validate_records_for_sync(bundle["records"])
        _validate_secret_shapes(bundle["records"])
        return self._request("POST", self._import_path(scope, project_id), payload)

    # ---- 错题卡质量闭环（联调方案 §2.4）----
    def post_log_report(self, body: dict) -> ReportResult:
        """内测期诊断日志上报（POST /api/v1/agent/log-reports，登录认证 + 每日限额）。

        body: {device_id, client_version, kind, payload}；payload 为日志文档
        （JSONL 原文或文本）。服务端按账号每日限额（后台可配置）。
        """
        return self._request("POST", "/api/v1/agent/log-reports", body)

    def post_lesson_hit_feedback(self, feedbacks: list[dict], *, run_id: str = "",
                                 project_name: str = "") -> ReportResult:
        """扫描收尾批量回报错题卡质量计数（POST /api/v1/agent/lessons/hit-feedback）。

        feedbacks: [{lesson_id, outcome}]，outcome ∈ hit|use|false_positive|miss|rejected；
        同一 (lesson_id, outcome) 去重；单请求上限 100 条（分批由调用方负责）。
        鉴权与云同步同会话语义（bxk_ API key / web JWT）。
        """
        dedup: dict[tuple[str, str], dict] = {}
        for index, item in enumerate(feedbacks or []):
            lesson_id = str((item or {}).get("lesson_id", "")).strip()
            outcome = str((item or {}).get("outcome", "")).strip()
            if not lesson_id or outcome not in LESSON_FEEDBACK_OUTCOMES:
                raise ValueError(
                    f"hit-feedback[{index}] 参数无效: lesson_id 必填，outcome ∈ "
                    f"{'|'.join(LESSON_FEEDBACK_OUTCOMES)}"
                )
            dedup[(lesson_id, outcome)] = {"lesson_id": lesson_id, "outcome": outcome}
        if not dedup:
            raise ValueError("hit-feedback feedbacks 不能为空")
        if len(dedup) > LESSON_FEEDBACK_MAX_ITEMS:
            raise ValueError(f"hit-feedback 单请求最多 {LESSON_FEEDBACK_MAX_ITEMS} 条")
        payload: dict = {"feedbacks": list(dedup.values())}
        if run_id:
            payload["run_id"] = str(run_id)
        if project_name:
            payload["project_name"] = str(project_name)
        return self._request("POST", "/api/v1/agent/lessons/hit-feedback", payload)

    def pull_knowledge(self, scope: str, project_id: str = "", cursor: str = "",
                       page_size: int = DEFAULT_PAGE, *,
                       device_id: str | None = None) -> ReportResult:
        """V2 pull：参数只走 query；cursor 是服务端 opaque token，原样透传。"""
        if scope not in {"global", "project"}:
            raise ValueError("scope 必须是 global 或 project")
        if scope == "project" and not str(project_id).strip():
            raise ValueError("project scope 必须显式提供 project_id")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 500:
            raise ValueError("page_size 必须在 1..500")
        if not isinstance(cursor, str):
            raise ValueError("cursor 必须是字符串（opaque token 原样透传）")
        if len(cursor) > MAX_CURSOR_LENGTH:
            raise ValueError(f"cursor 长度不能超过 {MAX_CURSOR_LENGTH}")
        requested_device_id = get_device_id() if device_id is None else device_id
        if not isinstance(requested_device_id, str):
            raise ValueError("device_id 必须是字符串")
        params = {
            "schema_version": SCHEMA_VERSION, "cursor": cursor,
            "page_size": page_size,
        }
        if requested_device_id:
            params["device_id"] = requested_device_id
        result = self._request(
            "GET", self.scope_path(scope, project_id), None,
            params=params,
            extra_headers={"X-BugX-Transport": "base64-json"},
        )
        # The device cursor is a server-side resume hint. If a deployment
        # rotated the server signing key, the hint can be invalid even though
        # the client's local cursor is empty. Retry the initial pull without
        # that hint so the server can return a fresh baseline cursor. Explicit
        # cursors remain fail-closed and are never retried this way.
        if not result.ok and not cursor and requested_device_id:
            err = self.sync_error(result)
            if err.code == "SYNC_INVALID_ENVELOPE" and "cursor" in err.message:
                baseline_params = dict(params)
                baseline_params.pop("device_id", None)
                result = self._request(
                    "GET", self.scope_path(scope, project_id), None,
                    params=baseline_params,
                )
        if result.ok:
            data = self._data(result)
            # 无损容器：payload_b64 = envelope 原文 base64 + sha256 完整性校验。
            if isinstance(data, dict) and data.get("transport") == "base64-json":
                raw = base64.b64decode(data.get("payload_b64") or "")
                payload = json.loads(raw)
                expected = data.get("content_sha256")
                if expected and expected != "sha256:" + hashlib.sha256(raw).hexdigest():
                    return ReportResult(ok=False, status=result.status,
                                        data=result.data, error="pull 容器 sha256 校验失败")
                # ReportResult 是 frozen dataclass：用 replace 派生解码后的结果
                result = dataclasses.replace(result, data=payload)
            try:
                validate_pull_envelope(self._data(result), scope=scope, project_id=project_id)
            except SchemaError as exc:
                return ReportResult(ok=False, status=result.status,
                                    data=result.data, error=str(exc))
        return result

    def ack_knowledge(self, scope: str, project_id: str = "", cursor: str = "",
                      *, device_id: str | None = None) -> ReportResult:
        """ACK a durably imported pull cursor so the server may run GC."""
        if scope not in {"global", "project"}:
            raise ValueError("scope 必须是 global 或 project")
        if scope == "project" and not str(project_id).strip():
            raise ValueError("project scope 必须显式提供 project_id")
        if scope == "global" and project_id not in {"", None}:
            raise ValueError("global scope 不得提供 project_id")
        if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
            raise ValueError("cursor 必须是非空字符串且长度不超过 512")
        requested_device_id = device_id or get_device_id()
        if not isinstance(requested_device_id, str) or not requested_device_id:
            raise ValueError("device_id 必须是非空字符串")
        body = {
            "schema_version": SCHEMA_VERSION,
            "scope": scope,
            "project_id": _wire_project_id(project_id) if scope == "project" else None,
            "cursor": cursor,
            "device_id": requested_device_id,
        }
        return self._request("POST", self._ack_path(scope, project_id), body)

    def prefetch_knowledge(
        self,
        classification: Mapping[str, Any] | None = None,
        *,
        language: str = "",
        forms: list[str] | None = None,
        surfaces: list[str] | None = None,
        frameworks: list[str] | None = None,
        routing_tags: dict[str, list[str]] | None = None,
        tools: list[str] | None = None,
        tool_names: list[str] | None = None,
        limit: int = 20,
        project_id: int | str | None = None,
    ) -> ReportResult:
        """DOC-041 Phase 2: 定向预拉错题、经验、推荐工具链与预热规则。

        Agent 在任务启动前按分类画像一次性拉取最相关的错题与经验，
        无需执行全量同步。服务端支持 GET 查询参数与 POST JSON 体。
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1..100 之间")

        payload: dict[str, Any] = {}
        active_tools = tools if tools is not None else tool_names
        if classification and isinstance(classification, (dict, Mapping)):
            if not language:
                language = str(classification.get("primary_language") or classification.get("language") or "")
            if forms is None and "forms" in classification:
                forms = classification.get("forms")
            if surfaces is None and "surfaces" in classification:
                surfaces = classification.get("surfaces")
            if frameworks is None and "frameworks" in classification:
                frameworks = classification.get("frameworks")
            if routing_tags is None and "routing_tags" in classification:
                routing_tags = classification.get("routing_tags")
            if active_tools is None:
                active_tools = classification.get("tools") or classification.get("tool_names")

        if language and isinstance(language, str) and language.strip() and language.strip() != "unknown":
            payload["language"] = language.strip()
        if forms and isinstance(forms, (list, tuple, set)):
            payload["forms"] = [str(f).strip() for f in forms if str(f).strip()]
        if surfaces and isinstance(surfaces, (list, tuple, set)):
            payload["surfaces"] = [str(s).strip() for s in surfaces if str(s).strip()]
        if frameworks and isinstance(frameworks, (list, tuple, set)):
            payload["frameworks"] = [str(fw).strip() for fw in frameworks if str(fw).strip()]
        if active_tools and isinstance(active_tools, (list, tuple, set)):
            payload["tools"] = [str(t).strip() for t in active_tools if str(t).strip()]
        if routing_tags and isinstance(routing_tags, dict):
            payload["routing_tags"] = {
                str(k).strip(): [str(v).strip() for v in vals if str(v).strip()]
                for k, vals in routing_tags.items()
                if isinstance(vals, (list, tuple, set))
            }
        payload["limit"] = limit
        if project_id is not None and str(project_id).strip():
            try:
                pid_int = int(project_id)
                if pid_int > 0:
                    payload["project_id"] = pid_int
            except (ValueError, TypeError):
                pass

        return self._request("POST", KNOWLEDGE_PREFETCH_PATH, payload)

    def sync_status(self) -> ReportResult:
        return self._request("GET", SYNC_STATUS_PATH)

    def scope_path(self, scope: str, project_id: str) -> str:
        if scope == "global":
            return f"{AGENT_API_PREFIX}/global/knowledge"
        value = str(project_id)
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ValueError("project_id 必须是服务端返回的正整数 ID")
        return f"{AGENT_API_PREFIX}/projects/{value}/knowledge"

    def _import_path(self, scope: str, project_id: str) -> str:
        return self.scope_path(scope, project_id) + "/import"

    def _ack_path(self, scope: str, project_id: str) -> str:
        """Return the post-import ACK endpoint for one V2 knowledge scope."""
        return self.scope_path(scope, project_id) + "/ack"

    def _profile_path(self, project_id: str) -> str:
        value = str(project_id)
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ValueError("project_id 必须是服务端返回的正整数 ID")
        return f"{AGENT_API_PREFIX}/projects/{value}/profile"

    # ---- 版本检查与 CLI 包下载（Phase 2 自动更新）----

    def check_version(self, current_version: str = "", channel: str = "") -> ReportResult:
        """GET /api/v1/agent/version — 查询最新版本元数据。

        当传入 current_version 时，服务端额外返回 update_available 和
        update_required 布尔字段。channel 用于选择发布通道（stable /
        alpha）；留空使用服务端默认（stable）通道。此端点公开无需认证，仅限流。
        """
        params = {}
        if current_version:
            params["current"] = current_version
        if channel and channel.strip():
            params["channel"] = channel.strip()
        return self._request("GET", VERSION_CHECK_PATH, None,
                             auth=False, retry_auth=False, params=params or None)

    @staticmethod
    def _is_allowed_agent_download_url(url: str) -> bool:
        """升级包最终 URL 白名单：https 任意主机；http 仅限回环地址。

        明文 HTTP 到远端会被中间人篡改升级包，一律拒绝；但回环地址
        （127.0.0.1/::1/localhost）不出机器，本地镜像与测试夹具依赖它。
        """
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "https":
            return True
        if parts.scheme == "http":
            host = (parts.hostname or "").strip("[]").lower()
            return host in {"127.0.0.1", "::1", "localhost"}
        return False

    def download_package(self, dest: Path, *, expected_sha256: str = "",
                         expected_version: str = "") -> tuple[bool, str]:
        """GET /api/v1/agent/download — 下载 CLI 包到指定路径。

        返回 (success, message)。下载后做 SHA-256 校验（必须提供）。
        如果 download_url 指向外部地址，服务端会返回 302 重定向，此方法
        跟随重定向下载，但最终 URL 必须仍是 https://（回环地址例外，
        见 `_is_allowed_agent_download_url`），否则拒绝。

        dest: 目标文件路径（含文件名）。
        expected_sha256: 下载后校验哈希（必填，缺失即拒绝下载）。
        expected_version: 仅用于日志/提示，不影响逻辑。
        """
        expected_sha256 = str(expected_sha256 or "").strip().lower()
        if not expected_sha256:
            return False, "缺少 SHA-256 校验值，拒绝下载"
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            return False, "下载失败: expected_sha256 必须是 64 位 SHA-256 hex"

        url = self.base + DOWNLOAD_PATH
        # 跟随重定向（版本检查端点可能配置了外部 PyPI 镜像地址）
        opener = _build_redirect_opener(self.base)
        headers = {"Accept": "application/octet-stream", **client_headers()}
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = opener.open(req, timeout=self.timeout)
            try:
                # 升级包绝不能从明文 HTTP 端点拉取；服务端 302 跳转外部
                # 镜像时，最终 URL 必须仍是 https://，否则整体拒绝。
                final_url = ""
                geturl = getattr(resp, "geturl", None)
                if callable(geturl):
                    final_url = str(geturl() or "")
                if not self._is_allowed_agent_download_url(final_url):
                    return False, (
                        f"下载失败: 最终 URL 非 HTTPS，拒绝下载: {final_url}"
                    )
                headers = getattr(resp, "headers", None)
                content_length = None
                if headers is not None:
                    raw_length = headers.get("Content-Length")
                    if raw_length:
                        try:
                            content_length = int(raw_length)
                        except (TypeError, ValueError):
                            return False, "下载失败: Content-Length 无效"
                        if content_length < 0 or content_length > MAX_AGENT_PACKAGE_BYTES:
                            return False, "下载失败: CLI 包超过允许大小"

                chunks: list[bytes] = []
                total = 0
                while True:
                    try:
                        chunk = resp.read(AGENT_PACKAGE_READ_CHUNK)
                    except TypeError:  # 简化测试响应仍可能只接受无参 read()
                        chunk = resp.read()
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_AGENT_PACKAGE_BYTES:
                        return False, "下载失败: CLI 包超过允许大小"
                    chunks.append(chunk)
                if content_length is not None and total != content_length:
                    return False, "下载失败: 响应长度与 Content-Length 不一致"
                data = b"".join(chunks)
            finally:
                resp.close()
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
            return False, f"下载失败: HTTP {exc.code} {message_from_response(raw, exc)}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"下载失败: {exc}"

        # SHA-256 校验
        if expected_sha256:
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected_sha256:
                return False, (
                    f"SHA-256 校验失败: 期望 {expected_sha256}，实际 {actual}"
                )

        # 写入目标文件
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write_nofollow(dest, data)
        except (OSError, ValueError) as exc:
            return False, f"写入文件失败: {exc}"

        version_note = f" (v{expected_version})" if expected_version else ""
        return True, f"CLI 包已下载到 {dest}{version_note}"


def current_version_str() -> str:
    """返回当前引擎/CLI 版本号，用于 User-Agent（自包含零反向导入）。"""
    return client_version()

