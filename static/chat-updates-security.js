// ===== UPDATES CARD (System workspace) =====
// Version check is entirely server-cached (meshsrv/update_service.py) -
// this card only ever reads that cache via /api/updates/status, except
// for the explicit "Check now" click and the preflight/apply steps,
// which are real user-triggered actions, not page-load polling.

let _lastUpdatesStatus = null;

function toggleUpdatesChangelog() {
    const panel = document.getElementById('updatesChangelogPanel');
    const arrow = document.getElementById('updatesChangelogArrow');
    const button = document.getElementById('updatesChangelogToggle');
    if (!panel) return;

    const opening = panel.style.display === 'none';
    panel.style.display = opening ? 'block' : 'none';
    if (arrow) arrow.textContent = opening ? '▴' : '▾';
    if (button) button.setAttribute('aria-expanded', opening ? 'true' : 'false');
}

function renderUpdatesStatus(status) {
    _lastUpdatesStatus = status;

    const currentEl = document.getElementById('updatesCurrentVersion');
    if (currentEl) currentEl.textContent = status.current_version || '--';

    const latestRow = document.getElementById('updatesLatestRow');
    const latestEl = document.getElementById('updatesLatestVersion');
    const changelogWrap = document.getElementById('updatesChangelogToggleWrap');
    const changelogPanel = document.getElementById('updatesChangelogPanel');
    const applyBtn = document.getElementById('updatesApplyBtn');
    const statusText = document.getElementById('updatesStatusText');

    const hasLatest = !!status.latest_version && status.check_ok;
    if (latestRow) latestRow.style.display = hasLatest ? 'block' : 'none';
    if (latestEl) latestEl.textContent = status.latest_version || '--';

    if (changelogWrap) changelogWrap.style.display = hasLatest ? 'block' : 'none';
    if (changelogPanel) {
        changelogPanel.textContent = status.release_notes || '';
    }

    if (applyBtn) applyBtn.style.display = status.update_available ? 'inline-block' : 'none';

    if (statusText) {
        if (!status.last_checked_at) {
            statusText.textContent = window.I18N.t('system.updates_never_checked');
        } else if (!status.check_ok) {
            statusText.textContent = window.I18N.t('system.updates_check_failed', { reason: status.check_error || '' });
        } else if (status.update_available) {
            statusText.textContent = window.I18N.t('system.updates_available', { version: status.latest_version });
        } else {
            statusText.textContent = window.I18N.t('system.updates_up_to_date');
        }
    }

    const autoCheckEl = document.getElementById('updatesAutoCheckEnabled');
    if (autoCheckEl) autoCheckEl.checked = appSettings?.updates?.auto_check !== false;
}

async function loadUpdatesStatus() {
    try {
        const response = await fetch('/api/updates/status');
        const data = await response.json();
        if (data.ok) renderUpdatesStatus(data);
    } catch (error) {
        console.warn('Updates status load failed:', error);
    }
}

async function checkForUpdatesNow(button) {
    const originalText = button?.textContent || '';
    if (button) {
        button.disabled = true;
        button.textContent = window.I18N.t('system.updates_checking');
    }

    try {
        const response = await fetch('/api/updates/check', { method: 'POST' });
        const data = await response.json();
        if (data.ok) {
            renderUpdatesStatus(data);
        } else {
            showToast(`❌ ${data.error || window.I18N.t('errors.unknown_error')}`, 'error');
        }
    } catch (error) {
        showToast(`❌ ${error.message}`, 'error');
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = originalText;
        }
    }
}

async function toggleUpdatesAutoCheck(checked) {
    try {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ updates: { auto_check: checked } })
        });
        const data = await response.json();
        if (data.ok) {
            appSettings = data.settings;
        } else {
            alert(window.I18N.t('settings.unable_to_save_settings', { reason: data.error || window.I18N.t('errors.unknown_error') }));
        }
    } catch (error) {
        alert(window.I18N.t('settings.unable_to_save_settings', { reason: error.message }));
    }
}

// ===== Security (optional password protection) =====

