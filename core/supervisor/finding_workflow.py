#!/usr/bin/env python3
"""core/supervisor/finding_workflow.py - Finding 决策同步、报告封存、确认与复现验证工作流。"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.ledger import finding_lifecycle
from core.planning import unit_kind
from core.supervisor.checkpoint_pipeline import load_manifest_units
import sys

def _get_as_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get('core.supervisor.audit_supervisor')
    if mod and hasattr(mod, name):
        return getattr(mod, name)
    return default

from .supervisor_models import (
    COVERAGE_COVERED,
    COVERAGE_NOT_APPLICABLE,
    COVERAGE_STALE,
    InvalidCheckpoint,
    InvalidTransition,
    RunState,
    Status,
    SupervisorError,
    default_coverage_manifest,
    target_identity,
    workspace_fingerprint,
)

try:
    from core.supervisor import (
        coverage_sync,
        finding_sync,
    )
    from core.supervisor.checkpoint_pipeline import (
        migrate_coverage,
    )

except ImportError:
    from core.supervisor import (
        coverage_sync,
        finding_sync,
    )
    from core.supervisor.checkpoint_pipeline import (
        migrate_coverage,
    )


if TYPE_CHECKING:
    from .audit_supervisor import Supervisor

logger = logging.getLogger(__name__)

def reopen_finding(
    self, finding_id: str, *, reason: str, expected_revision: int,
) -> RunState:
    """Host-only entry exposed by MCP; reopen never confirms a finding."""
    if not isinstance(finding_id, str) or not finding_id.strip():
        raise InvalidCheckpoint(
        "finding_id 必须是非空字符串，形如 \"cand-275-a\"；"
        "应与此前 submit 的 candidate id 一致"
    )
    if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or expected_revision < 0):
        raise InvalidCheckpoint("reopen 必须提供非负 expected_revision")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise InvalidCheckpoint("reopen 必须提供非空 reason（最多2000字符）")

    def operation(state: RunState, now: float) -> RunState:
        self._check_revision(state, expected_revision)
        if state.completed_at is not None or state.status in {Status.COMPLETE, Status.FAILED}:
            raise InvalidTransition("终态 run 不允许 reopen Finding")
        self._assert_target_fingerprint(state, now)
        matches = [item for item in state.findings
                   if item.get("id") == finding_id or item.get("fingerprint") == finding_id]
        if len(matches) != 1:
            raise InvalidCheckpoint("Finding 不存在或身份不唯一")
        try:
            finding_lifecycle.reopen(matches[0], reason=reason, revision=state.revision + 1)
        except finding_lifecycle.LifecycleError as exc:
            raise InvalidCheckpoint(str(exc)) from exc
        self._refresh_confirmation_gate(state)
        self._append_event(state, "finding_reopened", now, finding_id=finding_id,
                           reason=reason.strip(), previous_revision=expected_revision)
        return state

    return self._mutate(operation)

def admit_business_unit(
    self, unit_id: str, *, risk_score: float, reason: str,
    expected_revision: int | None = None,
) -> RunState:
    """显式授权高风险业务链路；partial coverage 不会被伪装成 complete。"""
    if not isinstance(unit_id, str) or not unit_kind.is_business(unit_id):
        raise InvalidCheckpoint("只允许授权业务 coverage unit")
    if not isinstance(risk_score, (int, float)) or not math.isfinite(float(risk_score)):
        raise InvalidCheckpoint(
        "risk_score 必须是有限数字（0-10），形如 7.5；"
        "不要传 NaN / Infinity / \"7.5\" 这样的字符串"
    )
    if not 70 <= float(risk_score) <= 100:
        raise InvalidCheckpoint("只有 70 <= risk_score <= 100 的业务链路可提前准入")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidCheckpoint("business admission 必须提供 reason")

    def operation(state: RunState, now: float) -> RunState:
        self._check_revision(state, expected_revision)
        self._refresh(state, now)
        # LOG-09：业务准入会写事件、改变后续调度输入，因此同样必须在
        # 「仍在进行中」的 run 上执行；终态 run 不得再追加事件。
        self._ensure_live_for_work(state)
        if unit_id not in state.coverage:
            raise InvalidCheckpoint(f"未知 business coverage unit: {unit_id}")
        if not 70 <= float(risk_score) <= 100:
            raise InvalidCheckpoint("只有 70 <= risk_score <= 100 的业务链路可提前准入")
        state.business_admissions[unit_id] = {
            "risk_score": float(risk_score), "reason": reason.strip()[:512],
        }
        self._append_event(
            state, "business_admission", now, unit=unit_id,
            risk_score=float(risk_score), reason=reason.strip()[:512],
            coverage_state=state.coverage[unit_id],
        )
        return state

    return self._mutate(operation)  # type: ignore[return-value]

def rebind_target(
    self, *, reason: str, expected_revision: int | None = None,
) -> RunState:
    """覆盖计划重建后显式重绑目标指纹；宿主专属的前向恢复。

    计划变更（枚举器升级、业务链路补全）会让旧绑定必然漂移，而
    finish/next-round/reset_budget 都被同一断言拦截，计划前向滚动
    缺少出口。本操作在文件锁下留审计痕迹：记录新旧指纹与 reason、
    释放残留 claim、推进 epoch。

    既有证据留不留，由**本轮重扫结果**决定（``inventory-diff.json``）：
    字节没变的单元沿用覆盖结论，变了的（含删除/重命名）作废重验。没有
    重扫结果时退回严格模式——证据必须匹配新目标，一切重做。
    """
    normalized = reason.strip() if isinstance(reason, str) else ""
    if not normalized:
        raise InvalidCheckpoint("rebind_target 必须提供 reason")

    def operation(state: RunState, now: float) -> RunState:
        self._check_revision(state, expected_revision)
        wf_fn = _get_as_attr("workspace_fingerprint", workspace_fingerprint)
        current = wf_fn(self.workspace_root)
        previous = state.target_fingerprint
        if current == previous:
            raise InvalidTransition("目标指纹未变化，无需重绑")
        manifest = default_coverage_manifest(self.workspace_root)
        lmu_fn = _get_as_attr("load_manifest_units", load_manifest_units)
        current_units = lmu_fn(self.workspace_root, manifest)
        coverage, migration = migrate_coverage(state.coverage, current_units)
        changed = self._rescan_changed_scopes(manifest)
        if changed is None:
            # 没有重扫证据 ⇒ 保守：旧证据一律不认，全部重新覆盖。
            demoted: set[str] = set()
            expected_target = current
        else:
            demoted = self._units_in_scopes(state.coverage, changed)
            expected_target = previous
        # 缺陷 D3：migrate_coverage 只按「清单被重新信任」就把仍存在的 covered
        # 全部一刀切 stale ⇒ finish/recover 后的自动 rebind 把 5/5 打成 4/5，
        # post 被迫续轮，而源码一字未改。判据必须是**证据**而不是状态。
        reinstated = self._reinstate_covered_by_evidence(
            coverage, migration, expected_target=expected_target, excluded=demoted,
        )
        if reinstated:
            self._retarget_evidence(reinstated, current)
        reinstated += self._restore_not_applicable(
            coverage, migration, prior=state.coverage, excluded=demoted,
        )
        self._release_active_claims(state, now, reason="target_rebound")
        state.scheduler_epoch += 1
        old_coverage = dict(state.coverage)
        state.coverage = coverage
        # 授权跟着覆盖结论走：仍为 covered 的业务链路保留，作废的重议。
        state.business_admissions = {
            unit: value for unit, value in state.business_admissions.items()
            if coverage.get(unit) == COVERAGE_COVERED
        }
        # BX-R4-02：记录目标身份，供 assert_target_fingerprint 做
        # "同目标换路径"前滚判定（target_forward_rolled）。
        self._append_event(
            state, "target_rebound", now,
            previous_target_fingerprint=previous,
            target_fingerprint=current,
            target_identity=target_identity(self.workspace_root),
            reason=normalized,
            preserved_findings=len(state.findings),
            previous_coverage=old_coverage,
            coverage_migration={**migration, "reinstated": reinstated},
            invalidated_units=sorted(demoted),
        )
        state.target_fingerprint = current
        return state

    return self._mutate(operation)  # type: ignore[return-value]

def _rescan_changed_scopes(self, manifest: Path) -> set[str] | None:
    """本轮重扫判定的「字节已变 / 删除 / 重命名」scope 集合。

    ``inventory-diff.json`` 由 ``reconcile_discovery_rows`` 按字节哈希算出，
    是目标当前状态的权威观测。文件缺失或损坏返回 ``None``，调用方须按
    严格模式处理——不能把「看不见」当成「没变化」。
    """
    diff_path = manifest.with_name("inventory-diff.json")
    if diff_path.is_symlink() or not diff_path.is_file():
        return None
    try:
        payload = json.loads(diff_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    changes = payload.get("changes") if isinstance(payload, Mapping) else None
    if not isinstance(changes, Mapping):
        return None
    scopes: set[str] = set()
    for key in ("changed", "removed"):
        for value in changes.get(key) or ():
            if isinstance(value, str) and value.strip():
                scopes.add(value.strip())
    for item in changes.get("renamed") or ():
        if isinstance(item, Mapping):
            source = item.get("from")
            if isinstance(source, str) and source.strip():
                scopes.add(source.strip())
    return scopes

def _units_in_scopes(self, coverage: Mapping[str, str], scopes: set[str]) -> set[str]:
    """把 scope 集合映射回覆盖单元 ID（内部单元形如 ``内部:3:src/a.py::f``）。"""
    from core.coverage import module_coverage

    try:
        rows, errors = module_coverage.parse_manifest(
            default_coverage_manifest(self.workspace_root),
        )
    except (OSError, ValueError, TypeError):
        return set(coverage)  # 清单读不了 ⇒ 全部作废（fail-closed）
    if errors:
        return set(coverage)
    affected: set[str] = set()
    for row in rows:
        scope = getattr(row, "path", "").strip().strip("`").rstrip("/")
        # diff 里的 scope 就是 discovery 单元的 scope（可能带 ``::symbol``）；
        # 同时接受文件级匹配，避免枚举器在一轮内改变拆分粒度时漏判。
        if scope and (scope in scopes or scope.split("::", 1)[0] in scopes):
            affected.add(module_coverage.coverage_unit_id(row))
    return affected & set(coverage)

def _restore_not_applicable(
    self,
    coverage: dict[str, str],
    migration: Mapping[str, Any],
    *,
    prior: Mapping[str, str],
    excluded: set[str],
) -> list[str]:
    """把字节未变的 ``not_applicable`` 单元还原为原终态。

    这类单元是 judge 判定「无需复现」的结论，**本来就不产生 coverage-evidence
    记录**，所以走不了 ``_reinstate_covered_by_evidence``。``migrate_coverage``
    会把它们一并降成 ``stale``，若不还原就会从「终态」变成覆盖债务，
    让 ``coverage_complete`` 永远为假、``finish`` 拒绝 COMPLETE。
    """
    restored = sorted(
        unit for unit in migration.get("stale", ())
        if prior.get(unit) == COVERAGE_NOT_APPLICABLE
        and coverage.get(unit) == COVERAGE_STALE
        and unit not in excluded
    )
    for unit in restored:
        coverage[unit] = COVERAGE_NOT_APPLICABLE
    return restored

def _retarget_evidence(self, units: Iterable[str], target_fingerprint: str) -> list[str]:
    """把已恢复单元的既有证据改写到新目标指纹。

    不改写就会留下「state 指向新指纹、证据仍指向旧指纹」的分叉，下一次
    ``post`` 的 checkpoint 结算必然判 stale，等于把本次通过推迟成下次失败。
    """
    path = self._coverage_evidence_path()
    if path is None or not path.is_file():
        return []
    try:
        from core.coverage import coverage_evidence
    except ImportError:  # pragma: no cover
        from core import coverage_evidence  # type: ignore
    try:
        return coverage_evidence.EvidenceStore(
            path, workspace_root=self.workspace_root,
        ).retarget(list(units), target_fingerprint)
    except Exception:
        # 改写失败不能遮蔽恢复结果：证据仍按旧指纹留存，下一轮会如实判 stale。
        return []

def _reinstate_covered_by_evidence(
    self,
    coverage: dict[str, str],
    migration: Mapping[str, Any],
    *,
    expected_target: str,
    excluded: set[str] | None = None,
) -> list[str]:
    """把「证据在 ``expected_target`` 下仍然有效」的 stale 单元恢复为 covered。

    ``migrate_coverage`` 的 stale 只是「清单重读」的信号，不是覆盖失效的证明。
    这里用 ``coverage-evidence`` 做唯一判据：记录存在、工件哈希可复算、目标指纹
    等于 ``expected_target``、无重复/冲突，才算覆盖仍然成立。任何一项不满足（含
    清单里没有该单元、证据缺失、真漂移）都保持 stale —— 宁可多一轮，也不把旧
    结论套到新目标。

    ``excluded`` 是本轮重扫判定字节已变/删除的单元：它们的旧证据必须作废，
    不参与恢复。
    """
    stale_units = [
        unit for unit in migration.get("stale", ())
        if coverage.get(unit) == COVERAGE_STALE and unit not in (excluded or ())
    ]
    if not stale_units:
        return []
    path = self._coverage_evidence_path()
    if path is None or not path.is_file():
        return []
    try:
        from core.coverage import coverage_evidence
    except ImportError:  # pragma: no cover
        from core import coverage_evidence  # type: ignore
    try:
        report = coverage_evidence.validate_manifest(
            path,
            workspace_root=self.workspace_root,
            expected_units=stale_units,
            expected_targets={unit: expected_target for unit in stale_units},
        )
    except Exception:
        # 证据清单损坏/不可读 ⇒ 一律不恢复（fail-closed 到 stale）。
        return []
    valid_units = {record.unit_id for record in report.records}
    blocked = {unit for unit in report.missing}
    for bucket in ("stale", "invalid", "duplicate", "conflicts"):
        for item in getattr(report, bucket, ()) or ():
            if isinstance(item, Mapping):
                unit = item.get("unit_id")
                if isinstance(unit, str):
                    blocked.add(unit)
    reinstated = sorted(
        unit for unit in stale_units
        if unit in valid_units and unit not in blocked
    )
    for unit in reinstated:
        coverage[unit] = COVERAGE_COVERED
    return reinstated

def sync_finding_decisions(
    self, *, expected_revision: int | None = None,
    canonicalize_report_identity: bool = False,
) -> RunState:
    """Merge reviewed lifecycle decisions through the Finding domain service."""
    return self._mutate(
        lambda state, now: finding_sync.apply_decision_sync(
            self, state, now, expected_revision, canonicalize_report_identity,
        )
    )  # type: ignore[return-value]

def sync_finding_decisions_audited(
    self, *, expected_revision: int | None = None,
    canonicalize_report_identity: bool = False,
) -> RunState:
    """DOC-204 L1：sync 的审计包装——拒绝（非 RevisionConflict）时以
    lifecycle_blocked 落权威链后原样抛出；RevisionConflict（良性并发）
    显式排除不落事件不 persist。DOC-212：InvalidCheckpoint 与
    InvalidTransition 是兄弟类（supervisor_models），read_decision_report
    三门（symlink/16MiB/payload）与 apply_decision_sync 的绑定/检查点
    拒绝全抛 InvalidCheckpoint——此前漏网在权威链零痕（D14/D22 盲区），
    并入同一 catch 走既有 record_lifecycle_block。"""
    try:
        return self.sync_finding_decisions(
            expected_revision=expected_revision,
            canonicalize_report_identity=canonicalize_report_identity,
        )
    except (InvalidTransition, InvalidCheckpoint) as exc:
        self.record_lifecycle_block(
            operation="sync", gate="finding_decision_sync",
            reason=str(exc), expected_revision=expected_revision,
        )
        raise

def freeze_report_snapshot(self, *, expected_revision: int | None = None) -> dict[str, object]:
    """Freeze the current sealed report while holding the Supervisor lock."""
    def operation(state: RunState, now: float) -> dict[str, object]:
        self._check_revision(state, expected_revision)
        if state.active_claims:
            raise InvalidTransition("仍有 active worker，不能冻结 Finding 报告")
        if not isinstance(state.report_seal, dict):
            raise InvalidTransition("当前 run 没有可冻结的 report seal")
        phase = str(state.report_consumption.get("phase", "OPEN"))
        if phase == "SETTLED":
            return {"phase": phase, "seal": copy.deepcopy(state.report_seal), "revision": state.revision}
        if phase != "OPEN":
            raise InvalidTransition(f"report consumption phase={phase} 不能冻结")
        state.report_consumption = {
            "phase": "POSTING", "findings_revision": state.findings_revision,
            "report_sha256": state.report_seal.get("report_sha256"), "frozen_at": now,
        }
        self._append_event(state, "finding_report_frozen", now,
                           findings_revision=state.findings_revision,
                           report_sha256=state.report_seal.get("report_sha256"))
        return {"phase": "POSTING", "seal": copy.deepcopy(state.report_seal)}
    return self._mutate(operation)  # type: ignore[return-value]

def settle_report(
    self, receipt: Mapping[str, object], *, expected_revision: int | None = None,
) -> RunState:
    """Publish one settlement receipt against the frozen report snapshot."""
    if not isinstance(receipt, Mapping):
        raise InvalidCheckpoint(
        "settlement receipt 必须是对象，形如 "
        '{"settled_at": 1720000000, "confirmed": 2, "credited": 3}'
        "；不要直接把 JSON 字符串或列表塞进该字段"
    )
    def operation(state: RunState, now: float) -> RunState:
        phase = state.report_consumption.get("phase")
        if phase == "SETTLED" and state.report_consumption.get("receipt") == dict(receipt):
            return state
        self._check_revision(state, expected_revision)
        if phase != "POSTING":
            raise InvalidTransition("只有 POSTING 报告可以结算")
        expected_hash = state.report_consumption.get("report_sha256")
        if receipt.get("report_sha256") != expected_hash:
            raise InvalidCheckpoint("settlement receipt 未绑定冻结 report")
        state.report_consumption = {
            **state.report_consumption, "phase": "SETTLED",
            "settled_at": now, "receipt": copy.deepcopy(dict(receipt)),
        }
        self._append_event(state, "finding_report_settled", now,
                           findings_revision=state.findings_revision,
                           report_sha256=expected_hash)
        return state
    return self._mutate(operation)  # type: ignore[return-value]

def _verification_recipe_digest(self, recipe: Mapping[str, object]) -> str:
    from core.ledger.finding_verification import VerificationRecipe
    return VerificationRecipe.from_mapping(self.workspace_root, recipe).digest()

def _verification_precheck(
    self, finding: str, recipe_digest: str, expected_revision: int | None,
    request_id: str | None,
) -> dict[str, object]:
    with self._file_lock():
        state = self._read_state_locked()
        self._check_revision(state, expected_revision)
        if state.status.value in {"complete", "failed"}:
            raise InvalidTransition(f"run 已结束: {state.status.value}")
        if getattr(state, "verification_protocol_version", 1) != 2:
            raise InvalidTransition("旧 verification protocol 不能授予新执行资格")
        if state.active_claims:
            raise InvalidTransition("仍有 active worker，不能执行 Finding 验证")
        self._assert_target_fingerprint(state, float(self._clock()))
        candidates = [
            item for item in state.findings
            if isinstance(item, dict) and finding in {
                str(item.get("fingerprint") or "").strip(),
                str(item.get("id") or "").strip(),
                str(item.get("finding_id") or "").strip(),
                str(self._finding_key(item) or "").strip(),
            }
        ]
        if len(candidates) != 1:
            raise InvalidCheckpoint(
                f"finding key {'非唯一' if candidates else '不存在'}: {finding}"
            )
        if request_id:
            previous = [
                item for item in state.verification_runs
                if isinstance(item, dict) and item.get("request_id") == request_id
            ]
            if previous:
                receipt = previous[-1]
                if (receipt.get("finding_key") != self._finding_key(candidates[0])
                        or receipt.get("recipe_digest") != recipe_digest):
                    raise InvalidCheckpoint("request_id 已绑定其他 Finding/recipe")
                return {"idempotent": True, "receipt": copy.deepcopy(receipt), "revision": state.revision}
        return {
            "candidate": copy.deepcopy(candidates[0]), "revision": state.revision,
            "run_id": state.run_id, "lifecycle_id": state.life_id or state.round_id,
            "round_no": state.round_no,
        }

def _record_verification(
    self, finding: str, initial_revision: int, receipt: Mapping[str, object],
) -> int | None:
    def operation(current: RunState, now: float) -> RunState:
        self._check_revision(current, initial_revision)
        matches = [
            item for item in current.findings
            if isinstance(item, dict) and finding in {
                str(item.get("fingerprint") or "").strip(),
                str(item.get("id") or "").strip(),
                str(item.get("finding_id") or "").strip(),
                str(self._finding_key(item) or "").strip(),
            }
        ]
        if len(matches) != 1:
            raise InvalidCheckpoint(f"finding key 变为{'非唯一' if len(matches) > 1 else '不存在'}: {finding}")
        matches[0]["verification"] = copy.deepcopy(dict(receipt))
        if receipt.get("status") == "CONFIRMED":
            # L1-ADV-D5：修复后同配方复验会得到 CANDIDATE（缺陷不再复现），
            # 若只保留"最近一次"，原始 CONFIRMED 证据就被覆盖，导致无法再
            # 记录 remediation（确认要求 CONFIRMED receipt）。因此单独留存
            # 最近一次 CONFIRMED 证据，供确认/修复事务复用。
            matches[0]["verification_confirmed"] = copy.deepcopy(dict(receipt))
        current.verification_runs.append(copy.deepcopy(dict(receipt)))
        del current.verification_runs[:-100]
        self._append_event(current, "finding_verification_recorded", now,
                           finding=self._finding_key(matches[0]),
                           verification_status=receipt.get("status"),
                           verification_id=receipt.get("verification_id"),
                           receipt_sha256=receipt.get("verification_digest"),
                           engine_owned=True)
        return current
    updated = self._mutate(operation)
    return getattr(updated, "revision", None)

def confirm_finding(
    self, *, finding: str, decision: str, reason: str,
    expected_revision: int | None = None, rejection_class: str | None = None,
    root_cause_verified: bool | None = None,
    remediation: Mapping[str, object] | None = None,
    oracle: Mapping[str, object] | None = None,
    target_platform: str | None = None,
) -> RunState:
    """Host-owned Finding decision executed inside one Supervisor transaction.

    L1-ADV-D1：旧实现（MCP handler 内联）只写 ``{state, reason, at}``，与 post
    的确认门禁（oracle/observations/negative_control/root_cause_verified/
    target_execution）互斥 ⇒ confirmed 永远无法收敛。现在证据只能来自引擎核验
    receipt，由 ``finding_confirmation.apply_decision`` 合并并自检；不合法即
    抛错，状态对象未被修改（不会留下半成品 confirmed）。
    """
    if not isinstance(finding, str) or not finding.strip():
        raise InvalidCheckpoint("confirm_finding 必须提供 finding id")
    key = finding.strip()
    if decision not in {"confirmed", "rejected"}:
        raise InvalidCheckpoint("decision 只能是 confirmed 或 rejected")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidCheckpoint("confirm_finding 必须提供非空 reason")
    from core.ledger.finding_confirmation import apply_decision

    def operation(state: RunState, now: float) -> RunState:
        self._check_revision(state, expected_revision)
        if state.status.value in {"complete", "failed"}:
            raise InvalidTransition(f"run 已结束: {state.status.value}")
        matches = [
            item for item in state.findings
            if isinstance(item, dict) and item.get("id") == key
        ]
        if len(matches) != 1:
            raise InvalidCheckpoint("Finding 不存在或身份不唯一")
        target = matches[0]
        # 优先用"最近一次 CONFIRMED"证据：修复后的同配方复验会得到 CANDIDATE
        # （缺陷不再复现），不能让它把原始确认证据挤掉（L1-ADV-D5）。
        receipt = target.get("verification_confirmed") or target.get("verification")
        decided = apply_decision(
            target, decision, reason=reason, now=now,
            receipt=receipt, oracle=oracle,
            root_cause_verified=root_cause_verified, remediation=remediation,
            target_platform=target_platform, rejection_class=rejection_class,
            root=self.workspace_root,
        )
        target["confirmation"] = decided["confirmation"]
        if decided["remediation"] is not None:
            target["remediation"] = decided["remediation"]
        self._append_event(
            state,
            "finding_confirmed" if decision == "confirmed" else "finding_rejected",
            now,
            finding_id=key,
            reason=str(reason).strip(),
            channel="confirm_finding",
            verification_id=decided["confirmation"].get("verification_id"),
            remediation_decision=(decided["remediation"] or {}).get("decision"),
            previous_revision=expected_revision,
        )
        self._refresh_confirmation_gate(state)
        return state

    return self._mutate(operation)  # type: ignore[return-value]

def invalidate_coverage(
    self, *, units: Sequence[str], reason: str,
    expected_revision: int | None = None,
) -> dict[str, object]:
    """内容失效的唯一退回路径：``covered -> pending``（``L1-ADV-D4``）。

    ``covered`` 只能由 checkpoint 授予，且属于 ``TERMINAL_COVERED``：目标字节一变，
    旧证据即失效，没有退回路径时 claim 会永久拒绝该单元，单轮内无法重新覆盖。
    事务体在 ``supervisor/coverage_sync.apply_invalidation``：

    * 只退 ``covered``；``not_applicable``/``removed``/``superseded`` 跳过并报告；
    * 活跃租约拒绝失效（``ClaimRejected``：不能从 Worker 手里抽走单元）；
    * 未知单元拒绝（``InvalidCheckpoint``）；
    * 旧 claim 钉成历史代，让同一 Round 可以重新派发。
    """
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidCheckpoint("invalidate_coverage 必须提供非空 reason")
    wanted = coverage_sync.normalize_units(units)
    return self._mutate(
        lambda state, now: coverage_sync.apply_invalidation(
            self, state, now,
            units=wanted, reason=reason, expected_revision=expected_revision,
        )
    )  # type: ignore[return-value]

def verify_finding(
    self, *, finding: str, recipe: Mapping[str, object],
    expected_revision: int | None = None, request_id: str | None = None,
) -> dict[str, object]:
    """Run engine-owned dual positive and fixed negative verification."""
    if not isinstance(finding, str) or not finding.strip():
        raise InvalidCheckpoint("verify_finding 必须提供 finding key")
    if not isinstance(recipe, Mapping):
        raise InvalidCheckpoint(
        "verify_finding recipe 必须是对象，形如 "
        '{"command": ["python3", "probe.py"], "expected": "failure"}'
        "；不要只传命令数组本身"
    )
    key = finding.strip()
    digest = self._verification_recipe_digest(recipe)
    precheck = self._verification_precheck(key, digest, expected_revision, request_id)
    if precheck.get("idempotent"):
        return {"status": precheck["receipt"].get("status"), "finding": key,
                "receipt": precheck["receipt"], "revision": precheck["revision"],
                "idempotent": True}
    from core.ledger.finding_verification import verify_finding
    receipt = verify_finding(
        self.workspace_root, precheck["candidate"], recipe,
        run_id=precheck["run_id"], lifecycle_id=precheck["lifecycle_id"],
        round_no=precheck["round_no"], target_fingerprint=self.target_fingerprint,
        state_revision=precheck["revision"], request_id=request_id,
        artifact_root=self.state_path.parent / "verification",
    )
    revision = self._record_verification(key, int(precheck["revision"]), receipt)
    return {"status": receipt.get("status"), "finding": key,
            "receipt": receipt, "revision": revision}

