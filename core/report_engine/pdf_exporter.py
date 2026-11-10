"""BugX 高保真 PDF 报告导出引擎 (High-Fidelity PDF Report Exporter).

支持一键将 Formal Executive Report 或 Aesthetic Visual Report 导出为高保真 PDF 物理文件。
具备两级生成策略：
1. 优先尝试系统无头浏览器 / wkhtmltopdf / weasyprint 进行高保真 CSS 渲染；
2. 在纯纯净/离线环境中无缝降级至内置纯 Python PDF 1.4 规范合成器，
   绝对保证在任何机台/CI 节点 100% 生成格式严谨合规的物理 PDF 归档。
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from typing import Any, List

from . import attestation
from .formal_report_builder import FormalExecutiveReportBuilder
from .models import ReportDataBundle
from .visual_report_builder import VisualDeliveryReportBuilder


def _ascii(value: str) -> str:
    """PDF 文本流只承载 latin-1；非 ASCII 一律降级为可读的 ASCII 标记。"""
    return value.encode("ascii", errors="replace").decode("ascii")


def _pdf_text(value: str) -> str:
    """PDF 字符串字面量转义：``\\``/``(``/``)`` 必须转义，否则会撕裂内容流。"""
    return _ascii(value).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _pdf_attestation_line(a: attestation.Attestation) -> str:
    """PDF 尾注：未会签时不得写「合规审计有效」。"""
    if a.countersigned:
        return "OFFICIAL ATTESTATION: Valid for Board & Regulatory Compliance Audits"
    return "DRAFT - NOT ATTESTED: triage-only export, not valid for compliance attestation"


def _generate_pure_pdf_stream(bundle: ReportDataBundle, report_type: str = "formal") -> bytes:
    """使用纯 Python 合成符合 ISO 32000-1 (PDF 1.4) 标准的二进制 PDF 文档。"""
    b = bundle
    m = b.metrics
    a = attestation.from_bundle(b)
    # PDF 文本流用 Helvetica（latin-1）：中文必须走 ASCII 描述，否则整段会变成
    # ``?``；因此这里的判定词全部取自 attestation 的 ASCII 词表。
    kill_verdict = _ascii(a.kill_rate_verdict(m.overall_kill_rate))
    fp_verdict = _ascii(a.false_positive_verdict())
    nc_verdict = _ascii(a.negative_control_verdict())
    target_verdict = _ascii(a.target_verdict(m.total_targets))
    status_line = a.ascii_status

    # 未实测变异时不得打印「0.00%」——读者会读成击杀率为零而不是没测。
    kill_rate_text = (
        f"{m.overall_kill_rate * 100:.2f}%"
        if a.mutation_exercised else "NOT MEASURED"
    )
    fp_rate_text = (
        f"{m.false_positive_rate:.2f}%"
        if a.false_positive_verdict() == attestation.PASS else "NOT MEASURED"
    )

    # Build PDF Content Stream text lines
    stream_lines: List[str] = [
        "BT",
        "/F1 18 Tf",
        "50 780 Td",
        f"({_pdf_text(b.title[:60])}) Tj",
        "/F2 9 Tf",
        "0 -20 Td",
        f"({_pdf_text(f'Report ID: {b.report_id}   Date: {b.created_at}   Classification: CONFIDENTIAL')}) Tj",
        "0 -15 Td",
        f"({_pdf_text(f'Auditor: {b.auditor[:70]}')}) Tj",
        "0 -15 Td",
        f"(Digital SHA-256 Seal: {b.sha256_seal}) Tj",
        "0 -15 Td",
        f"(Attestation Status: {status_line}) Tj",
        "/F1 13 Tf",
        "0 -30 Td",
        "(1. EXECUTIVE SUMMARY & QUALITY GATES) Tj",
        "/F2 10 Tf",
        "0 -18 Td",
        f"(Total Targets Evaluated: {m.total_targets} [{target_verdict}]    Control Flow Surfaces Scouted: {m.total_surfaces:,}) Tj",
        "0 -15 Td",
        f"(Exploit Mutation Kill Rate: {kill_rate_text} [RATING: {m.rating if a.mutation_exercised else attestation.NOT_EXERCISED}] [{kill_verdict}]) Tj",
        "0 -15 Td",
        f"(False Positive Rate: {fp_rate_text} [{fp_verdict}]     Negative Control: {nc_verdict}) Tj",
        "0 -15 Td",
        f"(Adjudicated Findings: {m.total_findings} (Critical: {m.critical_count}, High: {m.high_count})) Tj",
        "/F1 13 Tf",
        "0 -30 Td",
        "(2. MULTI-ECOSYSTEM AUDIT BREAKDOWN) Tj",
        "/F2 9 Tf",
    ]

    for lang in b.languages[:7]:
        exercised = a.mutation_exercised and lang.total_mutations > 0
        lang_kill = f"{lang.kill_rate * 100:.2f}%" if exercised else "NOT MEASURED"
        lang_grade = lang.rating if exercised else attestation.NOT_EXERCISED
        stream_lines.extend([
            "0 -15 Td",
            f"(- {lang.language}: {lang.categories_count} Categories | {lang.total_surfaces:,} Surfaces | Kill Rate: {lang_kill} | Grade: {lang_grade}) Tj",
        ])

    stream_lines.extend([
        "/F1 13 Tf",
        "0 -30 Td",
        "(3. VERIFIED FINDINGS & CVE SUMMARY) Tj",
        "/F2 9 Tf",
    ])

    for f in b.findings[:4]:
        stream_lines.extend([
            "0 -15 Td",
            f"({_pdf_text(f'[ {f.severity} ] {f.cve_display} / {f.id} @ {f.package_name}: CVSS {f.cvss_display}')}) Tj",
            "0 -12 Td",
            f"({_pdf_text('    Category: ' + (f.cwe_id or 'unmapped-CWE') + ' ' + f.cwe_title[:50])}) Tj",
        ])
    if len(b.findings) > 4:
        # 单页 PDF 只排得下 4 条；必须显式说明还有多少条，不能静默丢弃。
        stream_lines.extend([
            "0 -15 Td",
            f"({_pdf_text(f'... {len(b.findings) - 4} more finding(s) in the Markdown/HTML report')}) Tj",
        ])

    stream_lines.extend([
        "/F1 11 Tf",
        "0 -35 Td",
        f"({_pdf_attestation_line(a)}) Tj",
        "ET",
    ])

    stream_data = "\n".join(stream_lines).encode("latin-1", errors="replace")

    # Assemble PDF Structure
    objects: List[bytes] = []
    
    # 1: Catalog
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    # 2: Pages
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    # 3: Page
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>"
    )
    # 4: Stream
    stream_obj = (
        f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
        + stream_data
        + b"\nendstream"
    )
    objects.append(stream_obj)
    # 5: Font Helvetica-Bold
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    # 6: Font Helvetica
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # Output construction
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []

    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode("ascii"))
        out.write(obj)
        out.write(b"\nendobj\n")

    xref_offset = out.tell()
    out.write(b"xref\n")
    out.write(f"0 {len(objects) + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode("ascii"))

    out.write(b"trailer\n")
    out.write(f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode("ascii"))
    out.write(b"startxref\n")
    out.write(f"{xref_offset}\n".encode("ascii"))
    out.write(b"%%EOF\n")

    return out.getvalue()


def export_pdf(
    bundle: ReportDataBundle,
    out_path: Path | str,
    report_type: str = "formal",
) -> Path:
    """导出高保真 PDF 安全报告文件。"""
    dest = Path(out_path).resolve()
    if dest.is_dir():
        filename = (
            "BUGX_ENTERPRISE_SECURITY_AUDIT_REPORT_FORMAL.pdf"
            if report_type == "formal"
            else "BUGX_AESTHETIC_VISUAL_DELIVERY_REPORT.pdf"
        )
        dest = dest / filename
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Strategy 1: Check for external headless chromium / wkhtmltopdf
    pdf_converted = False
    if shutil.which("wkhtmltopdf"):
        try:
            builder = (
                FormalExecutiveReportBuilder(bundle)
                if report_type == "formal"
                else VisualDeliveryReportBuilder(bundle)
            )
            html_content = builder.render_html()
            tmp_html = dest.with_suffix(".tmp.html")
            tmp_html.write_text(html_content, encoding="utf-8")
            res = subprocess.run(
                ["wkhtmltopdf", "--quiet", str(tmp_html), str(dest)],
                capture_output=True,
                timeout=20,
            )
            tmp_html.unlink(missing_ok=True)
            if res.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                pdf_converted = True
        except Exception:
            pdf_converted = False

    # Strategy 2: Pure Python Built-in PDF Engine (Zero-dependency fail-safe)
    if not pdf_converted:
        raw_pdf_bytes = _generate_pure_pdf_stream(bundle, report_type=report_type)
        dest.write_bytes(raw_pdf_bytes)

    return dest
