#!/usr/bin/env python3
"""Finding 决策报告的读取、校验与合并边界。

该模块只处理 Finding 数据，不持有 Supervisor 锁、不写 run-state，也不追加
审计事件；状态事务仍由 ``audit_supervisor.Supervisor`` 负责。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

from .supervisor_models import (
    InvalidCheckpoint,
    InvalidTransition,
    Status,
    confined_regular_file,
)


# V2（findings 内容哈希登记）：sync 与 post 结算是两个独立进程，期间
# findings.json 可被任意改写而 run_binding 不变。sync 成功收尾时把报告
# 原始字节的内容哈希登记进 supervisor state（hash-chain 审计事件）；
# post 结算路径（finding_settlement._verify_findings_report_seal）比对
# 登记哈希与文件实际哈希，不一致 fail-closed。决策合并事件
# ``finding_decisions_synced`` 自带 report_sha256，同样构成登记；
# ``findings_report_sealed`` 用于「内容变了但决策等价」的重密封场景。
# settlement 侧只读识别这两个事件类型，词汇表以本模块为唯一真源。
FINDINGS_SEAL_EVENT_TYPE = "findings_report_sealed"
_FINDINGS_SEAL_EVENT_TYPES = frozenset({
    "finding_decisions_synced", FINDINGS_SEAL_EVENT_TYPE,
})


def _rollover_findings_backup(path, *, revision: int | None = None) -> None:
    """DOC-204 L3：findings.json 写前/读前滚存（旁证，防错不防恶）。

    目标名 findings.json.bak-<revision|seq>；已存在（含 symlink）即跳过
    （O_EXCL 语义）；0600 强制；保留最近 5 份。审计旁证不参与任何校验。
    """
    import os
    from pathlib import Path as _P
    src = _P(path)
    if not src.is_file() or src.is_symlink():
        return
    seq_tag = str(revision) if revision is not None else "pre"
    dest = src.with_name(f"{src.name}.bak-{seq_tag}")
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(src.read_bytes())
    except FileExistsError:
        return
    except OSError:
        return
    baks = sorted(src.parent.glob(src.name + ".bak-*"))
    for old in baks[:-5]:
        try:
            old.unlink()
        except OSError:
            pass


def read_decision_report(
    workspace_root: Path,
    error_cls: type[Exception],
) -> tuple[dict, bytes, str, Path]:
    """Read one bounded, active-lifecycle Finding report."""
    try:
        import core.bugx_paths as bugx_paths
        from core.ledger import finding_io

        scope = bugx_paths.require(workspace_root)
        report_path = confined_regular_file(
            workspace_root, str(scope.lifecycle_root / "findings.json"),
        )
        if report_path.is_symlink():
            raise error_cls("活动 lifecycle findings.json 不能是符号链接")
        report_size = report_path.stat().st_size
        if report_size > 8 * 1024 * 1024:
            logger.warning(
                "活动 lifecycle findings.json 已超过 8 MiB，请优化 Finding context（%d bytes）",
                report_size,
            )
        if report_size > 16 * 1024 * 1024:
            raise error_cls("活动 lifecycle findings.json 超过 16 MiB")
        _rollover_findings_backup(report_path)
        report_bytes = report_path.read_bytes()
        payload = json.loads(report_bytes.decode("utf-8"))
        finding_io.validate_payload(payload, workspace_root)
        return payload, report_bytes, scope.lifecycle_root.name, report_path
    except error_cls:
        raise
    except (ImportError, OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise error_cls(f"Finding 决策报告校验失败: {exc}") from exc


def validate_report_binding(
    state: object,
    payload: Mapping[str, object],
    lifecycle_id: str,
    error_cls: type[Exception],
) -> None:
    """Require the reviewed report to bind to the active Supervisor lifecycle."""
    from core.ledger import finding_io

    binding = finding_io.validate_run_binding(dict(payload))
    if binding["run_id"] != state.run_id:
        raise error_cls("run_binding.run_id 与当前 Supervisor run 不一致")
    if binding["lifecycle_id"] != lifecycle_id:
        raise error_cls("run_binding.lifecycle_id 与活动 lifecycle 不一致")
    if state.round_id != lifecycle_id and state.life_id != lifecycle_id:
        raise error_cls("Supervisor round/life 未绑定到活动 lifecycle")
    if binding["round"] != state.round_no:
        raise error_cls("run_binding.round 与当前 Supervisor round 不一致")


def validate_decision(
    key: str,
    current: dict[str, object],
    reported: dict[str, object],
    workspace_root: Path,
    error_cls: type[Exception],
) -> str:
    """Validate identity, monotonic confirmation and remediation evidence."""
    from core.ledger import finding_confirmation

    for field_name in (
        "fingerprint", "finding_id", "id", "title", "root_cause",
        "severity", "location", "description",
    ):
        before = current.get(field_name)
        after = reported.get(field_name)
        if before is not None and after is not None and before != after:
            raise error_cls(f"Finding {key} 的 {field_name} 与现有候选不一致")
    confirmation = reported.get("confirmation")
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    new_state = str(confirmation.get("state") or "candidate")
    old_confirmation = current.get("confirmation")
    old_state = (
        str(old_confirmation.get("state") or "candidate")
        if isinstance(old_confirmation, dict) else "candidate"
    )
    states = finding_confirmation.CONFIRMATION_STATES
    if new_state not in states:
        raise error_cls(f"Finding {key} confirmation.state 无效")
    if states.index(new_state) < states.index(old_state):
        raise error_cls(
            f"Finding {key} confirmation 不得从 {old_state} 回退到 {new_state}"
        )
    if old_state in {"confirmed", "rejected"} and new_state != old_state:
        raise error_cls(f"Finding {key} 已有终态 {old_state}，报告决策冲突")
    if new_state == "confirmed":
        if not finding_confirmation.is_confirmed(reported):
            raise error_cls(f"Finding {key} 未通过 confirmed 门禁")
        remediation = finding_confirmation.validate_remediation(
            reported, workspace_root,
        )
        if remediation.get("status") != "ok":
            raise error_cls(
                f"Finding {key} remediation 无效: "
                + "; ".join(remediation.get("errors", []))
            )
    return new_state


def _run_reference_ok(
    reported: Mapping[str, object],
    run_ids: set[str],
) -> bool:
    """PE-20261005-006：报告是否绑定本次运行。

    通过条件（任一）：target_execution.run_reference 引用运行身份
    （state.run_id 或 runtime 会话 UUID，兼容 origin_receipt.execution_id
    归属本 run 的旧形态）；或任一 observation.run_id 引用运行身份。
    定位为防错+取证（stale/跨轮拼接事故），不防恶——防恶边界是工件
    哈希复核（verify_observation_artifacts）。
    """
    confirmation = reported.get("confirmation")
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    target = confirmation.get("target_execution")
    if isinstance(target, dict):
        reference = str(target.get("run_reference") or "")
        if reference in run_ids:
            return True
    for observation in confirmation.get("observations") or []:
        if isinstance(observation, dict) and \
                observation.get("run_id") in run_ids:
            return True
    return False


def _assert_run_ownership(
    state: Any,
    reported_by_key: Mapping[str, dict[str, object]],
    lifecycle_id: str,
    error_cls: type[Exception],
) -> None:
    """candidate→confirmed 迁移的报告必须绑定本次运行（报告级一次性判定）。"""
    run_ids = {
        value for value in (
            str(state.run_id or ""),
        ) if value
    }
    runtime_run_id = _runtime_session_run_id()
    if runtime_run_id:
        run_ids.add(runtime_run_id)
    if not run_ids:
        return
    for key, finding in reported_by_key.items():
        confirmation = finding.get("confirmation")
        state_name = (
            str(confirmation.get("state") or "candidate")
            if isinstance(confirmation, dict) else "candidate"
        )
        if state_name != "confirmed":
            continue
        current = next(
            (item for item in state.findings
             if item.get("fingerprint") == finding.get("fingerprint")),
            None,
        )
        old_state = (
            str((current.get("confirmation") or {}).get("state") or "candidate")
            if isinstance(current, dict) and isinstance(
                current.get("confirmation"), dict) else "candidate"
        )
        if old_state == "confirmed":
            continue
        if not _run_reference_ok(finding, run_ids):
            raise error_cls(
                f"Finding {key} 的确认记录未绑定本次运行"
                f"（run_reference/observations 均未引用 {sorted(run_ids)}）——"
                "报告可能来自其他运行；请在 target_execution.run_reference "
                "填入本 run 身份后重试"
            )


def _runtime_session_run_id() -> str | None:
    """读 runtime 会话 UUID（读不到返回 None，不阻断）。"""
    try:
        from core.launch_phases.session_runtime import load_runtime

        runtime = load_runtime()
    except Exception:  # noqa: BLE001 — 找不到 runtime 时降级为仅 state.run_id
        return None
    value = runtime.get("run_id") if isinstance(runtime, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def merge_decisions(
    state_findings: list[dict[str, object]],
    reported_by_key: Mapping[str, dict[str, object]],
    workspace_root: Path,
    error_cls: type[Exception],
    finding_key: Callable[[Mapping[str, object]], str],
) -> list[dict[str, str]]:
    """Validate and apply reviewed decisions to Supervisor-owned findings."""
    existing_by_key = {finding_key(item): item for item in state_findings}
    if set(existing_by_key) != set(reported_by_key):
        missing = sorted(set(existing_by_key) - set(reported_by_key))
        extra = sorted(set(reported_by_key) - set(existing_by_key))
        raise error_cls(
            f"Findings 报告必须精确对应现有 Supervisor keys；missing={missing}, extra={extra}"
        )
    updated: list[dict[str, str]] = []
    for key, current in existing_by_key.items():
        reported = reported_by_key[key]
        new_state = validate_decision(
            key, current, reported, workspace_root, error_cls,
        )
        changed = False
        if current.get("severity") is None and reported.get("severity") is not None:
            current["severity"] = copy.deepcopy(reported["severity"])
            changed = True
        confirmation = reported.get("confirmation")
        if (
            isinstance(confirmation, dict) and confirmation
            and current.get("confirmation") != confirmation
        ):
            current["confirmation"] = copy.deepcopy(confirmation)
            changed = True
        if (
            "remediation" in reported
            and current.get("remediation") != reported["remediation"]
        ):
            current["remediation"] = copy.deepcopy(reported["remediation"])
            changed = True
        for field_name in ("reproducible", "reproduction"):
            value = reported.get(field_name)
            if value is not None and current.get(field_name) != value:
                current[field_name] = copy.deepcopy(value)
                changed = True
        # 工具归因回填（build.42）：checkpoint 摄入历史上接受 tools=[]（如
        # polling-000008 的 osv 三候选），而 finding_confirmation.validate_tools
        # 把空数组判为非法——收敛核对因此死锁，且决策同步契约原本不合并
        # tools、无从修复。与 confirmation 同类：报告侧合法非空归因可回填
        # 候选的缺失/空归因；两侧都有合法值时不覆盖（先到优先）。
        reported_tools = reported.get("tools")
        # None/空数组与"未填"同义（noop）；其余形态（含字符串等非法形态）
        # 一律过 validate_tools——非法归因即使候选已有合法值也结构化拒绝。
        if reported_tools is not None and reported_tools != []:
            from core.ledger import finding_confirmation

            try:
                normalized_tools = finding_confirmation.validate_tools(
                    {"id": key, "tools": reported_tools}
                )
            except Exception as exc:  # noqa: BLE001 - 归因非法走结构化拒绝
                raise error_cls(
                    f"Finding {key} 的 tools 归因非法: {exc}"
                ) from exc
            if not current.get("tools"):
                current["tools"] = list(normalized_tools)
                changed = True
        if changed:
            updated.append({"key": key, "state": new_state})
    return updated


def latest_registered_report_sha256(state: Any, round_no: int | None = None) -> str | None:
    """Return the newest registered lifecycle findings.json content hash (V2).

    Registration sources are audit events carrying a 64-hex ``report_sha256``:
    ``finding_decisions_synced`` (decision merge) and
    ``findings_report_sealed`` (first registration on a decision-free sync).
    Returns ``None`` when the state carries no registration (older rounds) —
    the settlement side treats that as "skip comparison", never as a violation.
    """
    latest: tuple[int, str] | None = None
    history = getattr(state, "history", None)
    for event in history if isinstance(history, list) else ():
        if not isinstance(event, Mapping):
            continue
        if event.get("type") not in _FINDINGS_SEAL_EVENT_TYPES:
            continue
        digest = event.get("report_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        try:
            int(digest, 16)
        except ValueError:
            continue
        if round_no is not None:
            event_round = event.get("round_no")
            if (
                not isinstance(event_round, int) or isinstance(event_round, bool)
                or event_round != round_no
            ):
                continue
        seq = event.get("seq")
        order = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
        if latest is None or order >= latest[0]:
            latest = (order, digest.lower())
    return latest[1] if latest else None


def ensure_findings_report_seal(
    supervisor: Any,
    state: Any,
    now: float,
    lifecycle_id: str,
    report_sha256: str,
    decisions_applied: int,
) -> bool:
    """Register the reviewed report content hash into supervisor state (V2).

    Only the *first* decision-free sync of a round seals: once any
    registration exists for the current round, content rewrites without
    decision changes never re-seal — the post settlement side then compares
    the file against the reviewed bytes and fails closed, and the host makes
    the change visible through a decision-bearing sync (whose
    ``finding_decisions_synced`` event registers the new bytes).  This keeps
    repeated no-op syncs perfectly idempotent (no history growth, no
    revision bump).
    """
    if latest_registered_report_sha256(state, round_no=state.round_no) is not None:
        return False
    supervisor._append_event(
        state, FINDINGS_SEAL_EVENT_TYPE, now,
        run_id=state.run_id, lifecycle_id=lifecycle_id,
        round_no=state.round_no,
        report_sha256=report_sha256,
        decisions_applied=decisions_applied,
    )
    return True


def apply_decision_sync(
    supervisor: Any,
    state: Any,
    now: float,
    expected_revision: int | None,
    canonicalize_report_identity: bool = False,
) -> Any:
    """Apply one reviewed Finding report inside Supervisor's mutation lock.

    This function owns report reconciliation and event payload construction;
    the Supervisor still owns the lock, persistence, and lifecycle state.
    """
    supervisor._check_revision(state, expected_revision)
    if state.status in {
        Status.COMPLETE, Status.FAILED, Status.INCOMPLETE,
    }:
        raise InvalidTransition(f"run 已结束: {state.status.value}")
    if state.active_claims:
        raise InvalidTransition("仍有 active worker，不能同步 Finding 决策")
    supervisor._assert_target_fingerprint(state, now)
    payload, report_bytes, lifecycle_id, report_path = read_decision_report(
        supervisor.workspace_root, InvalidCheckpoint,
    )
    validate_report_binding(
        state, payload, lifecycle_id, InvalidCheckpoint,
    )
    existing_by_key = {
        supervisor._finding_key(item): item for item in state.findings
    }
    reported_keys = {supervisor._finding_key(item) for item in payload["findings"]}
    extra = sorted(reported_keys - set(existing_by_key))
    if extra:
        raise InvalidCheckpoint(
            f"Findings 报告包含未知 Supervisor candidates: extra={extra}"
        )
    added_candidates = sorted(set(existing_by_key) - reported_keys)
    reconciled_report_text: str | None = None
    if added_candidates:
        terminal_missing = sorted(
            key for key in added_candidates
            if isinstance(existing_by_key[key].get("confirmation"), dict)
            and existing_by_key[key]["confirmation"].get("state")
            in {"confirmed", "rejected"}
        )
        if terminal_missing:
            raise InvalidCheckpoint(
                "Findings 报告遗漏已终态 Supervisor candidates: "
                f"missing={terminal_missing}"
            )
        payload["findings"].extend(
            copy.deepcopy(existing_by_key[key]) for key in added_candidates
        )
        try:
            from core.ledger import finding_io

            finding_io.validate_payload(payload, supervisor.workspace_root)
        except Exception as exc:
            raise InvalidCheckpoint(
                "无法将未列出的 Supervisor candidate 安全补入 lifecycle 报告: "
                f"{exc}"
            ) from exc
        reconciled_report_text = json.dumps(
            payload, ensure_ascii=False, indent=1,
        ) + "\n"
    canonicalized: list[str] = []
    if canonicalize_report_identity:
        for finding in payload["findings"]:
            key = supervisor._finding_key(finding)
            current = existing_by_key[key]
            changed = False
            for field_name in ("title", "root_cause", "location", "description"):
                value = current.get(field_name)
                if value is not None and finding.get(field_name) != value:
                    finding[field_name] = copy.deepcopy(value)
                    changed = True
            if changed:
                canonicalized.append(key)
        if canonicalized:
            try:
                from core.ledger import finding_io

                finding_io.validate_payload(payload, supervisor.workspace_root)
            except Exception as exc:
                raise InvalidCheckpoint(
                    "无法按 Supervisor 候选身份字段修复 lifecycle 报告: "
                    f"{exc}"
                ) from exc
            reconciled_report_text = json.dumps(
                payload, ensure_ascii=False, indent=1,
            ) + "\n"
    reported_by_key: dict[str, dict[str, object]] = {}
    for finding in payload["findings"]:
        key = supervisor._finding_key(finding)
        if key in reported_by_key:
            raise InvalidCheckpoint(f"Findings 报告含重复 finding key: {key}")
        reported_by_key[key] = finding
    # PE-20261005-006：candidate→confirmed 的报告必须绑定本次运行
    #（防错+取证：堵陈旧文件/跨轮拼接；防恶边界仍是工件哈希复核）。
    _assert_run_ownership(state, reported_by_key, lifecycle_id, InvalidCheckpoint)
    updated = merge_decisions(
        state.findings,
        reported_by_key,
        supervisor.workspace_root,
        InvalidCheckpoint,
        supervisor._finding_key,
    )
    supervisor._refresh_confirmation_gate(state)
    if not updated and not added_candidates and not canonicalized:
        # V2：无决策变更的首个 sync 也要登记内容哈希，否则 post 结算无从
        # 复核「sync 之后文件是否被改写」。已有登记则保持幂等（无事件、
        # 不推 revision）；等价改写不重密封——post 会对未登记的新字节
        # fail-closed，宿主通过带决策变更的 sync 让新内容重新可见。
        ensure_findings_report_seal(
            supervisor, state, now, lifecycle_id,
            hashlib.sha256(report_bytes).hexdigest(), 0,
        )
        return state
    if reconciled_report_text is not None:
        if report_path.read_bytes() != report_bytes:
            raise InvalidCheckpoint(
                "lifecycle findings.json 已变化，请重新读取后重试"
            )
        # Do not bypass the Supervisor lock with a direct report write.  The
        # enclosing _persist_locked() publishes this byte sequence together
        # with state, backup and seal through finding-commit.
        report_bytes = reconciled_report_text.encode("utf-8")
    state.findings_revision = int(getattr(state, "findings_revision", 0)) + 1
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    state.report_consumption = {
        "phase": "OPEN",
        "run_id": state.run_id,
        "findings_revision": state.findings_revision,
    }
    state.report_seal = {
        "schema_version": 1,
        "run_id": state.run_id,
        "lifecycle_id": lifecycle_id,
        "round": state.round_no,
        "findings_revision": state.findings_revision,
        "report_sha256": report_sha256,
        "sealed_at": now,
    }
    state._finding_commit_pending = {
        "report_path": report_path,
        "seal_path": report_path.with_name("report-seal.json"),
        "report_bytes": report_bytes,
        "seal_bytes": (json.dumps(
            state.report_seal, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n").encode("utf-8"),
        "request_id": f"finding-sync:{state.run_id}:{state.findings_revision}",
    }
    supervisor._append_event(
        state, "finding_decisions_synced", now,
        run_id=state.run_id, lifecycle_id=lifecycle_id,
        round_no=state.round_no,
        report_sha256=report_sha256,
        findings=len(reported_by_key), decisions=updated,
        report_candidates_added=added_candidates,
        report_identity_canonicalized=canonicalized,
    )
    return state


# DOC-230 §3.8 第 4 项（A5-A）：severity 修复通道的审计事件类型。
# 单独成常量以便守卫测试与排障检索（事件词汇表单一真源）。
FINDING_SEVERITY_REPAIRED_EVENT = "finding_severity_repaired"


def repair_finding_severity(
    supervisor: Any,
    *,
    finding: str,
    severity: str,
    reason: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, object]:
    """修复单条 finding 的 ``severity``（历史脏数据出口，host / CLI 双入口）。

    设计约束（方案 A5-A，经第二轮/第三轮对抗审计确认）：

    * **只改一个字段**：`severity` 是写侧词表（``FINDING_SEVERITY_LEVELS``）
      的输入，``confirmation`` 是裁决结论。二者必须解耦——否则"修复脏数据"
      就成了"顺手改裁决"的提权面。本函数**绝不触碰** `confirmation`。
    * **白名单 fail-closed**：值先经 ``normalize_finding_severity`` 归一
      （`p0`→`critical` 等别名），再必须命中唯一词表，否则拒绝。
    * **合法值之间的改级必须给 reason**（C1 / A12）：`severity` **参与**
      `next_round_sync` 的 Life 奖励判定（`confirmed or severity in
      (critical, high)` → 生命成本 −0.5 而非 −1.0）以及 `agent_eval` 的
      minimum_severity 评分。因此"把 info 抬成 critical"＝**刷分**，"把
      critical 压成 info"＝**藏缺陷**——两者都是可自利的操纵。静默改级会把
      本通道变成操纵工具，故 valid→valid 的变更**强制 reason**，并写入审计
      事件；而"修复非法值"（脏数据出口的本职）无需 reason。
    * **可追溯**：真实变更写一条 ``finding_severity_repaired`` 事件，记录旧值、
      新值、方向、原因、当时确认状态与调用入口。
    * **幂等**：目标值与现值相同（含别名等价）时不写事件、不推进 revision，
      返回 ``changed=False``——重复执行修复不应污染审计链。
    * **绑定可寻址**：``finding`` 接受 finding_key（fingerprint / id / finding_id）
      三者任一，与 ``supervisor._finding_key`` 口径一致。
    """
    from .supervisor_models import finding_severity_levels, normalize_finding_severity

    target = str(finding or "").strip()
    if not target:
        raise InvalidCheckpoint("fix-severity 必须提供 --finding（finding_key/fingerprint）")
    raw = str(severity or "").strip()
    if not raw:
        raise InvalidCheckpoint("fix-severity 必须提供 --severity")
    normalized = normalize_finding_severity(raw)
    levels = tuple(finding_severity_levels())
    if normalized not in levels:
        raise InvalidCheckpoint(
            f"severity 必须属于词表 {list(levels)}，收到: {raw!r}"
        )
    normalized_reason = str(reason or "").strip()

    result: dict[str, object] = {}

    def operation(state: Any, now: float) -> Any:
        supervisor._check_revision(state, expected_revision)
        matched: dict[str, object] | None = None
        for item in state.findings:
            if not isinstance(item, dict):
                continue
            keys = {
                str(item.get("fingerprint") or "").strip(),
                str(item.get("id") or "").strip(),
                str(item.get("finding_id") or "").strip(),
                str(supervisor._finding_key(item) or "").strip(),
            }
            if target in keys:
                matched = item
                break
        if matched is None:
            raise InvalidCheckpoint(f"未找到 finding: {target}")
        previous = normalize_finding_severity(matched.get("severity"))
        key = str(supervisor._finding_key(matched) or target)
        if previous == normalized:
            result.update({
                "finding": key,
                "previous_severity": previous,
                "severity": normalized,
                "changed": False,
            })
            return state
        if previous in levels:
            direction = (
                "upgrade"
                if levels.index(str(normalized)) > levels.index(str(previous))
                else "downgrade"
            )
            if not normalized_reason:
                raise InvalidCheckpoint(
                    f"severity 从 {previous} 改为 {normalized}（{direction}）必须提供 reason："
                    "severity 参与 Life 奖励与 minimum_severity 评分，静默改级会成为"
                    "刷分/藏缺陷的操纵工具（C1/A12）"
                )
        else:
            # 脏数据出口的本职：修复非法值不需要理由（旧值本就不在词表内）。
            direction = "repair_invalid"
        confirmation = matched.get("confirmation")
        confirmation_state = (
            str(confirmation.get("state") or "candidate")
            if isinstance(confirmation, dict) else "candidate"
        )
        matched["severity"] = normalized
        # 变更必须落盘；_append_event 亦会置 dirty，此处显式声明本变更的持久化意图。
        state.dirty = True
        supervisor._append_event(
            state, FINDING_SEVERITY_REPAIRED_EVENT, now,
            finding=key,
            previous_severity=previous,
            severity=normalized,
            direction=direction,
            reason=normalized_reason or None,
            confirmation_state=confirmation_state,
            # 显式记录"未改裁决"，使审计链自身可证明边界未被越过。
            confirmation_unchanged=True,
        )
        result.update({
            "finding": key,
            "previous_severity": previous,
            "severity": normalized,
            "direction": direction,
            "reason": normalized_reason or None,
            "changed": True,
            "confirmation_state": confirmation_state,
        })
        return state

    updated = supervisor._mutate(operation)
    result["revision"] = getattr(updated, "revision", None)
    return result

