"""Namespace-isolated SQLite memory storage with FTS5 recall."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from core.governance.secret_redactor import redact_text


_QUERY_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SQLITE_INTEGER_MAX = (1 << 63) - 1


class MemoryStore:
    """Store keyed memories and search each namespace independently.

    ``db_path`` may be a filesystem path or ``":memory:"``. File-backed
    stores use SQLite WAL mode; SQLite's in-memory databases use their native
    ``memory`` journal mode because WAL is not supported for ``:memory:``.
    Framework memories are redacted before either the content table or FTS
    index sees their key, content, or tags.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str] = ":memory:",
        *,
        max_items_per_namespace: int | None = None,
    ) -> None:
        if isinstance(max_items_per_namespace, bool) or (
            max_items_per_namespace is not None
            and (not isinstance(max_items_per_namespace, int) or max_items_per_namespace < 1)
        ):
            raise ValueError("max_items_per_namespace must be a positive integer or None")
        self.max_items_per_namespace = max_items_per_namespace

        try:
            path = os.fspath(db_path)
        except TypeError as exc:
            raise TypeError("db_path must be a string or path-like value") from exc
        if not isinstance(path, str):
            raise TypeError("db_path must resolve to a string path")
        if not path:
            raise ValueError("db_path must not be empty")

        if path != ":memory:":
            path = str(Path(path).expanduser())
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        self.db_path = path
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            path,
            timeout=5.0,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row

        try:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if path != ":memory:":
                mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if mode is None or str(mode[0]).lower() != "wal":
                    raise sqlite3.OperationalError(
                        "SQLite could not enable WAL mode for the memory store"
                    )
                self._connection.execute("PRAGMA synchronous = NORMAL")
                self._connection.execute("PRAGMA cache_size = -8000")
                self._connection.execute("PRAGMA mmap_size = 67108864")
            self._initialize_schema()
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def _initialize_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id INTEGER PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
                    last_accessed_at REAL,
                    access_count INTEGER DEFAULT 0,
                    UNIQUE (namespace, key)
                )
                """
            )
            try:
                self._connection.execute("ALTER TABLE memory_items ADD COLUMN last_accessed_at REAL")
            except sqlite3.OperationalError:
                pass
            try:
                self._connection.execute("ALTER TABLE memory_items ADD COLUMN access_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            # Namespace and key identify/filter records but must not become
            # searchable text. Recall still applies an exact SQL namespace
            # predicate after FTS matching.
            self._connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    namespace UNINDEXED,
                    key UNINDEXED,
                    content,
                    tags,
                    tokenize = 'unicode61 remove_diacritics 2'
                )
                """
            )

    @staticmethod
    def _required_text(value: Any, name: str) -> str:
        if not isinstance(value, str):
            raise TypeError("%s must be a string" % name)
        if not value.strip():
            raise ValueError("%s must not be empty" % name)
        if "\x00" in value:
            raise ValueError("%s must not contain NUL characters" % name)
        return value

    @staticmethod
    def _normalize_tags(tags: Iterable[str]) -> list[str]:
        if isinstance(tags, (str, bytes, bytearray)):
            raise TypeError("tags must be an iterable of strings, not a string")
        try:
            values = list(tags)
        except TypeError as exc:
            raise TypeError("tags must be an iterable of strings") from exc

        if any(not isinstance(tag, str) for tag in values):
            raise TypeError("each tag must be a string")
        if isinstance(tags, (set, frozenset)):
            values.sort()

        normalized: list[str] = []
        seen: set[str] = set()
        for tag in values:
            if "\x00" in tag:
                raise ValueError("tags must not contain NUL characters")
            tag = tag.strip()
            if tag and tag not in seen:
                normalized.append(tag)
                seen.add(tag)
        return normalized

    @staticmethod
    def _confidence(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("confidence must be a number between 0 and 1")
        try:
            confidence = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("confidence must be a finite number between 0 and 1") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        return confidence

    @staticmethod
    def _query_expression(query: str) -> str | None:
        # Construct a safe AND expression from plain text tokens. Quoting each
        # token keeps FTS operators and punctuation supplied by callers from
        # changing the search expression or producing malformed MATCH syntax.
        tokens = list(dict.fromkeys(_QUERY_TOKEN_RE.findall(query)))
        if not tokens:
            return None
        return " AND ".join('"%s"' % token.replace('"', '""') for token in tokens)

    @staticmethod
    def _redact_framework_value(value: str) -> str:
        return redact_text(value, guard_syntax=False)

    def _prepare_entry(
        self,
        entry: dict[str, Any] | tuple[Any, ...] | list[Any],
    ) -> tuple[str, str, str, str, float, str]:
        """Validate and prepare a single memory entry in memory."""
        if isinstance(entry, dict):
            namespace = entry.get("namespace")
            key = entry.get("key")
            content = entry.get("content")
            tags = entry.get("tags", ())
            confidence = entry.get("confidence", 1.0)
        elif isinstance(entry, (tuple, list)):
            if len(entry) == 3:
                namespace, key, content = entry
                tags, confidence = (), 1.0
            elif len(entry) == 4:
                namespace, key, content, tags = entry
                confidence = 1.0
            elif len(entry) == 5:
                namespace, key, content, tags, confidence = entry
            else:
                raise ValueError("entry sequence must have 3, 4, or 5 elements")
        else:
            raise TypeError("each entry must be a dictionary or tuple")

        namespace = self._required_text(namespace, "namespace")
        key = self._required_text(key, "key")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        if "\x00" in content:
            raise ValueError("content must not contain NUL characters")
        normalized_tags = self._normalize_tags(tags)
        confidence = self._confidence(confidence)

        if namespace.startswith("framework:"):
            key = self._redact_framework_value(key)
            content = self._redact_framework_value(content)
            normalized_tags = [self._redact_framework_value(tag) for tag in normalized_tags]

        tags_json = json.dumps(
            normalized_tags,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fts_tags = " ".join(normalized_tags)
        return (namespace, key, content, tags_json, confidence, fts_tags)

    def store(
        self,
        namespace: str,
        key: str,
        content: str,
        tags: Iterable[str] = (),
        confidence: float = 1.0,
    ) -> None:
        """Insert or replace the memory identified by ``(namespace, key)``."""
        ns, k, cnt, tags_json, conf, fts_tags = self._prepare_entry(
            (namespace, key, content, tags, confidence)
        )
        now = time.time()
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO memory_items(namespace, key, content, tags, confidence, last_accessed_at, access_count)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(namespace, key) DO UPDATE SET
                        content = excluded.content,
                        tags = excluded.tags,
                        confidence = excluded.confidence,
                        last_accessed_at = excluded.last_accessed_at
                    """,
                    (ns, k, cnt, tags_json, conf, now),
                )
                item = self._connection.execute(
                    "SELECT id FROM memory_items WHERE namespace = ? AND key = ?",
                    (ns, k),
                ).fetchone()
                assert item is not None
                row_id = int(item["id"])
                self._connection.execute(
                    "DELETE FROM memory_fts WHERE rowid = ?", (row_id,)
                )
                self._connection.execute(
                    """
                    INSERT INTO memory_fts(rowid, namespace, key, content, tags)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (row_id, ns, k, cnt, fts_tags),
                )
                if self.max_items_per_namespace is not None:
                    self._evict_lru_unlocked(ns, self.max_items_per_namespace)

    def store_batch(
        self,
        entries: Iterable[dict[str, Any] | tuple[Any, ...]],
        *,
        chunk_size: int = 500,
    ) -> int:
        """Insert or replace multiple memories efficiently within chunked transactions.

        Validates all entries in memory before modifying the database.
        Returns the number of entries successfully stored.
        """
        if isinstance(entries, (str, bytes, bytearray)):
            raise TypeError("entries must be an iterable of entries, not a string")
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")

        prepared = [self._prepare_entry(e) for e in entries]
        if not prepared:
            return 0

        now = time.time()
        with self._lock:
            self._ensure_open()
            for i in range(0, len(prepared), chunk_size):
                chunk = prepared[i : i + chunk_size]
                with self._connection:
                    for ns, k, cnt, tags_json, conf, fts_tags in chunk:
                        self._connection.execute(
                            """
                            INSERT INTO memory_items(namespace, key, content, tags, confidence, last_accessed_at, access_count)
                            VALUES (?, ?, ?, ?, ?, ?, 0)
                            ON CONFLICT(namespace, key) DO UPDATE SET
                                content = excluded.content,
                                tags = excluded.tags,
                                confidence = excluded.confidence,
                                last_accessed_at = excluded.last_accessed_at
                            """,
                            (ns, k, cnt, tags_json, conf, now),
                        )
                        item = self._connection.execute(
                            "SELECT id FROM memory_items WHERE namespace = ? AND key = ?",
                            (ns, k),
                        ).fetchone()
                        assert item is not None
                        row_id = int(item["id"])
                        self._connection.execute(
                            "DELETE FROM memory_fts WHERE rowid = ?", (row_id,)
                        )
                        self._connection.execute(
                            """
                            INSERT INTO memory_fts(rowid, namespace, key, content, tags)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (row_id, ns, k, cnt, fts_tags),
                        )
            if self.max_items_per_namespace is not None:
                namespaces = {entry[0] for entry in prepared}
                with self._connection:
                    for ns in namespaces:
                        self._evict_lru_unlocked(ns, self.max_items_per_namespace)
        return len(prepared)

    def _evict_lru_unlocked(self, namespace: str, keep_max: int) -> int:
        cur = self._connection.execute(
            "SELECT COUNT(*) FROM memory_items WHERE namespace = ?", (namespace,)
        )
        total = cur.fetchone()[0]
        if total <= keep_max:
            return 0
        excess = total - keep_max
        rows = self._connection.execute(
            """
            SELECT id FROM memory_items
            WHERE namespace = ?
            ORDER BY COALESCE(last_accessed_at, 0) ASC, access_count ASC, id ASC
            LIMIT ?
            """,
            (namespace, excess),
        ).fetchall()
        if not rows:
            return 0
        row_ids = [int(r["id"]) for r in rows]
        placeholders = ",".join("?" for _ in row_ids)
        self._connection.execute(
            f"DELETE FROM memory_fts WHERE rowid IN ({placeholders})", row_ids
        )
        self._connection.execute(
            f"DELETE FROM memory_items WHERE id IN ({placeholders})", row_ids
        )
        return len(row_ids)

    def evict_lru(self, namespace: str, keep_max: int) -> int:
        """Evict oldest accessed memories in the namespace exceeding keep_max.

        Returns the number of evicted items.
        """
        namespace = self._required_text(namespace, "namespace")
        if isinstance(keep_max, bool) or not isinstance(keep_max, int) or keep_max < 0:
            raise ValueError("keep_max must be a non-negative integer")

        with self._lock:
            self._ensure_open()
            with self._connection:
                return self._evict_lru_unlocked(namespace, keep_max)

    def recall(
        self,
        namespace: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the best FTS5 matches from one exact namespace only.

        Results are dictionaries containing ``namespace``, ``key``,
        ``content``, ``tags``, ``confidence``, and the SQLite BM25 ``score``
        (lower scores rank first).
        """
        namespace = self._required_text(namespace, "namespace")
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise TypeError("top_k must be a non-negative integer")
        if top_k < 0 or top_k > _SQLITE_INTEGER_MAX:
            raise ValueError("top_k must be a non-negative SQLite integer")
        if top_k == 0:
            return []

        expression = self._query_expression(query)
        if expression is None:
            return []

        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT m.id, m.namespace, m.key, m.content, m.tags, m.confidence,
                       bm25(memory_fts, 0.0, 0.0, 1.0, 0.5) AS score
                FROM memory_fts
                JOIN memory_items AS m ON m.id = memory_fts.rowid
                WHERE memory_fts MATCH ? AND m.namespace = ?
                ORDER BY score ASC, m.confidence DESC, m.key ASC
                LIMIT ?
                """,
                (expression, namespace, top_k),
            ).fetchall()

            if rows:
                now = time.time()
                row_ids = [int(r["id"]) for r in rows]
                placeholders = ",".join("?" for _ in row_ids)
                with self._connection:
                    self._connection.execute(
                        f"""
                        UPDATE memory_items
                        SET last_accessed_at = ?, access_count = access_count + 1
                        WHERE id IN ({placeholders})
                        """,
                        [now] + row_ids,
                    )

        return [
            {
                "namespace": row["namespace"],
                "key": row["key"],
                "content": row["content"],
                "tags": json.loads(row["tags"]),
                "confidence": float(row["confidence"]),
                "score": float(row["score"]),
            }
            for row in rows
        ]

    @property
    def journal_mode(self) -> str:
        """Return SQLite's active journal mode (``wal`` for file stores)."""
        with self._lock:
            self._ensure_open()
            row = self._connection.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower() if row is not None else ""

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("memory store is closed")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> "MemoryStore":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = ["MemoryStore"]
