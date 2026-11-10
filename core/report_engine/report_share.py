#!/usr/bin/env python3
"""运行结论分享流程：把一次已完成的测试运行，汇总成一条匿名上报到后端。

服务为人——「测试完成 → 提示是否分享 → 一次运行一条汇总」。本模块自包含、可测：
  - detect_project_profile：知己阶段画像（项目类别 + 语言清单，按文件探测）。
  - build_environment_info：本机环境（os/arch/python/host/machine-id）。
  - collect_run_summary：从生命周期产物读取汇总字段，构造 run_id。
  - decide_share：--share / --no-share / 交互询问。
  - share_run：收集 → 判定 → 经 ReportClient 匿名 POST /api/v1/agent/runs。

产品边界：当前版本只允许私有多设备云同步，不允许公共运行报告发布。
公开分享入口 fail-closed：任何 "public" 可见性请求都不会发网，只有
visibility="private" 才允许上报；本地 preview 与私有同步行为保持不变。

所有读取都容错：缺文件/缺字段则省略，不抛异常、不阻塞本地主流程。
"""

from __future__ import annotations

import os
import platform
import json
import time
from pathlib import Path
from typing import Any

from core.runtime.project_profile import detect_project_profile as _detect_project_profile

# 保证同目录 import（core/report_client.py）
try:
    from core.report_engine.report_client import (
        build_run_conclusion_payload, compute_run_id, preview_report_payload,
        ReportClient, sanitize_anonymous_payload,
    )
except ImportError:  # pragma: no cover - 直接运行时的兜底
    try:
        from core.report_engine.report_client import (  # type: ignore
            build_run_conclusion_payload, compute_run_id, preview_report_payload,
            ReportClient, sanitize_anonymous_payload,
        )
    except ImportError:
        build_run_conclusion_payload = compute_run_id = None  # type: ignore
        preview_report_payload = sanitize_anonymous_payload = None  # type: ignore
        ReportClient = None  # type: ignore

def detect_project_profile(project_root: Path, *, walk_limit: int = 2000) -> dict[str, Any]:
    """Use the single bounded detector shared with startup preparation."""
    return _detect_project_profile(Path(project_root), file_limit=walk_limit)


def _load_persisted_project_profile(project_root: Path) -> dict[str, Any] | None:
    """Read the startup-produced profile; legacy report-only runs may return None."""
    try:
        from core.runtime.project_profile import load_project_profile
        from core.bugx_paths import project_knowledge_root
        return load_project_profile(
            project_knowledge_root(Path(project_root)) / "lesson-ledger.json"
        )
    except (ImportError, OSError, ValueError, TypeError, AttributeError):
        return None


def build_environment_info() -> dict[str, Any]:
    """仅返回不包含稳定设备标识的环境元数据。"""
    info: dict[str, Any] = {
        "os": platform.system() or "",
        "arch": platform.machine() or "",
        "python": platform.python_version() or "",
    }
    return info


def decide_share(flag: str | None) -> bool | None:
    """决定是否分享：--share=True / --no-share=False / 未指定=None（交由调用方询问）。"""
    if flag is None:
        return None
    return flag.strip().lower() in {"1", "yes", "true", "y", "share", "on"}


def _read_json(path: Path) -> dict | None:
    try:
        data = path.read_text(encoding="utf-8")
        return json.loads(data)
    except (OSError, ValueError, TypeError):
        return None


