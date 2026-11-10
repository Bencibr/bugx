"""Projects and Sync CSS and JavaScript assets for report engine."""

from __future__ import annotations

_PROJECTS_CSS = """
:root {
    --bg-base: #060911;
    --bg-card: #0d1322;
    --bg-drawer: #090e1a;
    --border-dim: rgba(255, 255, 255, 0.08);
    --text-main: #f8fafc;
    --text-muted: #94a3b8;
    --accent-green: #10b981;
    --accent-blue: #3b82f6;
    --accent-red: #ef4444;
    --accent-orange: #f97316;
    --accent-purple: #8b5cf6;
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
.hub-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border-dim);
    flex-wrap: wrap;
    gap: 16px;
}
.hub-title h1 {
    font-size: 22px;
    font-weight: 800;
    display: flex;
    align-items: center;
    gap: 10px;
    letter-spacing: -0.5px;
}
.hub-subtitle {
    font-size: 13px;
    color: var(--text-muted);
    margin-top: 4px;
    font-family: monospace;
}
.hub-actions {
    display: flex;
    align-items: center;
    gap: 10px;
}
.hub-btn {
    background: rgba(255, 255, 255, 0.05);
    color: #cbd5e1;
    border: 1px solid var(--border-dim);
    padding: 8px 16px;
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
.hub-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #ffffff;
    border-color: rgba(255, 255, 255, 0.2);
}
.hub-btn-primary {
    background: rgba(16, 185, 129, 0.15);
    color: var(--accent-green);
    border-color: rgba(16, 185, 129, 0.4);
}
.hub-btn-primary:hover {
    background: rgba(16, 185, 129, 0.25);
    border-color: var(--accent-green);
}
.bento-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
}
.bento-card {
    background: var(--bg-card);
    border: 1px solid var(--border-dim);
    border-radius: 12px;
    padding: 18px 20px;
    position: relative;
    overflow: hidden;
    transition: transform 0.2s, border-color 0.2s;
}
.bento-card:hover {
    border-color: rgba(255, 255, 255, 0.15);
    transform: translateY(-2px);
}
.bento-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-muted);
    margin-bottom: 6px;
    font-weight: 600;
}
.bento-value {
    font-size: 28px;
    font-weight: 800;
    font-family: monospace;
    color: #ffffff;
}
.bento-sub {
    font-size: 12px;
    color: #64748b;
    margin-top: 4px;
}
.toolbar {
    background: var(--bg-card);
    border: 1px solid var(--border-dim);
    border-radius: 12px;
    padding: 14px 20px;
    margin-bottom: 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 16px;
}
.filter-pills {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}
.pill-btn {
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
.pill-btn:hover {
    color: #ffffff;
    border-color: rgba(255, 255, 255, 0.2);
}
.pill-btn.active {
    background: #1e293b;
    color: #ffffff;
    border-color: var(--accent-blue);
}
.search-box {
    position: relative;
    flex: 1;
    max-width: 360px;
    min-width: 240px;
}
.search-box input {
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
.search-box input:focus {
    border-color: var(--accent-blue);
}
.search-box-icon {
    position: absolute;
    left: 12px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 13px;
    color: var(--text-muted);
    pointer-events: none;
}
.projects-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
    gap: 20px;
}
.project-card {
    background: var(--bg-card);
    border: 1px solid var(--border-dim);
    border-radius: 12px;
    padding: 20px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    transition: all 0.2s;
}
.project-card:hover {
    border-color: rgba(255, 255, 255, 0.2);
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
}
.pc-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
}
.pc-title {
    font-size: 16px;
    font-weight: 700;
    color: #ffffff;
}
.pc-badges {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-top: 6px;
}
.badge {
    font-size: 11px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 4px;
    font-family: monospace;
}
.badge-status { background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); }
.badge-branch { background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }
.badge-lang { background: rgba(255, 255, 255, 0.08); color: #cbd5e1; border: 1px solid rgba(255, 255, 255, 0.15); }
.pc-path {
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 12px;
    color: #94a3b8;
    background: #060911;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.05);
    margin-bottom: 16px;
    word-break: break-all;
}
.pc-metrics-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 16px;
}
.metric-box {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: 8px;
    padding: 10px;
}
.metric-box-label {
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
}
.metric-box-val {
    font-size: 18px;
    font-weight: 800;
    font-family: monospace;
    color: #ffffff;
    margin-top: 2px;
}
.progress-wrap {
    margin-top: 6px;
    height: 6px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 3px;
    overflow: hidden;
}
.progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #3b82f6, #10b981);
    border-radius: 3px;
}
.pc-sync-meta {
    font-size: 11px;
    color: #64748b;
    font-family: monospace;
    margin-bottom: 16px;
}
.pc-footer-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    border-top: 1px solid var(--border-dim);
    padding-top: 14px;
}
.btn-card-action {
    flex: 1;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border-dim);
    color: #cbd5e1;
    padding: 7px 10px;
    font-size: 12px;
    font-weight: 600;
    border-radius: 6px;
    cursor: pointer;
    text-align: center;
    text-decoration: none;
    transition: all 0.2s;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
}
.btn-card-action:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #ffffff;
    border-color: rgba(255, 255, 255, 0.2);
}
.btn-card-primary {
    background: rgba(59, 130, 246, 0.15);
    color: #60a5fa;
    border-color: rgba(59, 130, 246, 0.4);
}
.btn-card-primary:hover {
    background: rgba(59, 130, 246, 0.25);
    border-color: #60a5fa;
}

/* Open WebUI Style Drawer */
.drawer-backdrop {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0, 0, 0, 0.65);
    backdrop-filter: blur(4px);
    z-index: 1000;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.25s ease;
}
.drawer-backdrop.active {
    opacity: 1;
    pointer-events: auto;
}
.settings-drawer {
    position: fixed;
    top: 0; right: 0; bottom: 0;
    width: 520px;
    max-width: 90vw;
    background: var(--bg-drawer);
    border-left: 1px solid rgba(255, 255, 255, 0.1);
    box-shadow: -10px 0 30px rgba(0, 0, 0, 0.5);
    z-index: 1001;
    transform: translateX(100%);
    transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    display: flex;
    flex-direction: column;
}
.settings-drawer.active {
    transform: translateX(0);
}
.drawer-header {
    padding: 20px 24px;
    border-bottom: 1px solid var(--border-dim);
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.drawer-header h2 {
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
    display: flex;
    align-items: center;
    gap: 8px;
}
.drawer-close {
    background: transparent;
    border: none;
    color: #94a3b8;
    font-size: 20px;
    cursor: pointer;
    padding: 4px;
}
.drawer-close:hover { color: #ffffff; }
.drawer-tabs {
    display: flex;
    border-bottom: 1px solid var(--border-dim);
    background: rgba(255, 255, 255, 0.02);
}
.drawer-tab {
    flex: 1;
    text-align: center;
    padding: 12px;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    cursor: pointer;
    transition: all 0.2s;
}
.drawer-tab:hover { color: #ffffff; }
.drawer-tab.active {
    color: #60a5fa;
    border-bottom-color: #60a5fa;
    background: rgba(59, 130, 246, 0.05);
}
.drawer-body {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
}
.tab-pane { display: none; }
.tab-pane.active { display: block; }

.form-group {
    margin-bottom: 20px;
}
.form-label {
    display: block;
    font-size: 13px;
    font-weight: 600;
    color: #e2e8f0;
    margin-bottom: 6px;
}
.form-sub {
    font-size: 11px;
    color: var(--text-muted);
    margin-bottom: 8px;
}
.form-control {
    width: 100%;
    background: #060911;
    border: 1px solid var(--border-dim);
    color: #ffffff;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 13px;
    outline: none;
    transition: border-color 0.2s;
}
.form-control:focus {
    border-color: var(--accent-blue);
}
.form-toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.05);
    padding: 12px 16px;
    border-radius: 8px;
    margin-bottom: 12px;
}
.form-toggle-label {
    font-size: 13px;
    font-weight: 600;
    color: #e2e8f0;
}
.form-toggle-desc {
    font-size: 11px;
    color: #64748b;
    margin-top: 2px;
}
.switch {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
}
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
    position: absolute;
    cursor: pointer;
    top: 0; left: 0; right: 0; bottom: 0;
    background-color: #334155;
    transition: .3s;
    border-radius: 24px;
}
.slider:before {
    position: absolute;
    content: "";
    height: 18px;
    width: 18px;
    left: 3px;
    bottom: 3px;
    background-color: white;
    transition: .3s;
    border-radius: 50%;
}
input:checked + .slider {
    background-color: var(--accent-green);
}
input:checked + .slider:before {
    transform: translateX(20px);
}
.drawer-footer {
    padding: 16px 24px;
    border-top: 1px solid var(--border-dim);
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: rgba(255, 255, 255, 0.02);
}

/* Toast */
.toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: #0f172a;
    border: 1px solid var(--accent-green);
    color: #ffffff;
    padding: 12px 20px;
    border-radius: 8px;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
    font-size: 13px;
    font-weight: 600;
    z-index: 2000;
    opacity: 0;
    transform: translateY(20px);
    transition: all 0.3s ease;
    pointer-events: none;
    display: flex;
    align-items: center;
    gap: 8px;
}
.toast.active {
    opacity: 1;
    transform: translateY(0);
    pointer-events: auto;
}
"""

