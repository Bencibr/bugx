#!/usr/bin/env python3
"""Provider 限流窗口的持久化（审计 #35：窗内不得重跑宿主自治）。

为什么需要这个模块：`supervisor_host.run_host_autonomous` 已经会把分类器返回的
**结构化** `retry_window_seconds` 放进结果体（`supervisor_host.py:2799-2809`），
但 CLI（`bugx autonomous` / `bugx goal`）只打印结果、**不落盘** ⇒ 进程退出后
"窗内不得重跑"的承诺消失，下一次调用会立刻再撞一次 429。

本模块把窗口落成 `.bugx/state/provider-window.json`（原子写、读时容错），
让"限流窗口"跨越进程边界成立：

    record(root, seconds)  -> 写入 retry_at = now + seconds
    active(root)           -> 返回未过期窗口（含 remaining_seconds），过期即 None
    clear(root)            -> 运维显式解除

判据是**单调的墙钟时间**，不依赖进程内状态；文件损坏/缺失一律按"无窗口"处理
（fail-open 到"允许重跑"），因为误阻断比多撞一次 429 代价更高；但如果文件里
写着未来时间且格式合法，则必须以它为准（窗内阻断）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

WINDOW_FILENAME = "provider-window.json"


def _window_path(root: Path | str) -> Path:
    return Path(root) / ".bugx" / "state" / WINDOW_FILENAME


def record(
    root: Path | str, seconds: object, *, reason: str = "",
) -> dict[str, object] | None:
    """记录一个限流窗口；``seconds`` 非正数或不合法时返回 ``None``（不写盘）。"""

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    seconds_value = float(seconds)
    if seconds_value <= 0:
        return None
    now = time.time()
    payload: dict[str, object] = {
        "retry_at": now + seconds_value,
        "seconds": seconds_value,
        "recorded_at": now,
        "reason": str(reason or ""),
    }
    path = _window_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return None
    return payload


def active(root: Path | str, *, now: float | None = None) -> dict[str, object] | None:
    """返回**未过期**的窗口；无窗口/已过期/文件损坏一律返回 ``None``。"""

    path = _window_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    retry_at = payload.get("retry_at")
    if isinstance(retry_at, bool) or not isinstance(retry_at, (int, float)):
        return None
    current = time.time() if now is None else float(now)
    if float(retry_at) <= current:
        return None
    return {
        "retry_at": float(retry_at),
        "remaining_seconds": round(float(retry_at) - current, 3),
        "reason": str(payload.get("reason") or ""),
    }


def clear(root: Path | str) -> bool:
    """显式解除窗口；文件不存在也返回 ``True``（幂等）。"""

    try:
        _window_path(root).unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True
