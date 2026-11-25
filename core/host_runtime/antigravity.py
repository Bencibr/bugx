#!/usr/bin/env python3
"""Antigravity Worker process executor.

The Agent, not this module, owns the BugX lifecycle decisions.  This module
only starts one bounded ``agy`` Worker process after a claim exists, binds it to
the requested workspace, and refuses to report success without a Supervisor
checkpoint.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import core.hosts as host_plugins

from .common import (
    WorkerRuntimeError,
    claim_checkpoint_id,
    terminate_process_group,
)

__all__ = ["execute_worker"]


_HOST_NAME = __name__.rsplit(".", 1)[-1]
_PLUGIN = host_plugins.get(_HOST_NAME)
if _PLUGIN is None:  # pragma: no cover - plugin removal is an import-time defect
    raise RuntimeError(f"{__name__} 需要 core/hosts/{_HOST_NAME}.py 声明；插件缺失")

_NAME = _PLUGIN.name
_PROVIDER_KEY = _PLUGIN.worker_runtime.provider_key or _NAME


def execute_worker(
    workspace_root: Path,
    claim: Any,
    task_spec: str,
    *,
    supervisor: Any,
    stage_name: str,
    executable: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Run one pre-claimed Antigravity Worker and require a checkpoint."""

    del provider
    from core.supervisor.worker_runtime import resolve_worker_timeout

    failure = f"{_NAME}_worker_failed"
    root = workspace_root.resolve()
    worker_id = getattr(claim, "worker_id", str(claim))
    execution_id = getattr(claim, "execution_id", f"exec-{worker_id}")
    artifact_root_rel = getattr(claim, "artifact_root", "")
    if artifact_root_rel:
        artifact_dir = (root / artifact_root_rel).resolve()
        if artifact_dir != root and root not in artifact_dir.parents:
            raise WorkerRuntimeError(f"{failure}: claim artifact_root 越界")
        artifact_dir.mkdir(parents=True, exist_ok=True)

    profile_budget = getattr(_PLUGIN.worker_runtime, "worker_timeout_seconds", None)
    base_override = (
        float(profile_budget)
        if isinstance(profile_budget, (int, float))
        and not isinstance(profile_budget, bool)
        and profile_budget > 0
        else None
    )
    timeout_seconds = resolve_worker_timeout(
        model_tier={
            "exploit": "tier_2_exploit",
            "verify": "tier_3_verify",
        }.get(str(stage_name).split("-")[-1], "tier_1_recon"),
        attempt=int(getattr(claim, "attempt", 1) or 1),
        base_override=base_override,
    )

    host_executable = executable or shutil.which("agy") or "agy"
    command = [
        host_executable,
        "--agent", "bugx-worker",
        "--mode", "auto",
        "--dangerously-skip-permissions",
        "--add-dir", str(root),
        "--output-format", "json",
        "-p", task_spec,
    ]
    if model:
        command[3:3] = ["--model", str(model)]

    environment = os.environ.copy()
    popen_kwargs: dict[str, Any] = {
        "cwd": str(root),
        "env": environment,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        popen_kwargs["preexec_fn"] = os.setsid
    elif os.name == "nt":  # pragma: no cover - exercised on Windows hosts
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    process = subprocess.Popen(command, **popen_kwargs)
    try:
        supervisor.start_worker(
            worker_id,
            provider=_PROVIDER_KEY,
            execution_id=execution_id,
            process_pid=process.pid,
        )
    except Exception:
        terminate_process_group(process)
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            terminate_process_group(process, force=True)
        raise

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            terminate_process_group(process, force=True)
            stdout, stderr = "", ""
        checkpoint_id = claim_checkpoint_id(supervisor, worker_id, execution_id)
        if checkpoint_id is None:
            raise WorkerRuntimeError(
                f"{failure}: timeout after {timeout_seconds:.1f}s"
            ) from exc
        return {
            "worker_id": worker_id,
            "checkpoint_id": checkpoint_id,
            "status": "checkpointed",
            "exit_code": process.returncode,
            "stdout": (stdout or "")[-2000:],
            "stderr": (stderr or "")[-2000:],
        }

    checkpoint_id = claim_checkpoint_id(supervisor, worker_id, execution_id)
    if checkpoint_id is None:
        detail = (stderr or stdout or "").strip()[-500:]
        raise WorkerRuntimeError(
            f"{failure}: process exited without a valid Checkpoint"
            + (f": {detail}" if detail else "")
        )
    return {
        "worker_id": worker_id,
        "checkpoint_id": checkpoint_id,
        "status": "checkpointed",
        "exit_code": process.returncode,
        "stdout": (stdout or "")[-2000:],
        "stderr": (stderr or "")[-2000:],
    }

