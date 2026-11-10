#!/usr/bin/env python3
"""launch_phases/post_coverage_sync.py - 覆盖清单原子结算、证据失效回退、会话封存与攻击图谱吸收 (DOC-125 / DOC-154)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from core.launch_phases.common import (
    AGENT_DIR,
    resolve_project_root,
    uses_project_layout,
    bugx_paths,
    coverage_manifest_path,
)
from core.launch_phases.session_runtime import (
    runtime_lock,
    load_runtime,
    save_runtime,
    append_runtime_event,
    iso_time,
    baseline_json,
    runtime_summary,
)
from core.launch_phases.supervisor_bridge import get_supervisor
from core.launch_phases.finding_settlement import settle_findings_path


def _pp():
    return sys.modules.get("core.launch_phases.post_phase") or sys.modules.get("launch_phases.post_phase")


def _get_pp_attr(name: str, fallback=None):
    pp = _pp()
    if pp and hasattr(pp, name):
        return getattr(pp, name)
    return fallback


def complete_lifecycle(report_path: str) -> dict:
    with runtime_lock():
        state = load_runtime()
        state["lifecycle_terminal"] = True
        state["lifecycle_last_report"] = report_path
        state["lifecycle_history"] = [
            *state.get("lifecycle_history", []),
            {
                "lifecycle_no": state.get("lifecycle_no"),
                "lifecycle_id": state.get("lifecycle_id"),
                "auto_started": bool(state.get("lifecycle_auto_started")),
                "cycles_completed": state.get("lifecycle_cycles_completed", 0),
                "findings_total": state.get("lifecycle_findings_total", 0),
                "credited_total": state.get("lifecycle_credited_total", 0),
                "fraud_total": state.get("lifecycle_fraud_total", 0),
                "life_after": state.get("lifecycle_last_life"),
                "outcome": state.get("lifecycle_outcome") or "COMPLETE",
                "stop_reason": state.get("lifecycle_stop_reason") or "life_exhausted",
                "report": report_path,
            },
        ][-100:]
        append_runtime_event(
            state, "lifecycle-completed", time.time(),
            f"lifecycle={state.get('lifecycle_no')} "
            f"credited={state.get('lifecycle_credited_total', 0)}",
        )
        save_runtime(state)
    return state


def reconcile_checkpoint_coverage() -> int:
    """Settle validated Supervisor checkpoints into the lifecycle manifest."""
    from core.coverage import coverage_evidence
    from core.coverage import module_coverage
    import core.secure_atomic as secure_atomic

    root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
    root = root_fn().resolve()
    manifest_fn = _get_pp_attr("coverage_manifest_path", coverage_manifest_path)
    manifest = manifest_fn()
    if manifest.is_symlink():
        raise ValueError(f"拒绝结算符号链接覆盖清单: {manifest}")
    if not manifest.is_file():
        raise ValueError(f"覆盖清单不存在: {manifest}")
    rows, parse_errors = module_coverage.parse_manifest(manifest)
    if parse_errors or not rows:
        raise ValueError("覆盖清单无效，拒绝 checkpoint 结算: " + "; ".join(parse_errors))

    sup_fn = _get_pp_attr("get_supervisor", get_supervisor)
    supervisor = sup_fn()
    state = supervisor.status()
    coverage = getattr(state, "coverage", {})
    if not isinstance(coverage, dict):
        raise ValueError("Supervisor coverage 状态无效")
    covered_ids = {str(unit) for unit, value in coverage.items() if value == "covered"}
    rows_by_id = {module_coverage.coverage_unit_id(row): row for row in rows}
    unknown = sorted(covered_ids - set(rows_by_id))
    if unknown:
        raise ValueError("Supervisor covered unit 不在当前清单: " + ", ".join(unknown))

    if covered_ids:
        target_fingerprint = getattr(state, "target_fingerprint", None)
        if not isinstance(target_fingerprint, str) or not target_fingerprint.strip():
            raise ValueError("Supervisor 缺少 target_fingerprint，拒绝覆盖结算")
        gate = supervisor.parallel_gate()
        if bool(gate.get("target_drift")):
            raise ValueError("Supervisor target drift，拒绝使用旧 checkpoint 覆盖当前清单")
        evidence_path = manifest.with_name("coverage-evidence.json")
        report = coverage_evidence.validate_manifest(
            evidence_path,
            workspace_root=root,
            expected_units=covered_ids,
            expected_targets={unit: target_fingerprint for unit in covered_ids},
        )
        invalid = {
            "missing": list(report.missing),
            "invalid": list(report.invalid),
            "stale": list(report.stale),
            "duplicate": list(report.duplicate),
            "conflicts": list(report.conflicts),
        }
        problems = {key: value for key, value in invalid.items() if value}
        if problems:
            raise ValueError(
                "Supervisor covered checkpoint evidence 未通过校验: "
                + json.dumps(problems, ensure_ascii=False, sort_keys=True)
            )

        def evidence_field(evidence: Any, field: str, default: Any = None) -> Any:
            if isinstance(evidence, Mapping):
                return evidence.get(field, default)
            return getattr(evidence, field, default)

        def finding_targets_unit(finding: Any, unit_id: str) -> bool:
            if isinstance(finding, Mapping):
                finding_unit = finding.get("unit_id")
                target_units = finding.get("target_units")
            else:
                finding_unit = getattr(finding, "unit_id", None)
                target_units = getattr(finding, "target_units", None)
            return finding_unit == unit_id or (
                isinstance(target_units, (list, tuple, set, frozenset))
                and unit_id in target_units
            )

        evidence_by_unit = {
            str(evidence_field(record, "unit_id")): record
            for record in report.records
        }
        findings = getattr(state, "findings", ()) or ()
        for unit_id in covered_ids:
            evidence = evidence_by_unit.get(unit_id)
            passed = (
                evidence is not None
                and evidence_field(evidence, "status") == "passed"
                and evidence_field(evidence, "exit_code") == 0
            )
            poc = evidence is not None and (
                evidence_field(evidence, "reproduced_crash", False) is True
                or evidence_field(evidence, "evidence_type", "")
                in {"reproduction_poc", "exploit_poc"}
            )
            related_finding = any(
                finding_targets_unit(finding, unit_id) for finding in findings
            )
            if not passed and not (poc and related_finding):
                raise ValueError(f"Supervisor covered unit 缺少 passed evidence: {unit_id}")

    updated_rows = [
        replace(row, status="已覆盖")
        if module_coverage.coverage_unit_id(row) in covered_ids and row.status != "已覆盖"
        else row
        for row in rows
    ]
    if getattr(state, "coverage_complete", False):
        uncovered = [row for row in updated_rows if row.status != "已覆盖"]
        if uncovered:
            raise ValueError(
                "Supervisor 宣告 coverage_complete，但 lifecycle 清单仍有未覆盖单元: "
                + ", ".join(f"#{row.number} {row.name}" for row in uncovered)
            )

    changed = sum(old.status != new.status for old, new in zip(rows, updated_rows))
    if changed:
        by_number = {row.number: module_coverage.row_text(row) for row in updated_rows}
        lines = manifest.read_text(encoding="utf-8").splitlines()
        in_table = False
        seen_numbers: set[str] = set()
        for index, line in enumerate(lines):
            if "路径/范围" in line and "状态" in line:
                in_table = True
                continue
            if not in_table or not line.lstrip().startswith("|"):
                continue
            cells = module_coverage.parse_cells(line)
            number = cells[0].strip() if cells else ""
            replacement = by_number.get(number)
            if replacement is not None:
                lines[index] = replacement
                seen_numbers.add(number)
        expected_numbers = set(by_number)
        if seen_numbers != expected_numbers:
            raise ValueError("覆盖清单行在原子结算前发生变化，拒绝部分更新")
        secure_atomic.atomic_write_text(
            manifest, "\n".join(lines) + "\n", mode=0o600, parent_mode=0o700,
        )

    return changed


def content_invalidated_units() -> list[str]:
    """Content-changed coverage units reported by the rescan (L1-ADV-D4)."""
    from core.coverage import module_coverage

    manifest_fn = _get_pp_attr("coverage_manifest_path", coverage_manifest_path)
    manifest = manifest_fn()
    diff_path = manifest.with_name("inventory-diff.json")
    if diff_path.is_symlink() or not diff_path.is_file():
        return []
    try:
        payload = json.loads(diff_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return []
    changes = payload.get("changes") if isinstance(payload, dict) else None
    if not isinstance(changes, dict):
        return []
    changed = {
        str(scope).strip()
        for scope in changes.get("changed") or ()
        if isinstance(scope, str) and scope.strip()
    }
    if not changed:
        return []
    try:
        rows, errors = module_coverage.parse_manifest(manifest)
    except (OSError, ValueError):
        return []
    if errors or not rows:
        return []
    units: list[str] = []
    for row in rows:
        if getattr(row, "phase", "") != "内部":
            continue
        if getattr(row, "status", "") == "已覆盖":
            continue
        scope = getattr(row, "path", "").strip().strip("`").rstrip("/")
        if scope in changed:
            units.append(module_coverage.coverage_unit_id(row))
    return units


def hand_back_invalidated_units() -> dict:
    """Invalidate content-changed units so the round can re-dispatch them."""
    inv_fn = _get_pp_attr("_content_invalidated_units", content_invalidated_units)
    units = inv_fn()
    if not units:
        return {"invalidated": [], "skipped": [], "units": []}
    sup_fn = _get_pp_attr("get_supervisor", get_supervisor)
    supervisor = sup_fn()
    state = supervisor.status()
    coverage = getattr(state, "coverage", {}) or {}
    pending = [unit for unit in units if coverage.get(unit) == "covered"]
    if not pending:
        return {"invalidated": [], "skipped": units, "units": units}
    result = supervisor.invalidate_coverage(
        units=pending,
        reason="增量重扫：内容已变化，旧证据失效（post module_coverage 门禁）",
    )
    return {**result, "units": units}


def dsh_zstd_reader():
    """DSH 会话归档读取器：解压必须留在引擎侧。"""
    zstd = shutil.which("zstd")
    if not zstd:
        for candidate in ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd", "/usr/bin/zstd"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                zstd = candidate
                break
    if not zstd:
        return None

    def read_lines(path: Path):
        proc = subprocess.Popen(
            [zstd, "-dc", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stream = proc.stdout
            if stream is None:
                return
            for line in stream:
                yield line
            stream.close()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    return read_lines


def lifecycle_report_state(runtime: dict) -> dict:
    """Bind report counters to the just-validated lifecycle manifest."""
    from core.coverage import module_coverage

    manifest_fn = _get_pp_attr("coverage_manifest_path", coverage_manifest_path)
    rows, errors = module_coverage.parse_manifest(manifest_fn())
    if errors or not rows:
        raise ValueError("终态报告无法读取有效覆盖清单: " + "; ".join(errors))
    report_state = dict(runtime)
    from core.host_runtime.host_usage import collect_for_report

    root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
    report_state["host_usage"] = collect_for_report(
        root_fn(), runtime, reader_factory=dsh_zstd_reader,
    )
    report_state["coverage"] = {
        module_coverage.coverage_unit_id(row): (
            "covered" if row.status == "已覆盖" else "pending"
        )
        for row in rows
    }
    report_state["audit_units_total"] = len(rows)
    return report_state


def close_session(final_round_id: str | None = None, *, reason: str = "completed",
                  detail: str | None = None) -> dict:
    """自动封存当前 Session；Session 不生成独立报告或执行第三层同步。"""
    with runtime_lock():
        state = load_runtime()
        if state.get("session_closed_at"):
            return state
        current = time.time()
        final_id = final_round_id or state.get("round_id") or state.get("lifecycle_id")
        final_state = "completed" if reason == "completed" else reason
        session_record = {
            "session_id": state.get("session_id"),
            "startup_id": state.get("startup_id"),
            "started_at": state.get("session_started_at") or state.get("started_at"),
            "finished_at": iso_time(current),
            "outcome": final_state,
            "final_round_id": final_id,
            "elapsed_seconds": round(float(state.get("total_elapsed_seconds", 0.0)), 3),
        }
        if detail or (reason and reason != "completed"):
            session_record["reason"] = detail or reason
        completed = int(state.get("sessions_completed", 0))
        aborted = int(state.get("sessions_aborted", 0))
        if final_state == "completed":
            completed += 1
        elif final_state == "aborted":
            aborted += 1
        sessions = [*state.get("sessions", []), session_record][-100:]
        root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
        uses_layout_fn = _get_pp_attr("uses_project_layout", uses_project_layout)
        if uses_layout_fn():
            session_payload = bugx_paths.update_session_state(
                root_fn(),
                {
                    "state": final_state,
                    "session_id": state.get("session_id"),
                    "final_round_id": final_id,
                    "closed_at": session_record["finished_at"],
                    "total_elapsed_seconds": state.get("total_elapsed_seconds", 0.0),
                    "sessions_completed": completed,
                    "sessions_aborted": aborted,
                    "sessions": sessions,
                },
            )
            completed = int(session_payload.get("sessions_completed", completed))
            aborted = int(session_payload.get("sessions_aborted", aborted))
            sessions = list(session_payload.get("sessions", sessions))
        state.update({
            "session_state": final_state,
            "session_closed_at": session_record["finished_at"],
            "final_round_id": final_id,
            "startup_result": "completed" if final_state == "completed" else final_state,
            "sessions_completed": completed,
            "sessions_aborted": aborted,
            "sessions": sessions,
        })
        append_runtime_event(state, "session-closed", current, detail or reason)
        save_runtime(state)
        try:
            from core.runtime import global_telemetry
            p_root = root_fn()
            p_name = p_root.name if p_root else "default"
            history = state.get("intelligence_history") or []
            total_toks = 0
            for item in history:
                if isinstance(item, dict) and isinstance(item.get("efficiency"), dict):
                    total_toks += int(item["efficiency"].get("token_count") or 0)
            global_telemetry.record_session_telemetry({
                "session_id": state.get("session_id") or "session",
                "project_name": p_name,
                "project_hash": hashlib.sha256(p_name.encode("utf-8")).hexdigest()[:16],
                "start_time": session_record.get("started_at") or session_record.get("finished_at"),
                "end_time": session_record.get("finished_at"),
                "duration_seconds": session_record.get("elapsed_seconds", 0.0),
                "tokens_consumed": total_toks,
                "token_method": "aggregated",
                "findings_count": int(state.get("findings_total", 0) or 0),
                "credited_count": int(state.get("lifecycle_credited_total", 0) or 0),
                "rounds_count": int(state.get("polling_round", 1) or 1),
                "status": final_state,
            })
        except Exception:
            pass
    return runtime_summary(current)


def life_summary() -> dict | None:
    base_json_fn = _get_pp_attr("baseline_json", baseline_json)
    state = base_json_fn()
    if state is None:
        return None
    keys = ("life", "found_total", "round", "rounds_completed", "alive", "mode")
    return {key: state.get(key) for key in keys if key in state}


def print_new_findings() -> None:
    """打印本轮新增发现摘要。"""
    try:
        life_path = AGENT_DIR / "bugx-life.json"
        uses_layout_fn = _get_pp_attr("uses_project_layout", uses_project_layout)
        root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
        if uses_layout_fn():
            life_path = bugx_paths.require(root_fn()).life
        d = json.loads(
            life_path.read_text(encoding="utf-8")
        )
        hist = d.get("history") or []
        if not hist:
            return
        last = hist[-1]
        findings = last.get("findings") or []
        credited = last.get("credited", len(findings))
        print("-" * 56)
        print(f"本轮（round={last.get('round')}）新增发现：{len(findings)} 条 "
              f"（计命 {credited} 条）")
        for f in findings:
            print(f"  - {f}")
        print("请逐条复核：证据是否真实、修复是否转绿。")
    except Exception as e:
        print(f"（读取本轮 findings 摘要失败: {e}）")


def attack_graph_degraded(exc: BaseException) -> None:
    """把图谱故障降级成可审计的 attack-graph-degraded 运行事件。"""
    note = f"attack graph degraded: {type(exc).__name__}: {exc}"[:400]
    print(f"⚠ 攻击面图谱已降级（不影响本轮结算）：{note}")
    try:
        with runtime_lock():
            state = load_runtime()
            append_runtime_event(state, "attack-graph-degraded", time.time(), note)
            state["attack_graph"] = {"status": "degraded", "reason": note}
            save_runtime(state)
    except (OSError, ValueError, TypeError, RuntimeError, KeyError):
        pass


def read_json_or_none(path: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return None


def absorb_attack_graph(runtime: dict) -> dict | None:
    """Round 结算后把真实证据吸收进 LivingAttackGraph (DOC-125 C125-1/C125-5)。"""
    uses_layout_fn = _get_pp_attr("uses_project_layout", uses_project_layout)
    degraded_fn = _get_pp_attr("_attack_graph_degraded", attack_graph_degraded)
    if not uses_layout_fn():
        return None
    try:
        from core.autonomous_cve import attack_graph_store
    except (ImportError, OSError) as exc:
        degraded_fn(exc)
        return None
    root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
    root = root_fn()
    base_json_fn = _get_pp_attr("baseline_json", baseline_json)
    read_json_fn = _get_pp_attr("_read_json_or_none", read_json_or_none)
    rt_trans_fn = _get_pp_attr("_runtime_transaction")

    try:
        baseline = base_json_fn() or {}
        round_no = baseline.get("round")
        round_no = round_no if isinstance(round_no, int) else None
        scope = bugx_paths.require(root)

        evidence_rows: list[dict] = []
        try:
            from core.coverage import coverage_evidence
            if scope.coverage.is_file():
                store = coverage_evidence.EvidenceStore(
                    scope.coverage, workspace_root=root,
                )
                evidence_rows = [row.to_dict() for row in store.records()]
        except Exception as exc:
            print(f"  （攻击图谱未能读取覆盖证据: {exc}）")

        findings: list = []
        findings_path = None
        try:
            findings_path = settle_findings_path(runtime)
        except (OSError, ValueError, TypeError, KeyError):
            findings_path = None
        settled = read_json_fn(findings_path) if findings_path else None
        if isinstance(settled, dict) and isinstance(settled.get("findings"), list):
            findings = settled["findings"]
        else:
            supervisor = read_json_fn(scope.supervisor) or {}
            if isinstance(supervisor.get("findings"), list):
                findings = supervisor["findings"]

        existing_graph_meta = (runtime.get("attack_graph") or {})
        absorbed_unit_ids = set(existing_graph_meta.get("absorbed_unit_ids") or [])
        if absorbed_unit_ids and evidence_rows:
            unabsorbed_rows = [
                row for row in evidence_rows
                if row.get("unit_id") and str(row.get("unit_id")) not in absorbed_unit_ids
            ]
        else:
            unabsorbed_rows = evidence_rows

        summary = attack_graph_store.absorb_evidence(
            root, unabsorbed_rows, round_no=round_no,
        )
        summary.update(attack_graph_store.absorb_findings(
            root, findings, round_no=round_no,
        ))
        stats = attack_graph_store.stats(root)
        attack_graph_store.finalize_round(
            root, {**summary, "route_total": (stats.get("nodes") or {}).get("routes", 0)},
            round_no=round_no,
        )
        all_absorbed = set(absorbed_unit_ids)
        for row in evidence_rows:
            uid = row.get("unit_id")
            if uid:
                all_absorbed.add(str(uid))

        from contextlib import nullcontext
        ctx = rt_trans_fn(runtime) if rt_trans_fn else nullcontext(load_runtime())
        with ctx as state:
            state["attack_graph"] = {
                "status": "absorbed",
                "round": round_no,
                "absorbed": summary,
                "snapshot": stats.get("snapshot"),
                "nodes": stats.get("nodes"),
                "events": stats.get("events"),
                "verified_this_round": summary.get("verified", 0),
                "rejected_this_round": summary.get("rejected", 0),
                "backtrack_hypotheses": stats.get("backtrack_hypotheses"),
                "rollbacks": stats.get("rollbacks"),
                "schema_validation": (stats.get("schema_validation") or {}).get("mode"),
                "absorbed_unit_ids": sorted(all_absorbed)[-500:],
            }
            append_runtime_event(
                state, "attack-graph-absorbed", time.time(),
                f"round={round_no} verified={summary.get('verified')} "
                f"rejected={summary.get('rejected')} sinks={summary.get('sinks')} "
                f"units_evidence={summary.get('units_absorbed')}",
            )
            if not rt_trans_fn:
                save_runtime(state)
        print(
            f"  攻击面图谱已演化：实锤 {summary.get('verified', 0)} / "
            f"反驳 {summary.get('rejected', 0)}（已回溯回滚）/ "
            f"待验证 {summary.get('proposed', 0)}；"
            f"图谱节点 {stats.get('nodes')}"
        )
        return summary
    except Exception as exc:
        degraded_fn(exc)
        return None
