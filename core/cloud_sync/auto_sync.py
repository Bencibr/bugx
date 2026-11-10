#!/usr/bin/env python3
"""提交后自动云同步：合并触发、断网退避恢复，不承担新的同步协议。"""
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
import sys
from pathlib import Path


import argparse
import json
import logging
import os
import re
import subprocess
import time
from typing import Any

AUTOSYNC_WORKER_ENV = "BUGX_CLOUD_AUTOSYNC_WORKER"
DEBOUNCE_SECONDS = 0.35
MAX_BATCHES = 4
RECOVERY_DELAYS = (30, 60, 120, 240, 480, 900, 1800)
RECOVERY_CODES = {
    "SYNC_NETWORK_UNAVAILABLE", "SYNC_SERVER_UNAVAILABLE",
    "SYNC_MAINTENANCE", "SYNC_RATE_LIMITED", "SYNC_RETRY_BACKOFF",
}

#: 锁的绝对陈旧阈值（LOG-02）。它是**兜底**而非主要判据：worker 的最长退避
#: 序列累计 sum(RECOVERY_DELAYS)=3630s，再叠加每批次的同步耗时，因此阈值必须
#: ≥ 该序列，否则正在重试的 worker 会被误判为陈旧并被抢占。真正的判据是锁内
#: 记录的 pid 是否存活——持有者活着就绝不抢占。
LOCK_MAX_AGE_SECONDS = float(sum(RECOVERY_DELAYS) + 900)

CORE_ROOT = Path(__file__).resolve().parents[1]
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))


def _pid_alive(pid: int) -> bool:
    """判定 pid 是否存活；无法判定时按「存活」处理（绝不误抢锁）。

    方向性很重要：误抢会让同一 project/scope 出现两个 autosync worker，
    造成重复 push/ack；而多等一轮只是延迟。因此所有不确定分支都返回 True。
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _ensure_dir(path: Path) -> bool:
    """创建目录；已存在即视为成功（不依赖 mkdir(exist_ok=True) 的幂等性）。"""
    try:
        if path.is_dir():
            return True
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _project_root_for_ledger(path: Path) -> tuple[Path | None, str | None]:
    """由受控账本路径确定项目/global scope，不从文件内容推断边界。"""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return None, None
    global_knowledge = (Path.home() / ".bugx" / "knowledge").resolve()
    if resolved.parent == global_knowledge:
        root = Path.cwd().resolve()
        return (root, "global") if (root / ".bugx").is_dir() else (None, None)
    for parent in resolved.parents:
        if parent.name == ".bugx" and parent / "knowledge" == resolved.parent:
            return parent.parent, "project"
    return None, None


def _onboarding_done(root: Path) -> bool:
    path = root / ".bugx" / "cloud-onboard.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(value, dict) and value.get("state") == "done"


def _project_bound(root: Path) -> bool:
    identity = root / ".bugx-project.json"
    binding = root / ".bugx" / "cloud-project.json"
    if identity.is_symlink() or binding.is_symlink() or not identity.is_file() or not binding.is_file():
        return False
    try:
        value = json.loads(binding.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(value, dict) and bool(value.get("project_id"))


def _eligible(root: Path | None, scope: str) -> bool:
    if root is None or scope not in {"global", "project"} or not _onboarding_done(root):
        return False
    # F1：用户级显式撤销优先于项目 onboarding 状态，后台 autosync 必须同样停机。
    from core.cloud_sync.consent import sharing_revoked
    if sharing_revoked():
        return False
    return scope == "global" or _project_bound(root)


def _lock_path(root: Path, scope: str) -> Path:
    # One lock per project/scope keeps global and project payloads independent.
    return root / ".bugx" / "cloud-sync" / f"autosync-{scope}.lock"


def _lock_owner_token(path: Path) -> str | None:
    """读取锁文件里记录的持有者令牌；无法读取时返回 None。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    return token if isinstance(token, str) and token else None


