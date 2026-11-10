#!/usr/bin/env python3
"""iOS 二进制层探针：Mach-O / 代码签名 / entitlements / 加固属性。

为什么需要独立探针：semgrep 只能读源码与 plist 文本，**看不到**已构建产物里的
PIE/ASLR、栈保护、cryptid、签名与 entitlements。移动端审计若只跑规则，
会把"未加固的发布包"误判成"没问题"。

输入：`.ipa` / `.app` 目录 / Mach-O 文件。
输出：``READY`` / ``DEGRADED``（工具缺失或无产物时**不伪造**结论）。

只读契约：不修改目标；`.ipa` 解压到系统临时目录并在退出前清理。
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
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

TOOL_TIMEOUT = 10.0


def _run(args: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=TOOL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _find_app_bundle(root: Path) -> Path | None:
    """在目录/解压结果中定位 .app（优先 Payload/）。"""
    if root.suffix == ".app" and root.is_dir():
        return root
    payload = root / "Payload"
    for base in (payload, root):
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if child.suffix == ".app" and child.is_dir():
                    return child
    return None


def _is_macho(path: Path) -> bool:
    """魔数校验：避免把普通文件当成 Mach-O 后由 otool 噪声产出误报。"""
    try:
        magic = path.open("rb").read(4)
    except OSError:
        return False
    return magic in {
        b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",  # 64/32 小端
        b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",  # 64/32 大端
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",  # FAT
        b"\xca\xfe\xba\xbf",
    }


def _find_macho(bundle: Path) -> Path | None:
    """.app 的主二进制名默认等于包名（去扩展名）。"""
    candidate = bundle / bundle.stem
    if candidate.is_file():
        return candidate
    for child in sorted(bundle.iterdir()):
        if child.is_file() and child.stat().st_mode & 0o111:
            return child
    return None


def _read_plist(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _info_plist_findings(info: dict, evidence: str) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    ats = info.get("NSAppTransportSecurity") or {}
    if isinstance(ats, dict):
        if ats.get("NSAllowsArbitraryLoads") is True:
            findings.append({
                "id": "ios-binary-ats-arbitrary-loads",
                "severity": "high",
                "message": "NSAllowsArbitraryLoads=true：发布包关闭了 ATS",
                "evidence": evidence,
            })
        if ats.get("NSAllowsArbitraryLoadsInWebContent") is True:
            findings.append({
                "id": "ios-binary-ats-webcontent",
                "severity": "medium",
                "message": "NSAllowsArbitraryLoadsInWebContent=true：WebView 可走明文",
                "evidence": evidence,
            })
    if info.get("UIFileSharingEnabled") is True:
        findings.append({
            "id": "ios-binary-file-sharing",
            "severity": "medium",
            "message": "UIFileSharingEnabled=true：文档目录可被 Finder/iTunes 读取",
            "evidence": evidence,
        })
    facts = {
        "bundle_id": info.get("CFBundleIdentifier"),
        "min_os": info.get("MinimumOSVersion"),
        "platform": info.get("DTPlatformName"),
    }
    return findings, facts


def _entitlements_findings(binary: Path) -> tuple[list[dict], dict, str | None]:
    """读代码签名里的 entitlements（需要 codesign）。"""
    if shutil.which("codesign") is None:
        return [], {}, "codesign 不可用"
    code, output = _run(["codesign", "-d", "--entitlements", ":-", str(binary)])
    if code != 0 or "<key>" not in output:
        return [], {}, "二进制未签名或读取 entitlements 失败"
    start = output.find("<?xml")
    if start < 0:
        start = output.find("<plist")
    try:
        payload = plistlib.loads(output[start:].encode("utf-8"))
    except (plistlib.InvalidFileException, ValueError):
        return [], {}, "entitlements 解析失败"
    entitlements = payload if isinstance(payload, dict) else {}
    findings: list[dict] = []
    if entitlements.get("get-task-allow") is True:
        findings.append({
            "id": "ios-binary-get-task-allow",
            "severity": "high",
            "message": "entitlements 含 get-task-allow=true：允许调试器附加，发布包必须为 false",
            "evidence": str(binary),
        })
    if entitlements.get("com.apple.security.app-sandbox") is False:
        findings.append({
            "id": "ios-binary-no-app-sandbox",
            "severity": "high",
            "message": "entitlements 关闭了 app-sandbox",
            "evidence": str(binary),
        })
    return findings, entitlements, None


def _macho_findings(binary: Path) -> tuple[list[dict], dict, str | None]:
    """Mach-O 加固属性（需要 otool）。"""
    if shutil.which("otool") is None:
        return [], {}, "otool 不可用"
    findings: list[dict] = []
    facts: dict = {}
    code, header = _run(["otool", "-hv", str(binary)])
    if code != 0:
        return [], {}, "otool 读取 Mach-O 头失败"
    lines = [line for line in header.splitlines() if line.strip()]
    facts["macho_header"] = lines[1].strip() if len(lines) > 1 else lines[0].strip()
    if "PIE" not in header:
        findings.append({
            "id": "ios-binary-no-pie",
            "severity": "high",
            "message": "Mach-O 未设置 PIE：无法启用 ASLR",
            "evidence": str(binary),
        })
    if shutil.which("nm"):
        code, undefined = _run(["nm", "-u", str(binary)])
        if code == 0:
            has_canary = "__stack_chk_fail" in undefined
            facts["stack_canary"] = has_canary
            if not has_canary:
                findings.append({
                    "id": "ios-binary-no-stack-canary",
                    "severity": "medium",
                    "message": "未见 __stack_chk_fail：可能未启用栈保护",
                    "evidence": str(binary),
                })
    code, loads = _run(["otool", "-l", str(binary)])
    if code == 0:
        cryptid = None
        for line in loads.splitlines():
            stripped = line.strip()
            if stripped.startswith("cryptid"):
                value = stripped.split()[-1]
                if value.isdigit():
                    cryptid = int(value)
        facts["cryptid"] = cryptid
        if cryptid == 1:
            findings.append({
                "id": "ios-binary-encrypted",
                "severity": "info",
                "message": "二进制仍加密（cryptid=1）：静态符号分析受限，需先脱壳",
                "evidence": str(binary),
            })
    code, linked = _run(["otool", "-L", str(binary)])
    if code == 0:
        libs = [
            line.split("(")[0].strip()
            for line in linked.splitlines()[1:] if line.strip()
        ]
        facts["linked_libraries"] = libs[:20]
        risky = [item for item in libs if item.startswith(("@rpath", "@loader_path", "./"))]
        if risky:
            findings.append({
                "id": "ios-binary-relative-dylib",
                "severity": "medium",
                "message": f"存在相对路径依赖（潜在 dylib 劫持面）：{', '.join(risky[:5])}",
                "evidence": str(binary),
            })
    return findings, facts, None


def inspect(root: Path) -> dict:
    report: dict = {
        "name": "ios_binary",
        "status": "DEGRADED",
        "target": str(root),
        "findings": [],
        "facts": {},
        "tools": {},
        "notes": [],
    }
    if not root.exists():
        report["notes"].append("目标路径不存在")
        return report
    # 工具可用性属于**证据**：即使后续因非 Mach-O 提前返回，也必须记录
    # "哪些检查根本没跑"，否则读者会误以为做过二进制检查。
    report["tools"] = {
        name: bool(shutil.which(name)) for name in ("codesign", "otool", "nm")
    }
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        target = root
        if root.is_file() and root.suffix == ".ipa":
            temp_dir = tempfile.TemporaryDirectory(prefix="bugx-ipa-")
            with zipfile.ZipFile(root) as archive:
                archive.extractall(temp_dir.name)
            target = Path(temp_dir.name)
        bundle = _find_app_bundle(target) if target.is_dir() else None
        if bundle is None:
            report["notes"].append("未找到 .app 包（需要 .ipa / .app / Mach-O）")
            return report
        info_plist = bundle / "Info.plist"
        has_plist = info_plist.is_file()
        if has_plist:
            findings, facts = _info_plist_findings(_read_plist(info_plist), str(info_plist))
            report["findings"].extend(findings)
            report["facts"].update({k: v for k, v in facts.items() if v is not None})
        else:
            report["notes"].append("缺少 Info.plist")
        binary = _find_macho(bundle)
        if binary is None:
            report["notes"].append("未找到 Mach-O 主二进制")
            report["status"] = "READY" if has_plist else "DEGRADED"
            return report
        report["facts"]["binary"] = str(binary)
        is_macho = _is_macho(binary)
        report["facts"]["is_macho"] = is_macho
        if not is_macho:
            report["notes"].append(
                "主二进制不是 Mach-O（跳过 codesign/otool/nm：PIE、栈保护、cryptid 均未检查）"
            )
            report["status"] = "READY" if has_plist else "DEGRADED"
            return report
        ent_findings, entitlements, ent_note = _entitlements_findings(binary)
        report["findings"].extend(ent_findings)
        if entitlements:
            report["facts"]["entitlements"] = sorted(entitlements)
        if ent_note:
            report["notes"].append(ent_note)
        macho_findings, macho_facts, macho_note = _macho_findings(binary)
        report["findings"].extend(macho_findings)
        report["facts"].update(macho_facts)
        if macho_note:
            report["notes"].append(macho_note)
        # READY 的判据是"至少完成一类检查"，而不是"没发现问题"。
        executed = bool(entitlements) or bool(macho_facts) or has_plist
        report["status"] = "READY" if executed else "DEGRADED"
        return report
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def check_availability() -> dict:
    """Check host-side iOS binary tooling without reading a project artifact."""
    checks = []
    for name in ("codesign", "otool"):
        path = shutil.which(name)
        checks.append({
            "name": name,
            "ok": path is not None,
            "required": True,
            "detail": path or f"未找到 {name}",
        })
    nm_path = shutil.which("nm")
    checks.append({
        "name": "nm", "ok": nm_path is not None, "required": False,
        "detail": nm_path or "未找到 nm；符号表检查将跳过",
    })
    ready = all(check["ok"] for check in checks if check["required"])
    return {
        "name": "ios_binary",
        "status": "READY" if ready else "DEGRADED",
        "checks": checks,
        "capabilities": ["ios_binary_inspection"] if ready else [],
        "fallbacks": {},
        "warnings": [],
    }


def print_report(report: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"[{report['status']}] iOS 二进制层：{report.get('target', '宿主工具可用性')}")
    for key, value in report["facts"].items():
        if key == "entitlements":
            value = f"{len(value)} 项"
        print(f"  {key}: {value}")
    for finding in report["findings"]:
        print(f"  ! [{finding['severity']}] {finding['id']}: {finding['message']}")
    for note in report["notes"]:
        print(f"  · {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="iOS 二进制层探针（Mach-O/签名/entitlements）")
    parser.add_argument("--root", required=True, help=".ipa / .app / Mach-O / 工程目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--allow-fallback", action="store_true",
        help="DEGRADED 也返回 0（默认返回 2，避免把缺失当通过）",
    )
    parser.add_argument(
        "--check-availability", action="store_true",
        help="只检查宿主分析工具，不读取 --root 下的目标文件",
    )
    args = parser.parse_args(argv)
    report = (
        check_availability()
        if args.check_availability
        else inspect(Path(args.root).expanduser())
    )
    print_report(report, as_json=args.json)
    if report["status"] == "DEGRADED" and not args.allow_fallback:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())


