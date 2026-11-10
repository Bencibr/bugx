#!/usr/bin/env python3
"""只读审计的**执行侧强制**与廉价校验（CORE-308 P2 收尾）。

**为什么需要它**：``write_access=False`` 会让 claim 层不派生 ``file:`` 互斥键，
同文件多个 worker 因此可并发。这个豁免只有在「worker 真的改不了源文件」时才安全。

历史教训（build 268）：用 ``fix_mode == "auto"`` 推导写权限是**假前提**——
``fix_mode`` 只被 ``post_phase`` 消费，worker 执行侧从不读它，也没有只读沙箱。
于是「声明只读」只是纸面承诺，一旦模型自作主张改文件就是静默覆盖。

本模块提供两层：

1. **强制**（``prepare_readonly_view``）：给只读 claim 一个独立只读视图
   （``git worktree`` + ``chmod -R a-w``），worker 在那里读/跑，改不动真源；
2. **校验**（``snapshot_source_state`` / ``verify_untouched``）：用
   ``git status --porcelain`` 做基线/复检，任何「声明只读却新增改动」都被拒绝。
   校验是廉价的（无需全量 sha256 扫描），且跨平台。

**设计边界**：不猜测、不静默降级。git 不可用 → 返回 ``None`` 表示「无法强制」，
调用方必须据此**不授予**只读豁免（fail-closed），而不是乐观假设安全。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _git(root: Path, *args: str, timeout: float = 60.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, (proc.stdout or "")


def snapshot_source_state(root: Path) -> dict[str, object] | None:
    """记录源码基线：HEAD + 已改动文件集合。``None`` = 无法取证（git 不可用）。"""

    code, head = _git(root, "rev-parse", "HEAD")
    if code != 0 or not head.strip():
        return None
    code, status = _git(root, "status", "--porcelain")
    if code != 0:
        return None
    dirty = sorted(
        line[3:].strip() for line in status.splitlines() if len(line) > 3
    )
    return {"head": head.strip(), "dirty": dirty}


def verify_untouched(root: Path, baseline: dict[str, object] | None) -> str | None:
    """复检：返回**违规描述**（``None`` = 未越权）。

    判定只针对「新增/变化的已改动文件」：基线里本来就 dirty 的文件（并行会话、
    启动前脏工作树）不算这次 claim 的越权，避免把别人的改动栽给只读 worker。
    """

    if not baseline:
        return None
    current = snapshot_source_state(root)
    if current is None:
        return None
    before = set(baseline.get("dirty") or ())
    after = set(current.get("dirty") or ())
    fresh = sorted(after - before)
    if not fresh:
        return None
    preview = ", ".join(fresh[:5])
    return f"声明只读却新增改动: {preview}（共 {len(fresh)} 个文件）"


def prepare_readonly_view(root: Path, target: Path) -> Path | None:
    """为只读 claim 建**独立只读视图**（git worktree + 去写权限）。

    返回视图路径；``None`` 表示无法建立（git 不可用 / worktree 失败）——
    调用方必须据此不授予只读豁免。

    为什么用 worktree 而不是 chmod 真源：真源与写 worker 共享，chmod 会影响别人；
    worktree 是独立工作树（共享 object db，零复制成本），清理即消失。
    """

    resolved_root = root.resolve()
    view = target.resolve()
    # F-RSI-20261110-016 防御加固（收窄版）：view 即将对其子树 chmod
    # 0o555/0o444——它绝不能与真源 root 本体重合（轮 22 实测：仓库 core
    # 653 文件被 0444，bump 全链路 EPERM）。root 外的独立目录是合法姿势
    # （worktree 可任意位置，既有测试依赖）；同根内的子路径也合法
    # （worktree 子目录）。仅拒绝 view==root 这一真正的灾难形态。
    if view == resolved_root:
        raise ValueError(
            f"只读视图路径不安全：view 与真源 root 重合（{view}）——"
            "chmod 0o444 会把共享真源整体只读（F-016：bump 全链路 EPERM）；"
            "请指定 root 之外的独立 worktree 目录。"
        )
    if view.exists():
        shutil.rmtree(view, ignore_errors=True)
    view.parent.mkdir(parents=True, exist_ok=True)
    code, _ = _git(resolved_root, "worktree", "add", "--detach", str(view), "HEAD")
    if code != 0:
        return None
    # 去写权限：worker 即使想改也改不动（文件系统层强制，不依赖模型自觉）。
    # **fail-closed**：任何一处 chmod 失败都意味着「只读保证不完整」，此时必须
    # 回滚并返回 None（调用方据此不授予只读豁免），绝不静默返回「部分只读」的视图
    # ——那会让 claim 层误以为同文件并发安全。
    failures: list[str] = []
    targets: list[tuple[Path, int]] = []
    for path in sorted(view.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        targets.append((path, 0o555 if path.is_dir() else 0o444))
    targets.append((view, 0o555))
    for path, mode in targets:
        try:
            os.chmod(path, mode)
        except OSError:
            failures.append(str(path))
    if failures:
        _reset_permissions(view)
        _git(resolved_root, "worktree", "remove", "--force", str(view))
        shutil.rmtree(view, ignore_errors=True)
        return None
    return view


def _reset_permissions(view: Path) -> None:
    """恢复读写权限（回收/回滚前必须做，否则 remove 会失败）。"""

    try:
        os.chmod(view, 0o755)
    except OSError:
        pass
    for path in sorted(view.rglob("*"), reverse=True):
        try:
            if path.is_symlink():
                continue
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        except OSError:
            continue


def release_readonly_view(root: Path, view: Path | None) -> None:
    """回收只读视图（先恢复写权限，否则 worktree remove 会失败）。"""

    if view is None:
        return
    _reset_permissions(view)
    _git(root.resolve(), "worktree", "remove", "--force", str(view))
    shutil.rmtree(view, ignore_errors=True)
