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


def _version(cmd: list[str], *, timeout: float = 10.0) -> str:
    """读取工具版本首行；``timeout`` 用于 JVM 类工具（maestro ~7.5s 冷启动）。

    超时早退：``--version`` 超时说明是进程启动级问题，换 ``-version`` 变体
    只会把同一冷启动再烧一遍预算（实测 2s+2s 双倍溢出）——超时后直接返回
    空串，由调用方按「存在性已验证、版本未取到」处置。
    """
    result = _run([*cmd, "--version"], timeout=timeout)
    timed_out = bool(result and "timed out" in (result.stderr or ""))
    if result and result.returncode != 0 and not timed_out:
        result = _run([*cmd, "-version"], timeout=timeout)
    if not result or timed_out:
        return ""
    output = (result.stdout or result.stderr or "").strip().splitlines()
    return output[0] if output else ""


def _check_tool(
    name: str, version_args: list[str], *, version_timeout: float = 10.0,
) -> dict:
    """探测工具存在性与版本。

    通道判据是**存在性**（which 命中），版本探测只是详情增强：JVM 类工具
    （maestro）``--version`` 冷启动实测 ~7.5s，在 pre 的 5s 探针预算下会把
    整条 mobile 探针拖成 BLOCKED 并触发安装决策阻断（F-RSI-20261118-004
    实证）。版本探测超时不再把工具判成不可用——存在性已验证，detail 注明
    版本读取超时。
    """
    path = _which(name)
    if not path:
        return {"name": name, "ok": False, "path": None, "version": None, "detail": "未安装"}
    version = _version([path, *version_args], timeout=version_timeout)
    if version:
        return {"name": name, "ok": True, "path": path, "version": version,
                "detail": version}
    return {"name": name, "ok": True, "path": path, "version": None,
            "detail": f"已安装（{path}；版本读取超时/失败，存在性已验证）"}


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


#: 移动形态标记（与 core/project_profiler detector 同口径的文件名集合，
#: casefold 比较；profiler 判据为扫描树内任意层级的同名文件）。
_MOBILE_MARKER_FILES = frozenset({
    "androidmanifest.xml", "build.gradle", "build.gradle.kts",
    "gradlew", "info.plist", "podfile", "pubspec.yaml", "project.pbxproj",
})
#: 目录形态标记（Xcode 工程 / 工作区 / IPA 产物）。
_MOBILE_MARKER_SUFFIXES = (".xcodeproj", ".xcworkspace", ".ipa")
#: 有界探测硬上限：AndroidManifest.xml 位于 app/src/main/ 时需 4 层，
#: 深度 5 覆盖典型 Gradle/Xcode 布局且绝不退化为全盘扫描。
_MOBILE_SCAN_MAX_DEPTH = 5
_MOBILE_SCAN_MAX_DIRS = 128
#: 探测时跳过的隐藏目录与依赖/构建产物目录（内部含海量 Info.plist 等噪声，
#: 跳过既保性能上界，也避免 vendor 树误判为移动项目）。
_MOBILE_SCAN_SKIP_DIRS = frozenset({
    ".git", ".bugx", ".gradle", ".idea", "__pycache__", "node_modules",
    "build", "dist", "target", "vendor", "Pods", "DerivedData",
})


