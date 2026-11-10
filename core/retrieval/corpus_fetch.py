#!/usr/bin/env python3
"""corpus_fetch.py — 并发搜索开源项目，把真实代码片段纳入种子语料库。

背景：种子语料库的质量决定 fuzz 命中率。手工种子有限，真实开源项目的
代码片段（语法结构、边界输入、惯用写法）远比随机生成的起点更能触发
深层解析路径。本工具**并发**搜索 GitHub 开源项目，拉取代表性源码文件，
提取语法片段去重后并入 seed_corpus/<lang>.txt。

支持按项目定制（因地制宜）：
  - 纯语言：--lang python                → 搜该语言最热门仓库
  - 按项目类型：--lang python --query "json parser"
                                         → 只搜 JSON 解析器类 Python 项目（种子贴合被测项目材质）
  - 指定仓库：--lang rust --repo serde-rs/json
                                         → 直接从指定开源项目提取（测同类项目的上游/对照实现）
  --query 与 --repo 可组合使用（query 缩小搜索范围，repo 精确指定）。

用法：
  python3 corpus_fetch.py --lang python --count 200 --per-repo 20
  python3 corpus_fetch.py --lang python --query "json parser" --count 200
  python3 corpus_fetch.py --lang rust --repo serde-rs/json --count 100
  python3 corpus_fetch.py --lang rust --token <GH_TOKEN>   # 更高 rate limit

参数：
  --lang      语言（python/java/rust/swift/kotlin/c/cpp/typescript/javascript/css）
  --query     按项目类型定制搜索关键词（如 "json parser"/"http client"/
              "markdown"）——只搜该类项目，种子贴合被测项目材质
  --repo      指定仓库 full_name（如 "serde-rs/json"）直接提取，跳过搜索
  --count     目标种子数（默认 200）
  --per-repo  每仓库最多提取种子数（默认 20，防单仓刷屏）
  --token     GitHub token（可选，提升 rate limit 到 5000/h）
  --dry-run   只搜索并打印仓库列表，不下载（联调用）

流程：
  1. 仓库来源：--repo 直接指定；否则 GitHub 仓库搜索 API（并发，
     q=language:<lang> 或 +query 关键词，sort=stars）
  2. 对每个仓库，经 tree API 找到源码文件（按语言扩展名过滤）
  3. 并发下载文件 raw 内容（按仓库总种子预算提前取消未开始任务）
  4. 提取种子行：去掉注释/空行/import 块，截断超长行（>500 字符截断），
     保留含语法结构（括号/引号/关键字/字符串字面量）的行
  5. 去重后追加到 seed_corpus/<lang>.txt（保留已有种子）

安全：只读开源公开代码；不 clone 仓库、不执行任何下载内容。
"""
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
import sys
from pathlib import Path

from pathlib import Path

import argparse
from collections import defaultdict
import json
import random
import sys
import urllib.error
import urllib.request
from core.infra.client_metadata import tls_context
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path

SEED_CORPUS = Path(__file__).resolve().parents[1] / "seed_corpus"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# 语言 → GitHub 仓库搜索语言名 / 源码扩展名 / 跳过行前缀
LANGS = {
    "python":       {"gh": "python",       "ext": ".py",       "skip": ("import", "from ", "#")},
    "java":         {"gh": "java",         "ext": ".java",     "skip": ("import ", "package ", "//")},
    "rust":         {"gh": "rust",         "ext": ".rs",       "skip": ("use ", "//", "//!")},
    "swift":        {"gh": "swift",        "ext": ".swift",    "skip": ("import ", "//")},
    "kotlin":       {"gh": "kotlin",       "ext": ".kt",       "skip": ("import ", "package ", "//")},
    "c":            {"gh": "c",            "ext": ".c",        "skip": ("#include", "#define", "//", "/*")},
    "cpp":          {"gh": "c++",          "ext": ".cpp",      "skip": ("#include", "#define", "//", "/*", "using ")},
    "typescript":   {"gh": "typescript",   "ext": ".ts",       "skip": ("import ", "export ", "//")},
    "javascript":   {"gh": "javascript",   "ext": ".js",       "skip": ("import ", "//")},
    "css":          {"gh": "css",          "ext": ".css",      "skip": ("/*", "*/", "*")},
}


def _get(url: str, token: str | None, timeout: float = 10) -> dict | bytes:
    """GET 并返回 JSON 或原始 bytes。"""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "bugx-corpus-fetch")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout, context=tls_context()) as r:
        declared = r.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise ValueError(f"响应超过 {MAX_RESPONSE_BYTES} 字节上限")
        data = r.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError(f"响应超过 {MAX_RESPONSE_BYTES} 字节上限")
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype or isinstance(data, bytes) and data[:1] == b"{":
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return data
        return data


