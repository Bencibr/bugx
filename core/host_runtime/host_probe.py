#!/usr/bin/env python3
"""宿主进程祖先链探测（唯一权威来源，**不依赖宿主适配器层**）。

为什么独立成模块（架构收敛，2026-10-26）：``host_registry`` 与 ``supervisor_host``
此前互为依赖——``host_registry`` 在探测分支里 `import supervisor_host`，而
``supervisor_host`` 顶层 `import host_registry`。这类双向依赖把「模块依赖方向」从
静态可分析变成运行期才发现（`tests/test_import_graph_acyclic.py` 明确禁止）。

探测原语本身只依赖 ``startup_environment``（进程祖先链 + 可执行段识别），与
宿主适配器注册表无关，因此可以下沉到本模块：

    host_registry ──┐
                    ├──> host_probe ──> startup_environment
    supervisor_host ┘

结论仍遵守 PE-20261004-004：宿主身份只允许来自真实进程证据（``source ==
"process-tree"``），探查失败返回 ``None``，**不猜、不兜底、不受环境变量覆盖**。
"""

from __future__ import annotations


def probe_active_host() -> str | None:
    """主动探查当前进程祖先链所属宿主；探查失败返回 ``None``。

    复用 ``startup_environment`` 的 ``process_ancestry`` / ``_host_from_commands``
    （前者带进程内缓存，后者只认可执行段，天然防提示词数据伪装），并要求
    ``source == "process-tree"``——探查结论必须来自真实进程证据。
    """

    # 单一导入路径；``ImportError`` 只表示「精简安装里没有该模块」这一种
    # 合法情形，返回 ``None`` 而不是猜宿主名。**不要**再写第二套扁平名重试。
    try:
        from core.runtime import startup_environment as se
    except ImportError:  # pragma: no cover - 精简安装下无该模块
        return None
    try:
        host, source, _ = se._host_from_commands(se.process_ancestry())
    except Exception:  # noqa: BLE001 - 探查失败按 None 处理，禁止猜测
        return None
    return host if (host and source == "process-tree") else None
