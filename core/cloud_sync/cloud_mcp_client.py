#!/usr/bin/env python3
"""core/cloud_sync/cloud_mcp_client.py - 统一的云端 bugx-cloud MCP 网关调用器（云端优先 + 本地兜底）。"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _contract_version() -> str:
    try:
        from core.infra.build_info import BASE_VERSION

        m = re.match(r"([0-9]+\.[0-9]+\.[0-9]+)", str(BASE_VERSION))
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.1.6"


def is_cloud_mcp_allowed(project_root: Path | str | None = None) -> bool:
    """隐私与离线门禁：显式禁用云端或项目处于 local 模式时，禁止外发请求。"""
    if os.environ.get("BUGX_DISABLE_CLOUD", "").strip() in {"1", "true", "yes"}:
        return False
    if project_root is not None:
        onboard_path = Path(project_root) / ".bugx" / "cloud-onboard.json"
        if onboard_path.is_file() and not onboard_path.is_symlink():
            try:
                data = json.loads(onboard_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("state") == "local":
                    return False
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
    return True


def resolve_cloud_mcp_url(explicit: str | None = None) -> str:
    """解析并校验 bugx-cloud 远端 Streamable-HTTP MCP 端点 URL。"""
    from core.cloud_sync.endpoint_router import load_cloud_config, validate_base_url

    contract_ver = _contract_version()
    raw = str(explicit or os.environ.get("BUGX_CLOUD_MCP_URL") or "").strip()
    if raw:
        base_part, _, query = raw.partition("?")
        normalized = validate_base_url(base_part)
        if "/api/v1/mcp/cloud" not in normalized:
            normalized = f"{normalized}/api/v1/mcp/cloud"
        return f"{normalized}?{query}" if query else f"{normalized}?contract_version={contract_ver}"

    cfg = load_cloud_config()
    base = str(cfg.get("base_url") or "https://bugx.dev").strip()
    normalized_base = validate_base_url(base)
    return f"{normalized_base}/api/v1/mcp/cloud?contract_version={contract_ver}"


def _post_jsonrpc(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any] | None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "bugx-cloud-client",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status < 200 or resp.status >= 300:
                return None
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None


def probe_cloud_mcp_ping(url: str, *, timeout: float = 1.0) -> bool:
    """发送 JSON-RPC ping 探测远程 bugx-cloud MCP 端点真实可用性。"""
    resp = _post_jsonrpc(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=timeout)
    return isinstance(resp, dict) and "result" in resp and "error" not in resp


def call_cloud_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    project_root: Path | str | None = None,
    explicit_url: str | None = None,
    timeout: float = 1.5,
) -> dict[str, Any] | None:
    """调用云端 bugx-cloud MCP 工具；不可达、超时或禁用云端时返回 None 以触发本地兜底。"""
    if not is_cloud_mcp_allowed(project_root):
        return None
    try:
        url = resolve_cloud_mcp_url(explicit_url)
    except Exception:
        return None

    resp = _post_jsonrpc(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        timeout=timeout,
    )
    if not isinstance(resp, dict) or "error" in resp:
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        return sc
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
    return None
