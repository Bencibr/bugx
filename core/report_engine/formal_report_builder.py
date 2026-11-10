"""BugX 商务正式安全审计报告生成器 (Formal Executive Security Audit Report Builder).

面向企业高管 (CISO / CTO / 董事会 / 合规主管 / 审计师) 的正式商务化安全评估报告。
严格采用 Corporate Slate / Navy 商务冷色系，具备法律级合规声明、密码学哈希防篡改印章、
CVSS v3.1 / CWE Top 25 威胁热力图、SLA 整改路线图以及权威审计师签署栏。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List

from .models import ReportDataBundle, VulnerabilityFinding, LanguageAuditSummary
from . import attestation


class FormalExecutiveReportBuilder:
    """正式商务安全审计报告构建器。"""

    def __init__(self, bundle: ReportDataBundle):
        self.bundle = bundle

    def render_markdown(self) -> str:
        """渲染符合 GitHub Flavored Markdown 规范的正式商务审计报告。"""
        b = self.bundle
        m = b.metrics
        a = attestation.from_bundle(b)

        lines: List[str] = []
        lines.append(f"# 🛡️ {b.title}")
        lines.append("")
        lines.append(f"> **报告唯一识别码 (Report ID)**: `{b.report_id}`  ")
        lines.append(f"> **安全分发级别 (Classification)**: `CONFIDENTIAL // EXECUTIVE DISTRIBUTION ONLY`  ")
        lines.append(f"> **评估基准时间 (Timestamp)**: `{b.created_at}`  ")
        lines.append(f"> **主审计机构 (Auditor)**: `{b.auditor}`  ")
        lines.append(f"> **密码学数字印章 (SHA-256 Digest)**: `{b.sha256_seal}`  ")
        lines.append(f"> **背书状态 (Attestation)**: `{a.status_line}`")
        for blocker in a.gate_blockers:
            lines.append(f"> **门禁阻断 (Gate Blocker)**: {blocker}  ")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 1. 执行高管摘要 (Executive Summary)")
        lines.append("")
        if a.mutation_exercised:
            lines.append(
                f"本报告由 **BugX 智能自主安全审计引擎** 依据本轮**实际执行**的验证动作出具。"
                f"审计范围：**{b.assessment_scope}**。"
                f"实际覆盖 **{m.total_surfaces:,}** 个攻击暴露面，执行 **{m.total_probes:,}** 次攻防探测。"
            )
            lines.append(
                f"本次击杀 **{m.total_killed:,}** 处变异缺陷，**综合击杀率 {m.overall_kill_rate * 100:.2f}%**，"
                f"评级 **{m.rating}**；负对照基准放行率 **{m.negative_control_pass_rate * 100:.2f}%**。"
            )
        else:
            # 变异未实测时，任何击杀率/评级数字都只描述夹具，不是对本项目的
            # 结论——摘要里连数字一起撤掉，否则「97.35% ELITE」照样会被引用。
            # 但负对照/假阳性是另一回事：它们按逐条 verification 记录实测，
            # 不能因为没跑变异就连带说成「未测量」。
            nc_measured = a.negative_control_verdict() == attestation.PASS
            lines.append(
                f"本报告由 **BugX 智能自主安全审计引擎** 出具。"
                f"审计范围：**{b.assessment_scope}**。"
                f"**本轮未执行对抗性变异攻击：变异击杀率与评级均未测量**"
                + (
                    f"；负对照与假阳性率按逐条验证记录实测。"
                    if nc_measured
                    else "；负对照放行率亦未测量。"
                )
            )
        if not a.findings_present:
            lines.append("")
            lines.append(
                "> ⚠️ **本轮没有任何已裁决缺陷。** 「零发现」只说明本轮验证范围内未发现缺陷，"
                "既不等于「零风险」，也不构成「零假阳性裁决通过」——不存在裁决对象。"
            )
        lines.append("")

        # Executive Metrics Table：每一行的判定词都来自实测，未实测一律
        # NOT-EXERCISED，不得为了好看写 PASS/ZERO FP（历史缺陷 BUGX-AUDIT#1）。
        lines.append("| 核心安全评估指标 | 审计测量数值 | 业界基准基线 | 状态判定 |")
        lines.append("| :--- | :--- | :--- | :--- |")
        lines.append(
            f"| 评估资产靶标总数 | **{m.total_targets} 个靶标** | >= {attestation.TARGET_BASELINE} 个开源项目 | "
            f"`{a.target_verdict(m.total_targets)}` |"
        )
        lines.append(
            f"| 控制流暴露面总量 | **{m.total_surfaces:,} Surfaces** | 深度广域全覆盖 | "
            f"`{a.surface_coverage_verdict(m.total_surfaces)}` |"
        )
        # 未实测的指标连数字都不呈现：写「97.35%（NOT-EXERCISED）」仍然会被
        # 截屏引用成「击杀率 97%」，而它描述的是演示夹具而不是本项目。
        kill_cell = (
            f"{m.overall_kill_rate * 100:.2f}%" if a.mutation_exercised else "未测量"
        )
        fp_cell = (
            f"{m.false_positive_rate:.2f}%" if a.false_positive_verdict() == attestation.PASS
            else "未测量"
        )
        nc_cell = (
            f"{m.negative_control_pass_rate * 100:.2f}%"
            if a.negative_control_verdict() == attestation.PASS else "未测量"
        )
        lines.append(
            f"| 恶意变异击杀率 | **{kill_cell}** | "
            f">= {attestation.KILL_RATE_BASELINE * 100:.2f}% [ELITE] | "
            f"`{a.kill_rate_verdict(m.overall_kill_rate)}` |"
        )
        lines.append(
            f"| 假阳性误报率 (FP) | **{fp_cell}** | "
            f"<= {attestation.FALSE_POSITIVE_BASELINE:.2f}% (容忍限) | "
            f"`{a.false_positive_verdict()}` |"
        )
        lines.append(
            f"| 负对照基准放行率 | **{nc_cell}** | 100.00% (标准) | "
            f"`{a.negative_control_verdict()}` |"
        )
        # 「100% 具备可复现 PoC」曾是无条件硬编码：没有 PoC 的发现也会被算进去。
        # 这里按实测计数，分子分母都来自 findings 本身。
        poc_covered = sum(1 for f in b.findings if f.exploit_poc.strip())
        finding_cell = (
            f"**{m.total_findings} 项 (含 {m.critical_count} 严重 / {m.high_count} 高危)**"
            f"，{poc_covered} 条附复现命令"
            if m.total_findings else "**0 项**"
        )
        lines.append(
            f"| 确认已验证漏洞数 | {finding_cell} | "
            f"{'全部附可复现证据' if m.total_findings and poc_covered == m.total_findings else '存在未附复现证据的发现'} | "
            f"`{'ACTION REQUIRED' if m.total_findings else 'NONE REPORTED'}` |"
        )
        lines.append("")

        lines.append("## 2. 审计范围与多语言资产清单 (Assessment Scope)")
        lines.append("")
        if not b.languages:
            lines.append("本轮无多语言变异收据可列，故无生态语言清单。**这不构成已审计结论。**")
            lines.append("")
        lines.append("| 生态语言 | 覆盖类别数 | 控制流暴露面 | 探测攻击数 | 变异击杀数 | 击杀率 | 评级 |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
        for lang in b.languages:
            # 变异未实测的语言行不能写 "0.00%": 读者会把它读成「击杀率为零」，
            # 而事实是「没测」。未实测就用文字，实测才给数字。
            exercised = a.mutation_exercised and lang.total_mutations > 0
            kill_cell = f"**{lang.kill_rate * 100:.2f}%**" if exercised else "未测量"
            killed_cell = f"{lang.total_killed:,}" if exercised else "未测量"
            rating_cell = lang.rating if exercised else attestation.NOT_EXERCISED
            lines.append(
                f"| **{lang.language}** | {lang.categories_count} 类别 | {lang.total_surfaces:,} | "
                f"{lang.total_probes:,} | {killed_cell} | {kill_cell} | `{rating_cell}` |"
            )
        lines.append("")

        lines.append("## 3. 威胁全景与漏洞严重性分布 (Threat Landscape & CVSS Heatmap)")
        lines.append("")
        lines.append(f"- **Critical (严重 9.0-10.0)**: **{m.critical_count}** 项 — 立即进入 24 小时应急阻断")
        lines.append(f"- **High (高危 7.0-8.9)**: **{m.high_count}** 项 — 72 小时内发布修复补丁")
        lines.append(f"- **Medium (中危 4.0-6.9)**: **{m.medium_count}** 项 — 纳入下一迭代敏捷交付")
        lines.append(f"- **Low (低危 0.1-3.9)**: **{m.low_count}** 项 — 持续监控与代码加固")
        lines.append("")
        if not a.findings_present:
            lines.append("> 本节数值全为 0：本轮没有任何已裁决缺陷，不存在可展示的威胁分布。")
            lines.append("")

        lines.append("## 4. 已裁决缺陷账本 (Verified Findings Ledger)")
        lines.append("")
        if not a.findings_present:
            lines.append("本轮无已裁决缺陷。**这不构成「安全」结论**：请在审计范围章节核对覆盖边界。")
            lines.append("")
        for f in b.findings:
            lines.append(f"### 📍 [{f.severity}] {f.cve_display} ({f.id}): {f.title}")
            lines.append(f"- **受影响组件 / 生态**: `{f.package_name}` ({f.ecosystem})")
            cwe = f"- **缺陷分类**: `{f.cwe_id}` - {f.cwe_title}" if f.cwe_id else "- **缺陷分类**: 未映射 CWE（源码级逻辑缺陷）"
            lines.append(cwe)
            lines.append(f"- **CVSS v3.1 评分**: **{f.cvss_display}** (`{f.cvss_vector_display}`)")
            lines.append(f"- **处置 SLA 响应周期**: **<{f.sla_hours} 小时**")
            lines.append(f"- **漏洞物理路径**: `{f.target_path or '未记录'}`")
            lines.append(f"- **简要描述**: {f.description or '未记录根因描述'}")
            lines.append("")
            lines.append("#### ⚔️ 独立可复现 PoC 载荷:")
            lines.append("```python")
            lines.append(f.poc_display)
            lines.append("```")
            lines.append("")
            lines.append("#### 🛡️ 建议修复（未验证，未落盘）:")
            lines.append("```diff")
            lines.append(f.patch_display)
            lines.append("```")
            lines.append("")

        lines.append("## 5. 合规性与企业标准对齐 (Compliance & Regulatory Attestation)")
        lines.append("")
        for note in b.compliance_notes:
            lines.append(f"- 📌 {note}")
        lines.append("")

        lines.append("## 6. 安全整改路线图与治理 SLA (Remediation Roadmap)")
        lines.append("")
        lines.append("| 阶段 / 优先级 | 治理行动计划 | 涉及组件范围 | 响应时限 (SLA) |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for r in b.remediation_roadmap:
            lines.append(f"| **{r['priority']}** | {r['action']} | `{r['scope']}` | **{r['phase']}** |")
        lines.append("")

        lines.append("## 7. 权威签名与防伪声明 (Formal Attestation & Sign-off)")
        lines.append("")
        lines.append("```text")
        lines.append("================================================================================")
        # 只有真正满足会签条件时才允许写 CERTIFIED BY；否则只声明出具方。
        if a.countersigned:
            lines.append(f"  CERTIFIED BY: {b.auditor}")
        else:
            lines.append(f"  ISSUED BY   : {b.auditor}")
        lines.append(f"  SEAL HASH    : {b.sha256_seal}")
        lines.append(f"  DATE ISSUED  : {b.created_at}")
        lines.append(f"  STATUS       : {a.status_line}")
        lines.append("================================================================================")
        lines.append("```")
        for blocker in a.gate_blockers:
            lines.append(f"> 🚫 未会签原因: {blocker}")

        return "\n".join(lines)

    def render_html(self) -> str:
        """渲染具备国际商务级设计水准、支持高精度 PDF 打印的 HTML 报告。"""
        b = self.bundle
        m = b.metrics
        a = attestation.from_bundle(b)

        # Generate findings HTML rows
        findings_html = []
        for f in b.findings:
            sev_badge_color = (
                "#dc2626" if f.severity == "CRITICAL"
                else ("#d97706" if f.severity == "HIGH"
                else ("#2563eb" if f.severity == "MEDIUM" else "#64748b"))
            )
            poc_escaped = html.escape(f.poc_display)
            diff_escaped = html.escape(f.patch_display)
            cwe_cell = (
                f"<code>{html.escape(f.cwe_id)}</code> - {html.escape(f.cwe_title)}"
                if f.cwe_id else "未映射 CWE（源码级逻辑缺陷）"
            )

            findings_html.append(f"""
            <div class="finding-card">
                <div class="finding-header">
                    <div class="finding-title-group">
                        <span class="badge" style="background-color: {sev_badge_color}; color: #ffffff;">{f.severity}</span>
                        <span class="finding-id">{html.escape(f.cve_display)}</span>
                        <span class="finding-ghsa">({html.escape(f.id)})</span>
                        <h4 class="finding-title">{html.escape(f.title)}</h4>
                    </div>
                    <div class="cvss-pill">
                        <span class="cvss-label">CVSS v3.1</span>
                        <span class="cvss-value" style="color: {sev_badge_color};">{f.cvss_display}</span>
                    </div>
                </div>

                <div class="finding-meta-grid">
                    <div><strong>Affected Component:</strong> <code>{html.escape(f.package_name)}</code> ({html.escape(f.ecosystem)})</div>
                    <div><strong>CWE Classification:</strong> {cwe_cell}</div>
                    <div><strong>Target Location:</strong> <code>{html.escape(f.target_path or '未记录')}</code></div>
                    <div><strong>Remediation SLA:</strong> <span class="sla-tag">&lt; {f.sla_hours}h</span></div>
                </div>

                <p class="finding-desc">{html.escape(f.description or '未记录根因描述')}</p>

                <div class="code-duel-grid">
                    <div class="code-box red-box">
                        <div class="code-box-title">⚔️ Verified Exploit PoC Payload</div>
                        <pre><code>{poc_escaped}</code></pre>
                    </div>
                    <div class="code-box green-box">
                        <div class="code-box-title">🛡️ 建议修复（未验证，未落盘）</div>
                        <pre><code>{diff_escaped}</code></pre>
                    </div>
                </div>
            </div>
            """)
        if not findings_html:
            findings_html.append(
                '<div class="finding-card"><p class="finding-desc">'
                '本轮无已裁决缺陷。这不构成「安全」结论，请核对审计范围与门禁状态。'
                '</p></div>'
            )

        # Generate languages rows
        lang_rows = []
        for l in b.languages:
            exercised = a.mutation_exercised and l.total_mutations > 0
            html_kill = f"{l.kill_rate * 100:.2f}%" if exercised else "未测量"
            html_rating = html.escape(l.rating if exercised else attestation.NOT_EXERCISED)
            lang_rows.append(f"""
            <tr>
                <td><strong>{l.language}</strong></td>
                <td style="text-align: center;">{l.categories_count}</td>
                <td style="text-align: right; font-family: monospace;">{l.total_surfaces:,}</td>
                <td style="text-align: right; font-family: monospace;">{l.total_probes:,}</td>
                <td style="text-align: right; font-family: monospace;">{l.total_killed:,}</td>
                <td style="text-align: right; font-weight: 700; color: #059669;">{html_kill}</td>
                <td style="text-align: center;"><span class="badge badge-elite">{html_rating}</span></td>
            </tr>
            """)

        # Generate roadmap rows
        roadmap_rows = []
        for r in b.remediation_roadmap:
            roadmap_rows.append(f"""
            <tr>
                <td><span class="priority-pill">{r['priority']}</span></td>
                <td><strong>{html.escape(r['action'])}</strong></td>
                <td><code>{html.escape(r['scope'])}</code></td>
                <td><strong>{html.escape(r['phase'])}</strong></td>
            </tr>
            """)

        # Generate compliance notes
        # 合规条目按中性项目符号呈现：零发现/未实测的说明不能被渲染成
        # 「✓ 通过」（历史缺陷把「本轮无发现」配上对勾，读者只会看到绿色）。
        compliance_li = "".join(
            [f"<li><span class='check-icon'>&bull;</span> {html.escape(c)}</li>" for c in b.compliance_notes]
        )

        template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(b.title)}</title>
    <style>
        :root {{
            --bg-page: #f8fafc;
            --bg-card: #ffffff;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --border-light: #e2e8f0;
            --border-dark: #cbd5e1;
            --primary-blue: #1e3a8a;
            --accent-blue: #2563eb;
            --risk-critical: #dc2626;
            --risk-high: #d97706;
            --risk-medium: #2563eb;
            --risk-low: #64748b;
            --risk-safe: #059669;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            --font-mono: "JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, Courier, monospace;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-page);
            color: var(--text-main);
            font-family: var(--font-sans);
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
            padding: 40px 20px;
        }}

        .container {{
            max-width: 1100px;
            margin: 0 auto;
            background: var(--bg-card);
            border: 1px solid var(--border-light);
            border-radius: 12px;
            box-shadow: 0 4px 20px -2px rgba(15, 23, 42, 0.08);
            overflow: hidden;
        }}

        /* Formal Document Header */
        .doc-header {{
            background: #0f172a;
            color: #ffffff;
            padding: 36px 48px;
            border-bottom: 4px solid var(--accent-blue);
        }}

        .doc-classification {{
            display: inline-block;
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            padding: 4px 10px;
            background: rgba(220, 38, 38, 0.2);
            color: #fca5a5;
            border: 1px solid rgba(220, 38, 38, 0.4);
            border-radius: 4px;
            margin-bottom: 12px;
        }}

        .doc-title {{
            font-size: 26px;
            font-weight: 700;
            line-height: 1.3;
            letter-spacing: -0.5px;
            margin-bottom: 16px;
        }}

        .doc-meta-bar {{
            display: flex;
            flex-wrap: wrap;
            gap: 24px;
            font-size: 13px;
            color: #94a3b8;
            border-top: 1px solid rgba(255, 255, 255, 0.1);
            padding-top: 16px;
        }}

        .doc-meta-bar strong {{
            color: #f1f5f9;
        }}

        /* Section Layout */
        .content-body {{
            padding: 40px 48px;
        }}

        .section {{
            margin-bottom: 48px;
        }}

        .section-title {{
            font-size: 20px;
            font-weight: 700;
            color: #0f172a;
            border-bottom: 2px solid var(--border-light);
            padding-bottom: 8px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .lead-text {{
            font-size: 15px;
            color: #334155;
            line-height: 1.7;
            margin-bottom: 24px;
        }}

        /* KPI Callout Grid */
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 28px;
        }}

        .kpi-card {{
            background: #f8fafc;
            border: 1px solid var(--border-light);
            border-radius: 8px;
            padding: 20px;
            border-left: 4px solid var(--accent-blue);
        }}

        .kpi-card.kpi-emerald {{
            border-left-color: var(--risk-safe);
        }}

        .kpi-card.kpi-crimson {{
            border-left-color: var(--risk-critical);
        }}

        .kpi-label {{
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            margin-bottom: 6px;
        }}

        .kpi-value {{
            font-size: 28px;
            font-weight: 800;
            font-family: var(--font-mono);
            color: var(--text-main);
        }}

        .kpi-subtext {{
            font-size: 12px;
            color: #64748b;
            margin-top: 4px;
        }}

        /* Formal Tables */
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
            margin-bottom: 20px;
        }}

        th {{
            background: #f1f5f9;
            color: #334155;
            font-weight: 600;
            text-align: left;
            padding: 12px 14px;
            border-bottom: 2px solid var(--border-dark);
            font-size: 13px;
        }}

        td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--border-light);
            color: #1e293b;
        }}

        tr:hover td {{
            background: #f8fafc;
        }}

        /* Badges */
        .badge {{
            display: inline-block;
            font-size: 11px;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 4px;
            text-transform: uppercase;
        }}

        .badge-elite {{
            background: #dcfce7;
            color: #15803d;
            border: 1px solid #bbf7d0;
        }}

        .priority-pill {{
            font-size: 11px;
            font-weight: 700;
            padding: 3px 8px;
            background: #fee2e2;
            color: #b91c1c;
            border-radius: 4px;
        }}

        .sla-tag {{
            font-weight: 700;
            color: #dc2626;
            background: #fef2f2;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 12px;
        }}

        /* Finding Card */
        .finding-card {{
            border: 1px solid var(--border-light);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 24px;
            background: #ffffff;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }}

        .finding-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 14px;
        }}

        .finding-title-group {{
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
        }}

        .finding-id {{
            font-family: var(--font-mono);
            font-weight: 700;
            color: #1e293b;
        }}

        .finding-ghsa {{
            font-size: 12px;
            color: #64748b;
        }}

        .finding-title {{
            font-size: 16px;
            font-weight: 700;
            color: #0f172a;
            margin-left: 4px;
        }}

        .cvss-pill {{
            text-align: right;
        }}

        .cvss-label {{
            display: block;
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            color: #64748b;
        }}

        .cvss-value {{
            font-size: 20px;
            font-weight: 800;
            font-family: var(--font-mono);
        }}

        .finding-meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 10px;
            background: #f8fafc;
            padding: 12px 16px;
            border-radius: 6px;
            font-size: 13px;
            margin-bottom: 14px;
            border: 1px solid var(--border-light);
        }}

        .finding-desc {{
            font-size: 14px;
            color: #334155;
            margin-bottom: 16px;
        }}

        /* Code Duel Box */
        .code-duel-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
        }}

        .code-box {{
            border-radius: 6px;
            overflow: hidden;
            font-size: 12px;
            border: 1px solid var(--border-light);
        }}

        .code-box-title {{
            font-size: 12px;
            font-weight: 700;
            padding: 8px 12px;
            border-bottom: 1px solid var(--border-light);
        }}

        .red-box .code-box-title {{
            background: #fee2e2;
            color: #991b1b;
        }}

        .green-box .code-box-title {{
            background: #dcfce7;
            color: #166534;
        }}

        .code-box pre {{
            padding: 12px;
            background: #0f172a;
            color: #f8fafc;
            overflow-x: auto;
            font-family: var(--font-mono);
            line-height: 1.4;
            max-height: 220px;
        }}

        /* Compliance List */
        .compliance-list {{
            list-style: none;
        }}

        .compliance-list li {{
            padding: 10px 14px;
            background: #f8fafc;
            border: 1px solid var(--border-light);
            border-radius: 6px;
            margin-bottom: 8px;
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .check-icon {{
            color: #059669;
            font-weight: 800;
        }}

        /* Seal & Signature Box */
        .seal-box {{
            background: #f8fafc;
            border: 2px dashed #cbd5e1;
            border-radius: 8px;
            padding: 24px;
            text-align: center;
            margin-top: 40px;
        }}

        .seal-title {{
            font-size: 14px;
            font-weight: 800;
            color: #0f172a;
            letter-spacing: 1px;
            text-transform: uppercase;
            margin-bottom: 8px;
        }}

        .seal-hash {{
            font-family: var(--font-mono);
            font-size: 13px;
            color: #2563eb;
            word-break: break-all;
            background: #eff6ff;
            padding: 8px 12px;
            border-radius: 4px;
            display: inline-block;
            margin-bottom: 12px;
        }}

        .seal-footer {{
            font-size: 12px;
            color: #64748b;
        }}

        /* Print Media */
        @media print {{
            body {{
                background: #ffffff;
                padding: 0;
            }}
            .container {{
                box-shadow: none;
                border: none;
                max-width: 100%;
            }}
            .finding-card {{
                page-break-inside: avoid;
            }}
            .code-box pre {{
                background: #f1f5f9;
                color: #0f172a;
                border: 1px solid #cbd5e1;
            }}
        }}

        @media (max-width: 768px) {{
            .code-duel-grid {{
                grid-template-columns: 1fr;
            }}
            .doc-header, .content-body {{
                padding: 24px 20px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Document Header -->
        <header class="doc-header">
            <div class="doc-classification">CONFIDENTIAL // EXECUTIVE DISTRIBUTION ONLY</div>
            <h1 class="doc-title">{html.escape(b.title)}</h1>
            <div class="doc-meta-bar">
                <div><strong>REPORT ID:</strong> {html.escape(b.report_id)}</div>
                <div><strong>DATE:</strong> {html.escape(b.created_at)}</div>
                <div><strong>EVALUATOR:</strong> {html.escape(b.auditor)}</div>
                <div><strong>SCOPE:</strong> {html.escape(b.assessment_scope)}</div>
            </div>
        </header>

        <main class="content-body">
            <!-- Section 1: Executive Summary -->
            <section class="section">
                <h2 class="section-title">1. 执行高管摘要 (Executive Summary)</h2>
                <p class="lead-text">
                    本报告由 <strong>BugX 智能自主安全审计引擎</strong> 依据本轮<strong>实际执行</strong>的验证动作出具。
                    审计范围：<strong>{html.escape(b.assessment_scope)}</strong>，
                    实测覆盖 <strong>{m.total_surfaces:,} 个控制流暴露表面</strong>、<strong>{m.total_targets} 个靶标</strong>。
                    {_html_summary_sentence(m, a)}
                </p>
                {_html_unattested_banner(a)}

                <!-- KPI Grid -->
                <div class="kpi-grid">
                    <div class="kpi-card">
                        <div class="kpi-label">Scouted Attack Surfaces</div>
                        <div class="kpi-value">{m.total_surfaces:,}</div>
                        <div class="kpi-subtext">Across {m.total_targets} Targets</div>
                    </div>
                    <div class="kpi-card kpi-emerald">
                        <div class="kpi-label">Mutation Kill Rate</div>
                        <div class="kpi-value">{a.kill_rate_cell(m.overall_kill_rate)}</div>
                        <div class="kpi-subtext">Rating: {m.rating if a.mutation_exercised else attestation.NOT_EXERCISED} (Threshold &ge; {attestation.KILL_RATE_BASELINE * 100:.0f}%)</div>
                    </div>
                    <div class="kpi-card kpi-emerald">
                        <div class="kpi-label">False Positive Rate</div>
                        <div class="kpi-value">{a.false_positive_verdict()}</div>
                        <div class="kpi-subtext">{'Negative control exercised' if a.negative_control_exercised else 'No negative control exercised'}</div>
                    </div>
                    <div class="kpi-card kpi-crimson">
                        <div class="kpi-label">Adjudicated Findings</div>
                        <div class="kpi-value">{m.total_findings}</div>
                        <div class="kpi-subtext">{m.critical_count} Critical / {m.high_count} High (PoC Ready)</div>
                    </div>
                </div>
            </section>

            <!-- Section 2: Assessment Scope -->
            <section class="section">
                <h2 class="section-title">2. 审计范围与多语言资产清单 (Assessment Scope)</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Ecosystem / Language</th>
                            <th style="text-align: center;">Categories</th>
                            <th style="text-align: right;">Surfaces</th>
                            <th style="text-align: right;">Probes</th>
                            <th style="text-align: right;">Killed Mutations</th>
                            <th style="text-align: right;">Kill Rate</th>
                            <th style="text-align: center;">Grade</th>
                        </tr>
                    </thead>
                    <tbody>
                        {"".join(lang_rows) or '<tr><td colspan="7" style="text-align: center; color: #64748b;">本轮无多语言变异收据，未产出生态清单（不代表已审计）</td></tr>'}
                    </tbody>
                </table>
            </section>

            <!-- Section 3: Verified Findings -->
            <section class="section">
                <h2 class="section-title">3. 已验证漏洞与 CVE 档案账本 (Verified Findings & PoC Ledger)</h2>
                {"".join(findings_html)}
            </section>

            <!-- Section 4: Compliance -->
            <section class="section">
                <h2 class="section-title">4. 国际合规性与权威标准对齐 (Regulatory Attestation)</h2>
                <ul class="compliance-list">
                    {compliance_li}
                </ul>
            </section>

            <!-- Section 5: Remediation Roadmap -->
            <section class="section">
                <h2 class="section-title">5. 安全整改路线图与治理 SLA (Remediation Roadmap)</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Priority</th>
                            <th>Action Plan</th>
                            <th>Scope</th>
                            <th>Target SLA</th>
                        </tr>
                    </thead>
                    <tbody>
                        {"".join(roadmap_rows)}
                    </tbody>
                </table>
            </section>

            <!-- Section 6: Cryptographic Seal -->
            <div class="seal-box">
                <div class="seal-title">{a.seal_title}</div>
                <div class="seal-hash">SHA-256: {b.sha256_seal}</div>
                <div class="seal-footer">
                    {html.escape(_seal_footer(b, a))}
                </div>
            </div>
        </main>
    </div>
</body>
</html>
"""
        return template


