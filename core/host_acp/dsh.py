#!/usr/bin/env python3
"""core/host_acp/dsh.py - DeepSeek Harness (dsh) 的 ACP 状态声明（无 ACP 面）。

事实（2026-10-28 真机取证，``dsh 0.1.1-rc.2``，npm 全局包
``@deepseek-ai/dsh``）：

* 顶层 ``dsh --help`` 的子命令只有 ``web`` / ``plugin``，launcher flag 只有
  ``--profile`` / ``--patch`` / ``--dump-config`` / ``--dump-default-config``，
  **没有** ``acp`` 子命令，也没有 ``--acp`` flag；
* ``dsh --profile headless --help`` 只有 ``-h/--help``；
  ``dsh --profile web --help`` 只有 ``--host`` / ``--no-open`` / ``--port`` /
  ``--trusted-host``（浏览器 UI 服务，不是协议面）；
* 安装包内 ``lib/``、``package.json`` 与 ``README.md`` 中 grep
  ``acp`` / ``agent-client-protocol`` **零命中**。

因此声明 ``status = "unavailable"``：这不是「本仓还没适配」，而是**上游产品没有
该协议面**。dsh 的受支持通道是无头 ``dsh --profile headless <task>``
（argv 与实测见 ``core/hosts/dsh.py`` 的 ``engine_run`` 面）。

上游一旦发布 ACP 入口：把 ``status`` 改成 ``ready`` 并实现 ``acp_argv`` 即可，
探针代码一行都不用改。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import AcpPlugin

__all__ = ["DshAcpPlugin", "PLUGIN", "is_available", "run_static_degraded"]

#: 宿主名由模块名派生（``core/host_acp/<host>.py``），实现体不写字面量。
_HOST_NAME = __name__.rsplit(".", 1)[-1]


class DshAcpPlugin(AcpPlugin):
    """dsh 的 ACP 面：上游不存在，显式声明为 ``unavailable``。"""

    name = _HOST_NAME
    executable = _HOST_NAME
    status = "unavailable"
    #: 原因进探针报告；带版本号与实测入口，便于日后判断「上游是否已变」。
    reason = (
        "dsh 0.1.1-rc.2 的 --help 无 acp 子命令/--acp flag，lib/pkg/README 中 "
        "agent-client-protocol 零命中；宿主产品本身没有 ACP 入口；"
        "受支持通道是无头 dsh --profile headless <task>"
    )


PLUGIN = DshAcpPlugin


def is_available() -> tuple[bool, str]:
    """降级契约：机械探活宿主 CLI 是否安装（零子进程 ``shutil.which``）。

    ACP 适配器探活语义：宿主 CLI 缺失时本适配器无法拉起任何真实 ACP 会话，
    调用方必须走 :func:`run_static_degraded` 输出静态适配器事实声明；真实 ACP
    可达性（仅 ``acp_status == "ready"`` 的宿主适用）归宿主探活链路验证，
    本函数不发任何子进程。
    """
    path = shutil.which(PLUGIN.executable)
    if path is None:
        return False, (
            f"{PLUGIN.executable} 可执行文件未安装；ACP 适配器无法拉起真实会话"
        )
    return True, path


def run_static_degraded(root: Path | None = None) -> dict:
    """宿主 CLI 缺失/降级时的静态输出：只声明适配器事实，绝不伪装 ACP 会话。

    与真实 ACP 探活输出字段级可区分：``status="degraded"`` + ``degraded=True``
    + ``degraded_reason``；``acp_status`` 保持上游机读声明（``unavailable`` =
    宿主产品没有 ACP 协议面，非本仓欠适配），MCP 健康显式 ``unknown``
    （真实可达性归宿主探活链路，本函数不探 MCP、不发子进程）。
    """
    available, availability_detail = is_available()
    plugin = PLUGIN()
    return {
        "adapter": f"core/host_acp/{plugin.name}.py",
        "plugin": plugin.name,
        "schema_version": 1,
        "success": available,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": (
            "static_facts_only" if available else f"{plugin.name}_cli_not_installed"
        ),
        "probe_mode": "static_degraded",
        "acp_status": plugin.status,
        "acp_supported": plugin.supports_acp(),
        "acp_reason": plugin.reason or None,
        "executable": plugin.executable,
        "mcp_health": "unknown",
        "findings": [],
        "evidence": {
            "availability_detail": availability_detail,
            "project_root": str(root) if root else None,
        },
        "metadata": {
            "degraded_mode": "static_adapter_facts_only",
            "mcp_probing": "deferred_to_host_probe_chain",
            "fallback": "宿主 CLI 缺失时由外层宿主/用户决策接管；BugX 不代装宿主 CLI",
        },
    }
