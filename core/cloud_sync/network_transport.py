#!/usr/bin/env python3
"""Network diagnostics, TLS configuration, and low-level HTTP/curl transport for cloud client."""
from __future__ import annotations

import http.client
import io
import ipaddress
import logging
import os
import re
import shlex
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from core.infra.client_metadata import tls_context as client_tls_context

logger = logging.getLogger(__name__)

def is_private_target_host(host: str) -> bool:
    """Check if host is a local/private network target (excluding loopback)."""
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    if host.endswith(".local"):
        return True

    def _is_private_addr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if addr.is_loopback:
            return False
        if addr.version == 4:
            return addr.is_link_local or any(
                addr in ipaddress.ip_network(net)
                for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            )
        return any(
            addr in ipaddress.ip_network(net)
            for net in ("fc00::/7", "fe80::/10")
        )

    try:
        address = ipaddress.ip_address(host)
        return _is_private_addr(address)
    except ValueError:
        pass

    # 自建云端服务经常采用私网域名，需通过 DNS 判定解析出的 IP
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        for _, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                if _is_private_addr(ipaddress.ip_address(ip_str)):
                    return True
            except ValueError:
                continue
    except (socket.gaierror, OSError):
        pass

    return False


def diagnose_network_error(exc: Exception, target_url: str) -> dict[str, Any]:
    """Classify macOS private-network failures using the system curl as a probe."""
    original = {"is_local_network_blocked": False, "error": str(exc)}
    if sys.platform != "darwin":
        return original

    # 1. 快速检查异常是否包含 Errno 65 / 51 或不可达信息（非网络阻断类异常立即早退）
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    blocked_error = False
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        text = str(current)
        if (
            getattr(current, "errno", None) in {51, 65}
            or re.search(r"\berrno[\s:=]*(?:51|65)\b", text, re.IGNORECASE)
            or "no route to host" in text.lower()
            or "network is unreachable" in text.lower()
        ):
            blocked_error = True
            break
        for attribute in ("reason", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if isinstance(nested, BaseException):
                pending.append(nested)

    if not blocked_error:
        return original

    # 2. 检查目标是否属于私有/局域网目标（含私网 IP、.local 域名、以及解析为私网 IP 的自定义域名）
    try:
        parsed_target = urllib.parse.urlsplit(target_url)
        host = (parsed_target.hostname or "").rstrip(".").lower()
        port = parsed_target.port
    except (TypeError, ValueError):
        return original

    if not is_private_target_host(host):
        return original
    if parsed_target.scheme not in {"http", "https"}:
        return original

    probe_host = f"[{host}]" if ":" in host else host
    probe_netloc = f"{probe_host}:{port}" if port is not None else probe_host
    probe_url = urllib.parse.urlunsplit((parsed_target.scheme, probe_netloc, "/", "", ""))

    try:
        # 只传 scheme/host/port，避免 curl argv 暴露 URL 凭据或重放原 API 请求。
        # 只读 HEAD 探针兼容 macOS 系统 curl；自签证书或非 200 HTTP 响应不影响 remote_ip 连通证据。
        probe = subprocess.run(
            [
                "/usr/bin/curl", "-q", "-sS", "--insecure", "--noproxy", "*",
                "--connect-timeout", "1.0", "--max-time", "1.5",
                "--head", "--output", "/dev/null", "--write-out", "%{remote_ip}", probe_url,
            ],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        probe_connected = bool((probe.stdout or "").strip()) or probe.returncode == 0
    except subprocess.TimeoutExpired as probe_error:
        probe_stdout = probe_error.stdout or probe_error.output or ""
        if isinstance(probe_stdout, bytes):
            probe_stdout = probe_stdout.decode("utf-8", errors="replace")
        probe_connected = bool(probe_stdout.strip())
        if not probe_connected:
            return {
                "is_local_network_blocked": False,
                "code": "LOCAL_NETWORK_PROBE_UNAVAILABLE",
                "message": "系统 curl 探活超时，无法判定是解释器权限受限还是局域网目标不可达。",
            }
    except OSError as probe_error:
        return {
            "is_local_network_blocked": False,
            "code": "LOCAL_NETWORK_PROBE_UNAVAILABLE",
            "message": f"无法运行系统 curl 探活，无法判定局域网连接原因：{probe_error}",
        }

    if probe_connected:
        return {
            "is_local_network_blocked": True,
            "code": "LOCAL_NETWORK_BLOCKED",
            "message": (
                "检测到 Python 解释器本地网络特权受限（macOS TCC 限制且无法在系统设置中直接勾选该命令行工具）；"
                "系统受信任通道（/usr/bin/curl）影子探活已确认端点可达，原请求未通过 curl 重试。"
            ),
        }

    return {
        "is_local_network_blocked": False,
        "code": "LOCAL_NETWORK_UNREACHABLE",
        "message": "局域网目标物理不可达（目标主机未开机、端口未监听或 IP 变动）",
    }


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def tls_context(base_url: str | None = None) -> ssl.SSLContext:
    return client_tls_context(base_url=base_url)


def build_no_redirect_open(base_url: str | None = None):
    return urllib.request.build_opener(
        NoRedirectHandler(), urllib.request.HTTPSHandler(context=tls_context(base_url))
    ).open


def build_redirect_opener(base_url: str | None = None):
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=tls_context(base_url))
    )


