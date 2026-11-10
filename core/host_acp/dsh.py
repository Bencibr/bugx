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

from .base import AcpPlugin

__all__ = ["DshAcpPlugin", "PLUGIN"]

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
