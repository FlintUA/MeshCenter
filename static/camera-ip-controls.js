// ============================================================
// Network camera controls in the Camera card (CAM2 Phase 3b).
//
// A network camera has no CSI/USB mode or image controls (they stay hidden, see camera-ip.js), so this module
// renders what such a camera really offers, below the picture:
//
//   Mode  - what the stream is (resolution, frame rate, codec, bitrate, state), the Snapshot button with its
//           quality setting (full / small), and the note that image settings are changed in the camera app.
//   Lens  - only when the camera reported zoom (a saved "Check control" result): a zoom slider in the camera's
//           own steps. The position is read when the tab opens and after each move, never by polling. The slider
//           is disabled while the lens moves; drags made meanwhile coalesce (the latest target wins).
//
// Nothing here talks to the camera itself: it calls MeshCenter's own routes (/api/camera/sources/<id>/control/*,
// /api/camera/screenshot). The pure parts (the lens controller, the snapshot error mapping) are exported on
// window.MCIpControls for tests/frontend/test_camera_ip_controls.mjs.
// ============================================================
(function () {
    'use strict';

    const SOURCES = '/api/camera/sources/';
    const SCREENSHOT_URL = '/api/camera/screenshot';
    const REREAD_AFTER_MS = 2500;

    function t(key, params) {
        return window.I18N.t(key, params);
    }

    // ---------- pure logic ----------

    // Lens position state machine. `read()` -> {position, range:[min,max]|null, focus}; `move(target)` ->
    // {position, settled}. Both may throw an object with `error_code` / `right`. A request made while the lens is
    // moving (or being read) replaces any earlier waiting one: the latest target wins, and it is sent when the
    // current move is over. A permission error ends the control (no more commands are sent).
    function createLensController(options) {
        const state = { phase: 'idle', position: null, range: null, focus: null, target: null, settled: true, error: null, denied: false };
        let pending = null;
        let busy = false;

        function emit() {
            if (typeof options.onChange === 'function') options.onChange(Object.assign({}, state));
        }

        function fail(error) {
            state.phase = 'error';
            state.error = { code: (error && error.error_code) || 'error', right: (error && error.right) || '', status: error && error.status };
            if (error && error.error_code === 'permission_denied') state.denied = true;
        }

        function apply(result) {
            if (result.range) state.range = result.range;
            if (result.focus != null) state.focus = result.focus;
            if (result.position != null) state.position = result.position;
        }

        async function drain() {
            while (pending !== null && !busy && !state.denied) {
                const target = pending;
                pending = null;
                if (state.range && (target < state.range[0] || target > state.range[1])) continue;
                if (target === state.position && state.settled) continue;
                busy = true;
                state.phase = 'moving';
                state.target = target;
                state.error = null;
                emit();
                try {
                    const result = await options.move(target);
                    apply(result);
                    state.settled = result.settled !== false;
                    state.phase = 'ready';
                } catch (error) {
                    fail(error);
                }
                busy = false;
                state.target = null;
                emit();
            }
        }

        return {
            state,
            // read the position (tab opened, or once more after a lens that had not arrived)
            async refresh() {
                if (busy || state.denied) return;
                busy = true;
                state.phase = 'reading';
                state.error = null;
                emit();
                try {
                    apply(await options.read());
                    state.settled = true;
                    state.phase = 'ready';
                } catch (error) {
                    fail(error);
                }
                busy = false;
                emit();
                await drain();
            },
            // the user chose a position (slider released)
            request(target) {
                if (!Number.isInteger(target) || state.denied) return Promise.resolve();
                pending = target;
                return busy ? Promise.resolve() : drain();
            },
            busy() { return busy; },
        };
    }

    // The message for a failed /api/camera/screenshot call: an i18n key plus its parameters.
    const SNAPSHOT_ERRORS = {
        rate_limited: 'ipctl.snap_err_rate_limited',
        storage_full: 'ipctl.snap_err_storage_full',
        preview_not_running: 'ipctl.snap_err_preview_not_running',
        camera_off: 'ipctl.snap_err_camera_off',
        ffmpeg_missing: 'ipctl.snap_err_ffmpeg_missing',
        frame_grab_failed: 'ipctl.snap_err_frame_grab_failed',
        storage_write_failed: 'ipctl.snap_err_storage_write_failed',
    };
    const CONTROL_CODES = ['permission_denied', 'login_failed', 'too_many_sessions', 'unreachable', 'timeout', 'tls_error',
        'tls_untrusted', 'tls_pin_mismatch', 'http_not_allowed', 'address_blocked', 'not_supported'];

    function snapshotError(body) {
        const code = body && body.error_code;
        if (SNAPSHOT_ERRORS[code]) {
            const params = code === 'rate_limited' ? { seconds: Math.max(1, Math.ceil(Number(body.retry_after_s) || 2)) } : {};
            return { key: SNAPSHOT_ERRORS[code], params };
        }
        if (CONTROL_CODES.indexOf(code) !== -1) return { control: true, body };
        return { key: 'ipctl.snap_err_generic', params: { code: code || '-' } };
    }

    function errorMessage(body) {
        const failure = snapshotError(body);
        if (failure.control) {
            const sources = window.MCCameraSources;
            return sources && sources.controlErrorText ? sources.controlErrorText(body) : t('ipctl.snap_err_generic', { code: body.error_code });
        }
        return t(failure.key, Object.keys(failure.params).length ? failure.params : undefined);
    }

    // ---------- DOM helpers (text only: no innerHTML) ----------

    function el(tag, attrs, children) {
        const node = document.createElement(tag);
        Object.keys(attrs || {}).forEach((name) => {
            if (name === 'text') node.textContent = attrs[name];
            else if (name === 'class') node.className = attrs[name];
            else node.setAttribute(name, attrs[name]);
        });
        (children || []).forEach((child) => { if (child) node.appendChild(child); });
        return node;
    }

    function byId(id) {
        return document.getElementById(id);
    }

    // ---------- the panel ----------

    let built = null;           // {signature, nodes...}
    let activeTab = 'mode';
    let lens = null;
    let rereadTimer = null;
    let localQuality = null;     // what the user just chose, until the status catches up
    let snapshotBusy = false;
    let lastStatus = null;

    function signatureOf(status) {
        return [status.source_id, status.control_type, Boolean(status.zoom_control)].join('|');
    }

    function infoRow(labelKey, valueId) {
        return el('div', { class: 'ip-info-row' }, [
            el('span', { class: 'ip-info-label', text: t(labelKey) }),
            el('span', { class: 'ip-info-value', id: valueId, text: '-' }),
        ]);
    }

    function build(status, container) {
        container.textContent = '';
        const hasLens = Boolean(status.zoom_control);
        const hasApi = status.control_type === 'reolink';
        const tabs = el('div', { class: 'ip-camera-tabs', role: 'tablist', 'aria-label': t('ipctl.tablist_label') }, [
            el('button', { type: 'button', class: 'camera-control-tab', id: 'ipTabMode', role: 'tab', 'aria-controls': 'ipPanelMode', text: t('ipctl.tab_mode') }),
            hasLens ? el('button', { type: 'button', class: 'camera-control-tab', id: 'ipTabLens', role: 'tab', 'aria-controls': 'ipPanelLens', text: t('ipctl.tab_lens') }) : null,
        ]);

        const quality = hasApi ? el('select', { id: 'ipSnapshotQuality', class: 'ip-select', 'aria-label': t('ipctl.quality_label') }, [
            el('option', { value: 'full', text: t('ipctl.quality_full') }),
            el('option', { value: 'small', text: t('ipctl.quality_small') }),
        ]) : null;
        const modePanel = el('div', { class: 'ip-panel', id: 'ipPanelMode', role: 'tabpanel', 'aria-labelledby': 'ipTabMode' }, [
            el('div', { class: 'ip-info-grid' }, [
                infoRow('ipctl.info_stream', 'ipInfoStream'),
                infoRow('ipctl.info_resolution', 'ipInfoResolution'),
                infoRow('ipctl.info_fps', 'ipInfoFps'),
                infoRow('ipctl.info_codec', 'ipInfoCodec'),
                infoRow('ipctl.info_bitrate', 'ipInfoBitrate'),
                infoRow('ipctl.info_state', 'ipInfoState'),
            ]),
            el('div', { class: 'ip-section' }, [
                el('div', { class: 'ip-section-title', text: t('ipctl.snapshot_title') }),
                el('div', { class: 'ip-snapshot-row' }, [
                    el('button', { type: 'button', class: 'device-action-btn device-action-primary is-primary ip-snapshot-btn', id: 'ipSnapshotBtn', text: t('ipctl.snapshot_button') }),
                    hasApi ? el('label', { class: 'ip-quality', for: 'ipSnapshotQuality' }, [el('span', { text: t('ipctl.quality_label') }), quality]) : null,
                ]),
                el('div', { class: 'ip-hint', text: t(hasApi ? 'ipctl.snapshot_hint_api' : 'ipctl.snapshot_hint_stream') }),
            ]),
            el('div', { class: 'ip-hint ip-image-note', text: t('ipctl.image_note') }),
        ]);

        const slider = el('input', { type: 'range', id: 'ipLensSlider', class: 'ip-lens-slider', min: '0', max: '0', step: '1', value: '0', 'aria-label': t('ipctl.lens_slider_label') });
        slider.disabled = true;
        const lensPanel = hasLens ? el('div', { class: 'ip-panel', id: 'ipPanelLens', role: 'tabpanel', 'aria-labelledby': 'ipTabLens' }, [
            el('div', { class: 'ip-lens-value', id: 'ipLensValue', text: '-' }),
            slider,
            el('div', { class: 'ip-lens-focus', id: 'ipLensFocus', text: '' }),
            el('div', { class: 'ip-hint', text: t('ipctl.lens_hint') }),
            el('div', { class: 'ip-lens-status', id: 'ipLensStatus', role: 'status', 'aria-live': 'polite', text: '' }),
        ]) : null;

        container.appendChild(tabs);
        container.appendChild(modePanel);
        if (lensPanel) container.appendChild(lensPanel);

        byId('ipTabMode').addEventListener('click', () => selectTab('mode'));
        if (hasLens) byId('ipTabLens').addEventListener('click', () => selectTab('lens'));
        byId('ipSnapshotBtn').addEventListener('click', takePicture);
        if (hasApi) byId('ipSnapshotQuality').addEventListener('change', (event) => saveQuality(status.source_id, event.target.value));
        if (hasLens) {
            const input = byId('ipLensSlider');
            // the label follows the thumb while dragging; the camera is only asked on release
            input.addEventListener('input', () => renderLensValue(Number(input.value), true));
            input.addEventListener('change', () => lens && lens.request(Number(input.value)));
            lens = createLensController({
                read: () => lensRead(status.source_id),
                move: (target) => lensMove(status.source_id, target),
                onChange: renderLens,
            });
        } else {
            lens = null;
        }
        built = { signature: signatureOf(status), sourceId: status.source_id, hasLens, hasApi };
        if (activeTab === 'lens' && !hasLens) activeTab = 'mode';
        selectTab(activeTab, true);
    }

    function selectTab(name, silent) {
        activeTab = name === 'lens' && built && built.hasLens ? 'lens' : 'mode';
        const modeTab = byId('ipTabMode');
        const lensTab = byId('ipTabLens');
        const modePanel = byId('ipPanelMode');
        const lensPanel = byId('ipPanelLens');
        const showLens = activeTab === 'lens';
        if (modeTab) { modeTab.classList.toggle('active', !showLens); modeTab.setAttribute('aria-selected', String(!showLens)); }
        if (lensTab) { lensTab.classList.toggle('active', showLens); lensTab.setAttribute('aria-selected', String(showLens)); }
        if (modePanel) modePanel.hidden = showLens;
        if (lensPanel) lensPanel.hidden = !showLens;
        if (showLens && lens && !silent) lens.refresh(); // read when the tab is opened
        if (showLens && lens && silent && !lens.busy() && lens.state.position === null) lens.refresh();
    }

    // ---------- Mode: info and status ----------

    function setText(id, text) {
        const node = byId(id);
        if (node && node.textContent !== text) node.textContent = text;
    }

    function renderInfo(status) {
        setText('ipInfoStream', status.profile === 'main' ? t('ipcamera.profile_main') : t('ipcamera.profile_sub'));
        setText('ipInfoResolution', status.resolution || '-');
        setText('ipInfoFps', status.fps ? status.fps + ' FPS' : '-');
        setText('ipInfoCodec', status.codec ? String(status.codec).toUpperCase() : '-');
        setText('ipInfoBitrate', status.bitrate_kbps ? status.bitrate_kbps + ' kbps' : '-');
        const helpers = window.MCIpCamera && window.MCIpCamera._helpers;
        let text = '-';
        if (helpers) text = helpers.isTerminal(status) ? t('ipcamera.state_error') : t(helpers.stateMessageKey(status));
        setText('ipInfoState', text);
        const quality = byId('ipSnapshotQuality');
        if (quality) {
            const wanted = localQuality || status.snapshot_quality || 'full';
            if (localQuality && localQuality === status.snapshot_quality) localQuality = null;
            if (quality.value !== wanted) quality.value = wanted;
        }
    }

    // ---------- Mode: the picture ----------

    async function takePicture() {
        if (snapshotBusy) return;
        const button = byId('ipSnapshotBtn');
        snapshotBusy = true;
        if (button) { button.disabled = true; button.textContent = t('ipctl.snapshot_busy'); }
        try {
            const response = await fetch(SCREENSHOT_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            let body = null;
            try { body = await response.json(); } catch (error) { body = null; }
            if (response.ok && body && body.ok) {
                if (typeof mediaGalleryLoaded !== 'undefined') mediaGalleryLoaded = false; // the gallery lists it next time
                showToast(t(body.method === 'stream_frame' && lastStatus && lastStatus.control_type === 'reolink' ? 'ipctl.snapshot_saved_stream' : 'ipctl.snapshot_saved'), 'success');
            } else {
                showToast(errorMessage(body || {}), 'error', { duration: 10000 });
            }
        } catch (error) {
            showToast(t('errors.network_error'), 'error', { duration: 10000 });
        } finally {
            snapshotBusy = false;
            if (button) { button.disabled = false; button.textContent = t('ipctl.snapshot_button'); }
        }
    }

    async function saveQuality(sourceId, value) {
        const select = byId('ipSnapshotQuality');
        const previous = (lastStatus && lastStatus.snapshot_quality) || 'full';
        localQuality = value;
        try {
            const response = await fetch(SOURCES + encodeURIComponent(sourceId) + '/control/settings', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ snapshot_quality: value }),
            });
            const body = await response.json().catch(() => ({}));
            if (!response.ok || !body.ok) throw new Error(body.error_code || 'failed');
        } catch (error) {
            localQuality = null;
            if (select) select.value = previous;
            showToast(t('ipctl.quality_save_failed'), 'error', { duration: 10000 });
        }
    }

    // ---------- Lens ----------

    async function lensCall(url, options) {
        const response = await fetch(url, options);
        let body = null;
        try { body = await response.json(); } catch (error) { body = null; }
        if (!response.ok || !body || !body.ok) {
            const failure = Object.assign({}, body || {});
            failure.status = response.status;
            throw failure;
        }
        return body;
    }

    async function lensRead(sourceId) {
        const body = await lensCall(SOURCES + encodeURIComponent(sourceId) + '/control/zoom', { cache: 'no-store' });
        return { position: body.zoom.position, range: body.zoom.range, focus: body.zoom.focus_position };
    }

    async function lensMove(sourceId, target) {
        const body = await lensCall(SOURCES + encodeURIComponent(sourceId) + '/control/zoom', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pos: target }),
        });
        return { position: body.position, settled: body.settled, range: body.zoom_range };
    }

    function renderLensValue(value, dragging) {
        const range = lens && lens.state.range;
        if (value == null || !range) {
            setText('ipLensValue', '-');
            return;
        }
        setText('ipLensValue', t(dragging ? 'ipctl.lens_zoom_target' : 'ipctl.lens_zoom', { position: value, max: range[1] }));
    }

    function renderLens(state) {
        const slider = byId('ipLensSlider');
        if (!slider) return;
        if (state.range) {
            slider.min = String(state.range[0]);
            slider.max = String(state.range[1]);
        }
        const shown = state.phase === 'moving' && state.target != null ? state.target : state.position;
        if (shown != null && document.activeElement !== slider) slider.value = String(shown);
        slider.disabled = state.phase === 'reading' || state.phase === 'moving' || state.denied || state.position === null;
        renderLensValue(state.phase === 'moving' ? state.target : state.position, state.phase === 'moving');
        setText('ipLensFocus', state.focus != null ? t('ipctl.lens_focus', { position: state.focus }) : '');
        let message = '';
        if (state.phase === 'reading') message = t('ipctl.lens_reading');
        else if (state.phase === 'moving') message = t('ipctl.lens_moving');
        else if (state.error) message = state.denied ? t('ipcamera.control_denied', { right: rightLabel(state.error.right) }) : errorMessage({ error_code: state.error.code, right: state.error.right });
        else if (!state.settled) message = t('ipctl.lens_not_arrived');
        setText('ipLensStatus', message);
        const status = byId('ipLensStatus');
        if (status) status.classList.toggle('ip-lens-error', Boolean(state.error));
        if (rereadTimer) { clearTimeout(rereadTimer); rereadTimer = null; }
        if (state.phase === 'ready' && !state.settled && lens) {
            // the camera had not finished: ONE more reading shortly after, never a polling loop
            rereadTimer = setTimeout(() => { rereadTimer = null; if (lens && lens.state.phase === 'ready') lens.refresh(); }, REREAD_AFTER_MS);
        }
    }

    function rightLabel(right) {
        const map = { snapshot: 'ipcamera.control_right_snapshot', lens_control: 'ipcamera.control_right_lens_control', live_view: 'ipcamera.control_right_live_view' };
        return t(map[right] || 'ipcamera.control_right_unknown');
    }

    // ---------- entry point (called by camera-ip.js with every status) ----------

    function hide() {
        const container = byId('ipCameraControls');
        if (container) container.hidden = true;
        if (rereadTimer) { clearTimeout(rereadTimer); rereadTimer = null; }
    }

    function update(status) {
        const container = byId('ipCameraControls');
        if (!container) return;
        if (!status || status.source_type !== 'rtsp' || !status.source_id) {
            hide();
            built = null;
            lens = null;
            return;
        }
        lastStatus = status;
        container.hidden = false;
        if (!built || built.signature !== signatureOf(status)) build(status, container);
        renderInfo(status);
    }

    window.MCIpControls = {
        update, hide, selectTab,
        // exported for tests
        createLensController, snapshotError, errorMessage, _state: () => ({ built, activeTab, lens }),
    };
})();