class CurlResponse:
    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self._body.close()


def parse_curl_response_headers(raw: bytes) -> tuple[int, http.client.HTTPMessage]:
    for block in reversed(re.split(rb"\r?\n\r?\n", raw)):
        lines = block.splitlines()
        if not lines:
            continue
        status_line = re.fullmatch(rb"HTTP/\S+[ \t]+(\d{3})(?:[ \t]+(.*))?", lines[0])
        if status_line is None:
            continue
        headers = http.client.parse_headers(io.BytesIO(
            b"\r\n".join(lines[1:]) + b"\r\n\r\n"
        ))
        return int(status_line.group(1)), headers
    raise ValueError("system curl returned no HTTP response headers")


def trusted_curl_request(req: urllib.request.Request, timeout: float) -> CurlResponse:
    """Repeat one already-built request through macOS's system curl."""
    context = tls_context(req.full_url)
    command = [
        "/usr/bin/curl", "--disable", "--silent", "--show-error",
        "--noproxy", "*", "--max-time", str(timeout),
    ]
    if context.verify_mode == ssl.CERT_NONE:
        command.append("--insecure")
    else:
        ca_bundle = os.environ.get("BUGX_CA_BUNDLE", "").strip()
        if ca_bundle:
            command.extend(("--ca-native", "--cacert", str(Path(ca_bundle).expanduser())))

    for name, value in req.header_items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(
            char in value for char in "\r\n\0"
        ):
            raise ValueError("invalid HTTP header for system curl")
        command.extend(("--header", f"{name}: {value}"))
    if req.data is not None:
        command.extend(("--data-binary", "@-"))
    command.extend(("--request", req.get_method(), "--dump-header", "/dev/stderr",
                    "--output", "-", req.full_url))

    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("DYLD_")}
    environment.pop("CURL_CA_BUNDLE", None)
    try:
        result = subprocess.run(
            command, input=req.data, capture_output=True, timeout=timeout,
            check=False, env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise urllib.error.URLError("system curl request timed out") from exc
    except OSError as exc:
        raise urllib.error.URLError("system curl is unavailable") from exc
    if result.returncode:
        detail = next((line for line in reversed(result.stderr.decode(
            "utf-8", errors="replace").splitlines()
        ) if line.startswith("curl: ")), "")
        message = f"system curl request failed{': ' + detail if detail else ''}"
        raise urllib.error.URLError(message)

    status, headers = parse_curl_response_headers(result.stderr)
    return CurlResponse(status, headers, result.stdout)



# Compatibility aliases
_is_private_target_host = is_private_target_host
_NoRedirectHandler = NoRedirectHandler
_tls_context = tls_context
_build_no_redirect_open = build_no_redirect_open
_build_redirect_opener = build_redirect_opener
_CurlResponse = CurlResponse
_parse_curl_response_headers = parse_curl_response_headers
_trusted_curl_request = trusted_curl_request
