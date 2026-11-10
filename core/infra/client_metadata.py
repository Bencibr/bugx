#!/usr/bin/env python3
"""Shared BugX client metadata and outbound request headers."""

from __future__ import annotations

import importlib.metadata
import os
import re
import ssl
import sys
from urllib.parse import urlsplit
from pathlib import Path

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MARKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_CHANNELS = frozenset({"alpha", "stable"})
_MARKERS = frozenset({"alpha", "dev", "uat", "stable"})
_DEV_TLS_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TLS_ENV_STRICT = frozenset({"prod", "production"})
_TLS_ENV_RELAXED = frozenset({"dev", "development", "local", "test"})
_TLS_ENV_WARNED = False
# 稳定版客户端访问开发端点时**不得**静默放弃校验（DOC-110 设计项 5）：主机白名单只是
# 必要条件，还必须有明确的开发意图 —— dev 标记，或下面这个显式开关。真正推荐的做法
# 仍然是把自签 CA 放进 BUGX_CA_BUNDLE，那会保留完整校验与主机名校验。
_DEV_TLS_NO_VERIFY_ENV = "BUGX_DEV_TLS_NO_VERIFY"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _dev_tls_no_verify() -> bool:
    """是否**显式**同意对白名单内的开发端点放弃证书校验。"""
    return os.environ.get(_DEV_TLS_NO_VERIFY_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


def _read_metadata(metadata_path: str | Path | None = None) -> dict[str, str]:
    if metadata_path:
        path = Path(metadata_path)
    else:
        path = Path(__file__).with_name("metadata.txt")
        if not path.is_file():
            path = Path(__file__).resolve().parent.parent / "metadata.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _valid(value: object, pattern: re.Pattern[str], allowed: frozenset[str] | None = None) -> str | None:
    text = str(value or "").strip().lower()
    if not pattern.fullmatch(text):
        return None
    if allowed is not None and text not in allowed:
        return None
    return text


def client_version(metadata_path: str | Path | None = None) -> str:
    metadata = _read_metadata(metadata_path)
    value = _valid(metadata.get("bugx_version"), _VERSION_RE)
    if value:
        return value
    try:
        return importlib.metadata.version("bugx-cli")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def release_channel(metadata_path: str | Path | None = None) -> str:
    metadata = _read_metadata(metadata_path)
    value = _valid(os.environ.get("BUGX_RELEASE_CHANNEL") or metadata.get("bugx_channel"),
                   _MARKER_RE, _CHANNELS)
    return value or "stable"


def client_marker(metadata_path: str | Path | None = None) -> str:
    metadata = _read_metadata(metadata_path)
    value = _valid(os.environ.get("BUGX_CLIENT_MARKER") or metadata.get("bugx_marker"),
                   _MARKER_RE, _MARKERS)
    if value:
        return value
    return release_channel(metadata_path)


def client_user_agent(metadata_path: str | Path | None = None) -> str:
    return (
        f"BugX-Client/{client_version(metadata_path)} "
        f"(marker={client_marker(metadata_path)}; channel={release_channel(metadata_path)})"
    )


def client_headers(metadata_path: str | Path | None = None) -> dict[str, str]:
    """Return non-secret headers shared by every BugX outbound request."""
    version = client_version(metadata_path)
    marker = client_marker(metadata_path)
    channel = release_channel(metadata_path)
    return {
        "User-Agent": client_user_agent(metadata_path),
        "X-BugX-Client-Version": version,
        "X-BugX-Client-Marker": marker,
        "X-BugX-Release-Channel": channel,
    }


def _tls_env_mode() -> str:
    """解析 TLS 校验档位：``strict`` / ``default`` / ``relaxed``。

    - ``BUGX_ENV=prod|production`` → ``strict``：证书与主机名都必须校验，
      **任何**开发例外（dev 标记、``BUGX_DEV_TLS_NO_VERIFY``）都不生效；
    - ``BUGX_ENV=dev|development|local|test`` → ``relaxed``：开发意图默认成立，
      但降级范围仍被 ``_DEV_TLS_HOSTS`` 白名单限制；
    - 未设置 → ``default``：默认校验证书，只有「白名单主机 + 显式开发意图」
      才降级；
    - 无法识别的取值 → 按 ``strict`` 处理（fail-closed），并提示一次。
    """
    raw = os.environ.get("BUGX_ENV", "").strip().lower()
    if not raw:
        return "default"
    if raw in _TLS_ENV_STRICT:
        return "strict"
    if raw in _TLS_ENV_RELAXED:
        return "relaxed"
    global _TLS_ENV_WARNED
    if not _TLS_ENV_WARNED:
        _TLS_ENV_WARNED = True
        print(
            f"  ⚠ 未知的 BUGX_ENV={raw!r}，按最严格的证书校验处理"
            "（可用值：prod/production、dev/development/local/test）",
            file=sys.stderr,
        )
    return "strict"


def tls_context(metadata_path: str | Path | None = None,
                base_url: str | None = None) -> ssl.SSLContext:
    """Build the shared TLS policy for BugX outbound requests.

    默认安全：证书校验默认开启，只有同时满足「命中 ``_DEV_TLS_HOSTS`` 白名单」和
    「存在显式开发意图」才降级为不校验。显式配置 ``BUGX_CA_BUNDLE`` 最优先，
    并把用户 CA **追加**到系统信任库（保留公共 CA 端点的校验能力）。

    历史缺陷（P2-1）：早期实现只在 ``BUGX_ENV=prod`` 时才校验证书，未声明环境
    的普通安装默认对**任意主机**关闭校验，等于给所有出站请求留下 MITM 面。
    """
    bundle = os.environ.get("BUGX_CA_BUNDLE", "").strip()
    if bundle:
        path = Path(bundle).expanduser()
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"BUGX_CA_BUNDLE 不是可读 CA 文件: {path}")
        # CPython 只在**没有** cafile 时才 load_default_certs()，直接把 cafile 传给
        # create_default_context 会丢掉系统信任库：配了自签 CA 之后反而连不上公共 CA
        # 签发的正式端点。这里先取系统默认，再追加用户的 CA，两边都能校验证书。
        context = ssl.create_default_context()
        try:
            context.load_verify_locations(cafile=str(path))
        except (PermissionError, OSError) as exc:
            # 用户显式声明了 CA 却加载失败时**不得**静默退回系统信任库：那会让
            # 「配了自签 CA」表现为莫名其妙的证书校验失败，且掩盖配置错误本身。
            raise ValueError(
                f"BUGX_CA_BUNDLE 无法加载 CA 文件: {path}（{exc}）"
            ) from exc
        return context
    context = ssl.create_default_context()
    mode = _tls_env_mode()
    if mode == "strict":
        # prod（以及任何无法识别的声明）不接受任何降级例外。
        return context
    hostname = urlsplit(str(base_url or "")).hostname or ""
    # 降级需要两个条件同时成立：
    #   1) 主机在白名单内 —— 只有开发端点；
    #   2) 有显式开发意图 —— BUGX_ENV=dev 系列、dev 标记，或
    #      BUGX_DEV_TLS_NO_VERIFY=1 这个 bootstrap 开关。
    intent = (
        mode == "relaxed"
        or client_marker(metadata_path) == "dev"
        or _dev_tls_no_verify()
    )
    if hostname in _DEV_TLS_HOSTS and intent:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context