def _lock_is_stale(path: Path) -> bool:
    """锁是否可被抢占。

    主判据是「持有者进程已消失」——死进程永远不会自己释放锁。只有拿不到
    pid（锁内容损坏）时才退回年龄阈值，避免把正在重试的活 worker 抢掉。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        pid = payload.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            if pid == os.getpid():
                # 自己持有的锁：绝不「抢占」，否则会与自己并发。
                return False
            if _pid_alive(pid):
                return False
            return True
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age > LOCK_MAX_AGE_SECONDS


def _acquire_lock(path: Path) -> str | None:
    """尝试取得锁，成功返回持有者令牌（用于安全释放），失败返回 None。"""
    if path.is_symlink():
        return None
    if not _ensure_dir(path.parent):
        return None
    if path.parent.is_symlink():
        return None
    token = os.urandom(16).hex()
    try:
        fd = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        if _lock_is_stale(path):
            try:
                path.unlink()
            except OSError:
                return None
            return _acquire_lock(path)
        return None
    except OSError:
        return None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": os.getpid(), "created_at": time.time(), "token": token},
                handle,
            )
        return token
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _release_lock(path: Path, token: str | None = None) -> None:
    """释放锁；只在令牌匹配时删除，避免删掉别人的锁（LOG-02）。

    旧实现无条件 ``unlink``：worker A 被误判陈旧、锁被 B 抢占后，A 结束时会把
    **B 持有的锁**删掉，于是 C 又能进来，形成重复 push/ack。令牌比对把释放
    收窄为「只释放自己仍持有的那一把」。
    """
    if token is not None and _lock_owner_token(path) != token:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def schedule_recovery(root: Path, scope: str) -> bool:
    """调度一个与挖掘解耦的、可恢复的云同步 worker。"""
    if os.environ.get(AUTOSYNC_WORKER_ENV) == "1":
        return False
    if not _eligible(root, scope or ""):
        return False
    lock = _lock_path(root, scope or "")
    token = _acquire_lock(lock)
    if token is None:
        return False
    command = [
        sys.executable, str(Path(__file__).resolve()), "--worker",
        "--root", str(root), "--scope", str(scope), "--token", token,
    ]
    environment = os.environ.copy()
    environment[AUTOSYNC_WORKER_ENV] = "1"
    try:
        subprocess.Popen(
            command, cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True, env=environment,
        )
        return True
    except (OSError, ValueError):
        _release_lock(lock, token)
        return False


def schedule_ledger_sync(path: str | os.PathLike[str]) -> bool:
    """在账本提交后快速调度同步；返回是否启动了 worker。"""
    root, scope = _project_root_for_ledger(Path(path))
    if root is None or scope is None:
        return False
    return schedule_recovery(root, scope)


def _ledger_path(root: Path, scope: str) -> Path:
    if scope == "global":
        return Path.home() / ".bugx" / "knowledge" / "global.json"
    return root / ".bugx" / "knowledge" / "lesson-ledger.json"


def _ledger_state_hash(root: Path, scope: str) -> str:
    try:
        from core.ledger import lesson_ledger
        value = lesson_ledger.read(_ledger_path(root, scope))
        return str(value.get("state_hash") or "")
    except Exception:
        return ""


def _recovery_logger(root: Path, scope: str):
    try:
        from core.runtime import bugx_logging
        return bugx_logging, bugx_logging.configure(
            root, command=f"cloud-recovery-{scope}"
        )
    except (ImportError, OSError, ValueError):
        return None, None


def _log(logger, event: str, message: str = "", *,
         level: int = logging.INFO, **fields: Any) -> None:
    module, target = logger
    if module is not None and target is not None:
        module.log_event(target, event, message, level=level, **fields)


def _summary_error_code(summary: object) -> str:
    text = json.dumps(summary, ensure_ascii=False, default=str)
    match = re.search(r"SYNC_[A-Z0-9_]+", text)
    return match.group(0) if match else "SYNC_UNKNOWN_ERROR"


def _retryable_summary(summary: object) -> bool:
    return _summary_error_code(summary) in RECOVERY_CODES


def _drain_flywheel_if_enabled(logger: tuple[Any, Any]) -> None:
    try:
        from core.cloud_sync.flywheel_client import FlywheelUploader
        uploader = FlywheelUploader()
        if getattr(uploader, "data_sharing", False) and uploader.outbox.count() > 0:
            flushed = uploader.flush(limit=20, batch_size=20)
            sent = flushed.get("sent", 0) if isinstance(flushed, dict) else 0
            if sent > 0:
                _log(logger, "cloud_flywheel_drained", sent=sent, level=logging.INFO)
    except Exception:
        pass


def run_worker(root: Path, scope: str, token: str | None = None) -> None:
    """在后台有限退避重放；挖掘主进程不等待网络。"""
    lock = _lock_path(root, scope)
    logger = _recovery_logger(root, scope)
    try:
        time.sleep(DEBOUNCE_SECONDS)
        if not _eligible(root, scope):
            return
        _log(logger, "cloud_recovery_started", scope=scope)
        from core.cloud_sync.cloud_client import CloudClient
        client = CloudClient()
        auth_state = getattr(client.cred, "auth_state", lambda: "missing")()
        if auth_state != "valid":
            _log(logger, "cloud_recovery_skipped", "认证不可用", scope=scope,
                 auth_state=auth_state)
            return
        from core.cloud_sync.cloud_onboard import sync_layers
        successful_batches = 0
        for attempt in range(len(RECOVERY_DELAYS) + 1):
            if attempt:
                delay = RECOVERY_DELAYS[attempt - 1]
                _log(logger, "cloud_recovery_retry", scope=scope,
                     attempt=attempt + 1, retry_after_seconds=delay)
                time.sleep(delay)
            before = _ledger_state_hash(root, scope)
            summary: dict[str, Any] = sync_layers(client, root, scopes={scope})
            if summary.get("ok") is not True:
                code = _summary_error_code(summary)
                _log(logger, "cloud_recovery_failed",
                     str(summary.get("error") or code), level=40,
                     scope=scope, attempt=attempt + 1, error_code=code)
                if not _retryable_summary(summary):
                    return
                if attempt >= len(RECOVERY_DELAYS):
                    _log(logger, "cloud_recovery_exhausted", scope=scope,
                         attempts=attempt + 1, error_code=code, level=logging.ERROR)
                    return
                continue
            after = _ledger_state_hash(root, scope)
            successful_batches += 1
            if before == after:
                _drain_flywheel_if_enabled(logger)
                _log(logger, "cloud_recovery_succeeded", scope=scope,
                     attempts=attempt + 1, status="drained")
                return
            if successful_batches >= MAX_BATCHES:
                _drain_flywheel_if_enabled(logger)
                _log(logger, "cloud_recovery_succeeded", scope=scope,
                     attempts=attempt + 1, status="batch_limit")
                return
    except Exception:
        _log(logger, "cloud_recovery_crashed", scope=scope, level=40,
             exc_info=True)
        return
    finally:
        _release_lock(lock, token)
        module, _ = logger
        if module is not None:
            try:
                module.close_handlers_for_root(root)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BugX automatic cloud sync worker")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--root", required=True)
    parser.add_argument("--scope", choices=("global", "project"), required=True)
    parser.add_argument("--token", default=None,
                        help="调度方持有的锁令牌；仅用于安全释放自己的锁")
    args = parser.parse_args(argv)
    if not args.worker:
        return 2
    run_worker(Path(args.root).resolve(), args.scope, args.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
