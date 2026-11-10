"""Engine-side local usage integration, report projection and offline export.

Host-specific paths and parsers belong to the registered UsageFacet. Collection
does not authorize any audit action and never changes a cloud payload.
"""
from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import importlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping


def collect_for_report(
    root: Path, runtime: Mapping[str, Any], *,
    reader_factory: Callable[[], Any] | None = None,
    collector_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    import core.hosts as hosts

    plugin = hosts.get(str(runtime.get("host") or "unknown"))
    facet = plugin.usage if plugin else None
    if facet is None:
        return None
    try:
        module = importlib.import_module("core.hosts." + facet.module)
        kwargs = dict(collector_kwargs or {})
        if facet.reader_format == "zstd":
            reader = reader_factory() if reader_factory is not None else None
            if reader is None:
                raise RuntimeError("Required local archive reader is unavailable")
            kwargs["read_lines"] = reader
        usage = module.collect_lifecycle_usage(root, runtime, **kwargs)
        if not isinstance(usage, dict):
            raise TypeError("Usage collector did not return an object")
        return {"host": plugin.name, "measurement_status": "measured", **usage}
    except Exception as exc:
        # Accounting is ancillary to lifecycle safety. Preserve the failure as
        # data in the report instead of silently displaying an invented zero.
        return {"host": plugin.name, "measurement_status": "unavailable",
                "scheduler_tokens": None, "worker_sessions_tokens": None,
                "models": [], "duration_ms_total": None, "detail_path": None,
                "warnings": [{"reason": str(exc)}]}


def report_lines(usage: Any) -> list[str]:
    if not isinstance(usage, dict):
        return []
    from core.hosts._base_usage import format_duration_ms

    def count(value):
        return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "unmeasured"

    def duration(value):
        return format_duration_ms(value) if isinstance(value, int) else "unmeasured"

    lines = [
        f"- 宿主用量状态: `{usage.get('measurement_status', 'unmeasured')}`（本地记录，不是账单）",
        f"- 调度侧 Token（本地宿主采集）: `{count(usage.get('scheduler_tokens'))}`",
        f"- 原生子 Agent Token（宿主本地采集）: `{count(usage.get('worker_sessions_tokens'))}`",
    ]
    for model in usage.get("models") or []:
        lines.append(
            f"- 模型用量: `{model['model']}` — {count(model.get('calls'))} 次 / "
            f"Input {count(model.get('input_tokens'))} / Output {count(model.get('output_tokens'))} / "
            f"CacheRead {count(model.get('cache_read_tokens'))} / Reasoning {count(model.get('reasoning_tokens'))} / "
            f"Total {count(model.get('total_tokens'))} tok / 时长 {duration(model.get('duration_ms'))}"
        )
    lines.append(f"- 模型调用时长汇总: {duration(usage.get('duration_ms_total'))}")
    if usage.get("detail_path"):
        lines.append(f"- 宿主用量本地明细: `{usage['detail_path']}`")
    if usage.get("attribution_scope"):
        lines.append(f"- 用量归属范围: `{usage['attribution_scope']}`")
    if usage.get("warnings") or usage.get("measurement_status") != "measured":
        lines.append("- 用量边界: 部分记录缺失或不可用；未测量项不计为零，详见本地明细/状态。")
        for warning in usage.get("warnings") or []:
            reason = warning.get("reason") if isinstance(warning, dict) else str(warning)
            lines.append(f"- 用量诊断: {str(reason).replace(chr(10), ' ')[:240]}")
    if usage.get("limitations"):
        lines.append("- 采集边界: 未落本地日志的临时/远程会话不在汇总中；缓存和推理是输入/输出的子集。")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--since", type=float)
    parser.add_argument("--until", type=float)
    parser.add_argument("--session-id")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    import core.bugx_paths as bugx_paths
    import core.secure_atomic as secure_atomic

    runtime_path = args.runtime or bugx_paths.require(root).runtime
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if args.since is not None:
        runtime["lifecycle_started_epoch"] = args.since
    kwargs: dict[str, Any] = {"persist": False}
    if args.until is not None:
        kwargs["until_epoch"] = args.until
    if args.session_id is not None:
        kwargs["session_id"] = args.session_id
    result = collect_for_report(root, runtime, collector_kwargs=kwargs)
    if result is None:
        result = {"measurement_status": "unavailable", "reason": "Host has no local usage collector"}
    if args.output:
        secure_atomic.atomic_write_json(args.output.expanduser().resolve(), result, mode=0o600)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("measurement_status") in {"measured", "partial"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