def search_repos(lang: str, token: str | None, query: str | None = None,
                 per_page: int = 10, sort: str = "stars") -> list[str]:
    """搜索该语言的热门开源仓库（可选按项目类型 query 定制），返回 full_name 列表。"""
    return [item["full_name"] for item in search_repo_details(
        lang, token, query=query, per_page=per_page, sort=sort,
    )]


def search_repo_details(lang: str, token: str | None, query: str | None = None,
                        per_page: int = 10, sort: str = "stars") -> list[dict]:
    """同 search_repos 但返回 star/license/更新时间等元数据（差分参考选型用）。"""
    from urllib.parse import quote

    cfg = LANGS[lang]
    q = f"language:{cfg['gh']}"
    if query:
        # 按项目类型定制：JSON 解析器 / HTTP client / markdown 等，只搜该类项目
        # 用 + 连接（GitHub 搜索 AND 语义），query 内部空格再 URL 编码
        q += f"+{quote(query)}"
    q += f"&sort={sort}&order=desc"
    url = f"{API}/search/repositories?q={q}&per_page={per_page}"
    d = _get(url, token)
    if isinstance(d, bytes) or not d.get("items"):
        return []
    return [
        {
            "full_name": item.get("full_name"),
            "stars": item.get("stargazers_count"),
            "license": (item.get("license") or {}).get("spdx_id"),
            "pushed_at": item.get("pushed_at"),
            "url": item.get("html_url"),
            "description": item.get("description"),
        }
        for item in d["items"]
    ]


def list_source_files(repo: str, lang: str, token: str | None,
                      limit: int = 50) -> list[str]:
    """通过 git trees API 列出仓库内源码文件路径。"""
    cfg = LANGS[lang]
    url = f"{API}/repos/{repo}/git/trees/HEAD?recursive=1"
    try:
        d = _get(url, token)
    except urllib.error.HTTPError:
        return []
    if isinstance(d, bytes):
        return []
    paths = []
    for t in d.get("tree", []):
        p = t.get("path", "")
        if t.get("type") == "blob" and p.endswith(cfg["ext"]) and len(p) < 200:
            paths.append(p)
        if len(paths) >= limit:
            break
    return paths


def fetch_raw(repo: str, path: str, timeout: float = 10,
              max_bytes: int = 64 * 1024) -> str | None:
    """下载文件 raw 内容（只读前 max_bytes，防超大文件拖慢）。"""
    url = f"{RAW}/{repo}/HEAD/{path}"
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "bugx-corpus-fetch")
        with urllib.request.urlopen(req, timeout=timeout, context=tls_context()) as r:
            d = r.read(max_bytes + 1)
        if isinstance(d, bytes):
            try:
                return d.decode("utf-8", errors="replace")[:max_bytes]
            except Exception:  # noqa: BLE001
                return None
        return None
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            ConnectionError):
        return None


def extract_seeds(text: str, lang: str, limit: int) -> list[str]:
    """从源码提取语法种子行（去注释/空行/import，截断超长行）。"""
    cfg = LANGS[lang]
    seeds: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) > 500:
            continue
        if s.startswith(cfg["skip"]):
            continue
        # 保留含语法结构的行
        if any(ch in s for ch in ("(", ")", "{", "}", "\"", "'", "=", ":", ",", "[")):
            seeds.append(s)
        if len(seeds) >= limit:
            break
    return seeds


