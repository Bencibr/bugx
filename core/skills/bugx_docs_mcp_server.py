#!/usr/bin/env python3
"""BugX 双作用域本地/云端协同文档 MCP（SQLite FTS5 trigram + 章节切分 + 不可变缓存）。"""

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

from core.infra.build_info import BASE_VERSION

_CORE_DIR = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "skills" else Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
if str(_CORE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR.parent))

from core.skills import bugx_docs_cloud_sync as _docs_sync  # noqa: E402

JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "bugx-docs"
SERVER_VERSION = BASE_VERSION
SCHEMA_VERSION = "v2-trigram-section-hybrid"
OFFICIAL_LIBRARY_ID = "bugx:official"
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 500
MAX_RESULT_BYTES = 12_000
MAX_SECTION_CHARS = 6_000


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


def _collect_md_files(candidates: list[Path], base: Path, *, strip_prefix: str = "") -> list[tuple[str, Path]]:
    unique: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:
            continue
        if strip_prefix and relative.startswith(strip_prefix):
            relative = relative[len(strip_prefix):]
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
    return unique


def _project_source_files(root: Path) -> list[tuple[str, Path]]:
    """仅读取当前项目可控文档（优先级保证核心 README/docs 不被海量内部产物挤出配额）。"""
    candidates: list[Path] = []
    for name in ("README.md", "README_EN.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "CHANGELOG.md"):
        candidates.append(root / name)
    # 优先级队列：指南与设计 -> 技能目录 -> 内置核心（排除 bundled_docs 防双份重复）
    docs_dir = root / "docs"
    if docs_dir.is_dir() and not docs_dir.is_symlink():
        # 先收 01_guides/03_ai/04_reference 等公开文档，最后才收 08_internal
        public_mds: list[Path] = []
        internal_mds: list[Path] = []
        for path in sorted(docs_dir.rglob("*.md")):
            if "08_internal" in path.parts:
                internal_mds.append(path)
            else:
                public_mds.append(path)
        candidates.extend(public_mds)
        candidates.extend(internal_mds)
    for directory in (root / "skills", root / ".agents" / "skills", root / "core"):
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.rglob("*.md")):
            if "bundled_docs" in path.parts:
                continue
            candidates.append(path)
    return _collect_md_files(candidates, root)


def _official_source_files(core_root: Path | None = None) -> list[tuple[str, Path]]:
    """收集随包/全局引擎内自带的 BugX 官方文档资产。"""
    croot = (core_root or _CORE_DIR).resolve()
    candidates: list[Path] = []
    bundled = croot / "bundled_docs"
    if bundled.is_dir() and not bundled.is_symlink():
        for path in sorted(bundled.rglob("*.md")):
            if path.name == "README.md" and path.parent == bundled:
                continue
            candidates.append(path)
    out = _collect_md_files(candidates, bundled if bundled.is_dir() else croot)
    seen = {rel for rel, _ in out}
    extra_candidates: list[Path] = []
    if (croot / "bugx.md").is_file():
        extra_candidates.append(croot / "bugx.md")
    for sub in ("contracts", "prompts", "audit_packs"):
        subdir = croot / sub
        if subdir.is_dir() and not subdir.is_symlink():
            extra_candidates.extend(sorted(subdir.rglob("*.md")))
    for rel, path in _collect_md_files(extra_candidates, croot.parent if croot.name == "core" else croot):
        if not rel.startswith("core/"):
            rel = f"core/{rel}"
        if rel not in seen and len(out) < MAX_FILES:
            seen.add(rel)
            out.append((rel, path))
    return out


_source_files = _project_source_files


def _version(root: Path, supplied: str | None) -> str:
    value = str(supplied or os.environ.get("BUGX_DOCS_VERSION") or "working-tree").strip()
    if not value or len(value) > 128:
        raise DocsMcpError("文档版本无效")
    return value


def _doc_tier(source_path: str, library_id: str) -> float:
    path = source_path.lstrip("/")
    if path in ("README.md", "README_EN.md"):
        return 3.5
    if path.startswith("guides/04_") or path.startswith("guides/05_") or path.startswith("guides/06_") or path.startswith("guides/07_") or path.startswith("guides/08_"):
        return 4.0
    if path.startswith("docs/01_guides/04_") or path.startswith("docs/01_guides/05_") or path.startswith("docs/01_guides/06_") or path.startswith("docs/01_guides/07_") or path.startswith("docs/01_guides/08_"):
        return 4.0
    if path.startswith(("guides/", "docs/01_guides/", "ai/", "docs/03_ai/")):
        return 3.0
    if path in ("skills/platform/bugx/SKILL.md", "ai/SKILL.md"):
        return 2.8
    if path.startswith(("reference/", "docs/04_reference/", "hosts/", "core/contracts/", "core/prompts/")):
        return 2.0
    if path.startswith("docs/02_architecture/"):
        return 1.5
    if path.startswith("docs/08_internal/") or path.startswith("docs/05_release-notes/001_") or path.startswith("docs/05_release-notes/002_"):
        return -1.5
    if library_id == OFFICIAL_LIBRARY_ID:
        return 1.2
    return 1.0