def _is_mobile_project(root: Path) -> bool:
    """检测当前目标项目是否为移动端应用形态（Android/iOS/Flutter/RN）。

    与 core/project_profiler detector 同口径：根级标记或一级/多级子目录
    递归探测（覆盖 android-sample/ + ios-sample/ 子目录形态仓库）。
    性能有界：最大深度 ``_MOBILE_SCAN_MAX_DEPTH``、目录数上限
    ``_MOBILE_SCAN_MAX_DIRS``；跳过隐藏目录与依赖/构建产物目录，不跟随
    symlink，禁止全盘扫描。
    """
    try:
        if not root.is_dir():
            return False
        scanned_dirs = 0
        pending: list[tuple[Path, int]] = [(root, 0)]
        while pending:
            directory, depth = pending.pop()
            scanned_dirs += 1
            if scanned_dirs > _MOBILE_SCAN_MAX_DIRS:
                return False
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            subdirs: list[Path] = []
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        name_lower = entry.name.casefold()
                        if entry.name.startswith(".") or name_lower in _MOBILE_SCAN_SKIP_DIRS:
                            continue
                        if name_lower.endswith(_MOBILE_MARKER_SUFFIXES):
                            return True
                        subdirs.append(entry)
                    elif entry.is_file():
                        name_lower = entry.name.casefold()
                        if name_lower in _MOBILE_MARKER_FILES or name_lower.endswith(_MOBILE_MARKER_SUFFIXES):
                            return True
                except OSError:
                    continue
            if depth < _MOBILE_SCAN_MAX_DEPTH:
                pending.extend((sub, depth + 1) for sub in subdirs)
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

    # Tier 2/3 并行探测（F-RSI-20261118-004）：maestro 是 JVM 工具，冷启动
    # ``--version`` 实测 ~7.5s；串行探测必然超 pre 探针预算。两者并行 + 版本
    # 探测 2s 硬顶（存在性已由 which 验证，超时只丢版本详情不丢通道）。
    import concurrent.futures as _futures

    with _futures.ThreadPoolExecutor(max_workers=2) as pool:
        maestro_f = pool.submit(_check_tool, "maestro", ["--version"], version_timeout=2.0)
        appium_f = pool.submit(_check_tool, "appium", ["--version"], version_timeout=2.0)
        maestro_info = maestro_f.result()
        appium_info = appium_f.result()
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

    # F-RSI-20261118-005：critical 按「Tier1 是否全灭」判定，而非项目级
    # required 一刀切。双形态仓库（android+ios 样本）中，任一 Tier1 平台通道
    # 就绪即已覆盖黑盒动态面，其余平台通道未就绪（如 macOS 无 iOS runtime 时
    # iOS Simctl 永不可就绪）降为 advisory——静态探针（sast/android_probe/
    # ios_binary）对未就绪平台仍可完整行使。全灭才 critical（此时确无任何
    # 动态验证能力）。
    tier1_ready = any(c["tier"] == 1 and c["ok"] for c in channels)
    checks = []
    for c in channels:
        channel_critical = bool(required and not tier1_ready)
        checks.append({
            "name": c["name"],
            "ok": c["ok"],
            "detail": c["detail"],
            "kind": c["kind"],
            "tier": c["tier"],
            "required": required,
            "critical": channel_critical,
            "advisory": bool(required and tier1_ready and not c["ok"]),
            "install": install_map.get(c["kind"], "见项目文档"),
        })

    # 降级项在 ready 时也显式生成（标 advisory），避免 extract_degraded_actions
    # 回落到 checks 聚合把 advisory 通道重新当成 critical 缺口。
    degraded_actions = []
    if True:
        for c in channels:
            if not c["ok"]:
                cmd = install_map.get(c["kind"], "见项目文档")
                channel_critical = bool(required and not tier1_ready)
                if channel_critical:
                    detail_text = f"{c['detail']}（移动端黑盒自动化驱动通道缺失，全部 Tier1 均未就绪）"
                else:
                    detail_text = f"{c['detail']}（该平台动态通道不可用；已有 Tier1 通道就绪，静态审计仍可行使）"
                degraded_actions.append({
                    "name": f"mobile/{c['name']}",
                    "detail": detail_text,
                    "options": [
                        f"安装/启动：{cmd}",
                        "明确豁免：使用 --allow-degraded 或配置 tooling_exemptions 放行降级",
                    ],
                    "required": required,
                    "critical": channel_critical,
                    "advisory": bool(required and not channel_critical),
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


def is_available() -> tuple[bool, str]:
    """降级契约：机械探测移动端 UI 自动化通道前置条件（<5ms，零阻塞、零子进程）。

    仅做 PATH 工具存在性检查（adb/xcrun/maestro/appium 任一在 PATH 即具备
    至少一条动态通道）；不查询设备（设备查询属 inspect() 的慢路径）。
    返回 (available, detail)，detail 列出在场与缺失通道。
    """
    channel_tools = ("adb", "xcrun", "maestro", "appium")
    present = [name for name in channel_tools if _which(name)]
    missing = [name for name in channel_tools if name not in present]
    detail = (
        f"可用通道工具: {', '.join(present)}"
        if present
        else "adb/xcrun/maestro/appium 均未安装：移动端黑盒动态通道全部不可用"
    )
    if missing:
        detail += f"；缺失: {', '.join(missing)}"
    return bool(present), detail


def run_static_degraded(root: Path | str | None = None) -> dict:
    """降级路径：无任何动态通道时，仅做项目形态静态分析。

    降级契约：输出与成功路径（inspect()，status=READY）**可区分**——
    status="degraded" + degraded=True + degraded_reason，绝不伪造界面/设备证据；
    设备可用性、UI 树等动态结论一律标注未执行。
    """
    root_path = Path(root or Path.cwd()).resolve()
    notes: list[str] = []
    if not root_path.is_dir():
        return {
            "probe": "mobile",
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "target_root_not_found",
            "success": False,
            "root": str(root_path),
            "findings": [],
            "evidence": {},
            "notes": [f"目标目录不存在: {root_path}"],
            "checks_not_run": ["adb-devices", "simctl-booted", "ui-automation"],
            "install": {"adb": "brew install android-platform-tools", "xcrun": "xcode-select --install",
                        "maestro": "curl -FsSL https://get.maestro.mobile.dev | bash",
                        "appium": "npm install -g appium"},
        }
    is_mobile = _is_mobile_project(root_path)
    available, availability_detail = is_available()
    notes.append("降级模式：仅完成项目形态静态识别；设备/界面动态检查全部未执行，"
                 "禁止据此声称已完成移动端黑盒审计")
    if not available:
        notes.append(f"动态通道缺失：{availability_detail}")
    return {
        "probe": "mobile",
        "status": "degraded",
        "degraded": True,
        "degraded_reason": (
            "no_dynamic_channel_available" if not available else "static_mode_forced"
        ),
        "success": True,
        "root": str(root_path),
        "findings": [],
        "evidence": {
            "is_mobile_project": is_mobile,
            "dynamic_channels_available": available,
            "availability_detail": availability_detail,
            "mobile_markers_hint": (
                "AndroidManifest.xml/build.gradle/gradlew/Info.plist/Podfile/"
                "pubspec.yaml/project.pbxproj/*.xcodeproj（根级或子目录递归有界探测）"
                if is_mobile else "未命中任何移动端项目标记"
            ),
        },
        "notes": notes,
        "checks_not_run": ["adb-devices", "simctl-booted", "ui-automation"],
        "install": {"adb": "brew install android-platform-tools", "xcrun": "xcode-select --install",
                    "maestro": "curl -FsSL https://get.maestro.mobile.dev | bash",
                    "appium": "npm install -g appium"},
    }


def print_report(report: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"[{report['status']}] 移动端 UI 驱动通道（最佳通道: Tier {report.get('best_tier') or 'N/A'}）")
    for channel in report.get("channels", []):
        print(f"  {'✓' if channel['ok'] else '!'} {channel['name']}: {channel['detail']}")
    for note in report.get("notes", []):
        print(f"  · {note}")
    if report["status"] != "READY":
        print(f"  → 回退：{report.get('fallback', '见 install 建议或 notes')}")
        if report.get("degraded_reason"):
            print(f"  → 降级原因：{report['degraded_reason']}")


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
    parser.add_argument(
        "--static-degraded", action="store_true",
        help="显式执行降级路径（仅项目形态静态识别；输出 status=degraded，"
             "绝不伪造设备/界面证据）",
    )
    args = parser.parse_args(argv)
    if args.static_degraded:
        report = run_static_degraded(args.root)
        print_report(report, args.json)
        return 0
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
