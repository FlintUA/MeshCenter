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

    renderUpdatesLastUpdate(status);
    renderUpdatesManualSteps(status);

    const autoCheckEl = document.getElementById('updatesAutoCheckEnabled');
    if (autoCheckEl) autoCheckEl.checked = appSettings?.updates?.auto_check !== false;
}

// UPD1: "Last update: 1.12.0 -> 1.12.1 (date)" plus, after a successful update, a Roll back
// button and the exact manual command (always visible, also when the button is blocked).
function updatesRollbackCommand(status) {
    const sha = status?.previous_version_sha || '';
    if (!sha) return '';
    const move = status.previous_ref === 'detached' ? `git checkout --detach ${sha}` : `git reset --keep ${sha}`;
    return `${move} && sudo systemctl restart meshcenter.service`;
}

function renderUpdatesLastUpdate(status) {
    const lastEl = document.getElementById('updatesLastUpdate');
    const rollbackBtn = document.getElementById('updatesRollbackBtn');
    const rollbackRow = document.getElementById('updatesRollbackRow');
    const rollbackSection = document.getElementById('updatesRollbackSection');
    const commandEl = document.getElementById('updatesRollbackCommand');
    const last = status.last_update;

    if (lastEl) {
        if (last && last.from && last.to) {
            const key = `system.updates_last_${last.kind === 'rollback' ? 'rollback' : 'update'}_${last.ok ? 'ok' : 'failed'}`;
            lastEl.textContent = window.I18N.t(key, {
                from: last.from,
                to: last.to,
                when: last.at ? new Date(last.at * 1000).toLocaleString() : '',
            });
            lastEl.style.display = 'block';
        } else {
            lastEl.style.display = 'none';
        }
    }

    const command = status.rollback_available ? updatesRollbackCommand(status) : '';
    if (rollbackBtn) rollbackBtn.style.display = status.rollback_available ? 'inline-block' : 'none';
    // UX1: Roll back and its manual command live in their own section, the command collapsed in a <details>
    if (rollbackSection) rollbackSection.style.display = status.rollback_available ? 'block' : 'none';
    if (rollbackRow) rollbackRow.style.display = command ? 'block' : 'none';
    if (commandEl) commandEl.textContent = command;
}

function copyUpdatesRollbackCommand() {
    const command = updatesRollbackCommand(_lastUpdatesStatus);
    if (command && navigator.clipboard) navigator.clipboard.writeText(command);
}

// Why an update / rollback was refused, as a sentence (same keys for both).
function updatesRefusalMessage(preflight) {
    const t = (key, params) => window.I18N.t(key, params);
    const latest = _lastUpdatesStatus?.latest_version || '';
    const reasons = {
        dirty_tree: () => t('system.updates_preflight_dirty', { files: (preflight.dirty_files || []).join(', ') }),
        diverged: () => t('system.updates_preflight_diverged'),
        up_to_date: () => t('system.updates_preflight_up_to_date'),
        ahead: () => t('system.updates_preflight_ahead', { version: latest }),
        unsupported_branch: () => t('system.updates_preflight_branch', { branch: preflight.branch || '?' }),
        tag_missing: () => t('system.updates_preflight_tag_missing', { tag: preflight.target_tag || '' }),
        no_target: () => t('system.updates_preflight_no_target'),
        fetch_failed: () => t('system.updates_preflight_fetch_failed'),
        rollback_unavailable: () => t('system.updates_rollback_unavailable'),
    };
    const message = reasons[preflight.reason] ? reasons[preflight.reason]() : '';
    return message || preflight.error || t('errors.unknown_error');
}

// The persistent "blocked: dependency files changed" panel (update and rollback share it).
function renderUpdatesBlockedPanel(titleKey, bodyKey, bodyParams, data) {
    renderUpdatesResult(`
        <div class="updates-result-error">⚠️ ${escapeHtml(window.I18N.t(titleKey))}</div>
        <div class="updates-result-hint">${escapeHtml(window.I18N.t(bodyKey, bodyParams))}</div>
        <div class="updates-result-hint">${escapeHtml(window.I18N.t('system.updates_blocked_deps_instructions'))}</div>
        <div class="updates-rollback-row">
            <code class="updates-rollback-command">${escapeHtml(data.instructions || '')}</code>
            <button type="button" class="btn btn-xs" data-updates-copy="${escapeHtml(data.instructions || '')}">${escapeHtml(window.I18N.t('modals.copy'))}</button>
        </div>
    `);
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
            showToast(window.I18N.t('settings.unable_to_save_settings', { reason: data.error || window.I18N.t('errors.unknown_error') }), 'error', { duration: 10000 });
        }
    } catch (error) {
        showToast(window.I18N.t('settings.unable_to_save_settings', { reason: error.message }), 'error', { duration: 10000 });
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

    // H1-A2 (security review, F12): a current password is only ever
    // required once one actually exists - the very first password set
    // (data.password_set === false) has nothing to re-check against.
    const currentPasswordWrap = document.getElementById('securityCurrentPasswordWrap');
    if (currentPasswordWrap) currentPasswordWrap.style.display = data.password_set ? 'block' : 'none';
}