def collect_run_summary(
    lifecycle_root: Path,
    project_root: Path | None = None,
    *,
    polling_id: str = "",
    lifecycle_id: str = "",
    seed: str = "",
    profile: dict | None = None,
    visibility: str = "private",
) -> dict[str, Any]:
    """从生命周期产物收集汇总字段，构造 run_id + environment。

    容错：找不到的文件/字段一律省略（None 不写入），绝不为贴数据编造。
    lifecycle_root：含 findings.json 与 state/ 的目录。
    """
    lifecycle_root = Path(lifecycle_root)
    summary: dict[str, Any] = {"_source": str(lifecycle_root)}

    # 运行核心指标
    findings = _read_json(lifecycle_root / "findings.json") or {}
    rt_state = _read_json(lifecycle_root / "state" / "runtime.json") or {}
    summary["run_id"] = compute_run_id(
        polling_id=polling_id, lifecycle_id=lifecycle_id, seed=seed,
    )
    summary["project_name"] = (findings.get("project_name")
                               or str((project_root or Path(".")).resolve().name))
    dur = findings.get("duration_seconds")
    if isinstance(dur, (int, float)) and dur > 0:
        summary["test_duration"] = int(dur)
    else:
        # Fallback to supervisor state, runtime state, or life state
        supervisor_state = _read_json(lifecycle_root / "state" / "supervisor.json") or {}
        sv_start = supervisor_state.get("round_started_at") or supervisor_state.get("started_at")
        sv_end = supervisor_state.get("clock_high_water")
        if isinstance(sv_start, (int, float)) and sv_start > 0:
            end_ts = sv_end if isinstance(sv_end, (int, float)) and sv_end > sv_start else time.time()
            summary["test_duration"] = max(1, int(round(end_ts - sv_start)))
        else:
            rt_elapsed = (
                rt_state.get("total_elapsed_seconds")
                or rt_state.get("session_elapsed_seconds")
                or rt_state.get("current_elapsed_seconds")
            )
            if isinstance(rt_elapsed, (int, float)) and rt_elapsed > 0:
                summary["test_duration"] = max(1, int(round(rt_elapsed)))
            elif isinstance(dur, (int, float)):
                summary["test_duration"] = int(dur)
    if "test_duration" in summary and "survival_time" not in summary:
        summary["survival_time"] = summary["test_duration"]
    f_tokens = findings.get("token_count")
    if isinstance(f_tokens, (int, float)) and f_tokens > 0:
        summary["tokens_consumed"] = int(f_tokens)
    decision = findings.get("decision_summary") or {}
    if isinstance(decision.get("confirmed"), int):
        summary["bugs_found"] = decision["confirmed"]
    elif "findings" in findings and isinstance(findings["findings"], list):
        summary["bugs_found"] = len([f for f in findings["findings"] if isinstance(f, dict) and str(f.get("lifecycle_state") or f.get("decision") or "").lower() != "rejected"])
    if "fixes_total" not in summary:
        if isinstance(decision.get("repaired"), int):
            summary["fixes_total"] = decision["repaired"]
        elif isinstance(decision.get("fixed"), int):
            summary["fixes_total"] = decision["fixed"]

    # 轮次：state/life.json 或 findings 里都可能是来源
    life = _read_json(lifecycle_root / "state" / "life.json") or {}
    rounds = life.get("rounds_completed")
    if not isinstance(rounds, int):
        rounds = life.get("round")
    if isinstance(rounds, int):
        summary["major_rounds"] = rounds
    # A lifecycle may expose the finer-grained life/cycle counters separately.
    # Do not manufacture zero when the state contains an observed value.
    for key in ("minor_rounds", "life_rounds", "cycles_completed"):
        value = life.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            summary["minor_rounds"] = value
            break
    if "minor_rounds" not in summary:
        history = life.get("history")
        if isinstance(history, list):
            summary["minor_rounds"] = len(history)
    if "minor_rounds" not in summary and isinstance(rounds, int):
        # Legacy lifecycle files have no finer-grained counter. Preserve the
        # established field shape while making observed counters authoritative.
        summary["minor_rounds"] = 0
    for key in ("life_total", "total_lives", "lives_total"):
        value = life.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            summary["life_total"] = value
            break

    # 用例总数：supervisor coverage 计数
    supervisor = _read_json(lifecycle_root / "state" / "supervisor.json") or {}
    coverage = supervisor.get("coverage")
    if isinstance(coverage, dict) and coverage:
        summary["cases_total"] = len(coverage)
        covered_states = {"covered", "passed", "complete", "completed", "verified"}
        covered = 0
        for value in coverage.values():
            state = value.get("status") if isinstance(value, dict) else value
            if str(state or "").casefold() in covered_states:
                covered += 1
        summary["cases_covered"] = covered
    if "tokens_consumed" not in summary:
        for key in ("tokens_consumed", "token_count", "total_tokens"):
            value = supervisor.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                summary["tokens_consumed"] = int(value)
                break
        if "tokens_consumed" not in summary:
            rt_tokens = (
                rt_state.get("lifecycle_token_count")
                or rt_state.get("token_count")
                or rt_state.get("tokens_consumed")
            )
            if isinstance(rt_tokens, (int, float)) and not isinstance(rt_tokens, bool) and rt_tokens > 0:
                summary["tokens_consumed"] = int(rt_tokens)
            elif isinstance(f_tokens, (int, float)) and not isinstance(f_tokens, bool):
                summary["tokens_consumed"] = int(f_tokens)

    host_candidate = (
        findings.get("host")
        or rt_state.get("host")
        or os.environ.get("BUGX_HOST")
        or "cli"
    )
    if isinstance(host_candidate, str) and host_candidate.strip():
        summary["host"] = host_candidate.strip().lower()
    else:
        summary["host"] = "cli"

    # 项目类别 + 语言：优先知己画像，缺失则现探测
    if profile is None:
        project = project_root or lifecycle_root.parent.parent
        profile = _load_persisted_project_profile(project)
        if profile is None:
            # Report-only legacy runs have no initialized knowledge ledger.
            # Keep them usable, but the normal initialized path consumes the
            # profile generated during startup rather than rescanning here.
            profile = detect_project_profile(project)
    cat = profile.get("forms") or profile.get("category")
    if isinstance(cat, list) and cat:
        summary["project_type"] = cat[0]
    langs = profile.get("languages")
    if isinstance(langs, list) and langs:
        summary["languages"] = langs
    code_metrics = profile.get("code_metrics")
    if isinstance(code_metrics, dict):
        cv = code_metrics.get("code_lines") or code_metrics.get("total_lines")
        if isinstance(cv, (int, float)) and cv > 0:
            summary["code_volume"] = int(cv)
    if "code_volume" not in summary:
        scale_lines = (profile.get("scale_estimation") or {}).get("total_lines")
        if isinstance(scale_lines, (int, float)) and scale_lines > 0:
            summary["code_volume"] = int(scale_lines)

    # 环境信息：独立字段（后端已支持 environment）
    env = build_environment_info()
    summary["environment"] = env

    if polling_id:
        summary["polling_id"] = polling_id
    if lifecycle_id:
        summary["lifecycle_id"] = lifecycle_id

    # 完整测试报告（report.md）
    report_md_path = lifecycle_root / "report.md"
    if report_md_path.is_file() and not report_md_path.is_symlink():
        try:
            md_text = report_md_path.read_text(encoding="utf-8")
            if md_text.strip():
                summary["report_markdown"] = md_text
        except (OSError, UnicodeDecodeError):
            pass

    # 机器可读结论（conclusion.json）
    conclusion = _read_json(lifecycle_root / "conclusion.json")
    if isinstance(conclusion, dict) and conclusion:
        summary["conclusion_json"] = conclusion
        c_state = str(conclusion.get("state") or "").lower()
        if c_state in {"complete", "completed", "passed", "done"}:
            summary["status"] = "completed"
            summary["result"] = "passed"
        elif c_state in {"incomplete", "aborted", "cancelled", "stopped"}:
            summary["status"] = "completed" if conclusion.get("completed_at") else "aborted"
            summary["result"] = "at_risk" if (conclusion.get("findings_total") or 0) > 0 else "incomplete"
        elif c_state in {"at_risk", "failed"}:
            summary["status"] = "completed"
            summary["result"] = "at_risk"

        if isinstance(conclusion.get("token_count"), (int, float)):
            summary["tokens_consumed"] = int(conclusion["token_count"])
        if isinstance(conclusion.get("host"), str) and conclusion["host"].strip():
            summary["host"] = conclusion["host"].strip().lower()

        if isinstance(conclusion.get("findings_total"), int):
            summary["bugs_found"] = conclusion["findings_total"]
        if isinstance(conclusion.get("fix_count"), int):
            summary["fixes_total"] = conclusion["fix_count"]
        if isinstance(conclusion.get("lesson_count"), int):
            summary["lessons_count"] = conclusion["lesson_count"]

        cov = conclusion.get("coverage")
        if isinstance(cov, dict):
            if isinstance(cov.get("total_units"), int):
                summary["cases_total"] = cov["total_units"]
            if isinstance(cov.get("covered"), int):
                summary["cases_covered"] = cov["covered"]
        r_hash = conclusion.get("report_sha256") or conclusion.get("report_hash")
        if isinstance(r_hash, str) and r_hash:
            summary["report_hash"] = r_hash

    # 完整性校验（integrity.json）
    integrity = _read_json(lifecycle_root / "integrity.json")
    if isinstance(integrity, dict) and integrity:
        summary["integrity_json"] = integrity
        if "report_hash" not in summary:
            i_hash = integrity.get("report_sha256") or integrity.get("report_hash")
            if isinstance(i_hash, str) and i_hash:
                summary["report_hash"] = i_hash

    # 断言/证据计数
    claims_path = lifecycle_root / "state" / "acceptance-evidence-receipt.json"
    if claims_path.is_file():
        claims_doc = _read_json(claims_path) or {}
        cnt = claims_doc.get("claim_count") or claims_doc.get("claims_count")
        if isinstance(cnt, int):
            summary["claims_count"] = cnt

    # 云端项目 ID 关联
    proj_dir = project_root or lifecycle_root.parent.parent
    cloud_proj = _read_json(Path(proj_dir) / ".bugx" / "cloud-project.json") or {}
    if isinstance(cloud_proj.get("project_id"), int) and cloud_proj["project_id"] > 0:
        summary["project_id"] = cloud_proj["project_id"]

    # 可见性：产品边界只允许私有同步，公开分享已禁用（fail-closed）。
    # 任何非 "private" 的输入（含 "public" 与非法值）一律降级为 "private"。
    summary["visibility"] = "private"

    return summary


