// Token Guard UI. Loaded after safe.js (see the STRLINK_TOKEN_GUARD_UI
// placeholder in main.py), so it shares the same page/global scope and can
// reuse escapeHtml()/formatBytes()/t()/currentLang from index.html/safe.js.
// It wraps setMode() again on top of safe.js's own wrapper, the same
// pattern safe.js itself uses on the original setMode.

function tgApi() {
    if (!window.pywebview || !window.pywebview.api) throw new Error('واجهة الاتصال غير جاهزة؛ أعد فتح البرنامج.');
    return window.pywebview.api;
}

const originalSetModeForTokenGuard = setMode;
setMode = function(mode) {
    originalSetModeForTokenGuard(mode);
    document.getElementById('token-guard-workspace').hidden = mode !== 'token-guard';
    if (mode === 'token-guard') refreshTokenGuardStatus();
};

const TOOL_FILES = { claude: 'CLAUDE.md', codex: 'AGENTS.md', gemini: 'GEMINI.md' };

async function browseTokenGuardProject() {
    const sourceTool = document.getElementById('tg-source-tool').value;
    const response = await tgApi().browse_project(sourceTool);
    if (response.success) {
        document.getElementById('tg-project').value = response.path;
        await detectTokenGuardProject();
    } else if (response.error) {
        alert(response.error);
    }
}

async function detectTokenGuardProject() {
    const path = document.getElementById('tg-project').value.trim();
    const detectedEl = document.getElementById('tg-detected');
    if (!path) { detectedEl.replaceChildren(); return; }
    const res = await tgApi().detect_token_guard_project(path);
    if (!res.success) {
        detectedEl.textContent = t('tg-detect-error') + ' ' + res.error;
        return;
    }
    renderDetectedProject(res.project);
    await refreshTokenGuardStatus();
}

function renderDetectedProject(info) {
    const el = document.getElementById('tg-detected');
    const row = (label, value) => `<div style="display:flex;gap:8px;font-size:13px;padding:3px 0;"><strong style="min-width:150px;color:var(--text-muted);">${escapeHtml(label)}</strong><span style="overflow-wrap:anywhere;">${escapeHtml(value)}</span></div>`;
    el.innerHTML = `
        <div style="margin:10px 0;padding:12px;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:8px;">
            ${row(t('tg-languages'), info.languages.join(', '))}
            ${row(t('tg-frameworks'), info.frameworks.length ? info.frameworks.join(', ') : t('tg-none-detected'))}
            ${row(t('tg-package-manager'), info.packageManager || t('tg-none-detected'))}
            ${row(t('tg-git'), info.git ? t('tg-yes') : t('tg-no'))}
            ${row(t('tg-source-dirs'), info.sourceDirs.length ? info.sourceDirs.join(', ') : t('tg-none-detected'))}
            ${row(t('tg-test-dirs'), info.testDirs.length ? info.testDirs.join(', ') : t('tg-none-detected'))}
            ${row(t('tg-entry-points'), info.entryPoints.length ? info.entryPoints.join(', ') : t('tg-none-detected'))}
        </div>`;
}

async function refreshTokenGuardStatus() {
    const path = document.getElementById('tg-project').value.trim();
    const statusEl = document.getElementById('tg-status');
    if (!path) { statusEl.textContent = ''; return; }
    let res;
    try { res = await tgApi().get_token_guard_status(path); } catch (error) { statusEl.textContent = String(error); return; }
    if (!res.success) { statusEl.textContent = res.error || ''; return; }
    document.getElementById('tg-tool-claude').checked = (res.tools || []).includes('claude');
    document.getElementById('tg-tool-codex').checked = (res.tools || []).includes('codex');
    document.getElementById('tg-tool-gemini').checked = (res.tools || []).includes('gemini');
    if (!res.enabled) {
        statusEl.textContent = res.exists ? t('tg-status-disabled') : t('tg-status-none');
        return;
    }
    const files = (res.tools || []).map(tool => TOOL_FILES[tool]).filter(Boolean);
    statusEl.textContent = t('tg-status-enabled') + ' ' + files.join(', ');
}

function selectedTokenGuardTools() {
    const tools = [];
    if (document.getElementById('tg-tool-claude').checked) tools.push('claude');
    if (document.getElementById('tg-tool-codex').checked) tools.push('codex');
    if (document.getElementById('tg-tool-gemini').checked) tools.push('gemini');
    return tools;
}

async function enableTokenGuard() {
    const path = document.getElementById('tg-project').value.trim();
    if (!path) return alert(t('tg-project-label'));
    const res = await tgApi().enable_token_guard({ project_path: path, tools: selectedTokenGuardTools() });
    if (!res.success) return alert(res.error);
    renderDetectedProject(res.project);
    renderScanResults(res.scan);
    await refreshTokenGuardStatus();
}

async function disableTokenGuard() {
    const path = document.getElementById('tg-project').value.trim();
    if (!path) return;
    const res = await tgApi().disable_token_guard(path);
    if (!res.success) return alert(res.error);
    await refreshTokenGuardStatus();
}

async function rescanTokenGuardProject() {
    const path = document.getElementById('tg-project').value.trim();
    if (!path) return;
    const res = await tgApi().scan_token_guard_project(path);
    if (!res.success) return alert(res.error);
    renderScanResults(res);
}

async function openTokenGuardFolder() {
    const path = document.getElementById('tg-project').value.trim();
    if (!path) return;
    const res = await tgApi().open_token_guard_folder(path);
    if (!res.success) alert(res.error);
}

function renderScanResults(scan) {
    const el = document.getElementById('tg-scan-results');
    if (!scan) { el.replaceChildren(); return; }
    const dirRows = (scan.ignored_dirs || []).slice(0, 8)
        .map(d => `<div style="display:flex;justify-content:space-between;gap:8px;font-size:12px;padding:2px 0;"><span style="overflow-wrap:anywhere;">${escapeHtml(d.path)}</span><span style="color:var(--text-muted);white-space:nowrap;">${escapeHtml(formatBytes(d.size))}</span></div>`)
        .join('') || '<div style="font-size:12px;color:var(--text-faded);">-</div>';
    const fileRows = (scan.top_files || []).slice(0, 8)
        .map(f => `<div style="display:flex;justify-content:space-between;gap:8px;font-size:12px;padding:2px 0;"><span style="overflow-wrap:anywhere;">${escapeHtml(f.path)}</span><span style="color:var(--text-muted);white-space:nowrap;">${escapeHtml(formatBytes(f.size))}</span></div>`)
        .join('') || '<div style="font-size:12px;color:var(--text-faded);">-</div>';
    el.innerHTML = `
        <div class="section-head" style="margin-bottom:10px;"><div><h3 style="font-size:14px;margin:0;">${escapeHtml(t('tg-scan-title'))}</h3></div></div>
        <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px;font-size:13px;">
            <div><strong>${escapeHtml(t('tg-indexed'))}:</strong> ${scan.indexed_count}</div>
            <div><strong>${escapeHtml(t('tg-indexed-size'))}:</strong> ${escapeHtml(formatBytes(scan.indexed_bytes))}</div>
            <div><strong>${escapeHtml(t('tg-ignored-size'))}:</strong> ${escapeHtml(formatBytes(scan.ignored_bytes))}</div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
            <div><div style="font-size:12px;color:var(--text-muted);margin-bottom:4px;">${escapeHtml(t('tg-ignored-dirs'))}</div>${dirRows}</div>
            <div><div style="font-size:12px;color:var(--text-muted);margin-bottom:4px;">${escapeHtml(t('tg-top-files'))}</div>${fileRows}</div>
        </div>`;
}
