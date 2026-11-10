#!/usr/bin/env python3
"""BugX 自有项目文档 MCP（本地 stdio，SQLite FTS5，无第三方 Key）。"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_CORE_DIR = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "skills" else Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "bugx-docs"
SERVER_VERSION = "0.1.0"
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 500
MAX_RESULT_BYTES = 12_000


class DocsMcpError(ValueError):
    """受控文档服务错误；不泄露 token 或本地秘密。"""


def _resolve_under(root: Path, value: str | Path) -> Path:
    root = root.expanduser().resolve()
    path = Path(value).expanduser()
    path = path if path.is_absolute() else root / path
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise DocsMcpError(f"路径必须位于项目根目录内: {resolved}")
    if path.is_symlink():
        raise DocsMcpError(f"拒绝使用符号链接路径: {path}")
    return resolved


def _project_id(root: Path, supplied: str | None) -> str:
    value = str(supplied or os.environ.get("BUGX_DOCS_PROJECT") or root.name).strip()
    if not value or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", value):
        raise DocsMcpError("project_id 无效")
    return value


def _db_path(root: Path, value: str | Path) -> Path:
    path = _resolve_under(root, value)
    if path.exists() and not path.is_file():
        raise DocsMcpError(f"文档数据库不是普通文件: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _source_files(root: Path) -> list[tuple[str, Path]]:
    """只读取项目文档，避免把源码、运行态和凭据误纳入知识库。"""
    candidates: list[Path] = []
    for path in (root / "README.md",):
        if path.is_file() and not path.is_symlink():
            candidates.append(path)
    for directory in (root / "docs", root / "core"):
        if not directory.is_dir() or directory.is_symlink():
            continue
        candidates.extend(
            path for path in sorted(directory.rglob("*.md"))
            if path.is_file() and not path.is_symlink()
        )
    unique: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        unique.append((relative, path))
        if len(unique) >= MAX_FILES:
            break
    # ponytail: bounded recursive scan; add a signed manifest when repositories
    # need more than 500 documentation files or remote libraries.
    return unique


def _version(root: Path, supplied: str | None) -> str:
    value = str(supplied or os.environ.get("BUGX_DOCS_VERSION") or "working-tree").strip()
    if not value or len(value) > 128:
        raise DocsMcpError("文档版本无效")
    return value


def _citation(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "document_id": row["doc_id"],
        "library_id": row["library_id"],
        "library_name": row["library_name"],
        "version": row["version"],
        "commit": row["version"],
        "source": row["source_path"],
        "source_label": row["source_label"],
        "sha256": row["content_sha256"],
        "captured_at": row["captured_at"],
        "private": bool(row["private"]),
        "project_id": row["project_id"],
    }


def index_documents(root: Path, db: Path, *, project_id: str, version: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise DocsMcpError(f"项目根目录不是目录: {root}")
    db = _db_path(root, db)
    library_id = f"project:{project_id}"
    captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    rows: list[tuple[Any, ...]] = []
    for relative, path in _source_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        doc_id = f"{library_id}:{version}:{relative}"
        rows.append((
            doc_id, library_id, project_id, "project-docs", version, relative,
            content, digest, captured_at, "BugX project documentation", 1,
        ))
    try:
        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                DROP TABLE IF EXISTS documents;
                DROP TABLE IF EXISTS documents_fts;
                CREATE TABLE documents (
                    doc_id TEXT PRIMARY KEY,
                    library_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    library_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    private INTEGER NOT NULL CHECK(private IN (0, 1))
                );
                CREATE VIRTUAL TABLE documents_fts USING fts5(
                    doc_id UNINDEXED, title, content
                );
                """
            )
            connection.executemany(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.executemany(
                "INSERT INTO documents_fts(doc_id, title, content) VALUES (?, ?, ?)",
                ((row[0], str(row[5]).rsplit("/", 1)[-1], row[6]) for row in rows),
            )
            connection.commit()
    except sqlite3.Error as exc:
        raise DocsMcpError(f"文档数据库不可用: {exc}") from exc
    return {"indexed": len(rows), "db": str(db), "library_id": library_id, "version": version}


def _open_readonly(db: Path) -> sqlite3.Connection:
    if not db.is_file() or db.is_symlink():
        raise DocsMcpError(f"文档索引不存在或不安全，请先执行 docs index: {db}")
    try:
        connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
        return connection
    except sqlite3.Error as exc:
        raise DocsMcpError(f"文档索引无法只读打开: {exc}") from exc


def _authorize(arguments: Mapping[str, Any], project_id: str, expected_token: str | None) -> None:
    supplied_project = str(arguments.get("project_id") or "").strip()
    if supplied_project != project_id:
        raise DocsMcpError("文档 MCP project_id 不匹配，已拒绝检索")
    if expected_token is not None:
        if not isinstance(arguments.get("token"), str) or arguments["token"] != expected_token:
            raise DocsMcpError("文档 MCP token 无效")


def _fts_query(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 512:
        raise DocsMcpError("query 不能为空且不得超过 512 字符")
    tokens = re.findall(r"[\w.-]+", text, flags=re.UNICODE)
    if not tokens:
        raise DocsMcpError("query 不包含可检索文本")
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens[:32])


def _bounded(text: str, limit: int = MAX_RESULT_BYTES) -> str:
    raw = text.encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore")


def call_tool(db: Path, name: str, arguments: Mapping[str, Any], *, project_id: str,
              expected_token: str | None) -> dict[str, Any]:
    _authorize(arguments, project_id, expected_token)
    with _open_readonly(db) as connection:
        if name == "resolve_library":
            query = str(arguments.get("name") or "").strip()
            if not query or len(query) > 128:
                raise DocsMcpError("library name 无效")
            row = connection.execute(
                "SELECT DISTINCT library_id, library_name, version, project_id "
                "FROM documents WHERE project_id = ? AND (library_id = ? OR library_name LIKE ?) "
                "ORDER BY version DESC LIMIT 1",
                (project_id, query, f"%{query}%"),
            ).fetchone()
            if row is None:
                raise DocsMcpError("未找到受控文档库")
            return dict(row)
        if name == "search_docs":
            library_id = str(arguments.get("library_id") or f"project:{project_id}")
            version = arguments.get("version")
            try:
                limit = int(arguments.get("limit", 5))
            except (TypeError, ValueError) as exc:
                raise DocsMcpError("limit 必须是整数") from exc
            if not 1 <= limit <= 20:
                raise DocsMcpError("limit 必须在 1 到 20 之间")
            params: list[Any] = [project_id, library_id]
            version_clause = ""
            if version is not None:
                version_clause = " AND d.version = ?"
                params.append(str(version))
            params.extend([_fts_query(arguments.get("query")), limit])
            rows = connection.execute(
                "SELECT d.* FROM documents d JOIN documents_fts f ON f.doc_id = d.doc_id "
                "WHERE d.project_id = ? AND d.library_id = ?" + version_clause +
                " AND f.documents_fts MATCH ? ORDER BY d.source_path LIMIT ?",
                params,
            ).fetchall()
            return {
                "results": [{**_citation(row), "snippet": _bounded(row["content"], 2400)} for row in rows],
                "count": len(rows),
            }
        if name == "fetch_doc":
            document_id = str(arguments.get("document_id") or "").strip()
            row = connection.execute(
                "SELECT * FROM documents WHERE doc_id = ? AND project_id = ?",
                (document_id, project_id),
            ).fetchone()
            if row is None:
                raise DocsMcpError("document_id 不存在或不属于当前项目")
            content = row["content"]
            section = str(arguments.get("section") or "").strip()
            if section:
                match = re.search(rf"(?im)^#+\s*{re.escape(section)}\s*$", content)
                if match:
                    tail = content[match.end():]
                    next_heading = re.search(r"(?m)^#+\s+", tail)
                    content = tail[:next_heading.start()] if next_heading else tail
            return {**_citation(row), "content": _bounded(content)}
    raise DocsMcpError(f"未知文档工具: {name}")


TOOLS = [
    {
        "name": "resolve_library",
        "description": "解析当前项目受控文档库；结果只来自本地索引。",
        "inputSchema": {"type": "object", "required": ["name", "project_id"], "properties": {
            "name": {"type": "string", "maxLength": 128},
            "project_id": {"type": "string"}, "token": {"type": "string"},
        }},
    },
    {
        "name": "search_docs",
        "description": "在当前项目版本的受控 Markdown 文档中全文检索并返回可引用证据。",
        "inputSchema": {"type": "object", "required": ["query", "project_id"], "properties": {
            "library_id": {"type": "string"}, "query": {"type": "string", "maxLength": 512},
            "version": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "project_id": {"type": "string"}, "token": {"type": "string"},
        }},
    },
    {
        "name": "fetch_doc",
        "description": "读取一份受控文档或其中标题段落，并返回版本、哈希和来源引用。",
        "inputSchema": {"type": "object", "required": ["document_id", "project_id"], "properties": {
            "document_id": {"type": "string"}, "section": {"type": "string"},
            "project_id": {"type": "string"}, "token": {"type": "string"},
        }},
    },
]


def _result(payload: Mapping[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}], "structuredContent": dict(payload)}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id,
            "error": {"code": code, "message": message}}


