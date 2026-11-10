#!/usr/bin/env python3
"""One-way, privacy-gated attack-data flywheel client (DOC-126).

This module intentionally has no dependency on the five-record Cloud Sync
schema.  Its only durable state is a separate bounded SQLite outbox.
"""

from __future__ import annotations

from contextlib import closing
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping

from core.governance.secret_redactor import SecretRedactor

try:
    from core.infra.client_metadata import client_headers, tls_context
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.infra.client_metadata import client_headers, tls_context

MAX_OUTBOX_RECORDS = 1000
MAX_OUTBOX_BYTES = 50 * 1024 * 1024
MAX_AUDIT_BYTES = 10 * 1024 * 1024
AUDIT_BACKUPS = 3
FLYWHEEL_PATH = "/api/v1/agent/telemetry/flywheel"
QUALITY_TIERS = {"tier-1": 3, "tier-2": 2, "tier-3": 1, "TIER-1": 3, "TIER-2": 2, "TIER-3": 1}


def _bugx_root() -> Path:
    return Path(os.environ.get("BUGX_HOME", str(Path.home() / ".bugx"))).expanduser()


def default_outbox_path() -> Path:
    return Path(os.environ.get("BUGX_FLYWHEEL_OUTBOX", str(_bugx_root() / "telemetry" / "flywheel_outbox.sqlite3"))).expanduser()


def default_audit_path() -> Path:
    return Path(os.environ.get("BUGX_FLYWHEEL_AUDIT", str(_bugx_root() / "telemetry" / "egress_audit.log"))).expanduser()


def _stored_data_sharing() -> bool:
    # 单一真源：与 cloud_onboard.sync_layers / auto_sync 共用 consent 判定，
    # 禁止在这里另写一套读取逻辑（双轨实现会让撤销在两处产生不同结论）。
    from core.cloud_sync.consent import sharing_allowed
    return sharing_allowed()


def _stored_base_url() -> str | None:
    path = Path(os.environ.get("BUGX_CLOUD_CONFIG", str(_bugx_root() / "cloud-config.json"))).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if isinstance(data, dict):
            val = data.get("base_url") or data.get("api_base")
            if isinstance(val, str) and val.strip():
                return val.strip()
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return None


def _stored_auth_token() -> str | None:
    path = Path(os.environ.get("BUGX_CLOUD_CREDENTIALS", str(_bugx_root() / "cloud-credentials.json"))).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if isinstance(data, dict):
            token = data.get("token") or data.get("access_token") or data.get("api_key")
            if isinstance(token, str) and token.strip():
                return token.strip()
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return None


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def merkle_root(nodes: Iterable[Any]) -> str:
    """Calculate a deterministic SHA-256 Merkle root for static topology."""
    leaves = [hashlib.sha256(_canonical(node)).digest() for node in nodes]
    leaves.sort()
    if not leaves:
        return "sha256:" + hashlib.sha256(b"").hexdigest()
    while len(leaves) > 1:
        if len(leaves) % 2:
            leaves.append(leaves[-1])
        leaves = [hashlib.sha256(leaves[i] + leaves[i + 1]).digest() for i in range(0, len(leaves), 2)]
    return "sha256:" + leaves[0].hex()


def quality_tier(value: str | int | None) -> int:
    if isinstance(value, int):
        return max(1, min(3, value))
    return QUALITY_TIERS.get(str(value or "tier-3"), 1)


def quality_tier_name(value: str | int | None) -> str:
    rank = quality_tier(value)
    return {3: "tier-1", 2: "tier-2", 1: "tier-3"}.get(rank, "tier-3")


