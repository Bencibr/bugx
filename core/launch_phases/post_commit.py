#!/usr/bin/env python3
"""launch_phases/post_commit.py - Git 提交边界、变更判定与 Finding 回滚 (DOC-181)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess as _real_subprocess
import sys
import time
from pathlib import Path
from typing import Any

from core.launch_phases.common import (
    resolve_project_root,
    uses_project_layout,
    bugx_paths,
    host_workspace_dirs,
)
from core.launch_phases.session_runtime import (
    load_runtime,
    save_runtime,
    runtime_lock,
    append_runtime_event,
)
from core.launch_phases.finding_settlement import settle_findings_path

try:
    from core.ledger import finding_confirmation
except ImportError:
    try:
        from core import finding_confirmation
    except ImportError:
        finding_confirmation = None  # type: ignore[assignment]


_GIT_COMMIT_TIMEOUT = 60
_GIT_RUNTIME_NAMES = {
    "audit-supervisor.json", "bugx-life.json", "bugx-runtime.json",
    "coverage-evidence.json", "finding-ledger.json", "lesson-ledger.json",
    "module-coverage.md", "prep-record.md", "startup-todo.md",
}
_GIT_HOST_GENERATED_DIRS = {
    ".claude", ".zcode",
    ".workbuddy", ".codebuddy", ".pi", ".dsh", ".agents",
} | set(host_workspace_dirs())


def _pp():
    return sys.modules.get("core.launch_phases.post_phase") or sys.modules.get("launch_phases.post_phase")


def _get_pp_attr(name: str, fallback=None):
    pp = _pp()
    if pp and hasattr(pp, name):
        return getattr(pp, name)
    return fallback


class EngineRepoGitWriteBlocked(RuntimeError):
    """测试上下文试图对 BugX 引擎仓本身执行 git 写操作（F-RSI-20261110-002）。"""


def _running_in_test_context() -> bool:
    """进程内测试特征：unittest/pytest 已加载，或测试 runner 注入的环境标记。"""
    if os.environ.get("BUGX_TEST_CONTEXT") == "1":
        return True
    return "unittest" in sys.modules or "pytest" in sys.modules


def _engine_repo_root() -> Path | None:
    """本进程正在运行的引擎根（core/bugx.md 所在目录）。

    pip 安装副本里同样成立（share/bugx-agent/core 完整存在），因此无论
    源码直跑还是快照运行，引擎仓身份都由「本模块实际所在位置」判定。
    """
    engine_root = Path(__file__).resolve().parents[2]
    if (engine_root / "core" / "bugx.md").is_file():
        return engine_root
    return None


def _is_engine_repo(root: Path) -> bool:
    """root 是否为 BugX 引擎仓自身。

    判据必须是「root == 本进程代码所在的引擎根」（resolve 后比对），而
    不能用「root 下存在引擎资产文件」——测试沙箱项目的 copytree 模板
    含全套引擎拷贝（双文件存在性判定会把每个测试项目误判成引擎仓，
    build.435 实测 test_launch_bugx 大面积误拦）。
    """
    engine_root = _engine_repo_root()
    if engine_root is None:
        return False
    try:
        # 两边都 resolve：root 常含 /tmp→/private/tmp 类系统别名，而
        # 可注入的引擎根（测试 mock）未必预先归一化。
        return Path(root).resolve() == engine_root.resolve()
    except OSError:
        return False


def engine_repo_git_write_guard(root: Path) -> None:
    """工作树安全护栏：测试上下文禁止对引擎仓本身执行 git 写。

    F-RSI-20261110-002 实锤事故链：测试的 ``resolve_project_root`` mock
    覆盖不全 → 引擎以主仓为 root 跑真实 post → ``_commit_boundary`` 把
    用户暂存区当 round 变更自动 commit、``rollback_finding_touched_files``
    对主仓执行 checkout 销毁未提交改动。此护栏在「测试进程（unittest/
    pytest/BUGX_TEST_CONTEXT）× root 为引擎仓」时 fail-closed 抛
    :class:`EngineRepoGitWriteBlocked`；真实宿主进程与非引擎仓的测试沙箱
    不受影响。确需放行时显式设置
    ``BUGX_TEST_ALLOW_ENGINE_REPO_GIT_WRITE=1``。
    """
    if os.environ.get("BUGX_TEST_ALLOW_ENGINE_REPO_GIT_WRITE") == "1":
        return
    if _running_in_test_context() and _is_engine_repo(root):
        raise EngineRepoGitWriteBlocked(
            "测试上下文禁止写 BugX 引擎仓 git（root="
            f"{Path(root).resolve()}）：测试 root 泄漏到主仓会自动提交/回滚"
            "用户未提交改动。测试应在 tempfile 沙箱 root 上运行，"
            "或用 BUGX_PROJECT_ROOT 钉住；确需放行设 "
            "BUGX_TEST_ALLOW_ENGINE_REPO_GIT_WRITE=1。"
        )


def git_failure(result: Any) -> str:
    text = (getattr(result, "stderr", None) or getattr(result, "stdout", None) or "").strip().replace("\x00", " ")
    rc = getattr(result, "returncode", "unknown")
    return text[-400:] or f"git exit code {rc}"


def git_paths(text: str) -> list[str]:
    return [item for item in text.split("\x00") if item]


def is_project_change(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if (not normalized or normalized.startswith("/") or normalized == ".."
            or normalized.startswith("../") or "/../" in normalized):
        return False
    if normalized == ".bugx" or normalized.startswith(".bugx/"):
        return False
    parts = normalized.split("/")
    if parts[0] in _GIT_HOST_GENERATED_DIRS or "__pycache__" in parts:
        return False
    name = normalized.rsplit("/", 1)[-1]
    if (name in _GIT_RUNTIME_NAMES or name.startswith("findings_round")
            or name.lower().endswith((".pyc", ".pyo"))):
        return False
    return True


def write_fix_review_draft(runtime: dict, round_result: dict) -> Path | None:
    """Record receipt-backed fix work without creating a terminal prompt gate."""
    if int(round_result.get("credited", 0) or 0) <= 0:
        return None
    if runtime.get("realized_fix_incidents"):
        return None
    root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
    root = root_fn()
    subp = _get_pp_attr("subprocess", _real_subprocess)
    is_change_fn = _get_pp_attr("_is_project_change", is_project_change)
    settle_path_fn = _get_pp_attr("_settle_findings_path", settle_findings_path)
    fc = _get_pp_attr("finding_confirmation", finding_confirmation)

    try:
        changed = subp.run(
            ["git", "diff", "--name-only", "-z", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=_GIT_COMMIT_TIMEOUT,
        )
        untracked = subp.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root,
            capture_output=True, text=True, timeout=_GIT_COMMIT_TIMEOUT,
        )
    except (OSError, getattr(subp, "TimeoutExpired", TimeoutError)):
        return None
    if changed.returncode != 0 or untracked.returncode != 0:
        return None
    paths = sorted({
        path for text in (changed.stdout, untracked.stdout)
        for path in git_paths(text) if is_change_fn(path)
    })
    if not paths:
        return None
    try:
        diff_stat_result = subp.run(
            ["git", "diff", "--stat", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=_GIT_COMMIT_TIMEOUT,
        )
        diff_summary = (diff_stat_result.stdout or "")[-4000:]
    except (OSError, getattr(subp, "TimeoutExpired", TimeoutError)):
        diff_summary = ""
    findings: list[dict[str, Any]] = []
    assurance_rows: list[dict[str, Any]] = []
    execution_receipts: list[dict[str, Any]] = []
    try:
        findings_path = settle_path_fn(runtime)
        if findings_path and Path(findings_path).is_file():
            payload = json.loads(Path(findings_path).read_text(encoding="utf-8"))
            raw_findings = payload.get("findings", []) if isinstance(payload, dict) else []
            for finding in raw_findings:
                if isinstance(finding, dict):
                    confirmation = finding.get("confirmation")
                    if isinstance(confirmation, dict) and confirmation.get("state") == "confirmed":
                        findings.append({
                            "fingerprint": finding.get("fingerprint") or finding.get("id"),
                            "title": finding.get("title") or finding.get("summary"),
                            "severity": finding.get("severity"),
                        })
                        try:
                            assurance = fc.confirmation_assurance(finding) if fc else {"score": 0, "level": "unavailable"}
                        except Exception:
                            assurance = {"score": 0, "level": "unavailable"}
                        assurance_rows.append({
                            "fingerprint": finding.get("fingerprint") or finding.get("id"),
                            "assurance": assurance,
                        })
                        receipt = finding.get("evidence_receipt")
                        if isinstance(receipt, dict):
                            execution_receipts.append({
                                key: receipt[key]
                                for key in (
                                    "execution_id", "lease_id", "worker_id",
                                    "artifact_path", "artifact_sha256", "command", "status",
                                )
                                if key in receipt
                            })
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        findings = []
    draft = {
        "schema_version": 1,
        "event_type": "fix-review-draft",
        "status": "auto_recorded",
        "lifecycle_id": runtime.get("lifecycle_id"),
        "round": round_result.get("round"),
        "credited": int(round_result.get("credited", 0) or 0),
        "findings": findings,
        "changed_files": paths,
        "diff_summary": diff_summary,
        "verification_tests": [
            path for path in paths
            if "/test" in f"/{path}" or path.startswith("test_") or path.endswith("_test.py")
        ],
        "manual_confirmation_required": False,
        "settlement_flow": "execution_receipt_assurance_weighted",
        "assurance": assurance_rows,
        "execution_receipts": execution_receipts,
    }
    try:
        uses_layout_fn = _get_pp_attr("uses_project_layout", uses_project_layout)
        state_dir = bugx_paths.require(root).state_root if uses_layout_fn() else Path(root) / ".bugx" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        import core.secure_atomic as secure_atomic
        path = state_dir / "fix-review-draft.json"
        secure_atomic.atomic_write_json(path, draft)
        return path
    except (OSError, ValueError, RuntimeError):
        return None


def commit_project_changes(
    root: Path,
    boundary: str,
    message: str,
    *,
    excluded_paths=None,
    touched_files=None,
) -> dict[str, Any]:
    """Commit only user project changes; runtime artifacts never enter Git."""
    excluded = {str(item) for item in (excluded_paths or [])}
    touched = {str(item) for item in (touched_files or [])} or None
    subp = _get_pp_attr("subprocess", _real_subprocess)
    fail_fn = _get_pp_attr("_git_failure", git_failure)
    is_change_fn = _get_pp_attr("_is_project_change", is_project_change)
    engine_repo_git_write_guard(root)

    try:
        probe = subp.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_COMMIT_TIMEOUT,
        )
    except OSError as exc:
        return {"status": "failed", "error": f"无法启动 git：{exc}"}
    except getattr(subp, "TimeoutExpired", TimeoutError):
        return {"status": "failed", "error": "git 仓库探测超时"}
    if probe.returncode != 0:
        uses_layout_fn = _get_pp_attr("uses_project_layout", uses_project_layout)
        if not uses_layout_fn():
            return {"status": "skipped_no_git", "boundary": boundary}
        return {"status": "failed", "error": fail_fn(probe)}
    try:
        head = subp.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_COMMIT_TIMEOUT,
        )
        tracked = subp.run(
            ["git", "diff", "--name-only", "-z"]
            + (["HEAD"] if head.returncode == 0 else []) + ["--"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_COMMIT_TIMEOUT,
        )
        staged_base = None
        if head.returncode != 0:
            staged_base = subp.run(
                ["git", "diff", "--cached", "--name-only", "-z", "--"], cwd=root,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_GIT_COMMIT_TIMEOUT,
            )
        untracked = subp.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_COMMIT_TIMEOUT,
        )
    except (OSError, getattr(subp, "TimeoutExpired", TimeoutError)) as exc:
        return {"status": "failed", "error": f"Git 变更扫描失败：{exc}"}
    if tracked.returncode != 0:
        return {"status": "failed", "error": fail_fn(tracked)}
    if staged_base is not None and staged_base.returncode != 0:
        return {"status": "failed", "error": fail_fn(staged_base)}
    if untracked.returncode != 0:
        return {"status": "failed", "error": fail_fn(untracked)}
    paths = sorted({path for path in (
                    *git_paths(tracked.stdout),
                    *(git_paths(staged_base.stdout) if staged_base is not None else []),
                    *git_paths(untracked.stdout),
                )
                    if is_change_fn(path)})
    if excluded:
        paths = [path for path in paths if path not in excluded]
    if touched is not None:
        paths = [path for path in paths if path in touched]
    if not paths:
        return {"status": "no_changes", "boundary": boundary}
    try:
        staged = subp.run(
            ["git", "--literal-pathspecs", "add", "--all", "--", *paths], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_COMMIT_TIMEOUT,
        )
        if staged.returncode != 0:
            return {"status": "failed", "error": fail_fn(staged)}
        staged_diff = subp.run(
            ["git", "--literal-pathspecs", "diff", "--cached", "--quiet", "--", *paths], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_COMMIT_TIMEOUT,
        )
        if staged_diff.returncode == 0:
            return {"status": "no_changes", "boundary": boundary}
        if staged_diff.returncode != 1:
            return {"status": "failed", "error": fail_fn(staged_diff)}
        committed = subp.run(
            ["git", "--literal-pathspecs", "commit", "--only", "-m", message, "--", *paths], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_COMMIT_TIMEOUT,
        )
        if committed.returncode != 0:
            return {"status": "failed", "error": fail_fn(committed)}
        ref = subp.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_COMMIT_TIMEOUT,
        )
        return {
            "status": "committed", "boundary": boundary,
            "ref": ref.stdout.strip() if ref.returncode == 0 else None,
        }
    except (OSError, getattr(subp, "TimeoutExpired", TimeoutError)) as exc:
        return {"status": "failed", "error": f"Git 提交失败：{exc}"}


def _backup_worktree_file(root: Path, path: Path, backup_root: Path, normalized: str) -> None:
    """回滚销毁前，把工作树当前版本备份到 .bugx/rollback-backups/（可恢复兜底）。

    DOC-176 注册表误删事故后的硬红线：任何对工作树的 checkout/unlink 销毁都必须
    先留物理备份；备份失败抛 OSError，由调用方 fail-closed，禁止带病销毁。
    """
    if not (path.is_file() or path.is_symlink()):
        return
    dest = backup_root / normalized
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest, follow_symlinks=False)


def rollback_finding_touched_files(
    root: Path,
    finding_touched_paths,
    *,
    startup_dirty_paths=None,
) -> dict[str, Any]:
    """撤销 finding 修复对指定文件的触碰 (DOC-181 安全自愈契约)。

    回滚安全红线：``git checkout HEAD --`` / ``unlink`` 会静默销毁工作树未提交
    改动，因此销毁前必须先把当前版本备份到 ``root/.bugx/rollback-backups/<时刻>/``；
    备份失败立即 fail-closed 返回 ``failed``，绝不带病销毁。返回值额外携带
    ``backups``（已备份的相对路径）与 ``backup_root``（备份目录）供恢复与审计。
    """
    engine_repo_git_write_guard(root)
    startup_dirty = {str(item) for item in (startup_dirty_paths or set())}
    reverted: list[str] = []
    backups: list[str] = []
    subp = _get_pp_attr("subprocess", _real_subprocess)
    fail_fn = _get_pp_attr("_git_failure", git_failure)
    root_path = Path(root)
    backup_root = root_path / ".bugx" / "rollback-backups" / time.strftime(
        "%Y%m%dT%H%M%S", time.gmtime(),
    )

    for rel in sorted({str(item) for item in (finding_touched_paths or [])}):
        if not rel or rel in startup_dirty:
            continue
        normalized = rel.replace("\\", "/")
        if (normalized.startswith("/") or normalized == ".."
                or normalized.startswith("../") or "/../" in normalized):
            continue
        path = root_path / normalized
        try:
            in_head = subp.run(
                ["git", "cat-file", "-e", f"HEAD:{normalized}", "--"],
                cwd=root, capture_output=True, text=True, timeout=_GIT_COMMIT_TIMEOUT,
            )
            if in_head.returncode == 0:
                # HEAD 存在该文件，但工作树可能带着用户未提交改动——先备份再回滚
                _backup_worktree_file(root_path, path, backup_root, normalized)
                backups.append(normalized)
                restored = subp.run(
                    ["git", "--literal-pathspecs", "checkout", "HEAD", "--", normalized],
                    cwd=root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=_GIT_COMMIT_TIMEOUT,
                )
                if restored.returncode != 0:
                    return {"status": "failed", "error": fail_fn(restored)}
            elif path.exists():
                _backup_worktree_file(root_path, path, backup_root, normalized)
                backups.append(normalized)
                path.unlink()
            else:
                continue
            reverted.append(normalized)
        except (OSError, getattr(subp, "TimeoutExpired", TimeoutError)) as exc:
            return {
                "status": "failed",
                "error": f"Git 回滚失败（工作树已保全，未销毁）：{exc}",
                "backups": backups,
                "backup_root": str(backup_root),
            }
    return {
        "status": "reverted",
        "reverted": reverted,
        "backups": backups,
        "backup_root": str(backup_root),
    }


def record_commit_result(boundary: str, result: dict[str, Any]) -> dict:
    with runtime_lock():
        state = load_runtime()
        status = str(result.get("status") or "failed")
        changed = (
            state.get("commit_status") != status
            or state.get("commit_boundary") != boundary
            or state.get("commit_ref") != result.get("ref")
            or state.get("commit_error") != result.get("error")
        )
        state.update({
            "commit_status": status,
            "commit_boundary": boundary,
            "commit_ref": result.get("ref") if status == "committed" else None,
            "commit_error": result.get("error"),
        })
        if changed:
            detail = f"{boundary}:{status}"
            if result.get("error"):
                detail += f" {result['error']}"
            append_runtime_event(state, "git-commit", time.time(), detail[-600:])
        save_runtime(state)
    return state


def commit_boundary(runtime: dict, round_result: dict, *, final: bool) -> tuple[dict, bool]:
    policy = runtime.get("commit_policy") or "manual"
    life_after = round_result.get("life_after")
    round_due = final or (isinstance(life_after, int) and life_after <= 0) \
        or bool(runtime.get("lifecycle_stop_reason"))
    boundary = "round" if round_due else "life"
    record_fn = _get_pp_attr("_record_commit_result", record_commit_result)
    if policy == "life":
        due = True
    elif policy == "round":
        due = round_due
    else:
        status = "not_applicable" if policy == "not_applicable" else "manual_pending"
        return record_fn(boundary, {"status": status}), True
    if not due:
        return record_fn(boundary, {"status": "pending_round"}), True
    engine_repo_git_write_guard(_get_pp_attr("resolve_project_root", resolve_project_root)())
    message = (
        f"bugx({boundary}): complete "
        f"Round {runtime.get('lifecycle_no', '?')}"
        + (f" Life {runtime.get('lifecycle_cycles_completed', 0)}" if boundary == "life" else "")
    )
    startup_dirty = [
        str(item) for item in (runtime.get("startup_git_dirty_paths") or [])
        if isinstance(item, str) and item
    ]
    touched_files = [
        str(item) for item in (
            runtime.get("realized_fix_touched_files")
            or runtime.get("touched_files")
            or []
        )
        if isinstance(item, str) and item
    ] or None
    commit_kwargs = {}
    if startup_dirty:
        commit_kwargs["excluded_paths"] = startup_dirty
    if touched_files:
        commit_kwargs["touched_files"] = touched_files

    commit_proj_fn = _get_pp_attr("_commit_project_changes", commit_project_changes)
    root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
    result = commit_proj_fn(root_fn(), boundary, message, **commit_kwargs)
    if result.get("status") == "failed":
        result = {**result, "status": "pending"}
    state = record_fn(boundary, result)
    if result.get("status") == "pending":
        print(f"⚠ Git {boundary} 提交待恢复：{state.get('commit_error', '未知错误')}")
        print("  本地挖掘继续；下一边界或下次启动会自动重试 Git 提交。")
    if result.get("status") == "committed":
        print(f"✓ Git {boundary} 提交完成：{result.get('ref') or 'HEAD'}")
        try:
            from core.supervisor import supervisor_models
            supervisor_models.invalidate_git_head_cache(root_fn())
        except (ImportError, OSError, ValueError):
            pass
    elif result.get("status") == "no_changes":
        print(f"✓ Git {boundary} 无项目变更，跳过空提交。")
    elif result.get("status") == "skipped_no_git":
        print(f"⚠ Git {boundary} 跳过：当前为无项目布局的兼容运行。")
    return state, True


def retry_pending_commit() -> dict[str, Any]:
    """Retry a deferred Git commit without opening mining."""
    state = load_runtime()
    if state.get("commit_status") != "pending":
        return state
    boundary = str(state.get("commit_boundary") or "round")
    if boundary not in {"life", "round"}:
        return state
    commit_proj_fn = _get_pp_attr("_commit_project_changes", commit_project_changes)
    record_fn = _get_pp_attr("_record_commit_result", record_commit_result)
    root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
    result = commit_proj_fn(
        root_fn(), boundary,
        f"bugx({boundary}): recover deferred commit",
    )
    if result.get("status") == "failed":
        result = {**result, "status": "pending"}
    state = record_fn(boundary, result)
    if result.get("status") == "committed":
        print(f"✓ 已恢复 Git {boundary} 提交：{result.get('ref') or 'HEAD'}")
    elif result.get("status") == "pending":
        print(f"⚠ Git {boundary} 仍待恢复：{state.get('commit_error', '未知错误')}")
    return state
