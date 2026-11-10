#!/usr/bin/env python3
"""pattern_probe.py — 把错题模式的「同类排查点」编译成可执行扫描。

错题集的价值在「往哪挖」，但 probes 一直是自然语言说明书，靠 agent 自觉
执行。本工具按模式类别内置机械化正则库，对目标源码做有界扫描，输出
「per-pattern 嫌疑文件:行」清单——启动注入的经验卡从"往这类方向挖"变成
"从这 N 行开始挖"。

运行时才能验证的类别（UI 渲染、真实 HTTP 行为）标记 runtime-only，
不在静态扫描里伪装覆盖。

用法：
  python3 pattern_probe.py scan --ledger .bugx/knowledge/lesson-ledger.json \
    --root <目标根> [--query "并发 状态"] [--limit-patterns 6] [--json]

只读、确定性、受预算约束（文件数/文件大小/每模式命中数全部封顶）。
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

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR), str(_CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCRIPT_DIR = _CURRENT_DIR
MAX_FILES = 2000
MAX_FILE_BYTES = 512 * 1024
MAX_MATCHES_PER_PATTERN = 200
SNIPPET_WIDTH = 100
DEFAULT_LEDGER = Path(".bugx/knowledge/lesson-ledger.json")

SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".java", ".kt",
    ".kts", ".go", ".rs", ".swift", ".c", ".h", ".cc", ".cpp", ".cxx", ".cs",
    ".rb", ".php", ".scala", ".sh", ".sql", ".tf",
}
SKIP_DIRS = {
    ".git", ".bugx", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", ".tox", ".mypy_cache", ".pytest_cache",
    "vendor", "Pods", ".gradle", ".idea",
}

# 类别 → 机械化探针（正则）。None = runtime-only，静态扫描不覆盖。
# 正则刻意保守：宁可漏报交给 agent，不可高频误报淹没线索。
PROBE_LIBRARY: dict[str, list[str] | None] = {
    "type-assumption": [
        r"float\(\s*[A-Za-z_]", r"int\(\s*[A-Za-z_]",
        r"\.value\b", r"['\"]timestamp['\"]\]", r"json\.loads\([^)]*\)\[",
    ],
    "ordering": [
        r"ascending\s*=\s*True", r"ORDER\s+BY[^\n]*ASC",
        r"\.sort\(\s*\)", r"sorted\([^)]*\)\[:", r"\.limit\(\s*\d+\s*\)",
    ],
    "secrets": [
        r"_?sanitize|_?redact", r"decrypt\(", r"Fernet",
        r"api[_-]?key|Authorization|Bearer\s",
    ],
    "concurrency": [
        r"asyncio\.run\(", r"ThreadPool(Executor)?|multiprocessing",
        r"(max|min)\(\s*[A-Za-z_].*(?:,\s*key)?\)", r"async\s+def.*await",
    ],
    "contract": [
        r"\.get\(['\"][a-z_]+['\"]\s*,\s*\{\}\)", r"result\[['\"]result['\"]\]",
        r"(warning|caution|fallback)",
    ],
    "regression": [
        r"re\.(search|match|findall|sub)\(", r"\.find\(.{0,40}\)\s*[+\-]",
    ],
    "drift": [
        r"(SUBCOMMANDS|COMPLETION|completion_items|choices\s*=\s*\[)",
        r"(whitelist|allowlist|blacklist)",
    ],
    "validation": [
        r"def _?validate_[a-z_]+\(", r"isinstance\([^)]*\)\s*(or|and)\s*not",
        r"(probation|promote|register)_tool",
    ],
    "streaming": [
        r"text/event-stream", r"data:", r"for\s+\w+\s+in\s+.*(line|chunk|event)",
    ],
    "input-boundary": [
        r"re\.(search|match|findall|sub)\([^)]*(?:\[\\w|\\b)", r"(?<!\\w)",
        r"@\[[^\]]+\]",
    ],
    "partial-failure": [
        r"except\s+[A-Za-z_.]+\s*:\s*$", r"def _(load|replay|_load)",
        r"except[^:]*:\s*(pass|continue|break)\b",
    ],
    "ui": None,
    "api-contract": [
        r"status(_code)?\s*==?\s*\d{3}", r"@(app|router|blueprint)\.(get|post|put|delete)",
        r"require(s)?_(auth|login|token)",
    ],
    # 认证/回调地址拼接类：服务端把内网 host 拼进发给用户的授权 URL，
    # 用户浏览器无法携带会话完成确认（原型：device-code verification_uri 用 127.0.0.1:8080）。
    "auth-flow": [
        r"(verification_uri|device_code|authorize[_-]?url|redirect_uri|callback[_-]?url)",
        r"(base[_-]?url|public[_-]?url|site[_-]?url|frontend[_-]?url|verification[_-]?base[_-]?url)\s*[=:]",
        r"https?://(127\.0\.0\.1|localhost|0\.0\.0\.0)(:\d+)?",
        r"(getBaseURL|resolveBaseURL|publicURL)\s*\(",
    ],
    "oracle-integrity": [
        r"(PASS|READY|COMPLETE|RESOLVED)\b", r"except[^:]*:\s*(pass|continue|\[\]|\{\}|None)",
        r"exit\s*==?\s*0",
    ],
}


class ProbeError(ValueError):
    """探针扫描协议或输入无效。"""


def _ledger_module():
    path = SCRIPT_DIR.parent / "ledger" / "lesson_ledger.py"
    if not path.is_file():
        path = SCRIPT_DIR / "lesson_ledger.py"
    spec = importlib.util.spec_from_file_location("bugx_probe_ledger", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iter_source_files(root: Path, *, max_files: int = MAX_FILES) -> list[Path]:
    """收集受预算约束的源码文件清单；符号链接目录不进入。"""
    if not root.is_dir():
        raise ProbeError(f"--root 不是目录: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= max_files:
            break
        if path.is_symlink() or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def scan(ledger_path: Path, root: Path, *, query: str = "",
         limit_patterns: int = 6) -> dict:
    """检索相关模式 → 编译类别探针 → 有界扫描目标源码。"""
    if not isinstance(limit_patterns, int) or isinstance(limit_patterns, bool) \
            or not 1 <= limit_patterns <= 14:
        raise ProbeError("limit-patterns 必须在 1..14")
    ledger = _ledger_module()
    patterns = ledger.retrieve(
        ledger_path, query=query, limit=limit_patterns,
        token_budget=1200, byte_budget=9000,
    )
    if not patterns:
        return {
            "schema_version": 1, "status": "no-patterns",
            "note": "账本检索无相关模式；先运行 review 沉淀经验",
        }
    files = iter_source_files(root)
    contents: list[tuple[Path, list[str]]] = []
    for path in files:
        try:
            contents.append((path, path.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines()))
        except OSError:
            continue

    results: list[dict] = []
    runtime_only: list[str] = []
    total_matches = 0
    for pattern in patterns:
        category = pattern.get("category", "")
        library = PROBE_LIBRARY.get(category)
        if library is None:
            runtime_only.append(category)
            continue
        regexes = [(expr, re.compile(expr)) for expr in library]
        matches: list[dict] = []
        for path, lines in contents:
            relative = str(path.relative_to(root))
            for line_no, line in enumerate(lines, 1):
                hit = next(
                    (expr for expr, regex in regexes if regex.search(line)),
                    None,
                )
                if hit is None:
                    continue
                snippet = line.strip()[:SNIPPET_WIDTH]
                matches.append({
                    "file": relative, "line": line_no,
                    "probe": hit, "snippet": snippet,
                })
                if len(matches) >= MAX_MATCHES_PER_PATTERN:
                    break
            if len(matches) >= MAX_MATCHES_PER_PATTERN:
                break
        total_matches += len(matches)
        results.append({
            "pattern_id": pattern.get("pattern_id"),
            "category": category,
            "title": pattern.get("title"),
            "pattern_hit_count": pattern.get("hit_count", 0),
            "match_count": len(matches),
            "matches": matches[:40],
            "lead_score": len(matches) * (1 + int(pattern.get("hit_count", 0))),
        })
    results.sort(key=lambda item: (-item["lead_score"], item["category"]))
    return {
        "schema_version": 1,
        "status": "ok",
        "root": str(root),
        "query": query,
        "scanned_files": len(files),
        "patterns_scanned": len(results),
        "total_matches": total_matches,
        "leads": results,
        "runtime_only_categories": sorted(set(runtime_only)),
        "note": (
            "命中是嫌疑线索不是结论：按 lead_score 降序人工复核，"
            "再走确认门禁；runtime-only 类别需动态验证"
        ),
        "budget": {
            "max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES,
            "max_matches_per_pattern": MAX_MATCHES_PER_PATTERN,
        },
    }


def check_readiness() -> dict:
    """Validate the packaged pattern engine without opening a project tree."""
    checks = []
    try:
        ledger = _ledger_module()
        ledger_ready = callable(getattr(ledger, "retrieve", None))
        checks.append({
            "name": "lesson_ledger_adapter",
            "ok": ledger_ready,
            "detail": "账本检索适配器已加载" if ledger_ready else "账本检索接口缺失",
        })
    except Exception as exc:
        checks.append({
            "name": "lesson_ledger_adapter", "ok": False,
            "detail": f"账本检索适配器不可用: {type(exc).__name__}: {exc}",
        })
    try:
        regex_count = 0
        for expressions in PROBE_LIBRARY.values():
            if expressions is None:
                continue
            for expression in expressions:
                re.compile(expression)
                regex_count += 1
        checks.append({
            "name": "pattern_library", "ok": regex_count > 0,
            "detail": f"已校验 {regex_count} 条内置规则",
        })
    except (re.error, TypeError) as exc:
        checks.append({
            "name": "pattern_library", "ok": False,
            "detail": f"内置规则不可编译: {type(exc).__name__}: {exc}",
        })
    ready = all(check["ok"] for check in checks)
    return {
        "schema_version": 1,
        "name": "pattern",
        "status": "READY" if ready else "DEGRADED",
        "checks": checks,
        "capabilities": ["pattern_engine", "lesson_ledger_query"] if ready else [],
        "fallbacks": {},
        "warnings": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("check", "scan"), nargs="?", default="scan")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--query", default="", help="检索词（空=通用高信号模式）")
    parser.add_argument("--limit-patterns", type=int, default=6)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "check":
        result = check_readiness()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[{result['status']}] pattern 探针可用性")
            for check in result["checks"]:
                print(f"  {'✓' if check['ok'] else '✗'} {check['name']}: {check['detail']}")
        return 0 if result["status"] == "READY" else 2
    try:
        result = scan(
            args.ledger, args.root, query=args.query,
            limit_patterns=args.limit_patterns,
        )
    except (ProbeError, OSError, ValueError) as exc:
        print(f"[pattern_probe] ✗ {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[pattern_probe] 扫描 {result.get('scanned_files', 0)} 个源码文件，"
              f"{result.get('patterns_scanned', 0)} 个模式，"
              f"命中 {result.get('total_matches', 0)} 条线索")
        for lead in result.get("leads", []):
            print(f"  [{lead['category']}] {lead['title']} "
                  f"(模式命中 {lead['pattern_hit_count']}) → "
                  f"{lead['match_count']} 条，lead_score={lead['lead_score']}")
            for match in lead["matches"][:5]:
                print(f"    {match['file']}:{match['line']}  {match['snippet'][:60]}")
        if result.get("runtime_only_categories"):
            print("  runtime-only（需动态验证）: "
                  + ", ".join(result["runtime_only_categories"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
