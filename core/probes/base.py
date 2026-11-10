#!/usr/bin/env python3
"""core/probes/base.py - 领域形态专属黑盒探针基类与优雅降级契约 (DOC-248)。

落实 AUDIT-04：
  - 极速前置探活 is_available() -> bool (<5ms，零阻塞，无子进程发散)；
  - 统一 execute_probe(...) 签名与结构化响应契约；
  - 环境缺失（无 adb / 无真机 / 无微信 CLI / 无 Playwright）时，
    自动优雅降级为静态结构分析 (probe_mode="static_degraded")，
    绝不向调用方抛出未捕获异常导致崩溃。
"""

from __future__ import annotations

import abc
import os
import shutil
import time
from pathlib import Path
from typing import Any, Mapping


class BaseProbe(abc.ABC):
    """领域形态专属黑盒探针抽象基类。"""

    @property
    @abc.abstractmethod
    def probe_name(self) -> str:
        """探针唯一标识。"""
        ...

    @abc.abstractmethod
    def is_available(self) -> bool:
        """极速前置探活 (<5ms，零阻塞，如 shutil.which)。

        返回当前宿主环境是否具备执行真实动态交互的二进制或运行时依赖。
        必须保证低延迟，严禁在此执行耗时子命令或全盘扫描。
        """
        ...

    @abc.abstractmethod
    def run_dynamic(
        self,
        target: str | Path,
        *,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """执行黑盒/动态真实探针动作。若动态执行失败抛出异常，将触发降级。"""
        ...

    @abc.abstractmethod
    def run_static_degraded(
        self,
        target: str | Path,
        *,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """执行静态结构与反编译语义验证（优雅降级模式，AUDIT-04）。"""
        ...

    def execute_probe(
        self,
        target: str | Path,
        *,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        force_mode: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """统一探针入口。

        具备防御性自愈与自动降级保障 (AUDIT-04)：
        - 若 force_mode == "static_degraded" 或环境探活未就绪，直接执行静态降级；
        - 若动态环境就绪但动态运行时报错，自动兜底降级至静态结构分析；
        - 捕获任何非预期异常，保证返回标准结构字典且绝不崩溃。
        """
        start_time = time.monotonic()
        try:
            if force_mode == "static_degraded":
                result = self.run_static_degraded(
                    target, payload=payload, context=context, **kwargs
                )
                result["probe_mode"] = "static_degraded"
            elif force_mode == "dynamic":
                if not self.is_available():
                    result = self.run_static_degraded(
                        target, payload=payload, context=context, **kwargs
                    )
                    result["probe_mode"] = "static_degraded"
                    metadata = result.setdefault("metadata", {})
                    metadata["degraded_reason"] = "dynamic_environment_unavailable"
                else:
                    result = self.run_dynamic(
                        target, payload=payload, context=context, **kwargs
                    )
                    result.setdefault("probe_mode", "dynamic")
            else:
                # 默认自动检测与自愈降级
                if self.is_available():
                    try:
                        result = self.run_dynamic(
                            target, payload=payload, context=context, **kwargs
                        )
                        result.setdefault("probe_mode", "dynamic")
                    except Exception as dynamic_exc:
                        result = self.run_static_degraded(
                            target, payload=payload, context=context, **kwargs
                        )
                        result["probe_mode"] = "static_degraded"
                        metadata = result.setdefault("metadata", {})
                        metadata["dynamic_error"] = str(dynamic_exc)
                        metadata["degraded_reason"] = "dynamic_execution_failed"
                else:
                    result = self.run_static_degraded(
                        target, payload=payload, context=context, **kwargs
                    )
                    result["probe_mode"] = "static_degraded"
                    metadata = result.setdefault("metadata", {})
                    metadata["degraded_reason"] = "dynamic_environment_unavailable"
        except Exception as exc:
            # 绝对安全兜底：严禁未捕获异常崩溃上层 Worker (AUDIT-04)
            result = {
                "success": False,
                "status": "degraded_error",
                "probe_name": self.probe_name,
                "probe_mode": "static_degraded",
                "findings": [],
                "evidence": {},
                "metadata": {
                    "exception": str(exc),
                    "exception_type": exc.__class__.__name__,
                    "degraded_reason": "unhandled_exception_caught",
                },
            }

        elapsed_ms = (time.monotonic() - start_time) * 1000.0
        result.setdefault("probe_name", self.probe_name)
        result.setdefault("duration_ms", round(elapsed_ms, 2))
        result.setdefault("success", True)
        result.setdefault("status", "ok")
        result.setdefault("findings", [])
        result.setdefault("evidence", {})
        result.setdefault("metadata", {})
        return result
