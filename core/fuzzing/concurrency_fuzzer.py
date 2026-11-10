#!/usr/bin/env python3
"""core/concurrency_fuzzer.py - 并发竞态与异步时序死锁专项探针。

提供轻量级调度扰动（微秒级抢占窗口）与死锁/数据竞态 Oracle，
专门捕获多线程/并发环境下的 TOCTOU、锁乱序死锁与共享内存脏读写。
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
from collections import defaultdict
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Sequence

_CORE_DIR = Path(__file__).resolve().parents[1]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from core.supervisor import worker_execution


CONCURRENCY_CRASH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"fatal error: all goroutines are asleep - deadlock!", re.I), "GOROUTINE_DEADLOCK"),
    (re.compile(r"fatal error: concurrent map (?:read and map )?write", re.I), "GO_CONCURRENT_MAP_RACE"),
    (re.compile(r"OperationalError: database (?:is locked|table is locked)", re.I), "SQLITE_LOCK_CONTENTION"),
    (re.compile(r"ConcurrentModificationException", re.I), "JAVA_CONCURRENT_MODIFICATION"),
    (re.compile(r"WARNING: ThreadSanitizer: data race", re.I), "TSAN_DATA_RACE"),
    (re.compile(r"deadlock detected", re.I), "DATABASE_DEADLOCK"),
    (re.compile(r"Segmentation fault", re.I), "SEGMENTATION_FAULT_RACE"),
]


class ConcurrencyCrashError(RuntimeError):
    """由已知并发竞争或死锁特征模式触发的异常。"""


class LockInversionDetector:
    """锁获取顺序反转（Lock Ordering Inversion / ABBA 潜在死锁）有向图环路检测器。"""

    def __init__(self):
        self._thread_locks: dict[int, list[str]] = defaultdict(list)
        self._lock_graph: dict[str, set[str]] = defaultdict(set)
        self._violations: list[dict[str, Any]] = []
        self._mu = threading.Lock()

    def record_acquire_attempt(self, thread_id: int, lock_name: str) -> list[str] | None:
        """记录线程尝试获取锁。若同线程已有持锁且构成有向环路，记录并返回环路。"""
        with self._mu:
            held = self._thread_locks[thread_id]
            cycle = None
            if held:
                last_held = held[-1]
                if last_held != lock_name:
                    self._lock_graph[last_held].add(lock_name)
                    cycle = self._detect_cycle()
                    if cycle:
                        violation = {
                            "thread_id": thread_id,
                            "held_locks": list(held),
                            "attempted_lock": lock_name,
                            "cycle": cycle,
                            "description": f"检测到潜在死锁锁逆序环路: {' -> '.join(cycle)}",
                        }
                        if violation not in self._violations:
                            self._violations.append(violation)
            self._thread_locks[thread_id].append(lock_name)
            return cycle

    def record_release(self, thread_id: int, lock_name: str) -> None:
        """记录线程释放锁。"""
        with self._mu:
            held = self._thread_locks[thread_id]
            if lock_name in held:
                held.remove(lock_name)

    def _detect_cycle(self) -> list[str] | None:
        """DFS 有向图环路检测。"""
        visited: set[str] = set()
        rec_stack: list[str] = []

        def dfs(node: str) -> list[str] | None:
            visited.add(node)
            rec_stack.append(node)
            for neighbor in self._lock_graph.get(node, ()):
                if neighbor not in visited:
                    res = dfs(neighbor)
                    if res:
                        return res
                elif neighbor in rec_stack:
                    idx = rec_stack.index(neighbor)
                    return rec_stack[idx:] + [neighbor]
            rec_stack.pop()
            return None

        for n in list(self._lock_graph.keys()):
            if n not in visited:
                c = dfs(n)
                if c:
                    return c
        return None

    @property
    def violations(self) -> list[dict[str, Any]]:
        with self._mu:
            return list(self._violations)


class SchedulePerturber:
    """并发调度扰动注入器。"""

    def __init__(self, switch_interval: float = 1e-5, jitter_range: tuple[float, float] = (1e-6, 1e-5)):
        self.switch_interval = switch_interval
        self.jitter_range = jitter_range
        self._orig_interval: float | None = None

    def enable(self) -> None:
        """激活极短上下文切换间隔，最大化线程交错窗口。"""
        if hasattr(sys, "getswitchinterval") and hasattr(sys, "setswitchinterval"):
            self._orig_interval = sys.getswitchinterval()
            sys.setswitchinterval(self.switch_interval)

    def disable(self) -> None:
        """恢复原有上下文切换间隔。"""
        if self._orig_interval is not None and hasattr(sys, "setswitchinterval"):
            sys.setswitchinterval(self._orig_interval)
            self._orig_interval = None

    @contextmanager
    def active(self):
        self.enable()
        try:
            yield
        finally:
            self.disable()

    def jitter(self) -> None:
        """在并发敏感路径注入微秒级随机抖动。"""
        low, high = self.jitter_range
        # 使用轻量伪随机或时间纳秒作为抖动偏置
        duration = low + ((time.perf_counter_ns() % 1000) / 1000.0) * (high - low)
        time.sleep(duration)


class ConcurrencyOracle:
    """并发竞争与死锁探测评测机。"""

    def __init__(self, perturber: SchedulePerturber | None = None):
        self.perturber = perturber or SchedulePerturber()

    def fuzz_callable(
        self,
        func: Callable[[], Any],
        *,
        concurrency: int = 8,
        iterations: int = 50,
        timeout_per_iter: float = 2.0,
    ) -> dict[str, Any]:
        """对目标无参可调用对象进行高并发压测扰动。"""
        self.perturber.enable()
        try:
            for iteration in range(1, iterations + 1):
                barrier = threading.Barrier(concurrency)
                exceptions: list[Exception] = []
                lock = threading.Lock()

                def worker():
                    try:
                        barrier.wait(timeout=timeout_per_iter)
                        self.perturber.jitter()
                        func()
                    except Exception as exc:
                        with lock:
                            exceptions.append(exc)

                # daemon=True：检测到死锁后，卡死的工作线程无法被杀死
                # （纯 Python 无线程终止原语），非 daemon 线程会永久阻塞
                # 进程退出——正是本探针曾导致 runner 600s 超时的根因。
                threads = [
                    threading.Thread(target=worker, daemon=True)
                    for _ in range(concurrency)
                ]
                for t in threads:
                    t.start()

                for t in threads:
                    t.join(timeout=timeout_per_iter)

                # 检查死锁/挂起
                alive = [t for t in threads if t.is_alive()]
                if alive:
                    return {
                        "race_detected": True,
                        "error_type": "DEADLOCK_OR_HANG",
                        "iteration": iteration,
                        "details": f"{len(alive)} 个工作线程在 {timeout_per_iter}s 内未退出，疑似发生死锁或超时卡死",
                        "alive_threads": len(alive),
                    }

                if exceptions:
                    first = exceptions[0]
                    tb = "".join(traceback.format_exception(type(first), first, first.__traceback__))
                    return {
                        "race_detected": True,
                        "error_type": type(first).__name__,
                        "iteration": iteration,
                        "details": str(first),
                        "stack_trace": tb,
                    }

            return {
                "race_detected": False,
                "error_type": None,
                "iterations_completed": iterations,
                "details": f"在 {iterations} 轮并发压测中未观察到竞态异常",
            }
        finally:
            self.perturber.disable()


def normalize_command(cmd: Sequence[str]) -> list[str]:
    """把命令入参归一化为 argv 数组；字符串命令一律拒绝。

    ``shell=True`` 在本模块被彻底移除——它会让 Worker 传入的字符串获得完整
    shell（命令拼接、管道、重定向），而执行又发生在 MCP server 进程内，宿主
    ``permission.bash`` 的 deny 规则对此完全不生效。因此这里不再接受字符串：
    调用方必须显式提供 argv 数组；确需解析字符串的受信调用方（如 CLI）应先
    用 ``worker_execution.split_argv``，该函数会拒绝 shell 运算符。
    """
    if isinstance(cmd, (str, bytes)):
        raise TypeError("命令必须是 argv 数组（list[str]），禁止字符串命令")
    argv = [str(item) for item in cmd]
    if not argv or any(not item for item in argv):
        raise ValueError("命令必须是非空 argv 数组")
    return argv


def fuzz_command(
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    concurrency: int = 4,
    iterations: int = 10,
    timeout_per_iter: float = 3.0,
    lock_detector: LockInversionDetector | None = None,
) -> dict[str, Any]:
    """对外部命令或可执行测试进行高并发调度扰动压测。

    命令一律以 argv 形式执行（``shell=False``），并统一走
    ``worker_execution.run_bounded``：既获得输出字节上限，也保证每轮压测
    的整组子进程被回收（并发压测本身会放大进程泄漏）。
    """
    argv = normalize_command(cmd)
    workdir = Path(cwd).resolve() if cwd else Path.cwd()
    perturber = SchedulePerturber()
    oracle = ConcurrencyOracle(perturber)

    detected_category: list[str] = []

    def target_call():
        res = worker_execution.run_bounded(
            argv,
            cwd=workdir,
            timeout=timeout_per_iter,
        )
        if res.returncode != 0:
            err_text = f"{res.stderr.strip()}\n{res.stdout.strip()}"
            matched_tag = None
            for pattern, tag in CONCURRENCY_CRASH_PATTERNS:
                if pattern.search(err_text):
                    matched_tag = tag
                    break
            if matched_tag:
                detected_category.append(matched_tag)
                raise ConcurrencyCrashError(f"[{matched_tag}] Exit {res.returncode}: {err_text[:300]}")
            raise RuntimeError(f"Command failed (exit {res.returncode}): {err_text[:300]}")

    report = oracle.fuzz_callable(
        target_call,
        concurrency=concurrency,
        iterations=iterations,
        timeout_per_iter=timeout_per_iter,
    )
    report["command"] = " ".join(argv)
    if detected_category:
        report["concurrency_anomaly_type"] = detected_category[0]
    if lock_detector and lock_detector.violations:
        report["lock_order_violations"] = lock_detector.violations
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BugX 并发时序死锁专项探针")
    parser.add_argument("action", nargs="?", default="check", choices=["check", "run"], help="探针动作")
    parser.add_argument("--root", type=str, default=".", help="项目根目录")
    parser.add_argument("--cmd", type=str, default="", help="要进行并发扰动的命令")
    parser.add_argument("--threads", type=int, default=8, help="并发线程数 (默认: 8)")
    parser.add_argument("--iterations", type=int, default=50, help="扰动压测轮数 (默认: 50)")
    parser.add_argument("--timeout", type=float, default=2.0, help="单轮超时 (秒)")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    perturber = SchedulePerturber()
    oracle = ConcurrencyOracle(perturber)

    if args.cmd:
        # CLI 是受信调用方：字符串命令先经 split_argv 解析为 argv（并拒绝
        # shell 运算符），再进入只接受 argv 数组的 fuzz_command。
        try:
            cmd_argv = worker_execution.split_argv(args.cmd)
        except worker_execution.WorkerExecutionError as exc:
            print(f"[ConcurrencyFuzzer] 非法命令: {exc}", file=sys.stderr)
            return 2
        report = fuzz_command(
            cmd_argv,
            cwd=args.root,
            concurrency=args.threads,
            iterations=args.iterations,
            timeout_per_iter=args.timeout,
        )
    elif args.action == "check":
        has_switch = hasattr(sys, "getswitchinterval") and hasattr(sys, "setswitchinterval")
        report = {
            "status": "READY" if has_switch else "DEGRADED",
            "checks": [
                {
                    "name": "schedule_perturber",
                    "ok": bool(has_switch),
                    "detail": f"sys switch interval: {sys.getswitchinterval():.6f}s" if has_switch else "不支持上下文切换微扰",
                }
            ],
            "capabilities": ["concurrency_fuzzer", "schedule_jitter"],
            "fallbacks": {"concurrency": "标准多线程测试"},
            "warnings": [] if has_switch else ["当前 Python 环境不支持调整 switch interval"],
        }
    else:
        shared_counter = {"val": 0}
        def dummy_task():
            v = shared_counter["val"]
            perturber.jitter()
            shared_counter["val"] = v + 1

        report = oracle.fuzz_callable(
            dummy_task,
            concurrency=args.threads,
            iterations=args.iterations,
            timeout_per_iter=args.timeout,
        )

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        if "status" in report:
            print(f"[ConcurrencyProbe] status={report['status']}")
        else:
            print(f"[ConcurrencyFuzzer] 检测结果: race_detected={report.get('race_detected')}")
            if report.get("race_detected"):
                print(f"  异常类型: {report.get('error_type')}")
                print(f"  细节: {report.get('details')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
