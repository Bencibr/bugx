#!/usr/bin/env python3
"""core/host_acp/pi.py - Pi 的 ACP 状态声明（宿主产品无 ACP 面）。

事实（2026-10-28 实测，``pi 0.85.1``）：

* 顶层 ``pi --help`` 的子命令只有 ``install`` / ``remove`` / ``uninstall`` /
  ``update`` / ``list`` / ``config`` / ``auth``，**没有** ``acp``，也没有 ``--acp`` flag；
* 因此本插件声明 ``status = "unavailable"``：这不是「本仓还没适配」，而是
  **上游没有该协议面**。探针据此报 ``BLOCKED(adapter_status=unavailable)``，并把
  Pi 的受支持通道指向无头 ``pi -p`` 与标准技能/扩展机制（见
  ``core/hosts/pi.py`` 的 ``engine_run`` 面）。
"""

from __future__ import annotations

from .base import AcpPlugin

__all__ = ["PiAcpPlugin", "PLUGIN"]

#: 宿主名由模块名派生（``core/host_acp/<host>.py``），实现体不写字面量。
_HOST_NAME = __name__.rsplit(".", 1)[-1]


class PiAcpPlugin(AcpPlugin):
    """Pi 的 ACP 面：上游不存在，显式声明为 ``unavailable``。"""

    name = _HOST_NAME
    executable = _HOST_NAME
    status = "unavailable"
    reason = (
        "pi 0.85.1 的 --help 无 acp 子命令/--acp flag，"
        "宿主产品本身没有 ACP 入口；受支持通道是无头 pi -p 与 Pi 扩展/技能机制"
    )


PLUGIN = PiAcpPlugin
