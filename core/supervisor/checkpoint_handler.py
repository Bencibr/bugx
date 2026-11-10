#!/usr/bin/env python3
"""core/supervisor/checkpoint_handler.py - Checkpoint 提交、验证与原子提交流水线。"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .supervisor_models import (
    BugRecord,
    ClaimRejected,
    InvalidCheckpoint,
    LeaseExpired,
    RunState,
    SupervisorError,
    WorkerClaim,
    finite_number,
)

try:
    from core.supervisor import (
        attach_evidence_receipts,
        checkpoint_sync,
        graph_sync,
        mark_claim_expired,
    )
    from core.supervisor.checkpoint_pipeline import (
        checkpoint_payload_digest,
    )
except ImportError:
    from core.supervisor import (
        attach_evidence_receipts,
        checkpoint_sync,
        graph_sync,
        mark_claim_expired,
    )
    from core.supervisor.checkpoint_pipeline import (
        checkpoint_payload_digest,
    )


if TYPE_CHECKING:
    from .audit_supervisor import Supervisor

logger = logging.getLogger(__name__)

def _apply_checkpoint_operation(
    self,
    state: RunState,
    now: float,
    **kwargs,
) -> RunState:
    """Compatibility seam for the checkpoint mutation boundary."""
    return checkpoint_sync.apply_checkpoint_operation(
        self, state, now, **kwargs,
    )

def _record_checkpoint_rejection(
    self, exc: BaseException, *, worker_id: str, checkpoint_id: str | None,
) -> None:
    """Keep rejected checkpoint attempts visible without applying their mutation."""
    try:
        def block_event(state: RunState, now: float) -> RunState:
            self._append_event(
                state, "lifecycle_blocked", now,
                operation="checkpoint", gate="checkpoint_rejected",
                reason=str(exc)[:512], worker_id=worker_id,
                checkpoint_id=checkpoint_id,
            )
            return state
        self._mutate(block_event)
    except Exception:  # noqa: BLE001 - 留痕失败不掩盖原拒绝
        pass

def _verify_readonly_claim(
    self, state: RunState, worker_key: str, now: float,
) -> None:
    """只读 claim 的越权校验（CORE-308 P2）：声明只读却新增改动 -> 拒绝。

    为什么要校验而不是只靠声明：claim 层对 ``write_access=False`` 不派生
    ``file:`` 互斥键（放开同文件并发），若执行侧没有真强制，模型改文件就会
    静默覆盖。这里用 claim 时的 ``git status`` 基线复检，把「只读声明」
    变成可验证事实。基线缺失（git 不可用）时跳过——那是授予阶段的
    fail-closed 责任，不在提交侧猜测。
    """

    claim = state.claims.get(worker_key)
    if claim is None or getattr(claim, "write_access", None) is not False:
        return
    baseline = getattr(claim, "source_baseline", None)
    if not baseline:
        return
    try:
        from core.governance import readonly_enforcer
    except ImportError:
        return
    violation = readonly_enforcer.verify_untouched(self.workspace_root, baseline)
    if violation:
        self._append_event(
            state, "checkpoint_rejected", now, worker_id=worker_key,
            reason="readonly_write_violation", detail=violation,
        )
        raise InvalidCheckpoint(
            f"只读 claim 越权: {violation}；请改由具备写权限的轮次（fix_mode=auto）重做"
        )

def submit_checkpoint(
    self,
    worker_id: str,
    *,
    findings: Iterable[Mapping[str, object]] | None = None,
    coverage: Mapping[str, str] | None = None,
    evidence: Mapping[str, Mapping[str, object]] | None = None,
    life_delta: float = 0.0,
    expected_revision: int | None = None,
    signatures: Iterable[str] | None = None,
    notes: Mapping[str, str] | None = None,
    suggested_modules: Iterable[str] | None = None,
    checkpoint_id: str | None = None,
    salvage: bool = False,
    append_verdict: bool = False,
) -> RunState:
    """在锁内串行提交并合并 worker checkpoint。

    ``salvage=True``（DOC-203 D2）仅供 host-only 的
    ``bugx_supervisor_salvage_checkpoint`` 使用：对 state=="expired"
    的 claim 代交已固化证据（详见 checkpoint_sync.apply_checkpoint_operation）。

    ``append_verdict=True``（F-RSI-20261110-013）仅供 judge settle 的逐单元
    verdict 落账使用：多单元 claim 的第一个 verdict 已把 claim 推到
    "submitted"，其余单元的 verdict 经此通道追加（checkpoint_id 必须全新、
    coverage 键 ⊆ claim.unit_ids、life_delta 强制 0——三重约束在
    apply_checkpoint_operation 与入口校验中原样生效）。
    """
    worker_key = self._normalize_worker_id(worker_id)
    checkpoint_key = self._normalize_checkpoint_id(checkpoint_id)
    delta = finite_number(life_delta, "life_delta")
    if delta != 0:
        raise InvalidCheckpoint(
            "worker checkpoint 不得提交 life_delta；由宿主完成证据确认和生命结算"
        )
    incoming_signatures = self._normalize_signatures(signatures)
    incoming_notes = self._normalize_notes(notes)
    incoming_findings = self._normalize_findings(
        findings, workspace_root=self.workspace_root,
    )
    incoming_suggested = [
        str(m).strip().lower()
        for m in (suggested_modules or [])
        if str(m).strip()
    ]
    from core.contracts.engine_gap_contracts import mark_semantic_duplicates
    _validate_incoming_findings(incoming_findings, self.workspace_root)
    incoming_coverage = self._normalize_coverage(coverage)
    incoming_evidence = self._normalize_evidence(evidence)
    # F1'：findings 摄入侧 receipt 关联——必须在 payload_digest 之前完成，
    # 使同一 checkpoint 重试的摘要保持确定性（同输入 → 同关联 → 同摘要）。
    attach_evidence_receipts(incoming_findings, incoming_evidence)
    payload_digest = checkpoint_payload_digest(
        findings=incoming_findings,
        coverage=incoming_coverage,
        evidence=incoming_evidence,
        notes=incoming_notes,
        signatures=incoming_signatures,
        suggested_modules=incoming_suggested,
        life_delta=delta,
    )
    if not any((
        incoming_coverage,
        incoming_evidence,
        incoming_findings,
        incoming_notes,
        incoming_signatures,
        incoming_suggested,
    )):
        raise InvalidCheckpoint(
            "checkpoint 必须包含 findings、coverage、evidence、notes、signatures 或 suggested_modules"
        )
    # LOG-08：证据发布与 run-state 持久化必须落在同一可恢复边界，且**同生共死**。
    # operation 在锁内只登记「待发布的证据」；真正的 coverage-evidence.json
    # 写入通过 _mutate 的 before_persist 钩子在 _persist_locked 之前执行：
    #   - 发布成功 → 随后持久化 run-state（失败时恢复原证据与原 state）；
    #   - 发布失败（含 conflict）→ 本次提交整体放弃，覆盖/claim/事件都不落盘，
    #     worker 可带同一 checkpoint_id 安全重试，绝不出现
    #     「coverage=covered 而证据缺失」的自相矛盾状态（Life 2 复验事故根因）。
    pending_evidence: list[Mapping[str, Mapping[str, object]]] = []

    def operation(state: RunState, now: float) -> RunState:
        self._verify_readonly_claim(state, worker_key, now)
        return self._apply_checkpoint_operation(
            state,
            now,
            checkpoint_key=checkpoint_key,
            payload_digest=payload_digest,
            delta=delta,
            expected_revision=expected_revision,
            incoming_coverage=incoming_coverage,
            incoming_evidence=incoming_evidence,
            incoming_findings=incoming_findings,
            incoming_notes=incoming_notes,
            incoming_signatures=incoming_signatures,
            incoming_suggested=incoming_suggested,
            mark_semantic_duplicates=mark_semantic_duplicates,
            pending_evidence=pending_evidence,
            worker_key=worker_key,
            salvage=salvage,
            append_verdict=append_verdict,
        )

    def publish_staged_evidence() -> Callable[[], None] | None:
        if pending_evidence:
            return self._publish_checkpoint_evidence(pending_evidence[0])
        return None

    return self._commit_checkpoint_transaction(
        operation,
        publish_staged_evidence,
        worker_id=worker_id,
        checkpoint_id=checkpoint_id,
    )  # type: ignore[return-value]

def _commit_checkpoint_transaction(
    self,
    operation: Callable[[RunState, float], RunState],
    before_persist: Callable[[], Callable[[], None] | None],
    *,
    worker_id: str,
    checkpoint_id: str | None,
) -> object:
    """checkpoint 事务的失败收尾（DES-03：submit_checkpoint 保持单职责）。

    两条失败路径都必须**另起事务**落账，因为 atomic_pair 事务本体在异常时整体
    回滚：
    * ``LeaseExpired``：过期是既成事实，回滚会把 expired 标记/计数/事件一起丢掉，
      claim 永久停留 active ⇒ 单元被「本 Round 已领取」永久占位（实测 w1 过期后
      w2 永远领不到同一 unit）；
    * ``InvalidCheckpoint``/``ClaimRejected``：DOC-204 L1 / LOG-08，拒绝原因落
      权威链，worker 可用同 checkpoint_id 重试。
    """
    try:
        return self._mutate(operation, before_persist=before_persist)
    except LeaseExpired:
        self._commit_expired_claims(worker_id=worker_id)
        raise
    except (InvalidCheckpoint, ClaimRejected) as exc:
        self._record_checkpoint_rejection(
            exc, worker_id=worker_id, checkpoint_id=checkpoint_id,
        )
        raise

def _commit_expired_claims(self, *, worker_id: str | None = None) -> int:
    """把「已过期但仍是 active」的 claim 落账，返回本次标记的条数。

    过期判定与 ``active_claim`` 复用同一 ``claim.is_expired`` 契约和
    ``mark_claim_expired`` 记账单点（状态 / ``total_expired`` / hash-chain 事件），
    避免出现第二条「标记过期」的旁路实现。
    """

    def operation(state: RunState, now: float) -> int:
        marked = 0
        for claim in list(state.claims.values()):
            if worker_id is not None and claim.worker_id != worker_id:
                continue
            if claim.active and claim.is_expired(now):
                mark_claim_expired(state, claim, now, include_execution_id=True)
                marked += 1
        return marked

    return int(self._mutate(operation) or 0)  # type: ignore[arg-type]


def submit_graph_checkpoint(
    self,
    worker_id: str,
    *,
    graph_task_id: str,
    lease_id: str,
    execution_id: str,
    artifact_digest: str,
    observation: Mapping[str, object],
    bugs: Sequence[BugRecord | Mapping[str, object]] = (),
    completed: bool = True,
    review_coverage: Mapping[str, str] | None = None,
    checkpoint_id: str | None = None,
    expected_revision: int | None = None,
) -> RunState:
    """Commit a graph artifact and its evidence through the state lock."""

    worker_key = self._normalize_worker_id(worker_id)
    return self._mutate(
        lambda state, now: graph_sync.graph_checkpoint_sync(
            self, state, now,
            worker_id=worker_key,
            graph_task_id=graph_task_id,
            lease_id=lease_id,
            execution_id=execution_id,
            artifact_digest=artifact_digest,
            observation=observation,
            bugs=bugs,
            completed=completed,
            review_coverage=review_coverage,
            checkpoint_id=self._normalize_checkpoint_id(checkpoint_id),
            expected_revision=expected_revision,
        )
    )  # type: ignore[return-value]


def _validate_incoming_findings(findings: list, workspace_root) -> None:
    """checkpoint 摄入侧统一校验：evidence_bundle + reproducible 契约。

    两件事必须在**摄入时**完成而不是留到整轮结束的完成门禁：
    * ``evidence_bundle`` 归一化失败要立刻拒绝，避免脏 bundle 入库；
    * ``reproducible=True`` 缺 ``reproduction`` 属契约违规，若放到门禁才报，
      worker 已把违法 finding 当「提交成功」而无法自我修正，只能整轮卡在
      ``finding_confirmation_pending``（实测 cve_cjson_repair：2 条 high
      finding，autonomous 804s 后 BLOCKED）。
    """
    from core.contracts.engine_gap_contracts import validate_evidence_bundle
    for finding in findings:
        bundle = finding.get("evidence_bundle")
        if bundle is not None:
            try:
                finding["evidence_bundle"] = validate_evidence_bundle(
                    bundle, root=workspace_root,
                )
            except (TypeError, ValueError) as exc:
                raise InvalidCheckpoint(f"evidence_bundle 无效: {exc}") from exc
        _assert_reproducible_contract(finding)



def _assert_reproducible_contract(finding: dict) -> None:
    """摄入侧 fail-fast：``reproducible=True`` 必须带非空 ``reproduction``。

    契约的唯一真源是 ``finding_confirmation.reproducible_contract_violation``；
    此前它只在完成门禁（整轮结束）与评测协议里执行，worker 提交违法 finding
    仍被当作「成功」，只能整轮卡在 ``finding_confirmation_pending``（实测
    cve_cjson_repair：2 条 high finding 标为可复现却无 reproduction，
    autonomous 804s 后 BLOCKED）。在摄入侧拒绝后 worker 可立即补
    ``reproduction`` 或显式置 ``reproducible=false`` 重试。
    """
    from core.ledger import finding_confirmation
    violation = finding_confirmation.reproducible_contract_violation(finding)
    if not violation:
        return
    label = str(
        finding.get("id") or finding.get("finding_id")
        or finding.get("fingerprint") or "<unnamed>"
    )
    raise InvalidCheckpoint(
        f"Finding {label} 违反 reproducible/reproduction 契约（{violation}）："
        + (
            "reproduction 必须是**可直接执行的命令**，不能把命令和英文解说拼在一起"
            "（实测：`python3 p.py records the ... (no network); unit.json shows a=b` "
            "满足「非空」但跑不起来 → bash 语法错误）。"
            "请只保留命令本身，解说移到 root_cause 或另建 artifact。"
            if violation == "reproduction_not_executable"
            else "标为可复现必须提供非空 reproduction，否则请显式置 reproducible=false"
        )
    )




