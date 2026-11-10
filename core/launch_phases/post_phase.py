#!/usr/bin/env python3
"""launch_phases/post_phase.py - Round 结束协议、生命周期结算、报告生成与会话收尾 (DOC-072)。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.launch_phases.common import (
    AGENT_DIR,
    LESSON_LEDGER_FILE,
    LIFECYCLE_MAX_SMALL_CYCLES,
    LIFECYCLE_MAX_NO_PROGRESS_CYCLES,
    LIFECYCLE_MAX_SECONDS,
    POLLING_DECISION_REQUIRED,
    coverage_manifest_path,
    format_duration,
    resolve_project_root,
    run_verify_life,
    uses_project_layout,
    bugx_paths,
    host_serial_worker,
    host_workspace_dirs,
)
from core.launch_phases.session_runtime import (
    append_runtime_event,
    baseline_json,
    finish_runtime,
    iso_time,
    load_runtime,
    heartbeat_runtime,
    refresh_audit_todo,
    close_runtime,
    runtime_lock,
    save_runtime,
    runtime_summary,
    set_polling_stop,
    start_runtime,
)

def _load_runtime(*args, **kwargs):
    return load_runtime(*args, **kwargs)

def _baseline_json(*args, **kwargs):
    return baseline_json(*args, **kwargs)

def _set_polling_stop(*args, **kwargs):
    return set_polling_stop(*args, **kwargs)
from core.launch_phases.startup_gates import (
    refresh_startup_todo,
    run_cloud_boundary_sync,
    detect_startup_environment,
)
from core.launch_phases.supervisor_bridge import (
    advance_supervisor_round,
    check_supervisor_gate,
    ensure_supervisor,
    recover_supervisor_budget,
    run_prep_validate,
    get_supervisor,
    run_fix_mutation_gate,
)
def _supervisor(*args, **kwargs):
    return get_supervisor(*args, **kwargs)
# 覆盖债务取证的唯一权威实现（R2 审计 P2-3 / P2-4）：产品侧结算收据必须
# 物理导出债务数字，否则 L2-C1 的「回执含 coverage_debt_units」只由测试
# harness 满足，收据无法自证诚实。
from core.coverage.coverage_forensics import coverage_debt_units
# 收据口径契约的**唯一权威实现**（第四批）：产品侧收据核验与 CLI 核验器共用同一份
# 规则——此前两侧各写一套（产品侧那套对债务零校验、工件缺失时静默跳过 = fail-open），
# 真实 live 轮次实测四种伪造全部「校验通过」。规则只留一份 ⇒ 孪生分叉从根上消除。
from core.ledger.receipt_contract import (
    verify_receipt_coverage_contract,
    verify_receipt_terminal_contract,
)
from core.launch_phases.lesson_context import (
    lesson_context,
    lesson_review_matches,
    set_lesson_context,
)
from core.launch_phases.lifecycle_reporter import (
    write_lifecycle_report,
    print_post_intelligence,
)
from core.launch_phases.finding_settlement import (
    auto_settle,
    auto_build_findings_json,
    auto_mode,
    capture_findings_snapshot,
    derive_settle_line,
    gate_confirmed_findings,
    archive_stale_findings_binding,
    settle_findings_path,
    verify_findings_binding,
    FindingsSnapshot,
    FindingsSnapshotError,
)
from core.launch_phases.report_sharing import maybe_share_lifecycle
from core.launch_phases import post_observability

try:
    from core.ledger import finding_confirmation
except ImportError:  # pragma: no cover
    from core import finding_confirmation

try:
    import core.i18n as i18n
except ImportError:
    try:
        from core import i18n
    except ImportError:
        i18n = None  # type: ignore[assignment]


_GIT_COMMIT_TIMEOUT = 60
_GIT_RUNTIME_NAMES = {
    "audit-supervisor.json", "bugx-life.json", "bugx-runtime.json",
    "coverage-evidence.json", "finding-ledger.json", "lesson-ledger.json",
    "module-coverage.md", "prep-record.md", "startup-todo.md",
}
_GIT_HOST_GENERATED_DIRS = {
    # 未迁移宿主的目录基线（逐宿主切片时删除对应项）；已迁移插件由
    # ``core/hosts/<host>.py`` 的 ``install.marker`` / ``generated_dirs`` 声明。
    ".claude", ".zcode",
    ".workbuddy", ".codebuddy", ".pi", ".dsh", ".agents",
} | set(host_workspace_dirs())


_POST_GATE_ALIASES = {
    "open_findings": "finding_open",
}


from core.launch_phases.post_receipt import (
    write_acceptance_receipt,
    git_repo_root,
    resolve_receipt_artifact_dirs,
    ReceiptVerificationResult,
    verify_acceptance_receipt,
)
from core.launch_phases.post_commit import (
    git_failure,
    git_paths,
    is_project_change,
    write_fix_review_draft,
    commit_project_changes,
    rollback_finding_touched_files,
    record_commit_result,
    commit_boundary,
    retry_pending_commit,
)
import core.launch_phases.post_round_lifecycle as _prl
_LAST_POST_FAILURE = _prl._LAST_POST_FAILURE
_LAST_POST_FAILURE_CONTEXT = _prl._LAST_POST_FAILURE_CONTEXT
from core.launch_phases.post_round_lifecycle import (
    get_last_post_failure,
    set_last_post_failure,
    fail_post,
    begin_post_gate,
    finish_post_gate,
    lifecycle_stop_reason,
    continue_small_lifecycle,
    round_result,
    runtime_transaction,
    record_polling_round,
    inherit_prep_record,
    warn_inherit,
    inherit_coverage_evidence,
    prepare_next_round,
    validate_runtime_serial_authorization,
    continue_after_death,
    knowledge_round_result,
    knowledge_review_matches,
)
from core.launch_phases.post_coverage_sync import (
    complete_lifecycle,
    reconcile_checkpoint_coverage,
    content_invalidated_units,
    hand_back_invalidated_units,
    dsh_zstd_reader,
    lifecycle_report_state,
    close_session,
    life_summary,
    print_new_findings,
    attack_graph_degraded,
    read_json_or_none,
    absorb_attack_graph,
)

# Backward compatibility / mock dispatch aliases
_settle_findings_path = settle_findings_path
_write_acceptance_receipt = write_acceptance_receipt
_git_repo_root = git_repo_root
_git_failure = git_failure
_git_paths = git_paths
_is_project_change = is_project_change
_write_fix_review_draft = write_fix_review_draft
_commit_project_changes = commit_project_changes
_record_commit_result = record_commit_result
_commit_boundary = commit_boundary

_fail_post = fail_post
_begin_post_gate = begin_post_gate
_finish_post_gate = finish_post_gate
_lifecycle_stop_reason = lifecycle_stop_reason
_continue_small_lifecycle = continue_small_lifecycle
_round_result = round_result
_runtime_transaction = runtime_transaction
_record_polling_round = record_polling_round
_inherit_prep_record = inherit_prep_record
_warn_inherit = warn_inherit
_inherit_coverage_evidence = inherit_coverage_evidence
_prepare_next_round = prepare_next_round
_validate_runtime_serial_authorization = validate_runtime_serial_authorization
_knowledge_round_result = knowledge_round_result
_knowledge_review_matches = knowledge_review_matches

_complete_lifecycle = complete_lifecycle
_reconcile_checkpoint_coverage = reconcile_checkpoint_coverage
_content_invalidated_units = content_invalidated_units
_hand_back_invalidated_units = hand_back_invalidated_units
_dsh_zstd_reader = dsh_zstd_reader
_lifecycle_report_state = lifecycle_report_state
_print_new_findings = print_new_findings
_attack_graph_degraded = attack_graph_degraded
_read_json_or_none = read_json_or_none
_absorb_attack_graph = absorb_attack_graph


def _reconcile_additive_plan_drift() -> dict[str, object]:
    """把「post 自己扩写覆盖清单」造成的指纹漂移在源头收口。

    ``module_coverage`` 门禁会用自动重扫把新发现的单元补进清单；若本轮还顺手
    改了源码或提交了代码，``git HEAD`` 也会变。两种情况都让
    ``supervisor_parallel`` 判 ``target_drift`` 而死锁。

    重绑是安全的：哪些单元的旧证据还能沿用，由**本轮重扫结果**决定
    （``inventory-diff.json`` 的 ``changed``/``removed``/``renamed`` 对应单元
    一律作废重验，其余沿用）。没有重扫证据时退回严格模式，全部重做。
    """
    try:
        supervisor = _supervisor()
        gate = supervisor.parallel_gate()
        if not isinstance(gate, Mapping) or gate.get("target_drift") is not True:
            return {"status": "ok"}
    except (AttributeError, NotImplementedError):
        return {"status": "ok"}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"status": "error", "detail": str(exc)}
    try:
        before = supervisor.status()
        before_units = {
            unit for unit, status in (getattr(before, "coverage", {}) or {}).items()
            if status != "removed"
        }
        supervisor.rebind_target(
            reason="post 自动重绑：覆盖计划随本轮重扫结果前滚",
        )
        after = supervisor.status()
    except (OSError, ValueError, AttributeError, RuntimeError) as exc:
        return {"status": "drift", "detail": str(exc)}
    added = sorted(set(getattr(after, "coverage", {}) or {}) - before_units)
    _refresh_acceptance_receipt_after_rebind()
    return {"status": "rebound", "added_count": len(added), "added": added}


def _refresh_acceptance_receipt_after_rebind() -> None:
    """重绑后刷新已存在的验收收据工件哈希（否则下次自检判哈希漂移）。"""
    from core.launch_phases.post_receipt import refresh_receipt_supervisor_hash

    try:
        active = bugx_paths.require(resolve_project_root())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return
    try:
        refresh_receipt_supervisor_hash(
            active.supervisor,
            (
                active.state_root / "acceptance-evidence-receipt.json",
                active.lifecycle_root / "acceptance-evidence-receipt.json",
            ),
        )
    except (OSError, ValueError):
        # 收据不存在或不可解析时无需刷新；写入失败不得阻断本轮结算。
        pass


def post(
    final: bool = False, share: str | None = None, *, allow_incomplete: bool = False,
    allow_serial: bool = False, dry_run: bool = False,
    _post_attempt_token: object | None = None,
) -> int:
    """Own an attempt for direct calls or claim the dispatcher token once."""
    if dry_run:
        # Keep the direct library affordance used by older callers, but route
        # it through the same frozen, read-only snapshot evaluator as the CLI.
        from core.launch_phases import post_dry_run
        result = post_dry_run.post_dry_run(resolve_project_root())
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
        return (
            0 if result.report.get("status") == "passed"
            and result.report.get("coherence") == "proven" else 1
        )
    parent_attempt = post_observability.current_attempt()
    with post_observability.attempt_scope(
        "direct", attempt_token=_post_attempt_token,
    ) as attempt:
        owner = attempt is not parent_attempt
        _LAST_POST_FAILURE_CONTEXT.set("")
        if _post_attempt_token is not None and not attempt.token_claim_rejected:
            post_observability.begin_gate("attempt_token_claim")
            post_observability.finish_gate(status="ok")
        if attempt.token_claim_rejected:
            if owner:
                post_observability.log_attempt_finished(1)
            return 1
        _begin_post_gate("runtime_load")
        try:
            result = _post_impl(
                final=final, share=share,
                allow_incomplete=allow_incomplete,
                allow_serial=allow_serial,
            )
        except BaseException as exc:
            exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
            if isinstance(exc, SystemExit):
                code = exc.code
                exit_code = code if isinstance(code, int) and code != 0 else 1
            post_observability.record_unhandled_exception(exc, exit_code=exit_code)
            if owner:
                post_observability.log_attempt_finished(exit_code)
            raise
        if owner:
            post_observability.log_attempt_finished(result)
        return result


def _post_impl(
    final: bool = False, share: str | None = None, *, allow_incomplete: bool = False,
    allow_serial: bool = False,
) -> int:
    # DES-06：``run_module_coverage`` 已下沉到共享层；``_round_result`` /
    # ``_record_polling_round`` / ``_continue_small_lifecycle`` / ``_continue_after_death``
    # 已下沉到本模块（它们唯一的调用方都在这里），不再反向依赖 round_orchestrator。
    from core.launch_phases.common import coverage_ratio_failure, run_module_coverage

    print("=" * 56)
    print("bugx 结束协议：核对 life 变化 → 异常回滚")
    print("=" * 56)
    try:
        heartbeat_runtime()
        active_runtime = load_runtime()
    except (OSError, ValueError) as exc:
        return _fail_post(f"无法读取当前运行会话: {exc}", gate_status="error")
    _finish_post_gate("ok")
    post_observability.set_identity(
        runtime_run_id=active_runtime.get("run_id"),
        lifecycle_id=active_runtime.get("lifecycle_id"),
        polling_id=active_runtime.get("polling_id") or active_runtime.get("session_id"),
    )
    _begin_post_gate("runtime_active")
    if not active_runtime.get("active") or active_runtime.get("state") not in {"running", "finished"}:
        return _fail_post(
            "post 必须紧接对应的活动 pre/runtime 会话执行，拒绝复用旧快照结算",
            gate="runtime_active",
        )
    _finish_post_gate("ok")
    quiet_auto = active_runtime.get("review_mode") == "mcp-auto"
    _begin_post_gate("lifecycle_open")
    if active_runtime.get("lifecycle_terminal"):
        return _fail_post("当前 Round 已结算，不能重复 post")
    _finish_post_gate("ok")
    serial_authorized = False

    def _restore_rollback_binding() -> None:
        """回滚 life 快照时同步把 current.json 指回本会话的 Round。

        自动续轮会切换 current.json 到新生命周期；失败回滚若只还原
        life/runtime 而不还原会话绑定，两边就永久分叉（runtime 指向
        旧 Round、current.json 指向新 Round），后续 pre/post 全部
        挂接到错误生命周期。
        """
        try:
            import core.bugx_paths as bugx_paths
            lifecycle_id = str(active_runtime.get("lifecycle_id") or "")
            polling_id = str(
                active_runtime.get("polling_id")
                or active_runtime.get("session_id")
                or ""
            )
            if lifecycle_id and polling_id:
                bugx_paths.restore_current(
                    resolve_project_root(), polling_id, lifecycle_id,
                )
        except (OSError, ValueError, RuntimeError, ImportError) as exc:
            print(f"[post] ⚠ 会话绑定恢复失败: {exc}")

    def _restore_preserving_evidence() -> None:
        """Restore authoritative source state without deleting audit evidence.

        ``restore`` is allowed to replace the life snapshot, but preparation
        records and checkpoint evidence are append-only forensic artifacts.
        Keep their bytes across the restore boundary so a failed post cannot
        erase the very evidence needed to diagnose the failure.
        """
        preserved: dict[Path, bytes] = {}
        try:
            import core.bugx_paths as bugx_paths
            active = bugx_paths.require(resolve_project_root())
            # BX-ENGINE-26：此处曾写 `active.coverage_evidence`——`coverage_evidence`
            # 是**模块**而不是 `Scope` 字段，因此只要布局可解析就抛 AttributeError，
            # 且该异常不在下方捕获列表内 → **恢复路径自身崩溃**，把"保全证据"的
            # 承诺变成"保全失败"（越需要它的时候越失败）。证据记录的真实载体是
            # 覆盖清单（`coverage_evidence.EvidenceStore(scope.coverage, ...)` 正
            # 是从它读取），故改为 `active.coverage`。
            for path in (active.prep_record, active.coverage):
                if path.is_file() and not path.is_symlink():
                    preserved[path] = path.read_bytes()
        except (OSError, ValueError, RuntimeError, ImportError, AttributeError) as exc:
            # 不静默：布局/字段解析失败必须可见（否则会误以为旁证已保全）。
            print(f"[post] ⚠ 保全旁证时无法解析布局或字段: {exc}")
        run_verify_life("restore")
        for path, payload in preserved.items():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.is_symlink():
                    path.write_bytes(payload)
            except OSError as exc:
                print(f"[post] ⚠ 保留旁证失败 {path}: {exc}")

    _begin_post_gate("git_diff")
    recover_supervisor_budget()
    if run_verify_life("diff") != 0:
        print("→ diff 检出异常，回滚到基线快照…")
        _restore_preserving_evidence()
        _restore_rollback_binding()
        return _fail_post(
            "已回滚到启动前基线。",
            "  请复核 bugx 的报告：findings 是否真实存在、修复是否真转绿。",
        )
    _finish_post_gate("ok")
    _begin_post_gate("checkpoint_reconcile")
    try:
        reconciled = _reconcile_checkpoint_coverage()
        if reconciled:
            print(f"[post] ✓ 已从校验通过的 Supervisor checkpoint 更新覆盖清单：{reconciled} 项")
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        print("→ 保持 fail-closed，回滚到基线快照…")
        _restore_preserving_evidence()
        _restore_rollback_binding()
        return _fail_post(
            f"Supervisor checkpoint 与 lifecycle 覆盖清单无法一致结算: {exc}",
            gate_status="error",
        )
    _finish_post_gate("ok")
    _begin_post_gate("module_coverage")
    if run_module_coverage(final=final and not allow_incomplete) != 0:
        # L1-ADV-D4：内容失效不是"覆盖比例不足"，`--allow-incomplete` 治不了它。
        # 先把失效单元交回 Supervisor（收回 covered 终态），在同一轮内重新派发。
        try:
            handback = _hand_back_invalidated_units()
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError) as exc:
            print("→ 内容失效单元无法交回调度器，保持 fail-closed…")
            _restore_preserving_evidence()
            _restore_rollback_binding()
            return _fail_post(
                f"内容失效单元无法交回调度器: {exc}",
                gate="module_coverage", gate_status="error",
            )
        if handback.get("invalidated"):
            print(
                "[post] ✗ COVERAGE_CONTENT_INVALIDATED："
                f"{len(handback['invalidated'])} 个单元因内容变化失效，"
                "已退回可派发状态（旧证据作废）："
                + ", ".join(handback["invalidated"][:20])
            )
            print("  next_action=REDISPATCH_UNITS")
            print("  请重新 claim 并重新核验这些单元（新内容 → 新证据），然后重跑 post。")
            print("  `--allow-incomplete` 不能替代重新覆盖：它只接受诚实的未覆盖债务。")
            _restore_preserving_evidence()
            _restore_rollback_binding()
            return _fail_post("覆盖证据因内容变化失效，需在轮内重新覆盖后重跑 post。")
        print("→ 最小单元/业务链路覆盖门禁失败，回滚到基线快照…")
        # DOC-203 D1-b（审计员 #3 修订）：仅当失败形态是"覆盖比例不足"
        # （清单本身有效、存在未覆盖单元）时才提示 --allow-incomplete；
        # 结构无效的清单走该旗标只会二次失败，不得误导。
        if not allow_incomplete and coverage_ratio_failure():
            print(
                "  提示：若失败原因为覆盖比例不足（清单有效、预算内未跑完），"
                "且接受诚实的不完整收口，可显式 `post --allow-incomplete` 重跑；"
                "该旗标不可用于规避真实覆盖义务。"
            )
        _restore_preserving_evidence()
        _restore_rollback_binding()
        return _fail_post("最小单元/业务链路清单无效或未达到覆盖要求。")
    _finish_post_gate("ok")
    # 覆盖清单刚被 auto-backfill 扩写过，指纹可能因此漂移（本轮提交的修复代码
    # 同样会改变 git HEAD）。按本轮重扫结果前滚重绑，避免 supervisor_parallel
    # 把「自己刚写的清单」判成外部漂移。
    _begin_post_gate("target_reconcile")
    drift = _reconcile_additive_plan_drift()
    if drift.get("status") == "rebound":
        print(
            "[post] ✓ 覆盖计划随本轮重扫前滚，已重绑目标指纹："
            f"{drift['added_count']} 个新增单元待覆盖；"
            "字节已变/删除的单元旧证据已作废，需重新覆盖"
        )
    elif drift.get("status") == "error":
        print(f"[post] ⚠ 目标重绑检查失败，交由并发门禁裁决: {drift['detail']}")
    _finish_post_gate("ok")
    _begin_post_gate("prep_validate")
    if run_prep_validate() != 0:
        print("→ 开工准备记录不完整，回滚到基线快照…")
        _restore_preserving_evidence()
        _restore_rollback_binding()
        return _fail_post("缺项目识别、工具调研/选择/就绪或协作记录。")
    _finish_post_gate("ok")
    auto_serial_host = host_serial_worker(active_runtime.get("host"))
    if allow_serial or auto_serial_host:
        # Keep the capability receipt check observable as its own gate.  It
        # must finish before the Supervisor parallel gate starts because the
        # attempt-local tracker permits only one active gate at a time.
        _begin_post_gate("serial_authorization")
        from core.supervisor.serial_authorization import SerialAuthorizationError
        try:
            _validate_runtime_serial_authorization(active_runtime)
            serial_authorized = True
        except SerialAuthorizationError as exc:
            return _fail_post(
                f"串行 host 缺少有效的 pre 授权: {exc}",
                gate="serial_authorization",
            )
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError) as exc:
            return _fail_post(
                f"串行 host 无法验证 pre 授权: {exc}",
                gate="serial_authorization",
                gate_status="error",
            )
        _finish_post_gate("ok")
    _begin_post_gate("supervisor_parallel")
    try:
        gate_ok, gate = check_supervisor_gate()
    except (OSError, ValueError, AttributeError, RuntimeError) as exc:
        return _fail_post(f"并发 Supervisor 检查失败: {exc}", gate_status="error")
    serial_override = (
        serial_authorized
        and not gate.get("active_workers")
        and not gate.get("expired_workers")
        and int(gate.get("max_active_workers", 0) or 0) >= 1
        and set(gate.get("reasons", [])) <= {"parallel_peak_below_two"}
    )
    if serial_override:
        gate_ok = True
        print("并发门禁：已验证合法串行凭证，允许单 Worker 收尾。")
    if not gate_ok:
        return _fail_post(
            "并发门禁未满足：Life 未满足（"
            + ", ".join(str(item) for item in gate.get("reasons", []))
            + "）",
            "  请先并发完成 worker checkpoint；共享资源冲突必须在启动时给出串行原因。",
        )
    _finish_post_gate("ok")

    _begin_post_gate("finding_open")
    try:
        supervisor_status = get_supervisor().status()
        post_observability.set_identity(
            supervisor_run_id=getattr(supervisor_status, "run_id", None),
        )
        open_findings = finding_confirmation.open_findings(
            getattr(supervisor_status, "findings", []), resolve_project_root(),
        )
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        return _fail_post(f"无法核对 Finding 收敛状态: {exc}", gate_status="error")
    if open_findings:
        fix_mode = (baseline_json() or {}).get("mode", "auto")
        if fix_mode == "log-only":
            print(
                f"[launch_bugx] ℹ 处于仅记录模式（fix_mode=log-only），保留 {len(open_findings)} 个待修复缺陷并允许完成收敛："
                + ", ".join(str(item["id"]) for item in open_findings[:20])
            )
        else:
            return _fail_post(
                "open_findings",
                "仍有未关闭 Finding，禁止把当前 Round 标记为完成："
                + ", ".join(str(item["id"]) for item in open_findings[:20]),
                "  自动模式将保留活动状态，宿主必须完成修复/Live 复验后重试 post。",
                "  若本轮只需记录 Finding，可先用 pre --fix-mode log-only 再重试收口。",
            )
    _finish_post_gate("ok")
    print(
        f"并发门禁：required={gate.get('required')}，"
        f"peak={gate.get('max_active_workers')}，"
        f"overlap={gate.get('overlap_seconds')}s"
    )

    review_state = baseline_json() or {}
    review_history = review_state.get("history") or []
    current_round = review_state.get("round", 1)
    last_settled = review_history[-1] if review_history else None
    round_at_start = active_runtime.get("round_at_start")
    segment_settled = isinstance(last_settled, dict) and (
        last_settled.get("round") == current_round
        or (isinstance(current_round, int)
            and isinstance(round_at_start, int)
            and current_round > round_at_start
            and last_settled.get("round") == current_round - 1)
    )
    findings_snapshot: FindingsSnapshot | None = None
    _begin_post_gate("findings_snapshot")
    try:
        if quiet_auto and auto_mode(active_runtime) and settle_findings_path(active_runtime) is None:
            auto_build_findings_json(active_runtime)
        # 遗留 binding 归档（2026-11-09 实测修复 findings_binding_mismatch 死锁）：
        # findings.json 绑定指向**已终结的旧 run** 时（auto 续轮换 run_id 后旧
        # 文件还在），那是旧 Life 的合法产物——归档回它所属的位置而不是拒绝
        # 当前 Life 结算。当前会话真损坏（同 run_id）不受影响，照常报错。
        archived_note = archive_stale_findings_binding(active_runtime)
        if archived_note:
            print(f"[post] {archived_note}")
        findings_snapshot = capture_findings_snapshot(active_runtime)
    except FindingsSnapshotError as exc:
        detail = getattr(exc, "detail", "") or ""
        suffix = f"：{detail}" if detail else ""
        return _fail_post(f"Finding 快照校验失败：{exc.reason_code}{suffix}")
    _finish_post_gate("ok")
    review_blocked = False
    if segment_settled:
        pre_review_result = _round_result()
    else:
        _begin_post_gate("finding_confirmation")
        credited, lines, _gate_note = gate_confirmed_findings(
            active_runtime, findings_snapshot, snapshot_captured=True,
            allow_incomplete=allow_incomplete,
        )
        if credited is None:
            # DOC-195: 真实根因透传，拒绝将确认门禁或运行绑定失败折叠丢弃
            return _fail_post(
                f"Finding 确认门禁未通过：{_gate_note}",
                "  请修正 Finding 终态/绑定后重跑 post。",
            )
        _finish_post_gate("ok")
        pre_review_result = {
            "round": current_round,
            "findings": len(lines),
            "credited": credited,
        }
    # R5 复盘 D5：复盘时序原子化。写入类复盘动作（ensure_auto_review/
    # materialize）延后到 _auto_settle 成功之后执行——结算失败时不再留下
    # 已固化的终态复盘（R5 实测 16 条旧 review 卡死 20 条结算的诱因）。
    # settle 前只保留只读校验：
    #   - 外部 review 模式：_knowledge_review_matches 仍要求 Supervisor 在
    #     结算前提交与结算预期一致的 cycle-review（不写库）；
    #   - mcp-auto 模式：复盘尚未写入，无行可校验，交由结算成功后的
    #     “复盘门禁未满足（结算后复核）”兜底（对账对象是真实结算结果）。
    if not quiet_auto:
        _begin_post_gate("lesson_review_pre_settle")
        review_ok, review_note = _knowledge_review_matches(
            _knowledge_round_result(pre_review_result, active_runtime),
            active_runtime,
        )
        if not review_ok:
            return _fail_post(
                f"复盘门禁未满足：{review_note}。",
                "请由外部 Supervisor 提交 lesson_ledger.py review [--supersede]，"
                "并使 finding_count/credited_count/fingerprint 与结算结果一致；"
                "明确复用/新增经验或填写 no_new_lesson_reason。",
            )
        _finish_post_gate("ok")
    _begin_post_gate("finding_binding")
    binding_ok, binding_note = verify_findings_binding(
        active_runtime, snapshot=findings_snapshot, snapshot_captured=True,
    )
    if not binding_ok:
        return _fail_post(
            f"findings 运行绑定校验失败：{binding_note}",
            "  拒绝完成本轮结算记录；请复核报告来源后重跑 post。",
        )
    _finish_post_gate("ok")

    if active_runtime.get("life_kind") == "fix":
        _begin_post_gate("fix_mutation")
        mutation_result = run_fix_mutation_gate(
            resolve_project_root(),
            active_runtime.get("regression"),
            report_only=bool(active_runtime.get("fix_mutation_report_only", True)),
        )
        with _runtime_transaction(active_runtime) as state:
            state["fix_mutation_report"] = mutation_result
        print(
            "fix Life mutation 验收："
            f"status={mutation_result.get('status')} "
            f"score={mutation_result.get('mutation_score', 'n/a')}"
        )
        if mutation_result.get("status") in {"invalid", "error", "not_applicable", "rejected"}:
            return _fail_post(
                "fix Life mutation 门禁未通过；不会继续结算。",
                gate_status=(
                    "error" if mutation_result.get("status") == "error" else "blocked"
                ),
            )
        _finish_post_gate("ok")

    _life_state = baseline_json() or {}
    if _life_state.get("alive", True):
        _begin_post_gate("settlement")
        if not auto_settle(
            active_runtime, findings_snapshot, snapshot_captured=True,
        ):
            # R5 复盘 D5：走到这里时复盘必然还未写入（写入动作已全部
            # 下沉到结算成功之后），结算失败直接 fail，不留下任何终态复盘。
            return _fail_post(
                "自动结算被拒：请先完成候选终态确认（finding_confirmation transition 到 confirmed/rejected）或修正 findings.json 后重跑 post。"
            )
        _finish_post_gate("ok")
    if quiet_auto:
        # R5 复盘 D5：结算成功后才执行写入类复盘动作（幂等；重跑时
        # _auto_settle 对已结算 Round 直接放行，复盘补写语义不变）。
        # DOC-208 D1：Review/Pattern/Incident/feedback 必须由单次 projection
        # 和一次 revision-checked ledger transaction 一并提交。
        try:
            _begin_post_gate("knowledge_materialization")
            from core.retrieval.knowledge_auto import materialize_confirmed_knowledge
            materialized = materialize_confirmed_knowledge(
                LESSON_LEDGER_FILE, active_runtime,
                _knowledge_round_result(_round_result(), active_runtime),
                findings_snapshot=findings_snapshot,
                findings_snapshot_loaded=True,
            )
            review_status = materialized.get("review_status")
            if review_status == "recorded":
                print("✓ 自动复盘已记录：confirmed Finding 已完成分类并生成 candidate Pattern。")
            elif review_status == "superseded":
                replaced = materialized.get("replaced_review") or {}
                recorded = materialized.get("review") or {}
                print(
                    "✓ 自动复盘已自愈替换："
                    f"旧值 {replaced.get('finding_count', '?')}"
                    f"/{replaced.get('credited_count', '?')}"
                    f" → 新值 {recorded.get('finding_count', '?')}"
                    f"/{recorded.get('credited_count', '?')}。"
                )
            if materialized.get("incidents_created") or materialized.get("tool_feedback"):
                print(
                    "✓ 自动错题/工具经验已沉淀："
                    f"incidents={materialized.get('incidents_created', 0)}，"
                    f"tool_feedback={materialized.get('tool_feedback', 0)}。"
                )
            incident_ids = [str(item) for item in materialized.get("incident_ids", [])]
            signature_keys = [
                str(item) for item in materialized.get("signature_keys", [])
            ]
            if incident_ids or signature_keys:
                with _runtime_transaction(active_runtime) as state:
                    for field in ("lifecycle_incident_ids", "realized_fix_incidents"):
                        values = list(state.get(field, []))
                        values.extend(item for item in incident_ids if item not in values)
                        state[field] = values[-100:]
                    recent = [str(item) for item in state.get("lesson_recent_signatures", [])]
                    for signature_key in reversed(signature_keys):
                        if signature_key not in recent:
                            recent.insert(0, signature_key)
                    state["lesson_recent_signatures"] = recent[:20]
            _finish_post_gate("ok")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            return _fail_post(
                f"自动复盘与知识沉淀失败：{exc}",
                "  当前 runtime 保持活动状态；修正账本后重跑 post。",
                gate="knowledge_materialization",
                gate_status="error",
            )
    run_cloud_boundary_sync(active_runtime, "project")
    _absorb_attack_graph(active_runtime)
    try:
        round_result = _round_result()
        _begin_post_gate("lesson_review_post_settle")
        settled_review_ok, settled_review_note = _knowledge_review_matches(
            _knowledge_round_result(round_result, active_runtime),
            active_runtime,
        )
        if not settled_review_ok:
            return _fail_post(
                f"复盘门禁未满足（结算后复核）：{settled_review_note}。",
                "请由外部 Supervisor 按 settle 结果修正 lesson_ledger.py review，"
                "并使 finding_count/credited_count/fingerprint 与结算结果一致；"
                "修正后重跑 post 即可重试收尾。",
                gate="lesson_review_post_settle",
            )
        _finish_post_gate("ok")
        realized_incidents = active_runtime.get("realized_fix_incidents", [])
        if round_result.get("credited", 0) > 0 and not realized_incidents:
            if not quiet_auto:
                print(
                    "⚠ 本轮 credited>0，但未调用 fix-review 沉淀 incident 案例。"
                )
                print(
                    "  建议对已修复问题补充 修前/修后/决策链/commit，"
                    "避免同类问题下次从零排查。"
                )
        runtime = _record_polling_round(round_result)
        draft_path = _write_fix_review_draft(runtime, round_result)
        needs_stop_reason = final and not runtime.get("lifecycle_stop_reason")
        if draft_path is not None or needs_stop_reason:
            with _runtime_transaction(active_runtime) as state:
                if draft_path is not None:
                    state["fix_review_draft_path"] = str(draft_path)
                    append_runtime_event(
                        state, "fix-review-draft", time.time(),
                        f"auto-recorded receipt-backed settlement: {draft_path}",
                    )
                if needs_stop_reason:
                    state["lifecycle_stop_reason"] = (
                        "coverage_incomplete" if allow_incomplete
                        else "coverage_target_completed"
                    )
                    state["lifecycle_outcome"] = "INCOMPLETE" if allow_incomplete else "COMPLETE"
            runtime = state
            if draft_path is not None:
                print(f"✓ 已自动记录凭证加权修复结案：{draft_path}")
        life_after = round_result.get("life_after")
        runtime, commit_ok = _commit_boundary(runtime, round_result, final=final)
        if not commit_ok:
            return _fail_post(
                "生命周期边界提交失败", gate="settlement", gate_status="error",
            )
        if (isinstance(life_after, int) and life_after <= 0) or \
                runtime.get("lifecycle_stop_reason") or final:
            run_cloud_boundary_sync(runtime, "global")
        runtime = finish_runtime()
        refresh_audit_todo(runtime, "life-settled")
        if not quiet_auto:
            try:
                print_post_intelligence(runtime, round_result)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                print(f"  （效率/校准卡片输出失败: {exc}）")
    except (OSError, ValueError) as exc:
        return _fail_post(
            f"运行计时结算失败: {exc}", gate="settlement", gate_status="error",
        )
    if not quiet_auto:
        _print_new_findings()
    try:
        # 回归看护此前用裸名 `import supervisor_bridge`——模块迁移进
        # launch_phases 包后，引擎环境（sys.path 只有 core）必然
        # ModuleNotFoundError，每轮结算都打"巡查不可用"警告却从未真正
        # 巡查过（回归锚：test_post_phase_regression_guard_import）。
        from core.launch_phases.supervisor_bridge import (
            check_fix_regressions as _check_fix_regressions,
        )
        _check_fix_regressions(resolve_project_root())
    except Exception as exc:
        error_type = type(exc).__name__
        if not error_type.isascii() or not error_type.isidentifier() or len(error_type) > 64:
            error_type = "Exception"
        print(
            "⚠ [BugX 回归看护] 结算后巡查不可用 "
            f"(status=ERROR, error_type={error_type})；可检查安装资源后重试。"
        )
    suffix = (
        "，覆盖未完成，已按显式 --allow-incomplete 封存为 INCOMPLETE"
        if final and allow_incomplete else
        "，内部与业务双 100% 覆盖通过" if final else
        "，两阶段覆盖清单结构有效"
    )
    print(f"✓ 本轮结算正常，life 变化在合法范围内{suffix}。")
    print(f"  累计运行计时: {format_duration(runtime['effective_total_seconds'])}")
    summary = life_summary()
    life = summary.get("life") if summary else None
    exit_code = 0
    stop_reason = runtime.get("lifecycle_stop_reason")
    if final or (isinstance(life, int) and life > 0 and stop_reason):
        if final:
            print(
                f"■ 最终结算完成（--final）："
                f"{'覆盖未完成（INCOMPLETE）' if allow_incomplete else '覆盖目标达成'}"
                f"（{stop_reason or 'coverage_target_completed'}）。"
                "生成 Round 报告并封存当前 Round。"
            )
        else:
            print(
                f"■ Round 触发安全停止：{stop_reason}。"
                "即使 life 仍大于 0，也必须生成 Round 报告并停止。"
            )
        _begin_post_gate("lifecycle_finalize")
        try:
            lifecycle = _complete_lifecycle(
                write_lifecycle_report(_lifecycle_report_state(runtime))
            )
        except (OSError, ValueError) as exc:
            return _fail_post(
                f"Round 报告生成或生命周期完成处理失败: {exc}",
                gate_status="error",
            )
        print(f"✓ Round 报告已生成：{lifecycle.get('lifecycle_last_report')}")
        refresh_audit_todo(lifecycle, "round-completed")
        runtime = close_session(runtime.get("round_id") or runtime.get("lifecycle_id"), reason="completed")
        _finish_post_gate("ok")
        outcome = "INCOMPLETE" if (final and allow_incomplete) else "COMPLETE"
        share_state = maybe_share_lifecycle(runtime, lifecycle, share)
        refresh_startup_todo(
            "report",
            reason="Round 报告已封存，正在处理结论共享",
            environment=detect_startup_environment(),
        )
        refresh_audit_todo(
            runtime,
            "share-pending" if share_state == "pending" else "share-completed",
        )
        if share_state != "pending":
            refresh_startup_todo(
                "end",
                reason="当前 Round 已共享或明确不共享，Session 已自动收尾",
                environment=detect_startup_environment(),
            )
        # DOC-154: 原子生成标准化验收凭证收据 (第 6 完成门)，必须在所有 runtime/todo 写入完成后计算哈希
        _begin_post_gate("acceptance_receipt")
        receipt_path = _write_acceptance_receipt(runtime, outcome=outcome)
        if outcome == "COMPLETE":
            if not receipt_path or not receipt_path.is_file():
                return _fail_post("验收收据生成失败，拒绝以 COMPLETE 声明收官", gate="acceptance_receipt")
            verification = verify_acceptance_receipt(receipt_path)
            if not verification.get("verified"):
                return _fail_post(
                    f"验收收据反向自检失败: {verification.get('reason')}",
                    gate="acceptance_receipt",
                )
        _finish_post_gate("ok")
        return 0
    if isinstance(life, int) and life > 0:
        next_round = summary.get("round", "?")
        print(f"↻ Life 结算完成（life={life}），这里只是 Round 检查点。")
        print(
            f"  Round {runtime.get('lifecycle_no', '?')} 仍在进行："
            f"已完成 {runtime.get('lifecycle_cycles_completed', 0)} 个 Life，"
            f"累计有效 Bug {runtime.get('lifecycle_credited_total', 0)} 条。"
        )
        _begin_post_gate("next_life_start")
        try:
            next_runtime, started = _continue_small_lifecycle()
        except (OSError, ValueError, AttributeError, RuntimeError) as exc:
            return _fail_post(f"下一 Life 启动失败: {exc}", gate_status="error")
        if not started:
            return _fail_post("下一 Life 未建立活动 runtime，已停止推进。")
        _finish_post_gate("ok")
        print(
            f"  已自动进入下一个 Life（verify_life.round={next_round}，"
            f"run_id={next_runtime.get('run_id')}）。"
        )
        refresh_audit_todo(next_runtime, "running")
        refresh_startup_todo(
            "mining",
            reason="当前 Life 已结算，已自动进入下一个 Life",
            environment=detect_startup_environment(),
        )
    elif isinstance(life, int):
        print("✝ Life life≤0：当前 Round 完成，开始生成 Round 报告。")
        _begin_post_gate("lifecycle_finalize")
        try:
            lifecycle = _complete_lifecycle(
                write_lifecycle_report(_lifecycle_report_state(runtime))
            )
        except (OSError, ValueError) as exc:
            return _fail_post(
                f"Round 报告生成或生命周期完成处理失败: {exc}",
                gate_status="error",
            )
        _finish_post_gate("ok")
        print(
            f"✓ Round {lifecycle.get('lifecycle_no', '?')} 测试报告已生成："
            f"{lifecycle.get('lifecycle_last_report')}"
        )
        print(
            f"  Round 累计有效 Bug："
            f"{lifecycle.get('lifecycle_credited_total', 0)} 条；"
            f"Life：{lifecycle.get('lifecycle_cycles_completed', 0)} 个。"
        )
        refresh_audit_todo(lifecycle, "round-completed")
        refresh_startup_todo(
            "report",
            reason="Round 报告已封存，正在按既定轮询策略决定后续动作",
            environment=detect_startup_environment(),
        )
        _begin_post_gate("next_life_start")
        transition = continue_after_death()
        if transition == "continued":
            _finish_post_gate("ok")
        elif transition == "start-failed":
            _finish_post_gate("error")
        else:
            _finish_post_gate("blocked")
        if transition == "continued":
            print("↻ 已启用自动续轮：当前 Round 完成，下一 Round 已启动。")
            refresh_startup_todo(
                "mining",
                reason="已按 auto 策略启动下一 Round，继续漏洞挖掘",
                environment=detect_startup_environment(),
            )
        elif transition == "stopped-zero-findings":
            print("✝ 自动续轮终止：自动开启的新 Round 累计未发现任何有效 Bug 即死亡。")
        elif transition == "stopped-complete":
            print("✓ 目标覆盖已全部完成，Supervisor 宣告收敛；Session 已安全收尾。")
        elif transition == "stopped-complete-with-findings":
            print("✓ 目标覆盖已全部完成（fix_mode=log-only 仅记录模式）；缺陷已留痕，Session 已安全收尾。")
        elif transition == "stopped-manual":
            print("■ 手动续轮已生效：当前 Round 报告完成，Session 已自动收尾。")
        elif transition == "stopped-max-rounds":
            print("✓ 已达到测试预设最大 Round 轮次上限，测试验证已自动收敛停机。")
        elif transition == "decision-required":
            print("？ Round 已完成，但续轮策略尚未配置；当前 Session 已安全收尾。")
            print("  python3 launch_bugx.py polling auto|manual")
            exit_code = POLLING_DECISION_REQUIRED
        elif transition == "start-failed":
            print("✗ 自动轮询无法启动下一轮，已停止；请检查启动门禁。")
            exit_code = 1
        elif transition.startswith("stopped-"):
            print(f"■ 自动轮询已停机（原因：{transition[8:]}）；Session 已安全收尾。")
        if transition != "continued":
            is_complete = transition in {
                "stopped-zero-findings",
                "stopped-manual",
                "stopped-complete",
                "stopped-complete-with-findings",
            }
            runtime = close_session(
                runtime.get("round_id") or runtime.get("lifecycle_id"),
                reason="completed" if is_complete else "blocked",
            )
            share_state = maybe_share_lifecycle(runtime, lifecycle, share)
            if is_complete:
                share_phase = "share-pending" if share_state == "pending" else "share-completed"
            else:
                share_phase = "blocked"
            refresh_audit_todo(runtime, share_phase)
            if share_state == "pending":
                refresh_startup_todo(
                    "report",
                    reason="Round 已收尾，但结论共享待重放",
                    environment=detect_startup_environment(),
                )
            else:
                refresh_startup_todo(
                    "end",
                    reason="Round 结论已处理，Session 已自动收尾",
                    environment=detect_startup_environment(),
                )
            # DOC-154: 原子生成标准化验收凭证收据 (第 6 完成门)
            _begin_post_gate("acceptance_receipt")
            receipt_outcome = (
                "COMPLETE_WITH_FINDINGS" if transition == "stopped-complete-with-findings"
                else ("COMPLETE" if is_complete else "BLOCKED")
            )
            receipt_path = _write_acceptance_receipt(
                runtime,
                outcome=receipt_outcome,
            )
            if receipt_outcome in ("COMPLETE", "COMPLETE_WITH_FINDINGS"):
                if not receipt_path or not receipt_path.is_file():
                    return _fail_post("验收收据生成失败，拒绝以 COMPLETE 声明收官", gate="acceptance_receipt")
                verification = verify_acceptance_receipt(receipt_path)
                if not verification.get("verified"):
                    return _fail_post(
                        f"验收收据反向自检失败: {verification.get('reason')}",
                        gate="acceptance_receipt",
                    )
            _finish_post_gate("ok")
    if exit_code:
        set_last_post_failure(
            "续轮策略需要用户决策" if exit_code == POLLING_DECISION_REQUIRED
            else "自动轮询无法启动下一轮",
            exit_code=exit_code,
        )
    return exit_code


_continue_after_death = continue_after_death
