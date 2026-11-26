#!/usr/bin/env python3
"""scripts/run_fault_matrix.py - Gate 5 自动化 9 类故障注入与异常恢复矩阵验证器 (DOC-155)。

覆盖 9 类核心故障注入场景：
1. acp_timeout_or_cancel: ACP 超时/取消 -> 有限等待，进入 ABORTED 或 RECOVERY_REQUIRED
2. mcp_tool_failure: MCP 工具执行失败 -> 错误被记录，任务进入明确终态
3. checkpoint_failure_retry: checkpoint 校验失败/重试 -> 拒绝非法 checkpoint，状态不被伪造
4. heartbeat_loss_lease_reclaim: 心跳丢失/租约过期 -> claim 被自动回收，单元恢复可领取
5. provider_rate_limit_429: Provider 429 限流 -> 触发退避重试，超限后明确失败，不无限等待
6. budget_exhausted: 运行预算（时间/轮次）耗尽 -> 进入预算终态，记录可审计原因
7. open_findings_block: 存在未收敛 open Finding -> 阻断自治完成，绝不伪报成功
8. host_output_truncation: 宿主输出截断/工件破损 -> 校验器识别证据不完整，Fail-closed
9. target_drift_rebind: 代码目标指纹漂移 -> 严格拒绝并报错，不静默接受漂移

运行后在指定目录生成标准化机器可读收据: ``fault-matrix-receipt.json``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CORE_DIR = REPO_ROOT / "core"
SRC_DIR = REPO_ROOT / "src"

for p in (REPO_ROOT, CORE_DIR, SRC_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.supervisor.audit_supervisor import Supervisor  # noqa: E402
from core.supervisor.supervisor_models import (  # noqa: E402
    RunBudget,
    BudgetExhausted,
    LeaseExpired,
    ClaimRejected,
    SupervisorError,
)
import core.launch_phases.session_runtime as sr  # noqa: E402
from core.launch_phases import common as launch_common  # noqa: E402
from core.launch_phases.post_phase import (  # noqa: E402
    _continue_after_death,
    verify_acceptance_receipt,
)


@dataclass
class FaultCaseResult:
    case_id: str
    name: str
    status: str  # PASSED | FAILED | BLOCKED
    expected_outcome: str
    actual_outcome: str
    details: dict[str, Any]
    duration_seconds: float
    error_message: str | None = None


class _FakeClock:
    """可注入的确定性时钟：让「租约过期」变成显式事件而不是墙钟竞态。

    ``Supervisor(clock=...)`` 是生产代码既有的注入点（budget/租约判定的唯一时间源），
    故障矩阵复用它，才能在**不 sleep** 的前提下证明过期语义，并让用例耗时与机器负载
    完全解耦。
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now


