#!/usr/bin/env python3
"""launch_phases/supervisor_coverage.py - 覆盖清单初始化与会话恢复管理。"""

from __future__ import annotations

from contextlib import contextmanager

import csv
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

from core.launch_phases import common as _common
from core.launch_phases.common import (
    resolve_project_root,
    uses_project_layout,
    supervisor_state_path,
    bugx_paths,
    MANIFEST_TABLE_HEADER,
    RUNTIME_STARTED_THIS_PROCESS,
    is_runtime_started_this_process,
)
from core.launch_phases.session_runtime import (
    load_runtime,
    save_runtime,
    iso_time,
    runtime_lock,
)


def supervisor_state_path(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "supervisor_state_path"):
        fn = getattr(sb, "supervisor_state_path")
        if fn is not supervisor_state_path:
            return fn(*args, **kwargs)
    from core.launch_phases.common import supervisor_state_path as real
    return real(*args, **kwargs)

def resolve_project_root(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "resolve_project_root"):
        fn = getattr(sb, "resolve_project_root")
        if fn is not resolve_project_root:
            return fn(*args, **kwargs)
    from core.launch_phases.common import resolve_project_root as real
    return real(*args, **kwargs)

def is_runtime_started_this_process(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "is_runtime_started_this_process"):
        fn = getattr(sb, "is_runtime_started_this_process")
        if fn is not is_runtime_started_this_process:
            return fn(*args, **kwargs)
    from core.launch_phases.common import is_runtime_started_this_process as real
    return real(*args, **kwargs)

def _sb():
    return sys.modules.get("core.launch_phases.supervisor_bridge") or sys.modules.get("launch_phases.supervisor_bridge")

def _get_sb_attr(name: str, fallback=None):
    sb = _sb()
    if sb and hasattr(sb, name):
        return getattr(sb, name)
    return fallback

def carry_over_previous_manifest(manifest: Path) -> bool:
    """新生命周期重扫最近清单，只保留未变化单元的有效覆盖证据。

    纯路径推导（manifest 位于 ``<lifecycles>/<id>/state/`` 下），独立布局
    （无兄弟生命周期目录）自然返回 False，由 discover 兜底。
    """
    lifecycles_root = manifest.parent.parent.parent
    if not lifecycles_root.is_dir():
        return False

    def lifecycle_no(path: Path) -> int:
        try:
            return int(path.name.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    from core.coverage import coverage_bootstrap
    from core.coverage import module_coverage
    current = manifest.parent.parent.name
    for candidate in sorted(
        (item for item in lifecycles_root.iterdir() if item.is_dir()),
        key=lifecycle_no, reverse=True,
    ):
        if candidate.name == current:
            continue
        source = candidate / "state" / "module-coverage.md"
        if not source.is_file():
            continue
        rows, errors = module_coverage.parse_manifest(source)
        if errors or not rows:
            continue
        try:
            previous_metadata = coverage_bootstrap.manifest_discovery_metadata(source)
            use_symbols = previous_metadata.get("discovery_mode") not in {"files", "files-only"}
            discovered, changes = module_coverage.reconcile_discovery_rows(
                rows, resolve_project_root(), symbols=use_symbols,
            )
            # ``reconcile_discovery_rows`` returns rendered Markdown lines,
            # while the status validator accepts parsed ModuleRow objects.
            # Parse and validate the complete candidate before either atomic
            # writer runs; malformed/ambiguous output must leave recovery idle.
            if not isinstance(discovered, list) or any(
                not isinstance(line, str) for line in discovered
            ):
                return False
            discovered_rows = [module_coverage.row_from_line(line) for line in discovered]
            if module_coverage.status_manifest_errors(discovered_rows):
                return False
            coverage_bootstrap.write_manifest(  # shared atomic writer/markers
                manifest, discovered,
                discovery_metadata=coverage_bootstrap.build_discovery_metadata(
                    status="complete" if use_symbols else "partial",
                    mode="symbols" if use_symbols else "files",
                    confidence="high" if use_symbols else "low",
                ),
            )
            coverage_bootstrap.write_inventory_diff(manifest, changes)
        except (
            OSError, ValueError, RuntimeError, TypeError, AttributeError, IndexError,
        ):
            return False
        return True
    return False

def append_manifest_rows(manifest: Path, rows: list[str], fresh: bool) -> None:
    """把 discover 骨架行并进清单表；fresh 时连标题与表头一起原子写入。"""
    from core.coverage import module_coverage
    payload = "\n".join(rows) + "\n"
    if fresh:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "# 最小单元与业务链路覆盖清单（module-coverage）\n\n"
            f"{module_coverage.SYMBOL_INDEX_MARKER}\n"
            f"{module_coverage.PROJECT_GRAPH_MARKER}\n\n"
            "## 覆盖清单\n\n"
            + MANIFEST_TABLE_HEADER + payload
        )
    else:
        lines = manifest.read_text(encoding="utf-8").splitlines(keepends=True)
        insert_at = None
        for index, line in enumerate(lines):
            if "路径/范围" in line and "状态" in line:
                insert_at = index + 2  # 跳过表头行与分隔行
                break
        if insert_at is None:
            content = "".join(lines) + "\n" + payload
        else:
            content = "".join(lines[:insert_at]) + payload + "".join(lines[insert_at:])
    temp = manifest.with_name(manifest.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, manifest)