def share_run(
    lifecycle_root: Path,
    project_root: Path | None = None,
    *,
    polling_id: str = "",
    lifecycle_id: str = "",
    seed: str = "",
    visibility: str = "private",
    client: Any = None,
    operation_id: str | None = None,
    redaction: str | None = None,
) -> dict[str, Any]:
    """收集汇总 → 净化 → 上报 → 返回结构化结果。失败不抛异常（本地主流程不受影响）。

    产品边界：当前版本只允许私有多设备云同步，不允许公共运行报告发布。
    公开分享入口 fail-closed：visibility 非 "private"（含 "public"、非法值、
    None）一律不调用网络 client，直接返回失败结果；只有 visibility="private"
    才发网。

    ``redaction`` 默认跟随 ``resolve_redaction_mode``（off = 原样上报，2026-11-09
    用户口径）；``standard`` 才删除机器标识键并脱敏凭据/绝对路径（TO-008-021）。
    结果只带 redaction 计数，不携带被删改内容。
    """
    if build_run_conclusion_payload is None or compute_run_id is None \
            or sanitize_anonymous_payload is None:
        return {"ok": False, "error": "report_client 未成功导入",
                "summary": {"_source": str(lifecycle_root)}}
    # 公开分享已禁用（fail-closed）：任何非 private 可见性都不发网。
    if visibility != "private":
        return {
            "ok": False,
            "error": "公开分享已禁用（产品边界：仅支持私有多设备同步）",
            "summary": {"_source": str(lifecycle_root), "visibility": "private"},
        }
    project = project_root or lifecycle_root.parent.parent
    profile = _load_persisted_project_profile(project) or detect_project_profile(project)
    summary = collect_run_summary(
        lifecycle_root, project_root,
        polling_id=polling_id, lifecycle_id=lifecycle_id, seed=seed,
        profile=profile, visibility=visibility,
    )
    payload = build_run_conclusion_payload(**{k: v for k, v in summary.items()
                                              if k in _SUMMARY_FIELDS})
    payload, redactions = sanitize_anonymous_payload(payload, redaction=redaction)
    if client is None:
        token = None
        base_url = None
        try:
            from core.cloud_sync.cloud_client import CloudCredentials, configured_base_url
            credentials = CloudCredentials.load()
            # Token is transport authentication only; it is never embedded in
            # the sanitized report payload.  An absent token keeps the share
            # endpoint anonymous for local/private mode.
            token = getattr(credentials, "api_key", None) or getattr(credentials, "token", None)
            base_url = configured_base_url()
        except (ImportError, OSError, ValueError, TypeError):
            token = None
            base_url = None
        if ReportClient is not None:
            try:
                client = ReportClient(token=token, base_url=base_url)
            except TypeError:
                client = ReportClient(token=token)
        else:
            client = None
    if client is None:
        return {"ok": False, "error": "无法构造 ReportClient", "summary": summary}
    if operation_id:
        try:
            result = client.post(payload, operation_id=operation_id)
        except TypeError:
            # Test/custom clients from before the operation-id extension may
            # not accept the optional keyword.  The deterministic run_id still
            # preserves server-side idempotence for those clients.
            result = client.post(payload)
    else:
        result = client.post(payload)
    return {
        "ok": bool(getattr(result, "ok", False)),
        "status": getattr(result, "status", None),
        "error": getattr(result, "error", None),
        "data": getattr(result, "data", None),
        "redaction_count": len(redactions),
        "summary": summary,
    }