def _html_summary_sentence(m: Any, a: attestation.Attestation) -> str:
    """执行摘要正文：只陈述实测事实，未实测的指标一律显式标注。"""
    if m.total_mutations > 0:
        return (
            f"在 <strong>{m.total_mutations:,} 次对抗性变异攻击</strong> 中击杀 "
            f"<strong>{m.total_killed:,} 处缺陷</strong>，综合击杀率 "
            f"<strong>{m.overall_kill_rate * 100:.2f}%</strong>，评级 <strong>{m.rating}</strong>。"
        )
    return (
        "<strong>本轮未执行对抗性变异攻击</strong>：变异击杀率未测量，"
        f"评级记为 {attestation.NOT_EXERCISED}；"
        + (
            "假阳性率与负对照按逐条验证记录实测。"
            if a.negative_control_verdict() == attestation.PASS
            else "假阳性率与负对照放行率亦未测量。"
        )
        + "结论范围以第 2 节审计范围清单为界。"
    )


def _html_unattested_banner(a: attestation.Attestation) -> str:
    """未会签时在报告开头显式声明，避免读者把草稿当合规凭证。"""
    if a.countersigned:
        return ""
    blockers = "".join(
        f"<li>{html.escape(item)}</li>" for item in a.gate_blockers
    )
    detail = f"<ul class='compliance-list'>{blockers}</ul>" if blockers else ""
    return (
        "                <div class=\"finding-card\">\n"
        f"                    <p class=\"finding-desc\"><strong>本报告未获官方会签：</strong>"
        f"{html.escape(a.status_line)}</p>\n"
        f"                    {detail}\n"
        "                </div>"
    )


def _seal_footer(b: ReportDataBundle, a: attestation.Attestation) -> str:
    """印章页脚：会签与否决定措辞，绝不无条件写「有效合规凭证」。"""
    issued = f"Issued by {b.auditor} · Generated at {b.created_at}"
    if a.countersigned:
        return f"Countersigned · {issued} · Valid for Enterprise Compliance Audit"
    return f"NOT COUNTERSIGNED · {issued} · Draft export, not valid for compliance audit"
