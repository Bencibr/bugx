#!/usr/bin/env python3
"""Endpoint latency probing, route caching, and cloud configuration routing."""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from core.infra.client_metadata import client_user_agent
from core.cloud_sync.network_transport import build_no_redirect_open
from core.cloud_sync.sensitive_filter import SYNC_POLICY_VOCABS
from core.report_engine.report_client import atomic_write_nofollow
from core.secure_atomic import AtomicWriteError, reject_symlink_components

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://bugx.dev"
DEFAULT_BASE_CANDIDATES = ("https://bugx.com.cn", "https://bugx.dev")
CLOUD_CONFIG_PATH = os.environ.get(
    "BUGX_CLOUD_CONFIG", str(Path.home() / ".bugx" / "cloud-config.json")
)
CLOUD_ROUTE_CACHE_PATH = os.environ.get(
    "BUGX_CLOUD_ROUTE_CACHE", str(Path.home() / ".bugx" / "cloud-route-cache.json")
)
DEFAULT_ROUTE_TTL = float(os.environ.get("BUGX_CLOUD_ROUTE_TTL", "86400"))
DEFAULT_PROBE_TIMEOUT = float(os.environ.get("BUGX_PROBE_TIMEOUT", "0.8"))
_MEMOIZED_BASE_URL: str | None = None
def validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("云端地址不能为空")
    base_url = base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("云端地址必须是带 host 的 http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("云端地址不能包含用户名、密码、查询参数或 fragment")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程云端地址必须使用 HTTPS；HTTP 只允许本机测试服务")
    normalized = base_url.rstrip("/")
    # API 根防护（联调 I-1）：PATH 常量已含 /api/v1 前缀，调用方若传入
    # 带 /api/v1 后缀的 base 会拼出 /api/v1/api/v1/...（静默 401）。
    # 在入口剥离一次，两种传参习惯（裸域名 / 完整 API 根）都落到正确路径。
    if normalized.endswith("/api/v1"):
        normalized = normalized[: -len("/api/v1")]
    return normalized


def cloud_config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    import sys
    mod = sys.modules.get("core.cloud_sync.cloud_client")
    fallback = getattr(mod, "CLOUD_CONFIG_PATH", CLOUD_CONFIG_PATH) if mod else CLOUD_CONFIG_PATH
    configured = os.environ.get("BUGX_CLOUD_CONFIG") or fallback
    return Path(configured).expanduser()


