#!/usr/bin/env python3
"""BugX 官方文档云端热同步与不可变内容寻址缓存管理器 (DOC-006)。

提供：
1. 按 ``contract_version`` 分桶的只读官方文档清单探测 (ETag / 304 < 1.5s)；
2. 不可变快照缓存 ``~/.bugx/cache/docs/<contract_version>/<sha256>.sqlite3`` 与
   ``current.json`` 原子指针切换；
3. 校验门禁：HTTPS/本机测试地址白名单 + 无重定向下载 + 字节上限 (32 MiB gz / 64 MiB SQLite)
   + SHA-256 强校 + ``PRAGMA quick_check``；
4. 单飞并发锁 ``sync.lock``（防 6 Worker 并发拉起时踩踏雪崩）与旧快照安全清扫（兼容 Windows WinError 32）；
5. 严格断网与隐私边界：``BUGX_DOCS_OFFLINE=1`` 或 ``docs_remote_enabled=false`` 零出网；
   远程请求不附带任何本地项目路径、代码片段或敏感凭据。
"""

from __future__ import annotations

from contextlib import contextmanager
import gzip
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from core.cloud_sync.endpoint_router import (
    configured_base_url,
    load_cloud_config,
    validate_base_url,
)
from core.cloud_sync.network_transport import (
    build_no_redirect_open,
    diagnose_network_error,
    trusted_curl_request,
)
from core.infra.build_info import BASE_VERSION
from core.infra.client_metadata import client_headers
from core.report_engine.report_client import atomic_write_nofollow
from core.secure_atomic import AtomicWriteError, reject_symlink_components

DOCS_SCHEMA_VERSION = "v2-trigram-section-hybrid"
DOCS_MANIFEST_PATH = "/api/v1/agent/docs/manifest"
DOCS_BUNDLE_PATH = "/api/v1/agent/docs/bundle"
DOCS_SEARCH_PATH = "/api/v1/agent/docs/search"
DOCS_FETCH_PATH = "/api/v1/agent/docs/fetch"

MAX_MANIFEST_BYTES = 256 * 1024
MAX_BUNDLE_GZ_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_SQLITE_BYTES = 64 * 1024 * 1024
MAX_REMOTE_JSON_BYTES = 256 * 1024

DEFAULT_MANIFEST_TIMEOUT = 1.5
DEFAULT_BUNDLE_TIMEOUT = 5.0
DEFAULT_SEARCH_TIMEOUT = 2.0
DEFAULT_TTL_SECONDS = 3600
DEFAULT_FAILURE_BACKOFF_SECONDS = 120

_CONTRACT_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class DocsSyncError(ValueError):
    """受控文档同步或验证失败。"""


def contract_version_of(value: str | None = None) -> str:
    """提取 X.Y.Z 契约版本号（剥离 +build / prerelease）。"""
    raw = str(value or BASE_VERSION or "0.1.3").strip()
    m = re.match(r"^v?(\d+\.\d+\.\d+)", raw)
    ver = m.group(1) if m else "0.1.3"
    if not _CONTRACT_RE.fullmatch(ver):
        raise DocsSyncError(f"非法的契约版本号: {value!r}")
    return ver


def docs_offline_disabled() -> bool:
    """检查是否因空气隔离环境变量或配置而禁用文档远端连接。"""
    env_flag = os.environ.get("BUGX_DOCS_OFFLINE", "").strip().lower()
    if env_flag in _TRUTHY:
        return True
    try:
        cfg = load_cloud_config()
    except ValueError:
        return False
    return cfg.get("docs_remote_enabled") is False


def resolve_remote_base_url(explicit_url: str | None = None) -> str:
    """解析并严格校验服务端基地址（HTTPS 或本机测试回环）。"""
    candidate = (
        str(explicit_url or "").strip()
        or os.environ.get("BUGX_DOCS_REMOTE_URL", "").strip()
        or configured_base_url()
    )
    try:
        return validate_base_url(candidate)
    except ValueError as exc:
        raise DocsSyncError(str(exc)) from exc


