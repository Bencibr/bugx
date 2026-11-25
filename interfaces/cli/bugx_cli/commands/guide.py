#!/usr/bin/env python3
"""BugX 本地/云端协同文档与操作指南检索指令 (RAG on-demand)。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..common import (
    _ensure_core_importable,
    resolve_project_root,
)

_BUILTIN_GUIDES = [
    (
        {"login", "登录", "auth", "认证", "云端", "non-blocking", "verify"},
        "云端登录与非阻塞认证",
        "运行 `bugx auth login --root . --non-blocking` 立即获取授权链接与用户码；用户授权后运行 `bugx auth verify --root .` 保存凭据，或使用 `bugx auth set-key --api-key <KEY>` 极速鉴权。",
    ),
    (
        {"doctor", "诊断", "环境", "repair", "修复", "missing"},
        "环境诊断与依赖修复",
        "运行 `bugx doctor --root .` 检查引擎、依赖与宿主配置状态；若提示资产或依赖缺失，按提示执行 `bugx doctor --repair` 或 `bugx update --repair-assets`。",
    ),
    (
        {"sync", "同步", "经验", "错题", "lesson", "knowledge"},
        "经验同步与错题检索",
        "运行 `bugx cloud status --root . --scope all --json` 核对状态后使用 `bugx cloud sync --root . --scope all` 同步；查询历史错题经验运行 `bugx lesson retrieve --root . --query \"<关键词>\" --json`。",
    ),
    (
        {"init", "初始化", "新建"},
        "项目初始化规范",
        "进入目标项目根目录，运行 `bugx init --root .`，系统将把精简通用 Skill 安装到 `.agents/skills/bugx/SKILL.md`，可在宿主中通过 `/bugx` 激活。",
    ),
    (
        {"winerror", "32", "lock", "锁定"},
        "文件锁定 / WinError 32 处置",
        "新版 `bugx-docs` 采用不可变快照切换 + 延迟 GC，不直接覆盖正在只读占用的 SQLite 句柄；若遇残留占用，请关闭冗余宿主会话并重跑 `bugx mcp docs sync --root . --force`。",
    ),
]


def _lookup_builtin_guide(query: str) -> dict[str, str] | None:
    q_low = query.lower()
    for keywords, title, instruction in _BUILTIN_GUIDES:
        if any(k in q_low for k in keywords):
            return {"source": "builtin", "section": title, "heading_path": title, "snippet": instruction}
    return None


def command_guide(args: argparse.Namespace) -> int:
    """检索 BugX 官方文档与当前项目文档，返回精准章节步骤与引用。"""
    root = resolve_project_root(args.root).resolve()
    query = str(args.query or "").strip()
    if not query:
        print("[bugx guide] 错误: 查询关键词不能为空", file=sys.stderr)
        return 1

    limit = max(1, min(int(getattr(args, "limit", 3) or 3), 10))
    as_json = bool(getattr(args, "json", False))
    reindex = bool(getattr(args, "reindex", False))
    offline = bool(getattr(args, "offline", False))

    _ensure_core_importable()
    try:
        from core.skills import bugx_docs_cloud_sync as docs_sync
        from core.skills import bugx_docs_mcp_server as docs_server
    except ImportError as exc:
        print(f"[bugx guide] 错误: 无法加载文档检索核心: {exc}", file=sys.stderr)
        return 1

    db_path = root / ".bugx" / "knowledge" / "docs.sqlite3"
    project_id = root.name

    # 1. 尝试轻量级云端官方缓存同步（尊重 1h TTL 与 120s 故障退避，阻塞不超过 1.0s）
    if not offline and not docs_sync.docs_offline_disabled():
        try:
            docs_sync.sync_official_docs(
                force=reindex, offline=offline, manifest_timeout=1.0, bundle_timeout=3.0,
            )
        except Exception:
            pass

    # 2. 按需建立/刷新当前项目私有索引；若目标目录只读 (NEG-03)，静默退化，仍可读官方库
    if not db_path.is_file() or reindex:
        try:
            docs_server.index_documents(
                root, db_path, project_id=project_id, version="working-tree",
            )
        except Exception:
            pass

    results: list[dict[str, Any]] = []
    try:
        raw_res = docs_server.call_tool(
            db_path,
            "search_docs",
            {"query": query, "limit": limit, "library_id": "all", "project_id": project_id},
            project_id=project_id,
            expected_token=None,
            mode="local-only" if offline else "hybrid",
        )
        results = list(raw_res.get("results") or [])
    except Exception as exc:
        builtin = _lookup_builtin_guide(query)
        if builtin:
            results = [builtin]
        else:
            print(f"[bugx guide] 检索失败: {exc}", file=sys.stderr)
            return 1

    # 3. 无论本地库是否存在，只要双域均未命中即回落内置字典兜底
    if not results:
        builtin = _lookup_builtin_guide(query)
        if builtin:
            results = [builtin]

    if as_json:
        print(json.dumps({"query": query, "count": len(results), "results": results}, ensure_ascii=False, indent=2))
        return 0

    if not results:
        print(f"📖 BugX 知识检索: '{query}'")
        print("未检索到相关操作步骤或文档。")
        print("💡 建议：尝试使用更简短的关键词，如：'云端登录'、'doctor'、'错题集'、'poc'、'WinError 32'")
        return 0

    print(f"📖 BugX 知识检索: '{query}' (找到 {len(results)} 条相关指引)\n")
    for idx, item in enumerate(results, 1):
        source = item.get("source", "unknown")
        lib = item.get("library_id", "")
        hpath = item.get("heading_path") or item.get("section") or ""
        header = f"[{idx}] {source}" + (f" § {hpath}" if hpath else "") + (f" ({lib})" if lib else "")
        snippet = str(item.get("snippet") or "").strip()
        print(header)
        print("─" * 60)
        print(snippet)
        print()

    return 0