def _split_markdown_sections(doc_id: str, library_id: str, content: str) -> list[tuple[Any, ...]]:
    """按 fenced-code-aware ATX 标题切片，避免 # 注释误切与大文件截断。"""
    lines = content.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []  # (char_offset, level, title)
    offset = 0
    fence_marker: str | None = None
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")

    for line in lines:
        stripped = line.lstrip()
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fence_match:
            marker = fence_match.group(1)
            if fence_marker is None:
                fence_marker = marker
            elif stripped.startswith(fence_marker[0] * len(fence_marker)):
                fence_marker = None
        elif fence_marker is None:
            m = heading_re.match(line.rstrip("\r\n"))
            if m:
                headings.append((offset, len(m.group(1)), m.group(2).strip()))
        offset += len(line)

    raw_sections: list[tuple[str, str, int, int, int, str]] = []
    if not headings:
        raw_sections.append(("(document)", "(document)", 1, 0, len(content), content))
    else:
        stack: list[tuple[int, str]] = []
        if headings[0][0] > 0:
            intro = content[:headings[0][0]].strip()
            if intro:
                raw_sections.append(("(preamble)", "(preamble)", 1, 0, headings[0][0], intro))
        for idx, (h_start, level, title) in enumerate(headings):
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            h_path = " > ".join(t for _, t in stack)
            # 找到同级或更高级别的下一个标题作为整章边界
            end_pos = len(content)
            for j in range(idx + 1, len(headings)):
                if headings[j][1] <= level:
                    end_pos = headings[j][0]
                    break
            # 紧邻切片边界仅取到下一个标题开始以保持去重，完整跨子章节读取由 fetch_doc(start, full_end) 负责
            slice_end = headings[idx + 1][0] if idx + 1 < len(headings) else len(content)
            sec_text = content[h_start:slice_end].strip()
            if sec_text:
                raw_sections.append((title, h_path, level, h_start, end_pos, sec_text))

    rows: list[tuple[Any, ...]] = []
    seq = 0
    for title, h_path, level, c_start, c_end, sec_text in raw_sections:
        if len(sec_text) <= MAX_SECTION_CHARS:
            seq += 1
            sec_id = f"{doc_id}#s{seq}"
            rows.append((sec_id, doc_id, library_id, title, h_path, level, c_start, c_end, sec_text))
        else:
            # 按空行切分子片
            pos = 0
            part = 1
            while pos < len(sec_text):
                cut = min(len(sec_text), pos + MAX_SECTION_CHARS)
                if cut < len(sec_text):
                    nl = sec_text.rfind("\n\n", pos + 1000, cut)
                    if nl > pos:
                        cut = nl + 2
                chunk = sec_text[pos:cut].strip()
                if chunk:
                    seq += 1
                    sec_id = f"{doc_id}#s{seq}"
                    part_title = title if part == 1 else f"{title} (part {part})"
                    rows.append((sec_id, doc_id, library_id, part_title, h_path, level, c_start, c_end, chunk))
                    part += 1
                pos = cut if cut > pos else pos + MAX_SECTION_CHARS
    return rows


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


def _populate_database(
    connection: sqlite3.Connection,
    *,
    sources: list[tuple[str, Path]],
    library_id: str,
    project_id: str,
    library_name: str,
    version: str,
    source_label: str,
    private_flag: int,
    contract_version: str,
) -> int:
    captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    doc_rows: list[tuple[Any, ...]] = []
    section_rows: list[tuple[Any, ...]] = []
    for relative, path in sources:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        doc_id = f"{library_id}:{version}:{relative}"
        doc_rows.append((
            doc_id, library_id, project_id, library_name, version, relative,
            content, digest, captured_at, source_label, private_flag,
        ))
        section_rows.extend(_split_markdown_sections(doc_id, library_id, content))

    connection.executescript(
        """
        DROP TABLE IF EXISTS documents_meta;
        DROP TABLE IF EXISTS documents;
        DROP TABLE IF EXISTS document_sections;
        DROP TABLE IF EXISTS documents_fts;
        DROP TABLE IF EXISTS sections_fts;
        CREATE TABLE documents_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
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
        CREATE TABLE document_sections (
            section_id TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL,
            library_id TEXT NOT NULL,
            heading TEXT NOT NULL,
            heading_path TEXT NOT NULL,
            level INTEGER NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            content TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            doc_id UNINDEXED, title, content, tokenize='trigram'
        );
        CREATE VIRTUAL TABLE sections_fts USING fts5(
            section_id UNINDEXED, doc_id UNINDEXED, title, heading_path, content, tokenize='trigram'
        );
        """
    )
    connection.executemany(
        "INSERT INTO documents_meta VALUES (?, ?)",
        [
            ("schema_version", SCHEMA_VERSION),
            ("contract_version", contract_version),
            ("built_at", captured_at),
            ("library_id", library_id),
        ],
    )
    connection.executemany(
        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        doc_rows,
    )
    connection.executemany(
        "INSERT INTO document_sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        section_rows,
    )
    connection.executemany(
        "INSERT INTO documents_fts(doc_id, title, content) VALUES (?, ?, ?)",
        ((row[0], str(row[5]).rsplit("/", 1)[-1], row[6]) for row in doc_rows),
    )
    doc_title_map = {row[0]: str(row[5]).rsplit("/", 1)[-1] for row in doc_rows}
    connection.executemany(
        "INSERT INTO sections_fts(section_id, doc_id, title, heading_path, content) VALUES (?, ?, ?, ?, ?)",
        ((s[0], s[1], doc_title_map.get(s[1], ""), s[4], s[8]) for s in section_rows),
    )
    connection.commit()
    return len(doc_rows)