def serve(root: Path, db: Path, *, project_id: str, version: str,
          token: str | None = None, auto_index: bool = True) -> int:
    root = root.expanduser().resolve()
    db = _db_path(root, db)
    if auto_index:
        index_documents(root, db, project_id=project_id, version=version)
    expected_token = token or os.environ.get("BUGX_DOCS_TOKEN")
    # The stdio boundary is local; when a token is configured, every call must
    # still prove both token and project scope. No token is ever logged/returned.
    for raw_line in sys.stdin.buffer:
        request_id = None
        has_id = False
        parsed_request = False
        if len(raw_line) > 1_000_000:
            print(json.dumps(_error(None, -32602, "请求行过长"), ensure_ascii=False), flush=True)
            continue
        try:
            request = json.loads(raw_line.decode("utf-8"))
            parsed_request = True
            if not isinstance(request, dict) or request.get("jsonrpc") != JSONRPC_VERSION:
                raise DocsMcpError("只支持 JSON-RPC 2.0")
            request_id = request.get("id")
            has_id = "id" in request
            method = request.get("method")
            params = request.get("params") or {}
            if method in {"notifications/initialized", "notifications/cancelled"}:
                continue
            if method == "initialize":
                payload = {"protocolVersion": MCP_PROTOCOL_VERSION,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                           "instructions": "调用工具时必须提供当前 project_id；结果含引用哈希。"}
            elif method == "ping":
                payload = {}
            elif method == "tools/list":
                payload = {"tools": TOOLS}
            elif method == "tools/call":
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    raise DocsMcpError("tools/call 缺少 name")
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    raise DocsMcpError("arguments 必须是对象")
                payload = _result(call_tool(db, params["name"], args,
                                            project_id=project_id, expected_token=expected_token))
            else:
                raise DocsMcpError(f"不支持 MCP 方法: {method}")
            if "id" in request:
                print(json.dumps({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": payload},
                                 ensure_ascii=False, separators=(",", ":")), flush=True)
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(json.dumps(_error(None, -32700, "Parse error"), ensure_ascii=False), flush=True)
        except DocsMcpError as exc:
            if has_id:
                print(json.dumps(_error(request_id, -32001, str(exc)), ensure_ascii=False), flush=True)
        except (sqlite3.Error, OSError) as exc:
            if has_id:
                print(json.dumps(_error(request_id, -32002, f"文档 MCP 失败: {exc}"), ensure_ascii=False), flush=True)
        except Exception:
            if has_id or not parsed_request:
                print(json.dumps(_error(request_id, -32603, "Internal error"), ensure_ascii=False), flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("index", "serve"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--db", type=Path, default=Path(".bugx/knowledge/docs.sqlite3"))
        command.add_argument("--project-id", default=None)
        command.add_argument("--version", default=None)
        command.add_argument("--token", default=None, help=argparse.SUPPRESS)
        if name == "serve":
            command.add_argument("--no-auto-index", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.root.expanduser().resolve()
        project_id = _project_id(root, args.project_id)
        version = _version(root, args.version)
        if args.command == "index":
            print(json.dumps(index_documents(root, args.db, project_id=project_id, version=version),
                             ensure_ascii=False))
            return 0
        return serve(root, args.db, project_id=project_id, version=version,
                     token=args.token, auto_index=not args.no_auto_index)
    except DocsMcpError as exc:
        print(f"bugx-docs: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