def preview_run_report(
    lifecycle_root: Path,
    project_root: Path | None = None,
    *,
    polling_id: str = "",
    lifecycle_id: str = "",
    seed: str = "",
    visibility: str = "private",
    redaction: str | None = None,
) -> dict[str, Any] | None:
    """`bugx report preview --json` 的数据装配入口：只预览，不发送。

    复用 share_run 的收集逻辑构造负载，再经 preview_report_payload 输出
    目标 endpoint、字段类别、字节数与脱敏清单（DOC-008 §11）。``redaction``
    与实际上报同源（默认 off = 原样上报），预览必须与发网内容一致。
    """
    if build_run_conclusion_payload is None or compute_run_id is None \
            or preview_report_payload is None:
        return None
    project = project_root or lifecycle_root.parent.parent
    profile = _load_persisted_project_profile(project) or detect_project_profile(project)
    summary = collect_run_summary(
        lifecycle_root, project_root,
        polling_id=polling_id, lifecycle_id=lifecycle_id, seed=seed,
        profile=profile, visibility=visibility,
    )
    payload = build_run_conclusion_payload(**{k: v for k, v in summary.items()
                                              if k in _SUMMARY_FIELDS})
    return preview_report_payload(payload, redaction=redaction)


# build_run_conclusion_payload 允许的字段
_SUMMARY_FIELDS = {
    "run_id", "project_name", "host", "test_duration", "survival_time", "tokens_consumed",
    "major_rounds", "minor_rounds", "life_total", "bugs_found", "fixes_total",
    "modules_count", "features_count", "lessons_count", "incidents_count",
    "lessons_hit", "incidents_hit", "cases_total", "cases_covered",
    "cases_passed", "cases_failed", "cases_blocked", "claims_count",
    "status", "result", "code_volume", "project_type", "languages",
    "visibility", "environment", "report_markdown", "conclusion_json",
    "integrity_json", "artifacts_json", "report_hash", "polling_id",
    "lifecycle_id", "project_id",
}