def index_documents(root: Path, db: Path, *, project_id: str, version: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise DocsMcpError(f"项目根目录不是目录: {root}")
    db = _db_path(root, db)
    library_id = f"project:{project_id}"
    contract_ver = _docs_sync.contract_version_of(BASE_VERSION)
    sources = _project_source_files(root)
    try:
        with sqlite3.connect(db, timeout=5.0) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            count = _populate_database(
                connection,
                sources=sources,
                library_id=library_id,
                project_id=project_id,
                library_name="project-docs",
                version=version,
                source_label="BugX project documentation",
                private_flag=1,
                contract_version=contract_ver,
            )
    except sqlite3.Error as exc:
        raise DocsMcpError(f"文档数据库不可用: {exc}") from exc
    return {"indexed": count, "db": str(db), "library_id": library_id, "version": version, "schema": SCHEMA_VERSION}


def _official_baseline_fingerprint(sources: list[tuple[str, Path]]) -> str:
    h = hashlib.sha256()
    for rel, path in sources:
        try:
            st = path.stat()
            h.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}\n".encode("utf-8"))
        except OSError:
            continue
    return h.hexdigest()[:16]


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


def _is_v2_db(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT value FROM documents_meta WHERE key = 'schema_version'"
        ).fetchone()
        return bool(row and row[0] == SCHEMA_VERSION)
    except sqlite3.Error:
        return False


def _open_official_connection(
    *,
    contract_version: str | None = None,
    sync_if_missing: bool = False,
    offline: bool = False,
    remote_url: str | None = None,
) -> sqlite3.Connection | None:
    """打开 BugX 官方只读文档连接：云端不可变缓存 → 本地基线磁盘快照 → 内存库兜底 (NEG-03)。"""
    ver = _docs_sync.contract_version_of(contract_version)
    if sync_if_missing and not offline and not _docs_sync.docs_offline_disabled():
        try:
            _docs_sync.sync_official_docs(contract_version=ver, offline=offline, remote_url=remote_url)
        except Exception:
            pass
    cached = _docs_sync.current_cached_db(ver)
    if cached is not None:
        try:
            return _open_readonly(cached)
        except DocsMcpError:
            pass

    sources = _official_source_files()
    if not sources:
        return None
    fp = _official_baseline_fingerprint(sources)
    try:
        cdir = _docs_sync.docs_cache_dir(ver)
        baseline_db = cdir / f"baseline-{fp}.sqlite3"
        if _docs_sync.verify_sqlite_snapshot(baseline_db, ver):
            return _open_readonly(baseline_db)
        cdir.mkdir(parents=True, mode=0o700, exist_ok=True)
        tmp_db = cdir / f".tmp-baseline-{os.getpid()}.sqlite3"
        with sqlite3.connect(tmp_db) as conn:
            _populate_database(
                conn,
                sources=sources,
                library_id=OFFICIAL_LIBRARY_ID,
                project_id=OFFICIAL_LIBRARY_ID,
                library_name="bugx-official-docs",
                version=ver,
                source_label="BugX official documentation (bundled baseline)",
                private_flag=0,
                contract_version=ver,
            )
        os.replace(tmp_db, baseline_db)
        # 清理旧版本 baseline 文件
        for old in cdir.glob("baseline-*.sqlite3"):
            if old.name != baseline_db.name:
                try:
                    old.unlink()
                except OSError:
                    pass
        return _open_readonly(baseline_db)
    except (OSError, sqlite3.Error, _docs_sync.DocsSyncError):
        # NEG-03：只读文件系统容器兜底回落纯内存库
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _populate_database(
            conn,
            sources=sources,
            library_id=OFFICIAL_LIBRARY_ID,
            project_id=OFFICIAL_LIBRARY_ID,
            library_name="bugx-official-docs",
            version=ver,
            source_label="BugX official documentation (in-memory)",
            private_flag=0,
            contract_version=ver,
        )
        return conn


def _authorize(arguments: Mapping[str, Any], project_id: str, expected_token: str | None) -> None:
    supplied_project = str(arguments.get("project_id") or "").strip()
    if supplied_project != project_id:
        raise DocsMcpError("文档 MCP project_id 不匹配，已拒绝检索")
    if expected_token is not None:
        if not isinstance(arguments.get("token"), str) or arguments["token"] != expected_token:
            raise DocsMcpError("文档 MCP token 无效")


