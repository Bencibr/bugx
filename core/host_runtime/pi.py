#!/usr/bin/env python3
"""core/host_runtime/pi.py - Pi 宿主 Worker 预算透传执行器。

**实现体**，不是声明点：预算基数（600s）声明在 ``core/hosts/pi.py`` 的
``WorkerRuntimeFacet.worker_timeout_seconds``；本模块只把它接到引擎默认执行器上。

集成验证实测根因（2026-11-17）：pi 此前无 ``runner_module``，Worker 由引擎默认
执行器 ``worker_runtime._execute_autonomous_worker`` 驱动，模型 CLI 超时回落
tier 阶梯（recon=180s）。codex 模型在 checkpoint 准备阶段超过 180s 即被
``run_model`` 击杀（与 E1 cline FAIL 同根因，build.566 只覆盖了 cline）。

设计边界：

* **不复制审计逻辑**：心跳保活、artifact、差分验证、checkpoint、UCB1 记账
  仍由 ``worker_runtime._execute_autonomous_worker`` 独家承载；本模块只注入
  预算基数。
* **注入通道**是 ``resolve_worker_timeout`` 的设计扩展点 ``BUGX_WORKER_TIMEOUT``
  环境变量（``core/supervisor/worker_runtime.py``：无 ``base_override`` 时以其
  为基准基数）。预算只在委托调用窗口内生效：入口保存、``finally`` 恢复，
  异常路径同样恢复；窗口外进程 env 与调用前逐字节一致。
* **语义与 cline 的 ``base_override`` 逐字节一致**：``600 * 1.5**(attempt-1)``
  重试退避保留、exploit 保底 600s、``[10s, 1800s]`` 钳制；profile 预算优先于
  用户全局 ``BUGX_WORKER_TIMEOUT``（宿主事实比全局 env 更具体，字段契约见
  ``core/hosts/base.py``）——注入窗口内 env 即 profile 值。
* **并发安全**：pi 声明 ``mode="serial"`` 且 ``max_concurrency=1``，进程池逃逸口
  的单批并发上限也取 ``worker_runtime.max_concurrency``（即 1）；引擎进程单宿主
  会话语义保证窗口内不存在其他 ``resolve_worker_timeout`` 并发调用者。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import core.hosts as host_plugins

__all__ = ["execute_worker", "profile_budget_base"]

#: 引擎默认执行器的预算基准环境变量（``resolve_worker_timeout`` 扩展点）。
_TIMEOUT_ENV = "BUGX_WORKER_TIMEOUT"

#: 宿主名由模块名派生（``core/host_runtime/<host>.py``），实现体不写字面量。
_HOST_NAME = __name__.rsplit(".", 1)[-1]
_PLUGIN = host_plugins.get(_HOST_NAME)


def profile_budget_base() -> float | None:
    """宿主 profile 预算基数（``worker_timeout_seconds``）的正数规范化。

    与 ``core/host_runtime/cline.py`` 的 ``base_override`` 归一化同规则：
    非 int/float、bool、非正数一律回 ``None``（维持引擎 tier 阶梯既有行为）。
    """
    budget = getattr(_PLUGIN.worker_runtime, "worker_timeout_seconds", None)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        return None
    budget = float(budget)
    return budget if budget > 0 else None


def execute_worker(
    *,
    workspace_root: Path,
    claim: Any,
    task_spec: str,
    supervisor: Any = None,
    stage_name: str = "00-scope",
    executable: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """引擎统一 Worker 入口（``worker_runtime._host_runner`` 按名调用）。

    延迟导入并直接委托引擎默认执行器：公有
    ``worker_runtime.execute_autonomous_worker`` 会重新走 ``_host_runner``
    分派回本模块（无限递归），而模块内私有执行器正是 pi 无 runner 时代的
    唯一驱动路径——审计行为零漂移，本模块只叠加预算注入。
    """
    from core.supervisor.worker_runtime import _execute_autonomous_worker

    kwargs: dict[str, Any] = dict(
        workspace_root=workspace_root,
        claim=claim,
        task_spec=task_spec,
        stage_name=stage_name,
        provider=provider,
        executable=executable,
        supervisor=supervisor,
        scheduler=None,
    )
    budget = profile_budget_base()
    if budget is None:
        return _execute_autonomous_worker(**kwargs)
    saved = os.environ.get(_TIMEOUT_ENV)
    os.environ[_TIMEOUT_ENV] = str(budget)
    try:
        return _execute_autonomous_worker(**kwargs)
    finally:
        if saved is None:
            os.environ.pop(_TIMEOUT_ENV, None)
        else:
            os.environ[_TIMEOUT_ENV] = saved
