let safeItems = [];
let safeSelection = new Set();
let safeBusy = false;
let safeLoadRevision = 0;
const safeCategoryNames = {skills: 'المهارات', sessions: 'المحادثات وبيانات الجلسات', settings: 'الإعدادات الفعلية', connections: 'الاتصالات وبيانات الدخول', plugins: 'الإضافات', data: 'بيانات الأداة المخصّصة'};

function safeApi() {
    if (!window.pywebview || !window.pywebview.api) throw new Error('واجهة الاتصال غير جاهزة؛ أعد فتح البرنامج.');
    return window.pywebview.api;
}

function safeMessage(message) {
    document.getElementById('safe-status').textContent = message;
}

const originalSetMode = setMode;
setMode = function(mode) {
    if (safeBusy) return;
    originalSetMode(mode);
    // Leftover progress/result text from a finished operation (e.g. "Restored
    // and verified 353 files") stayed on screen after navigating away, since
    // nothing ever hid it except explicitly closing that operation's own
    // modal. Landing on an unrelated tab still showing a previous operation's
    // results reads as "this tab has a stuck/finished action", not as
    // stale state left behind - clear it on every navigation instead.
    document.getElementById('progress-container').style.display = 'none';
    document.getElementById('log-box').style.display = 'none';
    document.getElementById('log-box').textContent = '';
    const active = mode === 'backup' || mode === 'restore';
    document.getElementById('handoff-workspace').hidden = mode !== 'handoff';
    document.getElementById('apps-selection-section').hidden = mode === 'handoff';
    if (mode === 'handoff') document.getElementById('action-bar').style.display = 'none';
    document.getElementById('safe-workspace').hidden = !active;
    for (const identifier of ['accordion-skills', 'accordion-mcp', 'accordion-sessions', 'accordion-templates']) {
        document.getElementById(identifier).hidden = true;
    }
    if (mode === 'transfer') {
        // Direct app-to-app overwrite stays disabled (MCP/config formats
        // aren't interchangeable between tools, and it skips the preview/
        // undo safety net) - but leaving the button merely disabled was a
        // dead end with no way forward. It now does the one thing that
        // actually is safe: jump to the preview-based restore flow.
        document.getElementById('main-action-btn').disabled = false;
        document.getElementById('main-action-btn').textContent = t('transfer-use-restore-btn');
    } else {
        document.getElementById('main-action-btn').disabled = false;
    }
    safeSelection.clear();
    renderAppsGrid();
    if (active) refreshSafeWorkspace();
};

const originalToggleApp = toggleAppSelection;
toggleAppSelection = function(identifier) {
    if (safeBusy) return;
    originalToggleApp(identifier);
    // Checking an app card reads as "back this up" on its own - requiring a
    // second, separate pick from the items table below (out of view behind
    // the sticky action bar) produced a confusing "choose an item too" alert
    // after people had already picked an app. Default to everything for
    // that app and let them narrow it down, instead of starting from zero.
    const nowSelected = selectedAppIds.includes(identifier);
    for (const item of safeItems) {
        if (item.app !== identifier) continue;
        if (nowSelected) safeSelection.add(item.id);
        else safeSelection.delete(item.id);
    }
    renderSafeItems();
};

const originalBrowseDirectory = browseBackupDirectory;
browseBackupDirectory = async function() {
    if (safeBusy) return;
    await originalBrowseDirectory();
    await refreshSafeWorkspace();
};

// The password/verification-word fields ask for something most people never
// need: encryption is opt-in (unchecked by default) for a new backup, and a
// snapshot only needs a word typed in to restore it if it was itself made
// with encryption on. Showing the fields unconditionally meant everyone saw
// a password prompt every time, whether or not anything they were doing
// actually involved one. This keeps them out of sight until they're relevant.
function onEncryptToggle() {
    const on = document.getElementById('encrypt-backup').checked;
    document.getElementById('confirm-password-field').hidden = !on;
    if (currentMode === 'backup') document.getElementById('safe-password-section').hidden = !on;
}

function updatePasswordVisibility() {
    if (currentMode === 'backup') {
        onEncryptToggle();
        return;
    }
    const selector = document.getElementById('snapshot-select');
    const selected = selector.selectedOptions[0];
    const needsPassword = !!selected && selected.dataset.encrypted === '1';
    document.getElementById('safe-password-section').hidden = !needsPassword;
    document.getElementById('unlock-snapshot').hidden = !needsPassword;
}

