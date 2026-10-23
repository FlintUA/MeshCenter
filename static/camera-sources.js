// ============================================================
// NETWORK CAMERA SOURCES (Devices tab) - CAM2 Phase 1b
//
// Lists the saved network (RTSP) camera sources and lets the user add, edit
// and delete them. The password is write-only: it is typed into a password
// field, sent once, and never shown again (the server only says whether one
// is set). Nothing here ever puts a password into the page, a URL, a toast or
// the console.
// ============================================================
(function () {
    'use strict';

    const API = '/api/camera/sources';
    let mountedSection = null;

    function t(key, params) {
        return window.I18N.t(key, params);
    }

    function esc(value) {
        return typeof escapeHtml === 'function' ? escapeHtml(String(value == null ? '' : value)) : String(value == null ? '' : value);
    }

    // server error_code -> i18n key (anything unknown falls back to the generic text)
    const ERROR_KEYS = {
        not_lan_address: 'ipcamera.err_not_lan_address',
        loopback: 'ipcamera.err_loopback',
        own_address: 'ipcamera.err_own_address',
        metadata_address: 'ipcamera.err_not_lan_address',
        gateway_address: 'ipcamera.err_container_network',
        container_network: 'ipcamera.err_container_network',
        multicast: 'ipcamera.err_not_lan_address',
        invalid_host: 'ipcamera.err_invalid_host',
        unresolvable: 'ipcamera.err_unresolvable',
        duplicate_name: 'ipcamera.err_duplicate_name',
        too_many_sources: 'ipcamera.err_too_many_sources',
        missing_field: 'ipcamera.err_missing_field',
        invalid_field: 'ipcamera.err_invalid_field',
        path_is_url: 'ipcamera.err_path_is_url',
        rate_limited: 'ipcamera.err_rate_limited',
        storage_write_failed: 'ipcamera.err_storage',
    };

    function errorText(body) {
        const key = body && ERROR_KEYS[body.error_code];
        return t(key || 'ipcamera.err_generic');
    }

    async function request(method, url, payload) {
        const options = { method, headers: { 'Content-Type': 'application/json' } };
        if (payload !== undefined) options.body = JSON.stringify(payload);
        const response = await fetch(url, options);
        let body = null;
        try { body = await response.json(); } catch (error) { body = null; }
        return { ok: response.ok && body && body.ok, status: response.status, body: body || {} };
    }

    async function afterChange() {
        if (typeof loadPeripheralDevices === 'function') await loadPeripheralDevices(false);
        if (typeof loadCameraSourceSelector === 'function') await loadCameraSourceSelector();
        if (window.MCIpCamera) await window.MCIpCamera.refresh();
    }

    // ---------- list ----------

    function renderCard(source) {
        const profile = source.preferred_profile === 'main' ? t('ipcamera.profile_main') : t('ipcamera.profile_sub');
        return `
            <div class="mc-ipcam-row" data-source-id="${esc(source.id)}">
                <div class="mc-ipcam-row-main">
                    <div class="mc-ipcam-row-name">${esc(source.name)}</div>
                    <div class="mc-ipcam-row-meta">${esc(source.host)}:${esc(source.port)} · ${esc(profile)} · ${esc(source.transport.toUpperCase())}</div>
                </div>
                <div class="mc-ipcam-row-actions">
                    <button type="button" class="device-action-btn device-action-secondary" data-ipcam-action="edit">${esc(t('ipcamera.edit'))}</button>
                    <button type="button" class="device-action-btn device-action-secondary mc-ipcam-danger" data-ipcam-action="delete">${esc(t('ipcamera.delete'))}</button>
                </div>
            </div>`;
    }

    async function render(section) {
        let sources = [];
        try {
            const result = await request('GET', API);
            if (result.ok) sources = result.body.sources || [];
        } catch (error) { sources = []; }
        const rows = sources.length
            ? sources.map(renderCard).join('')
            : `<p class="mc-ipcam-empty">${esc(t('ipcamera.none'))}</p>`;
        section.innerHTML = `
            <div class="peripheral-card-header">
                <div>
                    <div class="device-card-eyebrow">${esc(t('ipcamera.subtitle'))}</div>
                    <h3>${esc(t('ipcamera.title'))}</h3>
                </div>
            </div>
            <div class="mc-ipcam-list">${rows}</div>
            <div class="device-action-row device-action-row-single">
                <button type="button" class="mc-refresh-btn" data-ipcam-action="add">${esc(t('ipcamera.add'))}</button>
            </div>
            <p class="mc-ipcam-note">${esc(t('ipcamera.lan_only_note'))}</p>`;
        section.querySelectorAll('[data-ipcam-action]').forEach((button) => {
            button.addEventListener('click', () => {
                const action = button.dataset.ipcamAction;
                const row = button.closest('[data-source-id]');
                const source = row ? sources.find((s) => s.id === row.dataset.sourceId) : null;
                if (action === 'add') openForm(null);
                if (action === 'edit' && source) openForm(source);
                if (action === 'delete' && source) confirmDelete(source);
            });
        });
    }

    function mount(container) {
        const grid = container.querySelector('.peripheral-grid') || container;
        mountedSection = document.createElement('section');
        mountedSection.className = 'peripheral-card mc-ipcam-card';
        mountedSection.id = 'ipCameraSources';
        grid.insertBefore(mountedSection, grid.firstChild);
        return render(mountedSection);
    }

    // ---------- delete (UI1 dialog, never the browser's confirm()) ----------

    async function confirmDelete(source) {
        const ok = await appConfirm({
            title: t('ipcamera.delete_title'),
            message: t('ipcamera.delete_confirm', { name: source.name }),
            confirmLabel: t('ipcamera.delete'),
            cancelLabel: t('ipcamera.cancel'),
            danger: true,
        });
        if (!ok) return;
        const result = await request('DELETE', `${API}/${encodeURIComponent(source.id)}`);
        if (result.ok) {
            showToast(t('ipcamera.deleted'), 'success');
            await afterChange();
        } else {
            showToast(errorText(result.body), 'error', { duration: 10000 });
        }
    }

    // ---------- add / edit form ----------

    function closeForm() {
        const overlay = document.getElementById('ipCameraFormModal');
        if (!overlay) return;
        if (overlay._onKeydown) document.removeEventListener('keydown', overlay._onKeydown, true);
        overlay.remove();
    }

    function field(id, labelKey, inner, hint) {
        return `
            <div class="mc-ipcam-field">
                <label for="${id}">${esc(t(labelKey))}</label>
                ${inner}
                ${hint ? `<div class="mc-ipcam-hint">${esc(hint)}</div>` : ''}
                <div class="mc-ipcam-error" id="${id}Error" role="alert"></div>
            </div>`;
    }

    function openForm(source) {
        closeForm();
        const editing = Boolean(source);
        const v = source || { name: '', host: '', port: 554, username: '', main_path: '', sub_path: '', preferred_profile: 'sub', transport: 'tcp', connect_timeout_s: 10, password_set: false };
        const overlay = document.createElement('div');
        overlay.id = 'ipCameraFormModal';
        overlay.className = 'modal-overlay mc-ipcam-overlay';
        const option = (value, selected, key) => `<option value="${value}"${value === selected ? ' selected' : ''}>${esc(t(key))}</option>`;
        overlay.innerHTML = `
            <div class="modal-content mc-ipcam-modal" role="dialog" aria-modal="true" aria-labelledby="ipCameraFormTitle">
                <h3 id="ipCameraFormTitle">${esc(t(editing ? 'ipcamera.form_title_edit' : 'ipcamera.form_title_add'))}</h3>
                <form id="ipCameraForm" autocomplete="off" novalidate>
                    ${field('ipcamName', 'ipcamera.field_name', `<input id="ipcamName" name="name" type="text" maxlength="60" value="${esc(v.name)}" required>`)}
                    ${field('ipcamHost', 'ipcamera.field_host', `<input id="ipcamHost" name="host" type="text" inputmode="url" maxlength="253" value="${esc(v.host)}" required>`, t('ipcamera.host_help'))}
                    <div class="mc-ipcam-two">
                        ${field('ipcamPort', 'ipcamera.field_port', `<input id="ipcamPort" name="port" type="number" min="1" max="65535" value="${esc(v.port)}">`)}
                        ${field('ipcamUser', 'ipcamera.field_username', `<input id="ipcamUser" name="username" type="text" maxlength="64" autocomplete="off" value="${esc(v.username)}">`)}
                    </div>
                    ${field('ipcamPassword', 'ipcamera.field_password', `<input id="ipcamPassword" name="password" type="password" maxlength="128" autocomplete="new-password" value="" placeholder="${esc(v.password_set ? t('ipcamera.password_is_set') : '')}">`, t(editing && v.password_set ? 'ipcamera.password_keep_hint' : 'ipcamera.password_storage_note'))}
                    ${editing && v.password_set ? `<label class="mc-ipcam-check"><input id="ipcamClearPassword" type="checkbox"> ${esc(t('ipcamera.clear_password'))}</label>` : ''}
                    <div class="mc-ipcam-two">
                        ${field('ipcamSubPath', 'ipcamera.field_sub_path', `<input id="ipcamSubPath" name="sub_path" type="text" maxlength="256" value="${esc(v.sub_path)}" placeholder="Preview_01_sub">`, t('ipcamera.path_help'))}
                        ${field('ipcamMainPath', 'ipcamera.field_main_path', `<input id="ipcamMainPath" name="main_path" type="text" maxlength="256" value="${esc(v.main_path)}" placeholder="Preview_01_main">`)}
                    </div>
                    <div class="mc-ipcam-two">
                        ${field('ipcamProfile', 'ipcamera.field_profile', `<select id="ipcamProfile" name="preferred_profile">${option('sub', v.preferred_profile, 'ipcamera.profile_sub')}${option('main', v.preferred_profile, 'ipcamera.profile_main')}</select>`)}
                        ${field('ipcamTransport', 'ipcamera.field_transport', `<select id="ipcamTransport" name="transport">${option('tcp', v.transport, 'ipcamera.transport_tcp')}${option('udp', v.transport, 'ipcamera.transport_udp')}</select>`, t('ipcamera.transport_hint'))}
                    </div>
                    ${field('ipcamTimeout', 'ipcamera.field_timeout', `<input id="ipcamTimeout" name="connect_timeout_s" type="number" min="2" max="60" value="${esc(v.connect_timeout_s)}">`)}
                    <div class="mc-ipcam-actions">
                        <button type="button" class="device-action-btn device-action-secondary" data-ipcam-form="cancel">${esc(t('ipcamera.cancel'))}</button>
                        <button type="submit" class="device-action-btn device-action-primary is-primary" data-ipcam-form="save">${esc(t('ipcamera.save'))}</button>
                    </div>
                </form>
            </div>`;

        overlay.querySelector('[data-ipcam-form="cancel"]').addEventListener('click', closeForm);
        overlay.addEventListener('click', (event) => { if (event.target === overlay) closeForm(); });
        overlay._onKeydown = (event) => {
            if (event.key === 'Escape') { event.stopPropagation(); closeForm(); }
        };
        document.addEventListener('keydown', overlay._onKeydown, true);
        overlay.querySelector('#ipCameraForm').addEventListener('submit', (event) => {
            event.preventDefault();
            submitForm(overlay, source);
        });
        document.body.appendChild(overlay);
        overlay.querySelector('#ipcamName').focus();
    }

    function clearErrors(overlay) {
        overlay.querySelectorAll('.mc-ipcam-error').forEach((node) => { node.textContent = ''; });
    }

    const FIELD_IDS = {
        name: 'ipcamName', host: 'ipcamHost', port: 'ipcamPort', username: 'ipcamUser', password: 'ipcamPassword',
        sub_path: 'ipcamSubPath', main_path: 'ipcamMainPath', preferred_profile: 'ipcamProfile',
        transport: 'ipcamTransport', connect_timeout_s: 'ipcamTimeout',
    };

    async function submitForm(overlay, source) {
        clearErrors(overlay);
        const value = (id) => overlay.querySelector('#' + id).value;
        const payload = {
            name: value('ipcamName').trim(),
            host: value('ipcamHost').trim(),
            port: parseInt(value('ipcamPort'), 10),
            username: value('ipcamUser').trim(),
            main_path: value('ipcamMainPath').trim(),
            sub_path: value('ipcamSubPath').trim(),
            preferred_profile: value('ipcamProfile'),
            transport: value('ipcamTransport'),
            connect_timeout_s: parseInt(value('ipcamTimeout'), 10),
        };
        if (Number.isNaN(payload.port)) payload.port = 0;
        if (Number.isNaN(payload.connect_timeout_s)) payload.connect_timeout_s = 0;
        const password = value('ipcamPassword');
        const clear = overlay.querySelector('#ipcamClearPassword');
        if (password) payload.password = password;
        else if (clear && clear.checked) payload.clear_password = true;

        const save = overlay.querySelector('[data-ipcam-form="save"]');
        save.disabled = true;
        let result;
        try {
            result = source
                ? await request('PUT', `${API}/${encodeURIComponent(source.id)}`, payload)
                : await request('POST', API, payload);
        } catch (error) {
            save.disabled = false;
            showToast(t('errors.network_error'), 'error', { duration: 10000 });
            return;
        }
        overlay.querySelector('#ipcamPassword').value = '';  // the secret leaves the page as soon as it was sent
        save.disabled = false;
        if (result.ok) {
            closeForm();
            showToast(t('ipcamera.saved'), 'success');
            await afterChange();
            return;
        }
        const fieldId = FIELD_IDS[result.body.field];
        const text = errorText(result.body);
        const target = fieldId && overlay.querySelector('#' + fieldId + 'Error');
        if (target) {
            target.textContent = text;
            overlay.querySelector('#' + fieldId).focus();
        } else {
            showToast(text, 'error', { duration: 10000 });
        }
    }

    window.MCCameraSources = { mount, render, openForm, errorText, _errorKeys: ERROR_KEYS };
})();
