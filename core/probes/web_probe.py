#!/usr/bin/env python3
"""core/probes/web_probe.py - Web 形态专属动态黑盒探针 (DOC-248)。

落实：
  - AUDIT-02: Playwright 并发上限约束 (MAX_BROWSER_INSTANCES = 2) 与资源池互斥锁；
  - AUDIT-04: 环境缺失时极速探活 (<5ms) 与自动降级为静态代码/DOM结构分析；
  - AUDIT-05: 严禁仅凭 HTTP 500/403 定性漏洞，返回结构化四元响应：
              (status_code, body_diff, response_time, error_stack)。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .base import BaseProbe
except ImportError:
    from core.probes.base import BaseProbe


class WebDynamicProbe(BaseProbe):
    """Web 专属黑盒动态探针，支持 HTTP 原始发包与 Playwright 无头浏览器探针。"""

    MAX_BROWSER_INSTANCES: int = 2
    _BROWSER_SEMAPHORE = threading.BoundedSemaphore(MAX_BROWSER_INSTANCES)
    _ACTIVE_INSTANCES = 0
    _LOCK = threading.Lock()

    # 常见数据库报错特征，用于判断是否为 SQL 注入导致的 500 报错，而非普通 500
    DATABASE_ERROR_SIGNATURES: tuple[str, ...] = (
        "syntax error at or near",
        "ora-00933",
        "ora-00936",
        "mysql_fetch_array()",
        "you have an error in your sql syntax",
        "sqlite3.operationalerror",
        "unclosed quotation mark after the character string",
        "pg_query(): query failed",
        "microsoft ole db provider for sql server",
        "org.hibernate.exception.sqlgrammarexception",
    )

    # 敏感堆栈暴露签名
    STACK_TRACE_SIGNATURES: tuple[str, ...] = (
        "traceback (most recent call last):",
        "at org.springframework.",
        "exception in thread \"main\"",
        "fatal error: uncaught exception",
        "microsoft.aspnetcore.diagnostics.developerexceptionpage",
    )

    # 静态 DOM XSS 危险汇聚点 (Sinks)
    DOM_XSS_PATTERNS: tuple[tuple[str, str], ...] = (
        (r"\.innerHTML\s*=", "DOM_XSS_INNER_HTML"),
        (r"document\.write\s*\(", "DOM_XSS_DOCUMENT_WRITE"),
        (r"\beval\s*\(", "DOM_XSS_EVAL"),
        (r"v-html\s*=", "DOM_XSS_V_HTML"),
        (r"dangerouslySetInnerHTML\s*=", "DOM_XSS_REACT_DANGEROUSLY_SET"),
    )

    @property
    def probe_name(self) -> str:
        return "web_dynamic_probe"

    def is_available(self) -> bool:
        """AUDIT-04: 极速探活 (<5ms，零阻塞)。"""
        if shutil.which("playwright") is not None:
            return True
        try:
            import importlib.util
            return importlib.util.find_spec("playwright") is not None
        except Exception:
            return False

    @classmethod
    @contextlib.contextmanager
    def browser_slot(cls, timeout: float = 2.0):
        """AUDIT-02: Playwright 浏览器实例资源池锁，上限 MAX_BROWSER_INSTANCES (2)。"""
        acquired = cls._BROWSER_SEMAPHORE.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError(
                f"Playwright browser instance pool exhausted (limit: {cls.MAX_BROWSER_INSTANCES})"
            )
        with cls._LOCK:
            cls._ACTIVE_INSTANCES += 1
        try:
            yield
        finally:
            with cls._LOCK:
                cls._ACTIVE_INSTANCES = max(0, cls._ACTIVE_INSTANCES - 1)
            cls._BROWSER_SEMAPHORE.release()

    @classmethod
    def get_active_instances(cls) -> int:
        """获取当前活跃的无头浏览器实例数。"""
        with cls._LOCK:
            return cls._ACTIVE_INSTANCES

    @classmethod
    def classify_http_response(
        cls,
        response_data: Mapping[str, Any],
        baseline_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """AUDIT-05: 严禁仅凭 HTTP 500/403 定性。

        返回结构化响应四元组 (status_code, body_diff, response_time, error_stack)。
        只有在明确检测到敏感堆栈泄露、数据库报错注入特征或差分越权时才裁定漏洞。
        """
        status_code = int(response_data.get("status_code") or 0)
        body = str(response_data.get("body") or "")
        error_stack = str(response_data.get("error_stack") or "")
        response_time = float(response_data.get("response_time") or 0.0)

        # 计算响应体差分
        baseline_body = str((baseline_data or {}).get("body") or "")
        body_diff = ""
        if baseline_body and body != baseline_body:
            body_diff = f"bytes_diff: {len(body) - len(baseline_body)}"

        lowered_body = body.lower()
        lowered_stack = error_stack.lower()

        is_vulnerable = False
        vuln_type = None
        reason = "Normal HTTP interaction"

        # 1. 检查数据库报错签名泄露
        for sig in cls.DATABASE_ERROR_SIGNATURES:
            if sig in lowered_body or sig in lowered_stack:
                is_vulnerable = True
                vuln_type = "SQL_ERROR_LEAK"
                reason = f"Database error signature leaked: '{sig}'"
                break

        # 2. 检查敏感异常堆栈泄露
        if not is_vulnerable:
            for sig in cls.STACK_TRACE_SIGNATURES:
                if sig in lowered_body or sig in lowered_stack:
                    is_vulnerable = True
                    vuln_type = "STACK_TRACE_DISCLOSURE"
                    reason = f"Internal framework stack trace leaked: '{sig}'"
                    break

        # 3. AUDIT-05 硬约束：若仅返回 500 或 403 且无敏感数据泄露，严禁判定为漏洞！
        if not is_vulnerable and status_code in (500, 403):
            reason = (
                f"HTTP {status_code} received without sensitive data leakage "
                "(Status Code Sole Reliance Disallowed per AUDIT-05)"
            )

        return {
            "status_code": status_code,
            "body_diff": body_diff,
            "response_time": response_time,
            "error_stack": error_stack,
            "is_vulnerable": is_vulnerable,
            "vulnerability_type": vuln_type,
            "reason": reason,
        }

    def http_raw_request(
        self,
        url: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        data: bytes | str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """执行原始 HTTP 网络发包，收集结构化响应指标。"""
        req_headers = dict(headers or {})
        req_data = data.encode("utf-8") if isinstance(data, str) else data

        req = urllib.request.Request(
            url=url,
            data=req_data,
            headers=req_headers,
            method=method.upper(),
        )

        start_time = time.monotonic()
        status_code = 0
        resp_headers: dict[str, str] = {}
        body = ""
        error_stack = ""

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status_code = response.status
                resp_headers = dict(response.headers.items())
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as http_err:
            status_code = http_err.code
            resp_headers = dict(http_err.headers.items())
            try:
                body = http_err.read().decode("utf-8", errors="replace")
            except Exception:
                body = str(http_err)
            error_stack = str(http_err)
        except Exception as exc:
            status_code = 0
            error_stack = str(exc)

        elapsed = time.monotonic() - start_time
        return {
            "status_code": status_code,
            "headers": resp_headers,
            "body": body,
            "response_time": round(elapsed, 4),
            "error_stack": error_stack,
        }

    def headless_browser_action(
        self,
        url: str,
        actions: Sequence[dict[str, Any]] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """执行 Playwright 无头浏览器动作。受 AUDIT-02 资源池并发限制保护。"""
        with self.browser_slot(timeout=timeout):
            if not self.is_available():
                raise RuntimeError("Playwright is not installed in current environment")

            # 在具备真实 Playwright 环境时运行
            import playwright.sync_api  # type: ignore

            with playwright.sync_api.sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, timeout=int(timeout * 1000))
                title = page.title()
                content = page.content()
                browser.close()
                return {
                    "success": True,
                    "title": title,
                    "content_length": len(content),
                    "url": url,
                }

    def run_dynamic(
        self,
        target: str | Path,
        *,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """执行 Web 动态发包或无头探测。"""
        url_target = str(target)
        if not (url_target.startswith("http://") or url_target.startswith("https://")):
            # 非网络 URL，直接交由静态代码分析
            return self.run_static_degraded(target, payload=payload, context=context, **kwargs)

        method = str((payload or {}).get("method") or "GET")
        headers = (payload or {}).get("headers")
        data = (payload or {}).get("data")
        baseline = (context or {}).get("baseline_response")

        raw_resp = self.http_raw_request(
            url=url_target,
            method=method,
            headers=headers,
            data=data,
        )
        audit_res = self.classify_http_response(raw_resp, baseline_data=baseline)

        findings: list[dict[str, Any]] = []
        if audit_res["is_vulnerable"]:
            findings.append({
                "rule_id": audit_res["vulnerability_type"],
                "severity": "HIGH",
                "message": audit_res["reason"],
                "target": url_target,
                "evidence": {
                    "status_code": audit_res["status_code"],
                    "body_diff": audit_res["body_diff"],
                    "response_time": audit_res["response_time"],
                },
            })

        return {
            "success": True,
            "status": "ok",
            "probe_mode": "dynamic",
            "findings": findings,
            "evidence": {
                "http_response": audit_res,
            },
            "metadata": {
                "url": url_target,
                "method": method,
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
        """AUDIT-04: 优雅降级为静态 Web 资产结构与 DOM XSS 汇聚点审计。"""
        path = Path(target)
        findings: list[dict[str, Any]] = []
        scanned_files = 0

        files_to_scan: list[Path] = []
        if path.is_file():
            files_to_scan.append(path)
        elif path.is_dir():
            for ext in ("*.html", "*.htm", "*.js", "*.jsx", "*.ts", "*.tsx", "*.vue"):
                files_to_scan.extend(path.glob(f"**/{ext}"))

        for file_path in files_to_scan[:100]:  # 限制扫描文件数防卡死
            scanned_files += 1
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for pattern, rule_id in self.DOM_XSS_PATTERNS:
                matches = re.finditer(pattern, content)
                for m in matches:
                    line_no = content[:m.start()].count("\n") + 1
                    findings.append({
                        "rule_id": rule_id,
                        "severity": "HIGH",
                        "file": str(file_path),
                        "line": line_no,
                        "matched": m.group(0),
                        "message": f"Detected potential unescaped DOM XSS sink: {m.group(0)}",
                    })

        return {
            "success": True,
            "status": "ok",
            "probe_mode": "static_degraded",
            "findings": findings,
            "evidence": {
                "scanned_files": scanned_files,
                "findings_count": len(findings),
            },
            "metadata": {
                "target": str(target),
                "degraded_mode": "static_dom_xss_analysis",
            },
        }
