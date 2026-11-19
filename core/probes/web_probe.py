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
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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
        """AUDIT-04: 极速探活 (<5ms，零阻塞)。

        降级契约：布尔返回值受 BaseProbe 布尔契约约束（execute_probe 以真值
        判定是否降级）；(bool, detail) 明细结构见模块级 ``check_availability()``。
        """
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
        *,
        screenshot_path: str | Path | None = None,
        viewport: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """执行 Playwright 动作并返回可复核的动态证据。

        ``actions`` 支持 ``click``、``fill``、``press``、``select_option``、
        ``wait_for``、``wait``、``screenshot`` 七种显式动作。动作失败直接抛出，
        不把未执行的浏览器旅程伪装成成功；``screenshot_path`` 用于保存动作完成后的
        默认截图，调用方应把过程文件放在仓库外的临时目录。

        返回值固定包含页面、DOM、无障碍快照、控制台、失败请求和截图摘要，供
        ``web_conformance.py`` 或外部视觉分析绑定页面、视口、浏览器版本与复现步骤。
        受 AUDIT-02 资源池并发限制保护。
        """
        with self.browser_slot(timeout=timeout):
            if not self.is_available():
                raise RuntimeError("Playwright is not installed in current environment")

            # 在具备真实 Playwright 环境时运行
            import playwright.sync_api  # type: ignore

            with playwright.sync_api.sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    normalized_viewport: dict[str, int] | None = None
                    if viewport is not None:
                        try:
                            normalized_viewport = {
                                "width": int(viewport["width"]),
                                "height": int(viewport["height"]),
                            }
                        except (KeyError, TypeError, ValueError) as exc:
                            raise ValueError(
                                "viewport must contain integer width and height"
                            ) from exc
                        if any(value <= 0 for value in normalized_viewport.values()):
                            raise ValueError("viewport width and height must be positive")

                    page = browser.new_page(viewport=normalized_viewport)
                    return self.capture_browser_page(
                        page, url, actions=actions, timeout=timeout,
                        screenshot_path=screenshot_path, browser_version=browser.version,
                    )
                finally:
                    browser.close()

    @staticmethod
    def capture_browser_page(
        page: Any,
        url: str,
        *,
        browser_version: str,
        actions: Sequence[dict[str, Any]] | None = None,
        timeout: float = 10.0,
        screenshot_path: str | Path | None = None,
        screenshot_full_page: bool = True,
        screenshot_masks: Sequence[str] = (),
        wait_until: str = "load",
        after_navigation: Callable[[Any], None] | None = None,
        inspect_page: Callable[[Any], Mapping[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Capture a caller-owned Playwright page after navigation and explicit actions."""
        console_messages: list[dict[str, Any]] = []
        page_errors: list[str] = []
        request_failures: list[dict[str, Any]] = []
        http_errors: list[dict[str, Any]] = []
        screenshots: list[dict[str, Any]] = []

        page.on(
            "console",
            lambda message: console_messages.append({
                "type": message.type,
                "text": message.text,
                "location": message.location,
            }),
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: request_failures.append({
                "url": request.url,
                "method": request.method,
                "failure": request.failure,
            }),
        )
        page.on(
            "response",
            lambda response: http_errors.append({
                "url": response.url,
                "status": response.status,
                "method": response.request.method,
            }) if response.status >= 400 else None,
        )

        response = page.goto(url, timeout=int(timeout * 1000), wait_until=wait_until)
        if after_navigation is not None:
            after_navigation(page)

        def capture_screenshot(
            target: str | Path,
            *,
            full_page: bool = True,
        ) -> dict[str, Any]:
            path = Path(target).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            masks = [page.locator(f"css={selector}").filter(visible=True)
                     for selector in screenshot_masks]
            mask_counts = {selector: item.count()
                           for selector, item in zip(screenshot_masks, masks, strict=True)}
            image = page.screenshot(path=str(path), full_page=full_page, mask=masks, caret="hide")
            record = {
                "path": str(path),
                "full_page": full_page,
                "sha256": hashlib.sha256(image).hexdigest(),
                "mask_counts": mask_counts,
            }
            screenshots.append(record)
            return record

        applied_actions: list[dict[str, Any]] = []
        for index, action in enumerate(actions or ()):
            if not isinstance(action, Mapping):
                raise TypeError(f"browser action {index} must be an object")
            action_type = str(
                action.get("type") or action.get("action") or ""
            ).strip().lower()
            selector = action.get("selector")
            action_timeout = int(
                float(action.get("timeout", timeout)) * 1000
            )
            if action_timeout <= 0:
                raise ValueError(f"browser action {index} timeout must be positive")

            if action_type == "click":
                if not isinstance(selector, str) or not selector:
                    raise ValueError(f"browser action {index} click requires selector")
                page.locator(selector).click(timeout=action_timeout)
            elif action_type == "fill":
                if not isinstance(selector, str) or not selector:
                    raise ValueError(f"browser action {index} fill requires selector")
                value = action.get("value")
                if value is None:
                    raise ValueError(f"browser action {index} fill requires value")
                page.locator(selector).fill(str(value), timeout=action_timeout)
            elif action_type == "press":
                key = action.get("key")
                if not isinstance(key, str) or not key:
                    raise ValueError(f"browser action {index} press requires key")
                if selector:
                    page.locator(str(selector)).press(key, timeout=action_timeout)
                else:
                    page.keyboard.press(key)
            elif action_type == "select_option":
                if not isinstance(selector, str) or not selector:
                    raise ValueError(
                        f"browser action {index} select_option requires selector"
                    )
                option = action.get("value")
                if option is None:
                    option = action.get("label")
                if option is None:
                    raise ValueError(
                        f"browser action {index} select_option requires value or label"
                    )
                if "label" in action:
                    page.locator(selector).select_option(
                        label=str(action["label"]), timeout=action_timeout
                    )
                else:
                    page.locator(selector).select_option(
                        value=str(option), timeout=action_timeout
                    )
            elif action_type == "wait_for":
                if not isinstance(selector, str) or not selector:
                    raise ValueError(f"browser action {index} wait_for requires selector")
                state = str(action.get("state", "visible"))
                if state not in {"attached", "detached", "visible", "hidden"}:
                    raise ValueError(f"unsupported wait_for state: {state}")
                page.locator(selector).wait_for(
                    state=state, timeout=action_timeout
                )
            elif action_type == "wait":
                duration_ms = int(action.get("ms", 0))
                if duration_ms < 0:
                    raise ValueError(f"browser action {index} wait ms must be non-negative")
                page.wait_for_timeout(duration_ms)
            elif action_type == "screenshot":
                target = action.get("path")
                if not isinstance(target, (str, Path)) or not str(target):
                    raise ValueError(f"browser action {index} screenshot requires path")
                applied_actions.append({
                    "index": index,
                    "type": action_type,
                    "screenshot": capture_screenshot(
                        target, full_page=bool(action.get("full_page", True))
                    ),
                })
                continue
            else:
                raise ValueError(
                    f"unsupported browser action type at index {index}: {action_type!r}"
                )
            action_record: dict[str, Any] = {
                "index": index,
                "type": action_type,
            }
            if selector:
                action_record["selector"] = str(selector)
            if action_type == "press":
                action_record["key"] = str(action["key"])
            if action_type == "fill":
                action_record["value_length"] = len(str(action["value"]))
            applied_actions.append(action_record)

        inspection = inspect_page(page) if inspect_page is not None else None
        if screenshot_path is not None:
            capture_screenshot(screenshot_path, full_page=screenshot_full_page)

        title = page.title()
        content = page.content()
        try:
            aria_snapshot = page.locator("body").aria_snapshot(
                timeout=int(timeout * 1000)
            )
            aria_error = None
        except Exception as exc:
            aria_snapshot = None
            aria_error = str(exc)
        dom_metrics = page.evaluate(
            """
            () => {
              const active = document.activeElement;
              const named = (node) => Boolean(
                node.getAttribute('aria-label') ||
                node.getAttribute('aria-labelledby') ||
                node.getAttribute('title') ||
                (node.textContent || '').trim()
              );
              const focusable = document.querySelectorAll(
                'a[href],button,input,textarea,select,[tabindex]:not([tabindex="-1"])'
              );
              const buttons = Array.from(document.querySelectorAll('button'));
              const rootStyles = getComputedStyle(document.documentElement);
              const customProperties = Array.from(rootStyles)
                .filter((name) => name.startsWith('--'))
                .sort();
              return {
                body_text_length: (document.body?.innerText || '').length,
                focusable_count: focusable.length,
                unnamed_button_count: buttons.filter((button) => !named(button)).length,
                custom_property_count: customProperties.length,
                custom_properties: customProperties,
                horizontal_overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                scroll_width: document.documentElement.scrollWidth,
                client_width: document.documentElement.clientWidth,
                scroll_height: document.documentElement.scrollHeight,
                client_height: document.documentElement.clientHeight,
                active_element: active ? {
                  tag: active.tagName.toLowerCase(),
                  id: active.id || null,
                  role: active.getAttribute('role'),
                } : null,
              };
            }
            """
        )
        return {
            "success": True,
            "title": title,
            "content_length": len(content),
            "url": page.url,
            "http_status": response.status if response is not None else None,
            "browser": {
                "engine": "chromium",
                "version": browser_version,
            },
            "viewport": page.viewport_size,
            "actions": applied_actions,
            **({"inspection": inspection} if inspection is not None else {}),
            "evidence": {
                "dom": {
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "content_length": len(content),
                    "metrics": dom_metrics,
                },
                "accessibility": {
                    "available": aria_snapshot is not None,
                    "snapshot": aria_snapshot,
                    "error": aria_error,
                },
                "console": console_messages,
                "page_errors": page_errors,
                "request_failures": request_failures,
                "http_errors": http_errors,
                "screenshots": screenshots,
            },
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
        """AUDIT-04: 优雅降级为静态 Web 资产结构与 DOM XSS 汇聚点审计。

        降级契约：输出与成功路径（run_dynamic，status="ok"）**可区分**——
        status="degraded" + degraded=True + degraded_reason，绝不伪装成正常动态发包结果。
        """
        path = Path(target)
        findings: list[dict[str, Any]] = []
        scanned_files = 0
        read_errors: list[str] = []

        files_to_scan: list[Path] = []
        if path.is_file():
            files_to_scan.append(path)
        elif path.is_dir():
            for ext in ("*.html", "*.htm", "*.js", "*.jsx", "*.ts", "*.tsx", "*.vue"):
                files_to_scan.extend(path.glob(f"**/{ext}"))

        for file_path in files_to_scan[:100]:  # 限制扫描文件数防卡死
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                read_errors.append(f"{file_path}: {exc}")
                continue
            scanned_files += 1

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

        analyzed = scanned_files > 0
        if analyzed:
            degraded_reason = (
                "playwright_unavailable" if not self.is_available() else "static_mode_forced"
            )
        else:
            degraded_reason = "web_assets_not_found"

        return {
            "success": analyzed,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": degraded_reason,
            "probe_mode": "static_degraded",
            "findings": findings,
            "evidence": {
                "scanned_files": scanned_files,
                "findings_count": len(findings),
                "read_errors": read_errors,
            },
            "metadata": {
                "target": str(target),
                "degraded_mode": "static_dom_xss_analysis",
            },
        }


def check_availability() -> dict[str, Any]:
    """降级契约：Web 审计能力结构化探活明细（(bool, detail) 的结构化形态）。

    机械探测 Playwright CLI / Python 包是否可用（<5ms，零阻塞、零子进程）；
    原始 HTTP 发包模式仅依赖标准库，恒可用。DEGRADED 时浏览器通道必须走
    ``WebDynamicProbe.run_static_degraded``，禁止伪装成动态审计结果。
    """
    cli_path = shutil.which("playwright")
    try:
        import importlib.util

        spec_ready = importlib.util.find_spec("playwright") is not None
    except Exception:
        spec_ready = False
    browser_ready = bool(cli_path or spec_ready)
    checks = [
        {
            "name": "playwright-cli",
            "ok": cli_path is not None,
            "required": False,
            "detail": cli_path or "未找到 playwright CLI",
        },
        {
            "name": "playwright-python",
            "ok": spec_ready,
            "required": True,
            "detail": "playwright 包可导入" if spec_ready else "未安装 playwright Python 包（pip install playwright && playwright install chromium）",
        },
        {
            "name": "raw-http",
            "ok": True,
            "required": False,
            "detail": "标准库 urllib 原始发包通道恒可用",
        },
    ]
    return {
        "name": "web_dynamic_probe",
        "status": "READY" if browser_ready else "DEGRADED",
        "available": browser_ready,
        "detail": (
            "Playwright 无头浏览器通道可用"
            if browser_ready
            else "Playwright 不可用：浏览器动态通道降级为静态 DOM/资产分析（原始 HTTP 发包仍可用）"
        ),
        "checks": checks,
        "capabilities": (
            ["headless_browser", "raw_http", "browser_instance_pool"]
            if browser_ready
            else ["raw_http"]
        ),
        "fallbacks": {} if browser_ready else {
            "audit": "WebDynamicProbe.run_static_degraded（静态 DOM XSS 汇聚点分析）",
        },
        "warnings": [],
    }
