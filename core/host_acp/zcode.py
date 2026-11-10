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

from .base import AcpPlugin

__all__ = ["ZCodeAcpPlugin", "PLUGIN"]

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