def _extract_tar_safely(
    archive: tarfile.TarFile, destination: Path, *, reserved_names: frozenset[str] = frozenset(),
) -> None:
    """Use tarfile's data filter where available, with a safe 3.11.0 fallback."""
    root = destination.resolve()
    from core.ledger import archive_ops

    member_limit = MAX_OUTBOX_RECORDS + 2  # outbox records plus manifest and NDJSON members
    inflated_limit = archive_ops.MAX_SEGMENT_INFLATED_BYTES
    member_count = 0
    inflated_bytes = 0
    targets: list[tuple[tarfile.TarInfo, Path]] = []
    for member in archive:
        member_count += 1
        if member_count > member_limit:
            raise ValueError(f"bundle member count exceeds import limit ({member_limit})")
        if member.size < 0:
            raise ValueError("unsafe bundle member")
        inflated_bytes += member.size
        if inflated_bytes > inflated_limit:
            raise ValueError(
                f"bundle decompressed content exceeds import limit ({inflated_limit} bytes)"
            )
        name = member.name
        posix_name = PurePosixPath(name)
        windows_name = PureWindowsPath(name)
        if (
            not name
            or "\x00" in name
            or "\\" in name
            or posix_name.is_absolute()
            or windows_name.is_absolute()
            or windows_name.drive
            or ".." in posix_name.parts
            or member.issym()
            or member.islnk()
            or member.isdev()
            or member.isfifo()
            or not (member.isdir() or member.isfile())
        ):
            raise ValueError("unsafe bundle member")
        if not posix_name.parts:
            if member.isdir():
                continue
            raise ValueError("unsafe bundle member")
        if len(posix_name.parts) == 1 and posix_name.parts[0] in reserved_names:
            raise ValueError("unsafe bundle member")
        target = root.joinpath(*posix_name.parts)
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("unsafe bundle member")
        targets.append((member, target))

    data_filter = getattr(tarfile, "data_filter", None)
    if callable(data_filter):
        try:
            archive.extractall(destination, filter=data_filter)
        except (OSError, tarfile.TarError) as exc:
            raise ValueError("unsafe bundle member") from exc
        return

    # Python 3.11.0–3.11.3 has no data_filter. Materialize only ordinary
    # directories/files after validating every path; never follow archive links.
    for member, target in targets:
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise ValueError("unsafe bundle member")
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


@dataclass(frozen=True)
class DPOPreferencePair:
    prompt: str
    chosen: tuple[Any, ...]
    rejected: tuple[Any, ...]
    metadata: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "chosen": list(self.chosen),
            "rejected": list(self.rejected),
            "metadata": dict(self.metadata),
        }


