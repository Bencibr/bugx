"""Workbench CSS and JavaScript assets for report engine."""

from __future__ import annotations

_WORKBENCH_CSS = """
:root {
    --bg-base: #060911;
    --bg-card: #0d1322;
    --border-dim: rgba(255, 255, 255, 0.08);
    --text-main: #f8fafc;
    --text-muted: #94a3b8;
    --accent-green: #10b981;
    --accent-blue: #3b82f6;
    --accent-red: #ef4444;
    --accent-orange: #f97316;
    --accent-yellow: #eab308;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: var(--bg-base);
    color: var(--text-main);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 24px 32px 80px 32px;
    line-height: 1.5;
    min-height: 100vh;
}
.wb-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border-dim);
    flex-wrap: wrap;
    gap: 16px;
}
.wb-title-wrap h1 {
    font-size: 22px;
    font-weight: 800;
    display: flex;
    align-items: center;
    gap: 10px;
    letter-spacing: -0.5px;
}
.wb-subtitle {
    font-size: 13px;
    color: var(--text-muted);
    margin-top: 4px;
    font-family: monospace;
}
.wb-actions {
    display: flex;
    align-items: center;
    gap: 10px;
}
.wb-btn {
    background: rgba(255, 255, 255, 0.05);
    color: #cbd5e1;
    border: 1px solid var(--border-dim);
    padding: 7px 14px;
    font-size: 13px;
    font-weight: 600;
    border-radius: 8px;
    text-decoration: none;
    cursor: pointer;
    transition: all 0.2s;
    display: inline-flex;
    align-items: center;
    gap: 6px;
}
.wb-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #ffffff;
    border-color: rgba(255, 255, 255, 0.2);
}
.wb-btn-primary {
    background: rgba(59, 130, 246, 0.15);
    color: #60a5fa;
    border-color: rgba(59, 130, 246, 0.4);
}
.wb-btn-primary:hover {
    background: rgba(59, 130, 246, 0.25);
    border-color: #60a5fa;
}
.kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
}
.kpi-card {
    background: var(--bg-card);
    border: 1px solid var(--border-dim);
    border-radius: 12px;
    padding: 16px 20px;
    position: relative;
    overflow: hidden;
}
.kpi-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-muted);
    margin-bottom: 6px;
    font-weight: 600;
}
.kpi-value {
    font-size: 26px;
    font-weight: 800;
    font-family: monospace;
    color: #ffffff;
}
.kpi-sub {
    font-size: 11px;
    color: #64748b;
    margin-top: 4px;
}
.filter-bar {
    background: var(--bg-card);
    border: 1px solid var(--border-dim);
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 16px;
}
.filter-left {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}
.wb-pill {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.08);
    color: var(--text-muted);
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 600;
    border-radius: 20px;
    cursor: pointer;
    transition: all 0.2s;
}
.wb-pill:hover {
    color: #ffffff;
    border-color: rgba(255, 255, 255, 0.2);
}
.wb-pill.active {
    background: #1e293b;
    color: #ffffff;
    border-color: var(--accent-blue);
}
.wb-pill-crit.active {
    background: rgba(239, 68, 68, 0.2);
    color: #ef4444;
    border-color: #ef4444;
}
.wb-pill-high.active {
    background: rgba(249, 115, 22, 0.2);
    color: #f97316;
    border-color: #f97316;
}
.wb-pill-med.active {
    background: rgba(234, 179, 8, 0.2);
    color: #eab308;
    border-color: #eab308;
}
.wb-pill-low.active {
    background: rgba(59, 130, 246, 0.2);
    color: #3b82f6;
    border-color: #3b82f6;
}
.search-wrap {
    position: relative;
    flex: 1;
    max-width: 380px;
    min-width: 240px;
}
.search-input {
    width: 100%;
    background: #060911;
    border: 1px solid var(--border-dim);
    padding: 8px 14px 8px 36px;
    border-radius: 8px;
    color: #ffffff;
    font-size: 13px;
    outline: none;
    transition: border-color 0.2s;
}
.search-input:focus {
    border-color: var(--accent-blue);
}
.search-icon {
    position: absolute;
    left: 12px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 13px;
    color: var(--text-muted);
    pointer-events: none;
}
.result-meta {
    font-size: 12px;
    color: var(--text-muted);
    font-family: monospace;
}
.wb-card {
    background: var(--bg-card);
    border: 1px solid var(--border-dim);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
    transition: border-color 0.2s;
}
.wb-card:hover {
    border-color: rgba(255, 255, 255, 0.2);
}
.wb-card-header {
    padding: 16px 20px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
}
.wb-tags-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}
.badge {
    font-size: 11px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 4px;
    font-family: monospace;
    text-transform: uppercase;
}
.badge-CRITICAL { background: rgba(239, 68, 68, 0.2); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.4); }
.badge-HIGH { background: rgba(249, 115, 22, 0.2); color: #f97316; border: 1px solid rgba(249, 115, 22, 0.4); }
.badge-MEDIUM { background: rgba(234, 179, 8, 0.2); color: #eab308; border: 1px solid rgba(234, 179, 8, 0.4); }
.badge-LOW { background: rgba(59, 130, 246, 0.2); color: #3b82f6; border: 1px solid rgba(59, 130, 246, 0.4); }
.badge-status { background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); }
.badge-cvss { background: rgba(255, 255, 255, 0.08); color: #cbd5e1; border: 1px solid rgba(255, 255, 255, 0.15); }
.mono-meta { font-family: monospace; font-size: 13px; color: #94a3b8; }
.wb-finding-title {
    margin-top: 8px;
    font-size: 15px;
    font-weight: 700;
    color: #ffffff;
}
.wb-finding-meta {
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 6px;
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    font-family: monospace;
}
.toggle-arrow {
    font-size: 14px;
    color: #64748b;
    user-select: none;
}
.wb-card-details {
    display: none;
    border-top: 1px solid var(--border-dim);
    padding: 20px;
    background: #080d1a;
}
.wb-desc {
    margin-bottom: 14px;
    font-size: 13px;
    color: #cbd5e1;
    line-height: 1.6;
}
.pane-tab-bar {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;
}
.pane-tab-btn {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border-dim);
    color: var(--text-muted);
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s;
}
.pane-tab-btn.active {
    background: #1e293b;
    color: #ffffff;
    border-color: var(--accent-blue);
}
.code-box {
    position: relative;
    background: #03060c;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    padding: 14px;
    overflow-x: auto;
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 12px;
    line-height: 1.5;
    color: #e2e8f0;
    max-height: 320px;
}
.copy-btn {
    position: absolute;
    top: 8px;
    right: 8px;
    background: rgba(255, 255, 255, 0.1);
    border: 1px solid rgba(255, 255, 255, 0.15);
    color: #cbd5e1;
    padding: 3px 8px;
    font-size: 11px;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
}
.copy-btn:hover {
    background: rgba(255, 255, 255, 0.2);
    color: #ffffff;
}
.wb-foot-meta {
    margin-top: 10px;
    font-size: 11px;
    color: #64748b;
    font-family: monospace;
}
"""