def docs_cache_dir(contract_version: str | None = None) -> Path:
    """返回全局不可变官方文档缓存目录。"""
    ver = contract_version_of(contract_version)
    override = os.environ.get("BUGX_DOCS_CACHE_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
    else:
        bugx_home = os.environ.get("BUGX_HOME", "").strip()
        home_root = Path(bugx_home).expanduser() if bugx_home else (Path.home() / ".bugx")
        base = home_root / "cache" / "docs"
    target = base / ver
    try:
        reject_symlink_components(target)
    except AtomicWriteError as exc:
        raise DocsSyncError(f"文档缓存路径不安全: {target}") from exc
    return target


def _read_current_state(cache_root: Path) -> dict[str, Any]:
    pointer = cache_root / "current.json"
    if not pointer.exists():
        return {}
    try:
        reject_symlink_components(pointer)
        if pointer.is_symlink() or not pointer.is_file():
            return {}
        data = json.loads(pointer.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, ValueError, AtomicWriteError):
        return {}


def _write_current_state(cache_root: Path, state: dict[str, Any]) -> None:
    pointer = cache_root / "current.json"
    cache_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_write_nofollow(
        pointer,
        json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
        parent_mode=0o700,
    )


def verify_sqlite_snapshot(db_path: Path, expected_contract: str | None = None) -> bool:
    """只读校验证明：文件非软链、PRAGMA quick_check 通过且包含双域 trigram 表。"""
    if db_path.is_symlink() or not db_path.is_file():
        return False
    try:
        reject_symlink_components(db_path)
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except (sqlite3.Error, OSError, AtomicWriteError):
        return False
    try:
        qc = conn.execute("PRAGMA quick_check").fetchone()
        if not qc or str(qc[0]).lower() != "ok":
            return False
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {"documents_meta", "documents", "document_sections", "sections_fts"}.issubset(tables):
            return False
        meta = dict(conn.execute("SELECT key, value FROM documents_meta").fetchall())
        if meta.get("schema_version") != DOCS_SCHEMA_VERSION:
            return False
        if expected_contract and meta.get("contract_version") not in {None, expected_contract}:
            return False
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def current_cached_db(contract_version: str | None = None) -> Path | None:
    """获取当前通过校验的不可变全局官方文档数据库路径（不触发网络）。"""
    ver = contract_version_of(contract_version)
    try:
        cache_root = docs_cache_dir(ver)
    except DocsSyncError:
        return None
    state = _read_current_state(cache_root)
    db_name = str(state.get("db_file") or "").strip()
    if not db_name or "/" in db_name or "\\" in db_name:
        return None
    db_path = cache_root / db_name
    if verify_sqlite_snapshot(db_path, ver):
        return db_path
    return None


@contextmanager
def _single_flight_lock(lock_path: Path) -> Iterator[bool]:
    """非阻塞文件单飞锁，避免并发 Worker 拉起同步踩踏。"""
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name == "nt":
        # Windows 排他创建锁文件，若已存在则判定竞争跳过
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            yield False
            return
        except OSError:
            yield False
            return
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            yield True
        finally:
            try:
                os.close(fd)
            finally:
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return

    import fcntl
    fd = -1
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _gc_stale_files(cache_root: Path, keep_names: set[str], pending: list[str] | None = None) -> list[str]:
    """物理清理历史版本 SQLite 快照；Windows 句柄占用时记录延迟回收。"""
    remaining: list[str] = []
    targets: set[str] = set(pending or [])
    if cache_root.is_dir():
        for entry in cache_root.iterdir():
            if entry.is_file() and entry.suffix == ".sqlite3" and entry.name not in keep_names:
                targets.add(entry.name)
    for name in sorted(targets):
        if name in keep_names:
            continue
        candidate = cache_root / name
        if not candidate.exists():
            continue
        try:
            if not candidate.is_symlink():
                candidate.unlink()
        except OSError:
            remaining.append(name)
    return remaining


def _http_request(
    method: str,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    max_bytes: int = MAX_BUNDLE_GZ_BYTES,
    urlopen: Callable[..., Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    req_headers = {"Accept": "application/json", **client_headers()}
    if headers:
        req_headers.update(headers)
    if body is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    opener = urlopen or build_no_redirect_open(url)
    try:
        resp = opener(req, timeout=timeout)
    except urllib.error.HTTPError as err:
        err_body = err.read(MAX_REMOTE_JSON_BYTES)
        err_headers = {str(k).lower(): str(v) for k, v in dict(getattr(err, "headers", {}) or {}).items()}
        return int(err.code), err_headers, err_body
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        if urlopen is not None or not diagnose_network_error(exc, url).get("is_local_network_blocked"):
            raise DocsSyncError(f"文档服务请求失败: {exc}") from exc
        resp = trusted_curl_request(req, timeout)

    status = int(getattr(resp, "status", 200) or 200)
    resp_headers = {str(k).lower(): str(v) for k, v in dict(getattr(resp, "headers", {}) or {}).items()}
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DocsSyncError(f"远端响应体超过最大安全限制 ({max_bytes} bytes)")
        chunks.append(chunk)
    return status, resp_headers, b"".join(chunks)


def _decompress_gzip_bounded(raw_gz: bytes, *, max_bytes: int = MAX_BUNDLE_SQLITE_BYTES) -> bytes:
    """防 Zip Bomb 流式限额解压，或在直接传未压缩 SQLite 时透传校验。"""
    import io
    if raw_gz.startswith(b"SQLite format 3\x00"):
        if len(raw_gz) > max_bytes:
            raise DocsSyncError("文档库体积超过安全限制")
        return raw_gz
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(raw_gz), mode="rb") as gz:
        while True:
            chunk = gz.read(65536)
            if not chunk:
                break
            if len(out) + len(chunk) > max_bytes:
                raise DocsSyncError(f"解压后的文档库超过最大限制 ({max_bytes} bytes)")
            out.extend(chunk)
    return bytes(out)


def _resolve_same_origin_url(base_url: str, target_url: str) -> str:
    """严防重定向或 manifest URL 指向非受控第三方域。"""
    raw = str(target_url or "").strip()
    if not raw:
        raise DocsSyncError("manifest 缺少 bundle_url")
    if raw.startswith("/"):
        return f"{base_url}{raw}"
    resolved = validate_base_url(raw) if "://" not in raw else raw
    base_parts = urllib.parse.urlsplit(base_url)
    target_parts = urllib.parse.urlsplit(resolved)
    if target_parts.scheme not in {"http", "https"}:
        raise DocsSyncError("拒绝非 http(s) bundle_url")
    if target_parts.scheme == "http" and (target_parts.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise DocsSyncError("拒绝非本地回环的明文 HTTP bundle_url")
    if target_parts.netloc.lower() != base_parts.netloc.lower():
        raise DocsSyncError(f"拒绝跨源下载文档包: {target_parts.netloc} != {base_parts.netloc}")
    return resolved


def sync_official_docs(
    *,
    contract_version: str | None = None,
    force: bool = False,
    offline: bool = False,
    remote_url: str | None = None,
    manifest_timeout: float = DEFAULT_MANIFEST_TIMEOUT,
    bundle_timeout: float = DEFAULT_BUNDLE_TIMEOUT,
    urlopen: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """同步 BugX 服务端官方文档快照至全局不可变缓存。"""
    ver = contract_version_of(contract_version)
    cache_root = docs_cache_dir(ver)
    state = _read_current_state(cache_root)
    existing_db = current_cached_db(ver)

    if offline or docs_offline_disabled():
        return {
            "ok": True,
            "status": "offline",
            "contract_version": ver,
            "db": str(existing_db) if existing_db else None,
            "revision": state.get("revision"),
        }

    now = time.time()
    if not force:
        backoff_until = float(state.get("backoff_until") or 0)
        if backoff_until > now:
            return {
                "ok": True,
                "status": "backoff_cached",
                "contract_version": ver,
                "db": str(existing_db) if existing_db else None,
                "revision": state.get("revision"),
            }
        ttl = max(60, int(state.get("ttl_seconds") or DEFAULT_TTL_SECONDS))
        checked_at = float(state.get("checked_at") or 0)
        if existing_db and checked_at > 0 and (checked_at + ttl) > now:
            return {
                "ok": True,
                "status": "fresh_cached",
                "contract_version": ver,
                "db": str(existing_db),
                "revision": state.get("revision"),
                "sha256": state.get("sha256"),
            }

    try:
        base_url = resolve_remote_base_url(remote_url)
    except DocsSyncError as exc:
        return {
            "ok": False,
            "status": "invalid_remote",
            "error": str(exc),
            "db": str(existing_db) if existing_db else None,
        }

    with _single_flight_lock(cache_root / "sync.lock") as acquired:
        if not acquired:
            return {
                "ok": True,
                "status": "locked_skip",
                "contract_version": ver,
                "db": str(existing_db) if existing_db else None,
                "revision": state.get("revision"),
            }

        headers: dict[str, str] = {}
        current_sha = str(state.get("sha256") or "")
        if existing_db and _SHA256_RE.fullmatch(current_sha) and not force:
            headers["If-None-Match"] = f'"{current_sha}"'

        manifest_url = f"{base_url}{DOCS_MANIFEST_PATH}?contract_version={urllib.parse.quote(ver)}"
        try:
            status, _, body = _http_request(
                "GET", manifest_url, timeout=manifest_timeout, headers=headers,
                max_bytes=MAX_MANIFEST_BYTES, urlopen=urlopen,
            )
        except DocsSyncError as exc:
            new_state = {**state, "backoff_until": now + DEFAULT_FAILURE_BACKOFF_SECONDS, "last_error": str(exc)}
            try:
                _write_current_state(cache_root, new_state)
            except (OSError, AtomicWriteError):
                pass
            return {
                "ok": False,
                "status": "degraded_fallback",
                "error": str(exc),
                "db": str(existing_db) if existing_db else None,
            }

        if status == 304 and existing_db:
            gc_pending = _gc_stale_files(cache_root, {existing_db.name}, state.get("gc_pending"))
            _write_current_state(cache_root, {
                **state, "checked_at": now, "backoff_until": 0, "last_error": None, "gc_pending": gc_pending,
            })
            return {
                "ok": True,
                "status": "not_modified",
                "contract_version": ver,
                "db": str(existing_db),
                "revision": state.get("revision"),
                "sha256": current_sha,
            }

        if status != 200:
            new_state = {
                **state,
                "backoff_until": now + DEFAULT_FAILURE_BACKOFF_SECONDS,
                "last_error": f"HTTP {status}",
            }
            try:
                _write_current_state(cache_root, new_state)
            except (OSError, AtomicWriteError):
                pass
            return {
                "ok": False,
                "status": "degraded_fallback",
                "http_status": status,
                "error": f"manifest HTTP {status}",
                "db": str(existing_db) if existing_db else None,
            }

        try:
            envelope = json.loads(body.decode("utf-8"))
            data = envelope.get("data") if isinstance(envelope, dict) and isinstance(envelope.get("data"), dict) else envelope
            if not isinstance(data, dict):
                raise DocsSyncError("manifest 响应结构非法")
            remote_ver = str(data.get("contract_version") or "").strip()
            if remote_ver != ver:
                raise DocsSyncError(f"跨版本拒绝接收: 请求 {ver}, 服务端返回 {remote_ver!r}")
            bundle_sha = str(data.get("bundle_sha256") or "").strip().lower()
            if not _SHA256_RE.fullmatch(bundle_sha):
                raise DocsSyncError("manifest bundle_sha256 格式非法")
            schema_tag = str(data.get("schema") or DOCS_SCHEMA_VERSION).strip()
            if schema_tag != DOCS_SCHEMA_VERSION:
                raise DocsSyncError(f"不兼容的文档库 schema: {schema_tag}")
            revision = int(data.get("revision") or 0)
            ttl_sec = max(60, min(86400, int(data.get("ttl_seconds") or DEFAULT_TTL_SECONDS)))
        except (ValueError, UnicodeDecodeError, TypeError, DocsSyncError) as exc:
            return {
                "ok": False,
                "status": "rejected",
                "error": str(exc),
                "db": str(existing_db) if existing_db else None,
            }

        if existing_db and current_sha == bundle_sha:
            gc_pending = _gc_stale_files(cache_root, {existing_db.name}, state.get("gc_pending"))
            _write_current_state(cache_root, {
                **state, "revision": revision, "ttl_seconds": ttl_sec,
                "checked_at": now, "backoff_until": 0, "last_error": None, "gc_pending": gc_pending,
            })
            return {
                "ok": True,
                "status": "up_to_date",
                "contract_version": ver,
                "db": str(existing_db),
                "revision": revision,
                "sha256": bundle_sha,
            }

        bundle_url = _resolve_same_origin_url(
            base_url,
            str(data.get("bundle_url") or f"{DOCS_BUNDLE_PATH}?contract_version={urllib.parse.quote(ver)}&sha256={bundle_sha}"),
        )
        try:
            b_status, _, raw_wire = _http_request(
                "GET", bundle_url, timeout=bundle_timeout, max_bytes=MAX_BUNDLE_GZ_BYTES, urlopen=urlopen,
            )
            if b_status != 200:
                raise DocsSyncError(f"bundle 下载失败: HTTP {b_status}")
            wire_digest = hashlib.sha256(raw_wire).hexdigest()
            sqlite_bytes = _decompress_gzip_bounded(raw_wire, max_bytes=MAX_BUNDLE_SQLITE_BYTES)
            sqlite_digest = hashlib.sha256(sqlite_bytes).hexdigest()
            expected_sqlite_sha = str(data.get("sqlite_sha256") or "").strip().lower()
            if expected_sqlite_sha:
                if wire_digest != bundle_sha or sqlite_digest != expected_sqlite_sha:
                    raise DocsSyncError("文档包 SHA-256 校验失败，已拒绝写入")
            elif bundle_sha not in {wire_digest, sqlite_digest}:
                raise DocsSyncError("文档包 SHA-256 校验失败，已拒绝写入")
        except DocsSyncError as exc:
            return {
                "ok": False,
                "status": "rejected",
                "error": str(exc),
                "db": str(existing_db) if existing_db else None,
            }

        cache_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        tmp_db = cache_root / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}.sqlite3"
        final_name = f"{sqlite_digest}.sqlite3"
        final_db = cache_root / final_name
        try:
            tmp_db.write_bytes(sqlite_bytes)
            if os.name != "nt":
                tmp_db.chmod(0o600)
            if not verify_sqlite_snapshot(tmp_db, ver):
                raise DocsSyncError("文档库完整性或 schema 校验未通过")
            os.replace(tmp_db, final_db)
        except Exception as exc:  # noqa: BLE001
            try:
                tmp_db.unlink(missing_ok=True)
            except OSError:
                pass
            return {
                "ok": False,
                "status": "rejected",
                "error": f"落盘校验失败: {exc}",
                "db": str(existing_db) if existing_db else None,
            }

        gc_pending = _gc_stale_files(cache_root, {final_name}, state.get("gc_pending"))
        _write_current_state(cache_root, {
            "contract_version": ver,
            "revision": revision,
            "sha256": bundle_sha,
            "sqlite_sha256": sqlite_digest,
            "db_file": final_name,
            "ttl_seconds": ttl_sec,
            "checked_at": now,
            "backoff_until": 0,
            "last_error": None,
            "source": "cloud",
            "gc_pending": gc_pending,
        })
        return {
            "ok": True,
            "status": "updated",
            "contract_version": ver,
            "db": str(final_db),
            "revision": revision,
            "sha256": bundle_sha,
            "gc_pending": gc_pending,
        }


def remote_search_docs(
    query: str,
    *,
    contract_version: str | None = None,
    limit: int = 5,
    remote_url: str | None = None,
    timeout: float = DEFAULT_SEARCH_TIMEOUT,
    urlopen: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """透传官方公开文档检索请求（严格剥离本地 project_id 与业务上下文）。"""
    if docs_offline_disabled():
        raise DocsSyncError("已启用离线隔离模式，禁止远端文档检索")
    ver = contract_version_of(contract_version)
    base_url = resolve_remote_base_url(remote_url)
    payload = json.dumps({
        "contract_version": ver,
        "query": str(query or "")[:512],
        "limit": max(1, min(int(limit), 20)),
    }, ensure_ascii=False).encode("utf-8")
    status, _, body = _http_request(
        "POST", f"{base_url}{DOCS_SEARCH_PATH}",
        timeout=timeout, body=payload, max_bytes=MAX_REMOTE_JSON_BYTES, urlopen=urlopen,
    )
    if status != 200:
        raise DocsSyncError(f"远端文档检索失败: HTTP {status}")
    parsed = json.loads(body.decode("utf-8"))
    data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else parsed
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise DocsSyncError("远端文档检索响应格式非法")
    return data


def remote_fetch_doc(
    document_id: str,
    *,
    contract_version: str | None = None,
    section: str = "",
    remote_url: str | None = None,
    timeout: float = DEFAULT_SEARCH_TIMEOUT,
    urlopen: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """透传官方公开文档分段读取请求。"""
    if docs_offline_disabled():
        raise DocsSyncError("已启用离线隔离模式，禁止远端文档读取")
    ver = contract_version_of(contract_version)
    base_url = resolve_remote_base_url(remote_url)
    payload = json.dumps({
        "contract_version": ver,
        "document_id": str(document_id or "")[:256],
        "section": str(section or "")[:256],
    }, ensure_ascii=False).encode("utf-8")
    status, _, body = _http_request(
        "POST", f"{base_url}{DOCS_FETCH_PATH}",
        timeout=timeout, body=payload, max_bytes=MAX_REMOTE_JSON_BYTES, urlopen=urlopen,
    )
    if status != 200:
        raise DocsSyncError(f"远端文档读取失败: HTTP {status}")
    parsed = json.loads(body.decode("utf-8"))
    data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else parsed
    if not isinstance(data, dict):
        raise DocsSyncError("远端文档读取响应格式非法")
    return data