function _securityCurrentPasswordErrorMessage(data) {
    if (data.error_code === 'current_password_invalid') {
        return window.I18N.t('system.security_current_password_invalid');
    }
    if (data.error_code === 'login_throttled') {
        return data.retry_after
            ? window.I18N.t('auth.login_throttled', { seconds: data.retry_after })
            : window.I18N.t('system.security_current_password_throttled');
    }
    return null;
}

function renderSecurityResult(message, isError) {
    const el = document.getElementById('securitySaveResult');
    if (!el) return;
    el.textContent = message;
    el.style.color = isError ? '#e14d68' : '';
}

async function toggleSecurityEnabled(checked) {
    // H1-A2 (security review, F12): disabling protection requires proving
    // you still know the current password - the same field the password-
    // change form uses, since both actions share one "prove it's really
    // you" gate. Enabling (checked === true) never requires it.
    const currentPasswordInput = document.getElementById('securityCurrentPassword');
    const body = { enabled: checked };
    if (!checked && currentPasswordInput) {
        body.current_password = currentPasswordInput.value || '';
        // An empty field can only fail on the server - and every failed attempt counts toward the lock-out.
        // So ask for the password first: put the box back, say what is missing, send nothing.
        if (!body.current_password) {
            const toggle = document.getElementById('securityEnabled');
            if (toggle) toggle.checked = true;
            renderSecurityResult(window.I18N.t('system.security_current_password_required'), true);
            currentPasswordInput.focus();
            return;
        }
    }

    try {
        const response = await fetch('/api/security', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();
        if (data.ok) {
            renderSecurityStatus(data);
            renderSecurityResult(window.I18N.t('system.security_saved'), false);
            if (currentPasswordInput) currentPasswordInput.value = '';
        } else {
            const enabledToggle = document.getElementById('securityEnabled');
            if (enabledToggle) enabledToggle.checked = !checked;
            const message = _securityCurrentPasswordErrorMessage(data)
                || (data.error_code === 'no_password_set'
                    ? window.I18N.t('system.security_enable_requires_password')
                    : data.error_code === 'storage_write_failed'
                    ? window.I18N.t('errors.storage_write_failed')
                    : window.I18N.t('system.security_save_failed', { reason: data.error || window.I18N.t('errors.unknown_error') }));
            renderSecurityResult(message, true);
        }
    } catch (error) {
        const enabledToggle = document.getElementById('securityEnabled');
        if (enabledToggle) enabledToggle.checked = !checked;
        renderSecurityResult(window.I18N.t('system.security_save_failed', { reason: error.message }), true);
    }
}

async function saveSecurityPassword() {
    const currentPasswordInput = document.getElementById('securityCurrentPassword');
    const newPasswordInput = document.getElementById('securityNewPassword');
    const confirmInput = document.getElementById('securityConfirmPassword');
    const newPassword = newPasswordInput?.value || '';
    const confirmPassword = confirmInput?.value || '';

    if (newPassword.length < 12) {
        renderSecurityResult(window.I18N.t('system.security_password_too_short'), true);
        return;
    }
    if (newPassword !== confirmPassword) {
        renderSecurityResult(window.I18N.t('system.security_password_mismatch'), true);
        return;
    }

    const body = { password: newPassword };
    // H1-A2 (security review, F12): only sent once a password already
    // exists (the field itself is hidden otherwise, see
    // renderSecurityStatus()) - the server only requires it in that case
    // too, so sending an empty string for a brand-new setup is harmless.
    if (currentPasswordInput) {
        body.current_password = currentPasswordInput.value || '';
    }

    try {
        const response = await fetch('/api/security', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();
        if (data.ok) {
            renderSecurityStatus(data);
            renderSecurityResult(window.I18N.t('system.security_saved'), false);
            if (currentPasswordInput) currentPasswordInput.value = '';
            if (newPasswordInput) newPasswordInput.value = '';
            if (confirmInput) confirmInput.value = '';
        } else {
            const message = _securityCurrentPasswordErrorMessage(data)
                || (data.error_code === 'storage_write_failed'
                    ? window.I18N.t('errors.storage_write_failed')
                    : data.error || window.I18N.t('errors.unknown_error'));
            renderSecurityResult(message, true);
        }
    } catch (error) {
        renderSecurityResult(window.I18N.t('system.security_save_failed', { reason: error.message }), true);
    }
}

// AUTH2: ends every session (the global auth_version is bumped) and any open camera stream; this browser too
async function securityLogoutAll() {
    const proceed = await appConfirm({
        title: window.I18N.t('system.security_logout_all_title'),
        message: window.I18N.t('system.security_logout_all_confirm'),
        confirmLabel: window.I18N.t('system.security_logout_all_btn'),
        danger: true,
    });
    if (!proceed) return;
    try {
        const response = await fetch('/api/auth/logout_all', { method: 'POST' });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) {
            showToast(data.error_code === 'auth_not_enabled'
                ? window.I18N.t('system.security_logout_all_not_enabled')
                : window.I18N.t('system.security_logout_all_failed'), 'error', { duration: 10000 });
            return;
        }
    } catch (error) {
        showToast(window.I18N.t('system.security_logout_all_failed'), 'error', { duration: 10000 });
        return;
    }
    window.location.href = '/login';
}

(function wireSecurityLogoutAll() {
    const wire = () => {
        const button = document.getElementById('securityLogoutAllBtn');
        if (button && typeof button.addEventListener === 'function') button.addEventListener('click', securityLogoutAll);
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
    else wire();
})();

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
    wireUpdatesCopyButtons(panel);
}

// Copy buttons carry their text in data-updates-copy (no inline handler built from a template literal).
function wireUpdatesCopyButtons(container) {
    container.querySelectorAll('[data-updates-copy]').forEach((button) => {
        button.addEventListener('click', () => {
            if (navigator.clipboard) navigator.clipboard.writeText(button.getAttribute('data-updates-copy') || '');
        });
    });
}

// v1.12.2: an update that changes host files MeshCenter never installs by itself (systemd unit,
// sudoers templates, udev rule, privileged helpers) is NOT blocked; this notice says which files
// and gives the exact command for each. Shown with the "Last update" line until the next update.
function renderUpdatesManualSteps(status) {
    const box = document.getElementById('updatesManualStep');
    if (!box) return;
    const last = status.last_update;
    const steps = last && last.ok && Array.isArray(last.manual_steps) ? last.manual_steps : [];
    if (!steps.length) {
        box.style.display = 'none';
        box.innerHTML = '';
        return;
    }
    box.innerHTML = `
        <div class="updates-result-error">⚠️ ${escapeHtml(window.I18N.t('system.updates_manual_title'))}</div>
        <div class="updates-result-hint">${escapeHtml(window.I18N.t('system.updates_manual_hint'))}</div>
        ${steps.map((step) => `
            <div class="updates-result-hint"><code>${escapeHtml(step.file)}</code></div>
            <div class="updates-rollback-row">
                <code class="updates-rollback-command">${escapeHtml(step.command)}</code>
                <button type="button" class="btn btn-xs" data-updates-copy="${escapeHtml(step.command)}">${escapeHtml(window.I18N.t('modals.copy'))}</button>
            </div>`).join('')}
    `;
    box.style.display = 'block';
    wireUpdatesCopyButtons(box);
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

// UX1: the preflight before the confirm dialog is a git fetch (several seconds): say so at once, on the status
// line and the button, and put both back as soon as it is over (refused, cancelled, failed or confirmed).
function _setUpdatesChecking(button, on, originalLabel) {
    const statusText = document.getElementById('updatesStatusText');
    if (on) {
        if (statusText) statusText.textContent = window.I18N.t('system.updates_checking');
        if (button) button.textContent = window.I18N.t('system.updates_checking');
        return;
    }
    if (button && originalLabel) button.textContent = originalLabel;
    if (_lastUpdatesStatus) renderUpdatesStatus(_lastUpdatesStatus);
}

async function applyUpdate(button) {
    const originalLabel = button ? button.textContent : '';
    if (button) button.disabled = true;
    _setUpdatesChecking(button, true);

    try {
        const preflightResponse = await fetch('/api/updates/preflight');
        const preflight = await preflightResponse.json();
        _setUpdatesChecking(button, false, originalLabel);

        if (!preflight.ok) {
            const message = updatesRefusalMessage(preflight);
            showToast(`❌ ${message}`, 'error');
            if (button) button.disabled = false;
            return;
        }

        const confirmMessage = window.I18N.t('system.updates_apply_confirm', {
            current: _lastUpdatesStatus?.current_version || '?',
            latest: _lastUpdatesStatus?.latest_version || '?'
        });
        const proceed = await appConfirm({
            title: window.I18N.t('dialog.title_apply_update'),
            message: confirmMessage,
            confirmLabel: window.I18N.t('dialog.update'),
            danger: true,
        });
        if (!proceed) {
            if (button) button.disabled = false;
            return;
        }

        const applyResponse = await fetch('/api/updates/apply', { method: 'POST' });
        const applyData = await applyResponse.json();

        // PR #231 review (3rd pass): a blocked update (dependency files
        // changed - see meshsrv/update_service.py's apply_update()) is a
        // distinct, non-error outcome from a real failure: nothing went
        // wrong, HEAD/the working tree are untouched, and no restart was
        // ever scheduled. Shown as a persistent result panel (not a
        // transient toast) with the manual steps, since this needs to
        // stay readable/copyable, not disappear after a few seconds -
        // the button is re-enabled and pollAfterUpdateRestart() is never
        // called, since there is nothing restarting to poll for.
        if (applyResponse.status === 409 && applyData.blocked) {
            renderUpdatesBlockedPanel('system.updates_blocked_deps_title', 'system.updates_blocked_deps_body', {
                files: (applyData.changed_requirements_files || []).join(', '),
            }, applyData);
            if (button) button.disabled = false;
            return;
        }

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
        _setUpdatesChecking(button, false, originalLabel);
        showToast(`❌ ${error.message}`, 'error');
        if (button) button.disabled = false;
    }
}

// UPD1: one-click rollback to the version recorded by the last update. Explicit only, a danger
// dialog naming both versions; blocked (manual command shown) when the dependency files differ.
async function rollbackUpdate(button) {
    const current = _lastUpdatesStatus?.current_version || '?';
    const previous = _lastUpdatesStatus?.previous_version || '?';
    if (button) button.disabled = true;

    try {
        const proceed = await appConfirm({
            title: window.I18N.t('system.updates_rollback_title'),
            message: window.I18N.t('system.updates_rollback_confirm', { current, previous }),
            confirmLabel: window.I18N.t('dialog.rollback'),
            danger: true,
        });
        if (!proceed) {
            if (button) button.disabled = false;
            return;
        }

        const response = await fetch('/api/updates/rollback', { method: 'POST' });
        const data = await response.json();

        if (response.status === 409 && data.blocked) {
            renderUpdatesBlockedPanel('system.updates_rollback_blocked_title', 'system.updates_rollback_blocked_body', {
                files: (data.changed_requirements_files || []).join(', '),
                previous,
            }, data);
            if (button) button.disabled = false;
            return;
        }

        if (!response.ok || !data.ok) {
            const message = data.preflight ? updatesRefusalMessage(data.preflight) : (data.error || window.I18N.t('errors.unknown_error'));
            showToast(`❌ ${message}`, 'error');
            if (button) button.disabled = false;
            return;
        }

        pollAfterUpdateRestart(data.to_version || previous, Date.now());
    } catch (error) {
        showToast(`❌ ${error.message}`, 'error');
        if (button) button.disabled = false;
    }
}

// ---- MCA-UX1: "a new version is installed" notice ---------------------------------------------------------------
// The page carries the version it was rendered with (<meta name="app-version">). When the server reports another one
// (an update happened while this tab stayed open) a clear notice with a Reload button appears; nothing changes
// silently, and the old scripts keep working until the user reloads.
let pageVersionNoticeShown = false;

function showPageVersionNotice(version) {
  if (pageVersionNoticeShown) return;
  pageVersionNoticeShown = true;
  const bar = document.createElement('div');
  bar.id = 'pageVersionNotice';
  bar.className = 'page-version-notice';
  bar.setAttribute('role', 'status');
  const text = document.createElement('span');
  text.textContent = window.I18N.t('common.new_version_notice', { version });
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'page-version-notice-btn';
  button.textContent = window.I18N.t('common.reload');
  button.addEventListener('click', () => window.location.reload());
  bar.append(text, button);
  document.body.appendChild(bar);
}

async function checkPageVersion() {
  if (pageVersionNoticeShown) return;
  const meta = document.querySelector('meta[name="app-version"]');
  const loaded = meta ? meta.getAttribute('content') : '';
  try {
    const response = await fetch('/api/system/info', { cache: 'no-store' });
    if (!response.ok) return;
    const info = await response.json();
    if (window.MCViews.pageVersionChanged(loaded, info.app_version)) showPageVersionNotice(info.app_version);
  } catch (e) { /* offline or restarting: the next check decides */ }
}

function startPageVersionWatch() {
  setTimeout(checkPageVersion, 15000);
  setInterval(checkPageVersion, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkPageVersion(); });
}