@contextmanager
def coverage_bootstrap_runtime_lock():
    """Serialize runtime classification and manifest recovery with lifecycle starts."""
    from core.launch_phases import session_runtime

    with session_runtime._pinned_runtime_paths(
        Path(_common.RUNTIME_FILE), Path(_common.RUNTIME_LOCK_FILE),
    ):
        with session_runtime.runtime_lock():
            yield


def runtime_coverage_recovery_mode(runtime_file: Path) -> str:
    """Classify persisted runtime as fresh, quiescent, or unsafe for bootstrap.

    Quiescent state may carry forward a prior manifest after hash reconciliation,
    but may not invent a new discovery skeleton. Every unknown/corrupt state blocks.
    """
    try:
        before = runtime_file.lstat()
    except FileNotFoundError:
        return "fresh"
    except OSError:
        return "blocked"
    if not stat.S_ISREG(before.st_mode):
        return "blocked"

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(runtime_file, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) \
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                return "blocked"
            state = json.load(stream)
    except (OSError, ValueError, TypeError, UnicodeError):
        return "blocked"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    if not isinstance(state, dict):
        return "blocked"
    try:
        from core.launch_phases import session_runtime

        session_runtime._require_current_runtime_schema(state, runtime_file)
    except (ImportError, OSError, TypeError, ValueError):
        return "blocked"

    if state.get("state") != "idle" or state.get("active") is not False:
        return "blocked"
    if state.get("session_state") != "initializing":
        return "blocked"

    empty_fields = (
        "run_id", "pid", "host_pid", "started_at", "started_epoch",
        "session_started_at", "session_started_epoch", "startup_action",
        "segment_started_epoch", "paused_at", "heartbeat_at", "heartbeat_epoch",
        "round_at_start", "startup_id", "startup_observation", "startup_intent",
        "startup_result", "session_id", "round_id", "life_no", "session_closed_at",
        "final_round_id", "polling_last_round", "lifecycle_id",
        "lifecycle_started_at", "lifecycle_started_epoch", "lifecycle_last_life",
        "lifecycle_last_report",
    )
    if any(state.get(key) is not None for key in empty_fields):
        return "blocked"

    zero_integer_fields = (
        "sessions_completed", "sessions_aborted", "lifecycle_no",
        "lifecycle_cycles_completed", "lifecycle_findings_total",
        "lifecycle_credited_total", "lifecycle_fraud_total",
        "lifecycle_no_progress_cycles",
    )
    if any(
        not isinstance(state.get(key), int)
        or isinstance(state.get(key), bool)
        or state[key] != 0
        for key in zero_integer_fields
    ):
        return "blocked"

    zero_duration_fields = (
        "session_elapsed_seconds", "last_session_elapsed_seconds",
        "total_elapsed_seconds",
    )
    if any(
        not isinstance(state.get(key), (int, float))
        or isinstance(state.get(key), bool)
        or state[key] != 0
        for key in zero_duration_fields
    ):
        return "blocked"

    empty_history_fields = (
        "sessions", "life_runs", "lifecycle_history", "lifecycle_lesson_ids",
        "lifecycle_incident_ids", "lesson_recent_signatures", "realized_fix_incidents",
    )
    if any(state.get(key) != [] for key in empty_history_fields):
        return "blocked"
    if state.get("lifecycle_terminal") is not False \
            or state.get("lifecycle_auto_started") is not False \
            or state.get("polling_round_auto_started") is not False:
        return "blocked"

    events = state.get("events")
    polling_stop = state.get("polling_stop_reason")
    lifecycle_stop = state.get("lifecycle_stop_reason")
    if not isinstance(events, list) \
            or (polling_stop is not None and not isinstance(polling_stop, str)) \
            or (lifecycle_stop is not None and not isinstance(lifecycle_stop, str)):
        return "blocked"
    if events or polling_stop is not None or lifecycle_stop is not None:
        return "quiescent"
    return "fresh"