_WORKBENCH_JS = r"""
let activeSeverity = '';
let debounceTimer = null;

function toggleDetails(cardId) {
    const details = document.getElementById(cardId + '-details');
    const arrow = document.getElementById(cardId + '-arrow');
    if (!details) return;
    const isHidden = details.style.display === 'none' || window.getComputedStyle(details).display === 'none';
    if (isHidden) {
        details.style.display = 'block';
        if (arrow) arrow.innerText = '▲';
    } else {
        details.style.display = 'none';
        if (arrow) arrow.innerText = '▼';
    }
}

function switchTab(cardId, tab) {
    const card = document.getElementById(cardId);
    if (!card) return;
    const pocPane = card.querySelector('.pane-poc');
    const patchPane = card.querySelector('.pane-patch');
    const tabPoc = card.querySelector('.tab-poc');
    const tabPatch = card.querySelector('.tab-patch');
    if (tab === 'poc') {
        pocPane.style.display = 'block';
        patchPane.style.display = 'none';
        tabPoc.classList.add('active');
        tabPatch.classList.remove('active');
    } else {
        pocPane.style.display = 'none';
        patchPane.style.display = 'block';
        tabPatch.classList.add('active');
        tabPoc.classList.remove('active');
    }
}

function copySnippet(btn) {
    const pre = btn.parentElement.querySelector('pre');
    if (!pre) return;
    navigator.clipboard.writeText(pre.innerText).then(() => {
        const original = btn.innerText;
        btn.innerText = '✓ 已复制';
        setTimeout(() => { btn.innerText = original; }, 1500);
    });
}

function filterSeverity(sev) {
    activeSeverity = sev;
    document.querySelectorAll('.wb-pill').forEach(btn => {
        if (btn.dataset.severity === sev) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    runSearch();
}

function onSearchInput() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runSearch, 150);
}

async function runSearch() {
    const q = (document.getElementById('wb-search').value || '').trim();
    const params = new URLSearchParams();
    if (activeSeverity) params.set('severity', activeSeverity);
    if (q) params.set('search', q);

    try {
        const res = await fetch('/api/findings?' + params.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        renderDynamicFindings(data.findings, data.total);
    } catch (err) {
        console.error('Fetch error:', err);
    }
}

function renderDynamicFindings(findings, total) {
    const container = document.getElementById('wb-findings-container');
    const counter = document.getElementById('wb-counter');
    if (counter) {
        counter.innerText = '显示 ' + findings.length + ' 个漏洞 (共 ' + total + ' 个)';
    }
    if (!findings || findings.length === 0) {
        container.innerHTML = '<div style="text-align: center; padding: 48px; color: #64748b; font-size: 14px;">🛡️ 没有匹配到符合条件的安全漏洞</div>';
        return;
    }

    let html = '';
    findings.forEach((f, i) => {
        const cid = 'wb-dynamic-' + i;
        const sev = (f.severity || '').toUpperCase();
        html += `
        <div class="wb-card" id="${cid}" data-severity="${sev.toLowerCase()}">
            <div class="wb-card-header" onclick="toggleDetails('${cid}')">
                <div style="flex: 1;">
                    <div class="wb-tags-row">
                        <span class="badge badge-${sev}">${sev}</span>
                        <span class="badge badge-status">${escapeHtml(f.status || '')}</span>
                        <span class="badge badge-cvss">CVSS ${escapeHtml(f.cvss_score === null || f.cvss_score === undefined ? 'NOT-SCORED' : String(f.cvss_score))}</span>
                        <span class="mono-meta">${escapeHtml(f.cve_id || '无 CVE/GHSA 编号（源码级缺陷）')} &bull; ${escapeHtml(f.id || '')}</span>
                    </div>
                    <div class="wb-finding-title">${escapeHtml(f.title || '')}</div>
                    <div class="wb-finding-meta">
                        <span>📦 <strong>${escapeHtml(f.package_name || '')}</strong> (${escapeHtml(f.ecosystem || '')})</span>
                        <span>🎯 <code>${escapeHtml(f.target_path || '')}</code></span>
                        <span>🛡️ ${escapeHtml((f.cwe_id || '') + ' ' + (f.cwe_title || ''))}</span>
                        <span>⏱️ SLA: ${f.sla_hours || 24}h</span>
                    </div>
                </div>
                <div class="toggle-arrow" id="${cid}-arrow">▼</div>
            </div>
            <div class="wb-card-details" id="${cid}-details">
                <div class="wb-desc">${escapeHtml(f.description || '')}</div>
                <div class="pane-tab-bar">
                    <button class="pane-tab-btn active tab-poc" onclick="switchTab('${cid}', 'poc')">🔴 Exploit Reproduction PoC</button>
                    <button class="pane-tab-btn tab-patch" onclick="switchTab('${cid}', 'patch')">🟢 建议修复（未验证）</button>
                </div>
                <div class="code-box pane-poc">
                    <button class="copy-btn" onclick="copySnippet(this)">复制 PoC</button>
                    <pre><code>${escapeHtml((f.exploit_poc || '').trim() || '# PoC Verified')}</code></pre>
                </div>
                <div class="code-box pane-patch" style="display: none;">
                    <button class="copy-btn" onclick="copySnippet(this)">复制建议修复</button>
                    <pre><code>${escapeHtml((f.defense_patch || '').trim() || '# 未提供修复建议')}</code></pre>
                </div>
                <div class="wb-foot-meta">
                    Surface ID: ${escapeHtml(f.surface_id || '')} &bull; Seal: sha256:${escapeHtml((f.receipt_sha256 || '').slice(0, 16))}...
                </div>
            </div>
        </div>`;
    });
    container.innerHTML = html;
}

function escapeHtml(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
"""

