#!/usr/bin/env python3
"""Supervisor checkpoint 状态合并与事件编排边界。

输入归一化、锁、目标身份检查和证据发布时序仍由
``audit_supervisor.Supervisor`` 负责；本模块只执行已校验 payload 的锁内
状态合并，保留显式 Supervisor 窄边界。
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Mapping

from .supervisor_models import (
    COVERAGE_COVERED,
    COVERAGE_UNVERIFIED,
    COVERAGE_STALE,
    ClaimRejected,
    InvalidCheckpoint,
    MAX_WORKER_ATTEMPTS,
    MAX_RECENT_SIGNATURES,
    MAX_UNIT_NOTES,
    RunState,
    Status,
    note_requests_deep_verification,
    TERMINAL_COVERED,
    worker_attempts_exhausted,
)

from core.supervisor.checkpoint_pipeline import (
    assert_artifact_not_before_claim,
    merge_coverage_updates,
    validate_worker_artifacts,
)

from core.supervisor.lease_manager import mark_worker_attempts_exhausted


def apply_checkpoint_operation(
    supervisor,
    state: RunState,
    now: float,
    *,
    checkpoint_key,
    payload_digest,
    delta,
    expected_revision,
    incoming_coverage,
    incoming_evidence,
    incoming_findings,
    incoming_notes,
    incoming_signatures,
    incoming_suggested,
    mark_semantic_duplicates,
    pending_evidence,
    worker_key,
    salvage: bool = False,
    append_verdict: bool = False,
) -> RunState:
    """``submit_checkpoint`` 的状态变更体（DES-03）。

    原先这是一段 328 行的嵌套闭包 ``operation(state, now)``，捕获了
    ``submit_checkpoint`` 的 12 个外层名字（含仅关键字参数
    ``expected_revision``）。闭包形式让这段逻辑无法单独命名、阅读、
    测试，也使 ``submit_checkpoint`` 膨胀到 388 行。现提升为具名方法：
    捕获的名字改为**仅关键字参数**传入，行为逐字不变
    （``pending_evidence`` 仍以就地追加的方式回传）。

    ``salvage=True``（DOC-203 D2）是宿主代交通道，仅由 host-only MCP 工具
    ``bugx_supervisor_salvage_checkpoint`` 进入：允许对 ``expired`` 或
    ``released``（且未被 ``superseded_by`` 接管）的 claim 落账已固化证据，
    但绑定链校验（五元组 + 工件哈希 + mtime 纪元）全部保留，且一次性
    （salvaged_at 非空即拒）。熔断（
    worker_attempts_exhausted）不放宽——salvage 只落账已完成证据，
    不产生新的 claim。
    """
    existing_claim = state.claims.get(worker_key)
    if checkpoint_key is not None:
        for recorded_claim in state.claims.values():
            if recorded_claim.last_checkpoint_id != checkpoint_key:
                continue
            if recorded_claim.worker_id != worker_key:
                raise ClaimRejected(
                    f"checkpoint_id 已被其他 worker 使用: {checkpoint_key}"
                )
            if recorded_claim.last_checkpoint_digest != payload_digest:
                raise InvalidCheckpoint(
                    "同一 checkpoint_id 的 payload 不一致，拒绝重放"
                )
    # The response may have been lost after the first commit.  An
    # identical checkpoint ID is an acknowledgement of that commit,
    # so it must be idempotent even when the caller retries with the
    # pre-commit revision it originally observed.
    if (
        existing_claim is not None
        and not existing_claim.active
        and existing_claim.state == "submitted"
        and checkpoint_key is not None
        and checkpoint_key == existing_claim.last_checkpoint_id
    ):
        if existing_claim.last_checkpoint_digest != payload_digest:
            raise InvalidCheckpoint(
                "同一 checkpoint_id 的 payload 不一致，拒绝重放"
            )
        if incoming_evidence:
            pending_evidence.append(incoming_evidence)
        return state
    supervisor._check_revision(state, expected_revision)
    if existing_claim is None:
        raise ClaimRejected(f"worker 未 claim: {worker_key}")
    # DOC-203 D2 + 缺陷 B4：salvage 通道接受 `expired` 或 `released` 的 claim，
    # **仅当未被 `superseded_by` 接管**（released 由 drain/孤儿巡检经
    # release_claim 产出；回收先到不能永久拒收宿主已完成的成品）。真正的
    # 防接管竞态判据是「是否已被接管」，不是 state 字面量；`superseded` 状态
    # 本身也不在可接受集合内。每 claim 一次性（salvaged_at 非空即拒）。
    # 五元组绑定（worker_id/run_id/round_no/lease_id/target_fingerprint）、
    # 工件哈希与 validate_worker_artifacts 全量校验在下文原样执行。
    # L2-09 P2-1（审计 M5 存活根因）：一次性守卫必须**先于** state 判定。
    # 成功代交在本函数尾部把 claim.state 置为 "submitted" 并同时写
    # salvaged_at，因此「先判 state」会让二次代交永远命中 state 分支，
    # salvaged_at 分支成为不可达死代码，其反例测试也就杀不掉该变异体。
    # 顺序即契约：先证「这个 claim 没被代交过」，再证「它确实处于可代交状态」，
    # 最后证「它还没被别的 claim 接管」。
    if salvage:
        if existing_claim.salvaged_at is not None:
            raise ClaimRejected(
                f"claim 已由宿主代交过（salvaged_at={existing_claim.salvaged_at}），"
                f"拒绝二次代交: {worker_key}"
            )
        if existing_claim.state not in {"expired", "released"}:
            raise ClaimRejected(
                f"salvage 仅接受 expired/released claim: {worker_key} "
                f"({existing_claim.state})"
            )
        if existing_claim.superseded_by:
            raise ClaimRejected(
                f"claim 已被 {existing_claim.superseded_by} 接管，拒绝代交: "
                f"{worker_key}"
            )
        if any("confirmation" in finding for finding in incoming_findings):
            raise InvalidCheckpoint(
                "salvage payload 的 findings 不得携带 confirmation 块"
                "（代交候选一律为纯 candidate，确认走正常结算门）"
            )
        # 纪元防污染：artifact_root 可被 supersede/recover 继承（claim_sync），
        # salvage 只信任本 claim 领取时刻之后写入的工件。判据与 worker 通道
        # 共用 assert_artifact_not_before_claim（L3-C2 同强度），mtime 可被
        # utime 伪造的残留弱点归 B6。
        for unit, item in incoming_evidence.items():
            artifact_rel = str(item.get("artifact_path") or "")
            if not artifact_rel:
                continue
            artifact_abs = Path(supervisor.workspace_root) / artifact_rel
            assert_artifact_not_before_claim(
                artifact=artifact_abs,
                claimed_at=existing_claim.claimed_at,
                unit=unit,
                channel="salvage",
            )
        claim = existing_claim
    else:
        # A different (or missing) id after submit is rejected.
        CHECKPOINT_GRACE_SECONDS = 60.0
        # A7（Wave 3 口径）：grace 窗只看**单调时钟**。旧实现用墙钟
        # ``lease_expires_at + grace``，墙钟前跳会把 window 算成负值而错拒提交。
        # F-RSI-20261110-018：mono_now 必须与 Supervisor 的注入时钟同源——
        # lease_expires_mono 由同一时钟锚定（claim 时经 _active_claim 写入），
        # 这里直调 time.monotonic() 会让注入时钟的测试/沙箱环境窗口判断
        # 永远 False（实测 lease_expires_mono=1011 vs 真实 monotonic=33 万秒）。
        mono_now = float(supervisor._mono_clock())
        is_grace_expired = (
            # F-RSI-20261110-018：mark/孤儿巡检两条回收路径实际产出
            # "expired"（mark_claim_expired）与 "released"（release_claim）
            # 两种终态，且 release_claim 不清空租约字段（lease_expires_mono
            # 保留）——grace 通道必须两者都认，否则 worker 在租约边界
            # 已完成的证据被永久丢弃（grace 语义死代码，轮 33 探针实测）。
            # 绑定链校验（epoch + lease_expires_mono 窗口）原样生效，
            # grace 窗口（60s）外仍走显式拒绝。
            existing_claim.state in ("expired", "released")
            and existing_claim.scheduler_epoch == state.scheduler_epoch
            and existing_claim.lease_expires_mono is not None
            and mono_now <= existing_claim.lease_expires_mono + CHECKPOINT_GRACE_SECONDS
        )
        if not existing_claim.active and not is_grace_expired:
            # F-RSI-20261110-013：judge 多单元 claim（unit_group）在 settle 时
            # 逐单元提交 verdict，第一个 verdict 已把 claim 推到 "submitted"，
            # 同 claim 其余单元的 verdict 被单次提交限制拒收，单元僵死在
            # 「已探针未裁决」。追加通道的三重约束：claim 必须处于 "submitted"
            # （探针/首 verdict 已合法落账）、checkpoint_id 必须全新（上方
            # 幂等通道只放行同 id 同 digest 重放）、coverage 键 ⊆ claim.unit_ids
            # （下方成员校验原样生效），且 life_delta 在入口已强制为 0。
            # F-RSI-20261110-018：grace 窗口内的终态 claim（expired/released，
            # is_grace_expired 已含窗口+epoch+租约字段校验）是租约边界的
            # 正当提交通道，优先放行——不能被 F-013 的 append 分支条件
            # （只认 state=="submitted"）误伤。
            if not (append_verdict and existing_claim.state == "submitted"
                    and not is_grace_expired):
                raise ClaimRejected(
                    f"worker claim 不可提交: {worker_key} ({existing_claim.state})"
                )
        if existing_claim.active or is_grace_expired:
            if is_grace_expired:
                claim = existing_claim
            else:
                claim = supervisor._active_claim(state, worker_key, now)
        else:
            claim = existing_claim
    supervisor._refresh(state, now)
    budget_exhausted = (
        state.status == Status.BUDGET_EXHAUSTED
        or state.budget_exhausted
    )
    expected_target = state.target_fingerprint or supervisor.target_fingerprint
    target_drifted_units: list[str] = []
    for item_unit, item in incoming_evidence.items():
        # This marker is Supervisor-owned; never trust a worker-supplied value.
        item.pop("target_drifted", None)
        item.pop("reproduced_crash", None)
        if item.get("target_fingerprint") != expected_target:
            target_drifted_units.append(item_unit)
    # New work remains fail-closed in claim/start paths.  Checkpoint admission
    # is the recovery boundary for a claim already in hand: budget exhaustion or
    # a worker-reported target drift must not strand its valid artifacts.
    if not budget_exhausted and not target_drifted_units:
        supervisor._assert_target_fingerprint(state, now)
    if len(incoming_coverage) > len(claim.unit_ids):
        raise InvalidCheckpoint(
            f"coverage 单元过多（>{len(claim.unit_ids)}）；当前 Worker 仅认领了 {len(claim.unit_ids)} 个单元"
        )
    if len(incoming_evidence) > len(claim.unit_ids):
        raise InvalidCheckpoint(
            f"evidence 单元过多（>{len(claim.unit_ids)}）；当前 Worker 仅认领了 {len(claim.unit_ids)} 个单元"
        )
    if len(incoming_notes) > len(claim.unit_ids):
        raise InvalidCheckpoint(
            f"notes 单元过多（>{len(claim.unit_ids)}）；当前 Worker 仅认领了 {len(claim.unit_ids)} 个单元"
        )
    # A worker that claimed before a hard deadline may still submit
    # its already-produced final evidence.  It must not heartbeat or
    # claim/execute new work after _refresh marks the run exhausted.
    unknown_coverage = sorted(
        set(incoming_coverage) - set(state.coverage)
    )
    if unknown_coverage:
        raise InvalidCheckpoint(
            "checkpoint 包含未注册 coverage unit: "
            + ", ".join(unknown_coverage)
        )
    unassigned = sorted(
        set(incoming_coverage) - set(claim.unit_ids)
    )
    if unassigned:
        raise InvalidCheckpoint(
            "worker 不能提交未分配的 coverage unit: "
            + ", ".join(unassigned)
        )
    # unit_notes 归属校验必须前移到任何突变/发布之前：否则一个
    # 携带未分配单元手记的 checkpoint 在失败时，findings/coverage/
    # evidence 的部分突变已持久化（fail-open 变成部分提交）。
    unassigned_notes = sorted(
        set(incoming_notes) - set(claim.unit_ids)
    )
    if unassigned_notes:
        raise InvalidCheckpoint(
            "unit_notes 含未分配单元: " + ", ".join(unassigned_notes)
        )
    unexpected_evidence = sorted(
        set(incoming_evidence) - set(incoming_coverage)
    )
    if unexpected_evidence:
        raise InvalidCheckpoint(
            "evidence 必须对应本次 coverage 更新: "
            + ", ".join(unexpected_evidence)
        )
    for unit, item in incoming_evidence.items():
        if item["worker_id"] != worker_key:
            raise InvalidCheckpoint(
                f"evidence[{unit}].worker_id 与提交 worker 不一致"
            )
        if item["run_id"] != state.run_id:
            raise InvalidCheckpoint(
                f"evidence[{unit}].run_id 与当前 Supervisor run 不一致"
            )
        if item["round_no"] != claim.round_no:
            raise InvalidCheckpoint(
                f"evidence[{unit}].round_no 与 worker claim 不一致"
            )
        if item["lease_id"] != claim.lease_id:
            raise InvalidCheckpoint(
                f"evidence[{unit}].lease_id 与 worker lease 不一致"
            )
        expected_target = state.target_fingerprint or supervisor.target_fingerprint
        if item["target_fingerprint"] != expected_target:
            item["target_drifted"] = True
            if unit not in target_drifted_units:
                target_drifted_units.append(unit)
    validate_worker_artifacts(
        workspace_root=supervisor.workspace_root,
        artifact_root=claim.artifact_root,
        evidence=incoming_evidence,
        findings=incoming_findings,
        claimed_at=claim.claimed_at,
        worker_id=claim.worker_id,
        lease_id=claim.lease_id,
        execution_id=claim.execution_id,
        trusted_execution=(
            # DOC-203 D2（审计员 #3 修订）：expired claim 的 execution_state
            # 不再是 "running"，现行判定必然为 False 并拒掉全部带 receipt 的
            # passed evidence——salvage 在其目标场景会 100% 失败。代交通道
            # 显式以受信执行对待，正当性：工件哈希已由 validate_worker_artifacts
            # 在盘上重算 + receipt 仍绑定 claim 自己的 execution 记录 +
            # salvaged_at 一次性落库 + origin_receipt.salvaged_by_host 全程
            # 留痕。这是有记录的例外，不是默认放宽；非 salvage 路径不变。
            salvage
            or is_grace_expired
            or claim.execution_state == "running"
            # F-RSI-20261110-013：judge 追加 verdict 通道（多单元 claim 的
            # 后续单元落账）与 salvage 同强度——工件哈希盘上重算 + receipt
            # 绑定 claim 自己的 execution 记录 + life_delta 强制 0 +
            # coverage 键 ⊆ claim.unit_ids，state=="submitted" 即首个
            # verdict 已合法落账、执行记录真实存在。
            or (append_verdict and existing_claim.state == "submitted")
            or not supervisor._parallel_required(state)
            or budget_exhausted
        ),
    )
    triage_upgrades = {
        unit for unit, note in incoming_notes.items()
        if note_requests_deep_verification(note)
    }
    applied_coverage = dict(incoming_coverage)
    for unit in triage_upgrades:
        if applied_coverage.get(unit) == COVERAGE_COVERED:
            applied_coverage[unit] = COVERAGE_STALE
    missing_evidence = sorted(
        unit for unit, status in applied_coverage.items()
        if status == COVERAGE_COVERED and unit not in incoming_evidence
    )
    if missing_evidence:
        raise InvalidCheckpoint(
            "covered unit 必须提供结构化 passed evidence: "
            + ", ".join(missing_evidence)
        )

    def is_valid_covered_evidence(unit: str, ev: dict[str, object]) -> bool:
        if ev.get("status") == "passed" and ev.get("exit_code") == 0:
            return True
        poc_declared = ev.get("evidence_type") in (
            "reproduction_poc", "exploit_poc",
        ) or ev.get("expected_failure") is True
        if not poc_declared:
            return False
        if not any(
            item.get("unit_id") == unit
            or (
                isinstance(item.get("target_units"), (list, tuple, set, frozenset))
                and unit in item.get("target_units", ())
            )
            for item in incoming_findings
        ):
            return False
        ev["reproduced_crash"] = True
        return True

    invalid_covered_evidence = sorted(
        unit for unit, status in applied_coverage.items()
        if status == COVERAGE_COVERED
        and not is_valid_covered_evidence(unit, incoming_evidence[unit])
    )
    if invalid_covered_evidence:
        raise InvalidCheckpoint(
            "covered unit 的 evidence 必须 status=passed 且 exit_code=0 "
            "(或声明为 reproduction_poc 且提交关联 Finding): "
            + ", ".join(invalid_covered_evidence)
        )
    for unit in target_drifted_units:
        applied_coverage[unit] = COVERAGE_UNVERIFIED
    if target_drifted_units:
        supervisor._append_event(
            state, "checkpoint_admitted_with_target_drift", now,
            worker_id=worker_key, lease_id=claim.lease_id,
            round_no=claim.round_no, execution_id=claim.execution_id,
            checkpoint_id=checkpoint_key,
            expected_target_fingerprint=expected_target,
            evidence_units=sorted(target_drifted_units),
        )
    if claim.execution_state == "claimed":
        if supervisor._parallel_required(state) and not budget_exhausted:
            raise InvalidCheckpoint(
                "并发模式必须先调用 worker-start，不能用 claim 代替真实启动"
            )
        # Serial mode keeps the legacy direct-checkpoint path, but records the
        # implicit start only after all input and artifact validation succeeds.
        claim.execution_state = "running"
        claim.execution_started_at = claim.claimed_at
        claim.last_heartbeat_at = now
        supervisor._append_event(
            state, "worker_started", now, worker_id=claim.worker_id,
            lease_id=claim.lease_id, execution_id=claim.execution_id,
            provider=claim.provider, artifact_root=claim.artifact_root,
            implicit=True,
        )
    # The worker checkpoint is the sole write path from a worker.  Once
    # all evidence has passed the lease/target/artifact checks above,
    # stage it for publication to the Life-local coverage manifest
    # consumed by the launch/post coverage gate.  LOG-08: the actual
    # write is deferred until run-state has been persisted, so the gate
    # never sees `passed` evidence for an uncommitted coverage unit.
    pending_evidence.append(incoming_evidence)
    semantic_findings = mark_semantic_duplicates(
        incoming_findings, round_no=state.round_no,
    )
    existing_by_key = {
        supervisor._finding_key(item): item for item in state.findings
    }
    added: list[str] = []
    duplicates: list[str] = []
    sealed_at = now
    for item in semantic_findings:
        key = supervisor._finding_key(item)
        if key in existing_by_key:
            # 同指纹去重时不得丢弃来源单元链接。poc 型证据的
            # covered 单元靠 finding 的 unit_id/target_units 关联到结算校验
            # （post_coverage_sync）；变体归并场景（判读包单元不同、证据行重叠
            # → 指纹改写到主指纹）在此处被静默跳过会导致主 finding 永远缺失
            # 变体单元链接，post --final 被 fail-closed 拒绝。这里把 incoming 的单元链接并入
            # 既有条目：幂等去重语义不变（不重复计命），只补链接。
            existing = existing_by_key.get(key)
            incoming_units: set[str] = set()
            if isinstance(item, Mapping):
                unit_id = item.get("unit_id")
                if isinstance(unit_id, str) and unit_id:
                    incoming_units.add(unit_id)
                for unit in (item.get("target_units") or []):
                    if isinstance(unit, str) and unit:
                        incoming_units.add(unit)
            if incoming_units and isinstance(existing, dict):
                current_units = {
                    u for u in (existing.get("target_units") or [])
                    if isinstance(u, str) and u
                }
                merged_units = sorted(current_units | incoming_units)
                if merged_units != existing.get("target_units"):
                    existing["target_units"] = merged_units
            duplicates.append(key)
            continue
        enriched = copy.deepcopy(
            supervisor._enrich_gate_finding(item, supervisor.workspace_root)
        )
        # PE-20261005-006（防错+取证，不防恶）：候选钉到产生它的 claim/
        # checkpoint 审计要素。字段全部取自已验证的既有记录，不做新验证；
        # 防恶边界仍是工件哈希复核（verify_observation_artifacts）。
        enriched["origin_receipt"] = {
            "worker_id": worker_key,
            "checkpoint_id": checkpoint_key,
            "lease_id": getattr(claim, "lease_id", "") or "",
            "execution_id": getattr(claim, "execution_id", "") or "",
            "sealed_at": sealed_at,
        }
        if target_drifted_units:
            enriched["origin_receipt"].update(
                target_drifted=True,
                target_drift_reason="checkpoint_admitted_with_target_drift",
            )
        if salvage:
            # DOC-203 D2：宿主代交标记全程透出（结算/报告消费方可见）。
            enriched["origin_receipt"]["salvaged_by_host"] = True
        state.findings.append(enriched)
        existing_by_key[key] = item
        added.append(key)
    state.findings.sort(key=supervisor._finding_key)
    confirmation_gate = supervisor._refresh_confirmation_gate(state)
    state.coverage = merge_coverage_updates(state.coverage, applied_coverage)
    for unit in target_drifted_units:
        # Target-drift admission intentionally overrides covered monotonicity:
        # the result is retained, but cannot be presented as current-target coverage.
        state.coverage[unit] = COVERAGE_UNVERIFIED
    state.coverage = dict(sorted(state.coverage.items()))
    claim.state = "submitted"
    claim.submitted_at = now
    # DOC-203 D2 缺陷修补（build.43）：从未 worker_start 的过期 claim
    # （execution_started_at=None，如被限流/取消的 worker）经 salvage 落账
    # 为 completed 时，from_dict 的 "completed 必须有 execution_started_at"
    # 校验会使整个 state 不可加载（控制面瘫痪）。补 claimed_at 作为执行
    # 起点锚——租约真实开始时刻，非伪造。
    if salvage and claim.execution_started_at is None:
        claim.execution_started_at = claim.claimed_at
    claim.state = "submitted"
    claim.submitted_at = now
    claim.execution_state = "completed"
    claim.execution_finished_at = now
    claim.last_checkpoint_id = checkpoint_key
    claim.last_checkpoint_digest = payload_digest
    if salvage:
        claim.salvaged_at = now
    state.scheduling_metrics.total_completed += 1
    if claim.execution_started_at is not None:
        dur = max(0.0, now - claim.execution_started_at)
        state.scheduling_metrics.total_execution_seconds += dur
        state.scheduling_metrics.max_execution_seconds = max(
            state.scheduling_metrics.max_execution_seconds, dur
        )
    try:
        from . import task_scheduler
        task_scheduler.AdaptiveConcurrencyController(
            min_concurrency=state.budget.min_concurrency,
            max_concurrency=state.budget.max_concurrency_ceiling,
        ).record_outcome(state, "completed")
    except Exception:
        pass
    if incoming_signatures:
        # 有界去重追加：只保留最近 MAX_RECENT_SIGNATURES 条，防止状态膨胀。
        merged = list(state.recent_signatures)
        for signature in incoming_signatures:
            if signature not in merged:
                merged.append(signature)
        state.recent_signatures = merged[-MAX_RECENT_SIGNATURES:]
    if incoming_notes:
        # 每单元保留最新手记；接手/恢复的 worker 在 claim 时读回。
        # 归属校验已前移到本函数开头的 payload 校验段。
        for unit_id, note in incoming_notes.items():
            state.unit_notes[unit_id] = {
                "worker_id": worker_key, "note": note, "at": now,
            }
        if len(state.unit_notes) > MAX_UNIT_NOTES:
            state.unit_notes = dict(
                list(state.unit_notes.items())[-MAX_UNIT_NOTES:],
            )
    if target_drifted_units:
        for unit_id in target_drifted_units:
            note_record = dict(state.unit_notes.get(unit_id, {}))
            note = str(note_record.get("note") or "").strip()
            if note and "checkpoint_admitted_with_target_drift" not in note:
                note = f"{note}; checkpoint_admitted_with_target_drift"
            elif not note:
                note = "checkpoint_admitted_with_target_drift"
            note_record.update(
                worker_id=worker_key,
                note=note,
                at=now,
            )
            state.unit_notes[unit_id] = note_record
        if len(state.unit_notes) > MAX_UNIT_NOTES:
            state.unit_notes = dict(
                list(state.unit_notes.items())[-MAX_UNIT_NOTES:],
            )
    if incoming_suggested and supervisor.workspace_root:
        strategy_path = Path(supervisor.workspace_root) / ".bugx" / "strategy.json"
        if strategy_path.is_file():
            from core.skills import skill_pack
            if skill_pack is not None:
                try:
                    strategy_data = json.loads(strategy_path.read_text(encoding="utf-8"))
                    plan = strategy_data.get("skill_plan")
                    if plan and isinstance(plan, Mapping):
                        patched_plan, added_modules = skill_pack.patch_skill_plan(
                            plan, incoming_suggested,
                        )
                        if added_modules:
                            strategy_data["skill_plan"] = patched_plan
                            strategy_path.write_text(
                                json.dumps(strategy_data, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                            supervisor._append_event(
                                state, "plan_patch", now,
                                worker_id=worker_key,
                                added_modules=added_modules,
                                parent_plan_hash=patched_plan.get("parent_plan_hash"),
                                new_plan_hash=patched_plan.get("plan_hash"),
                            )
                except Exception:
                    pass
    # DOC-196: Worker 提交 Checkpoint 时结算该单元的 Token 消耗
    incoming_tokens = 0
    for ev in incoming_evidence.values():
        if isinstance(ev, Mapping) and "tokens_consumed" in ev:
            try:
                incoming_tokens += int(ev.get("tokens_consumed") or 0)
            except (TypeError, ValueError):
                pass
    if incoming_tokens > 0:
        claim.tokens_consumed += incoming_tokens
        claim.token_method = "worker_reported"
        state.tokens_consumed = getattr(state, "tokens_consumed", 0) + incoming_tokens
        if not hasattr(state, "token_accounting") or state.token_accounting is None:
            state.token_accounting = {}
        for uid in claim.unit_ids:
            state.token_accounting[uid] = state.token_accounting.get(uid, 0) + incoming_tokens

    supervisor._reconcile_parallel(state, now)
    event_payload: dict[str, object] = {}
    if incoming_signatures:
        event_payload["signatures"] = incoming_signatures
    if incoming_notes:
        event_payload["note_units"] = sorted(incoming_notes)
    supervisor._append_event(
        state, "checkpoint_submitted", now, worker_id=worker_key,
        lease_id=claim.lease_id, round_no=claim.round_no,
        added_findings=added, duplicate_findings=duplicates,
        coverage_updates=applied_coverage, life_delta=float(delta),
        evidence_units=sorted(incoming_evidence),
        execution_id=claim.execution_id,
        confirmation_gate=confirmation_gate,
        life_score=state.life_score,
        checkpoint_id=checkpoint_key,
        payload_digest=payload_digest,
        **event_payload,
    )
    if salvage:
        supervisor._append_event(
            state, "checkpoint_salvaged_by_host", now,
            worker_id=worker_key, lease_id=claim.lease_id,
            checkpoint_id=checkpoint_key,
        )
    if claim.attempt >= MAX_WORKER_ATTEMPTS:
        exhausted_units = [
            unit for unit in claim.unit_ids
            if state.coverage.get(unit) not in TERMINAL_COVERED
            and worker_attempts_exhausted(state, unit)
        ]
        if exhausted_units:
            mark_worker_attempts_exhausted(
                state, now, exhausted_units,
                worker_id=worker_key, attempt=claim.attempt,
            )
    if state.status not in {
        Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
    }:
        state.status = (
            Status.BUDGET_EXHAUSTED
            if budget_exhausted else Status.CHECKPOINT
        )
    return state

__all__ = ["apply_checkpoint_operation"]
