#!/usr/bin/env python3
"""launch_phases/lifecycle_reporter.py - 生命周期报告渲染、边际收益与情报分析 (DOC-036)。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .common import (
    AGENT_DIR,
    LIFECYCLE_REPORT_NAME,
    resolve_project_root,
    uses_project_layout,
    pollings_root as _pollings_root,
)
from .session_runtime import (
    load_runtime,
    save_runtime,
    runtime_lock,
    iso_time,
)

try:
    import core.bugx_paths as bugx_paths
except ImportError:
    bugx_paths = None


def _findings_payload_path(runtime: dict | None = None) -> Path | None:
    """解析当前 Life 的 findings 落盘路径（0.6.109：实现下沉本模块）。

    旧实现经软环委托 round_orchestrator 且异常时盲回退相对 cwd 的
    ``.bugx/findings.json``——会读错文件。现在按 DOC-031 H1 修复语义：
    polling_id/session_id 候选链 → 目录存在性校验 → 最后候选
    ``findings.json``（存在与否由调用方校验）。
    """
    pollings_root = _pollings_root(resolve_project_root())
    lifecycle_id = str((runtime or {}).get("lifecycle_id") or "unknown")
    candidates: list[Path] = []
    for key in ("polling_id", "session_id"):
        value = str((runtime or {}).get(key) or "")
        if value.startswith("polling-") and value not in candidates:
            candidates.append(value)
    for candidate in candidates:
        lifecycle_dir = pollings_root / candidate / "lifecycles" / lifecycle_id
        findings = lifecycle_dir / "findings.json"
        if findings.is_file():
            return findings
        target_dir = lifecycle_dir
        if target_dir.is_dir():
            return findings
    fallback = pollings_root / "polling-000001" / "lifecycles" / lifecycle_id / "findings.json"
    if fallback.is_file() and not fallback.is_symlink():
        return fallback
    # Standalone/legacy execution has no formal polling lifecycle yet.  Keep
    # the compatibility path used by the launcher and isolated tests, but only
    # accept a regular file so a project-root symlink cannot redirect telemetry
    # to another runtime.
    if not uses_project_layout():
        legacy = Path(".bugx/findings.json")
        if legacy.is_file() and not legacy.is_symlink():
            return legacy
    return None


def _verify_findings_list_binding(runtime: dict, path: Path | None = None) -> tuple[bool, str]:
    """fail-closed 版本（0.6.109）：无路径/文件缺失 → 校验失败而非放行。

    R5 复盘 D6：本模块语义是 **list** findings（findings.json 为数组、逐条
    校验 lifecycle_id 绑定），与 finding_settlement 的 dict+run_binding 结算
    门禁同名不同契约。改名 ``_verify_findings_list_binding`` 消除裸导入
    混淆——import 错模块即静默错判。结算语义版本仍叫
    ``finding_settlement._verify_findings_binding``（post 主路径）。
    """
    if path is None:
        return False, "no-findings"
    if not path.is_file():
        return False, f"findings 文件不存在: {path}"
    runtime = runtime or {}
    lifecycle_id = str(runtime.get("lifecycle_id") or "")
    current_round = runtime.get("round")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return False, f"findings 无法解析: {exc}"
    if not isinstance(payload, list):
        return False, "findings 不是数组"
    recorded_round = runtime.get("round_at_start")
    if (isinstance(current_round, int) and isinstance(recorded_round, int)
            and current_round < recorded_round):
        return False, "findings 轮号早于当前 Round"
    if lifecycle_id and payload:
        for item in payload:
            if not isinstance(item, dict):
                return False, "findings 条目结构无效"
            if item.get("lifecycle_id") != lifecycle_id:
                return False, "findings 存在未绑定当前生命周期的条目"
    return True, "findings 已绑定当前生命周期"


def _remediation_report(state: dict) -> list[dict[str, Any]]:
    """Return confirmed findings without a valid fix or deferred proposal."""
    path = _findings_payload_path(state)
    if path is None or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list):
        return []
    try:
        from core.ledger.finding_confirmation import validate_remediation
    except ImportError:
        return []
    entries: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        confirmation = finding.get("confirmation")
        if not isinstance(confirmation, dict) or confirmation.get("state") != "confirmed":
            continue
        result = validate_remediation(finding, resolve_project_root())
        if result.get("status") == "ok":
            continue
        remediation = finding.get("remediation")
        entries.append({
            "fingerprint": finding.get("fingerprint") or finding.get("id") or "unknown",
            "severity": finding.get("severity") or "unknown",
            "proposal": remediation.get("proposal") if isinstance(remediation, dict) else None,
            "blocking_deps": remediation.get("blocking_deps", []) if isinstance(remediation, dict) else [],
            "errors": result.get("errors", []),
        })
    return entries

def write_lifecycle_report(state: dict) -> str:
    """Write the authoritative report, conclusion and integrity records.

    These files belong to the active lifecycle directory.  A marker in an
    arbitrary project-root report is not accepted as proof of completion:
    every terminal lifecycle gets a fresh report and a machine-readable
    conclusion whose digest is recorded in ``integrity.json``.
    """
    if uses_project_layout():
        active = bugx_paths.require(resolve_project_root())
        report_path = active.report
        conclusion_path = active.conclusion
        integrity_path = active.integrity
        life_path = active.life
        events_path = active.events
    else:
        report_path = resolve_project_root() / LIFECYCLE_REPORT_NAME
        conclusion_path = resolve_project_root() / "conclusion.json"
        integrity_path = resolve_project_root() / "integrity.json"
        life_path = AGENT_DIR / "bugx-life.json"
        events_path = AGENT_DIR / "events.jsonl"
    write_once = uses_project_layout()
    if write_once:
        for path in (report_path, conclusion_path, integrity_path):
            if path.exists() or path.is_symlink():
                raise OSError(f"终局报告已存在，拒绝覆盖: {path}")
    lifecycle_id = state.get("lifecycle_id") or "unknown"
    total_findings = state.get('lifecycle_findings_total', 0)
    credited = state.get('lifecycle_credited_total', 0)
    fraud = state.get('lifecycle_fraud_total', 0)
    real_rate = (credited / total_findings * 100.0) if total_findings > 0 else 0.0
    error_rate = (fraud / total_findings * 100.0) if total_findings > 0 else 0.0
    remediation_gaps = _remediation_report(state)
    remediation_lines = [
        "- 无：每条 confirmed Finding 均有修复证据或合法 deferred 提案。"
    ] if not remediation_gaps else [
        f"- `{item['fingerprint']}` [{item['severity']}] fix_gap："
        f"{item.get('proposal') or '未提供 remediation 提案'}；"
        f"blocking_deps={item.get('blocking_deps') or []}；"
        f"校验错误={item.get('errors') or []}"
        for item in remediation_gaps
    ]

    audit_units = len(state.get('coverage', {})) or state.get('audit_units_total', 0)

    from core.host_runtime.host_usage import report_lines as host_usage_lines

    # DOC-011 §10.2 结构化字段：session/round 标识、增量/累计、覆盖、同步、共享初始态等
    session_id = state.get("session_id") or state.get("polling_id") or "unknown"
    round_id = lifecycle_id
    round_no = state.get("lifecycle_no", 1)
    history = state.get("lifecycle_history") or []
    previous_round_id = history[-1].get("lifecycle_id") if history else None
    # 本 Round 为终局候选（报告封存即候选），后续共享通过 final_round_id 引用
    final_round_candidate = True
    # 累计汇总：从 Session 首个 Round 到本 Round
    cumulative_findings = sum(int(h.get("findings_total", 0) or 0) for h in history) + total_findings
    cumulative_credited = sum(int(h.get("credited_total", 0) or 0) for h in history) + credited
    cumulative_fraud = sum(int(h.get("fraud_total", 0) or 0) for h in history) + fraud
    # 覆盖维度（占位：真实维度由 coverage_status 驱动，此处至少保证结构化字段存在）
    coverage = state.get("coverage") or {}
    covered_cnt = sum(1 for v in coverage.values() if v in {"covered", "covered_with_findings", "covered_no_finding"}) if coverage else 0
    pending_cnt = len(coverage) - covered_cnt if coverage else 0
    # 同步状态（Life 项目级、Round 全局级）；没有边界记录时才回退到
    # 旧的总状态，避免把一次 project 同步误报为 global 已完成。
    boundaries = state.get("sync_boundaries") or {}
    project_boundary = boundaries.get("project") if isinstance(boundaries, dict) else None
    global_boundary = boundaries.get("global") if isinstance(boundaries, dict) else None
    sync_project = (
        project_boundary.get("status") if isinstance(project_boundary, dict)
        else state.get("sync_status", "local")
    )
    sync_global = (
        global_boundary.get("status") if isinstance(global_boundary, dict)
        else state.get("sync_status", "local")
    )
    # 共享初始态：自动续轮中间 Round 为 share_deferred，终局为 not_started
    share_initial = "not_started"
    try:
        if state.get("polling_policy") == "auto" and not state.get("lifecycle_terminal"):
            # 中间 Round 但尚未 terminal 的报告不会走到此函数；兜底仍置 not_started
            share_initial = "share_deferred"
    except Exception:
        share_initial = "not_started"
    section = "\n".join([
        f"<!-- bugx-round:{lifecycle_id} -->",
        f"<!-- bugx-session:{session_id} previous:{previous_round_id or 'none'} final_candidate:{str(final_round_candidate).lower()} -->",
        f"## BugX Round {round_no} 结束报告",
        "",
        "### 核心执行指标",
        f"- 开始时间: `{state.get('lifecycle_started_at') or 'N/A'}`",
        f"- 结束时间: `{iso_time(time.time())}`",
        f"- Session: `{session_id}` | Round: `No.{round_no}` (ID: `{round_id}`) | previous: `{previous_round_id or 'none'}` | final_candidate: `{str(final_round_candidate).lower()}`",
        f"- 经历 Life: `{state.get('lifecycle_cycles_completed', 0)} 个`",
        f"- 发现问题总数: `{total_findings}` (有效: `{credited}`, 拒绝: `{fraud}`)",
        f"- 已确认修复数: `{len(state.get('realized_fix_incidents', []))}`",
        f"- 审计模块数量: `{audit_units}` 个模块/单元",
        f"- 真实 Bug 率: `{real_rate:.1f}%` | 误报扣分率: `{error_rate:.1f}%`",
        f"- Worker 自报 Token: `{state.get('lifecycle_token_count', 0)}`",
        *host_usage_lines(state.get("host_usage")),
        f"- 错题集沉淀数: `{len(state.get('lifecycle_lesson_ids', []))}`",
        f"- 终点 life: `{state.get('lifecycle_last_life')}` | 停止原因: `{state.get('lifecycle_stop_reason') or 'life_exhausted'}`",
        f"- 结论状态: `{state.get('lifecycle_outcome') or 'COMPLETE'}`",
        "",
        "### 待决修复提案（P1-1）",
        *remediation_lines,
        "",
        "### 结构化摘要（DOC-011 §10.2）",
        f"- session_id: `{session_id}` | round_id: `{round_id}` | round_no: `{round_no}` | previous_round_id: `{previous_round_id or 'null'}` | final_round_candidate: `{str(final_round_candidate).lower()}`",
        f"- 增量: findings={total_findings} credited={credited} fraud={fraud} | 累计: findings={cumulative_findings} credited={cumulative_credited} fraud={cumulative_fraud}",
        f"- 覆盖: 单元总数 {len(coverage)} 已覆盖 {covered_cnt} 待处理 {pending_cnt} | 声明维度: 清单/静态/业务链路/动态/证据/修复验证/风险",
        f"- 覆盖债务: pending={pending_cnt} | 停止推荐: {state.get('lifecycle_stop_reason') or 'none'}",
        f"- 同步: Life项目级 {sync_project} | Round全局级 {sync_global} | receipt/outbox 见 lifecycle/sync",
        f"- 共享初始态: `{share_initial}` | 实际共享选择、receipt 与 outbox 属于独立共享操作，不回写已封存报告",
        f"- Worker: cycles={state.get('lifecycle_cycles_completed',0)} lesson_cards={len(state.get('lifecycle_lesson_ids',[]))}",
        "",
        "本报告由 BugX 外部启动协议生成；具体证据、复现、修复和覆盖债务"
        "保存在当前生命周期目录的过程记录中。",
        "",
    ])

    def write_atomic(path: Path, content: str) -> None:
        if path.is_symlink() or path.parent.is_symlink():
            raise OSError(f"拒绝写入符号链接路径: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def write_once_atomic(path: Path, content: str) -> None:
        if path.is_symlink() or path.parent.is_symlink():
            raise OSError(f"拒绝写入符号链接路径: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Hard-link publication is atomic and never replaces an existing
            # report. This closes the TOCTOU gap between existence checking
            # and a concurrent duplicate post.
            os.link(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    publish = write_once_atomic if write_once else write_atomic

    publish(report_path, section)
    report_digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    # DOC-011 §10.2 要求的结构化结论字段，与报告互相校验
    conclusion = {
        "schema_version": 1,
        "session_id": session_id,
        "round_id": round_id,
        "round_no": round_no,
        "lifecycle_id": lifecycle_id,
        "lifecycle_no": state.get("lifecycle_no"),
        "previous_round_id": previous_round_id,
        "final_round_candidate": final_round_candidate,
        "report_hash": report_digest,
        "state": state.get("lifecycle_outcome") or "completed",
        "stop_reason": state.get("lifecycle_stop_reason") or "life_exhausted",
        "cycles_completed": state.get("lifecycle_cycles_completed", 0),
        "findings_total": total_findings,
        "credited_total": credited,
        "fraud_total": fraud,
        "real_rate": round(real_rate, 1),
        "error_rate": round(error_rate, 1),
        "fix_count": len(state.get("realized_fix_incidents", [])),
        "remediation": {
            "fix_gap_count": len(remediation_gaps),
            "fix_gaps": remediation_gaps,
        },
        "token_count": state.get("lifecycle_token_count", 0),
        "lesson_count": len(state.get("lifecycle_lesson_ids", [])),
        "life_after": state.get("lifecycle_last_life"),
        "incremental": {
            "findings": total_findings,
            "credited": credited,
            "fraud": fraud,
        },
        "cumulative": {
            "findings": cumulative_findings,
            "credited": cumulative_credited,
            "fraud": cumulative_fraud,
        },
        "coverage": {
            "total_units": len(coverage),
            "covered": covered_cnt,
            "pending": pending_cnt,
        },
        "sync": {
            "project": sync_project,
            "global": sync_global,
        },
        "share_initial_state": share_initial,
        "report": report_path.name,
        "report_sha256": report_digest,
        "completed_at": iso_time(time.time()),
    }
    publish(conclusion_path, json.dumps(conclusion, ensure_ascii=False, indent=2) + "\n")
    hashes = {"report.md": report_digest, "conclusion.json": hashlib.sha256(
        conclusion_path.read_bytes()).hexdigest()}
    for label, path in (("life.json", life_path), ("events.jsonl", events_path)):
        if path.is_file() and not path.is_symlink():
            hashes[label] = hashlib.sha256(path.read_bytes()).hexdigest()
    # PE-20261005-006（B6 封章扩容）：findings.json 与 supervisor.json 纳入
    # 完整性封章——文件级拼接从此可检测。supervisor.json 用单次字节快照同时
    # 记 sha256+revision+status：revision 相等而哈希不符=篡改（fail-closed），
    # revision 增长=封章后合法演化（finish/next_round/迟到心跳）。缺失时写
    # 显式 false 字段，不静默。时点在结算完成后（post 写报告处），不等宿主
    # finish（真实运行可能从不 finish，且本文件 write-once）。
    sealed_findings = False
    sealed_supervisor = False
    if uses_project_layout():
        findings_path = active.lifecycle_root / "findings.json"
        supervisor_path = active.supervisor
    else:
        findings_path = resolve_project_root() / "findings.json"
        supervisor_path = resolve_project_root() / "supervisor.json"
    if findings_path.is_file() and not findings_path.is_symlink():
        hashes["findings.json"] = hashlib.sha256(
            findings_path.read_bytes()).hexdigest()
        sealed_findings = True
    if supervisor_path.is_file() and not supervisor_path.is_symlink():
        snapshot = supervisor_path.read_bytes()
        hashes["supervisor.json"] = hashlib.sha256(snapshot).hexdigest()
        try:
            supervisor_snapshot = json.loads(snapshot)
            hashes["supervisor_revision"] = supervisor_snapshot.get("revision")
            hashes["supervisor_status"] = supervisor_snapshot.get("status")
        except (json.JSONDecodeError, UnicodeDecodeError):
            hashes["supervisor_revision"] = None
            hashes["supervisor_status"] = None
        sealed_supervisor = True
    integrity = {
        "schema_version": 2,
        "lifecycle_id": lifecycle_id,
        "algorithm": "sha256",
        "files": hashes,
        "sealed_findings": sealed_findings,
        "sealed_supervisor": sealed_supervisor,
        "generated_at": iso_time(time.time()),
    }
    publish(integrity_path, json.dumps(integrity, ensure_ascii=False, indent=2) + "\n")
    return str(report_path)



def confidence_calibration(runtime: dict | None = None) -> dict | None:
    """G6：本轮 confirmed/rejected 的置信度校准画像（自报 vs 结局）。"""
    path = _findings_payload_path(runtime)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list) or not findings:
        return None
    buckets: dict[str, list[float]] = {"confirmed": [], "rejected": []}
    artifact_bound = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        confirmation = finding.get("confirmation")
        if not isinstance(confirmation, dict):
            continue
        state = confirmation.get("state")
        if state not in buckets:
            continue
        confidence = finding.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            buckets[state].append(float(confidence))
        for item in confirmation.get("observations", []):
            if isinstance(item, dict) and item.get("artifact_path"):
                artifact_bound += 1
                break
    confirmed_mean = (
        sum(buckets["confirmed"]) / len(buckets["confirmed"])
        if buckets["confirmed"] else None
    )
    rejected_mean = (
        sum(buckets["rejected"]) / len(buckets["rejected"])
        if buckets["rejected"] else None
    )
    total = len(buckets["confirmed"]) + len(buckets["rejected"])
    overconfident = (
        total >= 5
        and rejected_mean is not None
        and confirmed_mean is not None
        and (confirmed_mean - rejected_mean) < 0.1
        and len(buckets["rejected"]) / total > 0.3
    )
    return {
        "confirmed": len(buckets["confirmed"]),
        "rejected": len(buckets["rejected"]),
        "confirmed_confidence_mean": (
            round(confirmed_mean, 3) if confirmed_mean is not None else None
        ),
        "rejected_confidence_mean": (
            round(rejected_mean, 3) if rejected_mean is not None else None
        ),
        "observation_artifact_bound_findings": artifact_bound,
        "overconfident_warning": overconfident,
    }

def efficiency_card(runtime: dict, round_result: dict) -> dict:
    """E3：轮级效率遥测——每命中耗时、token 成本与知识卡片利用率。"""
    credited = int(round_result.get("credited", 0) or 0)
    duration = float(runtime.get("effective_total_seconds", 0.0) or 0.0)
    tokens = None
    token_method = None
    path = _findings_payload_path(runtime)
    if path is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                value = payload.get("token_count")
                if isinstance(value, int) and not isinstance(value, bool):
                    tokens = value
                method = payload.get("token_method")
                if isinstance(method, str) and method.strip():
                    token_method = method.strip()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            tokens = None
    # runner 无法计量时（token_method=unmeasured）显式置空，防止 0 被当作
    # 真实成本参与 tokens/credited 统计。
    if token_method == "unmeasured":
        tokens = None
    context = runtime.get("lesson_context") or {}
    card = {
        "round": round_result.get("round"),
        "credited": credited,
        "findings": round_result.get("findings", 0),
        "duration_seconds": round(duration, 3),
        "seconds_per_credited": round(duration / credited, 3) if credited else None,
        "token_count": tokens,
        "token_method": token_method,
        "tokens_per_credited": (
            round(tokens / credited, 1) if tokens and credited else None
        ),
        "lesson_cards_prefetched": len(context.get("recommendations") or []),
        "tool_cards_prefetched": len(context.get("tool_cards") or []),
        "incident_cards_prefetched": len(context.get("deep_cards") or []),
        "fraud": round_result.get("fraud", 0),
    }
    return card

def print_post_intelligence(runtime: dict, round_result: dict) -> dict:
    """post 的效率/校准/绑定输出与持久化；失败只降级不阻断结算。"""
    card = efficiency_card(runtime, round_result)
    calibration = confidence_calibration(runtime)
    binding_ok, binding_note = _verify_findings_list_binding(runtime)
    print("-" * 56)
    per_credited = (
        f"{card['seconds_per_credited']}s/条" if card["seconds_per_credited"]
        else "n/a（本轮无计命）"
    )
    tokens_note = (
        f"{card['tokens_per_credited']} tok/条"
        if card["tokens_per_credited"] else (
            f"{card['token_count']} tok（{card['token_method']}）"
            if card["token_count"] else "tokens 未计量"
        )
    )
    print(
        f"效率卡片：耗时 {card['duration_seconds']}s · 计命 {card['credited']}"
        f" · {per_credited} · {tokens_note} · 预取候选 "
        f"P{card['lesson_cards_prefetched']}+T{card['tool_cards_prefetched']}"
        f"+I{card['incident_cards_prefetched']}"
    )
    if calibration:
        print(
            f"校准：confirmed {calibration['confirmed']}"
            f"（均值置信 {calibration['confirmed_confidence_mean']}）/"
            f" rejected {calibration['rejected']}"
            f"（均值 {calibration['rejected_confidence_mean']}）"
            f" · 工件绑定 finding {calibration['observation_artifact_bound_findings']}"
        )
        if calibration.get("overconfident_warning"):
            print("  ⚠ 置信度失准：拒报率高但与确认组置信差距小——下调自报置信度")
    if binding_note == "unbound":
        print("⚠ findings.json 未盖 run_binding 章：报告可被拼接/重放而不被察觉；"
              "导出时补 run_id/lifecycle_id/round")
    elif not binding_ok:
        print(f"✗ {binding_note}")
    record = {
        "efficiency": card,
        "calibration": calibration,
        "findings_binding": binding_note if binding_ok else f"invalid:{binding_note}",
    }
    try:
        with runtime_lock():
            state = load_runtime()
            history = state.setdefault("intelligence_history", [])
            if not isinstance(history, list):
                history = []
            history.append(record)
            state["intelligence_history"] = history[-50:]
            advisory = marginal_return_advisor(state)
            record["marginal_return"] = advisory
            state["intelligence_history"][-1] = record
            save_runtime(state)
        if advisory["recommendation"] != "continue":
            print(f"边际收益顾问 [{advisory['status']}] → {advisory['recommendation']}："
                  f"{advisory['reason']}")
        else:
            print(f"边际收益顾问：{advisory['reason']}")
    except (OSError, ValueError):
        print("  （效率卡片持久化失败，仅打印）")
        advisory = marginal_return_advisor(
            {"intelligence_history": [record]},
        )
        record["marginal_return"] = advisory
    return record

def marginal_return_advisor(state: dict, *, window: int = 3) -> dict:
    """消费 intelligence_history 的边际收益顾问：继续 / 换接口面 / 建议停止。

    固定阈值（50 轮/3 轮无进展/4h）不看成本趋势。本顾问按滚动窗口对比
    计命产出：近窗归零 → 建议停止或换面；近窗显著衰减（≤前窗 1/2 且前窗
    有产出）→ 建议换接口面；样本不足 → 观望。只输出建议，不做决策——
    最终停止仍由固定阈值与人工把关。
    """
    history = [
        item for item in (state.get("intelligence_history") or [])
        if isinstance(item, dict) and isinstance(item.get("efficiency"), dict)
    ]
    credited_series = [int(item["efficiency"].get("credited", 0) or 0) for item in history]
    seconds_series = [
        float(item["efficiency"].get("duration_seconds", 0.0) or 0.0)
        for item in history
    ]
    if len(credited_series) < window:
        return {"status": "insufficient-data", "rounds": len(credited_series),
                "recommendation": "continue", "reason": f"样本不足（<{window} 轮）"}
    recent = credited_series[-window:]
    prior = credited_series[-2 * window:-window] or [0]
    recent_sum = sum(recent)
    prior_sum = sum(prior)
    recent_seconds = sum(seconds_series[-window:])
    if recent_sum == 0:
        return {
            "status": "low-yield", "rounds": len(credited_series),
            "recommendation": "stop_or_switch_surface",
            "reason": f"近 {window} 轮计命为 0（耗时 {round(recent_seconds, 1)}s）",
            "recent_credited": recent_sum, "prior_credited": prior_sum,
        }
    if prior_sum > 0 and recent_sum * 2 <= prior_sum:
        return {
            "status": "declining", "rounds": len(credited_series),
            "recommendation": "switch_surface",
            "reason": f"边际收益衰减：近窗计命 {recent_sum} ≤ 前窗 {prior_sum} 的一半",
            "recent_credited": recent_sum, "prior_credited": prior_sum,
        }
    per_credited = (
        round(recent_seconds / recent_sum, 1) if recent_sum else None
    )
    return {
        "status": "healthy", "rounds": len(credited_series),
        "recommendation": "continue",
        "reason": f"近 {window} 轮计命 {recent_sum}（{per_credited}s/条）",
        "recent_credited": recent_sum, "prior_credited": prior_sum,
        "seconds_per_credited": per_credited,
    }