async function loadSecurityStatus() {
    try {
        const response = await fetch('/api/security');
        const data = await response.json();
        if (data.ok) renderSecurityStatus(data);
    } catch (error) {
        console.warn('Security status load failed:', error);
    }
}

function renderSecurityStatus(data) {
    const enabledToggle = document.getElementById('securityEnabled');
    if (enabledToggle) enabledToggle.checked = !!data.enabled;

    const statusEl = document.getElementById('securityPasswordStatus');
    if (statusEl) {
        statusEl.textContent = data.password_set
            ? window.I18N.t('system.security_password_set_note')
            : window.I18N.t('system.security_password_not_set_note');
    }

    const saveBtn = document.getElementById('securitySavePasswordBtn');
    if (saveBtn) {
        saveBtn.textContent = data.password_set
            ? window.I18N.t('system.security_change_password_btn')
            : window.I18N.t('system.security_set_password_btn');
    }
}

function renderSecurityResult(message, isError) {
    const el = document.getElementById('securitySaveResult');
    if (!el) return;
    el.textContent = message;
    el.style.color = isError ? '#e14d68' : '';
}

async function toggleSecurityEnabled(checked) {
    try {
        const response = await fetch('/api/security', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: checked })
        });
        const data = await response.json();
        if (data.ok) {
            renderSecurityStatus(data);
            renderSecurityResult(window.I18N.t('system.security_saved'), false);
        } else {
            const enabledToggle = document.getElementById('securityEnabled');
            if (enabledToggle) enabledToggle.checked = !checked;
            const message = data.error_code === 'no_password_set'
                ? window.I18N.t('system.security_enable_requires_password')
                : window.I18N.t('system.security_save_failed', { reason: data.error || window.I18N.t('errors.unknown_error') });
            renderSecurityResult(message, true);
        }
    } catch (error) {
        const enabledToggle = document.getElementById('securityEnabled');
        if (enabledToggle) enabledToggle.checked = !checked;
        renderSecurityResult(window.I18N.t('system.security_save_failed', { reason: error.message }), true);
    }
}

async function saveSecurityPassword() {
    const newPasswordInput = document.getElementById('securityNewPassword');
    const confirmInput = document.getElementById('securityConfirmPassword');
    const newPassword = newPasswordInput?.value || '';
    const confirmPassword = confirmInput?.value || '';

    if (newPassword.length < 4) {
        renderSecurityResult(window.I18N.t('system.security_password_too_short'), true);
        return;
    }
    if (newPassword !== confirmPassword) {
        renderSecurityResult(window.I18N.t('system.security_password_mismatch'), true);
        return;
    }

    try {
        const response = await fetch('/api/security', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: newPassword })
        });
        const data = await response.json();
        if (data.ok) {
            renderSecurityStatus(data);
            renderSecurityResult(window.I18N.t('system.security_saved'), false);
            if (newPasswordInput) newPasswordInput.value = '';
            if (confirmInput) confirmInput.value = '';
        } else {
            renderSecurityResult(data.error || window.I18N.t('errors.unknown_error'), true);
        }
    } catch (error) {
        renderSecurityResult(window.I18N.t('system.security_save_failed', { reason: error.message }), true);
    }
}

async function securityLogout() {
    try {
        await fetch('/api/logout', { method: 'POST' });
    } catch (error) {
        // Best-effort - redirect regardless, /login itself is safe to load
        // even if this request failed.
    }
    window.location.href = '/login';
}

function renderUpdatesResult(html) {
    const panel = document.getElementById('updatesResultPanel');
    if (!panel) return;
    panel.style.display = 'block';
    panel.innerHTML = html;
}

