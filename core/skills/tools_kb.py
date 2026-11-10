#!/usr/bin/env python3
"""tools_kb.py — tools-kb.md 的验证日期/工具探测器 + 工具经验联动。

用法：
  python3 tools_kb.py check
  python3 tools_kb.py status
  python3 tools_kb.py experience [--ledger .bugx/knowledge/lesson-ledger.json] [--json]

exit 0 = 知识库可用；日期和可选工具探测异常只输出 warning；
exit 2 = 知识库缺失或没有可解析条目。
experience 是信息型子命令：把目录新鲜度与错题账本的 tool_usage 实测经验
（等级/命中/类别）合并展示，永远 exit 0；账本缺失只降级提示，不算失败。
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
import datetime as dt
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "skills" else Path(__file__).resolve().parent
# 目录权威位置是 ``core/planning/tools-kb.md``（随 planning 包归档）。扁平候选
# ``core/tools-kb.md`` 已被迁移删除，保留它只会让"路径漂移"永远拿不到错误信号
# （这正是 L7-7「引擎资产引用有效」设闸的理由）。唯一真源，**不要在别处重复推导**
# ——重复即漂移，见 ``launch_phases.supervisor_bridge.run_tools_kb`` 直接复用 ``KB_FILE``。
KB_FILE = _CORE_DIR / "planning" / "tools-kb.md"
DEFAULT_LEDGER = Path(".bugx/knowledge/lesson-ledger.json")
MAX_AGE_DAYS = 30
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

# The catalog is documentation, while these probes observe the executable
# actually available to the current process.  Missing entries are optional.
TOOL_COMMANDS = {
    "ripgrep": "rg",
    "ast-grep": "ast-grep",
    "jq": "jq",
    "playwright": "playwright",
    "postmcp": "postmcp",
    "schemathesis": "schemathesis",
    "restler": "restler",
    "k6": "k6",
    "vegeta": "vegeta",
    "codebase-memory-mcp": "codebase-memory-mcp",
    "semgrep": "semgrep",
    "codeql": "codeql",
    "trivy": "trivy",
    "bandit": "bandit",
    "maestro": "maestro",
    "appium": "appium",
    "uiautomator2": "uiautomator2",
    "mitmproxy": "mitmproxy",
}
# Optional version probes must not turn a startup warning into a long gate.
VERSION_PROBE_TIMEOUT_SECONDS = 0.25


@dataclass(frozen=True)
class Entry:
    line: int
    name: str
    date: dt.date | None
    error: str | None = None
    version: str | None = None
    command: str | None = None


def read_entries(path: Path = KB_FILE) -> list[Entry]:
    if not path.is_file():
        raise FileNotFoundError(path)
    entries: list[Entry] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"工具", "语言", "数据库"}:
            continue
        # 工具表的日期是最后一列；没有日期的普通 Markdown 行不计入条目。
        date_text = cells[-1]
        version = cells[3] if len(cells) >= 5 else None
        command = TOOL_COMMANDS.get(cells[0].casefold())
        if date_text in {"说明", "用途", "来源", "验证日期"}:
            continue
        if not DATE_RE.match(date_text):
            # 只有看起来像工具表的行才报缺日期；分隔线跳过。
            if set(date_text) <= {"-", " "}:
                continue
            if len(cells) >= 4:
                entries.append(Entry(
                    number, cells[0], None, "缺少 YYYY-MM-DD 验证日期",
                    version=version, command=command,
                ))
            continue
        try:
            parsed = dt.date.fromisoformat(date_text)
        except ValueError:
            entries.append(Entry(
                number, cells[0], None, f"日期无效: {date_text}",
                version=version, command=command,
            ))
            continue
        entries.append(Entry(
            number, cells[0], parsed, version=version, command=command,
        ))
    return entries


def _version_token(value: str | None) -> str | None:
    if not value or value.strip() in {"待验证", "—", "-"}:
        return None
    match = VERSION_RE.search(value)
    return match.group(0) if match else None


def _physical_probe_warning(entry: Entry) -> str | None:
    """Observe the real optional command without making the catalog mandatory."""
    if not entry.command:
        return None
    executable = shutil.which(entry.command)
    if not executable:
        return f"第 {entry.line} 行 {entry.name}: 物理命令 {entry.command} 不存在（可选，warning）"
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"第 {entry.line} 行 {entry.name}: 无法探测 {entry.command} 版本: {exc}（warning）"
    output = (result.stdout or result.stderr or "").strip().splitlines()
    actual = output[0].strip() if output else ""
    if result.returncode != 0 or not actual:
        return f"第 {entry.line} 行 {entry.name}: {entry.command} --version 失败（warning）"
    expected_token = _version_token(entry.version)
    actual_token = _version_token(actual)
    if expected_token and actual_token and expected_token != actual_token:
        return (
            f"第 {entry.line} 行 {entry.name}: 文档版本 {expected_token}，"
            f"物理命令版本 {actual_token}（以物理命令为准，warning）"
        )
    return None


def check(path: Path = KB_FILE, today: dt.date | None = None,
          max_age: int = MAX_AGE_DAYS) -> tuple[int, list[str]]:
    today = today or dt.date.today()
    try:
        entries = read_entries(path)
    except (FileNotFoundError, OSError, UnicodeError) as exc:
        hint = ""
        # PE-20261109-001：build.421 及更早的 pip wheel 把 *.md 装进
        # planning/docs/ 子目录（pyproject 收集映射错位），KB_FILE 指向
        # planning/ 根——缺文件报错曾只有一行异常，用户无从下手。检测到
        # 旧布局兄弟文件时给出可操作指引（保持 fail-closed，不做静默回退）。
        try:
            legacy = Path(path).parent / "docs" / Path(path).name
            if legacy.is_file():
                hint = (f"（检测到旧 wheel 布局 {legacy}——升级 bugx_agent 或"
                        f" cp 到 {Path(path)} 后重试）")
        except OSError:
            pass
        return 2, [f"知识库无法读取: {exc}{hint}"]
    if not entries:
        return 2, ["知识库没有可验证的工具条目"]
    warnings: list[str] = []
    for entry in entries:
        if entry.error:
            warnings.append(f"第 {entry.line} 行 {entry.name}: {entry.error}")
        else:
            assert entry.date is not None
            age = (today - entry.date).days
            if age < 0:
                warnings.append(f"第 {entry.line} 行 {entry.name}: 验证日期在未来 {entry.date}")
            elif age > max_age:
                warnings.append(f"第 {entry.line} 行 {entry.name}: 已过期 {age} 天（上限 {max_age}）")
        probe_warning = _physical_probe_warning(entry)
        if probe_warning:
            warnings.append(probe_warning)
    return 0, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "status", "experience"), nargs="?", default="check")
    parser.add_argument("--today", help="测试用日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--max-age", type=int, default=MAX_AGE_DAYS)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                        help="experience 子命令使用的错题账本路径")
    parser.add_argument("--json", action="store_true", help="experience 输出 JSON")
    args = parser.parse_args(argv)
    today = dt.date.fromisoformat(args.today) if args.today else None
    if args.command == "experience":
        payload = experience(args.ledger, today=today, max_age=args.max_age)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[tools_kb] 目录 {payload['catalog_total']} 条，"
                  f"账本工具 {payload['ledger_tools_total']} 个，"
                  f"有效期 {args.max_age} 天")
            if payload.get("ledger_error"):
                print(f"  ⚠ 账本不可用：{payload['ledger_error']}")
            for item in payload["joined"]:
                cats = "、".join(
                    f"{c['category']}×{c['hits']}" for c in item["top_categories"]
                ) or "—"
                print(f"  {item['name']:<24} 目录:{item['verified_date']}"
                      f"({item['freshness']})  经验:{item['experience_level']}"
                      f"  命中 {item['hit_count']}/{item['use_count']}  类别: {cats}")
            for name in payload["ledger_only"]:
                print(f"  ⚠ {name}: 账本有实测经验但目录缺登记，请补录 tools-kb.md")
        return 0
    return _run_check(KB_FILE, today=today, max_age=args.max_age)


def _run_check(path: Path | None = None, today: dt.date | None = None,
               max_age: int = MAX_AGE_DAYS) -> int:
    """进程内直接调用入口：验证工具条目日期有效性，输出结果并返回退出码。"""
    active_path = path or KB_FILE
    rc, errors = check(path=active_path, today=today, max_age=max_age)
    entries = read_entries(active_path) if rc != 2 else []
    print(f"[tools_kb] 条目 {len(entries)}，有效期 {max_age} 天")
    if errors:
        mark = "✗" if rc == 2 else "⚠"
        for error in errors:
            print(f"  {mark} {error}")
        if rc == 2:
            print("[tools_kb] 知识库配置错误，无法继续")
        else:
            print("[tools_kb] warning：目录或可选工具探测需要更新，但不阻断启动")
    else:
        print("[tools_kb] OK: 知识库可用，物理命令探测无异常")
    return rc


def _ledger_module():
    path = _CORE_DIR / "ledger" / "lesson_ledger.py"
    if not path.is_file():
        path = Path(__file__).resolve().parent / "ledger" / "lesson_ledger.py"
    spec = importlib.util.spec_from_file_location("bugx_tools_kb_ledger", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def experience(ledger: Path = DEFAULT_LEDGER, *, path: Path = KB_FILE,
               today: dt.date | None = None, max_age: int = MAX_AGE_DAYS) -> dict:
    """目录新鲜度 × 账本实测经验（等级/命中/类别）的只读联动视图。"""
    today = today or dt.date.today()
    entries = read_entries(path)
    by_name: dict[str, Entry] = {}
    for entry in entries:
        if entry.error is None and entry.date is not None:
            by_name.setdefault(entry.name, entry)
    ledger_tools: dict[str, dict] = {}
    ledger_error: str | None = None
    try:
        module = _ledger_module()
        database = module.database_path(ledger)
        if not database.is_file():
            ledger_error = f"缺少账本运行库: {database}"
        else:
            metrics = module.tool_metrics(ledger)
            ledger_tools = {item["tool_name"]: item for item in metrics["tools"]}
    except (OSError, ValueError, FileNotFoundError) as exc:
        ledger_error = str(exc)
    joined = []
    for name, entry in sorted(by_name.items()):
        age = (today - entry.date).days
        stats = ledger_tools.get(name)
        joined.append({
            "name": name,
            "verified_date": entry.date.isoformat(),
            "age_days": age,
            "freshness": "ok" if 0 <= age <= max_age else "expired",
            "experience_level": stats["experience_level"] if stats else "unproven",
            "hit_count": stats["hit_count"] if stats else 0,
            "use_count": stats["use_count"] if stats else 0,
            "precision": stats["precision"] if stats else None,
            "top_categories": stats["top_categories"] if stats else [],
        })
    return {
        "catalog_total": len(by_name),
        "ledger_tools_total": len(ledger_tools),
        "joined": joined,
        "catalog_only": [name for name in by_name if name not in ledger_tools],
        "ledger_only": sorted(name for name in ledger_tools if name not in by_name),
        **({"ledger_error": ledger_error} if ledger_error else {}),
    }


if __name__ == "__main__":
    sys.exit(main())
