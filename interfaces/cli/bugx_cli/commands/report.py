"""BugX 报告生成子命令 (bugx report: formal, visual, both, serve)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..common import _ensure_core_importable


def _load_bundle(ReportDataBundle: Any, *, source: str, project_root: Path, eval_dir: Path) -> Any:
    """按 ``--source`` 选择报告数据来源。

    ``project`` 取不到权威状态时**不回退**编造数据：返回带门禁阻断标记的
    bundle，由渲染层如实标注「未会签」。``auto`` 才允许回退到收据。
    """
    if source == "eval":
        return ReportDataBundle.from_eval_dir(eval_dir)
    if source == "project":
        has_eval = any(eval_dir.glob("*_all_categories_audit.json")) if eval_dir.is_dir() else False
        return ReportDataBundle.from_project(
            project_root, eval_dir=eval_dir if has_eval else None,
        )
    if source == "auto":
        has_eval = any(eval_dir.glob("*_all_categories_audit.json")) if eval_dir.is_dir() else False
        bundle = ReportDataBundle.from_project(
            project_root, eval_dir=eval_dir if has_eval else None,
        )
        blockers = (bundle.authority or {}).get("gate_blockers") or []
        if blockers and has_eval:
            return ReportDataBundle.from_eval_dir(eval_dir)
        return bundle
    raise ValueError(f"未知的报告数据来源: {source}（可选 project / eval / auto）")


def command_report(args: argparse.Namespace) -> int:
    """bugx report：生成双轨制报告或启动实时 Web 预览服务。

    数据来源按 ``--source`` 选择（默认 ``project``）：

    * ``project``：读项目运行态 ``.bugx/.../state/supervisor.json`` 的权威
      findings —— 这是**唯一**能反映真实审计结论的来源；
    * ``eval``：读 ``--eval-dir`` 下的引擎自测收据（``*_all_categories_audit.json``）；
    * ``auto``：项目运行态可用则用它，否则退回收据。

    历史缺陷：本命令只读收据、从不读 Supervisor，于是 5 条已 CONFIRMED 的漏
    洞被渲染成「0 漏洞 / PASS (COMPLIANT) / 官方会签」。现在 source=project
    是默认值，取不到权威状态时报告会显式标注「未会签 + 门禁阻断原因」。
    """
    subcommand = getattr(args, "report_type", "both") or "both"
    if subcommand in {"generate", None}:
        subcommand = getattr(args, "type", "both") or "both"

    source = (getattr(args, "source", "project") or "project").lower()
    project_root_arg = getattr(args, "root", None) or getattr(args, "project", None)
    project_root = Path(project_root_arg).resolve() if project_root_arg else Path.cwd()

    eval_dir_arg = getattr(args, "eval_dir", None)
    if eval_dir_arg:
        eval_dir = Path(eval_dir_arg).resolve()
    else:
        # 收据目录一律相对**被审计项目**解析，绝不回落到引擎自身仓库。
        # 历史缺陷：这里用 ``_find_repo_root(Path(__file__))``——那是**引擎**的
        # 仓库根，于是 ``bugx report --root <靶场> --source project`` 会把引擎
        # 自己的 ``verification/evals/oss/python_all_categories_audit.json``
        # 当成靶场收据，在靶场报告里写出「击杀 42,125 处变异 / 95.23% / ELITE」。
        # 那既不是靶场的测量值，也不是本轮的任何一次执行。
        candidates = (
            project_root / "verification" / "evals" / "oss",
            project_root / "evals" / "oss",
        )
        eval_dir = next((c for c in candidates if c.is_dir()), candidates[-1])

    out_arg = getattr(args, "out", None)
    out_dir = Path(out_arg).resolve() if out_arg else eval_dir
    if out_dir.is_file():
        out_dir = out_dir.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    output_format = (getattr(args, "format", "both") or "both").lower()
    as_json = bool(getattr(args, "json", False))

    try:
        _ensure_core_importable()
        from bugx_cli.infra.security import _find_repo_root
        repo_root = _find_repo_root(Path(__file__))
        core_root = repo_root / "core"
        if core_root.is_dir():
            # ``core/`` 是命名空间包，父目录（仓库根）必须在 sys.path 上。
            for entry in (core_root.parent, core_root):
                s = str(entry)
                while s in sys.path:
                    sys.path.remove(s)
                sys.path.insert(0, s)
        from core.report_engine import attestation as attestation_mod
        from core.report_engine import (
            FormalExecutiveReportBuilder,
            ReportDataBundle,
            VisualDeliveryReportBuilder,
            export_pdf,
            start_report_server,
        )
    except Exception as exc:
        print(f"[bugx report] ✗ 无法加载报告引擎: {exc}", file=sys.stderr)
        return 1

    try:
        bundle = _load_bundle(
            ReportDataBundle, source=source,
            project_root=project_root, eval_dir=eval_dir,
        )
        custom_title = getattr(args, "title", None)
        if custom_title:
            bundle.title = custom_title
    except Exception as exc:
        print(f"[bugx report] ✗ 聚合审计数据失败 ({eval_dir}): {exc}", file=sys.stderr)
        return 1

    # 会签资格只能由 attestation 判定——不能只看「有没有门禁阻断」。
    # 历史缺陷：这里写 ``not blockers``，于是「零变异 + 零验证」的项目也会
    # 在控制台打印 COUNTERSIGNED，而同一份 Markdown 里写着 NOT COUNTERSIGNED，
    # 两个通道互相矛盾，操作者只会读控制台那一行。
    attest = attestation_mod.from_bundle(bundle)
    blockers = list(attest.gate_blockers)
    if not as_json:
        for blocker in blockers:
            print(f"[bugx report] ⚠ 未会签原因: {blocker}", file=sys.stderr)
        if not blockers and not attest.countersigned:
            print(f"[bugx report] ⚠ 未会签原因: {attest.status_line}", file=sys.stderr)

    # 0. Live Preview Server
    if subcommand == "serve":
        host = getattr(args, "host", "127.0.0.1") or "127.0.0.1"
        port = int(getattr(args, "port", 8080) or 8080)
        run_once = bool(getattr(args, "once", False))

        try:
            server, thread = start_report_server(bundle, host=host, port=port, run_once=run_once)
            if as_json:
                print(json.dumps({
                    "status": "ok",
                    "server": f"http://{host}:{port}/",
                    "report_id": bundle.report_id,
                }))
            else:
                print("=" * 76)
                print(f"🌐 BugX 统一控制台与多项目同步中心已启动: http://{host}:{port}/")
                print(f"  - 🗂️ 受管项目空间:   http://{host}:{port}/?view=projects")
                print(f"  - 🔍 漏洞控制台:     http://{host}:{port}/?view=workbench")
                print(f"  - ⚡ 高颜值交付报告: http://{host}:{port}/?view=visual")
                print(f"  - 🏛️ 商务正式报告:   http://{host}:{port}/?view=formal")
                print(f"  - 🔄 同步中心看板:   http://{host}:{port}/?view=sync")
                print(f"  - 📊 项目列表 API:   http://{host}:{port}/api/projects")
                print(f"  - 📥 商务 PDF 下载:  http://{host}:{port}/api/export/pdf?report_type=formal")
                print(f"  - 📥 视觉 PDF 下载:  http://{host}:{port}/api/export/pdf?report_type=visual")
                print("=" * 76)
            if run_once:
                thread.join(timeout=5.0)
                server.server_close()
            else:
                thread.join()
            return 0
        except KeyboardInterrupt:
            print("\n[bugx report serve] 服务已停止。")
            return 0
        except Exception as exc:
            print(f"[bugx report serve] ✗ 启动失败: {exc}", file=sys.stderr)
            return 1

    generated_files: list[str] = []

    # 1. Formal Report
    if subcommand in {"formal", "both", "all"}:
        formal_builder = FormalExecutiveReportBuilder(bundle)
        if output_format in {"html", "both"}:
            formal_html = formal_builder.render_html()
            html_path = out_dir / "BUGX_ENTERPRISE_SECURITY_AUDIT_REPORT_FORMAL.html"
            html_path.write_text(formal_html, encoding="utf-8")
            generated_files.append(str(html_path))

        if output_format in {"md", "markdown", "both"}:
            formal_md = formal_builder.render_markdown()
            md_path = out_dir / "BUGX_ENTERPRISE_SECURITY_AUDIT_REPORT_FORMAL.md"
            md_path.write_text(formal_md, encoding="utf-8")
            generated_files.append(str(md_path))

        if output_format in {"pdf", "both"}:
            pdf_path = export_pdf(bundle, out_dir, report_type="formal")
            generated_files.append(str(pdf_path))

    # 2. Visual Report
    if subcommand in {"visual", "both", "all"}:
        visual_builder = VisualDeliveryReportBuilder(bundle)
        if output_format in {"html", "both"}:
            visual_html = visual_builder.render_html()
            html_path = out_dir / "BUGX_AESTHETIC_VISUAL_DELIVERY_REPORT.html"
            html_path.write_text(visual_html, encoding="utf-8")
            generated_files.append(str(html_path))

        if output_format in {"md", "markdown", "both"}:
            visual_md = visual_builder.render_markdown()
            md_path = out_dir / "BUGX_AESTHETIC_VISUAL_DELIVERY_REPORT.md"
            md_path.write_text(visual_md, encoding="utf-8")
            generated_files.append(str(md_path))

        if output_format in {"pdf", "both"}:
            pdf_path = export_pdf(bundle, out_dir, report_type="visual")
            generated_files.append(str(pdf_path))

    if as_json:
        result_payload = {
            "status": "ok",
            "report_id": bundle.report_id,
            "subcommand": subcommand,
            "format": output_format,
            "source": (bundle.authority or {}).get("source", source),
            "attestation": {
                "countersigned": attest.countersigned,
                "status_line": attest.status_line,
                "mutation_exercised": attest.mutation_exercised,
                "status": (bundle.authority or {}).get("status", ""),
                "gate_blockers": blockers,
            },
            "metrics": {
                "total_targets": bundle.metrics.total_targets,
                "total_surfaces": bundle.metrics.total_surfaces,
                "overall_kill_rate": bundle.metrics.overall_kill_rate,
                "rating": bundle.metrics.rating,
                "total_findings": bundle.metrics.total_findings,
            },
            "sha256_seal": bundle.sha256_seal,
            "generated_files": generated_files,
        }
        print(json.dumps(result_payload, indent=2, ensure_ascii=False))
        return 0

    print("=" * 76)
    print("✨ BugX 双轨制人性化报告引擎生成完成 (Dual-Track Humanized Report Engine)")
    print("=" * 76)
    print(f"  报告编号: {bundle.report_id}")
    print(f"  数据来源: {(bundle.authority or {}).get('source', source)}")
    print(f"  背书状态: {'COUNTERSIGNED' if attest.countersigned else 'NOT ATTESTED'}"
          f"{'' if attest.countersigned else ' — ' + attest.status_line}")
    print(f"  安全评级: {bundle.metrics.rating} "
          f"(Kill Rate: {attest.kill_rate_cell(bundle.metrics.overall_kill_rate)})")
    print(f"  防伪印章: {bundle.sha256_seal}")
    print(f"  产出目录: {out_dir}")
    print("  生成文件清单:")
    for f in generated_files:
        p = Path(f)
        size_kb = p.stat().st_size / 1024
        print(f"    - {p.name} ({size_kb:.1f} KB)")
    print("=" * 76)
    return 0