async function refreshSafeWorkspace() {
    if (safeBusy || !window.pywebview) return;
    try {
        const restore = currentMode === 'restore';
        document.getElementById('safe-restore-controls').hidden = !restore;
        document.getElementById('safe-encrypt-controls').hidden = restore;
        const selector = document.getElementById('snapshot-select');
        const previous = selector.value;
        selector.replaceChildren();
        const snapshots = await safeApi().list_snapshots();
        for (const snapshot of snapshots) {
            const label = `${new Date(snapshot.created).toLocaleString('ar-EG')} | ${snapshot.status} | ${snapshot.files} ملف ${snapshot.encrypted ? '🔒' : ''}`;
            const option = new Option(label, snapshot.id);
            option.dataset.encrypted = snapshot.encrypted ? '1' : '0';
            selector.add(option);
        }
        const libraryOption = new Option(t('snapshot-library-option'), 'library');
        libraryOption.dataset.encrypted = '0';
        selector.add(libraryOption);
        if (Array.from(selector.options).some(option => option.value === previous)) selector.value = previous;
        const recovery = document.getElementById('recovery-select');
        recovery.replaceChildren();
        for (const point of await safeApi().list_recovery_points()) {
            recovery.add(new Option(`${point.id} | ${point.status}${point.encrypted ? ' 🔒' : ''}`, point.id));
        }
        updatePasswordVisibility();
        await loadSafeItems();
    } catch (error) {
        safeMessage(String(error));
    }
}

async function loadSafeItems() {
    if (safeBusy) return;
    const revision = ++safeLoadRevision;
    safeItems = [];
    safeSelection.clear();
    renderSafeItems();
    safeMessage('جارٍ قراءة العناصر…');
    try {
        let items;
        if (currentMode === 'backup') {
            items = await safeApi().get_backup_inventory();
        } else {
            const response = await safeApi().get_snapshot_items(document.getElementById('snapshot-select').value, document.getElementById('backup-password').value);
            if (!response.success) throw new Error(response.error);
            items = response.items;
        }
        if (revision !== safeLoadRevision) return;
        safeItems = items;
        renderSafeItems();
    } catch (error) {
        if (revision === safeLoadRevision) safeMessage(String(error));
    }
}

function visibleSafeItems() {
    const query = document.getElementById('safe-filter').value.toLocaleLowerCase();
    // Matching name+app only meant typing the category itself - "sessions",
    // "محادثات", "plugins" - found nothing, since item names are paths like
    // "projects/D--AI-BACKUP" that never contain that word. With ~1,900
    // skills and single-digit counts for every other category, that made
    // sessions/connections/plugins effectively invisible: they're really in
    // the (unfiltered) list, just buried, and searching for them by category
    // name silently returned zero instead of finding them.
    return safeItems.filter(item => (item.app === 'library' || selectedAppIds.includes(item.app)) &&
        `${item.app} ${item.name} ${item.category} ${safeCategoryNames[item.category] || ''}`.toLocaleLowerCase().includes(query));
}

// One icon per inventory category, matching the icon language used
// everywhere else in the app (see the sprite in index.html).
const SAFE_CATEGORY_ICONS = { skills: 'icon-library', sessions: 'icon-chat', settings: 'icon-settings', connections: 'icon-mcp', plugins: 'icon-plugin' };