// Polls /api/system/info after a restart, staged per the design worked out
// live on dev (Zero 2W): ~11-12s is typical (verify_radio_identity() alone
// takes ~4-5s of that), so 0-30s is treated as normal, 30-60s as slower
// than usual but not yet a problem, and 60s is a hard cutoff - never an
// infinite spinner. A response is only accepted as success if its
// app_version actually matches the version this update was applying -
// a response during the old process's last gasp before systemd kills it
// (or a stray successful request that raced the restart) must not be
// mistaken for the new one having actually started.
async function pollAfterUpdateRestart(expectedVersion, startedAt) {
    const tick = async () => {
        const elapsedS = Math.round((Date.now() - startedAt) / 1000);

        let versionMatches = false;
        try {
            const response = await fetch('/api/system/info', { cache: 'no-store' });
            if (response.ok) {
                const data = await response.json();
                versionMatches = !expectedVersion || data.app_version === expectedVersion;
            }
        } catch (e) {
            versionMatches = false;
        }

        if (versionMatches) {
            renderUpdatesResult(`
                <div class="updates-result-success">✅ ${escapeHtml(window.I18N.t('system.updates_restart_success'))}</div>
            `);
            setTimeout(() => window.location.reload(), 1500);
            return;
        }

        if (elapsedS >= 60) {
            const sha = _lastUpdatesStatus?.previous_version_sha || '';
            renderUpdatesResult(`
                <div class="updates-result-error">⚠️ ${escapeHtml(window.I18N.t('system.updates_restart_timeout'))}</div>
                ${sha ? `
                    <div class="updates-rollback-row">
                        <code class="updates-rollback-command">git checkout ${escapeHtml(sha)} && sudo systemctl restart meshcenter.service</code>
                        <button type="button" class="btn btn-xs" onclick="navigator.clipboard.writeText('git checkout ${escapeHtml(sha)} && sudo systemctl restart meshcenter.service')">${escapeHtml(window.I18N.t('modals.copy'))}</button>
                    </div>
                ` : ''}
                <div class="updates-result-hint">${escapeHtml(window.I18N.t('system.updates_restart_timeout_hint'))}</div>
                <button type="button" class="btn btn-xs" onclick="pollAfterUpdateRestart('${escapeHtml(expectedVersion)}', Date.now())">${escapeHtml(window.I18N.t('system.updates_keep_waiting'))}</button>
            `);
            return;
        }

        const stage = elapsedS < 30
            ? window.I18N.t('system.updates_restarting')
            : window.I18N.t('system.updates_restarting_slow');
        renderUpdatesResult(`<div class="updates-result-pending">⏳ ${escapeHtml(stage)}</div>`);

        setTimeout(tick, 2500);
    };

    tick();
}

async function applyUpdate(button) {
    if (button) button.disabled = true;

    try {
        const preflightResponse = await fetch('/api/updates/preflight');
        const preflight = await preflightResponse.json();

        if (!preflight.ok) {
            const reasons = {
                dirty_tree: window.I18N.t('system.updates_preflight_dirty', { files: (preflight.dirty_files || []).join(', ') }),
                diverged: window.I18N.t('system.updates_preflight_diverged'),
                up_to_date: window.I18N.t('system.updates_preflight_up_to_date'),
                no_upstream: window.I18N.t('system.updates_preflight_no_upstream'),
                fetch_failed: window.I18N.t('system.updates_preflight_fetch_failed'),
            };
            const message = reasons[preflight.reason] || preflight.error || window.I18N.t('errors.unknown_error');
            showToast(`❌ ${message}`, 'error');
            if (button) button.disabled = false;
            return;
        }

        const confirmMessage = window.I18N.t('system.updates_apply_confirm', {
            current: _lastUpdatesStatus?.current_version || '?',
            latest: _lastUpdatesStatus?.latest_version || '?'
        });
        if (!confirm(confirmMessage)) {
            if (button) button.disabled = false;
            return;
        }

        const applyResponse = await fetch('/api/updates/apply', { method: 'POST' });
        const applyData = await applyResponse.json();

        if (!applyResponse.ok || !applyData.ok) {
            showToast(`❌ ${applyData.error || window.I18N.t('errors.unknown_error')}`, 'error');
            if (button) button.disabled = false;
            return;
        }

        // Left disabled deliberately - the service is about to restart, and
        // the result panel (poll progress / success / timeout) takes over
        // as the primary status indicator until that resolves.
        const expectedVersion = _lastUpdatesStatus?.latest_version || '';
        pollAfterUpdateRestart(expectedVersion, Date.now());
    } catch (error) {
        showToast(`❌ ${error.message}`, 'error');
        if (button) button.disabled = false;
    }
}