_STOP_WORDS_CN = {
    "怎么", "如何", "怎样", "请问", "我想", "要", "请教", "帮我", "关于", "对于", "进行",
    "排查", "查看", "一下", "一个", "我们", "你们", "什么", "为什么", "在哪里", "哪个",
    "命令", "方法", "步骤", "规范", "操作", "手册", "指南", "指引", "说明"
}
_STOP_WORDS_EN_PATTERN = re.compile(
    r"\b(how|to|what|is|the|a|an|in|on|for|with|about|can|i|do|does|did|please|help|me|my)\b",
    re.IGNORECASE,
)
_CN_STOP_PATTERN = re.compile(
    r"|".join(re.escape(w) for w in sorted(_STOP_WORDS_CN, key=len, reverse=True))
)


def _extract_search_terms(query: str) -> tuple[list[str], list[str]]:
    """从自然语言查询中提取检索词：(fts_terms >=3 chars, sub_terms >=2 chars)。"""
    text = str(query or "").strip()
    if not text:
        return [], []
    cleaned = _STOP_WORDS_EN_PATTERN.sub(" ", text)
    cleaned = _CN_STOP_PATTERN.sub(" ", cleaned)
    chunks = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_.-]+", cleaned)
    fts_terms: list[str] = []
    sub_terms: list[str] = []
    seen_sub: set[str] = set()

    def _add_sub(term: str) -> None:
        t = term.strip().lower()
        if len(t) >= 2 and t not in seen_sub:
            seen_sub.add(t)
            sub_terms.append(t)

    def _add_fts(term: str) -> None:
        t = term.strip()
        if len(t) >= 3 and t not in fts_terms:
            fts_terms.append(t)

    for chunk in chunks:
        if re.match(r"^[\u4e00-\u9fff]+$", chunk):
            if len(chunk) >= 3:
                _add_fts(chunk)
            if len(chunk) == 2:
                _add_sub(chunk)
            elif len(chunk) == 3:
                _add_sub(chunk)
                _add_sub(chunk[:2])
                _add_sub(chunk[1:])
            elif len(chunk) == 4:
                _add_sub(chunk[:2])
                _add_sub(chunk[2:])
            else:
                for i in range(0, len(chunk) - 1, 2):
                    _add_sub(chunk[i:i+2])
        else:
            _add_sub(chunk)
            if len(chunk) >= 3:
                _add_fts(chunk)

    if not sub_terms:
        raw_chunks = re.findall(r"[\w.-]+", text, flags=re.UNICODE)
        for rc in raw_chunks:
            _add_sub(rc)
            if len(rc) >= 3:
                _add_fts(rc)

    return fts_terms, sub_terms


def _extract_snippet(content: str, terms: list[str], max_chars: int = 1200) -> str:
    """提取关键词高频命中窗口，避免截取前缀导致丢失真正答案。"""
    if not content:
        return ""
    if not terms or len(content) <= max_chars:
        snippet = content[:max_chars].strip()
        return snippet + " ..." if len(content) > max_chars else snippet

    term_matches: list[int] = []
    for term in terms:
        for m in re.finditer(re.escape(term), content, re.IGNORECASE):
            term_matches.append(m.start())

    if not term_matches:
        snippet = content[:max_chars].strip()
        return snippet + " ..." if len(content) > max_chars else snippet

    term_matches.sort()
    best_start = 0
    best_score = -1
    for idx in term_matches:
        w_start = max(0, idx - 150)
        w_end = min(len(content), w_start + max_chars)
        window = content[w_start:w_end]
        cnt = sum(1 for t in terms if t.lower() in window.lower())
        total_occ = sum(len(re.findall(re.escape(t), window, re.IGNORECASE)) for t in terms)
        score = cnt * 10 + total_occ
        if score > best_score:
            best_score = score
            best_start = w_start

    end = min(len(content), best_start + max_chars)
    snippet = content[best_start:end].strip()
    if best_start > 0:
        snippet = "... " + snippet
    if end < len(content):
        snippet = snippet + " ..."
    return snippet


def _bounded(text: str, limit: int = MAX_RESULT_BYTES) -> str:
    raw = text.encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore")