def dedup_append(lang: str, new_seeds: list[str]) -> tuple[int, int]:
    """去重后追加到 seed_corpus/<lang>.txt，返回 (新增数, 总数)。"""
    f = SEED_CORPUS / f"{lang}.txt"
    existing: set[str] = set()
    if f.is_file():
        existing = {
            ln.strip()
            for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        }
    added = []
    seen = set(existing)
    for seed in new_seeds:
        if seed in seen:
            continue
        seen.add(seed)
        added.append(seed)
    with f.open("a", encoding="utf-8") as fh:
        for s in added:
            fh.write(s + "\n")
    return len(added), len(existing) + len(added)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lang", required=True, choices=sorted(LANGS),
                   help="目标语言")
    p.add_argument("--query", default=None,
                   help="按项目类型定制搜索关键词（如 'json parser'/'http client'），"
                        "只搜该类项目，种子贴合被测项目材质")
    p.add_argument("--repo", default=None,
                   help="指定仓库 full_name（如 'serde-rs/json'）直接提取，跳过搜索")
    p.add_argument("--count", type=int, default=200, help="目标种子数（默认 200）")
    p.add_argument("--per-repo", type=int, default=20,
                   help="每仓库最多提取种子数（默认 20）")
    p.add_argument("--token", default=None, help="GitHub token（可选，提升 rate limit）")
    p.add_argument("--dry-run", action="store_true", help="只搜索并打印仓库列表不下载")
    p.add_argument("--find-reference", action="store_true",
                   help="差分参考实现选型：搜索同能力开源项目并打印 star/license/"
                        "活跃度候选清单（供 differential_probe --reference-cmd 用），"
                        "不提取种子")
    p.add_argument("--seed", type=int, default=None, help="随机种子")
    args = p.parse_args()
    if args.count < 1 or args.per_repo < 1:
        print("[corpus_fetch] --count 与 --per-repo 必须 >= 1")
        return 2
    if args.find_reference:
        if not args.query:
            print("[corpus_fetch] --find-reference 必须配合 --query 描述目标能力"
                  "（如 'json parser'）")
            return 2
        print(f"[corpus_fetch] 搜索 {args.lang} 的 {args.query!r} 参考实现候选…")
        candidates = search_repo_details(
            args.lang, args.token, query=args.query, per_page=10,
        )
        if not candidates:
            print("[corpus_fetch] ✗ 未找到候选（rate limit 或网络问题）")
            return 1
        print("[corpus_fetch] 参考实现候选（人工核验许可证与适配性后使用）：")
        for item in candidates:
            license_note = item.get("license") or "NOASSERTION"
            print(f"  {item['stars']:>6}★ {str(license_note):<12} "
                  f"{item['full_name']}  (pushed {item.get('pushed_at')})")
            if item.get("description"):
                print(f"          {str(item['description'])[:90]}")
        print("[corpus_fetch] 选定后：克隆/安装该实现，构造 "
              "--reference-cmd，语料用本工具常规模式提取。")
        return 0

    # 仓库来源：--repo 指定 或 搜索（按语言 + 可选 query 定制）
    if args.repo:
        repos = [args.repo]
        print(f"[corpus_fetch] 指定仓库: {args.repo}")
    else:
        src = f"语言={args.lang}" + (f" 项目类型={args.query!r}" if args.query else "")
        print(f"[corpus_fetch] 搜索 {src} 开源项目…")
        repos = search_repos(args.lang, args.token, query=args.query)
        if not repos:
            print("[corpus_fetch] ✗ 未找到仓库（rate limit 或网络问题）")
            return 1
        print(f"[corpus_fetch] 找到 {len(repos)} 个热门仓库: {repos[:5]}…")

    if args.dry_run:
        print("[corpus_fetch] dry-run: 不下载。")
        return 0

    # 并发列出源码文件
    all_files: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=min(10, len(repos))) as pool:
        futs = {pool.submit(list_source_files, r, args.lang, args.token): r
                for r in repos}
        for fut in as_completed(futs):
            repo = futs[fut]
            try:
                all_files[repo] = fut.result()
            except Exception:  # noqa: BLE001
                all_files[repo] = []

    # 并发下载 + 提取（每仓库最多提取 per_repo 条唯一种子，控制总量）
    rng = random.Random(args.seed)
    collected: list[str] = []
    collected_seen: set[str] = set()
    repo_seen: dict[str, set[str]] = defaultdict(set)
    repo_futures: dict[str, set[object]] = defaultdict(set)
    jobs = [
        (repo, path)
        for repo, paths in all_files.items()
        for path in paths
    ]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as pool:
        futs = {}
        for repo, path in jobs:
            future = pool.submit(fetch_raw, repo, path)
            futs[future] = (repo, path)
            repo_futures[repo].add(future)
        stop_requested = False
        for fut in as_completed(futs):
            repo, path = futs[fut]
            repo_futures[repo].discard(fut)
            try:
                text = fut.result()
            except CancelledError:
                continue
            except Exception:  # noqa: BLE001
                continue
            remaining = args.per_repo - len(repo_seen[repo])
            if text and remaining > 0:
                for seed in extract_seeds(text, args.lang, remaining):
                    if seed in repo_seen[repo]:
                        continue
                    repo_seen[repo].add(seed)
                    if seed in collected_seen:
                        continue
                    collected_seen.add(seed)
                    collected.append(seed)
                    if len(collected) >= args.count:
                        stop_requested = True
                        break
            if len(repo_seen[repo]) >= args.per_repo:
                for pending in tuple(repo_futures[repo]):
                    pending.cancel()
            if stop_requested:
                for pending in futs:
                    if pending is not fut:
                        pending.cancel()
                break

    # 随机采样到目标数量
    if len(collected) > args.count:
        rng.shuffle(collected)
        collected = collected[:args.count]

    added, total = dedup_append(args.lang, collected)
    print(f"[corpus_fetch] 提取 {len(collected)} 条，新增 {added} 条"
          f"（seed_corpus/{args.lang}.txt 现有 {total} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