def state_path_exists_or_symlink(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def bootstrap_coverage_manifest(manifest: Path) -> bool:
    with _coverage_bootstrap_runtime_lock():
        return _bootstrap_coverage_manifest_locked(manifest)


def bootstrap_coverage_manifest_locked(manifest: Path) -> bool:
    """解除 fresh 安装/新生命周期「清单未建 → 门禁拒绝 → agent 无法开工」死锁。

    - 清单已有可调度行 → 原样保留（幂等）；
    - pristine idle 的清单缺失 → 优先沿用上一生命周期清单，否则用符号级
      discover 生成骨架；
    - quiescent idle 带有 stop/event 历史 → 只允许重扫继承上一生命周期清单，
      不得创建新骨架；
    - 清单存在但为空表/只有示例行 → 追加 discover 骨架行。
    结构性错误（表头缺字段等）不自动改写，交由原始报错暴露。
    运行中、终态、含活动身份/历史计数或存在 Supervisor 状态时，缺失清单继续
    fail-closed。返回 False 表示仍无可调度单元；发现失败抛 ValueError。
    """
    from core.coverage import module_coverage
    # The first ``pre`` can create runtime.json before it creates Supervisor; it
    # is fresh bootstrap state. Persisted runtime is classified under the runtime
    # lock: only a strict idle state is recoverable. Historical stop/event markers
    # allow carry-over only, never a new discovery skeleton.
    supervisor_state = supervisor_state_path()
    supervisor_backup = supervisor_state.with_name(supervisor_state.name + ".bak")
    runtime_started_this_process = (
        getattr(_sb(), "_RUNTIME_STARTED_THIS_PROCESS", getattr(_sb(), "RUNTIME_STARTED_THIS_PROCESS", False))
        or is_runtime_started_this_process()
    )
    runtime_mode = (
        "fresh" if runtime_started_this_process
        else _runtime_coverage_recovery_mode(Path(_common.RUNTIME_FILE))
    )
    supervisor_anchor = (
        _state_path_exists_or_symlink(supervisor_state)
        or _state_path_exists_or_symlink(supervisor_backup)
    )
    active_lifecycle = supervisor_anchor or runtime_mode == "blocked"
    if manifest.is_file():
        rows, errors = module_coverage.parse_manifest(manifest)
        if errors:
            if active_lifecycle:
                raise ValueError(
                    f"活动生命周期覆盖清单损坏，拒绝自动重建: {manifest}; "
                    "请先执行 bugx doctor --repair 或从备份恢复"
                )
            return False
        if rows:
            return True
        if active_lifecycle:
            raise ValueError(
                f"活动生命周期覆盖清单为空，拒绝自动重建: {manifest}; "
                "请先恢复清单后再继续"
            )
    elif active_lifecycle:
        raise ValueError(
            f"活动生命周期覆盖清单缺失，拒绝自动生成: {manifest}; "
            "请先执行 bugx doctor --repair 或从备份恢复"
        )
    elif runtime_mode == "quiescent":
        if not _carry_over_previous_manifest(manifest):
            raise ValueError(
                f"空闲历史生命周期缺少可继承的有效覆盖清单，拒绝生成新骨架: {manifest}"
            )
        rows, errors = module_coverage.parse_manifest(manifest)
        if errors or not rows:
            raise ValueError(
                f"覆盖清单历史重扫后未通过复核，拒绝继续: {manifest}; "
                + "; ".join(errors or ["no schedulable rows"])
            )
        print(
            "[launch_bugx] ✓ 已从上一生命周期哈希重扫恢复空闲 lifecycle 覆盖清单: "
            f"{manifest.name}"
        )
        return True
    elif _carry_over_previous_manifest(manifest):
        rows, _ = module_coverage.parse_manifest(manifest)
        if rows:
            print(
                "[launch_bugx] ✓ 已完成上一生命周期清单重扫（按哈希保留有效证据）: "
                f"{manifest.name}"
            )
            return True
    try:
        discovered = module_coverage.discovery_rows(resolve_project_root())
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError(f"覆盖清单自动发现失败: {exc}") from exc
    if not discovered:
        return False
    _append_manifest_rows(manifest, discovered, fresh=not manifest.is_file())
    try:
        rows, _ = module_coverage.parse_manifest(manifest)
        if not any(r.phase == "业务" for r in rows):
            business_lines = module_coverage.business_discovery_rows(resolve_project_root(), rows)
            if business_lines:
                with open(manifest, "a", encoding="utf-8") as f:
                    for bl in business_lines:
                        f.write(bl + "\n")
    except Exception:
        pass
    print(
        f"[launch_bugx] ✓ 已自动生成覆盖清单骨架：{len(discovered)} 个最小单元"
        "（module_coverage discover；Agent 开工后继续细化难度/工具/业务链路列）"
    )
    return True



def _carry_over_previous_manifest(manifest: Path) -> bool:
    sb = _sb()
    if sb and hasattr(sb, "_carry_over_previous_manifest"):
        fn = getattr(sb, "_carry_over_previous_manifest")
        if fn is not _carry_over_previous_manifest:
            return fn(manifest)
    return carry_over_previous_manifest(manifest)
_append_manifest_rows = append_manifest_rows
_coverage_bootstrap_runtime_lock = coverage_bootstrap_runtime_lock
_runtime_coverage_recovery_mode = runtime_coverage_recovery_mode
_state_path_exists_or_symlink = state_path_exists_or_symlink
_bootstrap_coverage_manifest = bootstrap_coverage_manifest
_bootstrap_coverage_manifest_locked = bootstrap_coverage_manifest_locked