_PROJECTS_JS = r"""
let activeCategory = 'all';
let currentProject = null;

function filterCategory(cat) {
    activeCategory = cat;
    document.querySelectorAll('.pill-btn').forEach(btn => {
        if (btn.dataset.category === cat) {
            btn.classList.add('active');
        } else {
            btn.classList.remove('active');
        }
    });
    filterProjects();
}

function filterProjects() {
    const q = (document.getElementById('project-search').value || '').trim().toLowerCase();
    const normalizedQ = q.replace(/_/g, '-');
    const cards = document.querySelectorAll('.project-card');
    cards.forEach(card => {
        const text = (card.innerText + ' ' + (card.id || '')).toLowerCase().replace(/_/g, '-');
        const matchesQuery = !q || text.includes(normalizedQ);
        const status = card.dataset.status || '';
        let matchesCat = true;
        if (activeCategory === 'synced') matchesCat = (status === 'SYNCED');
        if (activeCategory === 'local') matchesCat = (status === 'LOCAL_ONLY' || status === 'SYNCED');
        if (matchesQuery && matchesCat) {
            card.style.display = 'flex';
        } else {
            card.style.display = 'none';
        }
    });
}

function openDrawer(pid) {
    currentProject = pid;
    const card = document.getElementById('project-card-' + pid);
    const title = card ? card.querySelector('.pc-title').innerText : pid;
    document.getElementById('drawer-project-name').innerText = title;
    
    // Fetch latest config
    fetch('/api/projects/' + pid + '/config')
        .then(r => r.json())
        .then(data => {
            const cfg = data.config || {};
            if (document.getElementById('cfg-depth')) document.getElementById('cfg-depth').value = cfg.audit_depth || 'standard';
            if (document.getElementById('cfg-workers')) {
                document.getElementById('cfg-workers').value = cfg.worker_concurrency || 4;
                document.getElementById('cfg-workers-val').innerText = cfg.worker_concurrency || 4;
            }
            if (document.getElementById('cfg-auto-heal')) document.getElementById('cfg-auto-heal').checked = !!cfg.auto_heal;
            if (document.getElementById('cfg-gate')) {
                const valStr = Number(cfg.kill_rate_gate !== undefined ? cfg.kill_rate_gate : 0.90).toFixed(2);
                document.getElementById('cfg-gate').value = valStr;
            }
            if (document.getElementById('cfg-fail-closed')) document.getElementById('cfg-fail-closed').checked = !!cfg.fail_closed;
            if (document.getElementById('cfg-local-watcher')) document.getElementById('cfg-local-watcher').checked = !!cfg.local_watcher;
            if (document.getElementById('cfg-cloud-sync')) document.getElementById('cfg-cloud-sync').checked = !!cfg.cloud_sync;
            if (document.getElementById('cfg-cloud-url')) document.getElementById('cfg-cloud-url').value = cfg.cloud_endpoint || 'https://cloud.bugx.io/api/v1';
        })
        .catch(console.error);

    document.getElementById('settings-drawer').classList.add('active');
    document.getElementById('drawer-backdrop').classList.add('active');
}

function closeDrawer() {
    document.getElementById('settings-drawer').classList.remove('active');
    document.getElementById('drawer-backdrop').classList.remove('active');
}

function switchDrawerTab(tabId) {
    document.querySelectorAll('.drawer-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    
    const btn = document.getElementById('tab-btn-' + tabId);
    const pane = document.getElementById('tab-pane-' + tabId);
    if (btn) btn.classList.add('active');
    if (pane) pane.classList.add('active');
}

async function saveProjectConfig() {
    if (!currentProject) return;
    const gateVal = document.getElementById('cfg-gate') ? parseFloat(document.getElementById('cfg-gate').value) : 0.90;
    const payload = {
        audit_depth: document.getElementById('cfg-depth').value,
        worker_concurrency: parseInt(document.getElementById('cfg-workers').value, 10),
        kill_rate_gate: isNaN(gateVal) ? 0.90 : gateVal,
        fail_closed: document.getElementById('cfg-fail-closed').checked,
        local_watcher: document.getElementById('cfg-local-watcher').checked,
        cloud_sync: document.getElementById('cfg-cloud-sync').checked,
        cloud_endpoint: document.getElementById('cfg-cloud-url').value,
        auto_heal: document.getElementById('cfg-auto-heal').checked
    };

    try {
        const res = await fetch('/api/projects/' + currentProject + '/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        showToast('✓ 项目配置已成功持久化并同步！');
        closeDrawer();
    } catch (err) {
        showToast('✗ 配置保存失败: ' + err.message, 'error');
    }
}

async function syncProject(pid, btn) {
    const originalText = btn ? btn.innerText : '';
    if (btn) btn.innerText = '🔄 同步中...';
    try {
        const res = await fetch('/api/projects/' + pid + '/sync', {method: 'POST'});
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const syncTimeEl = document.getElementById('sync-time-' + pid);
        if (syncTimeEl) syncTimeEl.innerText = '刚刚 (Just now)';
        showToast('✓ ' + (data.message || '项目同步成功！'));
        if (btn) btn.innerText = '✓ 已同步';
        setTimeout(() => { if (btn) btn.innerText = originalText; }, 2000);
    } catch (err) {
        showToast('✗ 同步失败: ' + err.message, 'error');
        if (btn) btn.innerText = originalText;
    }
}

function syncCurrentFromDrawer() {
    if (currentProject) {
        syncProject(currentProject);
    }
}

function showToast(msg, type = 'success') {
    const toast = document.getElementById('app-toast');
    if (!toast) return;
    toast.innerText = msg;
    if (type === 'error') {
        toast.style.borderColor = '#ef4444';
    } else {
        toast.style.borderColor = '#10b981';
    }
    toast.classList.add('active');
    setTimeout(() => {
        toast.classList.remove('active');
    }, 3000);
}

function viewWorkbench(pid) {
    if (window.parent && window.parent.switchView) {
        window.parent.switchView('workbench');
    } else {
        window.location.href = '/?view=workbench';
    }
}
"""

_SYNC_CSS = """
.sync-container {
    max-width: 1200px;
    margin: 0 auto;
}
.sync-card {
    background: #0d1322;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 24px;
}
.sync-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    margin-top: 16px;
}
.sync-table th {
    text-align: left;
    padding: 10px 14px;
    color: #94a3b8;
    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.sync-table td {
    padding: 12px 14px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    color: #cbd5e1;
}
.sync-table tr:hover td {
    background: rgba(255, 255, 255, 0.02);
}
"""