function formatBytes(bytes) {
    if (!bytes) return '—';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

function renderSafeItems() {
    const container = document.getElementById('safe-items');
    if (!container) return;
    container.replaceChildren();
    const available = new Set(safeItems.filter(item => item.app === 'library' || selectedAppIds.includes(item.app)).map(item => item.id));
    safeSelection = new Set([...safeSelection].filter(identifier => available.has(identifier)));

    const items = visibleSafeItems();
    if (items.length === 0) {
        const empty = document.createElement('p');
        empty.style.cssText = 'padding:24px;text-align:center;color:var(--text-muted);font-size:13px;';
        empty.textContent = currentLang === 'ar' ? 'لا توجد عناصر مطابقة.' : 'No matching items.';
        container.append(empty);
        updateSafeCounts();
        return;
    }

    const statusWord = currentLang === 'ar' ? { present: 'موجود', missing: 'غير موجود', type: 'النوع', name: 'الاسم', app: 'البرنامج', status: 'الحالة', size: 'الحجم' }
                                              : { present: 'Present', missing: 'Not found', type: 'Type', name: 'Name', app: 'App', status: 'Status', size: 'Size' };

    const table = document.createElement('table');
    table.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;';
    const thead = document.createElement('thead');
    const th = (text) => `<th style="padding:9px 8px;">${escapeHtml(text)}</th>`;
    thead.innerHTML = `<tr style="border-bottom:1px solid var(--border);text-align:start;color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:0.4px;">
        <th style="padding:9px 8px;width:36px;"></th>${th(statusWord.status)}${th(statusWord.type)}${th(statusWord.name)}${th(statusWord.app)}${th(statusWord.size)}
    </tr>`;
    table.append(thead);

    const tbody = document.createElement('tbody');
    for (const item of items) {
        const tr = document.createElement('tr');
        tr.style.cssText = 'border-bottom:1px solid var(--border);';
        const showStatus = currentMode === 'restore' && item.app !== 'library';
        let statusCell = '';
        if (showStatus) {
            statusCell = item.present
                ? `<span style="display:inline-flex;align-items:center;gap:5px;color:#34d399;"><svg class="icon"><use href="#icon-check-circle"/></svg>${escapeHtml(statusWord.present)}</span>`
                : `<span style="display:inline-flex;align-items:center;gap:5px;color:var(--text-faded);"><svg class="icon"><use href="#icon-circle"/></svg>${escapeHtml(statusWord.missing)}</span>`;
        }
        const catIcon = SAFE_CATEGORY_ICONS[item.category] || 'icon-folder';
        const catLabel = safeCategoryNames[item.category] || item.category;
        tr.innerHTML = `
            <td style="padding:9px 8px;"><input type="checkbox" class="safe-item-checkbox" ${safeSelection.has(item.id) ? 'checked' : ''}></td>
            <td style="padding:9px 8px;white-space:nowrap;">${statusCell}</td>
            <td style="padding:9px 8px;white-space:nowrap;color:var(--text-muted);"><span style="display:inline-flex;align-items:center;gap:6px;"><svg class="icon"><use href="#${catIcon}"/></svg>${escapeHtml(catLabel)}</span></td>
            <td style="padding:9px 8px;overflow-wrap:anywhere;">${escapeHtml(item.name)}</td>
            <td style="padding:9px 8px;color:var(--text-muted);white-space:nowrap;">${escapeHtml(item.app)}</td>
            <td style="padding:9px 8px;color:var(--text-muted);white-space:nowrap;">${escapeHtml(formatBytes(item.size))}</td>
        `;
        tr.querySelector('.safe-item-checkbox').addEventListener('change', (ev) => {
            ev.target.checked ? safeSelection.add(item.id) : safeSelection.delete(item.id);
            updateSafeCounts();
        });
        tbody.append(tr);
    }
    table.append(tbody);
    container.append(table);
    updateSafeCounts();
}

function updateSafeCounts() {
    document.getElementById('sel-skills-label').textContent = String(safeSelection.size);
    const selectedSize = safeItems.filter(item => safeSelection.has(item.id)).reduce((sum, item) => sum + (item.size || 0), 0);
    safeMessage(`${safeSelection.size} عنصر محدد (${formatBytes(selectedSize)}). ${visibleSafeItems().length} عنصر ظاهر. العناصر غير الموجودة لا تُدرج؛ الدعم يقتصر على مسارات البيانات المحلية المعروفة.`);
}

function selectSafeItems(select) {
    for (const item of visibleSafeItems()) select ? safeSelection.add(item.id) : safeSelection.delete(item.id);
    renderSafeItems();
}

function setSafeBusy(busy) {
    safeBusy = busy;
    if (busy) {
        for (const control of document.querySelectorAll('button, input, select')) {
            control.dataset.safeDisabled = control.disabled ? 'true' : 'false';
            control.disabled = true;
        }
        const cancel = document.createElement('button');
        cancel.id = 'safe-cancel';
        cancel.className = 'btn-outline';
        cancel.textContent = 'إلغاء آمن بعد الملف الحالي';
        cancel.onclick = () => safeApi().cancel_operation();
        document.getElementById('progress-container').append(cancel);
    } else {
        document.getElementById('safe-cancel')?.remove();
        for (const control of document.querySelectorAll('[data-safe-disabled]')) {
            control.disabled = control.dataset.safeDisabled === 'true';
            delete control.dataset.safeDisabled;
        }
    }
}

executeMainAction = async function() {
    if (safeBusy) return;
    if (currentMode === 'transfer') { setMode('restore'); return; }
    if (!['backup', 'restore'].includes(currentMode)) return;
    if (!selectedAppIds.length || !safeSelection.size) return alert('اختَر برنامجًا وعنصرًا واحدًا على الأقل.');
    const options = {
        apps: [...selectedAppIds], item_ids: [...safeSelection],
        password: document.getElementById('backup-password').value,
        encrypt: document.getElementById('encrypt-backup').checked,
        snapshot_id: document.getElementById('snapshot-select').value,
        conflict: document.getElementById('conflict-policy').value,
        apps_closed: document.getElementById('apps-closed').checked
    };
    if (currentMode === 'backup') {
        // Encryption is opt-in (the checkbox defaults unchecked) - no prompt
        // either way here now. Checking the box is itself the "yes, and I
        // accept typing/keeping a word for it" signal; nothing further to ask.
        if (options.encrypt && !options.password) return alert('اكتب كلمة تحقق للتشفير.');
        if (options.encrypt && document.getElementById('confirm-password').value !== options.password) return alert('كلمتا التحقق غير متطابقتين.');
    } else if (!options.apps_closed) {
        return alert('أغلق البرامج المستهدفة، ثم فعّل تأكيد الإغلاق.');
    }
    setSafeBusy(true);
    const log = document.getElementById('log-box');
    const progress = document.getElementById('progress-container');
    progress.style.display = 'block';
    log.style.display = 'block';
    log.style.whiteSpace = 'pre-wrap';
    log.textContent = 'جارٍ تجهيز العملية…';
    document.getElementById('progress-fill').style.width = '0%';
    document.getElementById('progress-percent').textContent = '0%';
    window.onStrLinkProgress = (current, total, message) => {
        const percent = total ? Math.round(current * 100 / total) : 0;
        document.getElementById('progress-fill').style.width = `${percent}%`;
        document.getElementById('progress-percent').textContent = `${percent}%`;
        document.getElementById('progress-status').textContent = message;
    };
    try {
        if (currentMode === 'restore') {
            const preview = await safeApi().preview_restore(options);
            if (!preview.success) throw new Error(preview.error);
            const counts = preview.counts;
            const explanation = `معاينة: إنشاء ${counts.create}، استبدال ${counts.replace}، تخطي ${counts.skip}، مطابق ${counts.unchanged}.\nستُحفظ نسخة تراجع قبل الاستبدال.\n`;
            log.textContent = explanation + preview.files.map(file => `${file.action}: ${file.path}`).join('\n');
            if (!confirm(explanation + '\nهل توافق على تنفيذ الاستعادة؟')) return;
            options.preview_token = preview.token;
        }
        const response = currentMode === 'backup' ? await safeApi().perform_backup(options) : await safeApi().perform_restore(options);
        log.textContent = (response.logs || []).join('\n') + (response.error ? '\n' + response.error : '');
        const status = response.success ? 'اكتملت العملية مع التحقق من الملفات' : response.status === 'partial' ? 'نجاح جزئي — راجع الملفات التي فشلت' : response.status === 'cancelled' ? 'أُلغيت العملية' : 'لم تكتمل العملية — راجع التفاصيل';
        document.getElementById('progress-status').textContent = status;
        alert(status + (response.snapshot_id ? '\n' + response.snapshot_id : '') + (response.error ? '\n' + response.error : ''));
        document.getElementById('backup-password').value = '';
        document.getElementById('confirm-password').value = '';
        appsData = await safeApi().get_apps_status();
        renderAppsGrid();
    } catch (error) {
        log.textContent += '\n' + String(error);
        document.getElementById('progress-status').textContent = 'لم تكتمل العملية';
        alert(String(error));
    } finally {
        options.password = '';
        window.onStrLinkProgress = null;
        setSafeBusy(false);
        await refreshSafeWorkspace();
    }
};

async function rollbackSafeRestore() {
    if (safeBusy) return;
    const identifier = document.getElementById('recovery-select').value;
    if (!identifier || !confirm('أغلق البرامج المستهدفة. هل تريد التراجع عن هذه الاستعادة؟')) return;
    setSafeBusy(true);
    try {
        const response = await safeApi().rollback_restore(identifier, document.getElementById('backup-password').value);
        alert(response.success ? 'اكتمل التراجع.' : response.error);
        document.getElementById('backup-password').value = '';
    } finally {
        setSafeBusy(false);
        await refreshSafeWorkspace();
    }
}

window.addEventListener('pywebviewready', async () => {
    await loadInitialData();
    setMode('backup');
});

window.addEventListener('DOMContentLoaded', () => {
    setMode('backup');
});

let handoffResult = '';

async function browseHandoffProject() {
    if (safeBusy) return;
    const sourceApp = document.getElementById('handoff-source').value;
    const response = await safeApi().browse_project(sourceApp);
    if (response.success) document.getElementById('handoff-project').value = response.path;
    else if (response.error) alert(response.error);
}

async function exportHandoffProject() {
    if (safeBusy) return;
    const options = {
        project_path: document.getElementById('handoff-project').value.trim(),
        source_app: document.getElementById('handoff-source').value,
        target_app: document.getElementById('handoff-target').value,
        notes: document.getElementById('handoff-notes').value,
        next_steps: document.getElementById('handoff-next').value,
        test_command: document.getElementById('handoff-test').value
    };
    if (!options.project_path) return alert('اختَر مجلد المشروع أولًا.');
    if (options.source_app === options.target_app) return alert('اختَر برنامجين مختلفين.');
    const status = document.getElementById('handoff-status');
    const report = document.getElementById('handoff-report');
    setSafeBusy(true);
    document.getElementById('progress-container').style.display = 'block';
    document.getElementById('handoff-open').hidden = true;
    handoffResult = '';
    window.onStrLinkProgress = (current, total, message) => {
        status.textContent = message;
        document.getElementById('progress-fill').style.width = `${Math.round(100 * current / Math.max(total, 1))}%`;
        document.getElementById('progress-percent').textContent = `${current}/${total}`;
    };
    try {
        status.textContent = 'جارٍ فحص المشروع وحساب بصمات الملفات…';
        const preview = await safeApi().preview_project_handoff(options);
        if (!preview.success) throw new Error(preview.error);
        report.textContent = `الملفات: ${preview.count}\nالحجم: ${(preview.bytes / 1024 / 1024).toFixed(2)} MB\n\nسيتم نسخ:\n` + preview.files.map(file => file.path).join('\n') + '\n\nالمستبعد:\n' + preview.excluded.join('\n');
        if (!confirm(`سيتم نسخ ${preview.count} ملف إلى مجلد جديد، بدون تعديل الأصل وبدون رفع للإنترنت. هل توافق؟`)) {
            status.textContent = 'أُلغيت المعاينة؛ لم يُصدّر المشروع.';
            return;
        }
        options.preview_token = preview.token;
        const response = await safeApi().export_project_handoff(options);
        if (!response.success) throw new Error(response.error + (response.incomplete_folder ? '\nمجلد غير مكتمل: ' + response.incomplete_folder : ''));
        handoffResult = response.folder;
        document.getElementById('handoff-open').hidden = false;
        status.textContent = `اكتمل التصدير: ${response.files} ملف، ${response.events} خطوة مسجلة، ${response.sessions} جلسة مرتبطة بالمشروع.`;
        report.textContent = `${response.folder}\n\n1. افتح START_HERE.md.\n2. افتح مجلد project داخل البرنامج الهدف.\n3. أرفق HANDOFF.md وWORK_MAP.json واطلب متابعة الخطوة التالية.\n\nلم تتحول المحادثة تلقائيًا إلى صيغة البرنامج الآخر؛ المنقول هو المشروع وخريطة العمل المتاحة.`;
    } catch (error) {
        status.textContent = 'لم يكتمل التصدير';
        report.textContent += '\n' + String(error);
        alert(String(error));
    } finally {
        window.onStrLinkProgress = null;
        setSafeBusy(false);
    }
}

async function openHandoffResult() {
    if (handoffResult) await safeApi().open_backup_folder(handoffResult);
}
