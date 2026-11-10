#!/usr/bin/env python3
"""原生/嵌入式项目预检（项目级、只读；不构建、不修改工程）。

与 ``platform_probe`` 互补：后者查**主机**能力（截图/自动化通道），本探针查
**项目**的原生构建与插桩能力，供 ``native-audit`` 技能包判定白盒证据是否可得。

用法：
  python3 native_probe.py check --root .
  python3 native_probe.py status --root . --json

状态：
  READY          识别到原生工程且工具链/插桩能力可支撑审计
  NOT_APPLICABLE 项目里没有原生源文件/构建标记：能力**不适用于本项目**
  BLOCKED        有原生工程但无法支撑（如缺编译器）；**禁止**把未执行写成 PASS

NOT_APPLICABLE 与 BLOCKED 必须严格区分（DOC-236 C1）：前者是项目形态决定的
"不适用"，不得进入启动阻断通道；后者是环境能力缺口，必须阻断或自愈。
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
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

NATIVE_SUFFIXES = (
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".rs", ".go", ".m", ".mm",
)
BUILD_MARKERS = (
    "CMakeLists.txt", "Makefile", "makefile", "meson.build", "Cargo.toml",
    "BUILD", "BUILD.bazel", "configure", "SConstruct", "build.gradle",
)
TOOLCHAIN = ("cc", "gcc", "clang", "cmake", "make", "meson", "cargo", "go", "ninja")
SANITIZERS = ("address", "undefined", "thread")
SKIP_DIRS = {".git", "node_modules", "target", "build", ".venv"}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass(frozen=True)
class Report:
    platform: str
    status: str
    checks: tuple[Check, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == "READY"


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


def _which(name: str) -> str | None:
    return shutil.which(name, path=_probe_environment().get("PATH"))


def _scan_sources(root: Path, limit: int = 4000) -> tuple[int, list[str]]:
    """统计原生源文件数量与命中的构建标记（只读、限深限数）。"""
    sources = 0
    markers: list[str] = []
    for _directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in filenames:
            if filename in BUILD_MARKERS and filename not in markers:
                markers.append(filename)
            if filename.endswith(NATIVE_SUFFIXES):
                sources += 1
                if sources >= limit:
                    return sources, markers
    return sources, markers


def _sanitizer_support(compiler: str) -> dict[str, bool]:
    """在临时目录用**真实编译**验证 sanitizer 可用性（不触碰项目目录）。"""
    support: dict[str, bool] = {}
    source = "#include <stdlib.h>\nint main(void){return 0;}\n"
    with tempfile.TemporaryDirectory(prefix="bugx-native-probe-") as tmp:
        path = Path(tmp) / "probe.c"
        path.write_text(source, encoding="utf-8")
        for sanitizer in SANITIZERS:
            command = [
                compiler, "-fsanitize=" + sanitizer,
                "-o", str(Path(tmp) / f"probe-{sanitizer}"), str(path),
            ]
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, timeout=20,
                    check=False, env=_probe_environment(),
                )
            except (OSError, subprocess.TimeoutExpired):
                support[sanitizer] = False
                continue
            support[sanitizer] = completed.returncode == 0
    return support


def native_report(root: Path | str | None = None) -> Report:
    """项目级原生能力预检（只读）。"""
    root = Path(root or Path.cwd())
    checks: list[Check] = []
    warnings: list[str] = []
    if not root.is_dir():
        return Report("native", "BLOCKED", (Check("项目目录", False, str(root)),))

    sources, markers = _scan_sources(root)
    checks.append(Check(
        "原生工程识别",
        sources > 0 or bool(markers),
        f"原生源文件 {sources} 个；构建标记 {', '.join(markers) or '无'}",
    ))
    if sources == 0 and not markers:
        # 项目形态结论：本项目没有原生代码，原生审计能力**不适用**。
        # 这不是环境缺口，绝不能进入启动阻断通道（BLOCKED 只留给缺编译器一类）。
        return Report("native", "NOT_APPLICABLE", tuple(checks), ())

    toolchain = {name: _which(name) for name in TOOLCHAIN}
    compiler = toolchain.get("clang") or toolchain.get("cc") or toolchain.get("gcc")
    checks.append(Check(
        "编译器可用", bool(compiler),
        str(compiler) if compiler else "未找到 clang/cc/gcc",
    ))
    checks.append(Check(
        "构建工具可用",
        any(toolchain.get(name) for name in ("cmake", "make", "meson", "cargo", "ninja")),
        ", ".join(name for name in TOOLCHAIN if toolchain.get(name)) or "无",
        required=False,
    ))
    if compiler:
        support = _sanitizer_support(compiler)
        available = [name for name, ok in support.items() if ok]
        checks.append(Check(
            "运行时插桩能力",
            bool(available),
            "可用 sanitizer: " + (", ".join(available) or "无（内存/并发证据只能静态推断）"),
            required=False,
        ))
        if not support.get("address"):
            warnings.append(
                "无 AddressSanitizer 支持：内存类结论必须标注仅静态推断、未运行验证"
            )
    else:
        warnings.append("无编译器：构建/插桩/行为证据均不可得，禁止声称已覆盖")
    machine = os.uname().machine if hasattr(os, "uname") else "unknown"
    checks.append(Check(
        "目标架构", True, f"{machine}（交叉编译/ABI 需在阶段内显式确认）",
        required=False,
    ))
    blocked = any(check.required and not check.ok for check in checks)
    return Report(
        "native", "BLOCKED" if blocked else "READY", tuple(checks), tuple(warnings),
    )


def print_report(report: Report, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return
    print(f"[native-probe] status={report.status}")
    for check in report.checks:
        mark = "✓" if check.ok else ("✗" if check.required else "!")
        print(f"  {mark} {check.name}: {check.detail}")
    for warning in report.warnings:
        print(f"  ⚠ {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "status"), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = native_report(args.root)
    print_report(report, as_json=args.json)
    # “不适用”不是失败：退出码 0 表示探针结论有效，避免上游把它误判为执行失败。
    return 0 if report.status in {"READY", "NOT_APPLICABLE"} else 1


if __name__ == "__main__":
    sys.exit(main())