def build_dpo_preference_pair(
    prompt: str,
    chosen: Iterable[Any],
    rejected: Iterable[Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the wire-compatible DPO preference shape with margin and difficulty metrics (DES-19)."""
    rejected_list = list(rejected or [])
    meta = dict(metadata or {})
    depth = int(meta.get("backtrack_depth", len(rejected_list) + 1))
    has_neg = bool(meta.get("has_negative_control", False))
    if "preference_margin" not in meta:
        meta["preference_margin"] = min(1.0, round(0.5 + 0.25 * min(depth, 2) + (0.25 if has_neg else 0.0), 2))
    if meta.get("obstacle_types") or (depth >= 2 and any("WAF" in str(r) or "403" in str(r) for r in rejected_list)):
        meta.setdefault("is_hard_example", True)
    pair = {
        "prompt": str(prompt),
        "chosen": list(chosen),
        "rejected": rejected_list,
        "metadata": meta,
    }
    pair["metadata"].setdefault("has_rejected", bool(pair["rejected"]))
    return pair


class OutboxFullError(RuntimeError):
    pass


class FlywheelOutbox:
    """Independent FIFO SQLite queue with quality-aware bounded eviction."""

    def __init__(self, path: str | Path | None = None, *, max_records: int = MAX_OUTBOX_RECORDS, max_bytes: int = MAX_OUTBOX_BYTES) -> None:
        self.path = Path(path or default_outbox_path()).expanduser()
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload BLOB NOT NULL,
                tier INTEGER NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_quality ON outbox(tier, id)")
            conn.commit()

    def count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    def bytes(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM outbox").fetchone()[0])

    def _evict(self, conn: sqlite3.Connection, required: int = 0) -> int:
        removed = 0
        while True:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM outbox").fetchone()
            over = int(row[0]) + 1 > self.max_records or int(row[1]) + required > self.max_bytes
            if not over:
                break
            candidate = conn.execute("SELECT id, size FROM outbox WHERE tier < 3 ORDER BY tier ASC, id ASC LIMIT 1").fetchone()
            if candidate is None:
                raise OutboxFullError("outbox quota exhausted by Tier-1 records")
            conn.execute("DELETE FROM outbox WHERE id = ?", (candidate[0],))
            removed += 1
        return removed

    def enqueue(self, payload: Mapping[str, Any], *, tier: str | int | None = None) -> int:
        raw = _canonical(payload)
        # DES-18: 内部透明 gzip 压缩存储，大幅提升 Outbox 实际有效容量
        compressed = gzip.compress(raw, compresslevel=6)
        if len(compressed) > self.max_bytes:
            raise OutboxFullError("single flywheel record exceeds outbox quota")
        rank = quality_tier(tier or payload.get("quality_tier"))
        # DES-11: Tier-1 黄金资产必须具备 replay_proof_hash 或执行证据，缺少时降级为 Tier-2 防死锁
        if rank >= 3:
            has_proof = bool(
                payload.get("replay_proof_hash")
                or (payload.get("metrics") or {}).get("replay_proof_hash")
                or payload.get("replay_proof")
            )
            if not has_proof:
                rank = 2
        with closing(self._connect()) as conn:
            self._evict(conn, len(compressed))
            cursor = conn.execute(
                "INSERT INTO outbox(payload, tier, size, created_at) VALUES (?, ?, ?, ?)",
                (compressed, rank, len(compressed), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def items(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id, payload, tier, attempts, created_at FROM outbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            raw_blob = row[1]
            if isinstance(raw_blob, (bytes, bytearray)) and raw_blob.startswith(b"\x1f\x8b"):
                payload_data = json.loads(gzip.decompress(raw_blob))
            elif isinstance(raw_blob, (bytes, bytearray)):
                payload_data = json.loads(raw_blob.decode("utf-8"))
            else:
                payload_data = json.loads(raw_blob)
            result.append({"id": row[0], "payload": payload_data, "tier": row[2], "attempts": row[3], "created_at": row[4]})
        return result

    def remove(self, item_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM outbox WHERE id = ?", (item_id,))
            conn.commit()

    def remove_batch(self, item_ids: Iterable[int]) -> None:
        ids_list = list(item_ids)
        if not ids_list:
            return
        with closing(self._connect()) as conn:
            placeholders = ",".join("?" for _ in ids_list)
            conn.execute(f"DELETE FROM outbox WHERE id IN ({placeholders})", ids_list)
            conn.commit()

    def increment_attempts(self, item_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (item_id,))
            conn.commit()

    def clear(self) -> int:
        with closing(self._connect()) as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            conn.execute("DELETE FROM outbox")
            conn.commit()
            conn.execute("VACUUM")
            return count


class FlywheelUploader:
    """Privacy-gated uploader and offline shuttle coordinator."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        outbox_path: str | Path | None = None,
        audit_path: str | Path | None = None,
        urlopen: Callable[..., Any] | None = None,
        auth_token: str | None = None,
        data_sharing: bool | None = None,
        privacy_strict: bool | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 8.0,
    ) -> None:
        configured_endpoint = os.environ.get("BUGX_TELEMETRY_ENDPOINT")
        raw_target = (
            base_url
            or configured_endpoint
            or os.environ.get("BUGX_CLOUD_API_BASE")
            or _stored_base_url()
            or "https://bugx.dev"
        ).rstrip("/")
        parsed = urllib.parse.urlsplit(raw_target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("telemetry endpoint must be an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("telemetry endpoint cannot contain credentials, query, or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote telemetry endpoint must use HTTPS")

        if parsed.path.endswith(FLYWHEEL_PATH) or (parsed.path not in ("", "/") and not base_url and configured_endpoint):
            self._endpoint = raw_target
            self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        elif parsed.path in ("", "/"):
            self.base_url = f"{parsed.scheme}://{parsed.netloc}"
            self._endpoint = self.base_url + FLYWHEEL_PATH
        else:
            self.base_url = f"{parsed.scheme}://{parsed.netloc}"
            self._endpoint = raw_target

        self.outbox = FlywheelOutbox(outbox_path)
        self.audit_path = Path(audit_path or default_audit_path()).expanduser()
        self._urlopen = urlopen or urllib.request.urlopen
        self.auth_token = auth_token if auth_token is not None else _stored_auth_token()
        self.data_sharing = (
            os.environ.get("BUGX_ACCEPT_DATA_SHARING") == "1" or _stored_data_sharing()
            if data_sharing is None else data_sharing
        )
        self.privacy_strict = (os.environ.get("BUGX_PRIVACY_STRICT") == "1") if privacy_strict is None else privacy_strict
        self.sleep = sleep
        self.timeout = timeout
        self._connect_failures = 0
        self._client_failures = 0
        self._circuit_until = 0.0
        self._redactor = SecretRedactor(test_context=bool(os.environ.get("BUGX_TEST_CONTEXT") == "1"))

    def close(self) -> None:
        if getattr(self, "_redactor", None):
            self._redactor.close()

    def __enter__(self) -> "FlywheelUploader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _redact(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        clean = self._redactor.redact_records(dict(payload))
        if self.privacy_strict:
            metrics = clean.get("metrics") if isinstance(clean, dict) else None
            clean = {
                "schema_version": 1,
                "event_type": clean.get("event_type", "flywheel") if isinstance(clean, dict) else "flywheel",
                "quality_tier": clean.get("quality_tier", "tier-3") if isinstance(clean, dict) else "tier-3",
                "topology_hash": clean.get("topology_hash") if isinstance(clean, dict) else None,
                "metrics": metrics if isinstance(metrics, dict) else {},
                "privacy": "strict",
            }
        return clean

    def enqueue(self, payload: Mapping[str, Any], *, tier: str | int | None = None) -> int | None:
        if not self.data_sharing:
            return None
        clean = self._redact(payload)
        return self.outbox.enqueue(clean, tier=tier or clean.get("quality_tier"))

    queue = enqueue

    def enqueue_graph(
        self,
        static_topology: Iterable[Any],
        exploit_delta: Mapping[str, Any] | Iterable[Any],
        *,
        quality: str | int = "tier-2",
        source_code: str | None = None,
        dpo_pair: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
        include_topology: bool = False,
    ) -> int | None:
        topology = list(static_topology)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "event_type": "attack_graph",
            "quality_tier": quality_tier_name(quality),
            "topology_hash": merkle_root(topology),
            "exploit_delta": exploit_delta,
            "metrics": dict(metrics or {}),
        }
        if include_topology:
            payload["static_topology"] = topology
        if source_code is not None:
            payload["source_code"] = source_code
        if dpo_pair:
            payload["dpo_preference"] = dpo_pair.as_dict() if isinstance(dpo_pair, DPOPreferencePair) else dict(dpo_pair)
        return self.enqueue(payload, tier=quality)

    def _url(self) -> str:
        return self._endpoint

    def _post(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any] | None, str | None]:
        body = _canonical(payload)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **client_headers(),
        }
        if self.auth_token:
            headers["Authorization"] = "Bearer " + self.auth_token
        req = urllib.request.Request(self._url(), data=body, headers=headers, method="POST")
        try:
            try:
                if self._url().startswith("https://"):
                    response = self._urlopen(req, timeout=self.timeout, context=tls_context(base_url=self.base_url))
                else:
                    response = self._urlopen(req, timeout=self.timeout)
            except TypeError:
                response = self._urlopen(req, timeout=self.timeout)
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            raw = response.read()
            data = json.loads(raw.decode("utf-8")) if raw else None
            return status, data if isinstance(data, dict) else None, None
        except urllib.error.HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeError, json.JSONDecodeError):
                data = None
            return int(exc.code), data if isinstance(data, dict) else None, str(exc)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            return 0, None, str(exc)

    @staticmethod
    def _needs_topology(status: int, data: Mapping[str, Any] | None) -> bool:
        text = json.dumps(data or {}, ensure_ascii=False).upper()
        return status == 404 and "NEED_TOPOLOGY" in text

    def _audit(self, payload: Mapping[str, Any], status: int, error: str | None = None) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "payload_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
            "topology_hash": payload.get("topology_hash"),
            "redacted_fields": [key for key, value in payload.items() if "REDACTED" in json.dumps(value, ensure_ascii=False)],
            "status": status,
            "error": error,
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        self._rotate_audit()

    def _rotate_audit(self) -> None:
        if not self.audit_path.exists() or self.audit_path.stat().st_size <= MAX_AUDIT_BYTES:
            return
        for index in range(AUDIT_BACKUPS, 0, -1):
            source = self.audit_path.with_name(self.audit_path.name + f".{index}")
            if index == AUDIT_BACKUPS and source.exists():
                source.unlink()
            elif source.exists():
                source.rename(self.audit_path.with_name(self.audit_path.name + f".{index + 1}"))
        self.audit_path.rename(self.audit_path.with_name(self.audit_path.name + ".1"))

    def _flush_single_item(self, item: dict[str, Any], topology_needed: list[dict[str, Any]]) -> bool:
        payload = item["payload"]
        last_status = 0
        last_error: str | None = None
        data = None
        for attempt in range(4):
            last_status, data, last_error = self._post(payload)
            self._audit(payload, last_status, last_error)
            if 200 <= last_status < 300:
                self.outbox.remove(item["id"])
                self._connect_failures = self._client_failures = 0
                return True
            if last_status == 0:
                self._connect_failures += 1
            elif last_status >= 400:
                # 404 NEED_TOPOLOGY is a normal Merkle handshake signal, not a failure
                if not self._needs_topology(last_status, data):
                    self._client_failures += 1
            retryable = last_status == 0 or last_status == 429 or last_status >= 500
            if not retryable or attempt == 3:
                break
            self.sleep(min(60.0, 2.0**attempt))
        else:
            last_status = 0
        self.outbox.increment_attempts(item["id"])
        if self._needs_topology(last_status, data):
            topology_needed.append(payload)
        return False

    def flush(self, *, limit: int = 100, batch_size: int = 1) -> dict[str, Any]:
        """Flush queued records to the telemetry flywheel (DES-06, DES-18)."""
        if not self.data_sharing or time.monotonic() < self._circuit_until:
            return {"sent": 0, "remaining": self.outbox.count(), "circuit_open": True}
        sent = 0
        topology_needed: list[dict[str, Any]] = []
        items = self.outbox.items(limit)

        if batch_size > 1 and len(items) > 1:
            for i in range(0, len(items), batch_size):
                chunk = items[i : i + batch_size]
                batch_payload = {"batch": [item["payload"] for item in chunk]}
                last_status, data, last_error = self._post(batch_payload)
                if 200 <= last_status < 300:
                    self.outbox.remove_batch([item["id"] for item in chunk])
                    for item in chunk:
                        self._audit(item["payload"], last_status, None)
                    self._connect_failures = self._client_failures = 0
                    sent += len(chunk)
                else:
                    for item in chunk:
                        if self._flush_single_item(item, topology_needed):
                            sent += 1
                        if self._connect_failures >= 10 or self._client_failures >= 5:
                            self._circuit_until = time.monotonic() + 30 * 60
                            break
                if self._connect_failures >= 10 or self._client_failures >= 5:
                    self._circuit_until = time.monotonic() + 30 * 60
                    break
        else:
            for item in items:
                if self._flush_single_item(item, topology_needed):
                    sent += 1
                if self._connect_failures >= 10 or self._client_failures >= 5:
                    self._circuit_until = time.monotonic() + 30 * 60
                    break

        return {
            "sent": sent,
            "remaining": self.outbox.count(),
            "circuit_open": time.monotonic() < self._circuit_until,
            "topology_needed": topology_needed,
        }

    upload = flush

    def upload_graph(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Queue a graph delta, then add one full topology retry if requested."""
        static_topology = list(args[0]) if args else list(kwargs.pop("static_topology"))
        kwargs["include_topology"] = False
        self.enqueue_graph(static_topology, *args[1:], **kwargs)
        result = self.flush(limit=1)
        if result.get("topology_needed"):
            payload = result["topology_needed"][0]
            for item in self.outbox.items(limit=self.outbox.max_records):
                if item["payload"] == payload:
                    self.outbox.remove(item["id"])
                    break
            payload = dict(payload)
            payload["static_topology"] = static_topology
            self.enqueue(payload, tier=payload.get("quality_tier"))
            result["topology_retry_queued"] = True
        else:
            result["topology_retry_queued"] = False
        return result

    def inspect(self, *, latest: bool = False) -> list[dict[str, Any]]:
        if not self.audit_path.is_file():
            return []
        lines = self.audit_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if latest:
            lines = lines[-1:]
        return [json.loads(line) for line in lines if line.strip()]

    def clear_outbox(self) -> int:
        return self.outbox.clear()

    def export_bundle(self, output: str | Path, *, passphrase: str | None = None) -> Path:
        if not passphrase:
            passphrase = os.environ.get("BUGX_TELEMETRY_PASSPHRASE")
        if not passphrase and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            import getpass
            try:
                passphrase = getpass.getpass("请输入离线穿梭包加密口令: ")
            except (EOFError, OSError):
                passphrase = None
        if not passphrase:
            raise ValueError("passphrase is required for encrypted telemetry export")
        items = self.outbox.items(limit=self.outbox.max_records)
        manifest = {"schema_version": 1, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "count": len(items)}
        with tempfile.TemporaryDirectory(prefix="bugx-telemetry-") as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_bytes(_canonical(manifest))
            with (root / "records.ndjson").open("w", encoding="utf-8") as handle:
                for item in items:
                    handle.write(json.dumps(item["payload"], ensure_ascii=False, sort_keys=True) + "\n")
            plain = root / "bundle.tar.gz"
            with tarfile.open(plain, "w:gz") as archive:
                archive.add(root / "manifest.json", arcname="manifest.json")
                archive.add(root / "records.ndjson", arcname="records.ndjson")
            encrypted = _encrypt(plain.read_bytes(), passphrase)
        destination = Path(output).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encrypted)
        return destination

    def import_bundle(self, bundle: str | Path, *, passphrase: str | None = None) -> int:
        if not passphrase:
            raise ValueError("passphrase is required for encrypted telemetry import")
        from core.ledger import archive_ops

        bundle_path = Path(bundle)
        compressed_limit = archive_ops.MAX_SEGMENT_BYTES
        with bundle_path.open("rb") as handle:
            encrypted = handle.read(compressed_limit + 1)
        if len(encrypted) > compressed_limit:
            raise ValueError(f"bundle compressed size exceeds import limit ({compressed_limit} bytes)")
        raw = _decrypt(encrypted, passphrase)
        with tempfile.TemporaryDirectory(prefix="bugx-telemetry-import-") as tmp:
            archive_path = Path(tmp) / "bundle.tar.gz"
            archive_path.write_bytes(raw)
            tar_path = Path(tmp) / "bundle.tar"
            inflated_limit = archive_ops.MAX_SEGMENT_INFLATED_BYTES
            total = 0
            try:
                with gzip.open(archive_path, "rb") as source, tar_path.open("wb") as output:
                    while chunk := source.read(1 << 20):
                        total += len(chunk)
                        if total > inflated_limit:
                            raise ValueError(
                                f"bundle decompressed content exceeds import limit ({inflated_limit} bytes)"
                            )
                        output.write(chunk)
            except (EOFError, OSError) as exc:
                raise ValueError("invalid telemetry bundle archive") from exc
            with tarfile.open(tar_path, "r:") as archive:
                _extract_tar_safely(
                    archive, Path(tmp),
                    reserved_names=frozenset({archive_path.name, tar_path.name}),
                )
            records = (Path(tmp) / "records.ndjson").read_text(encoding="utf-8").splitlines()
        for line in records:
            if line.strip():
                self.enqueue(json.loads(line))
        return len([line for line in records if line.strip()])


def _encrypt(data: bytes, passphrase: str) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError as exc:
        raise RuntimeError("cryptography is required for encrypted telemetry bundles") from exc
    salt = os.urandom(16)
    nonce = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    key = kdf.derive(passphrase.encode("utf-8"))
    return b"BUGX-TEL1" + salt + nonce + AESGCM(key).encrypt(nonce, data, b"bugx-telemetry")


def _decrypt(data: bytes, passphrase: str) -> bytes:
    if not data.startswith(b"BUGX-TEL1") or len(data) < 37:
        raise ValueError("invalid encrypted telemetry bundle")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError as exc:
        raise RuntimeError("cryptography is required for encrypted telemetry bundles") from exc
    salt, nonce, ciphertext = data[9:25], data[25:37], data[37:]
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    key = kdf.derive(passphrase.encode("utf-8"))
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, b"bugx-telemetry")
    except Exception as exc:
        raise ValueError("telemetry bundle authentication failed") from exc


__all__ = [
    "AUDIT_BACKUPS", "DPOPreferencePair", "FLYWHEEL_PATH", "FlywheelOutbox", "FlywheelUploader",
    "MAX_AUDIT_BYTES", "MAX_OUTBOX_BYTES", "MAX_OUTBOX_RECORDS", "OutboxFullError",
    "build_dpo_preference_pair", "default_audit_path", "default_outbox_path",
    "merkle_root", "quality_tier", "quality_tier_name",
]
