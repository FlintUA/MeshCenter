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
        http_not_allowed: 'ipcamera.control_err_http_not_allowed',
    };

    // The code the server sent (or the HTTP status when there is none) so a failure is never just "could not be saved".
    function errorText(body, status) {
        const code = body && body.error_code;
        const key = code && ERROR_KEYS[code];
        if (key) return t(key);
        if (code === 'csrf_invalid') return t('errors.csrf_invalid');
        if (code || status) return t('ipcamera.err_generic_code', { code: code || '-', status: status || '-' });
        return t('ipcamera.err_generic');
    }

    async function request(method, url, payload) {
        const options = { method, headers: { 'Content-Type': 'application/json' } };
        if (payload !== undefined) options.body = JSON.stringify(payload);
        const response = await fetch(url, options);
        let body = null;
        try { body = await response.json(); } catch (error) { body = null; }
        if (!(response.ok && body && body.ok)) {
            // path and codes only: never the body (it may echo a field) or the password
            console.warn('[CAMERA SOURCES] request failed', method, url, 'HTTP', response.status, 'error_code', body && body.error_code);
        }
        return { ok: response.ok && body && body.ok, status: response.status, body: body || {} };
    }

    async function afterChange() {
        if (typeof loadPeripheralDevices === 'function') await loadPeripheralDevices(false);
        if (typeof loadCameraSourceSelector === 'function') await loadCameraSourceSelector();
        if (window.MCIpCamera) await window.MCIpCamera.refresh();
    }

    // ---------- list ----------

    function featureList(caps) {
        const features = [];
        if (!caps) return features;
        if (caps.snapshot) features.push(t('ipcamera.control_feature_snapshot'));
        if (caps.zoom && Array.isArray(caps.zoom_range)) {
            // the position is a point in time: shown right after a check, not in the saved summary
            features.push(caps.zoom_pos == null
                ? t('ipcamera.control_feature_zoom_range', { min: caps.zoom_range[0], max: caps.zoom_range[1] })
                : t('ipcamera.control_feature_zoom', { position: caps.zoom_pos, min: caps.zoom_range[0], max: caps.zoom_range[1] }));
        } else if (caps.zoom) {
            features.push(t('ipcamera.control_feature_zoom_range', { min: '-', max: '-' }));
        }
        if (caps.focus_read) features.push(t('ipcamera.control_feature_focus'));
        return features;
    }

    function controlMeta(source) {
        const control = source.control || {};
        if (control.type !== 'reolink') return '';
        const caps = control.capabilities || {};
        const found = caps.probed_at ? featureList(caps).join(', ') : '';
        return `<div class="mc-ipcam-row-meta">${esc(t('ipcamera.control_badge'))}${found ? ' · ' + esc(found) : ''}</div>`;
    }

    function renderCard(source) {
        const profile = source.preferred_profile === 'main' ? t('ipcamera.profile_main') : t('ipcamera.profile_sub');
        const hasControl = Boolean(source.control && source.control.type === 'reolink');
        return `
            <div class="mc-ipcam-row" data-source-id="${esc(source.id)}">
                <div class="mc-ipcam-row-main">
                    <div class="mc-ipcam-row-name">${esc(source.name)}</div>
                    <div class="mc-ipcam-row-meta">${esc(source.host)}:${esc(source.port)} · ${esc(profile)} · ${esc(source.transport.toUpperCase())}</div>
                    ${controlMeta(source)}
                </div>
                <div class="mc-ipcam-row-actions">
                    ${hasControl ? `<button type="button" class="device-action-btn device-action-secondary" data-ipcam-action="probe">${esc(t('ipcamera.control_check'))}</button>` : ''}
                    <button type="button" class="device-action-btn device-action-secondary" data-ipcam-action="edit">${esc(t('ipcamera.edit'))}</button>
                    <button type="button" class="device-action-btn device-action-secondary mc-ipcam-danger" data-ipcam-action="delete">${esc(t('ipcamera.delete'))}</button>
                </div>
            </div>`;
    }

    async function render(section) {
        let sources = [];
        let noFfmpeg = null;
        try {
            const result = await request('GET', API);
            if (result.ok) {
                sources = result.body.sources || [];
                if (result.body.ffmpeg_available === false) {
                    noFfmpeg = result.body.container ? 'ipcamera.no_ffmpeg_container' : 'ipcamera.no_ffmpeg_native';
                }
            }
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
            ${noFfmpeg ? `<p class="mc-ipcam-hint mc-ipcam-hint-warn" role="alert">${esc(t(noFfmpeg))}</p>` : ''}
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
                if (action === 'probe' && source) probeControl(source, button);
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

    // ---------- camera control: check, trust a certificate ----------

    const CONTROL_ERRORS = {
        login_failed: 'ipcamera.control_err_login_failed',
        too_many_sessions: 'ipcamera.control_err_too_many_sessions',
        unreachable: 'ipcamera.control_err_unreachable',
        timeout: 'ipcamera.control_err_timeout',
        tls_error: 'ipcamera.control_err_tls_error',
        http_not_allowed: 'ipcamera.control_err_http_not_allowed',
        address_blocked: 'ipcamera.control_err_address_blocked',
        not_supported: 'ipcamera.control_err_not_supported',
        rate_limited: 'ipcamera.control_err_rate_limited',
    };
    const RIGHTS = {
        snapshot: 'ipcamera.control_right_snapshot',
        lens_control: 'ipcamera.control_right_lens_control',
        live_view: 'ipcamera.control_right_live_view',
    };

    function rightText(right) {
        return t(RIGHTS[right] || 'ipcamera.control_right_unknown');
    }

    function controlErrorText(body) {
        const code = body && body.error_code;
        if (code === 'permission_denied') return t('ipcamera.control_denied', { right: rightText(body.right) });
        if (CONTROL_ERRORS[code]) return t(CONTROL_ERRORS[code]);
        return t('ipcamera.control_err_generic', { code: code || '-' });
    }

    // aa:bb:cc... in upper case, the form the camera's own pages and browsers show
    function formatFingerprint(value) {
        return String(value || '').toUpperCase().replace(/(.{2})(?=.)/g, '$1:');
    }

    async function probeControl(source, button) {
        const base = `${API}/${encodeURIComponent(source.id)}/control`;
        if (button) button.disabled = true;
        try {
            if (typeof showToast === 'function') showToast(t('ipcamera.control_checking'), 'info', { duration: 3000 });
            let result = await request('POST', base + '/probe');
            const code = result.body && result.body.error_code;
            if (!result.ok && (code === 'tls_untrusted' || code === 'tls_pin_mismatch') && result.body.fingerprint) {
                const changed = code === 'tls_pin_mismatch';
                const trusted = await appConfirm({
                    title: t(changed ? 'ipcamera.control_changed_title' : 'ipcamera.control_trust_title'),
                    message: t(changed ? 'ipcamera.control_changed_message' : 'ipcamera.control_trust_message', { fingerprint: formatFingerprint(result.body.fingerprint) }),
                    confirmLabel: t('ipcamera.control_trust_confirm'),
                    cancelLabel: t('ipcamera.cancel'),
                    danger: changed,
                });
                if (!trusted) return;
                const trust = await request('POST', base + '/trust', { sha256: result.body.fingerprint });
                if (!trust.ok) {
                    showToast(controlErrorText(trust.body), 'error', { duration: 10000 });
                    return;
                }
                result = await request('POST', base + '/probe');
            }
            if (!result.ok) {
                showToast(controlErrorText(result.body), 'error', { duration: 10000 });
                return;
            }
            const caps = result.body.capabilities || {};
            const features = featureList(caps);
            if (features.length) showToast(t('ipcamera.control_ok', { features: features.join(', ') }), 'success', { duration: 8000 });
            else showToast(t('ipcamera.control_nothing'), 'warning', { duration: 10000 });
            (caps.denied || []).forEach((entry) => {
                showToast(t('ipcamera.control_denied', { right: rightText(entry.right) }), 'warning', { duration: 10000 });
            });
            await afterChange();
        } finally {
            if (button) button.disabled = false;
        }
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
            showToast(errorText(result.body, result.status), 'error', { duration: 10000 });
        }
    }

    // ---------- add / edit form ----------

    function closeForm() {
        const overlay = document.getElementById('ipCameraFormModal');
        if (!overlay) return;
        if (overlay._onKeydown) document.removeEventListener('keydown', overlay._onKeydown, true);
        overlay.remove();
    }

    function field(id, labelKey, inner, hint, extraHintId) {
        return `
            <div class="mc-ipcam-field">
                <label for="${id}">${esc(t(labelKey))}</label>
                ${inner}
                ${hint ? `<div class="mc-ipcam-hint">${esc(hint)}</div>` : ''}
                ${extraHintId ? `<div class="mc-ipcam-hint mc-ipcam-hint-warn" id="${extraHintId}" hidden>${esc(t('ipcamera.profile_main_hint'))}</div>` : ''}
                <div class="mc-ipcam-error" id="${id}Error" role="alert"></div>
            </div>`;
    }

    function openForm(source) {
        closeForm();
        const editing = Boolean(source);
        const v = source || { name: '', host: '', port: 554, username: '', main_path: '', sub_path: '', preferred_profile: 'sub', transport: 'tcp', connect_timeout_s: 10, password_set: false };
        const c = Object.assign({ type: 'none', scheme: 'https', port: 443, channel: 0, allow_http: false }, v.control || {});
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
                        ${field('ipcamProfile', 'ipcamera.field_profile', `<select id="ipcamProfile" name="preferred_profile">${option('sub', v.preferred_profile, 'ipcamera.profile_sub')}${option('main', v.preferred_profile, 'ipcamera.profile_main')}</select>`, null, 'ipcamProfileHint')}
                        ${field('ipcamTransport', 'ipcamera.field_transport', `<select id="ipcamTransport" name="transport">${option('tcp', v.transport, 'ipcamera.transport_tcp')}${option('udp', v.transport, 'ipcamera.transport_udp')}</select>`, t('ipcamera.transport_hint'))}
                    </div>
                    ${field('ipcamTimeout', 'ipcamera.field_timeout', `<input id="ipcamTimeout" name="connect_timeout_s" type="number" min="2" max="60" value="${esc(v.connect_timeout_s)}">`)}
                    <fieldset class="mc-ipcam-control">
                        <legend>${esc(t('ipcamera.control_title'))}</legend>
                        <div class="mc-ipcam-hint">${esc(t('ipcamera.control_help'))}</div>
                        ${field('ipcamCtlType', 'ipcamera.control_type_label', `<select id="ipcamCtlType">${option('none', c.type, 'ipcamera.control_none')}${option('reolink', c.type, 'ipcamera.control_reolink')}</select>`)}
                        <div id="ipcamCtlBody">
                            <div class="mc-ipcam-two">
                                ${field('ipcamCtlScheme', 'ipcamera.control_scheme_label', `<select id="ipcamCtlScheme">${option('https', c.scheme, 'ipcamera.control_https')}${option('http', c.scheme, 'ipcamera.control_http')}</select>`)}
                                ${field('ipcamCtlPort', 'ipcamera.control_port_label', `<input id="ipcamCtlPort" type="number" min="1" max="65535" value="${esc(c.port)}">`)}
                            </div>
                            ${field('ipcamCtlChannel', 'ipcamera.control_channel_label', `<input id="ipcamCtlChannel" type="number" min="0" max="31" value="${esc(c.channel)}">`)}
                            <div id="ipcamCtlHttpBox" hidden>
                                <label class="mc-ipcam-check"><input id="ipcamCtlAllowHttp" type="checkbox"${c.allow_http ? ' checked' : ''}> ${esc(t('ipcamera.control_allow_http'))}</label>
                                <div class="mc-ipcam-hint mc-ipcam-hint-warn">${esc(t('ipcamera.control_http_warning'))}</div>
                            </div>
                        </div>
                    </fieldset>
                    <div class="mc-ipcam-actions">
                        <button type="button" class="device-action-btn device-action-secondary" data-ipcam-form="cancel">${esc(t('ipcamera.cancel'))}</button>
                        <button type="submit" class="device-action-btn device-action-primary is-primary" data-ipcam-form="save">${esc(t('ipcamera.save'))}</button>
                    </div>
                </form>
            </div>`;

        const profileSelect = overlay.querySelector('#ipcamProfile');
        const profileHint = overlay.querySelector('#ipcamProfileHint');
        const syncProfileHint = () => { profileHint.hidden = profileSelect.value !== 'main'; };
        profileSelect.addEventListener('change', syncProfileHint);
        syncProfileHint();
        const ctlType = overlay.querySelector('#ipcamCtlType');
        const ctlScheme = overlay.querySelector('#ipcamCtlScheme');
        const ctlPort = overlay.querySelector('#ipcamCtlPort');
        const syncControl = () => {
            overlay.querySelector('#ipcamCtlBody').hidden = ctlType.value === 'none';
            overlay.querySelector('#ipcamCtlHttpBox').hidden = ctlScheme.value !== 'http';
        };
        ctlType.addEventListener('change', syncControl);
        ctlScheme.addEventListener('change', () => {
            // convenience only: the usual port of the chosen scheme, unless the user typed another one
            if (ctlScheme.value === 'http' && ctlPort.value === '443') ctlPort.value = '80';
            if (ctlScheme.value === 'https' && ctlPort.value === '80') ctlPort.value = '443';
            syncControl();
        });
        syncControl();
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
        control: 'ipcamCtlType', control_port: 'ipcamCtlPort', control_channel: 'ipcamCtlChannel',
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
        if (value('ipcamCtlType') === 'reolink') {
            payload.control = {
                type: 'reolink',
                scheme: value('ipcamCtlScheme'),
                port: parseInt(value('ipcamCtlPort'), 10) || 0,
                channel: parseInt(value('ipcamCtlChannel'), 10) || 0,
                allow_http: overlay.querySelector('#ipcamCtlAllowHttp').checked,
            };
        } else {
            payload.control = { type: 'none' };
        }
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
        const text = errorText(result.body, result.status);
        const target = fieldId && overlay.querySelector('#' + fieldId + 'Error');
        if (target) {
            target.textContent = text;
            overlay.querySelector('#' + fieldId).focus();
        } else {
            showToast(text, 'error', { duration: 10000 });
        }
    }

    window.MCCameraSources = { mount, render, openForm, errorText, _errorKeys: ERROR_KEYS, _request: request, controlErrorText, formatFingerprint, featureList, probeControl };
})();
