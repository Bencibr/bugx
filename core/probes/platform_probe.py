#!/usr/bin/env python3
"""桌面端与移动端测试环境预检（只读，不自动安装或修改设备）。

用法：
  python3 platform_probe.py check desktop
  python3 platform_probe.py check android
  python3 platform_probe.py check ios
  python3 platform_probe.py status all --json

状态：
  READY   当前主机上存在可用目标和基础观测/驱动能力
  BLOCKED 缺少主机、工具或可运行目标；禁止把未执行写成 PASS
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
            env=(_probe_environment()),
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


def _finish(platform: str, checks: list[Check], warnings: list[str] | None = None) -> Report:
    blocked = any(check.required and not check.ok for check in checks)
    return Report(
        platform=platform,
        status="BLOCKED" if blocked else "READY",
        checks=tuple(checks),
        warnings=tuple(warnings or ()),
    )


from core.runtime.platform_adapter import parse_adb_devices, parse_booted_simulators


def parse_xctrace_devices(output: str) -> list[str]:
    devices: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("==") or "Unavailable" in line:
            continue
        if "(" in line and ")" in line:
            devices.append(line)
    return devices


def desktop_report(platform: str | None = None) -> Report:
    host = host_name(platform)
    checks = [Check("支持的桌面主机", host != "unsupported", host)]
    if host == "unsupported":
        return _finish("desktop", checks)

    capture_by_host = {
        "macos": ("screencapture", "appium"),
        "windows": ("powershell", "WinAppDriver", "appium"),
        "linux": ("gnome-screenshot", "scrot", "import", "appium"),
    }
    automation_by_host = {
        "macos": ("appium", "xcodebuild", "osascript"),
        "windows": ("WinAppDriver", "appium", "dotnet"),
        "linux": ("dogtail", "dogtail-run-headless-next", "xdotool", "appium"),
    }
    capture, capture_name = _first_command(capture_by_host[host])
    automation, automation_name = _first_command(automation_by_host[host])
    checks.extend([
        Check("截图/视觉证据通道", bool(capture), capture_name),
        Check("桌面自动化通道", bool(automation), automation_name),
    ])
    warnings = [
        "Electron/Tauri/Qt/Flutter Desktop 等框架仍需在 prep-record.md 固定具体驱动、应用版本和窗口矩阵。"
    ]
    return _finish("desktop", checks, warnings)


def android_report(root: Path | None = None) -> Report:
    checks: list[Check] = []
    adb = _which("adb")
    checks.append(Check("Android SDK/ADB", bool(adb), adb or "adb 未安装"))
    if not adb:
        return _finish("android", checks)

    result = _run([adb, "devices"])
    devices = parse_adb_devices(result.stdout) if result.returncode == 0 else []
    detail = ", ".join(devices) if devices else (result.stderr.strip() or "没有处于 device 状态的目标")
    checks.append(Check("已连接真机/模拟器", bool(devices), detail))

    root = root or Path.cwd()
    routes: list[str] = []
    if (root / "gradlew").is_file():
        routes.append("项目 Gradle/Espresso/Compose/UI Automator")
    for command, label in (("appium", "Appium"), ("maestro", "Maestro"), ("gradle", "Gradle")):
        if _which(command):
            routes.append(label)
    warnings = [] if routes else [
        "当前只有 ADB 探索/截图能力；可执行重复 E2E 前需准备项目原生测试、Appium 或 Maestro。"
    ]
    checks.append(Check("可重复自动化通道", bool(routes), ", ".join(routes) or "未发现", required=False))
    return _finish("android", checks, warnings)


def ios_report(platform: str | None = None) -> Report:
    host = host_name(platform)
    checks = [Check("iOS 主机", host == "macos", host)]
    if host != "macos":
        return _finish("ios", checks)

    xcrun = _which("xcrun")
    xcodebuild = _which("xcodebuild")
    checks.extend([
        Check("Xcode 命令行工具", bool(xcrun), xcrun or "xcrun 未安装"),
        Check("XCTest 构建运行器", bool(xcodebuild), xcodebuild or "xcodebuild 未安装"),
    ])
    if not xcrun:
        return _finish("ios", checks)

    simctl = _run([xcrun, "simctl", "list", "devices", "booted", "-j"])
    targets = parse_booted_simulators(simctl.stdout) if simctl.returncode == 0 else []
    if not targets:
        xctrace = _run([xcrun, "xctrace", "list", "devices"])
        if xctrace.returncode == 0:
            targets = parse_xctrace_devices(xctrace.stdout)
    checks.append(Check(
        "可用 Simulator/真机",
        bool(targets),
        "; ".join(targets[:5]) if targets else "没有可用或已启动的测试目标",
    ))
    routes = [name for name in ("appium", "maestro") if _which(name)]
    checks.append(Check(
        "跨平台黑盒通道",
        bool(routes),
        ", ".join(routes) or "未发现（XCTest 仍可用）",
        required=False,
    ))
    warnings = [
        "Simulator 结果不得冒充真机；相机、生物识别、推送、性能、温控和签名敏感行为需要代表性真机。"
    ]
    return _finish("ios", checks, warnings)


def reports_for(target: str, root: Path | None = None,
                platform: str | None = None) -> list[Report]:
    if target == "desktop":
        return [desktop_report(platform)]
    if target == "android":
        return [android_report(root)]
    if target == "ios":
        return [ios_report(platform)]
    return [desktop_report(platform), android_report(root), ios_report(platform)]


def summary_dict(reports: list[Report], *, root: Path | None = None) -> dict[str, Any]:
    """构建单根标准环境探测报告字典。"""
    eval_reports = reports
    if root is not None:
        try:
            from core.runtime import project_profile
            profile = project_profile.detect_project_profile(root)
            forms = set(profile.get("forms", []))
            if "mobile" not in forms:
                eval_reports = [r for r in reports if r.platform not in {"android", "ios", "apple"}]
        except Exception:
            pass
    statuses = [r.status for r in eval_reports]
    if any(s == "BLOCKED" for s in statuses):
        overall_status = "BLOCKED"
    elif any(s == "DEGRADED" for s in statuses):
        overall_status = "DEGRADED"
    else:
        overall_status = "READY"
    all_checks = []
    all_warnings = []
    for r in reports:
        for c in r.checks:
            all_checks.append(asdict(c))
        all_warnings.extend(r.warnings)
    return {
        "status": overall_status,
        "platforms": [r.platform for r in reports],
        "checks": all_checks,
        "warnings": all_warnings,
        "reports": [asdict(r) for r in reports],
    }


def print_reports(
    reports: list[Report],
    as_json: bool = False,
    envelope: bool = True,
    *,
    root: Path | None = None,
) -> None:
    if as_json:
        if envelope:
            print(json.dumps(summary_dict(reports, root=root), ensure_ascii=False, indent=2))
        else:
            print(json.dumps([asdict(report) for report in reports], ensure_ascii=False, indent=2))
        return
    for report in reports:
        print(f"[{report.status}] {report.platform}")
        for check in report.checks:
            mark = "✓" if check.ok else ("✗" if check.required else "!")
            optional = "（可选）" if not check.required else ""
            print(f"  {mark} {check.name}{optional}: {check.detail}")
        for warning in report.warnings:
            print(f"  ! {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "status"), nargs="?", default="check")
    parser.add_argument("target", choices=("desktop", "android", "ios", "all"), nargs="?", default="all")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--envelope", action="store_true", default=True)
    parser.add_argument("--no-envelope", dest="envelope", action="store_false")
    args = parser.parse_args(argv)
    reports = reports_for(args.target, args.root)
    print_reports(reports, as_json=args.json, envelope=args.envelope, root=args.root)
    if all(report.ready for report in reports):
        return 0
    # DOC-226: 当 target 为 all 且基础桌面能力就绪时，若项目非移动端，返回 0 避免假性阻断
    if args.target == "all" and any(r.platform == "desktop" and r.ready for r in reports):
        try:
            from core.runtime import project_profile
            profile = project_profile.detect_project_profile(args.root)
            forms = set(profile.get("forms", []))
            if "mobile" not in forms:
                return 0
        except Exception:
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