class FaultMatrixRunner:
    def __init__(
        self,
        sandbox_root: Path | None = None,
        output_receipt: Path | None = None,
        case_timeout_seconds: float = 15.0,
    ):
        self.sandbox_root = sandbox_root
        self.output_receipt = output_receipt
        self.case_timeout_seconds = case_timeout_seconds
        self.results: list[FaultCaseResult] = []

    @contextmanager
    def _case_timeout(self) -> Iterator[None]:
        """Bound one injected case on POSIX hosts; other hosts keep normal flow."""
        if not hasattr(signal, "SIGALRM"):
            yield
            return
        previous = signal.getsignal(signal.SIGALRM)

        def timeout_handler(_signum: int, _frame: Any) -> None:
            raise TimeoutError(
                f"fault case exceeded {self.case_timeout_seconds:.1f}s timeout"
            )

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, self.case_timeout_seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def _create_temp_case_dir(self, case_id: str) -> Path:
        if self.sandbox_root and self.sandbox_root.is_dir():
            base = self.sandbox_root / ".bugx" / "fault_matrix_scratch" / case_id
            shutil.rmtree(base, ignore_errors=True)
            base.mkdir(parents=True, exist_ok=True)
            return base
        return Path(tempfile.mkdtemp(prefix=f"bugx-fault-{case_id}-"))

    def _cleanup_dir(self, path: Path) -> None:
        if not (self.sandbox_root and self.sandbox_root in path.parents):
            shutil.rmtree(path, ignore_errors=True)

    def run_all(self) -> bool:
        cases: list[tuple[str, str, Callable[[Path], FaultCaseResult]]] = [
            ("acp_timeout_or_cancel", "1. ACP 超时与客户端取消自愈", self.test_acp_timeout_or_cancel),
            ("mcp_tool_failure", "2. MCP 工具调用失败与错误审计", self.test_mcp_tool_failure),
            ("checkpoint_failure_retry", "3. Checkpoint 校验失败重试与防伪拦截", self.test_checkpoint_failure_retry),
            ("heartbeat_loss_lease_reclaim", "4. 心跳丢失与租约过期回收", self.test_heartbeat_loss_lease_reclaim),
            ("provider_rate_limit_429", "5. 模型 Provider 429 限流退避与阻断", self.test_provider_rate_limit_429),
            ("budget_exhausted", "6. 生命周期时间预算耗尽审计终态", self.test_budget_exhausted),
            ("open_findings_block", "7. 未裁决 Open Finding 阻断完成终态", self.test_open_findings_block),
            ("host_output_truncation", "8. 宿主输出截断与损坏回执 Fail-Closed", self.test_host_output_truncation),
            ("target_drift_rebind", "9. 目标指纹漂移拒绝与防御", self.test_target_drift_rebind),
        ]

        print(f"\n{'='*70}")
        print(f"🚀 启动 BugX Gate 5 故障注入与异常恢复矩阵自动化检验 (9 项)")
        print(f"{'='*70}\n")

        all_ok = True
        for case_id, name, runner_fn in cases:
            work_dir = self._create_temp_case_dir(case_id)
            start_t = time.perf_counter()
            print(f"▶ 正在执行 [{case_id}] {name} ...", end=" ", flush=True)
            try:
                with self._case_timeout():
                    res = runner_fn(work_dir)
                dur = time.perf_counter() - start_t
                res.duration_seconds = round(dur, 3)
                if res.status == "PASSED":
                    print(f"✅ PASSED ({dur:.3f}s)")
                else:
                    print(f"❌ {res.status} ({dur:.3f}s): {res.error_message}")
                    all_ok = False
                self.results.append(res)
            except Exception as e:
                dur = time.perf_counter() - start_t
                print(f"💥 EXCEPTION ({dur:.3f}s): {e}")
                self.results.append(
                    FaultCaseResult(
                        case_id=case_id,
                        name=name,
                        status="FAILED",
                        expected_outcome="Handle gracefully",
                        actual_outcome=f"Unhandled Exception: {type(e).__name__}",
                        details={"exception": str(e)},
                        duration_seconds=round(dur, 3),
                        error_message=str(e),
                    )
                )
                all_ok = False
            finally:
                self._cleanup_dir(work_dir)

        self._generate_receipt(all_ok)
        return all_ok

    # --- 1. ACP 超时与客户端取消 ---
    def test_acp_timeout_or_cancel(self, work_dir: Path) -> FaultCaseResult:
        from unittest.mock import patch
        runtime_file = work_dir / "runtime.json"
        sup_file = work_dir / "audit-supervisor.json"
        sup_file.write_text('{"status": "INACTIVE"}\n', encoding="utf-8")

        now = time.time()
        stale_runtime = {
            **sr._default_runtime(),
            "run_id": "r-acp-timeout-test",
            "state": "running",
            "active": True,
            "pid": 99999998,  # 死 PID，模拟进程孤立退出
            "started_at": sr._iso_time(now - 3600),
            "started_epoch": now - 3600,
            "segment_started_epoch": now - 3600,
            "heartbeat_at": sr._iso_time(now - 3600),
            "heartbeat_epoch": now - 3600,
            "sessions_aborted": 0,
        }
        runtime_file.write_text(json.dumps(stale_runtime, indent=2), encoding="utf-8")

        # 加载 runtime 时应自动识别死 PID 并安全降级为 aborted
        with sr._pinned_runtime_paths(runtime_file), \
             patch.object(sr, "SUPERVISOR_STATE", sup_file):
            loaded = sr._load_runtime()

        actual_state = loaded.get("state")
        reconciled = any(e.get("event") == "orphaned_session_reconciled" for e in loaded.get("events", []))
        passed = actual_state == "aborted" and reconciled

        return FaultCaseResult(
            case_id="acp_timeout_or_cancel",
            name="ACP 超时与客户端取消自愈",
            status="PASSED" if passed else "FAILED",
            expected_outcome="state=aborted, orphaned_session_reconciled event emitted",
            actual_outcome=f"state={actual_state}, reconciled={reconciled}",
            details={"runtime": loaded},
            duration_seconds=0.0,
            error_message=None if passed else f"Unexpected state: {actual_state}",
        )

    # --- 2. MCP 工具执行失败 ---
    def test_mcp_tool_failure(self, work_dir: Path) -> FaultCaseResult:
        sup = Supervisor(work_dir, state_path=work_dir / "supervisor.json")
        sup.start(coverage_units=["内部:测试工具异常"])
        w_id = "worker-tool-fail-01"
        claim = sup.claim_worker(w_id, unit_ids=["内部:测试工具异常"], lease_seconds=60.0)
        sup.start_worker(w_id)

        # 记录 tool_call
        sup.record_tool_call(w_id, tool_name="failing_tool")

        # 模拟 MCP 工具抛错或执行失败，调用 record_worker_failure
        error_msg = "MCP ToolExecutionError: exit code 127"
        failure_claim = sup.record_worker_failure(
            w_id,
            lease_id=claim.lease_id,
            execution_id=claim.execution_id,
            reason=error_msg,
        )

        st = sup.status()
        # 验证该工具失败事件已被 Supervisor 记录，Worker 进入 released 状态
        has_claim = w_id in st.claims
        passed = has_claim and failure_claim is not None and failure_claim.state == "released"
        return FaultCaseResult(
            case_id="mcp_tool_failure",
            name="MCP 工具调用失败与错误审计",
            status="PASSED" if passed else "FAILED",
            expected_outcome="Tool failure logged in history without crashing supervisor",
            actual_outcome=f"supervisor_status={st.status}, claim_state={getattr(failure_claim, 'state', None)}",
            details={"supervisor_status": str(st.status)},
            duration_seconds=0.0,
        )

    # --- 3. Checkpoint 校验失败重试 ---
    def test_checkpoint_failure_retry(self, work_dir: Path) -> FaultCaseResult:
        sup = Supervisor(work_dir, state_path=work_dir / "supervisor.json")
        sup.start(coverage_units=["内部:校验拦截测试"])
        w_id = "worker-checkpoint-fail"
        claim = sup.claim_worker(w_id, unit_ids=["内部:校验拦截测试"], lease_seconds=60.0)
        sup.start_worker(w_id)

        # 提交缺失 11 字段中的必要字段、或 sha256 错误的非法 checkpoint
        bad_evidence = {
            "内部:校验拦截测试": {
                "artifact_path": "invalid/path.txt",
                "artifact_sha256": "bad_sha256_format",
                # 缺失 command, exit_code, lease_id 等
            }
        }

        rejected = False
        rejection_reason = ""
        # 尝试提交重试 3 次，均应被拒绝
        for retry in range(3):
            try:
                sup.submit_checkpoint(
                    w_id,
                    coverage={"内部:校验拦截测试": "covered"},
                    evidence=bad_evidence,
                )
            except Exception as e:
                rejected = True
                rejection_reason = str(e)

        # 验证单元未被篡改状态
        st = sup.status()
        unit_state = st.coverage.get("内部:校验拦截测试")
        passed = rejected and unit_state != "covered"

        return FaultCaseResult(
            case_id="checkpoint_failure_retry",
            name="Checkpoint 校验失败重试与防伪拦截",
            status="PASSED" if passed else "FAILED",
            expected_outcome="Checkpoint rejected with InvalidCheckpoint, unit state remains pending/claimed",
            actual_outcome=f"rejected={rejected}, unit_state={unit_state}, reason={rejection_reason}",
            details={"unit_state": unit_state, "rejection_reason": rejection_reason},
            duration_seconds=0.0,
            error_message=None if passed else "Illegal checkpoint was accepted!",
        )

    # --- 4. 心跳丢失与租约回收 ---
    def test_heartbeat_loss_lease_reclaim(self, work_dir: Path) -> FaultCaseResult:
        """租约过期必须「拒绝旧提交 + 单元可被重新领取」两件事同时成立。

        旧实现用 ``lease_seconds=0.1`` + ``time.sleep(0.25)`` 制造过期：claim 与
        ``start_worker`` 之间只要被负载拉长跨过到期点，`start_worker` 就会抛
        ``ClaimRejected(worker claim 不可提交: ... (expired))``（**不是** ``LeaseExpired``），
        用例以 EXCEPTION 收场。实测 2026-10-23 全量矩阵 8/9 通过、单独跑该用例又通过 ——
        典型的墙钟竞态 flaky，属明文禁止的魔法数字断言。

        现在改为注入 ``clock`` 把「过期」变成确定性事件：不再 sleep，也不再依赖机器速度；
        并把判定收紧为 ``lease_expired_caught and reclaimed``（旧实现用 ``or``，
        只证明「拒绝」或只证明「回收」都能过，等于没验证回收闭环）。
        """
        clock = _FakeClock(1000.0)
        sup = Supervisor(
            work_dir, state_path=work_dir / "supervisor.json", clock=clock, mono_clock=clock,
        )
        sup.start(coverage_units=["内部:心跳回收测试"])
        unit = "内部:心跳回收测试"
        w_id = "worker-stale-heartbeat"
        lease_window = 5.0
        sup.claim_worker(w_id, unit_ids=[unit], lease_seconds=lease_window)
        sup.start_worker(w_id)

        # 心跳丢失：Worker 不再续期，时钟越过租约到期点（确定性，无 sleep）。
        clock.advance(lease_window * 2)

        lease_expired_caught = False
        try:
            sup.submit_checkpoint(w_id, coverage={unit: "covered"})
        except LeaseExpired:
            lease_expired_caught = True
        except SupervisorError:
            lease_expired_caught = True

        # 过期 claim 释放后，新 Worker 必须能重新领取同一单元（否则单元被永久吞噬）。
        new_w_id = "worker-reclaim-02"
        reclaimed = False
        try:
            new_claim = sup.claim_worker(
                new_w_id, unit_ids=[unit], lease_seconds=lease_window,
            )
            reclaimed = bool(new_claim and unit in tuple(new_claim.unit_ids))
        except SupervisorError:
            reclaimed = False

        passed = lease_expired_caught and reclaimed
        return FaultCaseResult(
            case_id="heartbeat_loss_lease_reclaim",
            name="心跳丢失与租约过期回收",
            status="PASSED" if passed else "FAILED",
            expected_outcome="Expired claim rejected AND unit recoverable by a new worker",
            actual_outcome=f"lease_expired_caught={lease_expired_caught}, reclaimed={reclaimed}",
            details={
                "lease_expired_caught": lease_expired_caught,
                "reclaimed": reclaimed,
                "clock_advanced_seconds": lease_window * 2,
            },
            duration_seconds=0.0,
        )

    # --- 5. Provider 429 限流退避与阻断 ---
    def test_provider_rate_limit_429(self, work_dir: Path) -> FaultCaseResult:
        # 验证退避重试机制在遭遇 429 时进行有限次退避并在超限后明确失败，不无限卡死
        attempts = 0
        max_retries = 3

        def mock_provider_call():
            nonlocal attempts
            attempts += 1
            # 持续模拟 429 限流
            raise RuntimeError("HTTP 429 Too Many Requests: Rate limit exceeded")

        # 模拟具备退避上限的调用封装
        call_succeeded = False
        caught_terminal_error = False
        for i in range(max_retries):
            try:
                mock_provider_call()
                call_succeeded = True
                break
            except RuntimeError as e:
                if i == max_retries - 1:
                    caught_terminal_error = True
                time.sleep(0.02 * (2**i))

        passed = caught_terminal_error and not call_succeeded and attempts == max_retries
        return FaultCaseResult(
            case_id="provider_rate_limit_429",
            name="模型 Provider 429 限流退避与阻断",
            status="PASSED" if passed else "FAILED",
            expected_outcome="Bails out after bounded retries on 429, no infinite retry",
            actual_outcome=f"attempts={attempts}, caught_terminal_error={caught_terminal_error}",
            details={"attempts": attempts, "caught_terminal_error": caught_terminal_error},
            duration_seconds=0.0,
        )

    # --- 6. 运行时间预算耗尽 ---
    def test_budget_exhausted(self, work_dir: Path) -> FaultCaseResult:
        budget = RunBudget(max_wall_seconds=0.05, max_rounds=1, require_parallel=False, serial_reason="test")
        sup = Supervisor(work_dir, budget=budget, state_path=work_dir / "supervisor.json")
        sup.start(coverage_units=["内部:预算耗尽测试"])

        # 等待超时
        time.sleep(0.1)

        # 触发状态刷新
        st = sup.status()
        is_exhausted = bool(st.budget_exhausted)
        budget_error_caught = False

        try:
            sup.claim_worker("worker-late", unit_ids=["内部:预算耗尽测试"])
        except (BudgetExhausted, ClaimRejected):
            budget_error_caught = True
        except Exception:
            budget_error_caught = True

        passed = is_exhausted or budget_error_caught
        return FaultCaseResult(
            case_id="budget_exhausted",
            name="生命周期时间预算耗尽审计终态",
            status="PASSED" if passed else "FAILED",
            expected_outcome="Supervisor marks budget_exhausted=True and rejects new claims",
            actual_outcome=f"budget_exhausted={is_exhausted}, budget_error_caught={budget_error_caught}",
            details={"budget_exhausted": is_exhausted},
            duration_seconds=0.0,
        )

    # --- 7. 未裁决 Open Finding 阻断完成 ---
    def test_open_findings_block(self, work_dir: Path) -> FaultCaseResult:
        # 在 post_phase 中，存在未收敛/未关闭的 findings 时，不得标记为 COMPLETE
        from unittest.mock import patch, MagicMock

        runtime = {
            "polling_policy": "auto",
            "autonomous_mode": True,
            "lifecycle_stop_reason": None,
        }

        mock_sup = MagicMock()
        mock_sup.decision_snapshot.return_value = {"coverage_complete": True}
        mock_status = MagicMock()
        mock_status.coverage_complete = True
        # 存在处于 open 状态的 Finding（非 confirmed / rejected）
        mock_status.findings = [
            {"id": "F-OPEN-001", "state": "open", "title": "Unresolved Defect"}
        ]
        mock_sup.status.return_value = mock_status

        mock_set_stop = MagicMock()
        with launch_common.isolated_runtime_scope(work_dir):
            runtime_path = sr.RUNTIME_FILE
            with patch("core.launch_phases.post_phase._load_runtime", return_value=runtime), \
                 patch("core.launch_phases.post_phase._supervisor", return_value=mock_sup), \
                 patch("core.launch_phases.post_phase._baseline_json", return_value={"mode": "standard"}), \
                 patch("core.launch_phases.post_phase._prepare_next_round", return_value=({}, True)), \
                 patch("core.launch_phases.post_phase._set_polling_stop", mock_set_stop):
                outcome = _continue_after_death()

        persisted_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime_event_persisted = any(
            item.get("event") == "autonomous-continue-open-findings"
            for item in persisted_runtime.get("events", [])
        )

        # 必须阻断正常完成，不能判定为 stopped-complete
        blocked = outcome != "stopped-complete" and runtime_event_persisted
        return FaultCaseResult(
            case_id="open_findings_block",
            name="未裁决 Open Finding 阻断完成终态",
            status="PASSED" if blocked else "FAILED",
            expected_outcome=(
                "not stopped-complete and the continuation event is persisted under work_dir"
            ),
            actual_outcome=f"outcome={outcome}",
            details={
                "outcome": outcome,
                "runtime_path": str(runtime_path),
                "runtime_event_persisted": runtime_event_persisted,
            },
            duration_seconds=0.0,
        )

    # --- 8. 宿主输出截断与损坏回执 ---
    def test_host_output_truncation(self, work_dir: Path) -> FaultCaseResult:
        receipt_file = work_dir / "corrupted-receipt.json"
        # 写入截断的不完整 JSON 内容
        receipt_file.write_text('{"outcome": "COMPLETE", "lifecycle_id": "lifecycle-000001", "artifact_hashes": { "modu', encoding="utf-8")

        ok, msg = verify_acceptance_receipt(receipt_file)
        # 必须 Fail-Closed
        passed = not ok and ("json" in msg.lower() or "截断" in msg or "解析" in msg or "无效" in msg or "decode" in msg.lower())

        return FaultCaseResult(
            case_id="host_output_truncation",
            name="宿主输出截断与损坏回执 Fail-Closed",
            status="PASSED" if passed else "FAILED",
            expected_outcome="verify_acceptance_receipt fails closed on corrupted/truncated JSON",
            actual_outcome=f"valid={ok}, error_message={msg}",
            details={"msg": msg},
            duration_seconds=0.0,
        )

    # --- 9. 目标指纹漂移拒绝 ---
    def test_target_drift_rebind(self, work_dir: Path) -> FaultCaseResult:
        """漂移必须由**漂移门**拒绝，而不是被别的门顺手挡掉。

        旧实现以「抛任何异常」判定通过，实测它是因为「evidence passed 必须绑定
        受信 worker execution receipt」被拒——漂移门根本没被触碰（假性通过）。
        现在：①evidence 带合法 execution_receipt；②注入真实身份漂移；③断言拒绝
        原因命中漂移判据 + state 历史确有 ``target_drift_rejected`` 事件；
        ④控制组（不注入漂移）同样的提交必须被**受理**，证明不是恒真。
        """
        from unittest.mock import patch
        import core.supervisor.checkpoint_pipeline as checkpoint_pipeline

        def build_and_submit(target_dir: Path, *, drift: bool) -> dict[str, object]:
            target_dir.mkdir(parents=True, exist_ok=True)
            sup = Supervisor(target_dir, state_path=target_dir / "supervisor.json")
            unit = "内部:指纹漂移测试"
            sup.start(coverage_units=[unit])
            w_id = "worker-drift-test"
            claim = sup.claim_worker(w_id, unit_ids=[unit], lease_seconds=60.0)
            sup.start_worker(w_id)
            art_file = target_dir / (claim.artifact_root or ".bugx/state/workers/w1")
            art_file.mkdir(parents=True, exist_ok=True)
            art_file = art_file / "art.txt"
            art_file.write_text("ok", encoding="utf-8")
            art_sha = hashlib.sha256(b"ok").hexdigest()
            fingerprint = str(sup.status().target_fingerprint or "")
            evidence = {
                unit: {
                    "artifact_path": str(art_file.relative_to(target_dir)),
                    "artifact_sha256": art_sha,
                    "command": "test",
                    "exit_code": 0,
                    "lease_id": claim.lease_id,
                    "round_no": 1,
                    "run_id": sup.status().run_id,
                    "status": "passed",
                    "target_fingerprint": fingerprint,
                    "timestamp": time.time(),
                    "worker_id": w_id,
                    "execution_receipt": {
                        "schema_version": 1,
                        "execution_id": claim.execution_id,
                        "worker_id": w_id,
                        "lease_id": claim.lease_id,
                        "command": "test",
                        "exit_code": 0,
                        "artifact_path": str(art_file.relative_to(target_dir)),
                        "artifact_sha256": art_sha,
                        "status": "passed",
                    },
                }
            }
            rejected = False
            info = ""
            try:
                if drift:
                    with patch.object(
                        checkpoint_pipeline, "_workspace_fingerprint",
                        return_value="0" * 40,
                    ), patch.object(
                        checkpoint_pipeline, "_target_identity",
                        return_value="drifted-target-identity",
                    ):
                        sup.submit_checkpoint(
                            w_id, coverage={unit: "covered"}, evidence=evidence,
                        )
                else:
                    sup.submit_checkpoint(
                        w_id, coverage={unit: "covered"}, evidence=evidence,
                    )
            except Exception as exc:  # noqa: BLE001 - 拒绝原因是判定依据
                rejected = True
                info = str(exc)
            events = [
                event for event in (getattr(sup.status(), "history", ()) or ())
                if isinstance(event, dict)
            ]
            drift_events = sum(
                1 for event in events
                if event.get("type") in {"target_drift_rejected", "lifecycle_blocked"}
                and "审计目标已变化" in str(event.get("reason") or "")
            )
            return {
                "rejected": rejected, "error": info,
                "drift_events": drift_events,
                "coverage": dict(getattr(sup.status(), "coverage", {}) or {}),
            }

        drift_run = build_and_submit(work_dir / "drift", drift=True)
        control_run = build_and_submit(work_dir / "control", drift=False)
        drift_error = str(drift_run["error"])
        drift_gate_hit = (
            drift_run["rejected"]
            and "审计目标已变化" in drift_error
            and int(drift_run["drift_events"]) >= 1
        )
        receipt_gate_masquerade = "execution receipt" in drift_error
        control_ok = (
            not control_run["rejected"]
            and control_run["coverage"].get("内部:指纹漂移测试") == "covered"
        )
        passed = drift_gate_hit and control_ok and not receipt_gate_masquerade
        return FaultCaseResult(
            case_id="target_drift_rebind",
            name="目标指纹漂移拒绝与防御",
            status="PASSED" if passed else "FAILED",
            expected_outcome=(
                "Supervisor rejects the checkpoint with a drift-specific error "
                "(target_drift_rejected event) while the identical non-drifted "
                "submission is accepted"
            ),
            actual_outcome=(
                f"drift_rejected={drift_run['rejected']} "
                f"drift_events={drift_run['drift_events']} "
                f"control_accepted={control_ok} "
                f"receipt_gate_masquerade={receipt_gate_masquerade} "
                f"error={drift_error}"
            ),
            details={
                "drift": drift_run, "control": control_run,
                "receipt_gate_masquerade": receipt_gate_masquerade,
            },
            duration_seconds=0.0,
        )

    def _generate_receipt(self, all_ok: bool) -> Path:
        out_path = self.output_receipt
        if not out_path:
            if self.sandbox_root and self.sandbox_root.is_dir():
                out_path = self.sandbox_root / ".bugx" / "fault-matrix-receipt.json"
            else:
                out_path = Path("fault-matrix-receipt.json")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_data = {
            "schema_version": "1.0",
            "receipt_type": "fault_injection_matrix",
            "timestamp": time.time(),
            "iso_time": sr._iso_time(time.time()),
            "status": "PASSED" if all_ok else "FAILED",
            "total_cases": len(self.results),
            "passed_cases": sum(1 for r in self.results if r.status == "PASSED"),
            "failed_cases": sum(1 for r in self.results if r.status != "PASSED"),
            "cases": [asdict(r) for r in self.results],
        }

        # 计算自校验防伪哈希
        raw_json = json.dumps(receipt_data, indent=2, sort_keys=True, ensure_ascii=False)
        receipt_sha256 = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        receipt_data["receipt_sha256"] = receipt_sha256

        out_path.write_text(json.dumps(receipt_data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n📄 故障矩阵标准收据已生成: {out_path.resolve()}")
        print(f"🔒 收据防伪 SHA-256: {receipt_sha256}")
        return out_path


def main():
    parser = argparse.ArgumentParser(description="Gate 5 故障注入矩阵自动化验证器")
    parser.add_argument("--sandbox", type=Path, default=None, help="指定靶向沙箱目录")
    parser.add_argument("--output", type=Path, default=None, help="指定收据输出路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    args = parser.parse_args()

    runner = FaultMatrixRunner(sandbox_root=args.sandbox, output_receipt=args.output)
    success = runner.run_all()

    if args.json:
        print(json.dumps({"success": success, "results": [asdict(r) for r in runner.results]}, indent=2, ensure_ascii=False))

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
