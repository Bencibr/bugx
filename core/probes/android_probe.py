#!/usr/bin/env python3
"""core/probes/android_probe.py - Android 形态专属黑盒探针 (DOC-248)。

落实：
  - Manifest 导出组件解析与安全策略评估（Activity / Service / Receiver / Provider）；
  - `adb_send_intent` 执行与设备锁隔离（AUDIT-02: ADB 设备锁与 `-s <device_id>` 互斥）；
  - 本地无 adb 或无设备时，优雅降级为静态 AndroidManifest.xml 语义解析（AUDIT-04: probe_mode="static_degraded"）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
        """AUDIT-04: 极速探活 (<5ms，零阻塞)。"""
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
        """AUDIT-04: 无 adb 环境时，优雅降级为静态 AndroidManifest.xml 语义解析。"""
        manifest_file = self._find_manifest(target)
        if manifest_file and manifest_file.exists():
            parsed = self.parse_manifest(manifest_file)
            return {
                "success": True,
                "status": "ok",
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
            parsed = self.parse_manifest(target)
            return {
                "success": True,
                "status": "ok",
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
            "success": True,
            "status": "degraded_empty",
            "probe_mode": "static_degraded",
            "findings": [],
            "evidence": {},
            "metadata": {
                "target": str(target),
                "degraded_reason": "android_manifest_not_found",
            },
        }
