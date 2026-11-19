#!/usr/bin/env python3
"""core/probes/android_probe.py - Android 形态专属黑盒探针 (DOC-248)。

落实：
  - Manifest 导出组件解析与安全策略评估（Activity / Service / Receiver / Provider）；
  - `adb_send_intent` 执行与设备锁隔离（AUDIT-02: ADB 设备锁与 `-s <device_id>` 互斥）；
  - 本地无 adb 或无设备时，优雅降级为静态 AndroidManifest.xml 语义解析（AUDIT-04: probe_mode="static_degraded"）。
"""

from __future__ import annotations

# 脚本被 CLI 以独立进程执行（bugx probe android）时的 sys.path 自举：
# 沿 parents 找 ``core/bugx.md`` 引擎标记，把引擎/仓库根插入 sys.path——
# 与 mobile_probe.py 同形态（缺了它 ``from core.probes.base`` 在子进程
# 里必然 ImportError，F-RSI-20261118-003 实测）。
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
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence
import xml.etree.ElementTree as ET

try:
    from .base import BaseProbe
except ImportError:
    from core.probes.base import BaseProbe

from core.infra.safe_xml import parse_xml, parse_xml_file


class AndroidProbe(BaseProbe):
    """Android 专属黑盒与静态结构降级探针。"""

    ANDROID_NS = "{http://schemas.android.com/apk/res/android}"

    # AUDIT-02: 设备级排他互斥锁，防止并发 Worker 向同一台 Android 设备发送 Intent 时冲突死锁
    _DEVICE_LOCKS: dict[str, threading.Lock] = {}
    _LOCK_GUARD = threading.Lock()

    @property
    def probe_name(self) -> str:
        return "android_probe"

    def is_available(self) -> bool:
        """AUDIT-04: 极速探活 (<5ms，零阻塞)。

        降级契约：布尔返回值受 BaseProbe 布尔契约约束（execute_probe 以真值
        判定是否降级）；(bool, detail) 明细结构见模块级 ``check_availability()``。
        """
        return shutil.which("adb") is not None

    @classmethod
    def get_device_lock(cls, device_id: str | None) -> threading.Lock:
        """AUDIT-02: 获取指定设备 ID 的局部排他锁。"""
        dev = device_id or "default"
        with cls._LOCK_GUARD:
            if dev not in cls._DEVICE_LOCKS:
                cls._DEVICE_LOCKS[dev] = threading.Lock()
            return cls._DEVICE_LOCKS[dev]

    @classmethod
    def _get_android_attr(
        cls, elem: ET.Element, attr_name: str, default: str | None = None
    ) -> str | None:
        """从 XML 元素中提取 android 命名空间属性。"""
        val = elem.attrib.get(f"{cls.ANDROID_NS}{attr_name}")
        if val is not None:
            return val
        val = elem.attrib.get(f"android:{attr_name}")
        if val is not None:
            return val
        val = elem.attrib.get(attr_name)
        if val is not None:
            return val
        return default

    def parse_manifest(self, manifest_input: str | Path | bytes) -> dict[str, Any]:
        """安全解析 AndroidManifest.xml，提取所有导出组件与潜在越权威胁。"""
        if isinstance(manifest_input, Path) or (
            isinstance(manifest_input, str) and "\n" not in manifest_input and Path(manifest_input).is_file()
        ):
            root = parse_xml_file(Path(manifest_input))
        elif isinstance(manifest_input, (str, bytes)):
            root = parse_xml(manifest_input)
        else:
            raise ValueError(f"Unsupported manifest input: {type(manifest_input)}")

        package_name = root.attrib.get("package", "")
        application = root.find("application")

        findings: list[dict[str, Any]] = []
        exported_components: list[dict[str, Any]] = []

        if application is not None:
            debuggable = self._get_android_attr(application, "debuggable")
            if debuggable and debuggable.lower() == "true":
                findings.append({
                    "rule_id": "DEBUGGABLE_APP_ENABLED",
                    "severity": "HIGH",
                    "message": "Application has android:debuggable='true' enabled in production build",
                })

            allow_backup = self._get_android_attr(application, "allowBackup")
            if allow_backup and allow_backup.lower() == "true":
                findings.append({
                    "rule_id": "ALLOW_BACKUP_ENABLED",
                    "severity": "MEDIUM",
                    "message": "Application has android:allowBackup enabled, allowing unauthorized adb backup extraction",
                })

            # AM-4: 明文流量声明（CWE-311/MSTG-NETWORK-1）。与 debuggable/allowBackup
            # 同形态的 application 级 Manifest 属性检查；targetSdk >= 28 默认禁用，
            # 显式声明 true 即为回退到明文通道，全量 HTTP 流量暴露于中间人拦截。
            uses_cleartext = self._get_android_attr(application, "usesCleartextTraffic")
            if uses_cleartext and uses_cleartext.lower() == "true":
                findings.append({
                    "rule_id": "CLEARTEXT_TRAFFIC_ENABLED",
                    "severity": "HIGH",
                    "message": "Application declares android:usesCleartextTraffic='true', permitting unencrypted HTTP traffic that is exposed to MITM interception",
                })

            component_tags = ("activity", "activity-alias", "service", "receiver", "provider")
            for elem in application:
                tag_name = elem.tag.split("}")[-1]
                if tag_name not in component_tags:
                    continue

                comp_name = self._get_android_attr(elem, "name", "")
                exported_raw = self._get_android_attr(elem, "exported")
                permission = self._get_android_attr(elem, "permission")

                # 解析 intent-filter
                intent_filters: list[dict[str, Any]] = []
                for child in elem:
                    if child.tag.split("}")[-1] == "intent-filter":
                        actions = [
                            self._get_android_attr(a, "name", "")
                            for a in child
                            if a.tag.split("}")[-1] == "action"
                        ]
                        categories = [
                            self._get_android_attr(c, "name", "")
                            for c in child
                            if c.tag.split("}")[-1] == "category"
                        ]
                        data_schemes = [
                            self._get_android_attr(d, "scheme", "")
                            for d in child
                            if d.tag.split("}")[-1] == "data"
                        ]
                        intent_filters.append({
                            "actions": [a for a in actions if a],
                            "categories": [c for c in categories if c],
                            "schemes": [s for s in data_schemes if s],
                        })

                # 判定导出状态：显式 true，或未显式声明但在 Android 早期规范中因存在 intent-filter 默认导出
                is_exported = False
                if exported_raw is not None:
                    is_exported = exported_raw.lower() == "true"
                else:
                    is_exported = len(intent_filters) > 0

                comp_info = {
                    "type": tag_name,
                    "name": comp_name,
                    "exported": is_exported,
                    "explicit_exported": exported_raw is not None,
                    "permission": permission,
                    "intent_filters": intent_filters,
                }
                exported_components.append(comp_info)

                # 判定越权风险
                if is_exported and not permission:
                    rule_id = f"UNPROTECTED_EXPORTED_{tag_name.upper()}"
                    severity = "CRITICAL" if tag_name == "provider" else "HIGH"
                    findings.append({
                        "rule_id": rule_id,
                        "severity": severity,
                        "component": comp_name,
                        "type": tag_name,
                        "message": (
                            f"Exported {tag_name} '{comp_name}' does not require any permission guard, "
                            "allowing any third-party app to hijack or invoke it."
                        ),
                    })

                # DeepLink 劫持风险分析
                for flt in intent_filters:
                    for scheme in flt.get("schemes", []):
                        if scheme in ("http", "https", "myapp", "app"):
                            findings.append({
                                "rule_id": "DEEPLINK_HIJACK_RISK",
                                "severity": "MEDIUM",
                                "component": comp_name,
                                "scheme": scheme,
                                "message": f"Component exposes deep link scheme '{scheme}' which may be intercepted",
                            })

        return {
            "package": package_name,
            "exported_components": exported_components,
            "findings": findings,
            "total_components": len(exported_components),
            "unprotected_count": len([f for f in findings if f["rule_id"].startswith("UNPROTECTED")]),
        }

    def adb_send_intent(
        self,
        device_id: str | None = None,
        action: str | None = None,
        component: str | None = None,
        data_uri: str | None = None,
        extras: Mapping[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """AUDIT-02: 发送 ADB Intent 测试组件拉起，持有指定设备锁防止并发冲突。"""
        if not self.is_available():
            return {
                "success": False,
                "status": "adb_not_available",
                "error": "ADB binary not found in PATH",
            }

        cmd: list[str] = ["adb"]
        if device_id:
            cmd.extend(["-s", device_id])
        cmd.extend(["shell", "am", "start"])

        if action:
            cmd.extend(["-a", action])
        if component:
            cmd.extend(["-n", component])
        if data_uri:
            cmd.extend(["-d", data_uri])
        if extras:
            for k, v in extras.items():
                if isinstance(v, bool):
                    cmd.extend(["--ez", str(k), str(v).lower()])
                elif isinstance(v, int):
                    cmd.extend(["--ei", str(k), str(v)])
                else:
                    cmd.extend(["--es", str(k), str(v)])

        lock = self.get_device_lock(device_id)
        with lock:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                output = f"{proc.stdout}\n{proc.stderr}"
                is_error = "Error: Activity not started" in output or "SecurityException" in output
                return {
                    "success": proc.returncode == 0 and not is_error,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "command": cmd,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "status": "execution_failed",
                    "error": str(exc),
                    "command": cmd,
                }

    def _find_manifest(self, target: str | Path) -> Path | None:
        """定位 AndroidManifest.xml 文件。"""
        path = Path(target)
        if path.is_file() and path.name.lower() == "androidmanifest.xml":
            return path
        if path.is_dir():
            direct = path / "AndroidManifest.xml"
            if direct.is_file():
                return direct
            src_main = path / "app" / "src" / "main" / "AndroidManifest.xml"
            if src_main.is_file():
                return src_main
            candidates = list(path.glob("**/AndroidManifest.xml"))
            if candidates:
                return candidates[0]
        return None

    def run_dynamic(
        self,
        target: str | Path,
        *,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """执行 Android 动态交互探针（拉起 Intent 验证越权）。"""
        manifest_file = self._find_manifest(target)
        if not manifest_file:
            # 找不到 Manifest，直接降级
            return self.run_static_degraded(target, payload=payload, context=context, **kwargs)

        parsed = self.parse_manifest(manifest_file)
        device_id = (payload or {}).get("device_id")
        intent_results: list[dict[str, Any]] = []

        # 尝试通过 adb 拉起所有未授权导出组件，验证是否可被直接拉起
        pkg = parsed.get("package", "")
        for comp in parsed.get("exported_components", []):
            if comp.get("exported") and not comp.get("permission"):
                comp_name = comp["name"]
                if comp_name.startswith("."):
                    full_name = f"{pkg}/{pkg}{comp_name}"
                elif "/" not in comp_name:
                    full_name = f"{pkg}/{comp_name}"
                else:
                    full_name = comp_name

                intent_res = self.adb_send_intent(
                    device_id=device_id,
                    component=full_name,
                )
                intent_results.append({
                    "component": full_name,
                    "result": intent_res,
                })

        return {
            "success": True,
            "status": "ok",
            "probe_mode": "dynamic",
            "findings": parsed["findings"],
            "evidence": {
                "manifest_package": pkg,
                "exported_count": parsed["unprotected_count"],
                "intent_verification": intent_results,
            },
            "metadata": {
                "target": str(target),
                "device_id": device_id or "default",
            },
        }

    def run_static_degraded(
        self,
        target: str | Path,
        *,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """AUDIT-04: 无 adb 环境时，优雅降级为静态 AndroidManifest.xml 语义解析。

        降级契约：输出与成功路径（run_dynamic，status="ok"）**可区分**——
        status="degraded" + degraded=True + degraded_reason，绝不伪装成正常动态结果。
        """
        if self.is_available():
            degraded_reason = "static_mode_forced"
        else:
            degraded_reason = "adb_unavailable"

        manifest_file = self._find_manifest(target)
        if manifest_file and manifest_file.exists():
            try:
                parsed = self.parse_manifest(manifest_file)
            except Exception as exc:
                # 显式失败结构，禁止静默吞错（解析失败 ≠ 未发现问题）
                return {
                    "success": False,
                    "status": "degraded",
                    "degraded": True,
                    "degraded_reason": "android_manifest_parse_failed",
                    "probe_mode": "static_degraded",
                    "findings": [],
                    "evidence": {
                        "manifest_path": str(manifest_file),
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                    },
                    "metadata": {
                        "target": str(target),
                        "degraded_mode": "static_manifest_semantic_analysis",
                    },
                }
            return {
                "success": True,
                "status": "degraded",
                "degraded": True,
                "degraded_reason": degraded_reason,
                "probe_mode": "static_degraded",
                "findings": parsed["findings"],
                "evidence": {
                    "manifest_path": str(manifest_file),
                    "package": parsed["package"],
                    "unprotected_components": parsed["unprotected_count"],
                    "total_components": parsed["total_components"],
                },
                "metadata": {
                    "target": str(target),
                    "degraded_mode": "static_manifest_semantic_analysis",
                },
            }

        # 若 target 是直接传入的 XML 文本
        if isinstance(target, str) and "<manifest" in target:
            try:
                parsed = self.parse_manifest(target)
            except Exception as exc:
                return {
                    "success": False,
                    "status": "degraded",
                    "degraded": True,
                    "degraded_reason": "android_manifest_parse_failed",
                    "probe_mode": "static_degraded",
                    "findings": [],
                    "evidence": {
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                    },
                    "metadata": {
                        "degraded_mode": "static_manifest_string_analysis",
                    },
                }
            return {
                "success": True,
                "status": "degraded",
                "degraded": True,
                "degraded_reason": degraded_reason,
                "probe_mode": "static_degraded",
                "findings": parsed["findings"],
                "evidence": {
                    "package": parsed["package"],
                    "unprotected_components": parsed["unprotected_count"],
                    "total_components": parsed["total_components"],
                },
                "metadata": {
                    "degraded_mode": "static_manifest_string_analysis",
                },
            }

        return {
            "success": False,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "android_manifest_not_found",
            "probe_mode": "static_degraded",
            "findings": [],
            "evidence": {},
            "metadata": {
                "target": str(target),
                "degraded_mode": "static_manifest_semantic_analysis",
            },
        }


def check_availability() -> dict[str, Any]:
    """降级契约：Android 审计能力结构化探活明细（(bool, detail) 的结构化形态）。

    机械探测 adb 是否在 PATH（<5ms，零阻塞、零子进程）；READY/DEGRADED
    与 check_availability 明细一并返回，DEGRADED 时必须走
    ``AndroidProbe.run_static_degraded``，禁止伪装成动态审计结果。
    """
    adb_path = shutil.which("adb")
    ready = adb_path is not None
    return {
        "name": "android_probe",
        "status": "READY" if ready else "DEGRADED",
        "available": ready,
        "detail": adb_path or "adb 未安装：动态 Intent 拉起不可用，降级为静态 Manifest 解析",
        "checks": [
            {
                "name": "adb",
                "ok": ready,
                "required": True,
                "detail": adb_path or "未找到 adb（安装：brew install android-platform-tools）",
            }
        ],
        "capabilities": ["adb_intent_dispatch", "device_lock_mutex"] if ready else [],
        "fallbacks": {} if ready else {
            "audit": "AndroidProbe.run_static_degraded（AndroidManifest.xml 静态语义解析）",
        },
        "warnings": [],
    }


def print_report(report: dict[str, Any], as_json: bool = False) -> None:
    """打印 check 报告（``--json`` 原样输出，否则人读摘要）。"""
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    status = report.get("status", "UNKNOWN")
    print(f"[{status}] Android 审计通道（Manifest 静态语义 + adb 动态）")
    for item in report.get("checks", []):
        print(f"  {'✓' if item.get('ok') else '!'} {item['name']}: {item['detail']}")
    if status != "READY":
        print("  ⏸ 动态 Intent 通道不可用；静态审计可显式执行：--static-degraded")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（``bugx probe android``）：check=通道探活，--static-degraded=静态审计。

    与 mobile_probe 同语义：``check``/``status`` 只探测通道可用性（不产出
    findings）；静态 manifest 审计必须显式 ``--static-degraded`` 触发，
    DEGRADED 结果绝不伪装为动态审计。
    """
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
        help="显式执行降级路径（AndroidManifest.xml 静态语义解析；"
             "输出 status=degraded，绝不伪造 adb 动态证据）",
    )
    args = parser.parse_args(argv)
    if args.static_degraded:
        report = AndroidProbe().run_static_degraded(args.root)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else
              f"[{report.get('status')}] findings={len(report.get('findings', []))}")
        return 0
    report = check_availability()
    print_report(report, args.json)
    if report["status"] == "READY":
        return 0
    if args.allow_degraded:
        return 0
    if args.strict:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
