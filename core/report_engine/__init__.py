"""BugX 双轨制报告引擎 (Dual-Track Humanized Reporting Engine).

提供两大独立演化的高品质报告体系与交互预览服务：
1. FormalExecutiveReportBuilder: 商务正式安全审计报告 (面向 CISO / CTO / 董事会 / 合规审计员)
2. VisualDeliveryReportBuilder: 现代化高颜值技术交付报告 (面向研发工程师 / 架构师 / 开源社区)
3. start_report_server: 轻量本地 Web 交互式实时预览服务 (bugx report serve)
4. export_pdf: 高保真物理 PDF 报告导出器 (bugx report --format pdf)
5. finding_report: 缺陷与验证收据标准化报告构建器
6. report_client: 云端与本地报告上传交互客户端
7. report_share: 审计报告跨平台分享与凭据打包
8. web_conformance: Web 前端标准合规性审计
"""

from __future__ import annotations

from .models import (
    AuditMetric,
    CategoryMetric,
    LanguageAuditSummary,
    ProjectConfig,
    ProjectWorkspaceInfo,
    ReportDataBundle,
    VulnerabilityFinding,
)
from .formal_report_builder import FormalExecutiveReportBuilder
from .visual_report_builder import VisualDeliveryReportBuilder
from .server import start_report_server
from .pdf_exporter import export_pdf
from . import finding_report, report_client, report_share, web_conformance

__all__ = [
    "AuditMetric",
    "CategoryMetric",
    "LanguageAuditSummary",
    "ProjectConfig",
    "ProjectWorkspaceInfo",
    "ReportDataBundle",
    "VulnerabilityFinding",
    "FormalExecutiveReportBuilder",
    "VisualDeliveryReportBuilder",
    "start_report_server",
    "export_pdf",
    "finding_report",
    "report_client",
    "report_share",
    "web_conformance",
]
