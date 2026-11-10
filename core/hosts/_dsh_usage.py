#!/usr/bin/env python3
"""_dsh_usage.py — **DeepSeek Harness (DSH) 宿主专属**的本地 Token 与模型用量收集器（只读）。

专属性边界：本模块只服务于 runtime.host 为 DSH 的会话。
读取 DSH Desktop 的 Session 日志归档（`~/Library/Application Support/dsh-desktop/harness/sessions/` 或 `~/.dsh/sessions/`）。
按被审计项目根（精确匹配 `--Users-...--` 目录）定位 session.v4.jsonl.zstd / session.jsonl.zstd，提取当前 lifecycle 时间窗内的模型调用、Token 消耗与时长。
输出同 _zcode_usage：仅进本地运行态（runtime 报告行 + lifecycle state/dsh-usage.json 明细），绝不上云。

宿主插件层只做纯解析与路径推导：解压 `.zstd` 依赖外部进程，由引擎侧注入 `read_lines`
（宿主模块不得 import subprocess/shutil，见 tests/test_host_plugins.py 的依赖方向门禁）。
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping

__all__ = ["DshUsageError", "collect_lifecycle_usage"]

class DshUsageError(RuntimeError):
    """DSH 本地用量读取失败。"""

def _session_roots() -> list[Path]:
    home = Path.home()
    candidates = [
        home / "Library" / "Application Support" / "dsh-desktop" / "harness" / "sessions",
        home / ".dsh" / "sessions",
    ]
    return [p for p in candidates if p.is_dir()]

def _dir_to_session_key(project_root: Path) -> str:
    raw = str(project_root.resolve()).strip("/").replace("/", "-")
    return f"--{raw}--"

def _session_files(project_root: Path) -> list[Path]:
    """该项目根对应的全部 DSH 会话归档（找不到时抛 DshUsageError）。"""
    session_roots = _session_roots()
    if not session_roots:
        raise DshUsageError("未找到 DSH sessions 目录")

    target_key = _dir_to_session_key(project_root)
    matched_dirs: list[Path] = []
    for s_root in session_roots:
        exact = s_root / target_key
        if exact.is_dir():
            matched_dirs.append(exact)
        else:
            # 兼容软链接或不同解析路径
            for child in s_root.iterdir():
                if child.is_dir() and child.name == target_key:
                    matched_dirs.append(child)

    if not matched_dirs:
        raise DshUsageError(f"未找到项目 {project_root} 的 DSH 会话目录（key: {target_key}）")

    session_files: list[Path] = []
    for d in matched_dirs:
        for f in d.rglob("*.zstd"):
            if f.is_file():
                session_files.append(f)

    if not session_files:
        raise DshUsageError(f"在 {matched_dirs} 下未找到任何 .zstd 会话日志")
    return session_files

def collect_lifecycle_usage(
    project_root: Path | str,
    runtime: Mapping[str, Any],
    *,
    read_lines: Callable[[Path], Iterable[str]] | None = None,
) -> dict[str, Any]:
    """解析 DSH 会话归档，返回本地用量明细。

    read_lines 由引擎侧提供（解压 + 逐行读取）；缺失时视为无法读取——
    宿主插件层不自行启动进程。
    """
    root = Path(project_root).resolve()
    started_epoch = runtime.get("started_epoch")
    if started_epoch is None or started_epoch <= 0:
        raise DshUsageError("runtime 缺少合法的 started_epoch 标记")
    since_ms = int(float(started_epoch) * 1000)
    until = runtime.get("stopped_epoch") or time.time()
    until_ms = int(float(until) * 1000)

    if read_lines is None:
        raise DshUsageError("缺少引擎侧注入的 read_lines，无法解压 DSH session 日志")

    session_files = _session_files(root)

    models_map: dict[str, dict[str, Any]] = {}
    main_tokens = 0
    worker_tokens = 0
    total_duration_ms = 0

    for s_file in session_files:
        try:
            for line in read_lines(s_file):
                line = line.strip()
                if not line or not line.startswith('{"type":"assistant/message"'):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                ts = int(obj.get("time") or 0)
                if ts < since_ms or ts > until_ms:
                    continue

                data = obj.get("data", {})
                usage = data.get("usage", {})
                msg = data.get("message", {})
                source = msg.get("source", {})
                model = source.get("model") or source.get("responseModel") or "default"

                inp = int(usage.get("inputTokens") or 0)
                outp = int(usage.get("outputTokens") or 0)
                total = int(usage.get("totalTokens") or 0)
                cache_read = int(usage.get("cacheReadTokens") or 0)
                # DSH 流式计费时间
                stream = data.get("stream", [])
                duration_ms = 0
                if len(stream) >= 2:
                    t0 = stream[0].get("time") or stream[0].get("time0")
                    t1 = stream[-1].get("time")
                    if t0 and t1 and t1 >= t0:
                        duration_ms = int(t1 - t0)

                total_duration_ms += duration_ms
                main_tokens += total

                if model not in models_map:
                    models_map[model] = {
                        "model": model,
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "total_tokens": 0,
                        "duration_ms": 0,
                    }
                m_entry = models_map[model]
                m_entry["calls"] += 1
                m_entry["input_tokens"] += inp
                m_entry["output_tokens"] += outp
                m_entry["cache_read_tokens"] += cache_read
                m_entry["total_tokens"] += total
                m_entry["duration_ms"] += duration_ms
        except Exception:
            # 单个归档解压/读取失败不影响其余归档
            continue

    models_list = sorted(models_map.values(), key=lambda x: x["total_tokens"], reverse=True)

    detail_path: Path | None = None
    try:
        import core.bugx_paths as bugx_paths
        import core.secure_atomic as secure_atomic

        scope = bugx_paths.require(root)
        detail_path = scope.state_root / "dsh-usage.json"
        detail = {
            "schema_version": 1,
            "project_root": str(root),
            "window": {"since_epoch": started_epoch, "until_epoch": until},
            "models": models_list,
            "duration_ms_total": total_duration_ms,
            "main_tokens": main_tokens,
            "worker_tokens": worker_tokens,
        }
        detail_path.parent.mkdir(parents=True, exist_ok=True)
        secure_atomic.atomic_write_json(detail_path, detail)
    except Exception:
        pass

    return {
        "scheduler_tokens": main_tokens,
        "worker_sessions_tokens": worker_tokens,
        "models": models_list,
        "duration_ms_total": total_duration_ms,
        "detail_path": str(detail_path) if detail_path else None,
    }