def load_cloud_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取非敏感的云端配置；配置文件保存 base_url 和 disable_version_check。"""
    p = cloud_config_path(path)
    try:
        reject_symlink_components(p)
    except AtomicWriteError as exc:
        raise ValueError(f"云端配置路径不安全: {p}") from exc
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"云端配置无法读取: {p}") from exc
    if not isinstance(data, dict):
        raise ValueError("云端配置必须是 JSON 对象")
    result: dict[str, Any] = {}
    raw = data.get("base_url")
    if raw is not None:
        result["base_url"] = validate_base_url(raw)
    # Persisted version-check flag (Phase 2 auto-update feature).
    dvc = data.get("disable_version_check")
    if isinstance(dvc, bool):
        result["disable_version_check"] = dvc
    # DOC-211：同步内容策略三键；非法值 fail-closed（绝不静默回退默认）。
    for key, vocab in SYNC_POLICY_VOCABS.items():
        raw = data.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str) or raw not in vocab:
            raise ValueError(
                f"云端配置 {key} 非法: {raw!r}（允许值: {'/'.join(vocab)}）；"
                f"可执行 bugx config set cloud.{key} <合法值> 修复，"
                f"或检查配置文件 {cloud_config_path(path)}")
        result[key] = raw
    return result


def cloud_route_cache_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    import sys
    mod = sys.modules.get("core.cloud_sync.cloud_client")
    fallback = getattr(mod, "CLOUD_ROUTE_CACHE_PATH", CLOUD_ROUTE_CACHE_PATH) if mod else CLOUD_ROUTE_CACHE_PATH
    configured = os.environ.get("BUGX_CLOUD_ROUTE_CACHE") or fallback
    return Path(configured).expanduser()


def load_route_cache(path: str | Path | None = None) -> dict[str, Any]:
    """读取路由测速持久化缓存。"""
    p = cloud_route_cache_path(path)
    try:
        reject_symlink_components(p)
    except (AtomicWriteError, OSError, ValueError):
        return {}
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_route_cache(
    selected_base_url: str,
    latency_ms: float | None = None,
    candidate_latencies: dict[str, float] | None = None,
    path: str | Path | None = None,
) -> None:
    """持久化路由测速优选结果。"""
    p = cloud_route_cache_path(path)
    try:
        reject_symlink_components(p)
    except (AtomicWriteError, OSError, ValueError):
        return
    payload = {
        "schema_version": 1,
        "selected_base_url": selected_base_url,
        "tested_at": time.time(),
        "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
        "candidate_latencies": candidate_latencies or {},
    }
    try:
        atomic_write_nofollow(
            p, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            parent_mode=0o700,
        )
    except (OSError, AtomicWriteError):
        pass


def clear_route_cache(path: str | Path | None = None) -> None:
    """清除路由测速缓存。"""
    global _MEMOIZED_BASE_URL
    _MEMOIZED_BASE_URL = None
    p = cloud_route_cache_path(path)
    try:
        reject_symlink_components(p)
        if p.exists():
            p.unlink()
    except (OSError, AtomicWriteError, ValueError):
        pass


def probe_candidate_latency(
    candidate: str,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    urlopen: Callable[..., Any] | None = None,
) -> float | None:
    """探测单个端点的延迟（毫秒）；返回 None 表示不可达或超时。"""
    normalized = validate_base_url(candidate)
    target = f"{normalized}/api/v1/health"
    req = urllib.request.Request(
        target,
        headers={"User-Agent": client_user_agent()},
        method="HEAD",
    )
    start = time.perf_counter()
    try:
        if urlopen is not None:
            resp = urlopen(req, timeout=timeout)
            if hasattr(resp, "close"):
                resp.close()
        else:
            opener = build_no_redirect_open(normalized)
            with opener(req, timeout=timeout) as resp:
                _ = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        # HTTP 状态码（如 200、401、403、404 等）证明服务端链路已通并响应，计为有效可达
        if hasattr(exc, "close"):
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # 网络异常、不可达或超时
        return None
    return (time.perf_counter() - start) * 1000.0


def select_lowest_latency_endpoint(
    candidates: tuple[str, ...] | list[str] | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    cache_path: str | Path | None = None,
    probe_fn: Callable[[str, float], float | None] | None = None,
) -> str:
    """并发竞速探测候选端点，优选最低延迟节点；失败或离线时回退到 DEFAULT_BASE。"""
    cand_list: list[str] = list(candidates or DEFAULT_BASE_CANDIDATES)
    if DEFAULT_BASE not in cand_list:
        cand_list.append(DEFAULT_BASE)

    # 离线或跳过探测门禁
    if os.environ.get("BUGX_SKIP_ROUTE_PROBE") in {"1", "true", "yes"} or \
       os.environ.get("BUGX_OFFLINE") in {"1", "true", "yes"}:
        return DEFAULT_BASE

    # 检查持久化缓存有效性
    cache = load_route_cache(cache_path)
    tested_at = cache.get("tested_at")
    selected_url = cache.get("selected_base_url")
    ttl = float(os.environ.get("BUGX_CLOUD_ROUTE_TTL", str(DEFAULT_ROUTE_TTL)))
    if (
        isinstance(selected_url, str)
        and selected_url in cand_list
        and isinstance(tested_at, (int, float))
        and (time.time() - tested_at) < ttl
    ):
        return selected_url

    probe = probe_fn or probe_candidate_latency
    latencies: dict[str, float] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(cand_list))) as executor:
        future_to_cand = {
            executor.submit(probe, cand, timeout): cand
            for cand in cand_list
        }
        for future in concurrent.futures.as_completed(future_to_cand):
            cand = future_to_cand[future]
            try:
                lat = future.result()
                if lat is not None:
                    latencies[cand] = lat
            except Exception:
                pass

    if latencies:
        best_candidate = min(latencies, key=latencies.get)
        save_route_cache(
            best_candidate,
            latency_ms=latencies[best_candidate],
            candidate_latencies=latencies,
            path=cache_path,
        )
        return best_candidate

    # 所有候选均超时或失败时，回退到 DEFAULT_BASE
    save_route_cache(
        DEFAULT_BASE,
        latency_ms=None,
        candidate_latencies={},
        path=cache_path,
    )
    return DEFAULT_BASE


def configured_base_url(path: str | Path | None = None, *, refresh: bool = False) -> str:
    """按环境变量 > 持久化配置 > 低延迟竞速优选 > 默认值解析云端地址。"""
    global _MEMOIZED_BASE_URL
    env_base = os.environ.get("BUGX_CLOUD_API_BASE")
    if env_base:
        return validate_base_url(env_base)
    configured = load_cloud_config(path).get("base_url")
    if configured:
        return configured
    if not refresh and path is None and _MEMOIZED_BASE_URL is not None:
        return _MEMOIZED_BASE_URL
    selected = select_lowest_latency_endpoint(cache_path=path)
    if path is None:
        _MEMOIZED_BASE_URL = selected
    return selected


def cloud_base_url_status(path: str | Path | None = None) -> dict[str, str | bool]:
    """返回当前生效地址及来源，不返回任何凭证。"""
    env_base = os.environ.get("BUGX_CLOUD_API_BASE")
    if env_base:
        return {"base_url": validate_base_url(env_base), "source": "environment"}
    configured = load_cloud_config(path).get("base_url")
    if configured:
        return {"base_url": configured, "source": "config"}
    selected = configured_base_url(path)
    source = "auto" if selected in DEFAULT_BASE_CANDIDATES else "default"
    return {"base_url": selected, "source": source}


def set_cloud_base_url(base_url: str, path: str | Path | None = None) -> str:
    """校验并持久化非敏感的云端 Base URL。"""
    global _MEMOIZED_BASE_URL
    _MEMOIZED_BASE_URL = None
    normalized = validate_base_url(base_url)
    p = cloud_config_path(path)
    try:
        reject_symlink_components(p)
    except AtomicWriteError as exc:
        raise ValueError(f"云端配置路径不安全: {p}") from exc
    # Preserve unrelated supported settings (notably the startup version
    # check switch and DOC-211 sync policy keys) when changing only the
    # endpoint.
    try:
        existing = load_cloud_config(p) if p.exists() else {}
    except ValueError:
        # 坏值状态下仍允许修复 base_url：raw 读出可解析的已知键，非法键不搬运
        # （修复后下次 load 会对残余坏值继续报错并给出出口）。
        existing = {}
        raw = read_cloud_config_raw(p)
        for key, vocab in SYNC_POLICY_VOCABS.items():
            value = raw.get(key)
            if isinstance(value, str) and value in vocab:
                existing[key] = value
        dvc = raw.get("disable_version_check")
        if isinstance(dvc, bool):
            existing["disable_version_check"] = dvc
    payload = {"schema_version": 1, "base_url": normalized}
    for key, value in existing.items():
        if key in SYNC_POLICY_VOCABS or key == "disable_version_check":
            payload[key] = value
    atomic_write_nofollow(
        p, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        parent_mode=0o700,
    )
    return normalized


def read_cloud_config_raw(path: Path) -> dict[str, Any]:
    """容忍坏 JSON 的 raw 读取（DOC-211：修复路径必须在任何文件状态下可用）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_cloud_config(updates: dict[str, str]) -> dict[str, str]:
    """DOC-211：raw-read 改写云端同步策略键（保留未知键，坏值状态可自修复）。

    只接受 SYNC_POLICY_VOCABS 内的键，值必须命中词表，否则 ValueError。
    """
    for key, value in updates.items():
        vocab = SYNC_POLICY_VOCABS.get(key)
        if vocab is None:
            raise ValueError(
                f"不支持的云端配置键: {key}（支持: {'、'.join(sorted(SYNC_POLICY_VOCABS))}）")
        if value not in vocab:
            raise ValueError(f"云端配置 {key} 非法: {value!r}（允许值: {'/'.join(vocab)}）")
    p = cloud_config_path()
    try:
        reject_symlink_components(p)
    except AtomicWriteError as exc:
        raise ValueError(f"云端配置路径不安全: {p}") from exc
    data = read_cloud_config_raw(p)
    data.update(updates)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_nofollow(
        p, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
        parent_mode=0o700,
    )
    return {key: data[key] for key in updates}


def clear_cloud_base_url(path: str | Path | None = None) -> None:
    """删除持久化地址；环境变量覆盖仍需由调用方单独清除。"""
    global _MEMOIZED_BASE_URL
    _MEMOIZED_BASE_URL = None
    p = cloud_config_path(path)
    try:
        reject_symlink_components(p)
    except AtomicWriteError as exc:
        raise ValueError(f"云端配置路径不安全: {p}") from exc
    try:
        p.unlink()
    except FileNotFoundError:
        pass

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



# Compatibility aliases
_validate_base_url = validate_base_url
_cloud_config_path = cloud_config_path
_cloud_route_cache_path = cloud_route_cache_path
_read_cloud_config_raw = read_cloud_config_raw
