#!/usr/bin/env python3
"""移动端 UI 自动化驱动探针（DOC-015）：三通道递进探测，READY/DEGRADED 退出语义。

通道分 Tier（AD-01）：
  Tier 1 — 原生 ADB（uiautomator dump / input tap|swipe）+ xcrun simctl：零依赖基座。
  Tier 2 — maestro CLI：YAML 声明式 E2E。
  Tier 3 — appium Server：WebDriver REST 透传（/source、/element/{id}/click）。
无任何通道时 DEGRADED，不伪造界面证据（AD-02 Fail-closed）。
交互动作仅在明确审计场景触发，探测本身只读（AD-03）。
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
import shutil
import subprocess
from pathlib import Path

VALIDATED = {
    "adb": "35.0.2",
    "xcrun": "16.0.0",
    "maestro": "1.40.0",
    "appium": "2.12.0",
}
MINIMUM = {
    "adb": "30.0.0",
    "xcrun": "15.0.0",
    "maestro": "1.35.0",
    "appium": "2.5.0",
}


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


def _run(args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=timeout,
            env=_probe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(exc))


def _version(cmd: list[str]) -> str:
    result = _run([*cmd, "--version"])
    if not result or result.returncode != 0:
        result = _run([*cmd, "-version"])
    if not result:
        return ""
    output = (result.stdout or result.stderr or "").strip().splitlines()
    return output[0] if output else ""


def _check_tool(name: str, version_args: list[str]) -> dict:
    path = _which(name)
    if not path:
        return {"name": name, "ok": False, "path": None, "version": None, "detail": "未安装"}
    version = _version([path, *version_args])
    return {"name": name, "ok": bool(version), "path": path, "version": version or None,
            "detail": version or "无法读取版本"}


from core.runtime.platform_adapter import parse_adb_devices, parse_booted_simulators


def _android_tier1(adb: str) -> dict:
    result = _run([adb, "devices"])
    devices = parse_adb_devices(result.stdout) if result.returncode == 0 else []
    return {
        "tier": 1,
        "kind": "adb",
        "ok": bool(devices),
        "devices": devices,
        "actions": ["dump", "tap", "swipe", "text", "screenshot"],
        "detail": f"已连接 {len(devices)} 台设备: {', '.join(devices)}" if devices else "无可用 Android 设备",
    }


def _ios_tier1(xcrun: str) -> dict:
    result = _run([xcrun, "simctl", "list", "devices", "booted", "-j"])
    targets = parse_booted_simulators(result.stdout) if result.returncode == 0 else []
    return {
        "tier": 1,
        "kind": "simctl",
        "ok": bool(targets),
        "devices": targets,
        "actions": ["screenshot", "launch", "terminate"],
        "detail": f"已启动 {len(targets)} 台 Simulator: {', '.join(targets[:3])}" if targets else "无可用 iOS Simulator",
    }


def _is_mobile_project(root: Path) -> bool:
    """检测当前目标项目是否为移动端应用形态（Android/iOS/Flutter/RN）。"""
    try:
        markers = [
            "AndroidManifest.xml", "build.gradle", "build.gradle.kts",
            "Info.plist", "Podfile", "pubspec.yaml",
        ]
        for m in markers:
            if (root / m).is_file():
                return True
        for m in ("*.xcodeproj", "*.xcworkspace"):
            if any(root.glob(m)):
                return True
    except OSError:
        pass
    return False


def inspect(root: Path) -> dict:
    root = root.resolve()
    channels: list[dict] = []

    # Tier 1: ADB / simctl
    adb_path = _which("adb")
    if adb_path:
        adb_info = _check_tool("adb", ["version"])
        adb_t1 = _android_tier1(adb_path)
        adb_ok = adb_info["ok"] and adb_t1["ok"]
    else:
        adb_info = {"name": "adb", "ok": False, "path": None, "version": None, "detail": "adb 未安装"}
        adb_t1 = {"tier": 1, "kind": "adb", "ok": False, "devices": [], "detail": "adb 未安装"}
        adb_ok = False
    channels.append({"name": "Android ADB (Tier 1)", "platform": "android", "tier": 1, "ok": adb_ok, **adb_t1})

    xcrun_path = _which("xcrun")
    ios_t1 = _ios_tier1(xcrun_path) if xcrun_path else {"tier": 1, "kind": "simctl", "ok": False, "devices": [], "detail": "xcrun 未安装"}
    channels.append({"name": "iOS Simctl (Tier 1)", "platform": "ios", "tier": 1, **ios_t1})

    # Tier 2: Maestro
    maestro_info = _check_tool("maestro", ["--version"])
    channels.append({
        "name": "Maestro CLI (Tier 2)",
        "platform": "cross",
        "tier": 2,
        "kind": "maestro",
        "ok": maestro_info["ok"],
        "actions": ["yaml-e2e", "tap", "swipe", "text"],
        "detail": maestro_info["detail"],
    })

    # Tier 3: Appium REST
    appium_info = _check_tool("appium", ["--version"])
    channels.append({
        "name": "Appium REST (Tier 3)",
        "platform": "cross",
        "tier": 3,
        "kind": "appium",
        "ok": appium_info["ok"],
        "actions": ["rest-source", "rest-click"],
        "detail": appium_info["detail"],
    })

    # READY = at least one channel has an active device or execution capability
    active = [c for c in channels if c["ok"]]
    ready = len(active) > 0
    best_tier = min((c["tier"] for c in active), default=None)

    is_mobile = _is_mobile_project(root)
    # 若被测项目为移动端项目，黑盒通道必须可用（required=True）；非移动端项目可作为建议性探针
    required = bool(is_mobile)

    install_map = {
        "adb": "brew install android-platform-tools",
        "xcrun": "xcode-select --install",
        "maestro": "curl -FsSL https://get.maestro.mobile.dev | bash",
        "appium": "npm install -g appium",
    }

    checks = []
    for c in channels:
        checks.append({
            "name": c["name"],
            "ok": c["ok"],
            "detail": c["detail"],
            "kind": c["kind"],
            "tier": c["tier"],
            "required": required,
            "critical": required,
            "install": install_map.get(c["kind"], "见项目文档"),
        })

    degraded_actions = []
    if not ready:
        for c in channels:
            if not c["ok"]:
                cmd = install_map.get(c["kind"], "见项目文档")
                degraded_actions.append({
                    "name": f"mobile/{c['name']}",
                    "detail": f"{c['detail']}（移动端黑盒自动化驱动通道缺失）",
                    "options": [
                        f"安装/启动：{cmd}",
                        "明确豁免：使用 --allow-degraded 或配置 tooling_exemptions 放行降级",
                    ],
                    "required": required,
                    "critical": required,
                    "advisory": not required,
                })

    return {
        "status": "READY" if ready else "DEGRADED",
        "required": required,
        "recommended": True,
        "root": str(root),
        "best_tier": best_tier,
        "active_channels": [c["name"] for c in active],
        "channels": channels,
        "checks": checks,
        "degraded_actions": degraded_actions,
        "install": install_map,
        "fallback": "仅静态截图 (android.json / ios.json) + 手动界面复现",
        "workflow": "mobile_probe 探测通道 → mobile_ui_inspector 提取 UI 树并执行 5 大几何 Oracle → 生成 Failure Pack 自洽证据",
    }


def print_report(report: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"[{report['status']}] 移动端 UI 驱动通道（最佳通道: Tier {report.get('best_tier') or 'N/A'}）")
    for channel in report["channels"]:
        print(f"  {'✓' if channel['ok'] else '!'} {channel['name']}: {channel['detail']}")
    if report["status"] != "READY":
        print(f"  → 回退：{report['fallback']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "status"), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--allow-degraded", action="store_true",
        help="明确允许使用回退方案；否则 DEGRADED 返回 2",
    )
    args = parser.parse_args(argv)
    report = inspect(args.root)
    print_report(report, args.json)
    if report["status"] == "READY":
        return 0
    if not args.json and not args.allow_degraded and not args.strict:
        print("  ⏸ 当前仅完成探测，等待用户明确允许回退；不会自动进入下一步。")
    if args.allow_degraded:
        return 0
    if args.strict:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
