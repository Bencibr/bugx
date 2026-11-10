#!/usr/bin/env python3
"""BugX Project Settings, Gates, and Intelligence Inspector.

Provides comprehensive inspection and modification of project configurations:
1. Gates & Safeguards: auto-fix, cloud sync, review mode, commit policy, privacy, backup.
2. Mining Intelligence: pollings, sessions, findings, lessons learned.
3. Mounted MCP Servers & Tools: parsed from .mcp.json and related config files.
4. Active & Available Skills: core BugX skills and project-level customized skills.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from core.webui.mcp_inspector import inspect_workspace_mcp


def _read_json_safe(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def resolve_project_dir_by_hash(proj_hash: str) -> Path | None:
    """Resolve a project directory by its unique hash from ~/.bugx/projects.json."""
    pfile = Path.home() / ".bugx" / "projects.json"
    if not pfile.is_file():
        return None
    try:
        data = json.loads(pfile.read_text(encoding="utf-8"))
        projects = data.get("projects", []) if isinstance(data, dict) else []
        for p in projects:
            if isinstance(p, dict) and p.get("hash") == proj_hash:
                wdir = p.get("working_dir")
                if wdir:
                    cand = Path(wdir).resolve()
                    if cand.is_dir():
                        return cand
    except Exception:
        pass
    return None


def _extract_project_profile_info(dot_bugx: Path, root: Path) -> dict[str, Any]:
    """Extract project positioning, scale attributes, engineering economics, and quality profile."""
    prof = None
    ledger_path = dot_bugx / "knowledge" / "lesson-ledger.json"
    try:
        try:
            from core.runtime import project_profile
        except ImportError:
            from core.runtime import project_profile as project_profile

        if ledger_path.is_file():
            prof = project_profile.load_project_profile(ledger_path)
        if not prof:
            # 读接口不得写盘、不得同步等 AI：实测无持久画像时 detect(allow_llm=True)
            # 会 spawn 仲裁子进程，源仓（864 文件）冷调 190s，而 /project/settings
            # 是同步 HTTP（客户端 15s 超时）。权威画像由 project init / pre 落盘，
            # 这里只在内存里取一份启发式快照，不污染 ledger。
            prof = project_profile.detect_project_profile(root, allow_llm=False, persist=False)
    except Exception:
        prof = None

    if not prof:
        return {
            "status": "not_analyzed",
            "positioning": {
                "forms": ["generic"],
                "primary_language": "unknown",
                "scale_level": "medium",
                "category": "software-system",
                "surfaces": ["cli", "api"],
            },
            "metrics": {
                "total_files": 0, "total_lines": 0, "code_lines": 0,
                "production_code_lines": 0, "test_code_lines": 0, "test_to_code_ratio": 0.0,
            },
            "languages": [],
            "engineering_economics": {
                "effort_person_months": 0.0, "estimated_schedule_months": 0.0,
                "tech_debt_days": 0.0, "tech_debt_ratio_pct": 0.0,
            },
            "quality_and_risk": {
                "health_grade": "B", "maintainability_index": 50.0,
                "avg_cyclomatic_complexity": 10.0, "avg_cognitive_complexity": 10.0,
                "duplication_percentage": 0.0, "sensitive_sinks": 0, "taint_flows": 0,
                "hardcoded_secrets": 0, "god_modules_count": 0, "top_hotspots": [],
            }
        }

    scale = prof.get("scale_estimation", {})
    metrics = prof.get("code_metrics", {})
    econ = prof.get("engineering_economics", {})
    risk = prof.get("risk_metrics", {})

    return {
        "status": "ok",
        "positioning": {
            "forms": prof.get("forms", []),
            "primary_language": scale.get("primary_language") or prof.get("primary_language", "unknown"),
            "scale_level": scale.get("scale_level", "medium"),
            "category": prof.get("category", "software-system"),
            "surfaces": prof.get("surfaces", []),
            "build_targets": prof.get("build_targets", []),
        },
        "metrics": {
            "total_files": metrics.get("total_files", 0),
            "total_lines": metrics.get("total_lines", 0),
            "code_lines": metrics.get("code_lines", 0),
            "production_code_lines": metrics.get("production_code_lines", 0),
            "test_code_lines": metrics.get("test_code_lines", 0),
            "test_to_code_ratio": round(metrics.get("test_to_code_ratio", 0.0), 3),
            "blank_lines": metrics.get("blank_lines", 0),
            "comment_lines": metrics.get("comment_lines", 0),
            "docstring_lines": metrics.get("docstring_lines", 0),
            "doc_and_config_lines": metrics.get("doc_and_config_lines", 0),
        },
        "languages": prof.get("code_languages", [])[:10],
        "engineering_economics": {
            "effort_person_months": round(econ.get("effort_person_months", 0.0), 1),
            "estimated_schedule_months": round(econ.get("estimated_schedule_months", 0.0), 1),
            "tech_debt_days": round(econ.get("tech_debt_days", 0.0), 1),
            "tech_debt_ratio_pct": round(econ.get("tech_debt_ratio_pct", 0.0), 1),
        },
        "quality_and_risk": {
            "health_grade": risk.get("health_grade", "B"),
            "maintainability_index": round(risk.get("maintainability_index", 0.0), 1),
            "avg_cyclomatic_complexity": round(risk.get("avg_cyclomatic_complexity", 0.0), 1),
            "avg_cognitive_complexity": round(risk.get("avg_cognitive_complexity", 0.0), 1),
            "duplication_percentage": round(risk.get("duplication_percentage", 0.0), 1),
            "sensitive_sinks": risk.get("total_sensitive_sinks", 0),
            "taint_flows": risk.get("total_taint_flows", 0),
            "hardcoded_secrets": risk.get("total_hardcoded_secrets", 0),
            "god_modules_count": risk.get("god_modules_count", 0),
            "circular_dependencies_count": risk.get("circular_dependencies_count", 0),
            "top_hotspots": risk.get("top_risk_hotspots", [])[:5],
        }
    }


def get_project_settings(project_root: Path | str) -> dict[str, Any]:
    """Retrieve complete settings, gates, mining, MCP, and skill info for a project."""
    root = Path(project_root).resolve()
    dot_bugx = root / ".bugx"

    # 1. Gates & Safeguards
    cfg_file = dot_bugx / "config.json"
    cfg = _read_json_safe(cfg_file)
    cloud_file = dot_bugx / "cloud-project.json"
    cloud_info = _read_json_safe(cloud_file)
    onboard_file = dot_bugx / "cloud-onboard.json"
    onboard_info = _read_json_safe(onboard_file)

    cloud_pid = str(cloud_info.get("project_id") or "")
    cloud_aid = str(cloud_info.get("account_id") or "")
    cloud_policy = cfg.get("cloud_policy") or ("authorized" if cloud_pid else "local")
    # 云端同步门禁是否开启：严格取决于策略是否允许出网同步 (authorized 或 full)
    cloud_sync_enabled = (cloud_policy not in ("local", "block"))
    cloud_bound = bool(cloud_pid)

    fix_mode = cfg.get("fix_mode", "auto")
    review_mode = cfg.get("review_mode", "auto")
    polling_policy = cfg.get("polling_policy", "auto")
    commit_policy = cfg.get("commit_policy", "round")
    privacy = cfg.get("privacy", "standard")
    backup_before_fix = cfg.get("backup_before_fix", True)

    # Model & Runtime Hyperparameters (REQ-03 / DOC-255)
    raw_mc = cfg.get("model_config") if isinstance(cfg.get("model_config"), dict) else {}
    model_config = {
        "default_host": str(raw_mc.get("default_host") or cfg.get("default_host") or "codex"),
        "default_model": str(raw_mc.get("default_model") or cfg.get("default_model") or "codex (OpenAI Luna6)"),
        "temperature": float(raw_mc.get("temperature", cfg.get("temperature", 0.2))),
        "top_p": float(raw_mc.get("top_p", cfg.get("top_p", 0.95))),
        "max_tokens": int(raw_mc.get("max_tokens", cfg.get("max_tokens", 16384))),
        "reasoning_effort": str(raw_mc.get("reasoning_effort") or cfg.get("reasoning_effort") or "medium"),
        "worker_concurrency": int(raw_mc.get("worker_concurrency", cfg.get("worker_concurrency", 4))),
        "high_risk_write_approval": bool(raw_mc.get("high_risk_write_approval", cfg.get("high_risk_write_approval", False))),
        "auto_mount_recommended_capabilities": bool(
            raw_mc.get("auto_mount_recommended_capabilities", cfg.get("auto_mount_recommended_capabilities", True))
        ),
    }

    gates = {
        # 五大核心启动与自愈门禁 (DOC-167 / DES-11)
        "cloud_sync_enabled": cloud_sync_enabled,
        "cloud_policy": cloud_policy,  # 1. 云端同步门禁: 'authorized', 'local', 'full', 'block'
        "cloud_bound": cloud_bound,
        "review_mode": review_mode,  # 2. 审查方式门禁: 'auto', 'interactive', 'off'
        "polling_policy": polling_policy,  # 3. 轮询策略门禁: 'auto', 'manual', 'off'
        "auto_fix_code": (fix_mode == "auto"),  # 严格自动合入补丁
        "fix_mode": fix_mode,  # 4. 修复模式门禁: 'auto', 'confirm', 'off'
        "commit_policy": commit_policy,  # 5. 提交策略门禁: 'round', 'task', 'off'

        # 辅助安全与边界配置
        "cloud_project_id": cloud_pid or None,
        "cloud_account_id": cloud_aid or None,
        "cloud_onboard_state": onboard_info.get("state", "local" if not cloud_pid else "done"),
        "privacy": privacy,  # 'standard', 'strict'
        "backup_before_fix": backup_before_fix,
        "high_risk_write_approval": model_config["high_risk_write_approval"],
        "auto_mount_recommended_capabilities": model_config["auto_mount_recommended_capabilities"],
    }

    # 2. Mining Intelligence, Strategy, Progress & Rounds
    pollings_dir = dot_bugx / "pollings"
    pollings = []
    if pollings_dir.is_dir():
        pollings = sorted([p for p in pollings_dir.iterdir() if p.name.startswith("polling-")])

    sessions_dir = dot_bugx / "sessions"
    sessions_cnt = 0
    if sessions_dir.is_dir():
        sessions_cnt = len([s for s in sessions_dir.iterdir() if not s.name.startswith(".")])

    ledger_file = dot_bugx / "knowledge" / "lesson-ledger.json"
    lessons_data = _read_json_safe(ledger_file)
    lessons_cnt = len(lessons_data.get("lessons", []))

    cur_file = dot_bugx / "current.json"
    cur_data = _read_json_safe(cur_file)
    current_polling_id = cur_data.get("polling_id") or (pollings[-1].name if pollings else None)
    current_polling_no = cur_data.get("polling_no") or len(pollings)

    rounds_history: list[dict[str, Any]] = []
    latest_strategy_obj: dict[str, Any] | None = None
    latest_runtime: dict[str, Any] = {}
    latest_supervisor: dict[str, Any] = {}

    findings_summary = {
        "total": 0,
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "patches_generated": 0,
    }

    latest_polling_name = pollings[-1].name if pollings else None
    latest_polling_time = None

    for p in pollings:
        p_state_file = p / "state.json"
        p_state = _read_json_safe(p_state_file)
        p_man_file = p / "manifest.json"
        p_man = _read_json_safe(p_man_file)

        status_str = p_state.get("state") or "unknown"
        elapsed = float(p_state.get("total_elapsed_seconds") or 0.0)
        created_at_raw = str(p_man.get("created_at") or "")
        created_at_fmt = created_at_raw[:19].replace("T", " ") if created_at_raw else ""
        round_findings_cnt = 0

        lcs = sorted((p / "lifecycles").glob("lifecycle-*")) if (p / "lifecycles").is_dir() else []
        for lc in lcs:
            rt_file = lc / "state" / "runtime.json"
            if rt_file.is_file():
                rt = _read_json_safe(rt_file)
                if rt.get("audit_strategy"):
                    latest_strategy_obj = rt["audit_strategy"]
                if rt.get("state"):
                    status_str = rt["state"]
                if rt.get("total_elapsed_seconds"):
                    elapsed = max(elapsed, float(rt["total_elapsed_seconds"]))
                latest_runtime = rt

            sv_file = lc / "state" / "supervisor.json"
            if sv_file.is_file():
                sv = _read_json_safe(sv_file)
                f_list = sv.get("findings") or []
                round_findings_cnt = max(round_findings_cnt, len(f_list))
                latest_supervisor = sv

        # Format elapsed string
        if elapsed < 60:
            elapsed_disp = f"{round(elapsed, 1)}s"
        elif elapsed < 3600:
            elapsed_disp = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
        else:
            elapsed_disp = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"

        rounds_history.append({
            "polling_id": p.name,
            "polling_no": p_man.get("polling_no") or len(rounds_history) + 1,
            "state": status_str,
            "created_at": created_at_fmt,
            "elapsed_seconds": round(elapsed, 1),
            "elapsed_display": elapsed_disp,
            "findings_count": round_findings_cnt,
        })

    # Latest polling time
    if pollings:
        latest_dir = pollings[-1]
        try:
            mtime = latest_dir.stat().st_mtime
            latest_polling_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        except Exception:
            pass

        # Inspect findings or reports from latest polling
        report_candidates = [
            latest_dir / "report.json",
            latest_dir / "findings.json",
            latest_dir / "summary.json",
        ]
        for rc in report_candidates:
            if rc.is_file():
                rep = _read_json_safe(rc)
                findings = rep.get("findings") or rep.get("items") or []
                if isinstance(findings, list):
                    findings_summary["total"] = len(findings)
                    for f in findings:
                        if isinstance(f, dict):
                            sev = str(f.get("severity", "")).lower()
                            if sev in findings_summary:
                                findings_summary[sev] += 1
                if rep.get("patches_count") is not None:
                    findings_summary["patches_generated"] = rep["patches_count"]
                break

    # If supervisor has findings, supplement summary
    sv_findings = latest_supervisor.get("findings") or []
    if sv_findings and findings_summary["total"] == 0:
        findings_summary["total"] = len(sv_findings)
        for f in sv_findings:
            if isinstance(f, dict):
                sev = str(f.get("severity", "")).lower()
                if sev in findings_summary:
                    findings_summary[sev] += 1

    # Check patches in .bugx root
    patches = list(dot_bugx.glob("*.patch"))
    if patches:
        findings_summary["patches_generated"] = max(findings_summary["patches_generated"], len(patches))

    # Coverage & Progress (REQ-07: Strategy-Group Waffle Grid + Progress Bar)
    cov_dict = latest_supervisor.get("coverage") or {}
    total_units = len(cov_dict)
    covered_units = sum(1 for v in cov_dict.values() if v in ("covered", "passed", "done"))
    pending_units = sum(1 for v in cov_dict.values() if v == "pending")
    coverage_pct = min(100.0, max(0.0, round((covered_units / total_units * 100), 1))) if total_units > 0 else 0.0

    run_status = (latest_runtime.get("state") or latest_supervisor.get("status") or ("completed" if pollings else "idle")).lower()
    
    # Calculate Phase & Phase Index
    if not pollings:
        current_phase = "就绪待命 (尚未开展挖掘)"
        phase_index = 0
    elif run_status in ("running", "active"):
        if covered_units == 0:
            current_phase = "Phase 1: 攻击面拓扑探查与静态规则匹配"
            phase_index = 1
        elif covered_units > 0 and findings_summary["patches_generated"] == 0:
            current_phase = "Phase 2: 动态漏洞验证与差异比对"
            phase_index = 2
        else:
            current_phase = "Phase 3: 防御性补丁自动合成与回归"
            phase_index = 3
    elif run_status == "completed":
        current_phase = "Phase 4: 本轮挖掘已收敛归档"
        phase_index = 4
    elif run_status == "budget_exhausted":
        current_phase = "预算上限已达成 (已封板)"
        phase_index = 4
    elif run_status == "aborted":
        current_phase = "前序挖掘中断 (待自愈重启)"
        phase_index = 0
    else:
        current_phase = f"状态: {run_status}"
        phase_index = 1

    # Assemble Strategy
    strat_cls = latest_strategy_obj.get("classification", {}) if latest_strategy_obj else {}
    strat_scale = latest_strategy_obj.get("scale_estimation", {}) if latest_strategy_obj else {}
    strat_risk = latest_strategy_obj.get("risk_metrics", {}) if latest_strategy_obj else {}

    rec_conc = int(strat_scale.get("recommended_concurrency") or model_config["worker_concurrency"] or 4)
    rec_rounds = int(strat_scale.get("recommended_rounds") or 5)

    # Build strategy task groups for Waffle Grid (REQ-07: 小方块数严格对应挖掘策略测算出的分组数)
    strategy_groups = _build_strategy_groups(
        root=root,
        latest_supervisor=latest_supervisor,
        latest_strategy_obj=latest_strategy_obj,
        sv_findings=sv_findings,
        rec_concurrency=rec_conc,
        rec_rounds=rec_rounds,
    )
    total_groups = len(strategy_groups)
    done_groups = sum(
        1 for g in strategy_groups if g["status"] in ("covered_clean", "covered_suspect", "covered_vulnerable")
    )
    running_groups = sum(1 for g in strategy_groups if g["status"] == "running")
    pending_groups = sum(1 for g in strategy_groups if g["status"] == "pending")
    group_pct = min(100.0, max(0.0, round(done_groups / total_groups * 100, 1))) if total_groups > 0 else 0.0
    if total_units == 0 and total_groups > 0:
        total_units = total_groups
        covered_units = done_groups
        pending_units = pending_groups
        coverage_pct = group_pct

    strategy_info = {
        "polling_policy": polling_policy,
        "review_mode": review_mode,
        "fix_mode": fix_mode,
        "languages": strat_cls.get("languages", ["python" if (root / "setup.py").exists() or list(root.glob("*.py")) else "generic"]),
        "surfaces": strat_cls.get("surfaces", ["api", "auth", "cli", "file", "network"]),
        "check_profiles": latest_strategy_obj.get("check_profiles", ["api-contract", "auth-flow", "secret-boundary", "state-transition", "web-input"]) if latest_strategy_obj else ["sast-scanner", "differential-probe", "regression-gate"],
        "recommended_concurrency": rec_conc,
        "recommended_rounds": rec_rounds,
        "calculated_groups_count": total_groups,
        "health_grade": strat_risk.get("health_grade", "B"),
        "top_hotspots": strat_risk.get("top_risk_hotspots", [])[:5],
    }

    # Reverse history so newest is first
    rounds_history_rev = list(reversed(rounds_history))
    trend_series = sorted(rounds_history, key=lambda x: x["polling_no"])

    # 构造动态雷达图 6 维攻防矩阵与评分
    surfaces_detected = set(strat_cls.get("surfaces") or ["api", "auth", "cli", "file", "network"])
    radar_dimensions = [
        {"key": "api", "label": "API 接口 (API)", "score": 88 if "api" in surfaces_detected else 35},
        {"key": "auth", "label": "认证权限 (Auth)", "score": 82 if "auth" in surfaces_detected else 30},
        {"key": "file", "label": "文件系统 (File)", "score": 78 if "file" in surfaces_detected else 40},
        {"key": "ui", "label": "前端交互 (UI)", "score": 75 if "ui" in surfaces_detected else 25},
        {"key": "storage", "label": "数据存储 (DB)", "score": 80 if ("storage" in surfaces_detected or "db" in surfaces_detected) else 35},
        {"key": "queue", "label": "消息任务 (Queue)", "score": 65 if "queue" in surfaces_detected else 20},
    ]

    pipeline_steps = [
        {"id": 1, "title": "架构与攻击面扫描", "subtitle": "AST & Surface Probe", "status": "done" if phase_index > 1 else ("active" if phase_index == 1 else "pending")},
        {"id": 2, "title": "漏洞挖掘与差异验证", "subtitle": "Vuln Hunt & Diff", "status": "done" if phase_index > 2 else ("active" if phase_index == 2 else "pending")},
        {"id": 3, "title": "防御补丁自动合成", "subtitle": "Patch Synthesis", "status": "done" if phase_index > 3 else ("active" if phase_index == 3 else "pending")},
        {"id": 4, "title": "沙箱双轨回归验证", "subtitle": "Sandbox Dual-Verification", "status": "done" if phase_index >= 4 else "pending"},
    ]

    mining = {
        # 概览数据
        "pollings_count": len(pollings),
        "sessions_count": sessions_cnt if sessions_cnt > 0 else (1 if len(pollings) > 0 else 0),
        "lessons_learned": lessons_cnt,
        "latest_polling": latest_polling_name,
        "latest_polling_time": latest_polling_time,
        "current_polling_id": current_polling_id,
        "current_polling_no": current_polling_no,
        "total_rounds": len(pollings),

        # 漏洞产出
        "findings": findings_summary,

        # 挖掘进度与管道 (含策略分组小方块矩阵 strategy_groups)
        "progress": {
            "status": run_status,
            "current_phase": current_phase,
            "phase_index": phase_index,
            "pipeline_steps": pipeline_steps,
            "coverage_total": total_units,
            "coverage_covered": covered_units,
            "coverage_pending": pending_units,
            "coverage_pct": coverage_pct,
            "groups_total": total_groups,
            "groups_completed": done_groups,
            "groups_running": running_groups,
            "groups_pending": pending_groups,
            "groups_pct": group_pct,
            "strategy_groups_total": total_groups,
            "strategy_groups_done": done_groups,
            "strategy_groups": strategy_groups,
            "total_elapsed_seconds": rounds_history_rev[0]["elapsed_seconds"] if rounds_history_rev else 0.0,
            "elapsed_display": rounds_history_rev[0]["elapsed_display"] if rounds_history_rev else "0s",
            "tokens_consumed": latest_supervisor.get("tokens_consumed") or 0,
            "tool_calls": latest_supervisor.get("tool_calls") or 0,
            "cost_units": latest_supervisor.get("cost_units") or 0.0,
        },

        # 挖掘策略
        "strategy": strategy_info,

        # 动态图表与时序数据源
        "chart_data": {
            "trend_series": trend_series,
            "radar_dimensions": radar_dimensions,
            "has_rounds": len(trend_series) > 0,
            "max_findings": max([r["findings_count"] for r in trend_series], default=0),
            "max_elapsed": max([r["elapsed_seconds"] for r in trend_series], default=0.0),
        },

        # 历史轮次清单
        "rounds_history": rounds_history_rev,
    }

    # 3. Project Positioning & Attributes Profile (DOC-057 / Analyze Engine)
    profile_info = _extract_project_profile_info(dot_bugx, root)

    # 4. Mounted MCP Servers & Tools
    mcp_report = inspect_workspace_mcp(root)
    mcp_servers = mcp_report.get("servers", [])
    total_tools = sum(s.get("tools", 0) for s in mcp_servers)
    mcps = {
        "total_servers": len(mcp_servers),
        "total_tools": total_tools,
        "servers": mcp_servers,
    }

    # 5. Active & Available Skills (REQ-09: 支持项目级启停与挂载)
    disabled_skills = set(cfg.get("disabled_skills") or [])
    mounted_skills = list(cfg.get("mounted_skills") or [])
    skills_list: list[dict[str, Any]] = [
        {"name": "audit", "command": "/audit", "description": "BugX 一键全息攻防渗透审计与双轨报告", "type": "core", "status": "disabled" if "audit" in disabled_skills else "enabled"},
        {"name": "review", "command": "/review", "description": "审查当前分支改动并输出结构化 Findings", "type": "core", "status": "disabled" if "review" in disabled_skills else "enabled"},
        {"name": "mining", "command": "/mining", "description": "多语种高危攻击面勘测与自动化加固", "type": "core", "status": "disabled" if "mining" in disabled_skills else "enabled"},
        {"name": "fix", "command": "/fix", "description": "自动合成防御性源码补丁并执行沙箱回归验证", "type": "core", "status": "disabled" if "fix" in disabled_skills else "enabled"},
        {"name": "test", "command": "/test", "description": "生成 AAA 单元测试与变异杀伤力审计", "type": "core", "status": "disabled" if "test" in disabled_skills else "enabled"},
    ]
    seen_skill_names = {s["name"] for s in skills_list}

    # Domain skill packs mounted on project
    prompts_skills_dir = Path(__file__).resolve().parents[1] / "prompts" / "skills"
    for m_name in mounted_skills:
        if m_name in seen_skill_names:
            continue
        sk_md = prompts_skills_dir / m_name / "SKILL.md"
        desc = f"领域专精审计技能包 ({m_name})"
        if sk_md.is_file():
            try:
                for line in sk_md.read_text(encoding="utf-8").splitlines()[:15]:
                    if line.startswith("description:"):
                        desc = line.split("description:", 1)[1].strip().strip("'\"")
                        break
            except Exception:
                pass
        skills_list.append({
            "name": m_name,
            "command": f"/{m_name}",
            "description": desc,
            "type": "domain",
            "status": "disabled" if m_name in disabled_skills else "enabled",
        })
        seen_skill_names.add(m_name)

    custom_skills_dir = root / ".agents" / "skills"
    if custom_skills_dir.is_dir():
        for sk in sorted(custom_skills_dir.iterdir()):
            if sk.is_dir() and (sk / "SKILL.md").is_file() and sk.name not in seen_skill_names:
                desc = f"项目定制技能 ({sk.name})"
                try:
                    for line in (sk / "SKILL.md").read_text(encoding="utf-8").splitlines()[:12]:
                        if line.startswith("description:"):
                            desc = line.split("description:", 1)[1].strip().strip("'\"")
                            break
                except Exception:
                    pass
                skills_list.append({
                    "name": sk.name,
                    "command": f"/{sk.name}",
                    "description": desc,
                    "type": "project",
                    "status": "disabled" if sk.name in disabled_skills else "enabled",
                })
                seen_skill_names.add(sk.name)

    skills = {
        "total_skills": len(skills_list),
        "skills": skills_list,
    }

    import hashlib
    project_hash = f"{root.name}-{hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:8]}"

    is_archived = bool(cfg.get("archived", False))
    try:
        pfile = Path.home() / ".bugx" / "projects.json"
        if pfile.is_file():
            raw_p = json.loads(pfile.read_text(encoding="utf-8"))
            for item in (raw_p.get("projects") or []):
                if isinstance(item, dict) and item.get("hash") == project_hash:
                    is_archived = bool(item.get("archived", False))
                    break
    except Exception:
        pass

    return {
        "project_hash": project_hash,
        "name": root.name,
        "working_dir": str(root),
        "archived": is_archived,
        "is_active_workspace": True,
        "gates": gates,
        "model_config": model_config,
        "mining": mining,
        "profile": profile_info,
        "mcps": mcps,
        "skills": skills,
        "updated_at": int(time.time() * 1000),
    }


def _build_strategy_groups(
    *,
    root: Path,
    latest_supervisor: dict[str, Any],
    latest_strategy_obj: dict[str, Any] | None,
    sv_findings: list[Any],
    rec_concurrency: int,
    rec_rounds: int,
) -> list[dict[str, Any]]:
    """Construct strategy-calculated task group blocks for the Waffle Grid (REQ-07).

    Priority:
    1. Explicit graph_state.tasks or tasks in supervisor.json (exact strategy task groups).
    2. Partitioning supervisor coverage units into the strategy-calculated group count.
    3. Cold-start strategy group partitioning from discovered project source modules.
    """
    # Map findings to file/unit paths
    findings_by_unit: dict[str, list[dict[str, Any]]] = {}
    for f in sv_findings:
        if not isinstance(f, dict):
            continue
        f_file = str(f.get("file") or f.get("path") or f.get("unit_id") or f.get("location") or "").strip()
        if f_file:
            findings_by_unit.setdefault(f_file, []).append(f)

    def _status_for_units(unit_ids: list[str], raw_state: str, cov_map: dict[str, Any]) -> tuple[str, int, int, int]:
        f_cnt = 0
        crit_cnt = 0
        for u in unit_ids:
            for item in findings_by_unit.get(u, []):
                f_cnt += 1
                if str(item.get("severity", "")).lower() in ("critical", "high", "p0", "p1"):
                    crit_cnt += 1
            # Also match by basename/prefix
            for k, flist in findings_by_unit.items():
                if k != u and (k.startswith(u) or u.startswith(k)):
                    f_cnt += len(flist)
                    crit_cnt += sum(
                        1 for it in flist if str(it.get("severity", "")).lower() in ("critical", "high", "p0", "p1")
                    )
        cov_done = sum(
            1 for u in unit_ids if str(cov_map.get(u, "")).lower() in ("covered", "passed", "done")
        )
        st_lower = raw_state.lower()
        if st_lower in ("claimed", "running", "in_progress", "active"):
            return "running", cov_done, f_cnt, crit_cnt
        if st_lower in ("completed", "covered", "passed", "done") or (unit_ids and cov_done == len(unit_ids)):
            if crit_cnt > 0:
                return "covered_vulnerable", cov_done or len(unit_ids), f_cnt, crit_cnt
            if f_cnt > 0:
                return "covered_suspect", cov_done or len(unit_ids), f_cnt, crit_cnt
            return "covered_clean", cov_done or len(unit_ids), f_cnt, crit_cnt
        return "pending", cov_done, f_cnt, crit_cnt

    cov_dict = latest_supervisor.get("coverage") if isinstance(latest_supervisor.get("coverage"), dict) else {}
    graph_state = latest_supervisor.get("graph_state") if isinstance(latest_supervisor.get("graph_state"), dict) else {}
    raw_tasks = graph_state.get("tasks") or latest_supervisor.get("tasks") or {}

    groups: list[dict[str, Any]] = []
    if isinstance(raw_tasks, dict) and raw_tasks:
        for idx, (tid, tval) in enumerate(sorted(raw_tasks.items()), 1):
            if not isinstance(tval, dict):
                continue
            u_ids = [str(x) for x in (tval.get("source_unit_ids") or [tval.get("subject_id") or tid]) if x]
            st, cov_cnt, f_cnt, crit_cnt = _status_for_units(u_ids, str(tval.get("state", "queued")), cov_dict)
            groups.append({
                "group_id": str(tid),
                "group_no": idx,
                "title": str(tval.get("subject_id") or tid),
                "stage": str(tval.get("stage") or "audit"),
                "status": st,
                "state": st,
                "files": u_ids,
                "file_count": len(u_ids),
                "covered_files": cov_cnt,
                "total_units": len(u_ids),
                "covered_units": cov_cnt,
                "findings_count": f_cnt,
                "critical_high_count": crit_cnt,
                "worker_id": tval.get("claim_worker_id"),
            })
        if groups:
            return groups

    # 2. If coverage units exist without explicit graph tasks, partition by strategy group count
    target_groups = max(1, min(32, int((latest_strategy_obj or {}).get("task_groups_count") or (rec_concurrency * max(1, min(rec_rounds, 4))))))
    if cov_dict:
        sorted_units = sorted(str(k) for k in cov_dict.keys())
        if len(sorted_units) <= target_groups:
            chunks = [[u] for u in sorted_units]
        else:
            chunk_size = max(1, (len(sorted_units) + target_groups - 1) // target_groups)
            chunks = [sorted_units[i:i + chunk_size] for i in range(0, len(sorted_units), chunk_size)]
        for idx, chunk in enumerate(chunks, 1):
            statuses = [str(cov_dict.get(u, "pending")).lower() for u in chunk]
            if any(s in ("running", "claimed", "in_progress") for s in statuses):
                raw_st = "running"
            elif all(s in ("covered", "passed", "done") for s in statuses):
                raw_st = "covered"
            elif any(s in ("covered", "passed", "done") for s in statuses):
                raw_st = "running"
            else:
                raw_st = "pending"
            st, cov_cnt, f_cnt, crit_cnt = _status_for_units(chunk, raw_st, cov_dict)
            lead = chunk[0] if len(chunk) == 1 else f"策略分组 #{idx} ({Path(chunk[0]).parent.as_posix() or chunk[0]})"
            groups.append({
                "group_id": f"group-{idx:02d}",
                "group_no": idx,
                "title": lead,
                "stage": "audit",
                "status": st,
                "state": st,
                "files": chunk,
                "file_count": len(chunk),
                "covered_files": cov_cnt,
                "total_units": len(chunk),
                "covered_units": cov_cnt,
                "findings_count": f_cnt,
                "critical_high_count": crit_cnt,
                "worker_id": None,
            })
        return groups

    # 3. Cold-start fallback: partition discovered source files into strategy-calculated task groups
    discovered_files: list[str] = []
    skip_dirs = {".git", ".bugx", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".idea", ".vscode"}
    code_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".rb", ".php", ".cs", ".swift", ".kt"}
    try:
        for item in sorted(root.iterdir()):
            if item.name in skip_dirs or item.name.startswith("."):
                continue
            if item.is_file() and item.suffix.lower() in code_exts:
                discovered_files.append(item.relative_to(root).as_posix())
            elif item.is_dir():
                for sub in sorted(item.rglob("*")):
                    if any(part in skip_dirs for part in sub.parts):
                        continue
                    if sub.is_file() and sub.suffix.lower() in code_exts:
                        discovered_files.append(sub.relative_to(root).as_posix())
                        if len(discovered_files) >= 160:
                            break
            if len(discovered_files) >= 160:
                break
    except Exception:
        pass

    if not discovered_files:
        discovered_files = [f"sector-{i:02d}" for i in range(1, target_groups + 1)]

    actual_groups = min(target_groups, len(discovered_files))
    chunk_size = max(1, (len(discovered_files) + actual_groups - 1) // actual_groups)
    chunks = [discovered_files[i:i + chunk_size] for i in range(0, len(discovered_files), chunk_size)][:actual_groups]
    for idx, chunk in enumerate(chunks, 1):
        prefix = Path(chunk[0]).parts[0] if Path(chunk[0]).parts else chunk[0]
        groups.append({
            "group_id": f"group-{idx:02d}",
            "group_no": idx,
            "title": f"策略分组 #{idx} ({prefix})",
            "stage": "planned",
            "status": "pending",
            "state": "pending",
            "files": chunk,
            "file_count": len(chunk),
            "covered_files": 0,
            "total_units": len(chunk),
            "covered_units": 0,
            "findings_count": 0,
            "critical_high_count": 0,
            "worker_id": None,
        })
    return groups


def update_project_settings(project_root: Path | str, updates: dict[str, Any]) -> dict[str, Any]:
    """Update gates, model hyperparameters, and configuration for a project, persisting to .bugx/config.json."""
    root = Path(project_root).resolve()
    dot_bugx = root / ".bugx"
    dot_bugx.mkdir(mode=0o700, exist_ok=True)
    cfg_file = dot_bugx / "config.json"
    cfg = _read_json_safe(cfg_file)

    # Allowed gates updates
    # 1. 修复模式门禁 (以显式 fix_mode 为准，兼顾 auto_fix_code 兼容)
    if "fix_mode" in updates:
        mode = str(updates["fix_mode"]).lower()
        if mode not in ("auto", "confirm", "off"):
            raise ValueError(f"Invalid fix_mode: {mode}")
        cfg["fix_mode"] = mode
    elif "auto_fix_code" in updates:
        auto_fix = bool(updates["auto_fix_code"])
        cfg["fix_mode"] = "auto" if auto_fix else "off"

    # 2. 云端同步门禁 (以显式 cloud_policy 为准，兼顾 cloud_sync_enabled 兼容)
    if "cloud_policy" in updates:
        cp = str(updates["cloud_policy"]).lower()
        if cp not in ("local", "authorized", "full", "block"):
            raise ValueError(f"Invalid cloud_policy: {cp}")
        cfg["cloud_policy"] = cp
    elif "cloud_sync_enabled" in updates:
        sync_enabled = bool(updates["cloud_sync_enabled"])
        if not sync_enabled:
            cfg["cloud_policy"] = "local"
        else:
            cfg["cloud_policy"] = "authorized"

    if "commit_policy" in updates:
        cp = str(updates["commit_policy"]).lower()
        if cp not in ("round", "task", "off"):
            raise ValueError(f"Invalid commit_policy: {cp}")
        cfg["commit_policy"] = cp

    if "review_mode" in updates:
        rm = str(updates["review_mode"]).lower()
        if rm not in ("auto", "interactive", "off"):
            raise ValueError(f"Invalid review_mode: {rm}")
        cfg["review_mode"] = rm

    if "polling_policy" in updates:
        pp = str(updates["polling_policy"]).lower()
        if pp not in ("auto", "manual", "off"):
            raise ValueError(f"Invalid polling_policy: {pp}")
        cfg["polling_policy"] = pp

    if "backup_before_fix" in updates:
        cfg["backup_before_fix"] = bool(updates["backup_before_fix"])

    if "privacy" in updates:
        priv = str(updates["privacy"]).lower()
        if priv not in ("standard", "strict"):
            raise ValueError(f"Invalid privacy: {priv}")
        cfg["privacy"] = priv

    # 3. Model & Runtime Hyperparameters (REQ-03 / DOC-255, strict range validation)
    mc = dict(cfg.get("model_config") or {}) if isinstance(cfg.get("model_config"), dict) else {}
    mc_input = dict(updates.get("model_config") or {}) if isinstance(updates.get("model_config"), dict) else {}
    for k in (
        "default_host",
        "default_model",
        "temperature",
        "top_p",
        "max_tokens",
        "reasoning_effort",
        "worker_concurrency",
        "high_risk_write_approval",
        "auto_mount_recommended_capabilities",
    ):
        if k in updates and k not in mc_input:
            mc_input[k] = updates[k]

    if "temperature" in mc_input:
        try:
            temp_val = float(mc_input["temperature"])
        except (TypeError, ValueError) as exc:
            raise ValueError("temperature must be a number") from exc
        if not (0.0 <= temp_val <= 2.0):
            raise ValueError(f"temperature out of range [0.0, 2.0]: {temp_val}")
        mc["temperature"] = round(temp_val, 2)

    if "top_p" in mc_input:
        try:
            top_p_val = float(mc_input["top_p"])
        except (TypeError, ValueError) as exc:
            raise ValueError("top_p must be a number") from exc
        if not (0.05 <= top_p_val <= 1.0):
            raise ValueError(f"top_p out of range [0.05, 1.0]: {top_p_val}")
        mc["top_p"] = round(top_p_val, 2)

    if "max_tokens" in mc_input:
        try:
            mt_val = int(mc_input["max_tokens"])
        except (TypeError, ValueError) as exc:
            raise ValueError("max_tokens must be an integer") from exc
        if not (256 <= mt_val <= 256000):
            raise ValueError(f"max_tokens out of range [256, 256000]: {mt_val}")
        mc["max_tokens"] = mt_val

    if "worker_concurrency" in mc_input:
        try:
            wc_val = int(mc_input["worker_concurrency"])
        except (TypeError, ValueError) as exc:
            raise ValueError("worker_concurrency must be an integer") from exc
        if not (1 <= wc_val <= 8):
            raise ValueError(f"worker_concurrency out of range [1, 8]: {wc_val}")
        mc["worker_concurrency"] = wc_val

    if "reasoning_effort" in mc_input:
        re_val = str(mc_input["reasoning_effort"]).lower().strip()
        if re_val not in ("low", "medium", "high", "xhigh"):
            raise ValueError(f"Invalid reasoning_effort: {re_val}")
        mc["reasoning_effort"] = re_val

    if "default_host" in mc_input and mc_input["default_host"]:
        mc["default_host"] = str(mc_input["default_host"]).strip()

    if "default_model" in mc_input and mc_input["default_model"]:
        mc["default_model"] = str(mc_input["default_model"]).strip()

    if "high_risk_write_approval" in mc_input:
        mc["high_risk_write_approval"] = bool(mc_input["high_risk_write_approval"])

    if "auto_mount_recommended_capabilities" in mc_input:
        mc["auto_mount_recommended_capabilities"] = bool(mc_input["auto_mount_recommended_capabilities"])

    if mc:
        cfg["model_config"] = mc

    if "scope_decisions" in updates and isinstance(updates["scope_decisions"], dict):
        existing = cfg.get("scope_decisions", {})
        if isinstance(existing, dict):
            existing.update(updates["scope_decisions"])
            cfg["scope_decisions"] = existing
        else:
            cfg["scope_decisions"] = updates["scope_decisions"]

    _write_json_atomic(cfg_file, cfg)
    return get_project_settings(root)


def get_scope_decisions(project_root: Path | str) -> dict[str, Any]:
    """获取项目中人机交互持久化的需求与领域范围决策 (DOC-245)。"""
    root = Path(project_root).resolve()
    cfg_file = root / ".bugx" / "config.json"
    cfg = _read_json_safe(cfg_file)
    decisions = cfg.get("scope_decisions", {})
    return decisions if isinstance(decisions, dict) else {}


def save_scope_decision(
    project_root: Path | str,
    domain: str,
    decision: str,
    detail: str = "",
) -> None:
    """原子持久化单条领域范围人机交互决策，实现跨会话防重复询问 (DOC-245)。"""
    root = Path(project_root).resolve()
    cfg_file = root / ".bugx" / "config.json"
    cfg = _read_json_safe(cfg_file)
    decisions = cfg.get("scope_decisions", {})
    if not isinstance(decisions, dict):
        decisions = {}
    decisions[domain] = {
        "decision": decision,
        "detail": detail,
        "updated_at": int(time.time()),
    }
    cfg["scope_decisions"] = decisions
    _write_json_atomic(cfg_file, cfg)
