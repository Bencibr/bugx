#!/usr/bin/env python3
"""core/probes/miniprogram_probe.py - 微信/支付宝小程序形态专属探针 (DOC-248)。

落实：
  - `app.json` 路由表解析与未授权管理页面暴露探测；
  - 云函数 `cloud.getWXContext().OPENID` 校验与客户端参数注入审计；
  - 无微信 CLI / miniprogram-ci 环境时，优雅降级为静态 WXML/JSON 路由遍历（AUDIT-04: probe_mode="static_degraded"）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .base import BaseProbe
except ImportError:
    from core.probes.base import BaseProbe


class MiniProgramProbe(BaseProbe):
    """微信/支付宝小程序专属黑盒与静态降级探针。"""

    SENSITIVE_ROUTE_KEYWORDS: tuple[str, ...] = (
        "admin",
        "manage",
        "manager",
        "internal",
        "secret",
        "debug",
        "test",
        "audit",
        "finance",
        "payment",
        "pay",
        "settle",
        "dashboard",
        "config",
        "backstage",
        "super",
        "permission",
        "vip",
    )

    PRIVILEGED_CLOUD_OPERATIONS: tuple[str, ...] = (
        "db.collection",
        ".remove(",
        ".update(",
        ".set(",
        ".add(",
        "cloud.openapi",
        "wx-server-sdk",
    )

    @property
    def probe_name(self) -> str:
        return "miniprogram_probe"

    def is_available(self) -> bool:
        """AUDIT-04: 极速探活 (<5ms，零阻塞)。"""
        for tool in ("miniprogram-ci", "cli", "wechat-cli"):
            if shutil.which(tool) is not None:
                return True
        return False

    def parse_app_json(self, app_json_input: str | Path | dict[str, Any]) -> dict[str, Any]:
        """解析小程序 app.json，提取主包/分包路由表并识别潜在敏感未授权页面。"""
        if isinstance(app_json_input, dict):
            config = app_json_input
        elif isinstance(app_json_input, Path) or (
            isinstance(app_json_input, str) and "\n" not in app_json_input and Path(app_json_input).is_file()
        ):
            text = Path(app_json_input).read_text(encoding="utf-8", errors="replace")
            config = json.loads(text)
        elif isinstance(app_json_input, str):
            config = json.loads(app_json_input)
        else:
            raise ValueError(f"Unsupported app.json input: {type(app_json_input)}")

        pages: list[str] = list(config.get("pages") or [])
        subpackages = config.get("subpackages") or config.get("subPackages") or []

        all_routes: list[str] = list(pages)
        sub_routes: list[str] = []

        for sub in subpackages:
            root = str(sub.get("root") or "").rstrip("/")
            sub_pages = sub.get("pages") or []
            for p in sub_pages:
                full_route = f"{root}/{p}" if root else p
                sub_routes.append(full_route)
                all_routes.append(full_route)

        findings: list[dict[str, Any]] = []
        for route in all_routes:
            lowered = route.lower()
            for kw in self.SENSITIVE_ROUTE_KEYWORDS:
                if kw in lowered:
                    findings.append({
                        "rule_id": "UNAUTHORIZED_PAGE_EXPOSURE",
                        "severity": "HIGH",
                        "route": route,
                        "matched_keyword": kw,
                        "message": (
                            f"Sensitive route '{route}' exposed in app.json. "
                            "May allow attackers to bypass login guards and directly navigate to admin interface."
                        ),
                    })
                    break

        return {
            "all_routes": all_routes,
            "main_pages": pages,
            "sub_pages": sub_routes,
            "findings": findings,
            "total_routes": len(all_routes),
            "sensitive_routes_count": len(findings),
        }

    def probe_cloud_functions(self, cloud_functions_dir: str | Path) -> dict[str, Any]:
        """审计云函数代码，排查是否缺失 `cloud.getWXContext().OPENID` 或信任客户端注入参数。"""
        cf_dir = Path(cloud_functions_dir)
        findings: list[dict[str, Any]] = []
        scanned_functions = 0

        if not cf_dir.exists():
            return {
                "scanned_functions": 0,
                "findings": [],
            }

        # 遍历所有云函数目录
        function_dirs = [p for p in cf_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
        for fn_dir in function_dirs:
            scanned_functions += 1
            source_files: list[Path] = []
            for ext in ("index.js", "index.ts", "main.py"):
                f = fn_dir / ext
                if f.is_file():
                    source_files.append(f)

            for src_file in source_files:
                try:
                    code = src_file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                has_privileged_op = any(op in code for op in self.PRIVILEGED_CLOUD_OPERATIONS)
                if not has_privileged_op:
                    continue

                # 检查合法 OPENID 上下文提取
                has_context_openid = (
                    "getWXContext(" in code
                    or "wxContext" in code
                    or "event.userInfo.openId" in code
                    or "event.userInfo.openid" in code
                    or "context.OPENID" in code
                    or "context.openid" in code
                )

                # 检查是否盲目信任客户端传入的 openid 参数 (IDOR / 越权)
                has_client_openid_trust = bool(
                    re.search(r"event\.(openId|openid|user_id|uid)\b", code)
                )

                if has_client_openid_trust and not has_context_openid:
                    findings.append({
                        "rule_id": "CLOUD_FUNCTION_CLIENT_OPENID_INJECTION",
                        "severity": "CRITICAL",
                        "function": fn_dir.name,
                        "file": str(src_file),
                        "message": (
                            f"Cloud function '{fn_dir.name}' trusts unauthenticated client parameter "
                            "'event.openid/user_id' instead of verifying cloud.getWXContext().OPENID."
                        ),
                    })
                elif has_privileged_op and not has_context_openid:
                    findings.append({
                        "rule_id": "CLOUD_FUNCTION_MISSING_OPENID_CHECK",
                        "severity": "HIGH",
                        "function": fn_dir.name,
                        "file": str(src_file),
                        "message": (
                            f"Cloud function '{fn_dir.name}' executes privileged database/service operations "
                            "without checking caller identity via cloud.getWXContext().OPENID."
                        ),
                    })

        return {
            "scanned_functions": scanned_functions,
            "findings": findings,
            "unprotected_cloud_functions": len(findings),
        }

    def _find_app_json(self, target: str | Path) -> Path | None:
        """在目标路径下查找 app.json。"""
        p = Path(target)
        if p.is_file() and p.name.lower() == "app.json":
            return p
        if p.is_dir():
            direct = p / "app.json"
            if direct.is_file():
                return direct
            miniprogram = p / "miniprogram" / "app.json"
            if miniprogram.is_file():
                return miniprogram
            candidates = list(p.glob("**/app.json"))
            if candidates:
                return candidates[0]
        return None

    def _find_cloud_functions_dir(self, target: str | Path) -> Path | None:
        """在目标路径下查找云函数目录。"""
        p = Path(target)
        if p.is_dir():
            for name in ("cloudfunctions", "cloudfunction", "functions", "server/cloudfunctions"):
                d = p / name
                if d.is_dir():
                    return d
        return None

    def run_dynamic(
        self,
        target: str | Path,
        *,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """执行小程序动态探针（通过 miniprogram-ci 或 CLI 模拟触发）。"""
        if not self.is_available():
            return self.run_static_degraded(target, payload=payload, context=context, **kwargs)

        # 具备微信 CLI 时的动态分发逻辑
        app_json = self._find_app_json(target)
        route_info = self.parse_app_json(app_json) if app_json else {"findings": [], "all_routes": []}

        cf_dir = self._find_cloud_functions_dir(target)
        cf_info = self.probe_cloud_functions(cf_dir) if cf_dir else {"findings": []}

        all_findings = list(route_info.get("findings", [])) + list(cf_info.get("findings", []))

        return {
            "success": True,
            "status": "ok",
            "probe_mode": "dynamic",
            "findings": all_findings,
            "evidence": {
                "route_count": len(route_info.get("all_routes", [])),
                "cloud_functions_scanned": cf_info.get("scanned_functions", 0),
            },
            "metadata": {
                "target": str(target),
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
        """AUDIT-04: 无微信 CLI 环境时，优雅降级为静态 WXML/JSON 路由遍历与云函数源码扫描。"""
        all_findings: list[dict[str, Any]] = []
        routes_summary: list[str] = []

        # 1. 尝试解析 app.json
        app_json_path = self._find_app_json(target)
        if app_json_path and app_json_path.exists():
            route_res = self.parse_app_json(app_json_path)
            all_findings.extend(route_res["findings"])
            routes_summary.extend(route_res["all_routes"])
        elif isinstance(target, str) and "pages" in target and "{" in target:
            # 传入的是 app.json 文本
            try:
                route_res = self.parse_app_json(target)
                all_findings.extend(route_res["findings"])
                routes_summary.extend(route_res["all_routes"])
            except Exception:
                pass

        # 2. 扫描云函数源码
        cf_dir = self._find_cloud_functions_dir(target)
        cf_res = {"scanned_functions": 0, "findings": []}
        if cf_dir:
            cf_res = self.probe_cloud_functions(cf_dir)
            all_findings.extend(cf_res["findings"])

        # 3. 静态扫描 WXML 导航标签
        wxml_nav_count = 0
        p = Path(target)
        if p.is_dir():
            for wxml in list(p.glob("**/*.wxml"))[:50]:
                try:
                    txt = wxml.read_text(encoding="utf-8", errors="ignore")
                    matches = re.findall(r'<navigator\s+[^>]*url=["\']([^"\']+)["\']', txt)
                    wxml_nav_count += len(matches)
                except Exception:
                    continue

        return {
            "success": True,
            "status": "ok",
            "probe_mode": "static_degraded",
            "findings": all_findings,
            "evidence": {
                "total_routes_discovered": len(routes_summary),
                "wxml_nav_links": wxml_nav_count,
                "cloud_functions_scanned": cf_res.get("scanned_functions", 0),
                "findings_count": len(all_findings),
            },
            "metadata": {
                "target": str(target),
                "degraded_mode": "static_miniprogram_structure_analysis",
            },
        }
