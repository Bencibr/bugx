#!/usr/bin/env python3
"""core/host_runtime/common.py - 宿主 CLI 执行器的共享基座（DOC-239 S2 续）。

宿主 CLI 进程执行器（``core/host_runtime/<host>.py``）是**实现体**，不是声明：
声明（runner 模块名、provider 标签、沙箱同步清单、沙箱目录名）在
``core/hosts/<host>.py``。执行器之间只共享真正与宿主无关的三件事：

* 失败异常类型（``WorkerRuntimeError``，``worker_runtime`` 对外仍是同一个类）；
* 工作区 ``.bugx/`` 忽略项写入（凭据与运行产物不得进版本控制）；
* 子进程组的终止与「已提交 checkpoint 的 claim 查询」（跨平台行为一致）。

依赖约束：只允许标准库 + 引擎内已被 ``worker_runtime`` 依赖的模块，**禁止**
import ``host_registry`` / ``hosts`` / ``bugx_cli.*``——这层不是声明点。
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

__all__ = [
    "WorkerRuntimeError",
    "claim_checkpoint_id",
    "ensure_workspace_gitignores_bugx",
    "terminate_process_group",
]

logger = logging.getLogger(__name__)


class WorkerRuntimeError(RuntimeError):
    """自主 Worker 执行失败异常（``worker_runtime`` 对外导出的同一类型）。"""


def ensure_workspace_gitignores_bugx(workspace_root: Path) -> None:
    """确保被测工作区与沙箱环境忽略 .bugx/，防止凭据与运行产物被意外加入版本控制。"""

    bugx_dir = workspace_root / ".bugx"
    try:
        bugx_dir.mkdir(parents=True, exist_ok=True)
        inner_gitignore = bugx_dir / ".gitignore"
        if not inner_gitignore.exists():
            inner_gitignore.write_text("*\n", encoding="utf-8")
            inner_gitignore.chmod(0o600)
    except OSError as exc:
        logger.warning(".bugx/.gitignore 写入失败: %s", exc)

    git_dir = workspace_root / ".git"
    if not (git_dir.is_dir() or git_dir.is_file()):
        return

    gitignore_path = workspace_root / ".gitignore"
    try:
        content = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
        lines = [line.strip() for line in content.splitlines()]
        bugx_ignored = any(
            re.match(r"^/?\.bugx/?$", line) or line == ".bugx/**"
            for line in lines
        )
        if not bugx_ignored:
            addition = "\n# BugX runtime & worker credentials\n.bugx/\n"
            if content and not content.endswith("\n"):
                addition = "\n" + addition
            with open(gitignore_path, "a", encoding="utf-8") as f:
                f.write(addition)
    except OSError as exc:
        logger.warning("写入工作区 .gitignore 失败: %s", exc)


def terminate_process_group(process: subprocess.Popen[str], *, force: bool = False) -> None:
    """Stop a host CLI process and all children without leaving MCP descendants."""

    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill() if force else process.terminate()
    except (OSError, ProcessLookupError):
        pass


def claim_checkpoint_id(supervisor: Any, worker_id: str, execution_id: str) -> str | None:
    """Return the persisted checkpoint id for a matching completed claim."""

    state = supervisor.status()
    claim = state.claims.get(worker_id)
    if not (
        claim is not None
        and claim.execution_id == execution_id
        and claim.state == "submitted"
    ):
        return None
    return claim.last_checkpoint_id
