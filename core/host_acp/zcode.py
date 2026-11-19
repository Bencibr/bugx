#!/usr/bin/env python3
"""core/host_acp/zcode.py - ZCode 的 ACP 状态声明（宿主产品无标准 ACP 面）。

事实来源（2026-10-28 实测，``zcode 0.17.0`` / Electron Desktop ``3.16.3``）：

* 桌面端内置 CLI 核心（``Contents/Resources/glm/zcode.cjs``）虽然提供 ``app-server`` 子命令，
  但其传输的是**专有 ZCode Protocol**（严格 Zod 契约：``runtime/capabilities``、
  ``session/create``、``session/send``、``session/stop``，且对请求消息执行严格校验，
  显式拒绝 JSON-RPC 2.0 的 ``jsonrpc`` 字段）；
* 上游**没有标准 ACP（Agent Client Protocol）面**（无标准 ``initialize`` /
  ``session/new`` / ``session/prompt`` 协议接口）；
* 因此本插件显式声明 ``status = "unavailable"``：这不是本仓缺失适配，而是上游宿主
  协议事实；探针如实报 ``BLOCKED(adapter_status=unavailable)``。
* ZCode 的受支持通道是内嵌/全局无头 CLI（``-p <prompt> --mode yolo``）与智能体注册面
  （见 ``core/hosts/zcode.py`` 的 ``engine_run`` 面与 ``install`` 面）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import AcpPlugin

__all__ = ["ZCodeAcpPlugin", "PLUGIN", "is_available", "run_static_degraded"]

#: 宿主名由模块名派生（``core/host_acp/<host>.py``），实现体不写字面量。
_HOST_NAME = __name__.rsplit(".", 1)[-1]


class ZCodeAcpPlugin(AcpPlugin):
    """ZCode 的 ACP 面：上游运行专有 ZCode Protocol 而非 ACP，显式声明为 ``unavailable``。"""

    name = _HOST_NAME
    executable = _HOST_NAME
    status = "unavailable"
    reason = (
        "zcode 0.17.0 内置 app-server 运行专有 ZCode Protocol（Strict JSON Schema，"
        "无标准 ACP initialize/session/prompt 接口）；受支持通道是内嵌/全局 zcode CLI"
        "（-p/--mode yolo）及智能体扩展注册面"
    )


PLUGIN = ZCodeAcpPlugin


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
