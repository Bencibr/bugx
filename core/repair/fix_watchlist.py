#!/usr/bin/env python3
"""fix_watchlist.py — 修复回归看护：巡查已修代码是否仍在原样存在。

incident 深卡存有修后片段（after_snippet）。修复回归（错题第 6 类）目前
只能靠下次恰好扫到；本工具在每轮 post 后巡查：对近期 active incident，
在其上下文文件中检索 after_snippet——修后代码仍在 = 修复健在；找不到 =
可能回归或重构，立即立案复核。不执行任何命令，纯只读文本存在性检查。

用法：
  python3 fix_watchlist.py check --ledger .bugx/knowledge/lesson-ledger.json \
    --root <项目根> [--limit 20] [--json]

exit 0 = 巡查完成（回归项见报告）；exit 2 = 输入错误。
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
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = Path(".bugx/knowledge/lesson-ledger.json")
DEFAULT_LIMIT = 20
MAX_SNIPPET_BYTES = 8192
MIN_SNIPPET_CHARS = 12
MAX_CANDIDATE_FILES = 2000
MAX_CANDIDATE_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_SCAN_BYTES = 64 * 1024 * 1024

SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs",
    ".swift", ".c", ".h", ".cc", ".cpp", ".cs", ".rb", ".php", ".sh",
}
SKIP_DIRS = {
    ".git", ".bugx", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target",
}


class WatchError(ValueError):
    """看护协议或输入无效。"""


@dataclass(frozen=True)
class _CandidateScan:
    files: tuple[Path, ...]
    truncated: bool = False
    rejected_out_of_scope: bool = False


def _ledger_module():
    path = SCRIPT_DIR / "ledger" / "lesson_ledger.py"
    spec = importlib.util.spec_from_file_location("bugx_watch_ledger", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_tokens(s: str) -> str:
    """归一化任意空白字符（换行、制表符、多重空格）为单个空格。"""
    return " ".join(s.split())


def _normalize_snippet_lines(s: str) -> list[str]:
    """剥离每行首尾空白，忽略纯空行，规范化代码行。"""
    return [line.strip() for line in s.strip().splitlines() if line.strip()]


def _contains_code_snippet(text: str, snippet: str) -> bool:
    """具备格式化容差的代码片段存在性匹配。

    支持：
    1. 快速字面包含；
    2. 全量空白归一化（换行与缩进差异容差）；
    3. 规范化行序列滑动窗口比对。
    """
    if snippet in text:
        return True
    norm_snippet = _normalize_tokens(snippet)
    if not norm_snippet:
        return False
    norm_text = _normalize_tokens(text)
    if norm_snippet in norm_text:
        return True
    snip_lines = _normalize_snippet_lines(snippet)
    if not snip_lines:
        return False
    text_lines = _normalize_snippet_lines(text)
    if not text_lines or len(text_lines) < len(snip_lines):
        return False
    # 滑动窗口比对归一化行（容忍缩进差异、多余空行调整）
    window_size = len(snip_lines)
    for i in range(len(text_lines) - window_size + 1):
        if text_lines[i : i + window_size] == snip_lines:
            return True
    return False


def _candidate_files(root: Path, context_hint: str) -> _CandidateScan:
    """返回 root 内有界候选，并标记越界引用与不完整扫描。"""
    root = root.resolve()
    files: list[Path] = []
    seen: set[Path] = set()
    truncated = False
    rejected_out_of_scope = False

    def add_candidate(candidate: Path) -> bool:
        """加入安全项目内源码；返回 False 表示已发现候选上限以外的文件。"""
        nonlocal truncated, rejected_out_of_scope
        try:
            lexical = Path(os.path.abspath(candidate))
            relative = lexical.relative_to(root)
        except (OSError, ValueError):
            rejected_out_of_scope = True
            return True
        if any(part in SKIP_DIRS for part in relative.parts):
            return True

        current = root
        for part in relative.parts:
            current = current / part
            try:
                if current.is_symlink():
                    rejected_out_of_scope = True
                    return True
            except OSError:
                return True

        try:
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file() or resolved.suffix.lower() not in SOURCE_EXTENSIONS:
                return True
        except (OSError, RuntimeError, ValueError):
            return True

        if resolved in seen:
            return True
        if len(files) >= MAX_CANDIDATE_FILES:
            truncated = True
            return False
        seen.add(resolved)
        files.append(resolved)
        return True

    hint = context_hint.strip()
    if hint:
        for token in hint.replace(",", " ").split():
            clean = token.strip("`'\"():")
            if "/" in clean or clean.endswith(tuple(SOURCE_EXTENSIONS)):
                candidate = Path(clean)
                if not candidate.is_absolute():
                    candidate = root / candidate
                if not add_candidate(candidate):
                    break
    if files or rejected_out_of_scope:
        return _CandidateScan(tuple(files), truncated, rejected_out_of_scope)

    if not files:
        # 尝试通过 git ls-files 极速获取项目内受控源码文件
        try:
            import subprocess
            out = subprocess.check_output(
                ["git", "ls-files"], cwd=root, text=True, stderr=subprocess.DEVNULL
            )
            for line in out.splitlines():
                if not add_candidate(root / line.strip()):
                    break
        except Exception:
            files.clear()
            seen.clear()
            truncated = False
            rejected_out_of_scope = False
    if not files:
        for path in root.rglob("*"):
            if not add_candidate(path):
                break
    return _CandidateScan(tuple(files), truncated, rejected_out_of_scope)


def _read_candidate_text(
    root: Path, candidate: Path, *, max_bytes: int,
) -> tuple[str, int, bool]:
    """Revalidate a candidate, then read a bounded prefix through its checked FD."""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(candidate, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("watchlist candidate is no longer a regular file")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root.resolve())
            current = resolved.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise OSError("watchlist candidate escaped root while opening") from exc
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("watchlist candidate changed while opening")
        stream = os.fdopen(fd, "rb")
        fd = -1
        with stream:
            raw = stream.read(max_bytes)
        # Reaching the byte boundary is conservatively incomplete: the file may
        # have more data, or may have grown while the descriptor was open.
        truncated = len(raw) == max_bytes
        return raw.decode("utf-8", errors="replace"), len(raw), truncated
    finally:
        if fd >= 0:
            os.close(fd)


def check(ledger_path: Path, root: Path, *, limit: int = DEFAULT_LIMIT) -> dict:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise WatchError("limit 必须在 1..100")
    if not root.is_dir():
        raise WatchError(f"--root 不是目录: {root}")
    ledger = _ledger_module()
    incidents = [
        item for item in ledger.list_incidents(ledger_path, limit=limit * 4)
        if item.get("status") in {"candidate", "active"}
    ][-limit:]
    entries: list[dict] = []
    intact = regressed = inconclusive = 0
    total_content_bytes_read = 0
    for incident in incidents:
        after = str(incident.get("after_snippet") or "").strip()
        if len(after) < MIN_SNIPPET_CHARS or len(after.encode()) > MAX_SNIPPET_BYTES:
            entries.append({
                "incident_id": incident.get("incident_id"),
                "status": "skipped",
                "reason": "after_snippet 过短/过长，无法可靠巡查",
            })
            inconclusive += 1
            continue
        scan = _candidate_files(root, str(incident.get("context") or ""))
        found_at = None
        read_failures = 0
        content_bytes_read = 0
        content_truncated_files = 0
        content_budget_exhausted = False
        for index, path in enumerate(scan.files):
            remaining = MAX_TOTAL_SCAN_BYTES - total_content_bytes_read
            if remaining <= 0:
                content_budget_exhausted = True
                break
            max_bytes = min(MAX_CANDIDATE_FILE_BYTES, remaining)
            try:
                text, bytes_read, content_truncated = _read_candidate_text(
                    root, path, max_bytes=max_bytes
                )
            except OSError:
                read_failures += 1
                continue
            content_bytes_read += bytes_read
            total_content_bytes_read += bytes_read
            if content_truncated:
                content_truncated_files += 1
            if _contains_code_snippet(text, after):
                found_at = str(path.relative_to(root.resolve()))
                break
            if content_truncated and max_bytes == remaining:
                content_budget_exhausted = True
                break
            if (
                total_content_bytes_read >= MAX_TOTAL_SCAN_BYTES
                and index + 1 < len(scan.files)
            ):
                content_budget_exhausted = True
                break
        reason = None
        reason_code = None
        if found_at:
            verdict, where = "intact", found_at
            intact += 1
        elif (
            scan.truncated or scan.rejected_out_of_scope or read_failures
            or content_truncated_files or content_budget_exhausted
        ):
            verdict, where = "inconclusive", None
            inconclusive += 1
            reason = (
                "候选扫描被上限截断，无法判定" if scan.truncated
                else "上下文包含项目根目录外或符号链接路径，已拒绝读取"
                if scan.rejected_out_of_scope
                else "本次巡查累计读取字节上限已到，无法完成负向判定"
                if content_budget_exhausted
                else "候选文件超过单文件读取上限，无法完成负向判定"
                if content_truncated_files
                else "候选文件读取失败，无法完成负向判定"
            )
            reason_code = (
                "candidate_scan_truncated" if scan.truncated
                else "candidate_scope_rejected" if scan.rejected_out_of_scope
                else "candidate_total_byte_limit" if content_budget_exhausted
                else "candidate_file_too_large" if content_truncated_files
                else "candidate_read_failed"
            )
        elif scan.files:
            verdict, where = "possible-regression", None
            regressed += 1
        else:
            verdict, where = "inconclusive", None
            inconclusive += 1
            reason = "没有找到可巡查的项目内源码文件"
            reason_code = "no_in_scope_candidates"
        entries.append({
            "incident_id": incident.get("incident_id"),
            "pattern_id": incident.get("pattern_id"),
            "summary": incident.get("summary"),
            "status": verdict,
            "after_snippet_at": where,
            "candidate_files_scanned": len(scan.files),
            "candidate_scan_truncated": scan.truncated,
            "candidate_scope_rejected": scan.rejected_out_of_scope,
            "candidate_read_failures": read_failures,
            "candidate_content_bytes_read": content_bytes_read,
            "candidate_content_truncated_files": content_truncated_files,
            "candidate_total_byte_limit_reached": content_budget_exhausted,
            **({"reason": reason} if reason else {}),
            **({"reason_code": reason_code} if reason_code else {}),
        })
    return {
        "schema_version": 1,
        "evaluation": "fix-watchlist",
        "root": str(root),
        "incidents_watched": len(entries),
        "intact": intact,
        "possible_regressions": regressed,
        "inconclusive": inconclusive,
        "candidate_content_bytes_read_total": total_content_bytes_read,
        "entries": entries,
        "note": (
            "possible-regression 只代表修后片段在候选文件中找不到——"
            "可能是回归，也可能是重构改名；按 pattern_id 复核后才能立案"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("check",), nargs="?", default="check")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check(args.ledger, args.root, limit=args.limit)
    except (WatchError, OSError, ValueError) as exc:
        print(f"[fix_watchlist] ✗ {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[fix_watchlist] 巡查 {result['incidents_watched']} 个修复："
              f"健在 {result['intact']}，疑似回归 {result['possible_regressions']}，"
              f"不可判定 {result['inconclusive']}")
        for entry in result["entries"]:
            if entry.get("status") in {"possible-regression", "skipped"}:
                print(f"  ⚠ {entry.get('incident_id')} [{entry['status']}] "
                      f"{entry.get('summary', '')[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
