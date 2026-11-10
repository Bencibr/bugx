#!/usr/bin/env python3
"""探测通用效率工具和文档 MCP；降级必须由调用方明确放行。"""

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
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

if __package__:
    from ..retrieval import symbol_index
    from ..runtime.tool_install_targets import OPTIONAL_TOOL_PACKAGE_SPECS
else:
    from core.retrieval import symbol_index
    from core.runtime.tool_install_targets import OPTIONAL_TOOL_PACKAGE_SPECS

VALIDATED = {
    "ripgrep": "15.2.0",
    "ast-grep": "0.45.1",
    "jq": "1.8.2",
    "github-mcp-server": "1.9.0",
}
MINIMUM = {"ripgrep": "14.0.0", "ast-grep": "0.35.0", "jq": "1.7.0"}


def _probe_environment() -> dict[str, str]:
    try:
        try:
            from .probe_registry import normalized_tool_environment
        except (ImportError, ValueError):
            try:
                from .probe_registry import normalized_tool_environment
            except ImportError:
                from core.probes.probe_registry import normalized_tool_environment
        return normalized_tool_environment()
    except ImportError:
        return dict(os.environ)


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+){1,3})", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _command(names: tuple[str, ...], expected: str) -> dict:
    environment = _probe_environment()
    path = None
    for name in names:
        path = shutil.which(name, path=environment.get("PATH"))
        if path:
            break
    if not path:
        return {"name": expected, "ok": False, "path": None, "version": None,
                "detail": "未安装"}
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            check=False, timeout=10, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": expected, "ok": False, "path": path, "version": None,
                "detail": str(exc)}
    output = (result.stdout or result.stderr).splitlines()
    text = output[0].strip() if output else ""
    actual = version_tuple(text)
    minimum = version_tuple(MINIMUM[expected])
    identity_ok = expected != "ast-grep" or "ast-grep" in text.lower()
    ok = result.returncode == 0 and identity_ok and actual >= minimum
    return {"name": expected, "ok": ok, "path": path, "version": text,
            "minimum": MINIMUM[expected], "validated": VALIDATED[expected],
            "detail": text or "无法读取版本"}


def _github_remote(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "remote", "-v"], cwd=root, capture_output=True,
            text=True, check=False, timeout=10, env=_probe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "github.com" in result.stdout.lower()


def inspect(root: Path) -> dict:
    root = root.resolve()
    base = [
        _command(("rg",), "ripgrep"),
        _command(("ast-grep", "sg"), "ast-grep"),
        _command(("jq",), "jq"),
    ]
    conditional = [
        {
            "name": "github-mcp-server", "applicable": _github_remote(root),
            "validated": VALIDATED["github-mcp-server"],
            "recommendation": "GitHub 项目需要 issue/PR/Actions/安全告警上下文时启用只读工具集",
            "security": "使用最小权限 OAuth/PAT；写操作保持 ask",
        },
    ]
    ready = all(item["ok"] for item in base)
    return {
        "status": "READY" if ready else "DEGRADED",
        "required": False,
        "recommended": True,
        "root": str(root),
        "base_tools": base,
        "conditional_tools": conditional,
        "install": {
            "ripgrep": "https://github.com/BurntSushi/ripgrep/releases/tag/15.2.0",
            "ast-grep": f"pipx install --force {OPTIONAL_TOOL_PACKAGE_SPECS['ast-grep']}",
            "jq": "https://github.com/jqlang/jq/releases/tag/jq-1.8.2",
        },
        "fallback": "Python 标准库 + 内置 symbol_index/project_graph + 官方文档搜索",
    }


def print_report(report: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"[{report['status']}] 基础效率工具（推荐，非硬依赖）")
    for item in report["base_tools"]:
        print(f"  {'✓' if item['ok'] else '!'} {item['name']}: {item['detail']}")
    for item in report["conditional_tools"]:
        if item["applicable"]:
            print(f"  ? {item['name']}: {item['recommendation']}")
    if report["status"] != "READY":
        print("  → 缺失项写入准备记录；用户同意后安装，本轮可使用内置回退继续")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "status"), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--allow-degraded", action="store_true",
        help="明确允许使用回退方案；否则 DEGRADED 返回 2，要求调用方先取得用户决策",
    )
    args = parser.parse_args(argv)
    report = inspect(args.root)
    print_report(report, args.json)
    if report["status"] == "READY":
        return 0
    if not args.json and not args.allow_degraded and not args.strict:
        print("  ⏸ 当前仅完成探测，等待用户明确允许回退或配置工具；不会自动进入下一步。")
    if args.strict:
        return 1
    return 0 if args.allow_degraded else 2


if __name__ == "__main__":
    raise SystemExit(main())
