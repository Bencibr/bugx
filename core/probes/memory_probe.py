#!/usr/bin/env python3
"""应用内存审计环境预检（只读，不安装工具、不修改目标或设备）。

用法：
  python3 memory_probe.py check native
  python3 memory_probe.py check python
  python3 memory_probe.py check jvm
  python3 memory_probe.py check electron
  python3 memory_probe.py check android
  python3 memory_probe.py check apple
  python3 memory_probe.py status all --json

状态：
  READY   至少存在该目标的基础采样、插桩或诊断通道
  BLOCKED 缺少必需运行时、设备或诊断通道；不得输出内存专项 PASS

READY 不代表目标已带符号、Sanitizer、可调试签名或可复现工作负载，这些仍须
写入 prep-record.md。
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
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass(frozen=True)
class Report:
    target: str
    status: str
    checks: tuple[Check, ...]
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == "READY"


def _which(name: str) -> str | None:
    return shutil.which(name, path=_probe_environment().get("PATH"))


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
        import os
        return dict(os.environ)


def _to_text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _run(args: list[str], timeout: float = 2.0) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
            env=_probe_environment(),
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode,
            stdout=_to_text(proc.stdout),
            stderr=_to_text(proc.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=args,
            returncode=124,
            stdout=_to_text(exc.stdout),
            stderr=_to_text(exc.stderr) or f"timed out after {timeout}s",
        )
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr=str(exc),
        )


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def host_name(platform: str | None = None) -> str:
    value = platform or sys.platform
    if value == "darwin":
        return "macos"
    if value.startswith("win"):
        return "windows"
    if value.startswith("linux"):
        return "linux"
    return "unsupported"


def _first_command(names: tuple[str, ...]) -> tuple[str | None, str]:
    for name in names:
        path = _which(name)
        if path:
            return path, name
    return None, "/".join(names)


def _finish(target: str, checks: list[Check],
            warnings: list[str] | None = None) -> Report:
    blocked = any(check.required and not check.ok for check in checks)
    return Report(
        target=target,
        status="BLOCKED" if blocked else "READY",
        checks=tuple(checks),
        warnings=tuple(warnings or ()),
    )


from core.runtime.platform_adapter import parse_adb_devices


def native_report(platform: str | None = None) -> Report:
    host = host_name(platform)
    checks = [Check("支持的主机", host != "unsupported", host)]
    if host == "unsupported":
        return _finish("native", checks)

    compiler, compiler_name = _first_command(("clang", "gcc", "cc"))
    profiler_by_host = {
        "macos": ("leaks", "heaptrack", "valgrind"),
        "windows": ("DrMemory", "devenv"),
        "linux": ("valgrind", "heaptrack"),
    }
    profiler, profiler_name = _first_command(profiler_by_host[host])
    checks.extend([
        Check(
            "Sanitizer/动态诊断通道",
            bool(compiler or profiler),
            compiler_name if compiler else profiler_name,
        ),
        Check("独立堆分析器", bool(profiler), profiler_name, required=False),
    ])
    warnings = [
        "编译器存在不代表目标已启用 ASan/LSan/UBSan；黑盒目标还需独立 profiler，"
        "并在准备记录固定符号、优化级别、分配器和启动参数。"
    ]
    return _finish("native", checks, warnings)


def python_report(version: tuple[int, int] | None = None) -> Report:
    current = version or (sys.version_info.major, sys.version_info.minor)
    version_ok = current >= (3, 11)
    tracemalloc_ok = _module_available("tracemalloc")
    memray = _which("memray") or ("Python module" if _module_available("memray") else None)
    checks = [
        Check("Python >= 3.11", version_ok, f"{current[0]}.{current[1]}"),
        Check("tracemalloc", tracemalloc_ok, "标准库" if tracemalloc_ok else "不可用"),
        Check("Memray 原生分配分析", bool(memray), memray or "未安装", required=False),
    ]
    warnings = [
        "tracemalloc 只跟踪 Python 分配；C 扩展、RSS 和原生泄漏需 Memray、"
        "Sanitizer 或系统 profiler 交叉验证。"
    ]
    return _finish("python", checks, warnings)


def jvm_report() -> Report:
    java = _which("java")
    diagnostic, diagnostic_name = _first_command(("jcmd", "jmap", "jfr"))
    checks = [
        Check("JVM", bool(java), java or "java 未安装"),
        Check("堆/运行时诊断", bool(diagnostic), diagnostic_name),
    ]
    warnings = [
        "JVM RSS 不等于 Java heap；需同时记录 heap、metaspace、native memory、GC、"
        "线程和直接缓冲区，并固定 JVM 参数。"
    ]
    return _finish("jvm", checks, warnings)


def electron_report() -> Report:
    node = _which("node")
    package_runner, runner_name = _first_command(("npx", "npm"))
    checks = [
        Check("Node.js", bool(node), node or "node 未安装"),
        Check("Electron/调试启动通道", bool(package_runner), runner_name),
    ]
    warnings = [
        "必须分别采集主进程、renderer、utility/GPU 子进程；单个 renderer heap "
        "snapshot 不能代表应用总内存。"
    ]
    return _finish("electron", checks, warnings)


def android_report() -> Report:
    adb = _which("adb")
    checks = [Check("ADB", bool(adb), adb or "adb 未安装")]
    if not adb:
        return _finish("android", checks)

    result = _run([adb, "devices"])
    devices = parse_adb_devices(result.stdout) if result.returncode == 0 else []
    detail = ", ".join(devices) if devices else (result.stderr.strip() or "没有 device 状态目标")
    checks.extend([
        Check("已连接真机/模拟器", bool(devices), detail),
        Check("dumpsys meminfo/heap dump 通道", bool(devices), "ADB 内置" if devices else "不可用"),
        Check(
            "Perfetto",
            bool(_which("perfetto") or _which("trace_processor_shell")),
            "host tool" if (_which("perfetto") or _which("trace_processor_shell")) else "未发现",
            required=False,
        ),
    ])
    warnings = [
        "模拟器内存结果不得冒充真机；LeakCanary、可调试 heap dump 和基准真机需按项目准备。"
    ]
    return _finish("android", checks, warnings)


def apple_report(platform: str | None = None) -> Report:
    host = host_name(platform)
    checks = [Check("Apple 平台主机", host == "macos", host)]
    if host != "macos":
        return _finish("apple", checks)

    xcrun = _which("xcrun")
    leaks = _which("leaks")
    xctrace_ok = False
    xctrace_detail = "xcrun/xctrace 不可用"
    if xcrun:
        result = _run([xcrun, "--find", "xctrace"])
        xctrace_ok = result.returncode == 0 and bool(result.stdout.strip())
        if xctrace_ok:
            xctrace_detail = result.stdout.strip()
    checks.extend([
        Check("Xcode 命令行工具", bool(xcrun), xcrun or "xcrun 未安装"),
        Check("Instruments/xctrace 或 leaks", bool(xctrace_ok or leaks),
              xctrace_detail if xctrace_ok else (leaks or "未发现")),
    ])
    warnings = [
        "Simulator 不能证明真机内存上限、Jetsam、Extension 或温控行为；"
        "发布构建、dSYM 和代表性真机需单独固定。"
    ]
    return _finish("apple", checks, warnings)


def reports_for(target: str, platform: str | None = None) -> list[Report]:
    factories = {
        "native": lambda: native_report(platform),
        "python": python_report,
        "jvm": jvm_report,
        "electron": electron_report,
        "android": android_report,
        "apple": lambda: apple_report(platform),
    }
    if target == "all":
        return [factory() for factory in factories.values()]
    return [factories[target]()]


def print_reports(reports: list[Report], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps([asdict(report) for report in reports], ensure_ascii=False, indent=2))
        return
    for report in reports:
        print(f"[{report.status}] {report.target}")
        for check in report.checks:
            mark = "✓" if check.ok else ("✗" if check.required else "!")
            optional = "（可选）" if not check.required else ""
            print(f"  {mark} {check.name}{optional}: {check.detail}")
        for warning in report.warnings:
            print(f"  ! {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "status"), nargs="?", default="check")
    parser.add_argument(
        "target",
        choices=("native", "python", "jvm", "electron", "android", "apple", "all"),
        nargs="?",
        default="all",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    reports = reports_for(args.target)
    print_reports(reports, args.json)
    if args.target == "all":
        return 0 if any(report.ready for report in reports) else 1
    return 0 if all(report.ready for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
