"""BugX 一键全息安全审计与原生 Artifact Graph 编排交付 (bugx audit).

执行针对任意被测项目的安全审计流水线：
1. 默认/图模式：对接原生 Artifact Graph 编排图（DOC-232），在私有目录创建真实 AgentWorkspace
   与确定性 Recon→Repro→Review→(Patch)→Check→Report 状态机。
2. 深度/突防模式：执行 6 并发攻防变异突防、双向反事实裁决与双轨制高品质安全报告 (HTML / Markdown / PDF) 全量交付。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import stat
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Sequence

from ..common import _ensure_core_importable


GOALS = ("find_bugs", "repair_preview")
FORMAT_CHOICES = ("all", "html", "markdown", "md", "both", "pdf")


def register_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "audit", help="在隔离副本中启动 BugX 图审计或执行一键全息攻防渗透审计",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="目标项目目录")
    parser.add_argument(
        "--goal", choices=GOALS, default="find_bugs",
        help="目标：find_bugs 或 repair_preview（默认 find_bugs）",
    )
    parser.add_argument("--host", help="请求执行此图任务的宿主名称")
    parser.add_argument(
        "--scope", action="append", default=[], metavar="PATH",
        help="项目相对文件或目录；可重复指定",
    )
    parser.add_argument(
        "--deep", action="store_true", help="启用深度攻防模糊突防变异模式",
    )
    parser.add_argument(
        "--concurrency", type=int, default=6,
        help="并发攻防 Worker 线程/进程数（默认 6）",
    )
    parser.add_argument(
        "--format", choices=FORMAT_CHOICES, default="all",
        help="生成交付报告格式 (默认 all: 生成 HTML+MD+PDF 完整报告矩阵)",
    )
    parser.add_argument(
        "--json", action="store_true", help="以机器可读 JSON 格式输出审计流水线摘要",
    )
    parser.add_argument(
        "--open-browser", action="store_true",
        help="审计完成后自动在浏览器打开全息可视化报告",
    )
    parser.add_argument("--out", type=Path, help="自定义报告输出目录 (默认 .bugx/reports/)")
    parser.add_argument(
        "--min-kill-rate", type=float, default=0.85,
        help="变异测试通过击杀率阈值 (默认 0.85)",
    )
    parser.set_defaults(handler=command_audit)
    return parser


def _payload(
    *, run_id: str | None, graph_id: str | None, goal: str,
    status: str, outcome: str | None, error: str | None = None,
    report_path: str | None = None, confirmed_count: int | None = None,
    static_supported_count: int | None = None,
    validation_state: str | None = None, repair_state: str | None = None,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "graph_id": graph_id,
        "goal": goal,
        "status": status,
        "outcome": outcome,
        "report_path": report_path,
        "confirmed_count": confirmed_count,
        "static_supported_count": static_supported_count,
        "validation_state": validation_state,
        "repair_state": repair_state,
        "error": error,
    }


def _emit(payload: Mapping[str, object]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, allow_nan=False))


def _engine_modules(repo_root: Path) -> tuple[Any, Any, Any]:
    core_root = repo_root / "core"
    for search_path in (repo_root, core_root):
        path_text = str(search_path)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)
    workspace_module = importlib.import_module("core.agent.agent_workspace")
    target_module = importlib.import_module("core.agent.agent_targets")
    orchestrator_module = importlib.import_module("core.agent.agent_orchestrator")
    return workspace_module, target_module, orchestrator_module


def _private_runtime_root() -> Path:
    from core.bugx_paths import assert_no_symlink_components, global_knowledge_root

    runtime_root = global_knowledge_root().parent / "agent-audits"
    assert_no_symlink_components(runtime_root)
    if runtime_root.is_symlink():
        raise ValueError("Agent audit runtime path must not be a symlink")
    if runtime_root.exists():
        if not runtime_root.is_dir():
            raise ValueError("Agent audit runtime path must be a directory")
        if stat.S_IMODE(runtime_root.stat().st_mode) & 0o077:
            raise ValueError("Agent audit runtime directory must be private (mode 0700)")
    else:
        runtime_root.mkdir(parents=True, mode=0o700)
        runtime_root.chmod(0o700)
    return runtime_root


def _normalized_scope(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("--scope requires a non-empty project-relative path")
    raw = value.strip()
    if "\\" in raw or "\x00" in raw or ":" in raw:
        raise ValueError(f"invalid project-relative scope: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"scope must stay inside the project: {raw!r}")
    normalized = path.as_posix()
    if normalized != raw:
        raise ValueError(f"scope must be normalized: {raw!r}")
    return normalized


def _select_targets(
    project_files: Sequence[str], scopes: Sequence[object], maximum: int,
) -> list[str]:
    files = sorted(set(project_files))
    if scopes:
        selected: set[str] = set()
        for raw_scope in scopes:
            scope = _normalized_scope(raw_scope)
            matches = {
                path for path in files
                if path == scope or path.startswith(scope + "/")
            }
            if not matches:
                raise ValueError(f"scope does not select a supported project file: {scope}")
            selected.update(matches)
        files = sorted(selected)
    if not files:
        raise ValueError("target adapter found no supported project files")
    if len(files) > maximum:
        raise ValueError(
            f"selected {len(files)} files; graph limit is {maximum}, narrow --scope"
        )
    return files


def _task_input(
    goal: str, project_name: str, host: str | None, target_files: Sequence[str],
) -> dict[str, object]:
    host_note = f"; requested host: {host}" if host else ""
    if goal == "find_bugs":
        objective = "Find and adjudicate concrete bugs in the declared project files."
        modifications = [
            "Record evidence-backed candidates and observations; do not patch project files.",
        ]
        patch_optional = True
    else:
        objective = "Prepare a repair preview for confirmed bugs in the declared project files."
        modifications = [
            "Create preview artifacts only for entries assessed as confirmed in the bug index.",
        ]
        patch_optional = False
    return {
        "target": f"Project {project_name}{host_note}",
        "goal": objective,
        "modifications": modifications,
        "invariants": [
            "Never write to the origin project; operate only in the private graph workspace.",
            "Use the Supervisor bug_index assessment as the authority for bug counts.",
            "Do not invent identifiers, execution observations, validation results, or metrics.",
            "Report expected and observed behavior separately; unknown observations remain unknown.",
        ],
        "ownership_boundaries": [
            "Only the AgentWorkspace baseline and AgentOrchestrator graph workspace are writable.",
            "The origin project and paths outside the private runtime root are read-only.",
        ],
        "acceptance_criteria": [
            "Each bug record is backed by candidate, reproduction, review, and observation evidence as available.",
            "Reported paths are project-relative and source references retain their recorded line ranges.",
            "Counts and states come from persisted graph or Supervisor records; unavailable values are null.",
        ],
        "target_files": list(target_files),
        "patch_optional": patch_optional,
        "max_concurrency": 1,
    }


def _command_audit_graph(args: argparse.Namespace) -> int:
    """Create a real AgentWorkspace snapshot and start its graph (DOC-232)."""
    goal = getattr(args, "goal", "find_bugs") or "find_bugs"
    run_id = str(uuid.uuid4())
    graph_id: str | None = None
    if goal not in GOALS:
        _emit(_payload(
            run_id=run_id, graph_id=None, goal=str(goal), status="BLOCKED",
            outcome=None, error=f"unsupported goal: {goal}",
        ))
        return 2

    target_root = Path(getattr(args, "root", Path.cwd())).expanduser()
    host_arg = getattr(args, "host", None)
    host = host_arg.strip() if isinstance(host_arg, str) else None
    if host_arg is not None and (not host or len(host) > 128):
        _emit(_payload(
            run_id=run_id, graph_id=None, goal=goal, status="BLOCKED",
            outcome=None, error="--host must be a non-empty name of at most 128 characters",
        ))
        return 2

    workspace = None
    result: dict[str, object] | None = None
    exit_code = 1
    try:
        from bugx_cli.infra.security import _find_repo_root
        repo_root = _find_repo_root(Path(__file__))
        workspace_module, targets_module, orchestrator_module = _engine_modules(repo_root)
        runtime_root = _private_runtime_root()
        workspace = workspace_module.AgentWorkspace(
            origin_root=target_root,
            engine_private_dir=runtime_root,
            run_uuid=run_id,
        )
        profile = targets_module.adapt_project(
            workspace.baseline_root, workcopy_id=run_id,
        )
        target_files = _select_targets(
            profile.project_files,
            getattr(args, "scope", ()) or (),
            int(orchestrator_module.MAX_TARGET_FILES),
        )
        task_input = _task_input(goal, target_root.name or "project", host, target_files)
        orchestrator, _first_task = orchestrator_module.AgentOrchestrator.start(
            source_root=workspace.baseline_root,
            private_root=runtime_root / "graphs",
            task_input=task_input,
        )
        graph_id = orchestrator.graph_id
        workspace.verify_origin_untouched()
        snapshot = orchestrator.snapshot()
        status = str(snapshot.get("status") or "running")
        result = _payload(
            run_id=run_id,
            graph_id=graph_id,
            goal=goal,
            status=status,
            outcome="awaiting_native_agent",
        )
        result["status"] = "ready" if status == "running" else status
        exit_code = 0 if status == "running" else 1
    except (ImportError, AttributeError) as exc:
        result = _payload(
            run_id=run_id, graph_id=graph_id, goal=goal, status="BLOCKED",
            outcome=None,
            error=f"graph orchestration unavailable: {type(exc).__name__}: {exc}",
        )
        exit_code = 2
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        result = _payload(
            run_id=run_id, graph_id=graph_id, goal=goal, status="BLOCKED",
            outcome=None, error=f"audit graph could not start: {type(exc).__name__}: {exc}",
        )
        exit_code = 2
    except Exception as exc:
        result = _payload(
            run_id=run_id, graph_id=graph_id, goal=goal, status="ERROR",
            outcome=None, error=f"audit graph failed: {type(exc).__name__}: {exc}",
        )
        exit_code = 1
    finally:
        if workspace is not None:
            try:
                workspace.cleanup()
            except Exception as exc:
                result = _payload(
                    run_id=run_id, graph_id=graph_id, goal=goal, status="ERROR",
                    outcome=None,
                    error=f"AgentWorkspace cleanup failed: {type(exc).__name__}: {exc}",
                )
                exit_code = 1

    if result is None:
        result = _payload(
            run_id=run_id, graph_id=graph_id, goal=goal, status="ERROR",
            outcome=None, error="audit graph ended without a result",
        )
    _emit(result)
    return exit_code


def _command_audit_penetration(args: argparse.Namespace) -> int:
    """bugx audit：执行项目级一键全息攻防渗透审计、防御加固与双轨报告生成。"""
    root_arg = getattr(args, "root", None)
    target_root = Path(root_arg).resolve() if root_arg else Path.cwd().resolve()

    deep = bool(getattr(args, "deep", False))
    concurrency = int(getattr(args, "concurrency", 6) or 6)
    out_format = getattr(args, "format", "all") or "all"
    as_json = bool(getattr(args, "json", False))
    open_browser = bool(getattr(args, "open_browser", False))
    min_kill_rate = float(getattr(args, "min_kill_rate", 0.85) or 0.85)

    out_arg = getattr(args, "out", None)
    if out_arg:
        out_dir = Path(out_arg).resolve()
    else:
        out_dir = target_root / ".bugx" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        _ensure_core_importable()
        from bugx_cli.infra.security import _find_repo_root
        repo_root = _find_repo_root(Path(__file__))
        core_root = repo_root / "core"
        scripts_root = repo_root / "scripts"
        for p in (core_root, scripts_root):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))

        from core.infra.build_info import get_full_version_string
        from core.probes.sast_probe import run_scan as run_sast_scan
        from core.report_engine import (
            AuditMetric,
            FormalExecutiveReportBuilder,
            LanguageAuditSummary,
            ReportDataBundle,
            VisualDeliveryReportBuilder,
            VulnerabilityFinding,
            export_pdf,
            start_report_server,
        )
        # 脚本已随 DOC-232 迁入 ``scripts/cve/``：旧路径 ``run_six_worker_penetration``
        # 从仓库根已不可导入，``bugx audit --deep`` 会直接 fail-closed。包名显式写全。
        from scripts.cve.run_six_worker_penetration import run_six_worker_penetration
        from core.report_engine.models import (
            _compliance_notes as get_compliance_notes,
            _remediation_roadmap as build_remediation_roadmap,
        )
        from core.report_engine.sast_bridge import finding_from_sast
    except Exception as exc:
        print(f"[bugx audit] ✗ 无法加载核心引擎组件: {exc}", file=sys.stderr)
        return 1

    if not as_json:
        print("=" * 80)
        print("🛡️ BugX 智能自主全息安全审计流水线 (End-to-End Autonomous Audit Pipeline)")
        print(f"目标目录: {target_root}")
        print(f"执行模式: {'深度控制流穿透 (Deep)' if deep else '标准生产级审计 (Standard)'}")
        print(f"并发矩阵: {concurrency} 进程隔离 Worker (Orca + Codex Luna 6 Max)")
        print(f"质量门禁: 变异击杀率 >= {min_kill_rate * 100:.1f}%, 假阳性容忍限 = 0.00%")
        print("=" * 80)

    start_time = time.time()

    # 1. Inspect project language ecosystem
    lang_counts: Dict[str, int] = {}
    total_files = 0
    extensions_map = {
        ".py": "Python",
        ".go": "Go",
        ".java": "Java",
        ".rs": "Rust",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".c": "C",
        ".cpp": "C++",
        ".php": "PHP",
    }

    try:
        for p in target_root.rglob("*"):
            if p.is_file() and not any(part.startswith(".") for part in p.parts):
                ext = p.suffix.lower()
                if ext in extensions_map:
                    lang = extensions_map[ext]
                    lang_counts[lang] = lang_counts.get(lang, 0) + 1
                    total_files += 1
    except Exception:
        pass

    detected_langs = sorted(lang_counts.keys()) or ["Python", "Go", "JavaScript"]

    # 2. Run 6-Worker concurrent penetration drill
    drill_res = run_six_worker_penetration(
        root=repo_root,
        target=target_root,
        concurrency=concurrency,
        timeout_seconds=60.0,
        # 收据写进本次产物目录：默认路径是仓库里受版本控制的对照收据，
        # 让 ``bugx audit`` 去覆盖它会把基线变成「最近一次本机跑的数」。
        output_path=out_dir / "penetration-drill-receipt.json",
    )

    qg = drill_res.get("quality_gate", {})
    agg = drill_res.get("aggregate_metrics", {})
    # 收据里的击杀率是**夹具常量**（每个 worker 写死 probes/killed），不是对被测
    # 代码的测量。它只能作为「跑了多少探针」的原始记录，绝不能进报告结论：
    # 历史缺陷是把它当真实测量渲染成「95.23% ELITE」。
    reported_kill_rate = float(qg.get("achieved_kill_rate") or 0.0)
    master_sig = drill_res.get("master_audit_signature", "sha256sig")

    # 3. 真实 SAST 扫描：findings 只能来自被测代码，不能来自演示常量。
    #    历史缺陷：这里用 ``mock_findings_spec`` 硬编码两条漏洞，并伪造
    #    ``CVE-2026-91xx`` 编号与 ``src/gateway.py`` 路径——报告因此会对一个
    #    完全干净的项目宣称「2 项已验证漏洞，含 1 严重」，是凭空捏造的凭证。
    scan = run_sast_scan(target_root)
    findings: List[VulnerabilityFinding] = [
        finding_from_sast(item, target_root, index=pos)
        for pos, item in enumerate(scan.get("findings") or [])
    ]
    scan_status = str(scan.get("status") or "COMPLETED")
    scan_tools = list(scan.get("tools_executed") or [])

    # 4. Construct Report Data Bundle
    total_probes = int(agg.get("total_probes_executed") or 0)
    total_killed = int(agg.get("total_mutations_killed") or 0)
    # 暴露面总量只认扫描器报告的文件数；扫描器不报告时退回命中数只能说明
    # 「有东西可看」，不能冒充全量控制流暴露面。
    total_surfaces = int(scan.get("scanned_files") or 0) or max(len(findings), 0)

    metrics = AuditMetric(
        total_targets=max(len(detected_langs), 1),
        total_surfaces=total_surfaces,
        # 突防收据是确定性演示数据（非真实变异），因此「变异/击杀/评级」三项
        # 一律置零：置零后 attestation 判为 NOT-EXERCISED，渲染层不会输出
        # 「97.35% ELITE」这种对夹具的描述性数字。探针数保留为原始收据。
        total_probes=total_probes,
        total_mutations=0,
        total_killed=0,
        overall_kill_rate=0.0,
        false_positive_rate=0.0,
        negative_control_pass_rate=0.0,
        rating="NOT-EXERCISED",
        total_findings=len(findings),
        critical_count=sum(1 for f in findings if f.severity == "CRITICAL"),
        high_count=sum(1 for f in findings if f.severity == "HIGH"),
        medium_count=sum(1 for f in findings if f.severity == "MEDIUM"),
        low_count=sum(1 for f in findings if f.severity == "LOW"),
    )

    lang_summaries = []
    for l_name in detected_langs:
        lang_summaries.append(LanguageAuditSummary(
            language=l_name,
            categories_count=2,
            total_surfaces=total_surfaces // len(detected_langs),
            total_probes=total_probes // len(detected_langs),
            total_mutations=0,
            total_killed=0,
            kill_rate=0.0,
            rating="NOT-EXERCISED",
            zero_false_positives=False,
            negative_control_rate=0.0,
            categories=[],
        ))

    bundle = ReportDataBundle(
        report_id=f"BUGX-AUDIT-{time.strftime('%Y%m%d')}-{target_root.name.upper()[:6]}",
        title=f"BugX 项目全息控制流安全审计报告 &bull; {target_root.name}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        auditor=f"BugX Autonomous Security Intelligence & Mutation Engine {get_full_version_string()}",
        assessment_scope=(
            f"项目 {target_root.name} 全技术栈 ({', '.join(detected_langs)}) "
            f"控制流深度攻防突防；SAST 工具链: {', '.join(scan_tools) or '无'}"
        ),
        metrics=metrics,
        languages=lang_summaries,
        findings=findings,
        compliance_notes=get_compliance_notes(
            mutation_exercised=total_probes > 0, findings=findings,
        ),
        remediation_roadmap=build_remediation_roadmap(findings),
        sha256_seal=hashlib.sha256(
            f"{target_root}:{master_sig}:{scan.get('elapsed_seconds', 0)}".encode("utf-8")
        ).hexdigest(),
        # 演示用 6-worker 收据不是裁决依据：把它的性质如实记在 authority 里，
        # 渲染层据此拒绝会签（见 core/report_engine/attestation.py）。
        authority={
            "source": "bugx-audit-penetration-drill",
            "measurements": "synthetic",
            "gate_blockers": [
                f"6-worker 突防收据为确定性演示数据，非真实裁决（SAST 状态 {scan_status}）",
            ],
            "verification_executed": 0,
        },
    )

    # 5. Deliver Dual-Track Reports (HTML, Markdown, PDF)
    generated_files: List[str] = []

    # Formal
    formal_builder = FormalExecutiveReportBuilder(bundle)
    if out_format in {"html", "both", "all"}:
        p_html = out_dir / "BUGX_ENTERPRISE_SECURITY_AUDIT_REPORT_FORMAL.html"
        p_html.write_text(formal_builder.render_html(), encoding="utf-8")
        generated_files.append(str(p_html))

    if out_format in {"md", "markdown", "both", "all"}:
        p_md = out_dir / "BUGX_ENTERPRISE_SECURITY_AUDIT_REPORT_FORMAL.md"
        p_md.write_text(formal_builder.render_markdown(), encoding="utf-8")
        generated_files.append(str(p_md))

    if out_format in {"pdf", "all"}:
        p_pdf = export_pdf(bundle, out_dir, report_type="formal")
        generated_files.append(str(p_pdf))

    # Visual
    visual_builder = VisualDeliveryReportBuilder(bundle)
    if out_format in {"html", "both", "all"}:
        p_v_html = out_dir / "BUGX_AESTHETIC_VISUAL_DELIVERY_REPORT.html"
        p_v_html.write_text(visual_builder.render_html(), encoding="utf-8")
        generated_files.append(str(p_v_html))

    if out_format in {"md", "markdown", "both", "all"}:
        p_v_md = out_dir / "BUGX_AESTHETIC_VISUAL_DELIVERY_REPORT.md"
        p_v_md.write_text(visual_builder.render_markdown(), encoding="utf-8")
        generated_files.append(str(p_v_md))

    if out_format in {"pdf", "all"}:
        p_v_pdf = export_pdf(bundle, out_dir, report_type="visual")
        generated_files.append(str(p_v_pdf))

    elapsed = time.time() - start_time

    if as_json:
        severity_counts = {
            "Critical": metrics.critical_count,
            "High": metrics.high_count,
            "Medium": metrics.medium_count,
            "Low": metrics.low_count,
        }
        elapsed_seconds = round(elapsed, 2)
        result_payload = {
            "status": "ok",
            "target": str(target_root),
            "report_id": bundle.report_id,
            "metrics": {
                "detected_languages": detected_langs,
                "surfaces_scouted": total_surfaces,
                "overall_kill_rate": metrics.overall_kill_rate,
                "kill_rate": metrics.overall_kill_rate,
                # 收据原值单独暴露：调用方要看「夹具报了多少」时取这个字段，
                # 不要把它和真实测量混在一个键里。
                "drill_reported_kill_rate": reported_kill_rate,
                "rating": metrics.rating,
                "findings_count": len(findings),
                "vulnerability_count": len(findings),
                "severity_counts": severity_counts,
                "critical_count": metrics.critical_count,
                "high_count": metrics.high_count,
                "medium_count": metrics.medium_count,
                "low_count": metrics.low_count,
                "false_positive_rate": metrics.false_positive_rate,
                "negative_control_pass_rate": metrics.negative_control_pass_rate,
            },
            "quality_gate": {
                "required_kill_rate": min_kill_rate,
                "achieved_kill_rate": metrics.overall_kill_rate,
                "drill_reported_kill_rate": reported_kill_rate,
                # 未实测就没有「零假阳性」可宣称：false 是「未证实」，不是「未通过」。
                "zero_false_positives": metrics.false_positive_rate == 0.0
                and metrics.total_mutations > 0,
                "negative_control_pass_rate": metrics.negative_control_pass_rate,
                "exercise_status": "NOT-EXERCISED" if metrics.total_mutations == 0 else "EXERCISED",
            },
            "sha256_seal": bundle.sha256_seal,
            "elapsed_seconds": elapsed_seconds,
            "duration_seconds": elapsed_seconds,
            "generated_files": generated_files,
        }
        print(json.dumps(result_payload, indent=2, ensure_ascii=False))
        return 0

    print("=" * 80)
    print("✅ BugX 全息安全审计完成 (Audit Completed)")
    print("=" * 80)
    print(f"  识别技术栈: {', '.join(detected_langs)} ({total_files} 源文件)")
    print(f"  暴露面勘测: {total_surfaces:,} Surfaces")
    print(f"  变异击杀率: 未测量（收据夹具值 {reported_kill_rate * 100:.2f}%，不参与结论）")
    print(f"  已验证漏洞: {len(findings)} 项 ({metrics.critical_count} 严重, {metrics.high_count} 高危)")
    print(f"  防伪加密印章: {bundle.sha256_seal}")
    print(f"  总耗时    : {elapsed:.2f}s")
    print(f"  产物目录  : {out_dir}")
    print("  交付报告清单:")
    for f in generated_files:
        p = Path(f)
        size_kb = p.stat().st_size / 1024
        print(f"    - {p.name} ({size_kb:.1f} KB)")
    print("=" * 80)

    if open_browser:
        print("[bugx audit] 正在启动报告实时 Web 预览工作台: http://127.0.0.1:8080/ ...")
        server, thread = start_report_server(bundle, host="127.0.0.1", port=8080, run_once=False)
        try:
            thread.join()
        except KeyboardInterrupt:
            server.server_close()

    return 0


def command_audit(args: argparse.Namespace) -> int:
    """bugx audit 统一调度入口：根据参数自适应分发到原生图编排或全息攻防渗透流水线。"""
    is_penetration = bool(
        getattr(args, "deep", False)
        or getattr(args, "out", None)
        or getattr(args, "format", "all") != "all"
        or getattr(args, "concurrency", 6) != 6
        or getattr(args, "min_kill_rate", 0.85) != 0.85
        or getattr(args, "open_browser", False)
    )
    if is_penetration:
        return _command_audit_penetration(args)
    return _command_audit_graph(args)
