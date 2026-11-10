"""前台宿主自治挖掘入口（DOC-138）。"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import __version__
from ..common import CliError, _cli_version_environment, _ensure_core_importable
from ..infra.mcp_advisor import verify_builtin_mcp_manifest
from ..hosts.registry import (
    engine_host_concurrency_bounds,
    engine_host_label,
    engine_host_runtime_flag,
)


def _estimate_complexity_baseline(root: Path, manifest_path: Path | None = None) -> float:
    """根据待覆盖单元规模与复杂度动态估算基准墙钟时间。"""
    unit_count = 0
    if manifest_path is not None and Path(manifest_path).is_file():
        try:
            content = Path(manifest_path).read_text(encoding="utf-8")
            lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
            unit_count = sum(1 for line in lines if "|" in line and "---" not in line)
        except OSError:
            unit_count = 0
    if unit_count <= 0:
        ledger = root / ".bugx" / "coverage-ledger.json"
        if ledger.is_file():
            try:
                data = json.loads(ledger.read_text(encoding="utf-8"))
                units = data.get("units") if isinstance(data, dict) else {}
                unit_count = len(units) if isinstance(units, dict) else 0
            except (OSError, ValueError):
                unit_count = 0
    unit_count = max(1, unit_count)
    return float(min(360.0, max(90.0, 60.0 + unit_count * 25.0)))


def _install_host(root: Path, host: str, *, timeout: float = 180.0) -> dict[str, object]:
    """Repair the selected host entry before starting the no-prompt run."""
    from bugx_cli.infra.security import _find_repo_root
    repo = _find_repo_root(Path(__file__))
    source_cli = repo / "interfaces" / "cli" / "bugx_cli" / "cli.py"
    if not source_cli.is_file():
        source_cli = repo / "src" / "bugx_cli" / "cli.py"
    if source_cli.is_file():
        command = [sys.executable, "-m", "bugx_cli.cli"]
        environment = os.environ.copy()
        source_root = str(source_cli.parent.parent)
        environment["PYTHONPATH"] = os.pathsep.join(
            [source_root, environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    else:
        executable = shutil.which("bugx")
        if not executable:
            return {"status": "blocked", "reason": "找不到 bugx CLI，无法自动补齐宿主入口"}
        command = [executable]
        environment = None
    try:
        result = subprocess.run(
            [*command, "install", "--root", str(root), "--agent", host,
             "--force", "--allow-missing", "--non-interactive", "--json"],
            cwd=root, env=environment, capture_output=True, text=True,
            timeout=min(180.0, timeout), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        detail = str(exc)[-1200:]
        return {
            "status": "blocked", "reason": "host_install_timeout",
            "detail": detail,
        }
    detail = (result.stderr or result.stdout or "").strip()[-1200:]
    return {
        "status": "ok" if result.returncode == 0 else "blocked",
        "exit_code": result.returncode,
        "detail": detail,
    }


def command_autonomous(args: argparse.Namespace) -> int:
    with _cli_version_environment():
        return _command_autonomous(args)


def _persist_runtime_context(
    root: Path,
    *,
    host: str,
    workers: int,
    model: str = "",
    provider: str = "",
) -> dict[str, object]:
    """Persist the complete autonomous dispatch context before starting a host."""
    from core.launch_phases.common import isolated_runtime_scope
    from core.launch_phases.session_runtime import load_runtime, runtime_lock, save_runtime

    with isolated_runtime_scope(root):
        with runtime_lock():
            runtime = load_runtime(reconcile=False)
            runtime.update({
                "host": host,
                "autonomous_mode": True,
                # 「CLI 自行以进程池驱动宿主」是插件事实（worker_runtime.autonomous_cli）。
                "execute": engine_host_runtime_flag(host, "autonomous_cli"),
                "workers": workers,
                "worker_capacity": workers or None,
                "subagent_type": "bugx-worker",
                "stagger_seconds": 0.0,
                "model": model or None,
                "provider": provider or None,
            })
            save_runtime(runtime)
            return runtime


def _command_autonomous(args: argparse.Namespace) -> int:
    """Run a supported CLI foreground loop without interactive fallback."""
    root = args.root.resolve()
    host = str(args.host)
    workers = int(getattr(args, "workers", 0) or 0)
    model = str(getattr(args, "model", "") or "").strip()
    provider = str(getattr(args, "provider", "") or "").strip()
    bounds = engine_host_concurrency_bounds(host)
    if bounds is not None and workers and not bounds[0] <= workers <= bounds[1]:
        # 并发区间是插件事实（worker_runtime.concurrency_bounds）：进程池宿主必须
        # 落在这个区间内，0 表示按 Supervisor 自适应容量。
        raise CliError(
            f"{engine_host_label(host)} --workers 必须是 {bounds[0]}~{bounds[1]} 的整数；"
            "0 表示按 Supervisor 自适应容量"
        )
    if not root.is_dir():
        raise CliError(f"项目根目录不存在: {root}")
    if (not math.isfinite(args.max_wall_time) or args.max_wall_time < 0
            or not math.isfinite(args.agent_timeout) or args.agent_timeout <= 0
            or args.max_turns < 0):
        raise CliError("--max-wall-time 必须是有限非负数，--agent-timeout 必须是有限正数，上限不能为负数")
    started = time.monotonic()
    adaptive_wall_time = getattr(args, "adaptive_wall_time", True)
    wall_time = args.max_wall_time
    if wall_time == 0.0 and getattr(args, "adaptive_baseline", False):
        wall_time = _estimate_complexity_baseline(root, getattr(args, "coverage_manifest", None))
    deadline = started + wall_time if wall_time > 0 else None
    # 显式审计范围：把清单路径经环境变量传给宿主会话内的 ``engine pre``。
    # 为什么不是 CLI 参数直达：autonomous 自己**不跑** pre，真正跑 pre 的是宿主
    # Agent（``bugx engine pre --autonomous``）；环境变量是两者之间唯一稳定通道
    # （与 BUGX_PROJECT_ROOT / BUGX_SANDBOX_TEST 同一机制）。gate 侧会做真实性
    # 校验（存在、可解析、单元路径真实），这里只负责传递，不擅自"修好"错误路径。
    explicit_manifest = getattr(args, "coverage_manifest", None)
    if explicit_manifest is not None:
        resolved_manifest = Path(explicit_manifest).expanduser()
        if resolved_manifest.is_symlink() or not resolved_manifest.is_file():
            raise CliError(f"--coverage-manifest 不可用: {resolved_manifest}")
        os.environ["BUGX_COVERAGE_MANIFEST"] = str(resolved_manifest.resolve())
    # The autonomous launcher may run from a source checkout while PATH still
    # exposes an older installed ``bugx``.  The current CLI is authoritative;
    # an explicit override remains available for controlled compatibility tests.
    verify_builtin_mcp_manifest()
    install_timeout = 180.0 if deadline is None else deadline - time.monotonic()
    if install_timeout <= 0:
        install = {
            "status": "blocked", "reason": "autonomous_wall_time_budget",
            "detail": "总墙钟预算在宿主初始化前已耗尽",
        }
    else:
        try:
            install = _install_host(root, host, timeout=install_timeout)
        except KeyboardInterrupt:
            payload = {"status": "aborted", "reason": "operator_interrupt",
                       "recovery_required": True, "host": host}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("[bugx] ABORTED: operator_interrupt (recovery required)")
            return 130
    if install["status"] != "ok":
        payload = {"status": "blocked", "reason": install, "host": host}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx] BLOCKED: {install.get('detail') or install.get('reason')}")
        return 1

    # The core is loaded only after install, so the project/global engine is the
    # same version as the host entry that was just repaired.
    _ensure_core_importable()
    try:
        _persist_runtime_context(root, host=host, workers=workers, model=model, provider=provider)
    except (OSError, ValueError, RuntimeError) as exc:
        payload = {"status": "blocked", "reason": "runtime_context_failed", "detail": str(exc), "host": host}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx] BLOCKED: {exc}")
        return 1
    from core.supervisor.supervisor_host import AUTONOMOUS_HOST_PROMPT, _ADAPTERS, run_host_autonomous

    from core.runtime import provider_window  # 审计 #35：限流窗口跨进程生效

    window = provider_window.active(root)
    if window is not None:
        # 审计 #35：provider 限流窗口跨进程生效，窗内不得重跑宿主自治。
        print(
            f"[bugx] BLOCKED: provider_rate_limited（限流窗口内，剩余 "
            f"{window['remaining_seconds']}s；确需提前重跑请删除 "
            f".bugx/state/provider-window.json）"
        )
        return 2

    adapter = _ADAPTERS[host]()
    previous_opencode_model = os.environ.get("BUGX_OPENCODE_MODEL")
    if model:
        # The OpenCode plugin reads this explicit override when the controller
        # builds its child argv. Keep it scoped to this foreground run so it
        # outranks user/project defaults without leaking into later commands.
        os.environ["BUGX_OPENCODE_MODEL"] = model
    try:
        try:
            result = run_host_autonomous(
                root, adapter, max_turns=args.max_turns,
                max_wall_time=args.max_wall_time, agent_timeout=args.agent_timeout,
                prompt=AUTONOMOUS_HOST_PROMPT, deadline=deadline,
                adaptive_wall_time=adaptive_wall_time,
            )
        except KeyboardInterrupt:
            result = {
                "status": "aborted", "reason": "operator_interrupt",
                "recovery_required": True, "turns": [],
            }
            result.update({"host": host, "install": install})
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                print("[bugx] ABORTED: operator_interrupt (recovery required)")
            return 130
    finally:
        if model:
            if previous_opencode_model is None:
                os.environ.pop("BUGX_OPENCODE_MODEL", None)
            else:
                os.environ["BUGX_OPENCODE_MODEL"] = previous_opencode_model
    if result.get("reason") == "provider_rate_limited":
        provider_window.record(
            root, result.get("retry_window_seconds"), reason="provider_rate_limited",
        )
    result.update({"host": host, "install": install})
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"[bugx] {result.get('status', 'blocked').upper()}: "
              f"{result.get('reason', '')}")
    return 0 if result.get("status") == "complete" else 1