def _search_single_connection(
    connection: sqlite3.Connection,
    *,
    scope_project_id: str,
    library_id: str,
    version: str | None,
    query_str: str,
    fts_terms: list[str],
    sub_terms: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    if scope_project_id == OFFICIAL_LIBRARY_ID:
        scope_clause = "d.library_id = ? AND d.private = 0"
        base_params: list[Any] = [library_id]
    else:
        scope_clause = "d.project_id = ? AND d.library_id = ?"
        base_params = [scope_project_id, library_id]
    version_clause = " AND d.version = ?" if version is not None else ""
    if version is not None:
        base_params.append(str(version))

    use_sections = _is_v2_db(connection)
    candidates_by_key: dict[str, dict[str, Any]] = {}

    if use_sections:
        if fts_terms:
            fts_exprs = [" AND ".join('"' + t.replace('"', '""') + '"' for t in fts_terms[:8])]
            if len(fts_terms) > 1:
                fts_exprs.append(" OR ".join('"' + t.replace('"', '""') + '"' for t in fts_terms[:8]))
            for fts_expr in fts_exprs:
                try:
                    res = connection.execute(
                        "SELECT d.*, s.section_id, s.heading, s.heading_path, s.content AS sec_content "
                        "FROM document_sections s "
                        "JOIN documents d ON d.doc_id = s.doc_id "
                        "JOIN sections_fts f ON f.section_id = s.section_id "
                        "WHERE " + scope_clause + version_clause +
                        " AND f.sections_fts MATCH ? ORDER BY f.rank ASC LIMIT 24",
                        list(base_params) + [fts_expr],
                    ).fetchall()
                    for r in res:
                        candidates_by_key[r["section_id"]] = dict(r)
                    if len(candidates_by_key) >= limit * 2:
                        break
                except sqlite3.OperationalError:
                    continue

        if len(candidates_by_key) < limit * 2 and sub_terms:
            and_clauses = ["(s.content LIKE ? OR s.heading_path LIKE ? OR d.source_path LIKE ?)" for _ in sub_terms[:5]]
            and_params = list(base_params)
            for t in sub_terms[:5]:
                pat = f"%{t}%"
                and_params.extend([pat, pat, pat])
            res = connection.execute(
                "SELECT d.*, s.section_id, s.heading, s.heading_path, s.content AS sec_content "
                "FROM document_sections s JOIN documents d ON d.doc_id = s.doc_id "
                "WHERE " + scope_clause + version_clause +
                " AND " + " AND ".join(and_clauses) + " LIMIT 24",
                and_params,
            ).fetchall()
            for r in res:
                candidates_by_key[r["section_id"]] = dict(r)

        if not candidates_by_key and sub_terms:
            or_clauses = ["(s.content LIKE ? OR s.heading_path LIKE ? OR d.source_path LIKE ?)" for _ in sub_terms[:5]]
            or_params = list(base_params)
            for t in sub_terms[:5]:
                pat = f"%{t}%"
                or_params.extend([pat, pat, pat])
            res = connection.execute(
                "SELECT d.*, s.section_id, s.heading, s.heading_path, s.content AS sec_content "
                "FROM document_sections s JOIN documents d ON d.doc_id = s.doc_id "
                "WHERE " + scope_clause + version_clause +
                " AND (" + " OR ".join(or_clauses) + ") LIMIT 30",
                or_params,
            ).fetchall()
            for r in res:
                candidates_by_key[r["section_id"]] = dict(r)
    else:
        # 兼容极早期无 document_sections 结构的旧索引读取
        if sub_terms:
            and_clauses = ["(d.content LIKE ? OR d.source_path LIKE ?)" for _ in sub_terms[:5]]
            and_params = list(base_params)
            for t in sub_terms[:5]:
                pat = f"%{t}%"
                and_params.extend([pat, pat])
            res = connection.execute(
                "SELECT d.* FROM documents d WHERE " + scope_clause +
                version_clause + " AND " + " AND ".join(and_clauses) + " LIMIT 20",
                and_params,
            ).fetchall()
            for r in res:
                row_d = dict(r)
                row_d["section_id"] = row_d["doc_id"]
                row_d["heading"] = ""
                row_d["heading_path"] = ""
                row_d["sec_content"] = row_d["content"]
                candidates_by_key[row_d["doc_id"]] = row_d

    results: list[dict[str, Any]] = []
    for row in candidates_by_key.values():
        path = str(row["source_path"])
        title = path.rsplit("/", 1)[-1].lower()
        hpath = str(row.get("heading_path") or "").lower()
        sec_text = str(row.get("sec_content") or row["content"])
        sec_lower = sec_text.lower()

        hits = sum(1 for t in sub_terms if t in sec_lower or t in hpath or t in title)
        if not hits and sub_terms:
            continue
        score = hits * 10.0
        if hits == len(sub_terms) and len(sub_terms) > 1:
            score += 25.0
        if any(t in hpath for t in sub_terms):
            score += 18.0
        if any(t in title for t in sub_terms):
            score += 12.0
        score += _doc_tier(path, library_id) * 4.0

        results.append({
            **_citation(row),
            "section": row.get("heading") or "",
            "heading_path": row.get("heading_path") or "",
            "snippet": _extract_snippet(sec_text, sub_terms, 1200),
            "_score": score,
        })
    return results


def _normalize_heading_key(text: str) -> str:
    stripped = re.sub(r"^[\d.]+\s*", "", text.strip())
    return re.sub(r"\s+", " ", stripped).lower()


def call_tool(
    db: Path,
    name: str,
    arguments: Mapping[str, Any],
    *,
    project_id: str,
    expected_token: str | None,
    mode: str = "hybrid",
    remote_url: str | None = None,
) -> dict[str, Any]:
    _authorize(arguments, project_id, expected_token)
    contract_ver = _docs_sync.contract_version_of(BASE_VERSION)

    if name == "resolve_library":
        query = str(arguments.get("name") or "").strip()
        if not query or len(query) > 128:
            raise DocsMcpError("library name 无效")
        q_low = query.lower()
        if q_low in {OFFICIAL_LIBRARY_ID, "bugx", "official", "bugx-official-docs"}:
            return {
                "library_id": OFFICIAL_LIBRARY_ID,
                "library_name": "bugx-official-docs",
                "version": contract_ver,
                "project_id": OFFICIAL_LIBRARY_ID,
            }
        if db.is_file() and not db.is_symlink():
            with _open_readonly(db) as connection:
                row = connection.execute(
                    "SELECT DISTINCT library_id, library_name, version, project_id "
                    "FROM documents WHERE project_id = ? AND (library_id = ? OR library_name LIKE ?) "
                    "ORDER BY version DESC LIMIT 1",
                    (project_id, query, f"%{query}%"),
                ).fetchone()
                if row is not None:
                    return dict(row)
        raise DocsMcpError("未找到受控文档库")

    if name == "search_docs":
        raw_lib = str(arguments.get("library_id") or "").strip()
        library_id = raw_lib or "all"
        version = arguments.get("version")
        try:
            limit = int(arguments.get("limit", 5))
        except (TypeError, ValueError) as exc:
            raise DocsMcpError("limit 必须是整数") from exc
        if not 1 <= limit <= 20:
            raise DocsMcpError("limit 必须在 1 到 20 之间")
        query_str = str(arguments.get("query") or "").strip()
        if not query_str or len(query_str) > 512:
            raise DocsMcpError("query 不能为空且不得超过 512 字符")

        fts_terms, sub_terms = _extract_search_terms(query_str)
        all_hits: list[dict[str, Any]] = []
        search_project = library_id.lower() in {"all", "*", f"project:{project_id}".lower()}
        search_official = library_id.lower() in {"all", "*", OFFICIAL_LIBRARY_ID}

        if search_project and db.is_file() and not db.is_symlink():
            try:
                with _open_readonly(db) as proj_conn:
                    all_hits.extend(_search_single_connection(
                        proj_conn,
                        scope_project_id=project_id,
                        library_id=f"project:{project_id}",
                        version=str(version) if version is not None else None,
                        query_str=query_str,
                        fts_terms=fts_terms,
                        sub_terms=sub_terms,
                        limit=limit,
                    ))
            except DocsMcpError:
                pass

        if search_official:
            remote_done = False
            if mode != "local-only" and not _docs_sync.docs_offline_disabled():
                try:
                    remote_res = _docs_sync.remote_search_docs(
                        query_str, contract_version=contract_ver, limit=limit, remote_url=remote_url,
                    )
                    for idx, item in enumerate(remote_res.get("results") or []):
                        if isinstance(item, dict):
                            all_hits.append({**item, "_score": float(100 - idx)})
                    remote_done = True
                except Exception:
                    if mode == "remote-only":
                        raise
                    remote_done = False

            if not remote_done:
                off_conn = _open_official_connection(
                    contract_version=contract_ver,
                    sync_if_missing=False,
                    offline=(mode == "local-only"),
                    remote_url=remote_url,
                )
                if off_conn is not None:
                    try:
                        all_hits.extend(_search_single_connection(
                            off_conn,
                            scope_project_id=OFFICIAL_LIBRARY_ID,
                            library_id=OFFICIAL_LIBRARY_ID,
                            version=None,
                            query_str=query_str,
                            fts_terms=fts_terms,
                            sub_terms=sub_terms,
                            limit=limit,
                        ))
                    finally:
                        off_conn.close()

        # 若 library_id 为自定义作用域且未命中以上两者，直接在 db 中尝试精确匹配
        if not search_project and not search_official and db.is_file() and not db.is_symlink():
            with _open_readonly(db) as custom_conn:
                all_hits.extend(_search_single_connection(
                    custom_conn,
                    scope_project_id=project_id,
                    library_id=library_id,
                    version=str(version) if version is not None else None,
                    query_str=query_str,
                    fts_terms=fts_terms,
                    sub_terms=sub_terms,
                    limit=limit,
                ))

        all_hits.sort(key=lambda item: float(item.get("_score", 0.0)), reverse=True)
        # 按 (内容哈希 + 章节名称) 去重，避免官方文档与 dev 仓内同类文档重复占用名额
        deduped: list[dict[str, Any]] = []
        seen_sig: set[tuple[str, str]] = set()
        for item in all_hits:
            sig = (str(item.get("sha256") or ""), str(item.get("section") or ""))
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            clean = {k: v for k, v in item.items() if k != "_score"}
            deduped.append(clean)
            if len(deduped) >= limit:
                break

        return {"results": deduped, "count": len(deduped)}

    if name == "list_sections":
        document_id = str(arguments.get("document_id") or "").strip()
        if not document_id:
            raise DocsMcpError("document_id 不能为空")
        row, sec_rows = _find_doc_and_sections(
            db, document_id=document_id, project_id=project_id, contract_ver=contract_ver, mode=mode,
        )
        return {
            **_citation(row),
            "sections": [
                {
                    "section": s["heading"],
                    "heading_path": s["heading_path"],
                    "level": s["level"],
                    "char_count": max(0, int(s["char_end"]) - int(s["char_start"])),
                }
                for s in sec_rows
            ],
        }

    if name == "fetch_doc":
        document_id = str(arguments.get("document_id") or "").strip()
        section = str(arguments.get("section") or "").strip()
        if document_id.startswith(f"{OFFICIAL_LIBRARY_ID}:") and mode != "local-only" and not _docs_sync.docs_offline_disabled():
            try:
                return _docs_sync.remote_fetch_doc(
                    document_id, contract_version=contract_ver, section=section, remote_url=remote_url,
                )
            except Exception:
                if mode == "remote-only":
                    raise
                # 远程读取失败，无感降级到本地 SQLite 兜底
                pass
        row, sec_rows = _find_doc_and_sections(
            db, document_id=document_id, project_id=project_id, contract_ver=contract_ver, mode=mode,
        )
        content = str(row["content"])
        if section:
            content = _extract_exact_section(content, sec_rows, section)
        return {
            **_citation(row),
            "content": _bounded(content),
            "available_sections": [s["heading"] for s in sec_rows[:25]],
        }

    raise DocsMcpError(f"未知文档工具: {name}")


def _find_doc_and_sections(
    db: Path,
    *,
    document_id: str,
    project_id: str,
    contract_ver: str,
    mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    def _query_one(conn: sqlite3.Connection, scope_pid: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        row = conn.execute(
            "SELECT * FROM documents WHERE doc_id = ? AND project_id = ?",
            (document_id, scope_pid),
        ).fetchone()
        if row is None:
            return None
        secs: list[dict[str, Any]] = []
        if _is_v2_db(conn):
            secs = [
                dict(s)
                for s in conn.execute(
                    "SELECT * FROM document_sections WHERE doc_id = ? ORDER BY rowid ASC",
                    (document_id,),
                ).fetchall()
            ]
        return dict(row), secs

    if document_id.startswith(f"{OFFICIAL_LIBRARY_ID}:"):
        off_conn = _open_official_connection(
            contract_version=contract_ver, sync_if_missing=False, offline=(mode == "local-only"),
        )
        if off_conn is not None:
            try:
                found = _query_one(off_conn, OFFICIAL_LIBRARY_ID)
                if found is not None:
                    return found
            finally:
                off_conn.close()

    if db.is_file() and not db.is_symlink():
        with _open_readonly(db) as proj_conn:
            found = _query_one(proj_conn, project_id)
            if found is not None:
                return found

    if not document_id.startswith(f"{OFFICIAL_LIBRARY_ID}:"):
        off_conn = _open_official_connection(
            contract_version=contract_ver, sync_if_missing=False, offline=(mode == "local-only"),
        )
        if off_conn is not None:
            try:
                found = _query_one(off_conn, OFFICIAL_LIBRARY_ID)
                if found is not None:
                    return found
            finally:
                off_conn.close()

    raise DocsMcpError("document_id 不存在或不属于当前项目")


def _extract_exact_section(content: str, sec_rows: list[dict[str, Any]], section: str) -> str:
    """精确或唯一子串匹配标题并切片到下一个同级或更高级标题；未命中或歧义时 fail-closed 返回可用目录。"""
    target_low = section.strip().lower()
    target_norm = _normalize_heading_key(section)
    usable = [s for s in sec_rows if s["heading"] not in {"(document)", "(preamble)"} and "(part " not in s["heading"]]

    if usable:
        # 1. 精确匹配（忽略大小写）
        exact = [s for s in usable if s["heading"].strip().lower() == target_low]
        if len(exact) == 1:
            return content[int(exact[0]["char_start"]):int(exact[0]["char_end"])].strip()
        # 2. 去编号规范化精确匹配（如 '8.5 auth' vs 'auth'）
        norm_hits = [s for s in usable if _normalize_heading_key(s["heading"]) == target_norm]
        if len(norm_hits) == 1:
            return content[int(norm_hits[0]["char_start"]):int(norm_hits[0]["char_end"])].strip()
        # 3. 唯一子串匹配
        sub_hits = [
            s for s in usable
            if target_low in s["heading"].lower() or (target_norm and target_norm in _normalize_heading_key(s["heading"]))
        ]
        if len(sub_hits) == 1:
            return content[int(sub_hits[0]["char_start"]):int(sub_hits[0]["char_end"])].strip()
        if len(sub_hits) > 1:
            cands = ", ".join(s["heading"] for s in sub_hits[:10])
            raise DocsMcpError(f"section 匹配存在多处歧义 ({section!r})，请传入更精确标题，候选: [{cands}]")
        toc = ", ".join(s["heading"] for s in usable[:20])
        raise DocsMcpError(f"未找到章节 {section!r}；可用章节列表: [{toc}]")

    # 极旧单表无 section 索引时的回落边界修复（fenced-code-safe + 同级停止）
    match = re.search(rf"(?im)^(#+)\s*{re.escape(section)}\s*$", content)
    if not match:
        raise DocsMcpError(f"未找到章节 {section!r}")
    lvl = len(match.group(1))
    tail = content[match.end():]
    next_heading = re.search(rf"(?m)^#{{1,{lvl}}}\s+", tail)
    return (match.group(0) + (tail[:next_heading.start()] if next_heading else tail)).strip()


TOOLS = [
    {
        "name": "resolve_library",
        "description": "解析当前项目或 BugX 官方受控文档库 (`bugx:official` / `project:<id>`)；结果只来自本地安全索引。",
        "inputSchema": {"type": "object", "required": ["name", "project_id"], "properties": {
            "name": {"type": "string", "maxLength": 128},
            "project_id": {"type": "string"}, "token": {"type": "string"},
        }},
    },
    {
        "name": "search_docs",
        "description": "在当前项目与 BugX 官方受控文档库的章节级索引中检索并返回可引用证据 (`library_id='all'|'bugx:official'|'project:<id>')。",
        "inputSchema": {"type": "object", "required": ["query", "project_id"], "properties": {
            "library_id": {"type": "string"}, "query": {"type": "string", "maxLength": 512},
            "version": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "project_id": {"type": "string"}, "token": {"type": "string"},
        }},
    },
    {
        "name": "fetch_doc",
        "description": "读取一份受控文档或其中精确章节（支持去编号标题与子小节完整边界），并返回版本、哈希和来源引用。",
        "inputSchema": {"type": "object", "required": ["document_id", "project_id"], "properties": {
            "document_id": {"type": "string"}, "section": {"type": "string"},
            "project_id": {"type": "string"}, "token": {"type": "string"},
        }},
    },
    {
        "name": "list_sections",
        "description": "列出指定文档的完整 Markdown 标题结构目录 (TOC) 与字符预算，便于精准拉取章节。",
        "inputSchema": {"type": "object", "required": ["document_id", "project_id"], "properties": {
            "document_id": {"type": "string"},
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


def serve(
    root: Path,
    db: Path,
    *,
    project_id: str,
    version: str,
    token: str | None = None,
    auto_index: bool = True,
    mode: str = "hybrid",
    remote_url: str | None = None,
) -> int:
    root = root.expanduser().resolve()
    db = _db_path(root, db)
    if auto_index:
        index_documents(root, db, project_id=project_id, version=version)
    if mode != "local-only" and not _docs_sync.docs_offline_disabled():
        try:
            _docs_sync.sync_official_docs(
                contract_version=_docs_sync.contract_version_of(BASE_VERSION),
                remote_url=remote_url,
            )
        except Exception:
            pass
    expected_token = token or os.environ.get("BUGX_DOCS_TOKEN")
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
                payload = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": "调用工具时必须提供当前 project_id；支持 library_id='all'|'bugx:official'|'project:<id>'。",
                }
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
                payload = _result(call_tool(
                    db, params["name"], args,
                    project_id=project_id,
                    expected_token=expected_token,
                    mode=mode,
                    remote_url=remote_url,
                ))
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
    for name in ("index", "serve", "sync"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument("--db", type=Path, default=Path(".bugx/knowledge/docs.sqlite3"))
        command.add_argument("--project-id", default=None)
        command.add_argument("--version", default=None)
        command.add_argument("--token", default=None, help=argparse.SUPPRESS)
        if name == "serve":
            command.add_argument("--no-auto-index", action="store_true")
            command.add_argument("--mode", choices=("hybrid", "local-only", "remote-only"), default="hybrid")
            command.add_argument("--remote-url", default=None)
        if name == "sync":
            command.add_argument("--force", action="store_true")
            command.add_argument("--offline", action="store_true")
            command.add_argument("--remote-url", default=None)
    args = parser.parse_args(argv)
    try:
        root = args.root.expanduser().resolve()
        project_id = _project_id(root, args.project_id)
        version = _version(root, args.version)
        if args.command == "index":
            print(json.dumps(index_documents(root, args.db, project_id=project_id, version=version),
                             ensure_ascii=False))
            return 0
        if args.command == "sync":
            proj_res = index_documents(root, args.db, project_id=project_id, version=version)
            cloud_res = _docs_sync.sync_official_docs(
                contract_version=_docs_sync.contract_version_of(BASE_VERSION),
                force=bool(args.force),
                offline=bool(args.offline),
                remote_url=args.remote_url,
            )
            print(json.dumps({"project": proj_res, "official": cloud_res}, ensure_ascii=False))
            return 0
        return serve(
            root, args.db, project_id=project_id, version=version,
            token=args.token, auto_index=not args.no_auto_index,
            mode=args.mode, remote_url=args.remote_url,
        )
    except DocsMcpError as exc:
        print(f"bugx-docs: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
