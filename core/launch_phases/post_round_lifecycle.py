#!/usr/bin/env python3
"""launch_phases/post_round_lifecycle.py - Round 推进、生命周期决策、前驱证据继承与串行授权核验 (DOC-127 / DOC-137 / DOC-154)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from core.launch_phases.common import (
    LIFECYCLE_MAX_SMALL_CYCLES,
    LIFECYCLE_MAX_NO_PROGRESS_CYCLES,
    LIFECYCLE_MAX_SECONDS,
    resolve_project_root,
    uses_project_layout,
    bugx_paths,
    run_verify_life,
    host_serial_worker,
)
from core.launch_phases.session_runtime import (
    load_runtime,
    save_runtime,
    runtime_lock,
    append_runtime_event,
    start_runtime,
    close_runtime,
    iso_time,
    baseline_json,
    set_polling_stop,
    refresh_audit_todo,
)
from core.launch_phases.lesson_context import (
    lesson_context,
    set_lesson_context,
    lesson_review_matches,
)
from core.launch_phases.supervisor_bridge import (
    get_supervisor,
    ensure_supervisor,
    advance_supervisor_round,
)
from core.launch_phases.finding_settlement import settle_findings_path, auto_mode
from core.launch_phases import post_observability

try:
    from core.ledger import finding_confirmation
except ImportError:
    try:
        from core import finding_confirmation
    except ImportError:
        finding_confirmation = None  # type: ignore[assignment]


_LAST_POST_FAILURE = ""
_LAST_POST_FAILURE_CONTEXT: ContextVar[str | None] = ContextVar(
    "bugx_last_post_failure", default=None,
)

_POST_GATE_ALIASES = {
    "open_findings": "finding_open",
}


def _pp():
    return sys.modules.get("core.launch_phases.post_phase") or sys.modules.get("launch_phases.post_phase")


def _get_pp_attr(name: str, fallback=None):
    pp = _pp()
    if pp and hasattr(pp, name):
        return getattr(pp, name)
    return fallback


def get_last_post_failure() -> str:
    """返回最近一次 post 失败原因。"""
    ctx_var = _get_pp_attr("_LAST_POST_FAILURE_CONTEXT", _LAST_POST_FAILURE_CONTEXT)
    value = ctx_var.get()
    last_val = _get_pp_attr("_LAST_POST_FAILURE", _LAST_POST_FAILURE)
    return last_val if value is None else value


def set_last_post_failure(
    reason: str, *, exit_code: int = 1, gate_status: str = "blocked",
    reason_code: str | None = None, detail: str = "",
) -> None:
    """Keep the legacy user-facing failure string and emit one attempt event."""
    global _LAST_POST_FAILURE
    val = str(reason).strip()
    _LAST_POST_FAILURE = val
    pp = _pp()
    if pp and hasattr(pp, "_LAST_POST_FAILURE"):
        setattr(pp, "_LAST_POST_FAILURE", val)
    ctx_var = _get_pp_attr("_LAST_POST_FAILURE_CONTEXT", _LAST_POST_FAILURE_CONTEXT)
    ctx_var.set(val)
    if not val:
        return
    # 2026-11-09 E6b（opencode 复盘）：固定 "post_failed" 让
    # _FAILURE_REASON_CODES 白名单形同虚设——7 条 post_failed 的 reason_code
    # 全是兜底值，真实门禁原因只能去 gate_finished 侧反查。fail_post 已算好
    # gate，用它拼专属 code；detail 一并透传（E6b-2 在 emit 层接收）。
    post_observability.record_failure(
        reason_code or f"{gate_status and gate_status or 'post'}_failed",
        # 注意：record_failure 的第二个位置形参即 detail——这里必须全关键字
        # 传参，位置传 val 会与 detail= 关键字冲突（实测 TypeError）。
        detail=(str(detail) or val)[:400],
        exit_code=exit_code, gate_status=gate_status,
    )


def fail_post(
    reason: str, *extra_lines: str, gate: str | None = None,
    gate_status: str = "blocked",
) -> int:
    """统一记录失败原因、向诊断日志输出、打印终端提示并返回 1。"""
    if (reason in post_observability.GATE_REGISTRY
            or reason in _POST_GATE_ALIASES) and extra_lines:
        gate = gate or _POST_GATE_ALIASES.get(reason, reason)
        reason, *extra_lines = extra_lines
    gate = _POST_GATE_ALIASES.get(gate, gate) if gate is not None else None
    if gate is None:
        attempt = post_observability.current_attempt()
        active_gate = getattr(attempt, "active_gate_id", None)
        if isinstance(active_gate, str) and active_gate.startswith("post.v1."):
            gate = active_gate.removeprefix("post.v1.")
        else:
            gate = "post_exception"
    gate_reason_code = f"{gate}_blocked" if gate else None
    set_last_post_failure(
        reason, exit_code=1, gate_status=gate_status,
        reason_code=gate_reason_code,
        detail="\n".join(extra_lines)[:400] if extra_lines else "",
    )
    try:
        sup_fn = _get_pp_attr("get_supervisor", get_supervisor)
        sup_fn().record_lifecycle_block(
            operation="post", gate=gate, reason=reason,
        )
    except Exception as exc:
        print(
            f"[post] ⚠ lifecycle_blocked 登记失败（{type(exc).__name__}）",
            file=sys.stderr,
        )
    print(f"✗ {reason}")
    for line in extra_lines:
        print(line)
    return 1


def begin_post_gate(name: str) -> None:
    post_observability.begin_gate(name)


def finish_post_gate(status: str = "ok", detail: object = "") -> None:
    post_observability.finish_active_gate(status, detail)


def lifecycle_stop_reason(cycles_completed, no_progress_cycles,
                          lifecycle_started_epoch, now):
    """Round 停止判定（纯函数，供表驱动测试）。"""
    max_small = _get_pp_attr("LIFECYCLE_MAX_SMALL_CYCLES", LIFECYCLE_MAX_SMALL_CYCLES)
    max_no_prog = _get_pp_attr("LIFECYCLE_MAX_NO_PROGRESS_CYCLES", LIFECYCLE_MAX_NO_PROGRESS_CYCLES)
    max_sec = _get_pp_attr("LIFECYCLE_MAX_SECONDS", LIFECYCLE_MAX_SECONDS)

    if cycles_completed >= max_small:
        return "max_small_lifecycle_cycles"
    if no_progress_cycles >= max_no_prog:
        return "no_valid_progress"
    if lifecycle_started_epoch is not None and (
            now - float(lifecycle_started_epoch) >= max_sec):
        return "lifecycle_wall_time_budget"
    return None


def continue_small_lifecycle() -> tuple[dict, bool]:
    """结算一个 Life 后，自动开启同一 Round 的下一段运行。"""
    runtime, started = start_runtime(auto_started=True, new_lifecycle=False)
    if not started:
        return runtime, False
    try:
        advance_supervisor_round()
    except (OSError, ValueError, AttributeError, RuntimeError):
        close_runtime("aborted", reason="next small lifecycle Supervisor setup failed")
        return load_runtime(), False
    previous_context = load_runtime().get("lesson_context") or {}
    try:
        context = lesson_context(
            str(previous_context.get("query") or ""),
            previous_context.get("category"),
        )
        set_lesson_context(context)
    except (OSError, ValueError, TypeError, KeyError):
        close_runtime("aborted", reason="next small lifecycle lesson context failed")
        return load_runtime(), False
    return load_runtime(), True


def round_result() -> dict:
    """读取刚结算轮次的结果；只读 life 文件，不信任调用方手算值。"""
    base_json_fn = _get_pp_attr("baseline_json", baseline_json)
    state = base_json_fn() or {}
    history = state.get("history") or []
    if not history or not isinstance(history[-1], dict):
        return {"round": state.get("round"), "findings": 0, "credited": 0,
                "fraud": 0, "life_after": state.get("life")}
    last = history[-1]
    findings = last.get("findings") or []
    credited = last.get("credited", len(findings))
    delta = last.get("delta")
    fraud = (-1 + credited - delta) if isinstance(delta, int) else 0
    return {
        "round": last.get("round"),
        "findings": len(findings) if isinstance(findings, list) else 0,
        "credited": credited if isinstance(credited, int) else 0,
        "fraud": fraud if isinstance(fraud, int) and fraud >= 0 else 0,
        "life_after": last.get("life_after", state.get("life")),
    }


@contextlib.contextmanager
def runtime_transaction(sync_active: dict | None = None):
    """原子事务性修改 runtime.json 并自动同步内存态 (L5-OPT-1)。"""
    lock_fn = _get_pp_attr("runtime_lock", runtime_lock)
    load_fn = _get_pp_attr("load_runtime", load_runtime)
    save_fn = _get_pp_attr("save_runtime", save_runtime)
    with lock_fn():
        state = load_fn()
        yield state
        save_fn(state)
        if sync_active is not None and isinstance(sync_active, dict):
            sync_active.update(state)


def record_polling_round(result: dict) -> dict:
    """记录一个 Life，并把发现累计到当前 Round。"""
    stop_fn = _get_pp_attr("_lifecycle_stop_reason", lifecycle_stop_reason)
    load_fn = _get_pp_attr("load_runtime", load_runtime)
    save_fn = _get_pp_attr("save_runtime", save_runtime)
    with runtime_lock():
        state = load_fn()
        previous = state.get("polling_last_round") or {}
        if (previous.get("round") == result.get("round")
                and previous.get("life_after") == result.get("life_after")):
            return state
        if state.get("lifecycle_id") is None:
            now = time.time()
            state.update({
                "lifecycle_no": int(state.get("lifecycle_no", 0)) + 1,
                "lifecycle_id": uuid.uuid4().hex,
                "lifecycle_auto_started": bool(
                    state.get("polling_round_auto_started")
                ),
                "lifecycle_started_at": state.get("started_at") or iso_time(now),
                "lifecycle_started_epoch": state.get("started_epoch") or now,
                "lifecycle_cycles_completed": 0,
                "lifecycle_findings_total": 0,
                "lifecycle_credited_total": 0,
                "lifecycle_fraud_total": 0,
                "lifecycle_last_life": None,
                "lifecycle_terminal": False,
                "lifecycle_last_report": None,
                "lifecycle_no_progress_cycles": 0,
                "lifecycle_stop_reason": None,
            })
        state["lifecycle_cycles_completed"] += 1
        state["lifecycle_findings_total"] += int(result.get("findings", 0))
        state["lifecycle_credited_total"] += int(result.get("credited", 0))
        state["lifecycle_fraud_total"] += int(result.get("fraud", 0))
        state["lifecycle_last_life"] = result.get("life_after")
        credited = int(result.get("credited", 0))
        state["lifecycle_no_progress_cycles"] = (
            0 if credited > 0
            else int(state.get("lifecycle_no_progress_cycles", 0)) + 1
        )
        state["lifecycle_stop_reason"] = (
            None if state.get("autonomous_mode") else stop_fn(
                state["lifecycle_cycles_completed"],
                state["lifecycle_no_progress_cycles"],
                state.get("lifecycle_started_epoch"),
                time.time(),
            )
        )
        state["polling_last_round"] = {
            **result,
            "auto_started": bool(state.get("lifecycle_auto_started")),
        }
        append_runtime_event(
            state, "small-cycle-settled", time.time(),
            f"cycle={state['lifecycle_cycles_completed']} "
            f"credited={result.get('credited', 0)}",
        )
        save_fn(state)
        try:
            from core.runtime import global_telemetry
            root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
            p_root = root_fn()
            p_name = p_root.name if p_root else "default"
            now_ts = time.time()
            prev_ts = float(state.get("lifecycle_started_epoch") or now_ts)
            duration_sec = float(result.get("duration_seconds") or max(0.0, now_ts - prev_ts))
            tokens_round = int(result.get("tokens_consumed") or result.get("token_count") or 0)
            if tokens_round == 0:
                history = state.get("intelligence_history") or []
                if history and isinstance(history[-1], dict) and isinstance(history[-1].get("efficiency"), dict):
                    tokens_round = int(history[-1]["efficiency"].get("token_count") or 0)
            session_id = str(state.get("session_id") or state.get("lifecycle_id") or "session")
            cycle_no = int(state.get("lifecycle_cycles_completed", 1) or 1)
            global_telemetry.record_round_telemetry({
                "session_id": session_id,
                "round_id": f"{session_id}_c{cycle_no}",
                "project_name": p_name,
                "project_hash": hashlib.sha256(p_name.encode("utf-8")).hexdigest()[:16],
                "round": int(result.get("round") or cycle_no),
                "duration_seconds": duration_sec,
                "tokens_consumed": tokens_round,
                "credited": int(result.get("credited", 0) or 0),
                "findings": int(result.get("findings", 0) or 0),
            })
        except Exception:
            pass
    return state


def inherit_prep_record(
    previous_lifecycle_id: str | None, new_lifecycle_id: str | None,
) -> None:
    """auto 续轮：目标指纹未变时继承上一 Round 的开工准备记录。"""
    if (
        not previous_lifecycle_id
        or not new_lifecycle_id
        or previous_lifecycle_id == new_lifecycle_id
        or bugx_paths is None
    ):
        return
    try:
        root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
        scope = bugx_paths.require(root_fn())
        base = scope.polling_root / "lifecycles"
        source = base / previous_lifecycle_id / "state" / "prep-record.md"
        target_dir = base / new_lifecycle_id / "state"
        target = target_dir / "prep-record.md"
        if not source.is_file() or target.exists():
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        text_content = source.read_text(encoding="utf-8")
        text_content += (
            f"\n\n> 继承自 {previous_lifecycle_id}（{stamp}，auto 续轮，"
            "目标指纹未变）\n"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(text_content, encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as exc:
        warn_inherit(
            f"prep-record 继承失败: {previous_lifecycle_id} -> "
            f"{new_lifecycle_id}: {exc}"
        )


def warn_inherit(message: str) -> None:
    print(f"[post] ⚠ {message}", file=sys.stderr)


def inherit_coverage_evidence(
    previous_lifecycle_id: str | None, new_lifecycle_id: str | None,
) -> None:
    """同 polling 续轮时继承覆盖证据快照并维护 Session 级总账本 (DOC-137 / RCA-01)。"""
    if (
        not previous_lifecycle_id
        or not new_lifecycle_id
        or previous_lifecycle_id == new_lifecycle_id
        or bugx_paths is None
    ):
        return
    source = None
    try:
        root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
        scope = bugx_paths.require(root_fn())
        base = scope.polling_root / "lifecycles"
        source = base / previous_lifecycle_id / "state" / "coverage-evidence.json"
        target_dir = base / new_lifecycle_id / "state"
        target = target_dir / "coverage-evidence.json"
        raw_data = ""
        if source.is_symlink():
            warn_inherit(f"拒绝经符号链接继承覆盖证据: {source}")
        elif source.is_file():
            raw_data = source.read_text(encoding="utf-8")
            try:
                json.loads(raw_data)
            except ValueError as exc:
                warn_inherit(f"覆盖证据继承源损坏，放弃复制: {source}: {exc}")
                raw_data = ""
        else:
            warn_inherit(
                f"前驱生命周期没有可继承的覆盖证据: {source}"
                "（跨 polling 不复制证据文件，结算门将以 inherited_from 引用"
                "校验源证据）"
            )
        if raw_data:
            rebuild = True
            if target.is_symlink():
                warn_inherit(f"拒绝写入符号链接覆盖证据目标: {target}")
                rebuild = False
            elif target.is_file():
                try:
                    json.loads(target.read_text(encoding="utf-8"))
                    rebuild = False
                except (OSError, ValueError) as exc:
                    warn_inherit(f"覆盖证据目标损坏，重建: {target}: {exc}")
            if rebuild:
                target_dir.mkdir(parents=True, exist_ok=True)
                target.write_text(raw_data, encoding="utf-8")

        ledger_path = scope.polling_root / "coverage-ledger.json"
        ledger_data: dict[str, Any] = {}
        if ledger_path.is_file():
            try:
                ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                warn_inherit(f"覆盖总账本损坏，重建: {ledger_path}: {exc}")
                ledger_data = {}
        if not isinstance(ledger_data, dict):
            warn_inherit(f"覆盖总账本形状无效，重建: {ledger_path}")
            ledger_data = {}
        records: list[dict[str, Any]] = ledger_data.get("records") or []
        existing_units = {r.get("unit_id") for r in records if isinstance(r, dict)}
        if raw_data:
            try:
                source_data = json.loads(raw_data)
                for r in source_data.get("records") or []:
                    if isinstance(r, dict) and r.get("unit_id") and r.get("unit_id") not in existing_units:
                        records.append(r)
                        existing_units.add(r.get("unit_id"))
            except (ValueError, KeyError, AttributeError) as exc:
                warn_inherit(f"覆盖总账本合并继承记录失败: {source}: {exc}")
        ledger_data["schema_version"] = 1
        ledger_data["polling_id"] = str(scope.polling_root.name)
        ledger_data["records"] = records
        ledger_data["updated_at"] = time.time()
        ledger_path.write_text(json.dumps(ledger_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        warn_inherit(
            f"覆盖证据继承失败: {previous_lifecycle_id} -> {new_lifecycle_id}"
            f" (source={source}): {exc}"
        )


def prepare_next_round() -> tuple[dict, bool]:
    """在已获自动续轮授权后重置生命状态并启动下一轮计时。"""
    allow_serial = False
    serial_reason = None
    previous_budget = None
    sup_fn = _get_pp_attr("get_supervisor", get_supervisor)
    try:
        previous_supervisor = sup_fn()
        previous_state = previous_supervisor.status()
        allow_serial = not bool(previous_state.budget.require_parallel)
        serial_reason = previous_state.budget.serial_reason
        previous_budget = previous_state.budget.to_dict()
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    previous_runtime = load_runtime()
    previous_lifecycle_id = previous_runtime.get("lifecycle_id")
    base_json_fn = _get_pp_attr("baseline_json", baseline_json)
    previous = base_json_fn() or {}
    previous_mode = previous.get("mode")

    root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)

    def restore_previous_binding() -> None:
        if bugx_paths is None or not previous_lifecycle_id:
            return
        polling_id = str(
            previous_runtime.get("polling_id")
            or previous_runtime.get("session_id")
            or ""
        )
        if not polling_id:
            return
        try:
            bugx_paths.restore_current(
                root_fn(), polling_id, str(previous_lifecycle_id),
            )
        except (OSError, ValueError, RuntimeError, ImportError):
            pass

    start_rt_fn = _get_pp_attr("start_runtime", start_runtime)
    runtime, started = start_rt_fn(
        auto_started=True, new_lifecycle=True, new_session=False
    )
    if not started:
        restore_previous_binding()
        return runtime, False

    run_life_fn = _get_pp_attr("run_verify_life", run_verify_life)
    if run_life_fn("reset", "--confirm-reset") != 0:
        close_runtime("aborted", reason="next round life reset failed")
        restore_previous_binding()
        return load_runtime(), False
    if previous_mode in {"auto", "log-only"}:
        if run_life_fn("set-mode", "--mode", previous_mode) != 0:
            close_runtime("aborted", reason="next round life set-mode failed")
            restore_previous_binding()
            return load_runtime(), False
    if run_life_fn("check") != 0 or run_life_fn("snapshot") != 0:
        close_runtime("aborted", reason="next round life check failed")
        restore_previous_binding()
        return load_runtime(), False
    try:
        prev_ctx = (previous_runtime or {}).get("lesson_context") or {}
        lesson_ctx_fn = _get_pp_attr("lesson_context", lesson_context)
        set_lesson_ctx_fn = _get_pp_attr("set_lesson_context", set_lesson_context)
        context = lesson_ctx_fn(
            str(prev_ctx.get("query") or ""),
            prev_ctx.get("category"),
        )
        set_lesson_ctx_fn(context)
    except (OSError, ValueError, TypeError, KeyError):
        close_runtime("aborted", reason="new lifecycle lesson context failed")
        restore_previous_binding()
        return load_runtime(), False
    try:
        ensure_sup_fn = _get_pp_attr("ensure_supervisor", ensure_supervisor)
        ensure_sup_fn(
            allow_serial=allow_serial,
            serial_reason=serial_reason,
            lifecycle_id=runtime.get("lifecycle_id"), new_run=True,
            session_id=runtime.get("session_id"),
            life_id=(f"life-{runtime.get('life_no')}"
                     if runtime.get("life_no") is not None else None),
            budget_override=previous_budget,
        )
    except (OSError, ValueError, AttributeError, RuntimeError):
        close_runtime("aborted", reason="new lifecycle Supervisor setup failed")
        restore_previous_binding()
        return load_runtime(), False
    inherit_prep_fn = _get_pp_attr("_inherit_prep_record", inherit_prep_record)
    inherit_cov_fn = _get_pp_attr("_inherit_coverage_evidence", inherit_coverage_evidence)
    refresh_todo_fn = _get_pp_attr("refresh_audit_todo", refresh_audit_todo)
    inherit_prep_fn(previous_lifecycle_id, runtime.get("lifecycle_id"))
    inherit_cov_fn(previous_lifecycle_id, runtime.get("lifecycle_id"))
    refresh_todo_fn(runtime, "running")
    return runtime, True


def validate_runtime_serial_authorization(runtime: dict) -> None:
    """Require the exact pre-issued runtime receipt and current Supervisor binding."""
    from core.supervisor.serial_authorization import (
        SerialAuthorizationError,
        derive_serial_policy_hash,
        validate_serial_authorization,
    )

    record = runtime.get("serial_authorization")
    record_fields = {
        "schema_version", "status", "authorization_source", "allow_serial",
        "identity", "base_policy_hash", "serial_reason_sha256", "policy_hash",
        "receipt",
    }
    if not isinstance(record, dict) or set(record) != record_fields:
        raise SerialAuthorizationError("runtime has no complete pre-issued serial authorization")
    if (
        record.get("schema_version") != 1
        or isinstance(record.get("schema_version"), bool)
        or record.get("status") != "authorized"
        or record.get("authorization_source") != "pre_cli"
        or record.get("allow_serial") is not True
    ):
        raise SerialAuthorizationError("runtime serial authorization is not an authorized pre_cli receipt")
    identity = record.get("identity")
    identity_fields = {
        "runtime_run_uuid", "supervisor_run_id", "session_id", "lifecycle_id",
        "round_id", "life_id", "life_kind", "round_no",
    }
    if not isinstance(identity, dict) or set(identity) != identity_fields:
        raise SerialAuthorizationError("runtime serial authorization identity is malformed")
    lifecycle_id = runtime.get("lifecycle_id")
    life_no = runtime.get("life_no")
    if (
        not isinstance(lifecycle_id, str)
        or not lifecycle_id.strip()
        or isinstance(life_no, bool)
        or not isinstance(life_no, int)
        or life_no < 1
    ):
        raise SerialAuthorizationError("runtime Life identity is incomplete or invalid")
    runtime_life_id = runtime.get("life_id")
    if runtime_life_id is not None and (
        not isinstance(runtime_life_id, str)
        or not runtime_life_id.strip()
        or runtime_life_id != f"life-{life_no}"
    ):
        raise SerialAuthorizationError("runtime life_id does not match life_no")
    runtime_round_id = runtime.get("round_id")
    if runtime_round_id is not None and runtime_round_id != lifecycle_id:
        raise SerialAuthorizationError("runtime round_id does not match lifecycle_id")
    session_ids: list[str] = []
    for field_name in ("session_id", "polling_id"):
        value = runtime.get(field_name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise SerialAuthorizationError(f"runtime {field_name} is invalid")
        session_ids.append(value)
    if not session_ids or any(value != session_ids[0] for value in session_ids[1:]):
        raise SerialAuthorizationError("runtime session_id and polling_id are missing or conflict")
    runtime_life_kind = runtime.get("life_kind")
    trusted_mine_host = (
        runtime_life_kind == "mine"
        and host_serial_worker(runtime.get("host"))
    )
    if (
        runtime_life_kind not in {"fix", "mine"}
        or (runtime_life_kind == "mine" and not trusted_mine_host)
        or identity.get("life_kind") != runtime_life_kind
        or identity.get("runtime_run_uuid") != runtime.get("run_id")
        or identity.get("lifecycle_id") != lifecycle_id
        or identity.get("round_id") != lifecycle_id
        or identity.get("session_id") != session_ids[0]
        or identity.get("life_id") != f"life-{life_no}"
    ):
        raise SerialAuthorizationError("runtime identity does not match its serial authorization")
    if record.get("base_policy_hash") != runtime.get("autonomous_policy_hash"):
        raise SerialAuthorizationError("runtime base policy changed after serial authorization")
    derived_policy_hash = derive_serial_policy_hash(
        identity=identity,
        base_policy_hash=record.get("base_policy_hash"),
        serial_reason_sha256=record.get("serial_reason_sha256"),
    )
    if record.get("policy_hash") != derived_policy_hash:
        raise SerialAuthorizationError("runtime serial authorization policy hash is invalid")

    sup_fn = _get_pp_attr("get_supervisor", get_supervisor)
    supervisor = sup_fn()
    state = supervisor.status()
    current_identity = {
        "supervisor_run_id": getattr(state, "run_id", None),
        "session_id": getattr(state, "session_id", None),
        "round_id": getattr(state, "round_id", None),
        "life_id": getattr(state, "life_id", None),
        "life_kind": getattr(state, "life_kind", None),
        "round_no": getattr(state, "round_no", None),
    }
    if any(identity.get(key) != value for key, value in current_identity.items()):
        raise SerialAuthorizationError("current Supervisor identity differs from pre authorization")
    if current_identity["round_id"] != lifecycle_id:
        raise SerialAuthorizationError("Supervisor round_id does not match runtime lifecycle_id")
    identity_args = {
        key: value for key, value in identity.items() if key != "life_kind"
    }
    validate_serial_authorization(
        state,
        record.get("receipt"),
        **identity_args,
        life_kind=identity["life_kind"],
        host=runtime.get("host"),
        policy_hash=derived_policy_hash,
    )


def continue_after_death() -> str:
    load_fn = _get_pp_attr("_load_runtime", _get_pp_attr("load_runtime", load_runtime))
    stop_fn = _get_pp_attr("_set_polling_stop", _get_pp_attr("set_polling_stop", set_polling_stop))
    lock_fn = _get_pp_attr("runtime_lock", runtime_lock)
    save_fn = _get_pp_attr("save_runtime", save_runtime)
    event_fn = _get_pp_attr("append_runtime_event", append_runtime_event)

    state = load_fn()
    policy = state.get("polling_policy")
    autonomous = bool(state.get("autonomous_mode"))
    if policy == "manual":
        stop_fn("manual_policy")
        return "stopped-manual"
    if policy != "auto":
        return "decision-required"

    stop_reason = state.get("lifecycle_stop_reason")
    if stop_reason in {
            "no_valid_progress", "max_small_lifecycle_cycles", "lifecycle_wall_time_budget",
    }:
        stop_fn(stop_reason)
        return f"stopped-{stop_reason}"

    sup = None
    try:
        sup_fn = _get_pp_attr("_supervisor", _get_pp_attr("get_supervisor", get_supervisor))
        sup = sup_fn()
    except Exception as exc:
        with lock_fn():
            state = load_fn()
            event_fn(state, "warning", time.time(), f"supervisor_lookup_failed: {exc}")
            save_fn(state)

    if sup is not None:
        try:
            if hasattr(sup, "decision_snapshot"):
                snap = sup.decision_snapshot()
                cov_complete = bool(snap.get("coverage_complete"))
            else:
                cov_complete = bool(getattr(sup.status(), "coverage_complete", False))

            st = sup.status()
            fc = _get_pp_attr("finding_confirmation", finding_confirmation)
            root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
            open_findings = fc.open_findings(
                getattr(st, "findings", []), root_fn(),
            ) if fc else []
            if cov_complete and not open_findings:
                stop_fn("coverage_target_completed")
                return "stopped-complete"
            if cov_complete and open_findings:
                base_json_fn = _get_pp_attr("_baseline_json", _get_pp_attr("baseline_json", baseline_json))
                fix_mode = state.get("fix_mode") or (base_json_fn() or {}).get("mode", "auto")
                if fix_mode == "log-only":
                    stop_fn("coverage_target_completed_log_only")
                    return "stopped-complete-with-findings"
                with lock_fn():
                    state = load_fn()
                    event_fn(
                        state, "autonomous-continue-open-findings", time.time(),
                        "; ".join(
                            f"{item.get('id', '')}:{item.get('reason') or item.get('title', '')}"
                            for item in open_findings[:20]
                        ),
                    )
                    save_fn(state)
        except Exception as exc:
            with lock_fn():
                state = load_fn()
                event_fn(state, "warning", time.time(), f"supervisor_status_failed: {exc}")
                save_fn(state)

    is_daemon = bool(state.get("daemon") or os.environ.get("BUGX_DAEMON") == "1")
    if not is_daemon and not autonomous:
        if (state.get("lifecycle_auto_started")
                and state.get("lifecycle_credited_total", 0) == 0):
            stop_fn("auto_lifecycle_zero_findings")
            return "stopped-zero-findings"

    try:
        from core.runtime.interaction import try_get_preset_runtime_config
    except ImportError:
        try:
            from core.runtime.interaction import try_get_preset_runtime_config
        except ImportError:
            try_get_preset_runtime_config = None
    if try_get_preset_runtime_config is not None:
        uses_layout_fn = _get_pp_attr("uses_project_layout", uses_project_layout)
        root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
        preset_cfg = try_get_preset_runtime_config(root_fn() if uses_layout_fn() else None)
        max_rounds = preset_cfg.get("max_rounds")
        current_round_no = int(state.get("lifecycle_no", 0) or 0)
        if max_rounds is not None and current_round_no >= int(max_rounds):
            stop_fn("fast_verification_max_rounds_reached")
            return "stopped-max-rounds"

    prep_fn = _get_pp_attr("_prepare_next_round", prepare_next_round)
    _, started = prep_fn()
    if not started:
        stop_fn("auto_next_round_start_failed")
        return "start-failed"
    return "continued"


def knowledge_round_result(r_result: dict, runtime: dict) -> dict:
    settle_fn = _get_pp_attr("_settle_findings_path", settle_findings_path)
    if auto_mode(runtime) and settle_fn(runtime) is None:
        return {**r_result, "findings": 0, "credited": 0}
    return r_result


def knowledge_review_matches(r_result: dict, runtime: dict) -> tuple[bool, str]:
    settle_fn = _get_pp_attr("_settle_findings_path", settle_findings_path)
    if auto_mode(runtime) and settle_fn(runtime) is None:
        return True, "legacy text settlement has no structured Finding review"
    return lesson_review_matches(
        r_result,
        runtime.get("lifecycle_id"),
        str(runtime.get("polling_id") or "") or None,
    )
