#!/usr/bin/env python3
"""core/hosts/base_usage.py - 宿主本地用量收集的基础模型与可复用协议。

背景：
各个宿主（ZCode, DSH, Claude Code, Codex, Cline, OpenCode, Pi 等）记录本地调用
日志的载体各不相同（SQLite, zstd-jsonl, json, jsonl, vscdb 等），但在 BugX
的生命周期（lifecycle）报告中，对用量核验的需求是高度一致的：
1. 调度侧 Token（主上下文交互 Token 增量）；
2. Worker 会话 Token（并发子 Agent/Task 消费增量）；
3. 逐模型维度聚合（调用次数 calls、输入 input_tokens、输出 output_tokens、
   缓存命中 cache_read_tokens、思考 reasoning_tokens、总计 total_tokens、耗时 duration_ms）；
4. 调用耗时汇总；
5. 详细审计账本落盘到当前 lifecycle 的 ``state/<state_filename>``，不上云。

本模块提供高内聚、无外部依赖的通用数据结构与基类：
- ``ModelUsageEntry``：单模型聚合记录；
- ``LifecycleUsageSummary``：生命周期窗口内的汇总数据契约；
- ``BaseHostUsageCollector``：抽象采集基类；
- ``format_duration_ms``：统一的时长格式化（"1h 2m 3s" / "15s"）。
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import time
from typing import Any, Mapping

__all__ = [
    "BaseHostUsageCollector",
    "HostUsageError",
    "LifecycleUsageSummary",
    "ModelUsageEntry",
    "format_duration_ms",
]


class HostUsageError(RuntimeError):
    """宿主本地用量采集异常（调用方统一静默捕获并降级，不阻断 lifecycle 终态）。"""


def format_duration_ms(ms: int) -> str:
    """统一将毫秒转换为宿主报告易读格式（如 1h 23m 45s、12m 34s 或 45s）。"""
    seconds = max(0, int(ms / 1000))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@dataclass
class ModelUsageEntry:
    """单个模型的调用量与 Token 聚合项。"""

    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0

    def add(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        reasoning_tokens: int = 0,
        total_tokens: int = 0,
        duration_ms: int = 0,
    ) -> None:
        self.calls += 1
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        self.cache_read_tokens += max(0, cache_read_tokens)
        self.reasoning_tokens += max(0, reasoning_tokens)
        self.total_tokens += max(0, total_tokens)
        self.duration_ms += max(0, duration_ms)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LifecycleUsageSummary:
    """生命周期内的统一用量报告。"""

    scheduler_tokens: int = 0
    worker_sessions_tokens: int = 0
    models: list[dict[str, Any]] = field(default_factory=list)
    duration_ms_total: int = 0
    detail_path: str | None = None
    worker_sessions: list[dict[str, Any]] = field(default_factory=list)
    main_sessions: list[dict[str, Any]] = field(default_factory=list)

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "scheduler_tokens": self.scheduler_tokens,
            "worker_sessions_tokens": self.worker_sessions_tokens,
            "models": self.models,
            "duration_ms_total": self.duration_ms_total,
            "detail_path": self.detail_path,
            "worker_sessions": self.worker_sessions,
            "main_sessions": self.main_sessions,
            "main_session_count": len(self.main_sessions),
            "worker_session_count": len(self.worker_sessions),
        }


class BaseHostUsageCollector(abc.ABC):
    """宿主本地用量收集器基类。"""

    host_name: str = ""
    state_filename: str = ""

    def __init__(self, host_name: str, state_filename: str = "") -> None:
        self.host_name = host_name
        self.state_filename = state_filename or f"{host_name}-usage.json"

    @abc.abstractmethod
    def collect(
        self,
        project_root: Path,
        runtime: Mapping[str, Any],
        **kwargs: Any,
    ) -> LifecycleUsageSummary:
        """执行本地读取并返回统一生命周期汇总。"""

    def persist_detail(
        self,
        project_root: Path,
        detail_data: Mapping[str, Any],
    ) -> Path | None:
        """将明细落盘至生命周期的 state/<state_filename>（0600 权限），失败时安全降级。"""
        try:
            import core.bugx_paths as bugx_paths
            import core.secure_atomic as secure_atomic

            scope = bugx_paths.require(project_root)
            detail_path = scope.state_root / self.state_filename
            detail_path.parent.mkdir(parents=True, exist_ok=True)
            secure_atomic.atomic_write_json(detail_path, dict(detail_data), mode=0o600)
            return detail_path
        except Exception:
            return None
