"""BugX 现代化高颜值技术交付报告生成器 (Aesthetic Visual Delivery Report Builder).

面向研发工程师、安全极客、架构师与开源社区的高颜值现代科技感报告。
采用 Linear / Vercel 美学风格，具备双层边框 (Double-Bezel)、微荧光彩标、
Bento Grid 卡片栅格墙、红绿攻防双栏对抗视窗与一键复制行动看板。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List

from .models import ReportDataBundle, VulnerabilityFinding, LanguageAuditSummary
from . import attestation

from core.infra.build_info import get_full_version_string


class VisualDeliveryReportBuilder:
    """高颜值现代化技术交付报告构建器。"""

    def __init__(self, bundle: ReportDataBundle):
        self.bundle = bundle

    def render_markdown(self) -> str:
        """渲染具备丰富视觉层级与微排版的 GitHub 风格 Markdown 报告。"""
        b = self.bundle
        m = b.metrics
        a = attestation.from_bundle(b)

        lines: List[str] = []
        lines.append(f"# ⚡ {b.title}")
        lines.append("")
        lines.append("> [!TIP]")
        lines.append(
            f"> **BugX Visual Delivery Report** &bull; Build ID `{get_full_version_string()}` "
            f"&bull; **Status**: `{a.hero_status}`  "
        )
        lines.append(f"> 密码学指纹: `{b.sha256_seal[:16]}...{b.sha256_seal[-16:]}` &bull; 生成于: `{b.created_at}`")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Hero Banner
        lines.append("## 🚀 全景攻防指标看板 (Hero Metrics Wall)")
        lines.append("")
        lines.append("| 🌐 控制流暴露表面 | 🎯 变异击杀率 | 🛡️ 误报率 (FP) | 🧪 负对照放行 | 🚨 已裁决发现 |")
        lines.append("| :---: | :---: | :---: | :---: | :---: |")
        lines.append(
            f"| **`{m.total_surfaces:,}`** | **`{a.kill_rate_cell(m.overall_kill_rate)}`** "
            f"{a.hero_kill_rate_tag} | **`{a.false_positive_verdict()}`** | "
            f"**`{a.negative_control_verdict()}`** | **`{m.total_findings}` 项** |"
        )
        lines.append("")
        if not a.countersigned:
            # status_line 已经聚合了阻断原因（"GATE BLOCKED: ..."），逐条重复
            # 一次是纯噪声；只在阻断原因较多时补明细。
            lines.append(f"> ⚠️ 本报告未获会签：`{a.status_line}`")
            for blocker in a.gate_blockers[1:] if len(a.gate_blockers) > 1 else ():
                lines.append(f"> 🚫 {blocker}")
            lines.append("")

        # Bento Grid Section：无语言数据时不出空标题（Supervisor 来源没有
        # 多语言收据，渲染出来的空 section 只会让人以为数据丢了）。
        lang_count = len(b.languages)
        if lang_count:
            lines.append(f"## 🍱 {lang_count} 大语言全类别渗透矩阵 (Bento Grid Multiverse)")
            lines.append("")
            for lang in b.languages:
                exercised = a.mutation_exercised and lang.total_mutations > 0
                lang_kill = f"{lang.kill_rate * 100:.2f}%" if exercised else "未测量"
                lang_rating = lang.rating if exercised else attestation.NOT_EXERCISED
                bar_len = int(lang.kill_rate * 20) if exercised else 0
                progress_bar = "█" * bar_len + "░" * (20 - bar_len)
                lines.append(f"### 💎 {lang.language} 生态 ({lang.categories_count} 大核心类别)")
                lines.append(f"- **击杀效能**: `[{progress_bar}]` **{lang_kill}** (`{lang_rating}`)")
                lines.append(f"- **表面覆盖**: `{lang.total_surfaces:,}` 暴露点 &bull; 探测 `{lang.total_probes:,}` 次 &bull; 击杀 `{lang.total_killed:,}` 次")
                lines.append("- **覆盖靶标清单**:")
                for cat in lang.categories:
                    cat_kill = f"{cat.kill_rate * 100:.1f}%" if exercised else "未测量"
                    lines.append(f"  * 🎯 **`{cat.target_name}`** (`{cat.repo}`): {cat.category} &rarr; `{cat.cve_id or '未分配 CVE'}` (击杀率 **{cat_kill}**)")
                lines.append("")

        # Attack vs Defense Section
        lines.append("## ⚔️ 攻防对抗对决视窗 (Attack vs. Defense Dual-Pane)")
        lines.append("")
        for f in b.findings:  # 全部已裁决发现，不允许静默截断
            cwe = f"`{f.cwe_id}` {f.cwe_title}" if f.cwe_id else "未映射 CWE"
            lines.append(f"### 📍 [{f.severity}] {f.package_name} &bull; {f.cve_display} / `{f.id}`")
            lines.append(f"> **缺陷类型**: {cwe} &bull; **CVSS v3.1**: `{f.cvss_display}` (`{f.cvss_vector_display}`)")
            lines.append("")
            lines.append("#### 🔴 Exploit Reproduction PoC (恶意载荷)")
            lines.append("```python")
            lines.append(f.poc_display)
            lines.append("```")
            lines.append("")
            lines.append("#### 🟢 建议修复（未验证，未落盘）")
            lines.append("```diff")
            lines.append(f.patch_display)
            lines.append("```")
            lines.append("")

        # Action Center
        lines.append("## 🛠️ 极客行动中心 (Action Center)")
        lines.append("")
        lines.append("- **本地一键复现审计**: `bugx matrix audit --lang all --concurrency 6`")
        lines.append("- **导出标准 GHSA 公告**: `bugx advisory export --cve <CVE-ID> --package <pkg>`（需先具备真实 CVE 编号）")
        lines.append("- **CI/CD 流水线守护**: `python3 scripts/ci_security_gate.py --min-kill-rate 0.85`")
        lines.append("")

        return "\n".join(lines)

    def render_html(self) -> str:
        """渲染 Awwwards 级别现代暗色/科技美学 HTML 交付报告。"""
        b = self.bundle
        m = b.metrics
        a = attestation.from_bundle(b)
        unattested_banner = _unattested_banner(a)

        # Language Bento cards
        bento_cards = []
        for l in b.languages:
            exercised = a.mutation_exercised and l.total_mutations > 0
            bar_width = int(l.kill_rate * 100) if exercised else 0
            bento_kill = f"{l.kill_rate * 100:.2f}%" if exercised else "未测量"
            bento_rating = html.escape(l.rating if exercised else attestation.NOT_EXERCISED)
            pill_tags = "".join([
                f"<span class='bento-target-pill'>{html.escape(c.target_name)}</span>"
                for c in l.categories[:4]
            ])
            bento_cards.append(f"""
            <div class="bento-card">
                <div class="bento-header">
                    <span class="bento-lang">{l.language}</span>
                    <span class="pill pill-neon-green">{bento_rating}</span>
                </div>
                <div class="bento-stat-num">{bento_kill}</div>
                <div class="bento-stat-sub">Kill Rate ({l.total_killed:,} / {l.total_mutations:,})</div>

                <div class="progress-track">
                    <div class="progress-bar" style="width: {bar_width}%;"></div>
                </div>

                <div class="bento-meta-row">
                    <span><strong>{l.total_surfaces:,}</strong> Surfaces</span>
                    <span><strong>{l.total_probes:,}</strong> Probes</span>
                    <span><strong>{l.categories_count}</strong> Targets</span>
                </div>

                <div class="bento-pills-wrap">
                    {pill_tags}
                </div>
            </div>
            """)

        # Dual Pane Exploit vs Defense
        duel_cards = []
        for f in b.findings:
            sev_class = "pill-red" if f.severity in {"CRITICAL", "HIGH"} else "pill-blue"
            poc_code = html.escape(f.poc_display)
            diff_code = html.escape(f.patch_display)
            cwe_text = (
                f"{html.escape(f.cwe_id)} {html.escape(f.cwe_title)}"
                if f.cwe_id else "未映射 CWE"
            )

            duel_cards.append(f"""
            <div class="duel-card">
                <div class="duel-header">
                    <div class="duel-title-group">
                        <span class="pill {sev_class}">{f.severity}</span>
                        <span class="duel-cve">{html.escape(f.cve_display)}</span>
                        <span class="duel-pkg">@ {html.escape(f.package_name)}</span>
                        <span class="duel-cwe">{cwe_text}</span>
                    </div>
                    <div class="duel-cvss">
                        CVSS <strong>{f.cvss_display}</strong>
                    </div>
                </div>

                <div class="split-code-container">
                    <!-- Red Side -->
                    <div class="split-pane pane-red">
                        <div class="pane-label">
                            <span class="indicator red-dot"></span>
                            <span>EXPLOIT REPRODUCTION POC (ATTACK)</span>
                        </div>
                        <pre><code>{poc_code}</code></pre>
                    </div>

                    <!-- Green Side -->
                    <div class="split-pane pane-green">
                        <div class="pane-label">
                            <span class="indicator green-dot"></span>
                            <span>SUGGESTED FIX (UNVERIFIED)</span>
                        </div>
                        <pre><code>{diff_code}</code></pre>
                    </div>
                </div>
            </div>
            """)

        bento_html = "".join(bento_cards) or (
            '<p class="section-desc">本轮无多语言变异收据，未产出生态矩阵（不代表已审计）。</p>'
        )

        template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BugX Aesthetic Visual Delivery Report</title>
    <style>
        :root {{
            --bg-canvas: #090d16;
            --bg-card: #0f172a;
            --bg-card-inner: #131d35;
            --border-card: rgba(255, 255, 255, 0.08);
            --border-highlight: rgba(255, 255, 255, 0.15);
            --text-heading: #f8fafc;
            --text-body: #94a3b8;
            --text-muted: #64748b;
            --neon-green: #10b981;
            --neon-green-glow: rgba(16, 185, 129, 0.25);
            --neon-cyan: #06b6d4;
            --neon-red: #f43f5e;
            --neon-red-glow: rgba(244, 63, 94, 0.25);
            --neon-purple: #a855f7;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", sans-serif;
            --font-mono: "JetBrains Mono", "Fira Code", monospace;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-canvas);
            color: var(--text-body);
            font-family: var(--font-sans);
            line-height: 1.5;
            padding: 40px 24px;
            -webkit-font-smoothing: antialiased;
        }}

        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}

        /* Glow Top Hero */
        .hero-section {{
            position: relative;
            padding: 48px 40px;
            background: linear-gradient(180deg, rgba(30, 41, 59, 0.6) 0%, rgba(15, 23, 42, 0.8) 100%);
            border: 1px solid var(--border-card);
            border-radius: 16px;
            margin-bottom: 36px;
            box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.7);
            overflow: hidden;
        }}

        .hero-section::before {{
            content: "";
            position: absolute;
            top: -50px;
            left: 20%;
            width: 400px;
            height: 200px;
            background: radial-gradient(circle, rgba(16, 185, 129, 0.15) 0%, transparent 70%);
            pointer-events: none;
        }}

        .hero-badge-bar {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 16px;
        }}

        .pill {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            padding: 4px 10px;
            border-radius: 9999px;
            font-family: var(--font-mono);
        }}

        .pill-neon-green {{
            background: rgba(16, 185, 129, 0.15);
            color: var(--neon-green);
            border: 1px solid rgba(16, 185, 129, 0.4);
            box-shadow: 0 0 12px var(--neon-green-glow);
        }}

        .pill-red {{
            background: rgba(244, 63, 94, 0.15);
            color: var(--neon-red);
            border: 1px solid rgba(244, 63, 94, 0.4);
        }}

        .pill-blue {{
            background: rgba(6, 182, 212, 0.15);
            color: var(--neon-cyan);
            border: 1px solid rgba(6, 182, 212, 0.4);
        }}

        .hero-title {{
            font-size: 32px;
            font-weight: 800;
            color: var(--text-heading);
            letter-spacing: -0.5px;
            margin-bottom: 12px;
        }}

        .hero-subtitle {{
            font-size: 15px;
            color: var(--text-body);
            max-width: 800px;
            margin-bottom: 32px;
            line-height: 1.6;
        }}

        /* Hero Big Numbers Grid */
        .hero-metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
        }}

        .metric-bezel-card {{
            background: var(--bg-card-inner);
            border: 1px solid var(--border-card);
            border-radius: 12px;
            padding: 20px 24px;
            position: relative;
            box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.05);
        }}

        .metric-title {{
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-muted);
            margin-bottom: 8px;
        }}

        .metric-huge-num {{
            font-size: 36px;
            font-weight: 800;
            font-family: var(--font-mono);
            color: #ffffff;
            line-height: 1.1;
            margin-bottom: 6px;
        }}

        .metric-huge-num.accent-green {{
            color: var(--neon-green);
            text-shadow: 0 0 20px var(--neon-green-glow);
        }}

        .metric-desc {{
            font-size: 12px;
            color: var(--text-muted);
        }}

        /* Section Headers */
        .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            margin-bottom: 24px;
            margin-top: 48px;
        }}

        .section-title {{
            font-size: 22px;
            font-weight: 700;
            color: var(--text-heading);
            letter-spacing: -0.3px;
        }}

        .section-desc {{
            font-size: 13px;
            color: var(--text-muted);
            margin-top: 4px;
        }}

        /* Bento Grid */
        .bento-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 20px;
        }}

        .bento-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-card);
            border-radius: 14px;
            padding: 24px;
            transition: transform 0.2s ease, border-color 0.2s ease;
        }}

        .bento-card:hover {{
            border-color: var(--border-highlight);
            transform: translateY(-2px);
        }}

        .bento-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }}

        .bento-lang {{
            font-size: 18px;
            font-weight: 700;
            color: #ffffff;
        }}

        .bento-stat-num {{
            font-size: 28px;
            font-weight: 800;
            font-family: var(--font-mono);
            color: var(--neon-green);
        }}

        .bento-stat-sub {{
            font-size: 12px;
            color: var(--text-muted);
            margin-bottom: 16px;
        }}

        .progress-track {{
            width: 100%;
            height: 6px;
            background: rgba(255, 255, 255, 0.06);
            border-radius: 9999px;
            overflow: hidden;
            margin-bottom: 16px;
        }}

        .progress-bar {{
            height: 100%;
            background: linear-gradient(90deg, #10b981 0%, #06b6d4 100%);
            border-radius: 9999px;
        }}

        .bento-meta-row {{
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            color: var(--text-muted);
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            padding-top: 12px;
            margin-bottom: 14px;
        }}

        .bento-meta-row strong {{
            color: #e2e8f0;
            font-family: var(--font-mono);
        }}

        .bento-pills-wrap {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}

        .bento-target-pill {{
            font-size: 11px;
            font-family: var(--font-mono);
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: #cbd5e1;
            padding: 2px 8px;
            border-radius: 4px;
        }}

        /* Duel Window (Attack vs Defense) */
        .duel-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-card);
            border-radius: 14px;
            padding: 24px;
            margin-bottom: 24px;
        }}

        .duel-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 18px;
            flex-wrap: wrap;
            gap: 12px;
        }}

        .duel-title-group {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }}

        .duel-cve {{
            font-family: var(--font-mono);
            font-weight: 700;
            color: #ffffff;
            font-size: 15px;
        }}

        .duel-pkg {{
            color: var(--neon-cyan);
            font-family: var(--font-mono);
            font-size: 13px;
        }}

        .duel-cwe {{
            font-size: 12px;
            color: var(--text-muted);
        }}

        .duel-cvss {{
            font-family: var(--font-mono);
            font-size: 13px;
            color: var(--text-heading);
            background: rgba(255, 255, 255, 0.05);
            padding: 4px 10px;
            border-radius: 6px;
            border: 1px solid var(--border-card);
        }}

        .duel-cvss strong {{
            color: var(--neon-red);
            font-size: 15px;
        }}

        .split-code-container {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }}

        .split-pane {{
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--border-card);
            background: #090d16;
        }}

        .pane-red {{
            border-color: rgba(244, 63, 94, 0.3);
        }}

        .pane-green {{
            border-color: rgba(16, 185, 129, 0.3);
        }}

        .pane-label {{
            padding: 8px 14px;
            font-size: 11px;
            font-weight: 700;
            font-family: var(--font-mono);
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(255, 255, 255, 0.02);
            border-bottom: 1px solid var(--border-card);
        }}

        .pane-red .pane-label {{
            color: #fca5a5;
        }}

        .pane-green .pane-label {{
            color: #6ee7b7;
        }}

        .indicator {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }}

        .red-dot {{
            background: var(--neon-red);
            box-shadow: 0 0 8px var(--neon-red-glow);
        }}

        .green-dot {{
            background: var(--neon-green);
            box-shadow: 0 0 8px var(--neon-green-glow);
        }}

        .split-pane pre {{
            padding: 14px;
            font-family: var(--font-mono);
            font-size: 12px;
            line-height: 1.5;
            color: #e2e8f0;
            overflow-x: auto;
            max-height: 240px;
        }}

        /* Action Center */
        .action-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 16px;
            margin-top: 20px;
        }}

        .action-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-card);
            border-radius: 12px;
            padding: 20px;
        }}

        .action-title {{
            font-size: 14px;
            font-weight: 700;
            color: #ffffff;
            margin-bottom: 8px;
        }}

        .action-cmd {{
            font-family: var(--font-mono);
            font-size: 12px;
            background: #090d16;
            padding: 10px 12px;
            border-radius: 6px;
            color: var(--neon-cyan);
            border: 1px solid var(--border-card);
            overflow-x: auto;
            white-space: nowrap;
        }}

        /* Footer */
        .footer-seal {{
            margin-top: 60px;
            padding: 24px;
            text-align: center;
            border-top: 1px solid var(--border-card);
            font-size: 12px;
            color: var(--text-muted);
            font-family: var(--font-mono);
        }}

        @media (max-width: 860px) {{
            .split-code-container {{
                grid-template-columns: 1fr;
            }}
            .hero-section {{
                padding: 28px 20px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Hero Section -->
        <header class="hero-section">
            <div class="hero-badge-bar">
                <span class="pill pill-neon-green">{html.escape(a.hero_status)}</span>
                <span class="pill pill-blue">{len(b.languages)} LANG / {m.total_targets} TARGETS</span>
                <span class="pill pill-red">FP: {html.escape(a.false_positive_verdict())}</span>
            </div>
            <h1 class="hero-title">{html.escape(b.title)}</h1>
            <p class="hero-subtitle">
                BugX 自动化攻防与控制流突防交付报告。审计范围：
                {html.escape(b.assessment_scope)}。报告仅呈现本轮实测数据，
                未执行的动作与未测量的指标均显式标注。
            </p>
            {unattested_banner}

            <!-- Metrics Wall -->
            <div class="hero-metrics-grid">
                <div class="metric-bezel-card">
                    <div class="metric-title">Control Flow Surfaces</div>
                    <div class="metric-huge-num">{m.total_surfaces:,}</div>
                    <div class="metric-desc">Scouted across {m.total_targets} targets</div>
                </div>
                <div class="metric-bezel-card">
                    <div class="metric-title">Mutation Kill Rate</div>
                    <div class="metric-huge-num accent-green">{html.escape(a.kill_rate_cell(m.overall_kill_rate))}</div>
                    <div class="metric-desc">{html.escape(a.kill_rate_verdict(m.overall_kill_rate))}</div>
                </div>
                <div class="metric-bezel-card">
                    <div class="metric-title">Adjudicated Findings</div>
                    <div class="metric-huge-num">{m.total_findings}</div>
                    <div class="metric-desc">{m.critical_count} Critical / {m.high_count} High</div>
                </div>
                <div class="metric-bezel-card">
                    <div class="metric-title">False Positive Rate</div>
                    <div class="metric-huge-num accent-green">{a.false_positive_verdict()}</div>
                    <div class="metric-desc">{html.escape(a.negative_control_verdict())} negative control</div>
                </div>
            </div>
        </header>

        <!-- Bento Grid Multiverse -->
        <section>
            <div class="section-header">
                <div>
                    <h2 class="section-title">🍱 多语言生态渗透矩阵 (Bento Grid Multiverse)</h2>
                    <p class="section-desc">{html.escape(_language_list(b))}</p>
                </div>
            </div>

            <div class="bento-grid">
                {bento_html}
            </div>
        </section>

        <!-- Duel Window (Attack vs Defense) -->
        <section>
            <div class="section-header">
                <div>
                    <h2 class="section-title">⚔️ 攻防对抗对决视窗 (Attack vs. Defense Dual-Pane)</h2>
                    <p class="section-desc">左栏为已验证可复现 PoC 载荷；右栏为**未经验证**的修复建议，尚未落盘</p>
                </div>
            </div>

            <div class="duel-wall">
                {"".join(duel_cards)}
            </div>
        </section>

        <!-- Action Center -->
        <section>
            <div class="section-header">
                <div>
                    <h2 class="section-title">🛠️ 极客行动中心 (Action Center)</h2>
                    <p class="section-desc">即插即用的 CLI 指令与 CI/CD 自动化集成入口</p>
                </div>
            </div>

            <div class="action-grid">
                <div class="action-card">
                    <div class="action-title">多语种攻防矩阵复现</div>
                    <div class="action-cmd">bugx matrix audit --lang all --concurrency 6</div>
                </div>
                <div class="action-card">
                    <div class="action-title">导出标准 GHSA 安全公告</div>
                    <div class="action-cmd">bugx advisory export --cve &lt;CVE-ID&gt; --package &lt;pkg&gt;</div>
                </div>
                <div class="action-card">
                    <div class="action-title">CI/CD 阻断门禁自检</div>
                    <div class="action-cmd">python3 scripts/ci_security_gate.py</div>
                </div>
            </div>
        </section>

        <!-- Footer -->
        <footer class="footer-seal">
            DIGITAL SHA-256 SEAL: {b.sha256_seal}<br>
            {html.escape(a.status_line)} &bull; BugX Autonomous Security Intelligence
            &bull; Build {get_full_version_string()} &bull; {b.created_at}
        </footer>
    </div>
</body>
</html>
"""
        return template


def _language_list(bundle: ReportDataBundle) -> str:
    """语言清单来自实测收据，不再硬编码「Python, Go, Rust, C/C++, JS/TS, Java, PHP」。"""
    names = [lang.language for lang in bundle.languages]
    return "全类别靶标攻防击杀看板：" + (", ".join(names) if names else "本轮无语言级收据")


def _unattested_banner(a: attestation.Attestation) -> str:
    """未会签时在视觉报告顶部给出显式提示，避免被当成合规凭证转发。"""
    if a.countersigned:
        return ""
    blockers = "".join(
        f"<li>{html.escape(item)}</li>" for item in a.gate_blockers
    )
    detail = f"<ul>{blockers}</ul>" if blockers else ""
    return (
        f'            <div class="metric-bezel-card" style="margin-top:16px;">\n'
        f'                <div class="metric-desc"><strong>NOT COUNTERSIGNED</strong>：'
        f'{html.escape(a.status_line)}</div>{detail}\n'
        '            </div>'
    )
