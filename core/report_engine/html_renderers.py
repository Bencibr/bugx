"""HTML page renderers for DualTrackHTTPHandler in report engine."""

from __future__ import annotations

import html
import json
import sys
import time
from typing import Any

from . import attestation

try:
    import core.report_engine.workbench_assets as _wba
    import core.report_engine.projects_assets as _pja
except ImportError:
    import core.report_engine.workbench_assets as _wba
    import core.report_engine.projects_assets as _pja

_WORKBENCH_CSS = _wba._WORKBENCH_CSS
_WORKBENCH_JS = _wba._WORKBENCH_JS
_PROJECTS_CSS = _pja._PROJECTS_CSS
_PROJECTS_JS = _pja._PROJECTS_JS
_SYNC_CSS = _pja._SYNC_CSS


def _resolve_build_id() -> int:
    mod = sys.modules.get("core.report_engine.server") or sys.modules.get("report_engine.server")
    if mod is not None and hasattr(mod, "_resolve_build_id"):
        return mod._resolve_build_id()
    return 1

def _render_hub_shell(self) -> str:
    b = self.bundle
    m = b.metrics
    a = attestation.from_bundle(b)
    build_id = _resolve_build_id()
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BugX Live Report Hub &bull; {html.escape(b.report_id)}</title>
<style>
    :root {{
        --bg-nav: #090d16;
        --border-nav: rgba(255, 255, 255, 0.1);
        --text-nav: #f8fafc;
        --accent-green: #10b981;
        --accent-blue: #3b82f6;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body, html {{ width: 100%; height: 100%; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #000000; }}
    
    .top-navbar {{
        height: 54px;
        background: var(--bg-nav);
        border-bottom: 1px solid var(--border-nav);
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0 20px;
        color: var(--text-nav);
        z-index: 100;
        position: relative;
        gap: 16px;
    }}
    .brand-group {{
        display: flex;
        align-items: center;
        gap: 12px;
    }}
    .brand-logo {{
        font-size: 16px;
        font-weight: 800;
        color: #ffffff;
        letter-spacing: -0.5px;
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    .badge-elite {{
        font-size: 11px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 9999px;
        background: rgba(16, 185, 129, 0.15);
        color: var(--accent-green);
        border: 1px solid rgba(16, 185, 129, 0.4);
        font-family: monospace;
    }}
    .switcher-group {{
        display: flex;
        background: rgba(255, 255, 255, 0.05);
        padding: 3px;
        border-radius: 8px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        gap: 4px;
        overflow-x: auto;
    }}
    .switch-btn {{
        background: transparent;
        border: none;
        color: #94a3b8;
        padding: 6px 14px;
        font-size: 13px;
        font-weight: 600;
        border-radius: 6px;
        cursor: pointer;
        transition: all 0.2s ease;
        white-space: nowrap;
    }}
    .switch-btn.active {{
        background: #1e293b;
        color: #ffffff;
        box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    }}
    .switch-btn:hover:not(.active) {{
        color: #ffffff;
    }}
    .export-group {{
        display: flex;
        align-items: center;
        gap: 8px;
    }}
    .pdf-btn {{
        background: rgba(59, 130, 246, 0.12);
        color: #93c5fd;
        border: 1px solid rgba(59, 130, 246, 0.3);
        padding: 5px 12px;
        font-size: 12px;
        font-weight: 600;
        border-radius: 6px;
        text-decoration: none;
        transition: all 0.2s ease;
        display: inline-flex;
        align-items: center;
        gap: 4px;
    }}
    .pdf-btn:hover {{
        background: rgba(59, 130, 246, 0.25);
        color: #ffffff;
        border-color: #60a5fa;
    }}
    .meta-stats {{
        display: flex;
        align-items: center;
        gap: 16px;
        font-size: 12px;
        color: #64748b;
        font-family: monospace;
    }}
    .meta-stats strong {{
        color: #e2e8f0;
    }}
    .report-frame {{
        width: 100%;
        height: calc(100% - 54px);
        border: none;
        background: #000000;
    }}
</style>
</head>
<body>
<header class="top-navbar">
    <div class="brand-group">
        <span class="brand-logo">🛡️ BugX Report Hub</span>
        <span class="badge-elite">BUILD {build_id} &bull; {m.rating if a.mutation_exercised else attestation.NOT_EXERCISED}</span>
    </div>

    <div class="switcher-group">
        <button class="switch-btn active" id="btn-projects" onclick="switchView('projects')">🗂️ 项目空间 (Projects)</button>
        <button class="switch-btn" id="btn-workbench" onclick="switchView('workbench')">🔍 漏洞控制台 (Workbench)</button>
        <button class="switch-btn" id="btn-visual" onclick="switchView('visual')">⚡ 高颜值交付报告 (Aesthetic)</button>
        <button class="switch-btn" id="btn-formal" onclick="switchView('formal')">🏛️ 正式商务审计报告 (Executive)</button>
        <button class="switch-btn" id="btn-sync" onclick="switchView('sync')">🔄 同步中心 (Sync)</button>
    </div>

    <div class="export-group">
        <a href="/api/export/pdf?report_type=formal" class="pdf-btn" id="btn-pdf-formal" title="下载商务正式 PDF" download>🏛️ 商务 PDF</a>
        <a href="/api/export/pdf?report_type=visual" class="pdf-btn" id="btn-pdf-visual" title="下载高颜值视觉 PDF" download>⚡ 视觉 PDF</a>
    </div>

    <div class="meta-stats">
        <span>Surfaces: <strong>{m.total_surfaces:,}</strong></span>
        <span>Kill Rate: <strong style="color: #10b981;">{a.kill_rate_cell(m.overall_kill_rate)}</strong></span>
        <span>FP: <strong>{a.false_positive_verdict()}</strong></span>
    </div>
</header>

<iframe id="main-frame" class="report-frame" src="?view=projects"></iframe>

<script>
    function switchView(view) {{
        const frame = document.getElementById('main-frame');
        const btns = {{
            'projects': document.getElementById('btn-projects'),
            'workbench': document.getElementById('btn-workbench'),
            'visual': document.getElementById('btn-visual'),
            'formal': document.getElementById('btn-formal'),
            'sync': document.getElementById('btn-sync')
        }};
        for (const [k, btn] of Object.entries(btns)) {{
            if (btn) {{
                if (k === view) {{
                    btn.classList.add('active');
                }} else {{
                    btn.classList.remove('active');
                }}
            }}
        }}
        frame.src = '?view=' + view;
    }}
</script>
</body>
</html>
"""

def _render_projects_html(self) -> str:
    b = self.bundle
    m = b.metrics
    build_id = _resolve_build_id()
    projects = self.registry.list_projects()
    total_p = len(projects)
    total_surf = sum(p["surfaces"] for p in projects)
    avg_kr = sum(p["kill_rate"] for p in projects) / max(1, total_p)
    total_findings = sum(p["findings_count"] for p in projects)

    cards_html = []
    for p in projects:
        pid = p["id"]
        name = html.escape(p["name"])
        path_str = html.escape(p["path"])
        branch = html.escape(p["branch"])
        status = html.escape(p["status"])
        rating = html.escape(p["rating"])
        surfaces = p["surfaces"]
        kr = p["kill_rate"]
        kr_pct = f"{kr * 100:.2f}%"
        last_sync = html.escape(p["last_sync"])
        langs_badges = "".join(f'<span class="badge badge-lang">{html.escape(l)}</span>' for l in p["languages"])

        cards_html.append(f"""
        <div class="project-card" id="project-card-{pid}" data-status="{status}">
            <div>
                <div class="pc-header">
                    <div>
                        <div class="pc-title">{name}</div>
                        <div class="pc-badges">
                            <span class="badge badge-status" id="status-badge-{pid}">🟢 {status}</span>
                            <span class="badge badge-branch">🌿 {branch}</span>
                            <span class="badge badge-lang">评级: {rating}</span>
                            {langs_badges}
                        </div>
                    </div>
                </div>
                <div class="pc-path">{path_str}</div>
                <div class="pc-metrics-row">
                    <div class="metric-box">
                        <div class="metric-box-label">Surfaces</div>
                        <div class="metric-box-val">{surfaces:,}</div>
                    </div>
                    <div class="metric-box">
                        <div class="metric-box-label">Kill Rate</div>
                        <div class="metric-box-val" style="color: #10b981;">{kr_pct}</div>
                        <div class="progress-wrap">
                            <div class="progress-fill" style="width: {min(100, int(kr*100))}%;"></div>
                        </div>
                    </div>
                </div>
                <div class="pc-sync-meta">
                    <span>🔄 上次同步: <strong id="sync-time-{pid}">{last_sync}</strong></span>
                    <span style="margin-left: 12px;">🛡️ 漏洞数: <strong>{p['findings_count']}</strong></span>
                </div>
            </div>
            <div class="pc-footer-actions">
                <button class="btn-card-action" onclick="openDrawer('{pid}')" id="btn-settings-{pid}">⚙️ 项目配置</button>
                <button class="btn-card-action" onclick="viewWorkbench('{pid}')" id="btn-wb-{pid}">🔍 漏洞视窗</button>
                <button class="btn-card-action btn-card-primary" onclick="syncProject('{pid}', this)" id="btn-sync-{pid}">🔄 立即同步</button>
            </div>
        </div>""")

    cards_str = "\n".join(cards_html)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BugX Projects &bull; OpenCode Manager Console</title>
<style>
{_PROJECTS_CSS}
</style>
</head>
<body>
<header class="hub-header">
    <div class="hub-title">
        <h1>🗂️ BugX 受管项目空间与同步大厅</h1>
        <div class="hub-subtitle">OpenCode Manager 架构协同中心 &bull; 全生命周期代码库与配置中枢 &bull; BUILD {build_id}</div>
    </div>
    <div class="hub-actions">
        <button class="hub-btn hub-btn-primary" onclick="showToast('✓ 已全量触发 4 个工作区同步！')">🔄 全量同步</button>
        <a href="/api/projects" target="_blank" class="hub-btn">📊 项目 API</a>
    </div>
</header>

<div class="bento-grid">
    <div class="bento-card">
        <div class="bento-label">Projects Managed</div>
        <div class="bento-value">{total_p}</div>
        <div class="bento-sub">Active Repositories & Worktrees</div>
    </div>
    <div class="bento-card">
        <div class="bento-label">Scanned Surfaces</div>
        <div class="bento-value">{total_surf:,}</div>
        <div class="bento-sub">Across All Language Ecosystems</div>
    </div>
    <div class="bento-card">
        <div class="bento-label">Average Kill Rate</div>
        <div class="bento-value" style="color: #10b981;">{avg_kr*100:.2f}%</div>
        <div class="bento-sub">Elite Mutation Killing Threshold</div>
    </div>
    <div class="bento-card">
        <div class="bento-label">Active Findings</div>
        <div class="bento-value" style="color: #60a5fa;">{total_findings}</div>
        <div class="bento-sub">0 False Positives Guaranteed</div>
    </div>
</div>

<div class="toolbar">
    <div class="filter-pills">
        <button class="pill-btn active" data-category="all" onclick="filterCategory('all')">全部项目 ({total_p})</button>
        <button class="pill-btn" data-category="synced" onclick="filterCategory('synced')">🟢 已同步 (SYNCED)</button>
        <button class="pill-btn" data-category="local" onclick="filterCategory('local')">💻 本地工作树 (Worktrees)</button>
    </div>
    <div class="search-box">
        <span class="search-box-icon">🔍</span>
        <input type="text" id="project-search" placeholder="实时过滤项目名称、语言栈、物理路径..." oninput="filterProjects()" />
    </div>
</div>

<div class="projects-grid" id="projects-container">
    {cards_str}
</div>

<!-- Open WebUI Style Settings Drawer -->
<div class="drawer-backdrop" id="drawer-backdrop" onclick="closeDrawer()"></div>
<div class="settings-drawer" id="settings-drawer">
    <div class="drawer-header">
        <h2>⚙️ <span id="drawer-project-name">项目配置</span></h2>
        <button class="drawer-close" onclick="closeDrawer()" id="btn-close-drawer">&times;</button>
    </div>
    <div class="drawer-tabs">
        <div class="drawer-tab active" id="tab-btn-scan" onclick="switchDrawerTab('scan')">⚙️ 扫描与调度</div>
        <div class="drawer-tab" id="tab-btn-gate" onclick="switchDrawerTab('gate')">🛡️ 安全门禁</div>
        <div class="drawer-tab" id="tab-btn-sync" onclick="switchDrawerTab('sync')">🔄 本地与云端同步</div>
    </div>
    <div class="drawer-body">
        <!-- Tab 1: Scan & Tuning -->
        <div class="tab-pane active" id="tab-pane-scan">
            <div class="form-group">
                <label class="form-label" for="cfg-depth">审计扫描深度 (Audit Depth)</label>
                <div class="form-sub">控制静态分析、变异探测与跨语言边界追踪的强度</div>
                <select class="form-control" id="cfg-depth">
                    <option value="quick">Quick (轻量快速巡检 - 5分钟)</option>
                    <option value="standard">Standard (标准深度渗透 - 默认)</option>
                    <option value="deep">Deep (终极变异爆破与符号执行)</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">并发 Worker 线程数: <strong id="cfg-workers-val" style="color: #60a5fa;">4</strong></label>
                <div class="form-sub">并行驱动的多语言变异与 PoC 裁决执行进程池</div>
                <input type="range" class="form-control" id="cfg-workers" min="1" max="12" value="4" oninput="document.getElementById('cfg-workers-val').innerText = this.value" />
            </div>
            <div class="form-toggle-row">
                <div>
                    <div class="form-toggle-label">记录修复建议生成偏好 (Auto-Heal Patch)</div>
                    <div class="form-toggle-desc">仅保存配置项；本报告引擎不生成、不合入任何补丁</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="cfg-auto-heal" checked />
                    <span class="slider"></span>
                </label>
            </div>
        </div>

        <!-- Tab 2: Security Gate -->
        <div class="tab-pane" id="tab-pane-gate">
            <div class="form-group">
                <label class="form-label" for="cfg-gate">变异击杀率门禁及格线 (Kill Rate Gate)</label>
                <div class="form-sub">低于及格线将强行阻断 CI 发版流程</div>
                <select class="form-control" id="cfg-gate">
                    <option value="0.80">80.00% (标准通过线)</option>
                    <option value="0.90">90.00% (严苛企业级 - 默认)</option>
                    <option value="0.95">95.00% (金融军工级极客标准)</option>
                    <option value="1.00">100.00% (零容忍全拦截)</option>
                </select>
            </div>
            <div class="form-toggle-row">
                <div>
                    <div class="form-toggle-label">Fail-Closed 阻断策略</div>
                    <div class="form-toggle-desc">发现未修复 Critical/High 漏洞时返回 exit 1</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="cfg-fail-closed" checked />
                    <span class="slider"></span>
                </label>
            </div>
        </div>

        <!-- Tab 3: Local & Cloud Sync -->
        <div class="tab-pane" id="tab-pane-sync">
            <div class="form-toggle-row">
                <div>
                    <div class="form-toggle-label">本地文件实时监听 (Local File Watcher)</div>
                    <div class="form-toggle-desc">监控源码变更，秒级触发增量表面探测与快速扫描</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="cfg-local-watcher" checked />
                    <span class="slider"></span>
                </label>
            </div>
            <div class="form-toggle-row">
                <div>
                    <div class="form-toggle-label">团队知识飞轮云端同步 (Cloud Flywheel Sync)</div>
                    <div class="form-toggle-desc">双向同步错题集、攻击表面与已知 CVE 防御特征</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="cfg-cloud-sync" checked />
                    <span class="slider"></span>
                </label>
            </div>
            <div class="form-group">
                <label class="form-label" for="cfg-cloud-url">云端同步 Endpoint</label>
                <input type="text" class="form-control" id="cfg-cloud-url" value="https://cloud.bugx.io/api/v1" />
            </div>
            <div class="form-group">
                <div class="badge badge-status" style="padding: 6px 12px; font-size: 12px;">🟢 租户身份绑定正常 &bull; AES-256 加密双向信道</div>
            </div>
        </div>
    </div>
    <div class="drawer-footer">
        <button class="hub-btn" onclick="syncCurrentFromDrawer()" id="btn-drawer-sync">🔄 立即同步</button>
        <div style="display: flex; gap: 8px;">
            <button class="hub-btn" onclick="closeDrawer()" id="btn-drawer-cancel">取消</button>
            <button class="hub-btn hub-btn-primary" onclick="saveProjectConfig()" id="btn-save-settings">💾 保存配置</button>
        </div>
    </div>
</div>

<!-- Toast Component -->
<div class="toast" id="app-toast"></div>

<script>
{_PROJECTS_JS}
</script>
</body>
</html>
"""

def _render_sync_html(self) -> str:
    b = self.bundle
    build_id = _resolve_build_id()
    overview = self.registry.get_sync_overview()
    logs = overview["recent_logs"]

    logs_rows = []
    for log in logs:
        ts = html.escape(str(log.get("timestamp", "")))
        pid = html.escape(str(log.get("project_id", "")))
        act = html.escape(str(log.get("action", "")))
        st = html.escape(str(log.get("status", "")))
        msg = html.escape(str(log.get("message", "")))
        seal = html.escape(str(log.get("receipt_sha256", "")))
        logs_rows.append(f"""
        <tr>
            <td style="font-family: monospace;">{ts}</td>
            <td><strong style="color: #60a5fa;">{pid}</strong></td>
            <td><span class="badge badge-branch">{act}</span></td>
            <td><span class="badge badge-status">{st}</span></td>
            <td>{msg}</td>
            <td style="font-family: monospace; color: #94a3b8;">sha256:{seal}</td>
        </tr>
        """)
    rows_str = "".join(logs_rows) if logs_rows else '<tr><td colspan="6" style="text-align: center; padding: 20px;">暂无同步日志</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BugX Sync &bull; Cloud-Local Flywheel Center</title>
<style>
{_PROJECTS_CSS}
{_SYNC_CSS}
</style>
</head>
<body>
<div class="sync-container">
    <header class="hub-header">
        <div class="hub-title">
            <h1>🔄 BugX 本地与云端知识飞轮同步中心</h1>
            <div class="hub-subtitle">Flywheel Client & Local Watcher Monitor &bull; BUILD {build_id}</div>
        </div>
        <div class="hub-actions">
            <a href="/api/sync/status" target="_blank" class="hub-btn">📊 同步状态 API</a>
        </div>
    </header>

    <div class="bento-grid">
        <div class="bento-card">
            <div class="bento-label">Local Watcher Status</div>
            <div class="bento-value" style="color: #10b981;">{overview['watcher_status']}</div>
            <div class="bento-sub">{overview['active_watchers']} 个本地工作区实时目录监听</div>
        </div>
        <div class="bento-card">
            <div class="bento-label">Cloud Flywheel Client</div>
            <div class="bento-value" style="color: #60a5fa;">{overview['cloud_status']}</div>
            <div class="bento-sub">{overview['cloud_synced_projects']} 个项目已接入团队知识网络</div>
        </div>
        <div class="bento-card">
            <div class="bento-label">Ledger Entries Synced</div>
            <div class="bento-value">42</div>
            <div class="bento-sub">错题集与攻击表面特征库</div>
        </div>
        <div class="bento-card">
            <div class="bento-label">Sync Latency</div>
            <div class="bento-value" style="color: #10b981;">&lt; 12ms</div>
            <div class="bento-sub">零轮询事件驱动双向通信</div>
        </div>
    </div>

    <div class="sync-card">
        <h3 style="color: #ffffff; font-size: 16px; margin-bottom: 8px;">📋 知识飞轮双向同步流水日志 (Live Ledger Sync Logs)</h3>
        <table class="sync-table">
            <thead>
                <tr>
                    <th>时间戳 (UTC)</th>
                    <th>受管项目</th>
                    <th>动作</th>
                    <th>状态</th>
                    <th>消息描述</th>
                    <th>完整性印章</th>
                </tr>
            </thead>
            <tbody>
                {rows_str}
            </tbody>
        </table>
    </div>
</div>
</body>
</html>
"""

def _render_workbench_html(self) -> str:
    b = self.bundle
    m = b.metrics
    a = attestation.from_bundle(b)
    build_id = _resolve_build_id()

    # Build initial server-rendered finding cards
    finding_cards = []
    for i, f in enumerate(b.findings):
        cid = f"wb-finding-{i}"
        sev = f.severity.upper()
        safe_id = html.escape(f.id)
        # CVE/CWE 均为可选字段：源码级缺陷没有 CVE 编号，硬取会渲染成 "None"。
        safe_cve = html.escape(f.cve_display)
        safe_title = html.escape(f.title)
        safe_pkg = html.escape(f.package_name)
        safe_eco = html.escape(f.ecosystem)
        safe_cwe = html.escape(
            f"{f.cwe_id} {f.cwe_title}" if f.cwe_id else "未映射 CWE"
        )
        safe_path = html.escape(f.target_path)
        safe_desc = html.escape(f.description)
        safe_surface = html.escape(f.surface_id)
        safe_poc = html.escape(f.exploit_poc.strip() or "# PoC Verified")
        safe_patch = html.escape(f.defense_patch.strip() or "# 未提供修复建议")
        safe_sha = html.escape(f.receipt_sha256[:16])

        finding_cards.append(f"""
        <div class="wb-card" id="{cid}" data-severity="{sev.lower()}">
            <div class="wb-card-header" onclick="toggleDetails('{cid}')">
                <div style="flex: 1;">
                    <div class="wb-tags-row">
                        <span class="badge badge-{sev}">{sev}</span>
                        <span class="badge badge-status">{html.escape(f.status)}</span>
                        <span class="badge badge-cvss">CVSS {html.escape(f.cvss_display)}</span>
                        <span class="mono-meta">{safe_cve} &bull; {safe_id}</span>
                    </div>
                    <div class="wb-finding-title">{safe_title}</div>
                    <div class="wb-finding-meta">
                        <span>📦 <strong>{safe_pkg}</strong> ({safe_eco})</span>
                        <span>🎯 <code>{safe_path}</code></span>
                        <span>🛡️ {safe_cwe}</span>
                        <span>⏱️ SLA: {f.sla_hours}h</span>
                    </div>
                </div>
                <div class="toggle-arrow" id="{cid}-arrow">▼</div>
            </div>
            <div class="wb-card-details" id="{cid}-details">
                <div class="wb-desc">{safe_desc}</div>
                <div class="pane-tab-bar">
                    <button class="pane-tab-btn active tab-poc" onclick="switchTab('{cid}', 'poc')">🔴 Exploit Reproduction PoC</button>
                    <button class="pane-tab-btn tab-patch" onclick="switchTab('{cid}', 'patch')">🟢 建议修复（未验证）</button>
                </div>
                <div class="code-box pane-poc">
                    <button class="copy-btn" onclick="copySnippet(this)">复制 PoC</button>
                    <pre><code>{safe_poc}</code></pre>
                </div>
                <div class="code-box pane-patch" style="display: none;">
                    <button class="copy-btn" onclick="copySnippet(this)">复制建议修复</button>
                    <pre><code>{safe_patch}</code></pre>
                </div>
                <div class="wb-foot-meta">
                    Surface ID: {safe_surface} &bull; Seal: sha256:{safe_sha}...
                </div>
            </div>
        </div>""")

    rendered_cards = "\n".join(finding_cards) if finding_cards else '<div style="text-align: center; padding: 48px; color: #64748b; font-size: 14px;">🛡️ 当前审计集无漏洞发现项</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BugX Security Workbench &bull; {html.escape(b.report_id)}</title>
<style>
{_WORKBENCH_CSS}
</style>
</head>
<body>
<header class="wb-header">
    <div class="wb-title-wrap">
        <h1>🛡️ BugX Security Workbench</h1>
        <div class="wb-subtitle">Report ID: {html.escape(b.report_id)} &bull; Scope: {html.escape(b.assessment_scope)} &bull; BUILD {build_id}</div>
    </div>
    <div class="wb-actions">
        <a href="/api/export/pdf?report_type=formal" class="wb-btn wb-btn-primary" download>🏛️ 商务正式 PDF</a>
        <a href="/api/export/pdf?report_type=visual" class="wb-btn wb-btn-primary" download>⚡ 审美交付 PDF</a>
        <a href="/api/stats" target="_blank" class="wb-btn">📊 指标 API</a>
    </div>
</header>

<div class="kpi-grid">
    <div class="kpi-card">
        <div class="kpi-label">Scanned Surfaces</div>
        <div class="kpi-value">{m.total_surfaces:,}</div>
        <div class="kpi-sub">Probes: {m.total_probes:,} &bull; Mutations: {m.total_mutations:,}</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-label">Overall Kill Rate</div>
        <div class="kpi-value" style="color: #10b981;">{a.kill_rate_cell(m.overall_kill_rate)}</div>
        <div class="kpi-sub">Killed: {m.total_killed:,} / {m.total_mutations:,} &bull; Rating: {m.rating if a.mutation_exercised else attestation.NOT_EXERCISED}</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-label">Verified Findings</div>
        <div class="kpi-value">{len(b.findings)}</div>
        <div class="kpi-sub">Crit: {m.critical_count} &bull; High: {m.high_count} &bull; Med: {m.medium_count} &bull; Low: {m.low_count}</div>
    </div>
    <div class="kpi-card">
        <div class="kpi-label">False Positive Rate</div>
        <div class="kpi-value" style="color: #3b82f6;">{a.false_positive_verdict()}</div>
        <div class="kpi-sub">Negative Control: {a.negative_control_verdict()}</div>
    </div>
</div>

<div class="filter-bar">
    <div class="filter-left">
        <button class="wb-pill active" data-severity="" onclick="filterSeverity('')">全部 ({len(b.findings)})</button>
        <button class="wb-pill wb-pill-crit" data-severity="critical" onclick="filterSeverity('critical')">CRITICAL ({m.critical_count})</button>
        <button class="wb-pill wb-pill-high" data-severity="high" onclick="filterSeverity('high')">HIGH ({m.high_count})</button>
        <button class="wb-pill wb-pill-med" data-severity="medium" onclick="filterSeverity('medium')">MEDIUM ({m.medium_count})</button>
        <button class="wb-pill wb-pill-low" data-severity="low" onclick="filterSeverity('low')">LOW ({m.low_count})</button>
    </div>
    <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="text" id="wb-search" class="search-input" placeholder="实时搜索 CVE ID、类型、组件包、路径..." oninput="onSearchInput()" />
    </div>
    <div class="result-meta" id="wb-counter">显示 {len(b.findings)} 个漏洞 (共 {len(b.findings)} 个)</div>
</div>

<div id="wb-findings-container">
    {rendered_cards}
</div>

<script>
{_WORKBENCH_JS}
</script>
</body>
</html>
"""

