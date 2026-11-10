#!/usr/bin/env python3
"""core/daemon_runner.py - BugX 无人值守常驻后台守护进程与自主 Worker 驱动引擎 (DOC-137 / D137-02)。

提供：
  - 前后台解耦与进程守护: bugx daemon start | stop | status | attach
  - 自治挖掘轮询驱动器: 自动闭环执行 claim -> dispatch -> execute -> checkpoint -> post
  - 故障自愈与硬熔断保护: 超时熔断、僵尸租约清理、进程存活心跳
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
import sys
from pathlib import Path


import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import time
from typing import Any

from core.governance.goal_governor import QuarantineManager

_CORE_DIR = Path(__file__).resolve().parents[1]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

DEFAULT_MAX_WALL_TIME = 14400.0  # 4 小时硬熔断
DEFAULT_LOOP_INTERVAL = 3.0      # 轮询间隔（秒）


def resolve_project_root(root_override: str | Path | None = None) -> Path:
    if root_override:
        return Path(root_override).resolve()
    cwd = Path.cwd().resolve()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".bugx").is_dir() or (parent / ".git").is_dir():
            return parent
    return cwd


def _pid_file(root: Path) -> Path:
    pdir = root / ".bugx"
    pdir.mkdir(parents=True, exist_ok=True)
    return pdir / "daemon.pid"


def _log_file(root: Path) -> Path:
    ldir = root / ".bugx" / "logs"
    ldir.mkdir(parents=True, exist_ok=True)
    return ldir / "daemon.log"


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class DaemonRunner:
    """BugX 后台常驻守护进程执行器。"""

    def __init__(
        self,
        root: Path | str | None = None,
        max_wall_time: float = DEFAULT_MAX_WALL_TIME,
        interval: float = DEFAULT_LOOP_INTERVAL,
        stall_threshold: float = 120.0,
    ) -> None:
        self.root = resolve_project_root(root)
        self.max_wall_time = float(max_wall_time)
        self.interval = float(interval)
        self.stall_threshold = float(stall_threshold)
        self.pid_path = _pid_file(self.root)
        self.log_path = _log_file(self.root)
        self._stop_requested = False
        self.last_progress_time = time.monotonic()
        self.last_covered_count = 0
        self.quarantine = QuarantineManager(max_retries=3)
        self.self_heal_count = 0
        self.last_cost_units = 0.0
        self.last_findings_count = 0
        self.roi_stagnant_cycles = 0
        self.sandbox_manager: Any | None = None

    @property
    def quarantined_units(self) -> set[str]:
        return set(self.quarantine.get_quarantined_list())

    @quarantined_units.setter
    def quarantined_units(self, val: Any) -> None:
        self.quarantine.quarantined = {str(u): "手动指定隔离" for u in (val or ())}

    @property
    def unit_failure_counts(self) -> dict[str, int]:
        return self.quarantine.unit_failures

    @unit_failure_counts.setter
    def unit_failure_counts(self, val: dict[str, int]) -> None:
        self.quarantine.unit_failures = dict(val or {})

    def start(self, foreground: bool = False) -> int:
        """启动后台守护进程。"""
        if self.is_running():
            pid = self.read_pid()
            print(f"[BugX Daemon] 守护进程已在运行中 (PID: {pid})")
            return 0

        if foreground:
            print(f"[BugX Daemon] 前台模式启动 (PID: {os.getpid()})")
            self._write_pid(os.getpid())
            try:
                return self.run_autonomous_loop()
            finally:
                self._remove_pid()

        # 后台子进程派发
        python_bin = sys.executable
        runner_script = Path(__file__).resolve()
        cmd = [
            python_bin, str(runner_script),
            "start", "--foreground",
            "--root", str(self.root),
            "--max-wall-time", str(self.max_wall_time),
            "--interval", str(self.interval),
        ]

        log_out = open(self.log_path, "a", encoding="utf-8")
        try:
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                    close_fds=True,
                )
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            self._write_pid(proc.pid)
            print(f"[BugX Daemon] 守护进程已成功启动 (PID: {proc.pid})")
            print(f"[BugX Daemon] 日志输出路径: {self.log_path}")
            return 0
        finally:
            log_out.close()

    def stop(self, force: bool = False) -> int:
        """停止常驻守护进程。"""
        pid = self.read_pid()
        if not pid or not _is_pid_alive(pid):
            print("[BugX Daemon] 守护进程未在运行")
            self._remove_pid()
            return 0

        print(f"[BugX Daemon] 正在终止守护进程 (PID: {pid})...")
        try:
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.kill(pid, sig)
        except OSError as exc:
            print(f"[BugX Daemon] 发送信号失败: {exc}")

        # 等待退出
        for _ in range(20):
            if not _is_pid_alive(pid):
                break
            time.sleep(0.2)

        if _is_pid_alive(pid) and force:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

        self._remove_pid()
        print("[BugX Daemon] 守护进程已停止")
        return 0

    def status(self, as_json: bool = False) -> int:
        """查询常驻守护进程状态。"""
        pid = self.read_pid()
        alive = bool(pid and _is_pid_alive(pid))
        data = {
            "running": alive,
            "pid": pid if alive else None,
            "root": str(self.root),
            "log_file": str(self.log_path),
            "max_wall_time": self.max_wall_time,
        }

        # 尝试读取 runtime 状态
        runtime_file = self.root / ".bugx" / "bugx-runtime.json"
        if runtime_file.is_file():
            try:
                rt = json.loads(runtime_file.read_text(encoding="utf-8"))
                data["lifecycle_id"] = rt.get("lifecycle_id")
                data["session_id"] = rt.get("session_id")
                data["polling_policy"] = rt.get("polling_policy")
            except Exception:
                pass

        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            state_str = "RUNNING" if alive else "STOPPED"
            print(f"[BugX Daemon] 状态: {state_str}")
            if alive:
                print(f"  PID:          {pid}")
                print(f"  根目录:       {self.root}")
                print(f"  当前 Lifecycle: {data.get('lifecycle_id', 'unknown')}")
                print(f"  日志路径:     {self.log_path}")
        return 0 if alive else 1

    def attach(self) -> int:
        """连接并实时回显守护进程日志。"""
        if not self.log_path.is_file():
            print(f"[BugX Daemon] 日志文件尚未生成: {self.log_path}")
            return 1

        print(f"[BugX Daemon] 正在监听实时日志（按 Ctrl+C 可安全分离，不影响后台）: {self.log_path}")
        print("=" * 60)
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                # 初始输出末尾部分
                lines = f.readlines()
                for line in lines[-20:]:
                    sys.stdout.write(line)
                sys.stdout.flush()

                # 流式 tail
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[BugX Daemon] 已安全分离 (Detached)。后台守护进程仍在持续运行。")
            return 0

    def run_autonomous_loop(self) -> int:
        """自治挖掘驱动核心轮询循环（集成自动检测与自治纠正自愈引擎，DOC-137）。"""
        self._setup_signals()
        self._log("=== BugX 自主持续挖掘守护进程启动 ===")
        self._log(f"空转检测阈值: {self.stall_threshold} 秒，检查轮询间隔: {self.interval} 秒")

        self.last_progress_time = time.monotonic()
        self.last_covered_count = 0
        self.unit_failure_counts = {}
        self.quarantined_units = set()
        self.self_heal_count = 0
        self._loop_started_at = time.monotonic()

        try:
            while not self._stop_requested:
                elapsed = time.monotonic() - self._loop_started_at
                if self.max_wall_time and elapsed >= self.max_wall_time:
                    # 文档声明的硬熔断此前从未被检查（max_wall_time 只被
                    # 存储/透传/展示）。无人值守长跑必须可强制停机。
                    self._log(
                        f"[硬熔断] 已达 max_wall_time={self.max_wall_time:.0f}s"
                        f"（已运行 {elapsed:.0f}s），守护进程停机"
                    )
                    self._stop_requested = True
                    break
                self._step_autonomous_cycle()
                time.sleep(self.interval)

            self._log("=== BugX 自主持续挖掘守护进程正常退出 ===")
            return 0
        except Exception as exc:
            self._log(f"[异常] 守护进程轮询异常崩溃: {exc}")
            return 1

    def _step_autonomous_cycle(self) -> None:
        """单步自治驱动：推进 Supervisor 单元认领与状态跃迁，并执行异常自动检测与自愈纠偏。"""
        try:
            from core.supervisor import audit_supervisor
            from core.launch_phases.common import supervisor_state_path
            from core.launch_phases.supervisor_bridge import get_supervisor
            from core.launch_phases.post_phase import post, continue_after_death

            state_path = supervisor_state_path()
            if not state_path.is_file():
                self._supervisor_wait_since = getattr(
                    self, "_supervisor_wait_since", None,
                ) or time.monotonic()
                wait_elapsed = time.monotonic() - self._supervisor_wait_since
                if wait_elapsed >= self.stall_threshold:
                    self._log(
                        f"[巡检告警] Supervisor 状态文件不存在，"
                        f"已空转 {wait_elapsed:.0f}s（阈值 {self.stall_threshold:.0f}s）。"
                        "可能原因：无 host agent（claude/codex/opencode）创建运行态。"
                        "请先运行 `bugx engine --root . pre` 初始化运行态，"
                        "或使用 `bugx autonomous --host <host>` 启动宿主驱动。"
                    )
                    self._supervisor_wait_since = time.monotonic()
                else:
                    self._log("[巡检] 等待 Supervisor 状态就绪...")
                return

            self._supervisor_wait_since = None

            sup = get_supervisor()
            st = sup.status()

            # 0. 被测目标服务健康探测与自愈 (DOC-137 2.0 / D137-05)
            target_port_str = os.environ.get("BUGX_TARGET_PORT")
            if target_port_str:
                try:
                    from core.planning.target_sandbox_manager import TargetSandboxManager
                    if self.sandbox_manager is None:
                        self.sandbox_manager = TargetSandboxManager(
                            self.root / ".bugx" / "sandboxes"
                        )
                    t_port = int(target_port_str)
                    health = self.sandbox_manager.check_health(port=t_port, timeout=1.5)
                    if not health.is_healthy:
                        self._log(f"[沙盒告警] 探测到被测目标服务不健康: {health.status_code} ({health.detail})")
                        start_cmd = os.environ.get("BUGX_TARGET_START_CMD")
                        if start_cmd:
                            self._log(f"[沙盒自愈] 尝试冷重启被测目标服务: {start_cmd}")
                            ok, new_pid, detail = self.sandbox_manager.recover_service(
                                start_cmd,
                                port=t_port,
                                pid=getattr(self, "_target_service_pid", None),
                                startup_timeout=10.0,
                            )
                            if ok:
                                self._target_service_pid = new_pid
                                self._log(f"[沙盒自愈成功] 目标服务已恢复正常运行 (PID={new_pid}): {detail}")
                            else:
                                self._log(f"[沙盒自愈失败] 服务未能成功恢复: {detail}")
                except Exception as s_exc:
                    self._log(f"[沙盒探活异常] {s_exc}")

            # 1. 正常终态与收敛判定
            debt = st.coverage_debt
            has_debt = bool(debt.get("has_unit_debt", False))
            covered = int(debt.get("covered", 0))

            if covered > self.last_covered_count:
                # 产生真实覆盖推进，刷新进展时间戳
                self.last_covered_count = covered
                self.last_progress_time = time.monotonic()
                self._log(f"[进展] 覆盖单元增加至: {covered}，已刷新活跃时间戳")

            # 1.1 Token ROI 边际产出弹性调控 (Token Yield Velocity)
            current_cost = float(getattr(st, "budget_cost_units", 0.0))
            current_findings = len(getattr(st, "findings", []))
            delta_cost = current_cost - self.last_cost_units
            delta_findings = current_findings - self.last_findings_count

            if delta_cost >= 100.0:
                if delta_findings == 0:
                    self.roi_stagnant_cycles += 1
                    self._log(f"[ROI监控] 累计消耗 {delta_cost:.1f} 算力单位无新增产出 (连续停滞 {self.roi_stagnant_cycles} 次)")
                    if self.roi_stagnant_cycles >= 3:
                        self._log("[ROI收敛] 边际产出递减至 0，触发弹性收敛停机 (stopped-roi_exhausted)...")
                        post(final=True)
                        self._stop_requested = True
                        return
                else:
                    self.roi_stagnant_cycles = 0
                    self.last_cost_units = current_cost
                    self.last_findings_count = current_findings

            if not has_debt and st.coverage_complete:
                self._log("[收敛] 全部单元已完成覆盖且债务出清，触发 post 终局结算...")
                post(final=True)
                self._stop_requested = True
                return

            if st.status.value in {"COMPLETE", "FAILED"}:
                self._log(f"[收敛] Supervisor 已进入终态: {st.status.value}，评估自动续轮...")
                transition = continue_after_death()
                self._log(f"[跃迁] 续轮结果: {transition}")
                # ``continued`` 是唯一表示"继续轮询"的跃迁，其余全是终态。
                # 旧白名单是反向枚举：stopped-no-debt 从未被返回（死成员），
                # stopped-roi_exhausted 也不经过这里（ROI 分支自行 post+停机），
                # 而可达的 start-failed / decision-required / stopped-zero-findings
                # 不在名单里——命中后守护进程既不停止也不推进，只会空转不休。
                # 改为"除 continued 外一律停机"，与 ``_continue_after_death``
                # 的返回值契约同构，新增终态无需再改这里。
                if transition != "continued":
                    self._stop_requested = True
                else:
                    self.last_progress_time = time.monotonic()
                return

            # 2. 自治任务驱动：当存在待覆盖单元债务时，主动触发 workers(execute=True) 推进执行 (DOC-137 / B-1)
            if has_debt and st.status.value not in {"COMPLETE", "FAILED"}:
                active_claims = getattr(st, "claims", {}) or {}
                active_count = len([
                    c for c in active_claims.values()
                    if getattr(c, "state", "") in {"active", "running"}
                ])
                if active_count < 2:
                    self._log(f"[调度] 发现待覆盖债务（covered={covered}），主动触发自主 Worker 执行...")
                    try:
                        from core.launch_phases.worker_orchestrator import workers
                        workers(strategy="risk", execute=True)
                    except Exception as w_exc:
                        self._log(f"[调度警告] 自主 Worker 执行异常: {w_exc}")

            # 3. 异常与空转自动检测（Auto-Detection）
            idle_duration = time.monotonic() - self.last_progress_time
            if idle_duration >= self.stall_threshold:
                self._log(f"[检测告警] 探测到流水线已连续 {idle_duration:.1f} 秒无有效覆盖进展，触发自治纠正机制...")
                self._auto_correct_anomalies(sup, st)
                # 纠正后重置时间戳，给予自愈恢复窗口
                self.last_progress_time = time.monotonic()

        except Exception as exc:
            self._log(f"[巡检轻量异常] {exc}")

    def _auto_correct_anomalies(self, sup: Any, st: Any) -> None:
        """纠偏与自愈机制：不中断会话，自动解决死锁、僵尸租约与阻塞单元。"""
        self.self_heal_count += 1
        healed_actions = []

        # 动作 1：僵死租约与超时 Claims 强制回收
        active_claims = getattr(st, "claims", {}) or {}
        now = time.time()
        for claim_id, claim in list(active_claims.items()):
            is_expired = False
            if hasattr(claim, "is_expired") and callable(claim.is_expired):
                try:
                    is_expired = bool(claim.is_expired(now))
                except Exception:
                    is_expired = False
            if not is_expired:
                # A7（Wave 1 交接 §4-2）：删除墙钟兜底 —— 墙钟前跳/回拨会把
                # 「未过期」判成「已过期」，与 ``WorkerClaim.is_expired`` 的
                # 单调时钟权威口径冲突。这里只做两件事：
                #   1) 缺 mono 锚点 ⇒ fail-closed 判过期（不靠墙钟猜）；
                #   2) mono 到期 ⇒ 过期。
                lease_expire_mono = (
                    getattr(claim, "lease_expires_mono", None)
                    or (claim.get("lease_expires_mono") if isinstance(claim, dict) else None)
                )
                if lease_expire_mono is None:
                    is_expired = True
                elif time.monotonic() > float(lease_expire_mono):
                    is_expired = True
            if is_expired:
                worker_id = (
                    getattr(claim, "worker_id", None)
                    or (claim.get("worker_id") if isinstance(claim, dict) else None)
                    or claim_id
                )
                # 过期/僵死租约的实际回收由 budget_governor.refresh 在
                # status() 内完成（续租/僵尸检测/标记 expired 三路径）。
                # 此处不再调用并不存在的 Supervisor.release_worker——
                # 那个调用只会抛 AttributeError 并被吞掉，让"自愈"变成
                # 静默空转；改为如实记账，使容量释放可观测。
                healed_actions.append(
                    f"检出过期租约 {claim_id} (worker: {worker_id})："
                    "回收由 status() 内的 refresh 路径执行"
                )
                # 失败计数：租约过期时该单元若仍未 covered，记一次失败，
                # 供动作 2 的隔离观察使用（接入 QuarantineManager 统合管理）。
                for unit in getattr(claim, "unit_ids", ()) or ():
                    status_now = (getattr(st, "coverage", {}) or {}).get(unit)
                    if status_now not in {"covered", "not_applicable"}:
                        self.quarantine.record_failure(str(unit), reason="租约过期未覆盖")
                    else:
                        self.quarantine.record_success(str(unit))

        # 动作 2：异常单元隔离与防死循环 (Quarantine)
        # 说明：Supervisor 没有 update_coverage API（旧调用恒不执行），
        # 且把单元写成 blocked 反而使其永久留在 UNIT_LEVEL_DEBT、阻碍
        # 覆盖出清——这里只做真实的失败计数与可观测隔离，不用假覆盖
        # 状态伪造终局。失败计数由 QuarantineManager 统合管理。
        for unit in getattr(st, "coverage", {}):
            u_str = str(unit)
            fails = self.quarantine.unit_failures.get(u_str, 0)
            if fails >= self.quarantine.max_retries and not self.quarantine.is_quarantined(u_str):
                self.quarantine.record_failure(u_str, reason=f"连续 {fails} 次租约过期未覆盖")
                healed_actions.append(
                    f"单元 {u_str} 连续 {fails} 次租约过期未覆盖，已列入隔离观察"
                    "（停止重复派发的调度侧跳过见后续里程碑）"
                )
            elif self.quarantine.is_quarantined(u_str):
                log_tag = f"单元 {u_str} 连续"
                if not any(log_tag in act for act in healed_actions):
                    healed_actions.append(
                        f"单元 {u_str} 连续 {fails} 次租约过期未覆盖，已列入隔离观察"
                        "（停止重复派发的调度侧跳过见后续里程碑）"
                    )

        # 动作 3：若因预算耗尽导致假死，自动弹性刷新预算配额
        if getattr(st, "status", None) and str(st.status.value).upper() == "BUDGET_EXHAUSTED":
            try:
                if hasattr(sup, "reset_budget"):
                    sup.reset_budget(force=True)
                    healed_actions.append("自动重置超额预算配额，激活下一 Epoch 调度")
            except Exception:
                pass

        if healed_actions:
            self._log(f"[自愈纠偏 #{self.self_heal_count}] 完成 {len(healed_actions)} 项自愈动作: {'; '.join(healed_actions)}。流水线已恢复调度。")
        else:
            # 旧实现调用 sup.heartbeat("daemon-self-heal", 0)：守护进程
            # 并非租约持有者，且 lease_seconds=0 必被 ClaimRejected 拒绝，
            # 该"队列扰动刷新"从未生效。无死锁租约时无需扰动，如实记录即可。
            self._log(
                f"[自愈纠偏 #{self.self_heal_count}] 未发现死锁租约；"
                "调度容量由 refresh 路径维护，无需额外扰动"
            )

    def _setup_signals(self) -> None:
        def _handle(signum: int, frame: Any) -> None:
            self._log(f"接收到信号 {signum}，请求优雅停机...")
            self._stop_requested = True

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    def _log(self, message: str) -> None:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {message}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def is_running(self) -> bool:
        pid = self.read_pid()
        return bool(pid and _is_pid_alive(pid))

    def read_pid(self) -> int | None:
        if not self.pid_path.is_file():
            return None
        try:
            raw = self.pid_path.read_text(encoding="utf-8").strip()
            return int(raw)
        except (ValueError, OSError):
            return None

    def _write_pid(self, pid: int) -> None:
        self.pid_path.write_text(str(pid), encoding="utf-8")

    def _remove_pid(self) -> None:
        try:
            if self.pid_path.is_file():
                self.pid_path.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bugx daemon",
        description="BugX 后台常驻守护进程管理与无头自治挖掘",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    # start
    p_start = sub.add_parser("start", help="启动后台常驻守护进程")
    p_start.add_argument("--root", help="目标工程根目录")
    p_start.add_argument("--foreground", action="store_true", help="在前台运行（调试用）")
    p_start.add_argument("--max-wall-time", type=float, default=DEFAULT_MAX_WALL_TIME, help="最大运行秒数上限")
    p_start.add_argument("--interval", type=float, default=DEFAULT_LOOP_INTERVAL, help="轮询间隔（秒）")

    # stop
    p_stop = sub.add_parser("stop", help="停止后台守护进程")
    p_stop.add_argument("--root", help="目标工程根目录")
    p_stop.add_argument("--force", action="store_true", help="强制 Kill 守护进程")

    # status
    p_status = sub.add_parser("status", help="查询守护进程运行状态")
    p_status.add_argument("--root", help="目标工程根目录")
    p_status.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 格式输出")

    # attach
    p_attach = sub.add_parser("attach", help="连接并实时回显守护进程日志")
    p_attach.add_argument("--root", help="目标工程根目录")

    args = parser.parse_args(argv)
    runner = DaemonRunner(
        root=args.root,
        max_wall_time=getattr(args, "max_wall_time", DEFAULT_MAX_WALL_TIME),
        interval=getattr(args, "interval", DEFAULT_LOOP_INTERVAL),
    )

    if args.action == "start":
        return runner.start(foreground=args.foreground)
    elif args.action == "stop":
        return runner.stop(force=args.force)
    elif args.action == "status":
        return runner.status(as_json=args.as_json)
    elif args.action == "attach":
        return runner.attach()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
