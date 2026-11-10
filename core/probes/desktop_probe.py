#!/usr/bin/env python3
"""桌面应用项目预检（项目级、只读；不安装、不运行、不修改工程）。

与 ``platform_probe`` 互补：后者查**主机**能力（截图/自动化通道），本探针查
**项目**的进程模型、打包完整性与更新通道，供 ``desktop-audit`` 技能包取证。

用法：
  python3 desktop_probe.py check --root .
  python3 desktop_probe.py status --root . --json

状态：
  READY          识别到桌面工程（可继续按阶段取证）
  NOT_APPLICABLE 项目没有 Electron/Tauri/Qt/WPF 等桌面框架：能力**不适用于本项目**
  BLOCKED        磁盘上确有桌面工程但取证失败/工程不可读；**禁止**把未执行写成 PASS

NOT_APPLICABLE 与 BLOCKED 必须严格区分（DOC-236 C1）：前者是项目形态决定的
"不适用"，不得进入启动阻断通道；后者是环境或取证缺口，必须如实阻断。
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
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "target", "dist", "out", ".venv"}
ELECTRON_MARKERS = ("electron", "electron-builder", "electron-forge", "@electron/")
TAURI_MARKERS = ("@tauri-apps/cli", "@tauri-apps/api")
QT_MARKERS = ("qt", "pyside", "pyqt")
WPF_MARKERS = ("UseWPF", "PresentationFramework")
# Electron 主进程安全开关（默认值不安全的那几项）
ISOLATION_KEYS = ("contextIsolation", "nodeIntegration", "sandbox", "webSecurity")
SIGNING_HINTS = ("identity", "certificateFile", "signingHashAlgorithms", "notarize")


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


def _iter_files(root: Path, limit: int = 4000) -> list[Path]:
    files: list[Path] = []
    for _directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in filenames:
            files.append(Path(_directory) / filename)
            if len(files) >= limit:
                return files
    return files


def _read(path: Path, limit: int = 400_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _detect_framework(files: list[Path]) -> tuple[str, str]:
    names = {path.name for path in files}
    package_json = next((p for p in files if p.name == "package.json"), None)
    blob = _read(package_json).lower() if package_json else ""
    if any(marker in blob for marker in TAURI_MARKERS) or "src-tauri" in {
        p.name for p in files
    }:
        return "tauri", "package.json/src-tauri"
    if any(marker in blob for marker in ELECTRON_MARKERS):
        return "electron", "package.json"
    if any(marker in blob for marker in QT_MARKERS):
        return "qt", "package.json"
    if any(p.suffix == ".pro" for p in files) or "CMakeLists.txt" in names:
        for path in files:
            if path.name == "CMakeLists.txt" and "qt" in _read(path, 20_000).lower():
                return "qt", "CMakeLists.txt"
    for path in files:
        if path.suffix == ".csproj":
            blob = _read(path, 40_000)
            if any(marker in blob for marker in WPF_MARKERS):
                return "wpf", path.name
    return "", ""


def _electron_findings(files: list[Path]) -> tuple[list[str], list[str]]:
    """扫描 Electron 主进程配置与打包设置，返回 (明细, 告警)。"""
    details: list[str] = []
    warnings: list[str] = []
    main_sources = [
        path for path in files
        if path.suffix in (".js", ".ts", ".mjs", ".cjs")
        and any(token in _read(path, 200_000) for token in ("BrowserWindow", "app.whenReady"))
    ]
    blob = "\n".join(_read(path) for path in main_sources)
    found = {key: re.search(rf"{key}\s*:\s*([^,\n}}]+)", blob) for key in ISOLATION_KEYS}
    enabled = [key for key, match in found.items() if match]
    details.append("主进程安全开关显式出现: " + (", ".join(enabled) or "未显式出现"))
    if not found["contextIsolation"]:
        warnings.append(
            "未显式设置 contextIsolation（默认值随 Electron 版本变化，须逐项确认）"
        )
    if found["nodeIntegration"] and "false" not in found["nodeIntegration"].group(1).lower():
        warnings.append("nodeIntegration 可能被启用：渲染进程可直接触达 Node API")
    if found["sandbox"] and "false" in found["sandbox"].group(1).lower():
        warnings.append("sandbox 被显式关闭：渲染进程隔离弱化")
    return details, warnings


def _packaging_findings(files: list[Path]) -> tuple[list[str], list[str]]:
    details: list[str] = []
    warnings: list[str] = []
    configs = [
        path for path in files
        if path.name in {"electron-builder.yml", "electron-builder.json",
                         "forge.config.js", "forge.config.cjs", "tauri.conf.json"}
    ]
    if not configs:
        warnings.append("未发现打包配置（electron-builder/forge/tauri）——打包完整性无法自动化核对")
        return details, warnings
    blob = "\n".join(_read(path) for path in configs)
    details.append("打包配置: " + ", ".join(path.name for path in configs))
    if not any(hint in blob for hint in SIGNING_HINTS):
        warnings.append("打包配置未声明代码签名/公证字段：分发产物可能未签名")
    if re.search(r"asar\s*:\s*false", blob):
        warnings.append("asar 被关闭：打包内源码/映射直接可读")
    return details, warnings


def _update_findings(files: list[Path]) -> tuple[list[str], list[str]]:
    details: list[str] = []
    warnings: list[str] = []
    hits = [
        path for path in files
        if path.suffix in (".js", ".ts", ".mjs", ".cjs", ".json")
        and ("autoUpdater" in _read(path, 200_000)
             or "checkForUpdates" in _read(path, 200_000))
    ]
    if not hits:
        details.append("未发现自更新通道")
        return details, warnings
    blob = "\n".join(_read(path) for path in hits)
    details.append("自更新入口: " + ", ".join(path.name for path in hits[:3]))
    if "http://" in blob and "https://" not in blob:
        warnings.append("更新源疑似明文 HTTP：存在中间人替换风险")
    if not any(hint in blob for hint in ("signature", "verifySignature", "publicKey")):
        warnings.append("自更新未体现签名校验：更新包完整性需人工确认")
    return details, warnings


def desktop_report(root: Path | str | None = None) -> Report:
    """项目级桌面能力预检（只读）。"""
    root = Path(root or Path.cwd())
    if not root.is_dir():
        return Report("desktop", "BLOCKED", (Check("项目目录", False, str(root)),))
    files = _iter_files(root)
    framework, evidence = _detect_framework(files)
    checks = [Check(
        "桌面工程识别", bool(framework),
        f"framework={framework or '未识别'}；证据={evidence or '无'}",
    )]
    if not framework:
        # 项目形态结论：没有 Electron/Tauri/Qt/WPF 等桌面框架 ⇒ 桌面能力不适用。
        # 与 BLOCKED（识别到桌面框架但取证失败）严格区分（DOC-236 C1）。
        return Report("desktop", "NOT_APPLICABLE", tuple(checks), ())
    warnings: list[str] = []
    if framework == "electron":
        for collector in (_electron_findings, _packaging_findings, _update_findings):
            details, found = collector(files)
            warnings.extend(found)
            if details:
                checks.append(Check(
                    collector.__name__.replace("_findings", "").lstrip("_"),
                    True, "；".join(details), required=False,
                ))
    else:
        details, found = _packaging_findings(files)
        warnings.extend(found)
        if details:
            checks.append(Check("打包配置", True, "；".join(details), required=False))
        warnings.append(
            f"{framework} 的进程模型/更新通道**无自动化探针**：需宿主 Agent 以现有工具取证"
        )
    return Report("desktop", "READY", tuple(checks), tuple(warnings))


def print_report(report: Report, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return
    print(f"[desktop-probe] status={report.status}")
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
    report = desktop_report(args.root)
    print_report(report, as_json=args.json)
    # “不适用”不是失败：退出码 0 表示探针结论有效，避免上游把它误判为执行失败。
    return 0 if report.status in {"READY", "NOT_APPLICABLE"} else 1


if __name__ == "__main__":
    sys.exit(main())
